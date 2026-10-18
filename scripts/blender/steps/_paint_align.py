"""Non-rigid silhouette alignment of a concept image to the model silhouette (paint_from_concept, align: warp).

The rigid camera fit (yaw / pitch / scale / offset / perspective) cannot absorb what a generator changes
non-rigidly: the arm angle, the helmet size, the sleeve length. A projection through the rigid camera then
puts the colours of one part on the neighbouring part. This module warps the CONCEPT (colour + alpha + masks)
so that its silhouette lies on the silhouette the model has through that camera:

  * boundary points of both masks (outer contours and holes), subsampled evenly along the contour;
  * ICP-style loop: map the model boundary through the current thin-plate spline, match it to the nearest
    concept boundary point (and the concept boundary back to the nearest mapped model point), robust Huber
    weights, refit a regularised TPS; matches farther than the cap are ignored;
  * the TPS maps MODEL pixels to CONCEPT positions: warped(p) = concept(p + d(p)); it is evaluated on a coarse
    grid (1/8 of the resolution), upsampled bilinearly, capped at align_max x subject height and the concept
    is resampled bilinearly.

numpy only (no scipy / cv2 in Blender's Python); no bpy import, so the maths is testable outside Blender.
Arrays follow _paint_lib: images (h, w, c) top row first, pixel (row i, col j) has its centre at
(x, y) = (j + 0.5, i + 0.5).
"""
import time

import numpy as np


# --------------------------------------------------------------------------- masks / points
def _shift(a, dy, dx):
    out = np.zeros_like(a)
    h, w = a.shape
    if abs(dy) < h and abs(dx) < w:
        ys, yd = (slice(0, h - dy), slice(dy, h)) if dy >= 0 else (slice(-dy, h), slice(0, h + dy))
        xs, xd = (slice(0, w - dx), slice(dx, w)) if dx >= 0 else (slice(-dx, w), slice(0, w + dx))
        out[yd, xd] = a[ys, xs]
    return out


def close_box(mask, r):
    """Morphological closing with a (2r+1) square (integral images, O(pixels) for any r): fills holes and
    gaps narrower than 2r + 1. The contour matched by the warp should not follow every ragged tip."""
    r = int(r)
    m = np.asarray(mask, dtype=bool)
    if r <= 0:
        return m
    k = 2 * r + 1

    def box_sum(a):
        c = np.pad(a.astype(np.int32).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
        return c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]
    h, w = m.shape
    pad = np.pad(m, 2 * r)                       # a margin, so the image border does not erode the shape
    dil = box_sum(np.pad(pad, r)) > 0
    ero = box_sum(np.pad(dil, r)) == k * k
    return ero[2 * r:2 * r + h, 2 * r:2 * r + w]


def boundary_points(mask, max_n):
    """Boundary pixels (mask pixels with a 4-neighbour outside the mask, image edges count as outside) as
    (n, 2) float64 (x, y) pixel centres, subsampled evenly along the row-major order (~ even in arc length)."""
    m = np.asarray(mask, dtype=bool)
    inner = m.copy()
    for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        inner &= _shift(m, dy, dx)
    ys, xs = np.nonzero(m & ~inner)
    n = len(xs)
    if n == 0:
        return np.zeros((0, 2), dtype=np.float64)
    if n > max_n:
        idx = np.linspace(0, n - 1, int(max_n)).astype(np.int64)
        xs, ys = xs[idx], ys[idx]
    return np.stack([xs + 0.5, ys + 0.5], axis=1).astype(np.float64)


def mask_iou(a, b):
    return float(np.logical_and(a, b).sum()) / float(max(np.logical_or(a, b).sum(), 1))


# --------------------------------------------------------------------------- thin-plate spline
def _sqdist(a, b):
    """Pairwise squared distances (n, m) through one matrix product (BLAS), clipped at 0."""
    d = (a * a).sum(1)[:, None] + (b * b).sum(1)[None, :] - 2.0 * (a @ b.T)
    return np.maximum(d, 0.0)


def _phi(r2):
    """TPS kernel r^2 log r = 0.5 r^2 log r^2 (0 at r = 0)."""
    return 0.5 * r2 * np.log(np.maximum(r2, 1e-18))


class TPS:
    """Weighted, regularised 2D thin-plate spline g: R^2 -> R^2 (here: the displacement of a pixel).
    minimises  sum w_i |g(src_i) - dst_i|^2 + lam * bending energy  (affine part unpenalised)."""

    def __init__(self, src, dst, w, lam):
        n = len(src)
        K = _phi(_sqdist(src, src))
        K[np.diag_indices(n)] += lam / np.maximum(w, 1e-6)
        P = np.concatenate([np.ones((n, 1)), src], axis=1)
        A = np.zeros((n + 3, n + 3))
        A[:n, :n] = K
        A[:n, n:] = P
        A[n:, :n] = P.T
        rhs = np.zeros((n + 3, 2))
        rhs[:n] = dst
        try:
            sol = np.linalg.solve(A, rhs)
        except np.linalg.LinAlgError:
            sol = np.linalg.lstsq(A, rhs, rcond=None)[0]
        self.src = src
        self.wgt = sol[:n]
        self.aff = sol[n:]          # rows: constant, x, y

    def __call__(self, q, chunk=8192):
        q = np.asarray(q, dtype=np.float64)
        out = np.empty((len(q), 2))
        for a in range(0, len(q), chunk):
            c = q[a:a + chunk]
            out[a:a + chunk] = _phi(_sqdist(c, self.src)) @ self.wgt + self.aff[0] + c @ self.aff[1:]
        return out


# --------------------------------------------------------------------------- the alignment
def interior_points(mask, radius_px, n, step=8):
    """Up to n points deep inside `mask` (farther than radius_px from its boundary), evenly subsampled:
    anchors that keep the warp from moving the middle of a thick part (torso) because its contour is noisy."""
    s = max(1, int(step))
    small = np.asarray(mask, dtype=bool)[s // 2::s, s // 2::s]
    inner = small.copy()
    for _ in range(max(1, int(round(radius_px / s)))):
        nxt = inner.copy()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            nxt &= _shift(inner, dy, dx)
        if not nxt.any():
            break
        inner = nxt
    ys, xs = np.nonzero(inner)
    if len(xs) == 0:
        return np.zeros((0, 2), dtype=np.float64)
    if len(xs) > n:
        idx = np.linspace(0, len(xs) - 1, int(n)).astype(np.int64)
        xs, ys = xs[idx], ys[idx]
    return np.stack([xs * s + s // 2 + 0.5, ys * s + s // 2 + 0.5], axis=1).astype(np.float64)


def estimate_warp(model_mask, concept_mask, subject_h, align_max=0.06, smooth=2.0, iters=4, max_points=450,
                  grid_step=8, anchor_radius=0.04, anchor_weight=1.0, anchor_points=250, close_radius=0.008):
    """Displacement field d (h, w, 2) float32 in pixels such that concept(p + d(p)) lies on the model at p.

    model_mask / concept_mask: bool (h, w), subject_h: concept subject height in px (units of align_max /
    smooth / anchor_radius). The stiffness starts 3^(iters-1) times higher and relaxes to `smooth` (coarse to
    fine: the pose first, the ragged contour last); anchor points deep inside the model silhouette pin zero
    displacement. Returns (field or None, info); None when a mask has no boundary (nothing to align)."""
    t0 = time.time()
    h, w = concept_mask.shape
    info = {"iterations": 0, "control_points": 0}
    H = max(float(subject_h), 8.0)
    rc = int(round(close_radius * H))
    M = boundary_points(close_box(model_mask, rc), max_points)
    C = boundary_points(close_box(concept_mask, rc), max_points)
    if len(M) < 8 or len(C) < 8:
        info["skipped"] = "mask without a boundary"
        return None, info
    cap = align_max * H
    delta = max(2.0, 0.01 * H)
    centre = np.array([w / 2.0, h / 2.0])
    Mn, Cn = (M - centre) / H, (C - centre) / H           # normalised: unit = subject height
    A = np.zeros((0, 2))
    if anchor_weight > 0 and anchor_points > 0:
        A = (interior_points(model_mask, anchor_radius * H, anchor_points, grid_step) - centre) / H
    info["anchors"] = int(len(A))
    # the penalty must grow with the number of constraints (a 3e-3 default at 600 + 600 points)
    lam0 = float(smooth) * 3e-3 * (0.5 * (len(M) + len(C)) / float(max_points))
    n_it = max(1, int(iters))
    cur = Mn.copy()                                       # model boundary through the current warp
    tps = None
    rms = []
    for it in range(n_it):
        lam = lam0 * 3.0 ** (n_it - 1 - it)
        D = _sqdist(cur, Cn)                                             # (n_model, n_concept)
        j = D.argmin(axis=1)
        k = D.argmin(axis=0)
        r_m = np.sqrt(D[np.arange(len(cur)), j]) * H                     # px
        r_c = np.sqrt(D[k, np.arange(len(Cn))]) * H
        src = np.concatenate([Mn, Mn[k]])
        dst = np.concatenate([Cn[j], Cn])
        r = np.concatenate([r_m, r_c])
        wt = np.where(r <= delta, 1.0, delta / np.maximum(r, 1e-9))     # Huber
        wt = np.where(r > cap, 0.0, wt)                                  # beyond the cap: not a match
        keep = wt > 1e-3
        if keep.sum() < 8:
            info["skipped"] = "no matches within the cap"
            break
        s_all = np.concatenate([src[keep], A])
        d_all = np.concatenate([(dst - src)[keep], np.zeros((len(A), 2))])
        w_all = np.concatenate([wt[keep], np.full(len(A), float(anchor_weight))])
        tps = TPS(s_all, d_all, w_all, lam)                              # displacement model -> concept
        cur = Mn + tps(Mn)
        rms.append(float(np.sqrt((r_m ** 2).mean())))
        info["iterations"] = it + 1
        info["control_points"] = int(len(s_all))
    if tps is None:
        return None, info
    info["edge_rms_px"] = [round(x, 2) for x in rms]
    # evaluate on a coarse grid, cap, upsample bilinearly
    s = max(1, int(grid_step))
    gh, gw = h // s + 2, w // s + 2
    gy, gx = np.mgrid[0:gh, 0:gw]
    pts = np.stack([gx.ravel() * s, gy.ravel() * s], axis=1).astype(np.float64)
    dn = tps((pts - centre) / H) * H                                      # px
    mag = np.sqrt((dn ** 2).sum(-1))
    scale = np.minimum(1.0, cap / np.maximum(mag, 1e-9))
    bmag = np.sqrt((tps(Mn) ** 2).sum(-1)) * H
    info["capped_share"] = round(float((bmag > cap).mean()), 4)       # model boundary points that wanted more
    dn *= scale[:, None]
    grid = dn.reshape(gh, gw, 2).astype(np.float32)
    # folds / squeezes: Jacobian determinant of p + d(p) at the grid nodes inside the model silhouette
    dxx, dxy = np.gradient(grid[..., 0], s, axis=1), np.gradient(grid[..., 0], s, axis=0)
    dyx, dyy = np.gradient(grid[..., 1], s, axis=1), np.gradient(grid[..., 1], s, axis=0)
    det = (1.0 + dxx) * (1.0 + dyy) - dxy * dyx
    inside = np.asarray(model_mask, dtype=bool)[np.minimum(gy * s, h - 1), np.minimum(gx * s, w - 1)]
    if inside.any():
        info["jacobian_min"] = round(float(det[inside].min()), 3)
        info["squeezed_share"] = round(float((det[inside] < 0.5).mean()), 4)
    field = upsample_field(grid, s, h, w)
    info["seconds"] = round(time.time() - t0, 2)
    return field, info


def upsample_field(grid, step, h, w):
    """Bilinear upsample of a coarse (gh, gw, 2) field sampled at pixel coordinates (k * step) to (h, w, 2)."""
    gh, gw = grid.shape[:2]
    gx = np.clip((np.arange(w, dtype=np.float32) + 0.5) / step, 0, gw - 1.001)
    gy = np.clip((np.arange(h, dtype=np.float32) + 0.5) / step, 0, gh - 1.001)
    x0 = gx.astype(np.int64)
    y0 = gy.astype(np.int64)
    ax = (gx - x0)[None, :, None]
    ay = (gy - y0)[:, None, None]
    tmp = grid[:, x0] * (1 - ax) + grid[:, x0 + 1] * ax                  # (gh, w, 2)
    return (tmp[y0] * (1 - ay) + tmp[y0 + 1] * ay).astype(np.float32)


def sample_bilinear(img, x, y):
    """img (h, w, c) sampled at pixel-centre coordinates x, y (any shape, edges clamped) -> (..., c)."""
    h, w = img.shape[:2]
    fx = np.clip(x - 0.5, 0, w - 1.001).astype(np.float32)
    fy = np.clip(y - 0.5, 0, h - 1.001).astype(np.float32)
    x0 = fx.astype(np.int64)
    y0 = fy.astype(np.int64)
    ax = (fx - x0)[..., None]
    ay = (fy - y0)[..., None]
    top = img[y0, x0] * (1 - ax) + img[y0, x0 + 1] * ax
    bot = img[y0 + 1, x0] * (1 - ax) + img[y0 + 1, x0 + 1] * ax
    return (top * (1 - ay) + bot * ay).astype(np.float32)


def apply_field(img, field):
    """warped(p) = img(p + d(p)) for every pixel centre p; img (h, w, c), field (h, w, 2) -> (h, w, c)."""
    h, w = img.shape[:2]
    X = np.arange(w, dtype=np.float32)[None, :] + 0.5 + field[..., 0]
    Y = np.arange(h, dtype=np.float32)[:, None] + 0.5 + field[..., 1]
    return sample_bilinear(img, X, Y)


def warp_concept(px, fit_mask, col_mask, field, dilate_colors):
    """Warp the concept (straight RGBA, top first) and its two masks by `field`.

    The colours are first padded outward from col_mask (dilate_colors(rgb, mask, passes) of _paint_lib), so the
    resampling at the new silhouette edge never mixes the background in. Returns (px_warped, fit_warped,
    col_warped)."""
    rgb, _ok = dilate_colors(px[..., :3], col_mask, 6)
    stack = np.concatenate([rgb, px[..., 3:4], fit_mask[..., None].astype(np.float32),
                            col_mask[..., None].astype(np.float32)], axis=2)
    out = apply_field(stack, field)
    px_w = np.concatenate([out[..., :3], out[..., 3:4]], axis=2)
    return px_w, out[..., 4] > 0.5, out[..., 5] > 0.5


def field_stats(field, region, subject_h):
    """Displacement statistics (% of the subject height) over the pixels of `region` (bool mask)."""
    if not region.any():
        return {"mean_pct": 0.0, "p95_pct": 0.0, "max_pct": 0.0}
    mag = np.sqrt((field[region] ** 2).sum(-1)) / max(float(subject_h), 1.0) * 100.0
    return {"mean_pct": round(float(mag.mean()), 3), "p95_pct": round(float(np.percentile(mag, 95)), 3),
            "max_pct": round(float(mag.max()), 3)}


def align_concept(px, fit_mask, col_mask, model_mask, subject_h, dilate_colors, align_max=0.06, smooth=2.0,
                  iters=4, max_points=450, **extra):
    """Whole alignment: (px, fit_mask, col_mask, info, field). info carries IoU before / after (model vs
    concept silhouette), displacement statistics over the model silhouette (% of the subject height) and the
    TPS diagnostics; when nothing could be fitted (or the IoU would drop) the inputs are returned unchanged
    with info["applied"] False and field None."""
    iou0 = mask_iou(model_mask, fit_mask)
    field, info = estimate_warp(model_mask, fit_mask, subject_h, align_max, smooth, iters, max_points, **extra)
    info.update({"applied": False, "iou_before": round(iou0, 4), "iou_after": round(iou0, 4),
                 "max_allowed_pct": round(100.0 * align_max, 3)})
    if field is None:
        return px, fit_mask, col_mask, info, None
    t0 = time.time()
    px_w, fit_w, col_w = warp_concept(px, fit_mask, col_mask, field, dilate_colors)
    iou1 = mask_iou(model_mask, fit_w)
    info["warp_seconds"] = round(time.time() - t0, 2)
    info["iou_after"] = round(iou1, 4)
    if iou1 + 1e-4 < iou0:
        info["skipped"] = "IoU would drop (%.4f -> %.4f)" % (iou0, iou1)
        return px, fit_mask, col_mask, info, None
    info["applied"] = True
    info["displacement"] = field_stats(field, model_mask | fit_mask, subject_h)
    info["displacement"].update({k: info[k] for k in ("jacobian_min", "squeezed_share") if k in info})
    return px_w, fit_w, col_w, info, field
