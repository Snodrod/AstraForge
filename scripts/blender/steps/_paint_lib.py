"""Helpers of the paint_from_concept step: colour a geometry-only mesh (Hunyuan3D 2.1 output) from
the concept image it was generated from.

Owned by the paint module (docs/forge/paint_from_concept.md). Blender 5.0.1, bundled Python 3.11 +
numpy only (no scipy / Pillow). Conventions:
  * concept images are float32 (h, w, 4) arrays, TOP row first, values as stored (sRGB-encoded);
    pixel (row i, col j) has its centre at (x, y) = (j + 0.5, i + 0.5), x right, y down;
  * textures are (T, T, c) arrays in Blender order, BOTTOM row first: texel (i, j) is the UV point
    ((j + 0.5) / T, (i + 0.5) / T);
  * the orthographic concept camera looks along d(yaw, pitch); yaw 0 / pitch 0 = along +Y, i.e. at the
    model's front (characters face -Y, contract section 4). Image x runs along +X (the character's left).
"""
import math
import os
import struct
import json

import bpy
import mathutils
import numpy as np

import af_common as af
from steps import _mesh_lib as ml

LUMA = np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)


# --------------------------------------------------------------------------- colour
def srgb_to_linear(c):
    c = np.asarray(c, dtype=np.float32)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4).astype(np.float32)


def linear_to_srgb(c):
    c = np.clip(np.asarray(c, dtype=np.float32), 0.0, None)
    return np.where(c <= 0.0031308, c * 12.92, 1.055 * np.power(c, 1 / 2.4) - 0.055).astype(np.float32)


def smoothstep(e0, e1, x):
    t = np.clip((x - e0) / max(e1 - e0, 1e-9), 0.0, 1.0)
    return t * t * (3 - 2 * t)


def rgb_to_hsv(rgb):
    """(..., 3) -> hue degrees [0, 360), saturation, value (all float32)."""
    mx = rgb.max(axis=-1)
    mn = rgb.min(axis=-1)
    d = mx - mn
    s = np.where(mx > 1e-6, d / np.maximum(mx, 1e-6), 0.0)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    dd = np.maximum(d, 1e-6)
    h = np.where(mx == r, ((g - b) / dd) % 6.0, np.where(mx == g, (b - r) / dd + 2.0, (r - g) / dd + 4.0))
    h = np.where(d > 1e-6, h * 60.0, 0.0)
    return h.astype(np.float32), s.astype(np.float32), mx.astype(np.float32)


# --------------------------------------------------------------------------- images
def load_rgba(path):
    """Any image Blender reads -> float32 (h, w, 4), top row first, stored values; alpha 1 when absent."""
    img = bpy.data.images.load(os.path.abspath(path), check_existing=False)
    try:
        try:
            img.colorspace_settings.name = "Non-Color"
        except TypeError:
            pass
        w, h = img.size
        if w == 0 or h == 0:
            raise ValueError("concept image is empty or unreadable: %s" % path)
        px = np.empty(w * h * 4, dtype=np.float32)
        img.pixels.foreach_get(px)
        channels = img.channels
    finally:
        bpy.data.images.remove(img)
    out = px.reshape(h, w, 4)[::-1].copy()
    if channels < 4:
        out[..., 3] = 1.0
    return out


def shift(a, dy, dx, fill):
    """a shifted by (dy, dx) cells (positive = towards higher indices); vacated cells = fill."""
    out = np.full_like(a, fill)
    h, w = a.shape[:2]
    ys, yd = (slice(0, h - dy), slice(dy, h)) if dy >= 0 else (slice(-dy, h), slice(0, h + dy))
    xs, xd = (slice(0, w - dx), slice(dx, w)) if dx >= 0 else (slice(-dx, w), slice(0, w + dx))
    if abs(dy) < h and abs(dx) < w:
        out[yd, xd] = a[ys, xs]
    return out


def dilate(m, r=1):
    out = m.copy()
    for _ in range(int(r)):
        cur = out.copy()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            out |= shift(cur, dy, dx, False)
    return out


def erode(m, r=1):
    return ~dilate(~m, r)


def close_mask(m, r=1):
    return erode(dilate(m, r), r)


def box_blur(a, r):
    """Square (2r+1) mean of a 2D float array, edges replicated (cumulative sums)."""
    r = int(r)
    if r <= 0:
        return a.astype(np.float32)
    p = np.pad(a.astype(np.float64), r, mode="edge")
    c = np.pad(p.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    k = 2 * r + 1
    s = c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]
    return (s / (k * k)).astype(np.float32)


def masked_blur(a, mask, r, passes=2):
    """Blur of a (h, w) or (h, w, c) array using only the pixels inside mask (normalised)."""
    m = mask.astype(np.float32)
    den = m
    num = a * (m[..., None] if a.ndim == 3 else m)
    for _ in range(passes):
        den = box_blur(den, r)
        if a.ndim == 3:
            num = np.stack([box_blur(num[..., k], r) for k in range(a.shape[2])], axis=-1)
        else:
            num = box_blur(num, r)
    d = np.maximum(den, 1e-6)
    return num / (d[..., None] if a.ndim == 3 else d)


def edge_distance(mask, k):
    """Per pixel: how many 4-neighbour erosions it survives (0 outside, capped at k)."""
    cur = mask.copy()
    dist = np.zeros(mask.shape, dtype=np.float32)
    for _ in range(int(k)):
        if not cur.any():
            break
        dist += cur
        cur = erode(cur, 1)
    return dist


def dilate_colors(rgb, mask, passes=4):
    """Pad colours outward from mask so bilinear sampling at the edge never mixes the background."""
    out = rgb.copy()
    ok = mask.copy()
    for _ in range(int(passes)):
        if ok.all():
            break
        s = np.zeros_like(out)
        c = np.zeros(ok.shape, dtype=np.float32)
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1)):
            so = shift(ok, dy, dx, False)
            s += np.where(so[..., None], shift(out, dy, dx, 0.0), 0.0)
            c += so
        new = (~ok) & (c > 0)
        out[new] = s[new] / c[new][:, None]
        ok |= new
    return out, ok


def label_components(mask):
    """8-connected components of a 2D bool mask (run-length union-find). Returns (labels, sizes):
    labels 1..n ordered by size (0 = background), sizes[i] = pixels of label i + 1."""
    h, w = mask.shape
    parent = [0]

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    rows = []
    prev = []
    for y in range(h):
        row = mask[y]
        if not row.any():
            rows.append([])
            prev = []
            continue
        d = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
        starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
        cur = []
        j = 0
        for s, e in zip(starts.tolist(), ends.tolist()):
            lab = 0
            while j < len(prev) and prev[j][1] < s:
                j += 1
            k = j
            while k < len(prev) and prev[k][0] <= e:
                pl = find(prev[k][2])
                if lab == 0:
                    lab = pl
                elif pl != lab:
                    a_, b_ = (lab, pl) if lab < pl else (pl, lab)
                    parent[b_] = a_
                    lab = a_
                k += 1
            if lab == 0:
                lab = len(parent)
                parent.append(lab)
            cur.append((s, e, lab))
        rows.append(cur)
        prev = cur
    labels = np.zeros((h, w), dtype=np.int32)
    sizes = {}
    for y, runs in enumerate(rows):
        for s, e, lab in runs:
            r = find(lab)
            labels[y, s:e] = r
            sizes[r] = sizes.get(r, 0) + (e - s)
    order = sorted(sizes, key=lambda r: -sizes[r])
    remap = np.zeros(len(parent) + 1, dtype=np.int32)
    for i, r in enumerate(order):
        remap[r] = i + 1
    return remap[labels], np.array([sizes[r] for r in order], dtype=np.int64)


def resize_mask(m, h_new):
    """Area-average resize of a bool mask to height h_new (aspect kept) -> (bool mask, factor)."""
    h0, w0 = m.shape
    f = h_new / float(h0)
    w_new = max(1, int(round(w0 * f)))
    ys = np.round(np.arange(h_new + 1) * h0 / float(h_new)).astype(int)
    xs = np.round(np.arange(w_new + 1) * w0 / float(w_new)).astype(int)
    c = np.pad(m.astype(np.float64).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    s = c[ys[1:]][:, xs[1:]] - c[ys[:-1]][:, xs[1:]] - c[ys[1:]][:, xs[:-1]] + c[ys[:-1]][:, xs[:-1]]
    area = np.outer(np.diff(ys), np.diff(xs)).astype(np.float64)
    return (s / np.maximum(area, 1)) > 0.5, (w_new / float(w0), h_new / float(h0))


def resize_area(img, w_new, h_new):
    """Area-average resize of an (h, w, c) float array (any direction; upsampling is nearest)."""
    h0, w0 = img.shape[:2]
    if w_new >= w0 and h_new >= h0:
        ys = np.minimum((np.arange(h_new) + 0.5) * h0 / h_new, h0 - 1).astype(int)
        xs = np.minimum((np.arange(w_new) + 0.5) * w0 / w_new, w0 - 1).astype(int)
        return img[ys][:, xs]
    ys = np.round(np.arange(h_new + 1) * h0 / float(h_new)).astype(int)
    xs = np.round(np.arange(w_new + 1) * w0 / float(w_new)).astype(int)
    ys[1:] = np.maximum(ys[1:], ys[:-1] + 1)
    xs[1:] = np.maximum(xs[1:], xs[:-1] + 1)
    ys = np.minimum(ys, h0)
    xs = np.minimum(xs, w0)
    out = np.zeros((h_new, w_new, img.shape[2]), dtype=np.float32)
    c = np.pad(img.astype(np.float64).cumsum(0).cumsum(1), ((1, 0), (1, 0), (0, 0)))
    s = c[ys[1:]][:, xs[1:]] - c[ys[:-1]][:, xs[1:]] - c[ys[1:]][:, xs[:-1]] + c[ys[:-1]][:, xs[:-1]]
    area = np.outer(np.maximum(np.diff(ys), 1), np.maximum(np.diff(xs), 1)).astype(np.float64)
    out[:] = s / area[..., None]
    return out


def bilinear(img, x, y):
    """Sample img (h, w, c) at float pixel coordinates (centres at +0.5), edges clamped."""
    h, w = img.shape[:2]
    fx = np.clip(np.asarray(x, dtype=np.float32) - 0.5, 0, w - 1)
    fy = np.clip(np.asarray(y, dtype=np.float32) - 0.5, 0, h - 1)
    x0 = np.minimum(fx.astype(np.int64), w - 2 if w > 1 else 0)
    y0 = np.minimum(fy.astype(np.int64), h - 2 if h > 1 else 0)
    ax = (fx - x0)[:, None]
    ay = (fy - y0)[:, None]
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    top = img[y0, x0] * (1 - ax) + img[y0, x1] * ax
    bot = img[y1, x0] * (1 - ax) + img[y1, x1] * ax
    return (top * (1 - ay) + bot * ay).astype(np.float32)


# --------------------------------------------------------------------------- concept analysis
def unpremultiply(px):
    """The studio's reference PNG stores colour x alpha (RGB <= alpha everywhere): divide it back
    where alpha >= 0.02 so the subject edge is not darkened. Straight-alpha images are left alone.
    Returns (px, premultiplied bool)."""
    a = px[..., 3]
    soft = (a > 0.02) & (a < 0.9)
    if soft.sum() < 50:
        return px, False
    over = (px[..., :3][soft].max(axis=1) > a[soft] + 2.5 / 255).mean()
    if over > 0.02:
        return px, False
    out = px.copy()
    k = a >= 0.02
    out[..., :3][k] = np.clip(px[..., :3][k] / a[k][:, None], 0, 1)
    return out, True


def merge_original(px, original, crop, content_rect):
    """Put the original image's RGB (straight colours, also under faint alpha) back into the
    reference frame: original[crop] lands at content_rect = [left, top, w, h] of the reference."""
    x0, y0, x1, y1 = [int(round(v)) for v in crop]
    left, top, cw, ch = [int(round(v)) for v in content_rect]
    sub = original[y0:y1, x0:x1, :3]
    if sub.shape[0] < 2 or sub.shape[1] < 2:
        return px, False
    if sub.shape[1] != cw or sub.shape[0] != ch:
        sub = resize_area(sub, cw, ch)
    out = px.copy()
    H, W = px.shape[:2]
    ty0, tx0 = max(0, top), max(0, left)
    ty1, tx1 = min(H, top + ch), min(W, left + cw)
    if ty1 <= ty0 or tx1 <= tx0:
        return px, False
    out[ty0:ty1, tx0:tx1, :3] = sub[ty0 - top:ty1 - top, tx0 - left:tx1 - left]
    return out, True


def concept_masks(px, extend=True):
    """Subject masks of the concept.

    fit  - alpha > 0.5 (or a border key when the image has no transparency), specks removed: the
           silhouette the camera is fitted to;
    color - fit plus faint but clearly coloured pixels (alpha >= 0.04, saturated, far from the
           background colour): rembg often drops a thin banner or a glow to a low alpha while the
           reference PNG keeps the original RGB there. Desaturated shadows on the floor stay out.
    Returns (fit, color, info)."""
    a = px[..., 3]
    info = {}
    if a.min() < 0.98:
        fit = a > 0.5
        info["mode"] = "alpha"
    else:
        border = np.concatenate([px[0, :, :3], px[-1, :, :3], px[:, 0, :3], px[:, -1, :3]])
        bg = np.median(border, axis=0)
        d = np.abs(px[..., :3] - bg).max(axis=2)
        spread = np.percentile(np.abs(border - bg).max(axis=1), 95)
        fit = d > max(0.06, 2.5 * spread)
        fit = close_mask(fit, 1)
        info["mode"] = "border_key"
        info["background_srgb"] = [round(float(v), 4) for v in bg]
    if fit.sum() < 50:
        raise ValueError("the concept image has no detectable subject (alpha or plain background)")
    lab, sizes = label_components(fit)
    keep = np.where(sizes >= max(0.02 * sizes[0], 25))[0] + 1
    fit = np.isin(lab, keep)
    info["components"] = int(len(keep))
    color = fit.copy()
    if extend and info["mode"] == "alpha":
        rgb = px[..., :3]
        cand = (a < 0.02) & (rgb.sum(axis=2) > 0.05)   # original background kept under alpha 0
        if cand.sum() > 200:
            bg = np.median(rgb[cand], axis=0)
            _h, s, _v = rgb_to_hsv(rgb)
            bg_s = float(rgb_to_hsv(bg[None, :])[1][0])
            faint = (a >= 0.04) & (a <= 0.5) & (s >= bg_s + 0.15) & (np.abs(rgb - bg).max(axis=2) >= 0.12)
            faint &= dilate(fit, 40)                     # next to the subject, not stray haze
            color = fit | faint
            info["color_extra_px"] = int(faint.sum())
            info["background_srgb"] = [round(float(v), 4) for v in bg]
    return fit, color, info


def delight(rgb, mask, strength, radius):
    """Light de-lighting: divide out the large-scale luminance of the subject (soft key light,
    darker lower body) towards its median, factor (median / blur) ** strength clamped to
    0.85..1.2. Works in linear light; returns sRGB values and info."""
    if strength <= 0:
        return rgb.copy(), {"strength": 0}
    lin = srgb_to_linear(rgb)
    Y = lin @ LUMA
    Yb = masked_blur(Y, mask, max(2, int(radius)))
    med = float(np.median(Y[mask])) if mask.any() else 0.2
    f = np.clip((med / np.maximum(Yb, 1e-4)) ** float(strength), 0.85, 1.2).astype(np.float32)
    f = np.where(mask, f, 1.0)
    out = linear_to_srgb(np.clip(lin * f[..., None], 0, 1))
    return out, {"strength": float(strength), "radius_px": int(radius), "median_luma": round(med, 4),
                 "factor_range": [round(float(f[mask].min()), 3), round(float(f[mask].max()), 3)]}


def emission_mask(rgb, mask, subject_h, v_min=0.45, s_min=0.3, sat_contrast=0.15, contrast=1.35, max_frac=0.04):
    """Glowing spots of the concept (eyes, crystals, runes): pixels that stand out from their
    neighbourhood (masked blur, 1.5 % of the subject height) in BOTH saturation (+sat_contrast) and
    luminance (x contrast), with a cool hue (green .. cyan .. blue .. violet, 70..330 deg). A blue
    highlight on a blue robe does not stand out in saturation; gold trim, skin and blood are warm.
    Warm hues count only when white-hot (value >= 0.95, saturation >= 0.5, 1.8 x the local
    luminance: fire). Returns (bool mask, info)."""
    h, s, v = rgb_to_hsv(rgb)
    Y = srgb_to_linear(rgb) @ LUMA
    r = max(2, int(0.015 * subject_h))
    Yb = masked_blur(Y, mask, r)
    sb = masked_blur(s, mask, r)
    cool = (h >= 70) & (h <= 330)
    inner = erode(mask, 1)
    hot = (v >= 0.95) & (s >= 0.5) & (Y >= 1.8 * Yb)
    glow = cool & (v >= v_min) & (s >= s_min) & (s >= sb + sat_contrast) & (Y >= contrast * Yb)
    cand = inner & (glow | hot)
    info = {"candidate_px": int(cand.sum())}
    if not cand.any():
        return cand, info
    lab, sizes = label_components(cand)
    min_px = max(3, int(round((subject_h / 700.0) ** 2 * 3)))
    budget = max_frac * float(mask.sum())
    keep = []
    used = 0
    for i, n in enumerate(sizes.tolist()):
        if n < min_px:
            break
        if used + n > budget:
            continue
        keep.append(i + 1)
        used += n
    out = np.isin(lab, keep) if keep else np.zeros_like(cand)
    out = dilate(out, 1) & mask
    info.update({"blobs": len(keep), "px": int(out.sum()), "min_blob_px": min_px})
    return out, info


# --------------------------------------------------------------------------- orthographic camera
class OrthoCam:
    """Concept camera: view direction d from yaw / pitch, image = scale * (x, -y) / (1 + k z) + offset with
    x = (P - centre) . right, y = (P - centre) . up, z = (P - centre) . d (depth, smaller = nearer).
    k = 0 is orthographic; k > 0 a pinhole at distance 1 / k in front of the centre (offset = the image
    of the centre = the principal point)."""

    def __init__(self, yaw_deg, pitch_deg, scale, tx, ty, resolution, k=0.0, centre=(0.0, 0.0, 0.0)):
        self.yaw = float(yaw_deg)
        self.pitch = float(pitch_deg)
        self.s = float(scale)
        self.tx = float(tx)
        self.ty = float(ty)
        self.res = (int(resolution[0]), int(resolution[1]))
        self.k = max(0.0, float(k or 0.0))
        self.centre = np.asarray(centre, dtype=np.float64).reshape(3)

    def basis(self):
        a, e = math.radians(self.yaw), math.radians(self.pitch)
        d = np.array([-math.sin(a) * math.cos(e), math.cos(a) * math.cos(e), -math.sin(e)])
        r = np.cross(d, [0.0, 0.0, 1.0])
        r /= max(np.linalg.norm(r), 1e-12)
        u = np.cross(r, d)
        return r, u, d

    def project(self, P):
        r, u, d = self.basis()
        P = np.asarray(P, dtype=np.float64) - self.centre
        x, y, z = P @ r, P @ u, P @ d
        if self.k > 0:
            den = np.maximum(1.0 + self.k * z, 0.05)
            x, y = x / den, y / den
        return (self.s * x + self.tx).astype(np.float32), (-self.s * y + self.ty).astype(np.float32), z.astype(np.float32)

    def location(self):
        """Camera position of a perspective camera (None for orthographic)."""
        if self.k <= 0:
            return None
        return self.centre - self.basis()[2] / self.k

    def toward(self, P):
        """Unit vectors from points to the camera (orthographic: -d everywhere)."""
        d = self.basis()[2]
        if self.k <= 0:
            return np.broadcast_to(-d, np.shape(P)).astype(np.float64)
        v = self.location()[None, :] - np.asarray(P, dtype=np.float64)
        return v / np.maximum(np.linalg.norm(v, axis=1, keepdims=True), 1e-12)

    def recentred(self, centre):
        """The same orthographic camera expressed around another centre (k must be 0)."""
        r, u, _d = self.basis()
        c = np.asarray(centre, dtype=np.float64) - self.centre
        return OrthoCam(self.yaw, self.pitch, self.s, self.tx + self.s * float(c @ r), self.ty - self.s * float(c @ u),
                        self.res, 0.0, centre)

    def to_dict(self):
        r, u, d = self.basis()
        out = {"type": "perspective" if self.k > 0 else "ortho", "yaw_deg": round(self.yaw, 3),
               "pitch_deg": round(self.pitch, 3), "px_per_m": round(self.s, 4),
               "offset_px": [round(self.tx, 3), round(self.ty, 3)], "resolution": list(self.res),
               "k_per_m": round(self.k, 6), "centre": [round(float(x), 5) for x in self.centre],
               "view_dir": [round(float(x), 5) for x in d], "right": [round(float(x), 5) for x in r],
               "up": [round(float(x), 5) for x in u]}
        if self.k > 0:
            out["distance_m"] = round(1.0 / self.k, 4)
        return out

    @staticmethod
    def from_dict(c, resolution):
        off = c.get("offset_px") or [0, 0]
        return OrthoCam(c.get("yaw_deg", 0.0), c.get("pitch_deg", 0.0), c["px_per_m"], off[0], off[1],
                        c.get("resolution") or resolution, c.get("k_per_m", 0.0), c.get("centre") or (0, 0, 0))


def splat_mask(u, v, w, h):
    j = np.floor(u).astype(np.int64)
    i = np.floor(v).astype(np.int64)
    ok = (j >= 0) & (j < w) & (i >= 0) & (i < h)
    m = np.zeros(h * w, dtype=bool)
    m[i[ok] * w + j[ok]] = True
    return close_mask(m.reshape(h, w), 1)


def mask_iou(a, b):
    return float(np.logical_and(a, b).sum()) / float(max(np.logical_or(a, b).sum(), 1))


def nelder_mead(f, x0, steps, iters=300, ftol=1e-5):
    """Plain Nelder-Mead (no scipy in Blender). Returns (x, fx, evaluations)."""
    n = len(x0)
    xs = [np.array(x0, dtype=np.float64)]
    for i in range(n):
        x = xs[0].copy()
        x[i] += steps[i]
        xs.append(x)
    fs = [f(x) for x in xs]
    ev = n + 1
    for _ in range(iters):
        order = np.argsort(fs)
        xs = [xs[i] for i in order]
        fs = [fs[i] for i in order]
        if abs(fs[-1] - fs[0]) < ftol:
            break
        cen = np.mean(xs[:-1], axis=0)
        xr = cen + (cen - xs[-1])
        fr = f(xr)
        ev += 1
        if fr < fs[0]:
            xe = cen + 2.0 * (cen - xs[-1])
            fe = f(xe)
            ev += 1
            xs[-1], fs[-1] = (xe, fe) if fe < fr else (xr, fr)
        elif fr < fs[-2]:
            xs[-1], fs[-1] = xr, fr
        else:
            xc = cen + 0.5 * (xs[-1] - cen)
            fc = f(xc)
            ev += 1
            if fc < fs[-1]:
                xs[-1], fs[-1] = xc, fc
            else:
                for i in range(1, n + 1):
                    xs[i] = xs[0] + 0.5 * (xs[i] - xs[0])
                    fs[i] = f(xs[i])
                    ev += 1
    i = int(np.argmin(fs))
    return xs[i], fs[i], ev


def fit_camera(points, mask, yaw_range=35.0, yaw_step=5.0, pitch_range=10.0, work_h=(200, 400),
               yaw_center=0.0, perspective=True, min_gain=0.005, log=None):
    """Camera whose silhouette of the surface samples `points` best matches `mask` (bool, concept
    resolution). Coarse grid over yaw (yaw_center +- yaw_range) and pitch with a 2D similarity alignment
    (silhouette area + centroid), then Nelder-Mead on [yaw, pitch, ln scale, tx, ty] (orthographic) at
    the working heights for the two best distinct starts; finally, with perspective, Nelder-Mead adds the
    inverse distance k (concept renders are perspective: a shield held forward looks bigger) and keeps it
    only when the IoU gains >= min_gain. Returns (camera at the mask resolution, IoU at the last working
    height, info)."""
    H0, W0 = mask.shape
    pts = np.asarray(points, dtype=np.float64)
    info = {"coarse": [], "refine": []}
    work = []
    for wh in work_h:
        wh = int(min(wh, H0))
        m, (fx, fy) = resize_mask(mask, wh)
        work.append((m, fx, fy))
    mc, fxc, fyc = work[0]
    hc, wc = mc.shape
    ys, xs = np.nonzero(mc)
    tgt_area = float(len(xs))
    tgt_c = np.array([xs.mean() + 0.5, ys.mean() + 0.5])
    pitches = sorted({0.0, -pitch_range, pitch_range, -pitch_range / 2, pitch_range / 2}) if pitch_range > 0 else [0.0]
    yaws = np.arange(yaw_center - yaw_range, yaw_center + yaw_range + 1e-6, yaw_step) if yaw_range > 0 else [yaw_center]
    cands = []
    for yaw in yaws:
        for pitch in pitches:
            cam = OrthoCam(yaw, pitch, 1.0, 0.0, 0.0, (wc, hc))
            u, v, _ = cam.project(pts)
            k0 = 0.8 * hc / max(float(v.max() - v.min()), 1e-9)
            um, vm = float(u.mean()), float(v.mean())
            m0 = splat_mask((u - um) * k0 + wc / 2, (v - vm) * k0 + hc / 2, wc, hc)
            a0 = float(m0.sum())
            if a0 < 4:
                continue
            yy, xx = np.nonzero(m0)
            c_src = (np.array([xx.mean() + 0.5, yy.mean() + 0.5]) - [wc / 2, hc / 2]) / k0 + [um, vm]
            k = k0 * math.sqrt(tgt_area / a0)
            tx = tgt_c[0] - c_src[0] * k
            ty = tgt_c[1] - c_src[1] * k
            m1 = splat_mask(u * k + tx, v * k + ty, wc, hc)
            cands.append((mask_iou(m1, mc), [float(yaw), float(pitch), math.log(k), tx, ty]))
    if not cands:
        raise ValueError("camera fit: the model projects to nothing")
    # near-ties go to the view closest to the model front (front / back silhouettes are mirror images)
    cands.sort(key=lambda c: -(c[0] - 0.01 * abs(c[1][0] - yaw_center) / 45.0))
    info["coarse"] = [{"iou": round(c[0], 4), "yaw": c[1][0], "pitch": c[1][1]} for c in cands[:6]]
    starts = []
    for iou, p in cands:
        if all(abs(p[0] - q[0]) > 1.5 * yaw_step or abs(p[1] - q[1]) > 4 for _, q in starts):
            starts.append((iou, p))
        if len(starts) >= 2:
            break
    best = None
    lo_yaw, hi_yaw = yaw_center - yaw_range - 10.0, yaw_center + yaw_range + 10.0

    def pitch_ok(q):
        return lo_yaw <= q[0] <= hi_yaw and abs(q[1]) <= max(pitch_range, 0.0) + 1e-9

    for iou0, p in starts:
        p = np.array(p, dtype=np.float64)
        steps = np.array([3.0, 3.0 if pitch_range > 0 else 0.0, 0.03, 2.0, 2.0])
        prev_f = (fxc, fyc)
        fx_best = 1.0
        for li, (mw, fx, fy) in enumerate(work):
            if li > 0:  # rescale scale / offsets to this working resolution
                p[2] += math.log(fx / prev_f[0])
                p[3] *= fx / prev_f[0]
                p[4] *= fy / prev_f[1]
                steps = steps * 0.5
                steps[3:] *= fx / prev_f[0]
            prev_f = (fx, fy)
            hw, ww = mw.shape

            def obj(q, mw=mw, ww=ww, hw=hw):
                if not pitch_ok(q):
                    return 1.0
                cam = OrthoCam(q[0], q[1], math.exp(q[2]), q[3], q[4], (ww, hw))
                u, v, _ = cam.project(pts)
                return 1.0 - mask_iou(splat_mask(u, v, ww, hw), mw)
            if pitch_range <= 0:
                q0 = np.delete(p, 1)

                def obj4(q, obj=obj):
                    return obj(np.insert(q, 1, 0.0))
                q, fx_best, ev = nelder_mead(obj4, q0, np.delete(steps, 1), iters=220 if li == 0 else 160)
                p = np.insert(q, 1, 0.0)
            else:
                p, fx_best, ev = nelder_mead(obj, p, steps, iters=220 if li == 0 else 160)
            info["refine"].append({"start_iou": round(iou0, 4), "work_h": hw, "iou": round(1 - fx_best, 4),
                                   "evals": ev, "yaw": round(float(p[0]), 2), "pitch": round(float(p[1]), 2)})
        if best is None or fx_best < best[0]:
            best = (fx_best, p.copy(), prev_f)
    fbest, p, (fx, fy) = best
    mw, _fx, _fy = work[-1]
    hw, ww = mw.shape
    centre = (pts.min(axis=0) + pts.max(axis=0)) / 2
    ext = float(np.linalg.norm(pts.max(axis=0) - pts.min(axis=0)))
    cam_w = OrthoCam(p[0], p[1], math.exp(p[2]), p[3], p[4], (ww, hw)).recentred(centre)
    info["perspective"] = {"tried": bool(perspective), "used": False}
    if perspective:
        q_o = np.array([cam_w.yaw, cam_w.pitch, math.log(cam_w.s), cam_w.tx, cam_w.ty, 0.0])

        def objp(q):
            # q[5] = k x model extent (0 = orthographic, 1 = camera one model length away)
            if not pitch_ok(q) or not (0.0 <= q[5] <= 0.9):
                return 1.0
            cam = OrthoCam(q[0], q[1], math.exp(q[2]), q[3], q[4], (ww, hw), q[5] / ext, centre)
            u, v, _ = cam.project(pts)
            return 1.0 - mask_iou(splat_mask(u, v, ww, hw), mw)
        f_o = objp(q_o)
        best_p = None
        for kh in (0.15, 0.4):
            q = q_o.copy()
            q[5] = kh
            qq, fq, ev = nelder_mead(objp, q, np.array([1.5, 1.5 if pitch_range > 0 else 0.0, 0.015, 1.0, 1.0, 0.08]),
                                     iters=200)
            if best_p is None or fq < best_p[1]:
                best_p = (qq, fq, ev)
        info["perspective"].update({"iou_ortho": round(1 - f_o, 4), "iou_perspective": round(1 - best_p[1], 4),
                                    "k_x_extent": round(float(best_p[0][5]), 4), "evals": best_p[2]})
        if f_o - best_p[1] >= min_gain:
            q = best_p[0]
            cam_w = OrthoCam(q[0], q[1], math.exp(q[2]), q[3], q[4], (ww, hw), q[5] / ext, centre)
            fbest = best_p[1]
            info["perspective"]["used"] = True
    cam = OrthoCam(cam_w.yaw, cam_w.pitch, cam_w.s / fx, cam_w.tx / fx, cam_w.ty / fy, (W0, H0), cam_w.k, centre)
    if log is not None:
        log.append({"stage": "camera", "coarse": info["coarse"][:3], "refine": info["refine"]})
    return cam, round(1.0 - fbest, 4), info


# --------------------------------------------------------------------------- rasterisation
def raster_chunks(P2, T, w, h, eps=1e-5, max_cand=2_500_000):
    """Pixel centres inside triangles. P2: (n, 2) pixel coordinates (x = column, y = row, centres at
    +0.5), T: (m, 3) vertex indices. Yields (tri int32, flat pixel int64, l0, l1, l2 float32) per
    chunk, l = barycentric weights of the three corners."""
    T = np.asarray(T, dtype=np.int64)
    if not len(T):
        return
    A, B, C = P2[T[:, 0]].astype(np.float64), P2[T[:, 1]].astype(np.float64), P2[T[:, 2]].astype(np.float64)
    den = (B[:, 0] - A[:, 0]) * (C[:, 1] - A[:, 1]) - (B[:, 1] - A[:, 1]) * (C[:, 0] - A[:, 0])
    xmin = np.minimum(np.minimum(A[:, 0], B[:, 0]), C[:, 0])
    xmax = np.maximum(np.maximum(A[:, 0], B[:, 0]), C[:, 0])
    ymin = np.minimum(np.minimum(A[:, 1], B[:, 1]), C[:, 1])
    ymax = np.maximum(np.maximum(A[:, 1], B[:, 1]), C[:, 1])
    j0 = np.clip(np.ceil(xmin - 0.5), 0, w).astype(np.int64)
    j1 = np.clip(np.floor(xmax - 0.5), -1, w - 1).astype(np.int64)
    i0 = np.clip(np.ceil(ymin - 0.5), 0, h).astype(np.int64)
    i1 = np.clip(np.floor(ymax - 0.5), -1, h - 1).astype(np.int64)
    nx = np.maximum(j1 - j0 + 1, 0)
    ny = np.maximum(i1 - i0 + 1, 0)
    n = nx * ny
    n[np.abs(den) < 1e-12] = 0
    idx_all = np.nonzero(n)[0]
    if not len(idx_all):
        return
    csum = np.cumsum(n[idx_all])
    start = 0
    while start < len(idx_all):
        base = csum[start - 1] if start > 0 else 0
        stop = int(np.searchsorted(csum, base + max_cand, side="right"))
        stop = max(stop, start + 1)
        idx = idx_all[start:stop]
        counts = n[idx]
        tot = int(counts.sum())
        rep = np.repeat(idx, counts)
        first = np.repeat(np.cumsum(counts) - counts, counts)
        local = np.arange(tot, dtype=np.int64) - first
        nxr = nx[rep]
        jj = j0[rep] + local % nxr
        ii = i0[rep] + local // nxr
        px = jj + 0.5
        py = ii + 0.5
        a = A[rep]
        dn = den[rep]
        dxa = px - a[:, 0]
        dya = py - a[:, 1]
        l1 = (dxa * (C[rep, 1] - a[:, 1]) - dya * (C[rep, 0] - a[:, 0])) / dn
        l2 = ((B[rep, 0] - a[:, 0]) * dya - (B[rep, 1] - a[:, 1]) * dxa) / dn
        l0 = 1.0 - l1 - l2
        ins = (l0 >= -eps) & (l1 >= -eps) & (l2 >= -eps)
        yield (rep[ins].astype(np.int32), (ii[ins] * w + jj[ins]).astype(np.int64),
               l0[ins].astype(np.float32), l1[ins].astype(np.float32), l2[ins].astype(np.float32))
        start = stop


def zbuffer(u, v, depth, T, w, h, tri_label=None):
    """Nearest depth per pixel (inf where empty) of triangles with projected vertices (u, v, depth).
    With tri_label (int per triangle) also returns the label of the nearest triangle per pixel (-1 empty)."""
    zb = np.full(w * h, np.inf, dtype=np.float32)
    P2 = np.stack([u, v], axis=1)
    dz = np.asarray(depth, dtype=np.float32)
    for tri, flat, l0, l1, l2 in raster_chunks(P2, T, w, h):
        t = T[tri]
        z = l0 * dz[t[:, 0]] + l1 * dz[t[:, 1]] + l2 * dz[t[:, 2]]
        np.minimum.at(zb, flat, z)
    if tri_label is None:
        return zb.reshape(h, w)
    lab = np.full(w * h, -1, dtype=np.int32)
    for tri, flat, l0, l1, l2 in raster_chunks(P2, T, w, h):
        t = T[tri]
        z = l0 * dz[t[:, 0]] + l1 * dz[t[:, 1]] + l2 * dz[t[:, 2]]
        win = z <= zb[flat]
        lab[flat[win]] = tri_label[tri[win]]
    return zb.reshape(h, w), lab.reshape(h, w)


def raster_uv(uv_corners, size):
    """Texel coverage of UV triangles. uv_corners: (m, 3, 2). Returns (tri int32 per texel, -1 empty;
    l1, l2 float32 barycentric weights of corners 1 and 2), arrays (size, size), bottom row first.
    Overlapping triangles: the last one wins."""
    m = len(uv_corners)
    P2 = (uv_corners.reshape(-1, 2) * size).astype(np.float64)
    T = np.arange(m * 3, dtype=np.int64).reshape(m, 3)
    tri_of = np.full(size * size, -1, dtype=np.int32)
    b1 = np.zeros(size * size, dtype=np.float32)
    b2 = np.zeros(size * size, dtype=np.float32)
    for tri, flat, _l0, l1, l2 in raster_chunks(P2, T, size, size):
        tri_of[flat] = tri
        b1[flat] = l1
        b2[flat] = l2
    return tri_of.reshape(size, size), b1.reshape(size, size), b2.reshape(size, size)


def uv_overlap(uv_corners, size=256):
    """Share of covered texels hit by more than one UV triangle (mirrored / stacked islands)."""
    m = len(uv_corners)
    if not m:
        return 0.0
    P2 = (uv_corners.reshape(-1, 2) * size).astype(np.float64)
    T = np.arange(m * 3, dtype=np.int64).reshape(m, 3)
    cnt = np.zeros(size * size, dtype=np.int32)
    for _tri, flat, _a, _b, _c in raster_chunks(P2, T, size, size, eps=-1e-3):
        np.add.at(cnt, flat, 1)
    cov = (cnt > 0).sum()
    return float((cnt > 1).sum()) / float(max(cov, 1))


# --------------------------------------------------------------------------- texture fill
def dilate_texels(img, valid, passes):
    """Grow valid texels outward by `passes` (8-neighbour mean); returns (img, valid)."""
    return dilate_colors(img, valid, passes)


def pull_push(img, valid):
    """Fill every invalid texel with a smooth extrapolation of the valid ones (mip pull-push)."""
    levels = [(img * valid[..., None], valid.astype(np.float32))]
    while min(levels[-1][1].shape) > 1:
        c, wgt = levels[-1]
        h, w = wgt.shape
        h2, w2 = (h + 1) // 2, (w + 1) // 2
        cp = np.zeros((h2 * 2, w2 * 2, c.shape[2]), dtype=np.float32)
        wp = np.zeros((h2 * 2, w2 * 2), dtype=np.float32)
        cp[:h, :w] = c
        wp[:h, :w] = wgt
        cs = cp.reshape(h2, 2, w2, 2, -1).sum(axis=(1, 3))
        ws = wp.reshape(h2, 2, w2, 2).sum(axis=(1, 3))
        levels.append((cs, ws))
    # normalise each level's colour by its weight
    filled = None
    for c, wgt in reversed(levels):
        col = c / np.maximum(wgt, 1e-9)[..., None]
        if filled is None:
            filled = col
            continue
        h, w = wgt.shape
        up = np.repeat(np.repeat(filled, 2, axis=0), 2, axis=1)[:h, :w]
        filled = np.where((wgt > 1e-9)[..., None], col, up)
    out = img.copy()
    out[~valid] = filled[~valid]
    return out


# --------------------------------------------------------------------------- mesh data
def mesh_data(ob):
    """World-space triangle data of a mesh object: vertex positions / normals, loop triangles,
    corner (loop) normals per triangle corner, UV corners (if a UV layer exists), polygon edges."""
    me = ob.data
    me.calc_loop_triangles()
    M = np.array(ob.matrix_world, dtype=np.float64)
    nv = len(me.vertices)
    co = np.empty(nv * 3, dtype=np.float64)
    me.vertices.foreach_get("co", co)
    co = co.reshape(nv, 3) @ M[:3, :3].T + M[:3, 3]
    Ninv = np.linalg.inv(M[:3, :3]).T
    vn = np.empty(nv * 3, dtype=np.float64)
    me.vertices.foreach_get("normal", vn)
    vn = vn.reshape(nv, 3) @ Ninv.T
    vn /= np.maximum(np.linalg.norm(vn, axis=1, keepdims=True), 1e-12)
    nt = len(me.loop_triangles)
    tv = np.empty(nt * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("vertices", tv)
    tl = np.empty(nt * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("loops", tl)
    nl = len(me.loops)
    try:
        cn = np.empty(nl * 3, dtype=np.float64)
        me.corner_normals.foreach_get("vector", cn)
        cn = cn.reshape(nl, 3) @ Ninv.T
        cn /= np.maximum(np.linalg.norm(cn, axis=1, keepdims=True), 1e-12)
    except (AttributeError, RuntimeError, TypeError):
        lv = np.empty(nl, dtype=np.int64)
        me.loops.foreach_get("vertex_index", lv)
        cn = vn[lv]
    uv = None
    if me.uv_layers.active is not None:
        uvl = np.empty(nl * 2, dtype=np.float64)
        me.uv_layers.active.data.foreach_get("uv", uvl)
        uv = uvl.reshape(nl, 2)[tl].reshape(nt, 3, 2)
    ne = len(me.edges)
    ev = np.empty(ne * 2, dtype=np.int64)
    me.edges.foreach_get("vertices", ev)
    return {"co": co, "vn": vn, "tv": tv.reshape(nt, 3), "tl": tl.reshape(nt, 3), "cn": cn[tl].reshape(nt, 3, 3),
            "uv": uv, "edges": ev.reshape(ne, 2)}


def uv_report(d):
    """Is the active UV layer usable for a unique bake? (no layer, tiny / degenerate islands,
    outside 0..1 or overlapping islands -> not usable). Returns (usable, info)."""
    uv = d["uv"]
    if uv is None:
        return False, {"reason": "no UV layer"}
    a = uv[:, 1] - uv[:, 0]
    b = uv[:, 2] - uv[:, 0]
    area = np.abs(a[:, 0] * b[:, 1] - a[:, 1] * b[:, 0]) / 2
    total = float(area.sum())
    co = d["co"][d["tv"]]
    a3 = np.linalg.norm(np.cross(co[:, 1] - co[:, 0], co[:, 2] - co[:, 0]), axis=1) / 2
    degen = float(((area < 1e-12) & (a3 > 1e-12)).mean()) if len(a3) else 1.0
    outside = float(((uv < -0.001) | (uv > 1.001)).any(axis=2).any(axis=1).mean()) if len(uv) else 1.0
    info = {"uv_area": round(total, 4), "degenerate_tris": round(degen, 4), "outside_0_1": round(outside, 4)}
    if total < 0.05 or degen > 0.1 or outside > 0.01:
        info["reason"] = "UV area %.3f, degenerate %.1f%%, outside 0..1 %.1f%%" % (total, 100 * degen, 100 * outside)
        return False, info
    ov = uv_overlap(uv)
    info["overlap"] = round(ov, 4)
    if ov > 0.08:
        info["reason"] = "overlapping UV islands (%.0f%% of texels)" % (100 * ov)
        return False, info
    return True, info


def area_samples(co, tv, n, seed=0):
    """Area-weighted surface samples of triangles (plus all vertices)."""
    P = co[tv]
    area = np.linalg.norm(np.cross(P[:, 1] - P[:, 0], P[:, 2] - P[:, 0]), axis=1) / 2
    tot = float(area.sum())
    if tot <= 0:
        return co.copy()
    rng = np.random.default_rng(seed)
    pick = rng.choice(len(tv), size=int(n), p=area / tot)
    a, b = rng.random(int(n)), rng.random(int(n))
    flip = a + b > 1
    a[flip], b[flip] = 1 - a[flip], 1 - b[flip]
    S = P[pick, 0] + (P[pick, 1] - P[pick, 0]) * a[:, None] + (P[pick, 2] - P[pick, 0]) * b[:, None]
    return np.concatenate([co, S])


def components(n, edges):
    return ml.components(n, edges)


def weld_index(co, eps):
    """Canonical vertex id per vertex: vertices at the same position (within eps) share one. glTF splits
    vertices at flat-shading and UV seams; pieces and smoothing must not stop at those splits."""
    q = np.round(np.asarray(co, dtype=np.float64) / max(float(eps), 1e-12)).astype(np.int64)
    _u, canon = np.unique(q, axis=0, return_inverse=True)
    return canon.ravel().astype(np.int64), int(len(_u))


def welded_pieces(co, edges, eps):
    """(piece id per vertex, canonical ids, canonical count, canonical edges without self loops)."""
    canon, nc = weld_index(co, eps)
    ce = canon[edges] if len(edges) else np.zeros((0, 2), dtype=np.int64)
    ce = ce[ce[:, 0] != ce[:, 1]] if len(ce) else ce
    labels = ml.components(nc, ce)
    _uniq, piece_c = np.unique(labels, return_inverse=True)
    return piece_c.ravel()[canon], canon, nc, ce


def smooth_fill_welded(colors, fixed, canon, nc, cedges, iters):
    """smooth_fill over welded vertices: duplicates are averaged, smoothed together and mapped back;
    fixed vertices keep their own colour."""
    if iters <= 0 or not len(cedges):
        return colors
    cnt = np.bincount(canon, minlength=nc).astype(np.float64)
    cc = np.stack([np.bincount(canon, weights=colors[:, k], minlength=nc) for k in range(colors.shape[1])], axis=1)
    cc /= np.maximum(cnt, 1)[:, None]
    fixed_c = np.bincount(canon, weights=fixed.astype(np.float64), minlength=nc) > 0
    out = smooth_fill(cc.astype(np.float32), fixed_c, cedges, iters)[canon]
    out[fixed] = colors[fixed]
    return out.astype(np.float32)


def pack_uv(ob, margin=0.002, shape="AABB"):
    """Repack the UV islands of ob (Smart UV leaves ~30 % of a noisy AI mesh's square used; AABB
    packing with rotation gives ~37 % in milliseconds, CONCAVE ~45 % in ~10 s)."""
    af.select_only([ob])
    with bpy.context.temp_override(object=ob, active_object=ob, selected_objects=[ob],
                                   selected_editable_objects=[ob]):
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.select_all(action="SELECT")
        try:
            bpy.ops.uv.pack_islands(rotate=True, margin=float(margin), shape_method=str(shape).upper(),
                                    margin_method="FRACTION")
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")


def head_band(V, mirror_point, mirror_normal, frac=0.13, column=0.1):
    """z above which the head is: the top of the body column (points within column x height of the
    mirror plane) minus frac x height. Props beside the head (staff, banner) are outside the column."""
    z0, z1 = float(V[:, 2].min()), float(V[:, 2].max())
    H = max(z1 - z0, 1e-9)
    d = np.abs((V - mirror_point) @ mirror_normal)
    col = V[d < column * H]
    top = float(np.percentile(col[:, 2], 99.7)) if len(col) > 50 else z1
    return top - frac * H, top


def symmetry_plane(V, band=(0.75, 0.85), max_angle=35.0):
    """Left/right mirror plane of a character: through the median of the shoulder band, normal
    along the band's main horizontal axis (if within max_angle of X, else X). Returns (point, normal, info)."""
    z0, z1 = float(V[:, 2].min()), float(V[:, 2].max())
    H = max(z1 - z0, 1e-9)
    sel = V[(V[:, 2] >= z0 + band[0] * H) & (V[:, 2] <= z0 + band[1] * H)]
    if len(sel) < 50:
        sel = V
    xy = sel[:, :2]
    c = np.median(xy, axis=0)
    lo, hi = np.percentile(xy[:, 0], 10), np.percentile(xy[:, 0], 90)
    core = xy[(xy[:, 0] >= lo) & (xy[:, 0] <= hi)]
    ang = 0.0
    if len(core) > 20:
        cov = np.cov((core - core.mean(axis=0)).T)
        w, vec = np.linalg.eigh(cov)
        e = vec[:, int(np.argmax(w))]
        ang = math.degrees(math.atan2(e[1], e[0]))
        ang = (ang + 90.0) % 180.0 - 90.0
        if abs(ang) > max_angle:
            ang = 0.0
    nrm = np.array([math.cos(math.radians(ang)), math.sin(math.radians(ang)), 0.0])
    return np.array([c[0], c[1], 0.0]), nrm, {"point": [round(float(c[0]), 4), round(float(c[1]), 4)],
                                               "angle_deg": round(ang, 2)}


def reflect(P, point, normal):
    d = (P - point) @ normal
    return P - 2.0 * d[:, None] * normal[None, :]


def kd_tree(points):
    kd = mathutils.kdtree.KDTree(len(points))
    for i, p in enumerate(np.asarray(points, dtype=np.float64).tolist()):
        kd.insert(p, i)
    kd.balance()
    return kd


def kd_nearest(kd, Q):
    """(index, distance) arrays of the nearest tree point for every row of Q."""
    idx = np.empty(len(Q), dtype=np.int64)
    dist = np.empty(len(Q), dtype=np.float64)
    for k, q in enumerate(np.asarray(Q, dtype=np.float64).tolist()):
        _co, i, dd = kd.find(q)
        idx[k] = i
        dist[k] = dd
    return idx, dist


def smooth_fill(colors, fixed, edges, iters):
    """Laplacian smoothing of per-vertex colours over the mesh graph; fixed vertices stay."""
    if iters <= 0 or not len(edges):
        return colors
    n = len(colors)
    a, b = edges[:, 0], edges[:, 1]
    deg = np.bincount(a, minlength=n) + np.bincount(b, minlength=n)
    free = (~fixed) & (deg > 0)
    c = colors.astype(np.float64).copy()
    for _ in range(int(iters)):
        acc = np.zeros_like(c)
        for k in range(c.shape[1]):
            acc[:, k] = np.bincount(a, weights=c[b, k], minlength=n) + np.bincount(b, weights=c[a, k], minlength=n)
        avg = acc / np.maximum(deg, 1)[:, None]
        c[free] = 0.5 * c[free] + 0.5 * avg[free]
    return c.astype(np.float32)


# --------------------------------------------------------------------------- images / material
def image_from_array(name, tex, colorspace="sRGB", path=None, pack=True):
    """Blender image from a (T, T, 3|4) array (bottom row first, stored values); PNG at path."""
    h, w = tex.shape[:2]
    rgba = np.ones((h, w, 4), dtype=np.float32)
    rgba[..., :tex.shape[2]] = np.clip(tex, 0, 1)
    img = bpy.data.images.new(name, width=w, height=h, alpha=False, float_buffer=False)
    try:
        img.colorspace_settings.name = colorspace
    except TypeError:
        pass
    img.pixels.foreach_set(rgba.ravel())
    if path:
        img.filepath_raw = os.path.abspath(path)
        img.file_format = "PNG"
        img.save()
    if pack:
        img.pack()
    return img


def painted_material(name, base_img, emi_img=None, emi_strength=1.0, roughness=0.75, metallic=0.0,
                     metal_img=None):
    """glTF-exportable Principled material: Base Color (and Emission) from images."""
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = ml.principled_of(mat)
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.name = "paint_base_color"
    tex.image = base_img
    tex.location = (-420, 260)
    nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    bsdf.inputs["Roughness"].default_value = float(roughness)
    bsdf.inputs["Metallic"].default_value = float(metallic)
    if "Specular IOR Level" in bsdf.inputs:
        bsdf.inputs["Specular IOR Level"].default_value = 0.35
    if metal_img is not None:
        mr = nt.nodes.new("ShaderNodeTexImage")
        mr.name = "paint_metal_rough"
        mr.image = metal_img
        mr.location = (-420, -40)
        sep = nt.nodes.new("ShaderNodeSeparateColor")
        sep.location = (-160, -40)
        nt.links.new(mr.outputs["Color"], sep.inputs["Color"])
        nt.links.new(sep.outputs["Green"], bsdf.inputs["Roughness"])
        nt.links.new(sep.outputs["Blue"], bsdf.inputs["Metallic"])
    if emi_img is not None:
        et = nt.nodes.new("ShaderNodeTexImage")
        et.name = "paint_emission"
        et.image = emi_img
        et.location = (-420, -340)
        nt.links.new(et.outputs["Color"], bsdf.inputs["Emission Color"])
        bsdf.inputs["Emission Strength"].default_value = float(emi_strength)
    nt.nodes.active = tex
    mat.diffuse_color = (0.8, 0.8, 0.8, 1.0)
    return mat


def material_is_emissive(mat):
    bsdf = ml.principled_of(mat)
    if bsdf is None:
        return False
    try:
        col = bsdf.inputs["Emission Color"]
        strength = float(bsdf.inputs["Emission Strength"].default_value)
    except KeyError:
        return False
    if col.is_linked:
        return strength > 0
    return strength > 0 and max(col.default_value[:3]) > 0.02


def glb_images(path):
    """(images, materials with a base colour texture, materials with an emissive texture) of a GLB."""
    with open(path, "rb") as fh:
        head = fh.read(12)
        if head[:4] != b"glTF":
            return 0, 0, 0
        ln, typ = struct.unpack("<II", fh.read(8))
        doc = json.loads(fh.read(ln).decode("utf-8"))
    mats = doc.get("materials", [])
    base = sum(1 for m in mats if (m.get("pbrMetallicRoughness") or {}).get("baseColorTexture") is not None)
    emi = sum(1 for m in mats if m.get("emissiveTexture") is not None)
    return len(doc.get("images", [])), base, emi


# --------------------------------------------------------------------------- renders
def render_camera_tiles(objects, cam, window, size, color_type="TEXTURE", light="FLAT", cavity=True):
    """Workbench render of objects through the fitted concept camera (orthographic or perspective with
    its principal point at the image of cam.centre), cropped to window (x0, y0, side) in concept pixels,
    size x size px. Returns a (size, size, 4) tile, bottom row first."""
    x0, y0, side = window
    r, u, d = cam.basis()
    cx, cy = x0 + side / 2.0, y0 + side / 2.0
    lo, hi = af.world_bbox(objects)
    extent = max(float(np.linalg.norm(np.array(hi) - np.array(lo))), 1e-3)
    scene = bpy.context.scene
    cam_data = bpy.data.cameras.new("af_paint_cam")
    cam_data.clip_start = 0.001
    cam_ob = bpy.data.objects.new("af_paint_cam", cam_data)
    scene.collection.objects.link(cam_ob)
    if cam.k > 0:
        # pinhole 1/k in front of the centre; focal length from px_per_m at the centre plane, the window
        # centre reached by lens shift (the principal point is the image of the centre)
        cam_data.type = "PERSP"
        cam_data.sensor_fit = "HORIZONTAL"
        cam_data.sensor_width = 36.0
        cam_data.lens = 36.0 * cam.s / (cam.k * side)
        cam_data.shift_x = (cx - cam.tx) / side
        cam_data.shift_y = (cam.ty - cy) / side
        cam_data.clip_end = 1.0 / cam.k + extent * 20
        loc = cam.location()
    else:
        cam_data.type = "ORTHO"
        cam_data.ortho_scale = side / cam.s
        cam_data.clip_end = extent * 20
        # point on the view plane through the centre that lands on the window centre
        P = cam.centre + r * ((cx - cam.tx) / cam.s) - u * ((cy - cam.ty) / cam.s)
        loc = P - d * extent * 5
    cam_ob.location = mathutils.Vector([float(x) for x in loc])
    R = mathutils.Matrix([r.tolist(), u.tolist(), (-d).tolist()]).transposed()
    cam_ob.rotation_euler = R.to_euler()
    return _render_with(cam_ob, cam_data, objects, size, color_type, light, cavity)


def render_view_tiles(objects, dirs, size, bbox=None, margin=1.04, color_type="TEXTURE", light="FLAT", cavity=True):
    """Workbench orthographic tiles from directions (vector target -> camera, Z up), framed on bbox."""
    lo, hi = bbox if bbox is not None else af.world_bbox(objects)
    lo, hi = mathutils.Vector(list(lo)), mathutils.Vector(list(hi))
    centre = (lo + hi) / 2
    extent = max((hi - lo).length, 1e-3)
    scene = bpy.context.scene
    tiles = []
    for dvec in dirs:
        cam_data = bpy.data.cameras.new("af_paint_view")
        cam_data.type = "ORTHO"
        cam_data.ortho_scale = max(hi.z - lo.z, 0.5 * extent) * margin
        cam_data.clip_end = extent * 20
        cam_ob = bpy.data.objects.new("af_paint_view", cam_data)
        scene.collection.objects.link(cam_ob)
        dv = mathutils.Vector([float(x) for x in dvec]).normalized()
        cam_ob.location = centre + dv * extent * 4
        look = (centre - cam_ob.location).normalized()
        cam_ob.rotation_euler = look.to_track_quat("-Z", "Y").to_euler()
        tiles.append(_render_with(cam_ob, cam_data, objects, size, color_type, light, cavity))
    return tiles


def _render_with(cam_ob, cam_data, objects, size, color_type, light, cavity):
    scene = bpy.context.scene
    prev = {"camera": scene.camera, "engine": scene.render.engine,
            "vt": scene.view_settings.view_transform, "look": scene.view_settings.look}
    keep = set(objects)
    hidden = []
    for o in scene.objects:
        if o.type == "MESH" and o not in keep and not o.hide_render:
            o.hide_render = True
            hidden.append(o)
    if color_type == "TEXTURE":
        ml.set_active_basecolor_nodes(objects)
    scene.camera = cam_ob
    rr = scene.render
    rr.engine = af.engine_id("WORKBENCH")
    rr.resolution_x = rr.resolution_y = int(size)
    rr.resolution_percentage = 100
    rr.film_transparent = True
    rr.image_settings.file_format = "PNG"
    rr.image_settings.color_mode = "RGBA"
    try:
        scene.view_settings.view_transform = "Standard"
        scene.view_settings.look = "None"
    except TypeError:
        pass
    sh = scene.display.shading
    sh.light = light
    sh.color_type = color_type
    sh.show_cavity = bool(cavity)
    sh.show_object_outline = False
    sh.show_specular_highlight = light != "FLAT"
    tmp = os.path.join(bpy.app.tempdir or os.environ.get("TEMP", "."), "af_paint_%d.png" % os.getpid())
    try:
        rr.filepath = tmp
        bpy.ops.render.render(write_still=True)
        tile = ml.load_png(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass
        for o in hidden:
            o.hide_render = False
        scene.camera = prev["camera"]
        rr.engine = prev["engine"]
        try:
            scene.view_settings.view_transform = prev["vt"]
            scene.view_settings.look = prev["look"]
        except TypeError:
            pass
        bpy.data.objects.remove(cam_ob)
        bpy.data.cameras.remove(cam_data)
    return tile


def over(tile, bg=(0.82, 0.82, 0.84)):
    a = tile[..., 3:4]
    out = np.empty_like(tile)
    out[..., :3] = tile[..., :3] * a + np.asarray(bg, dtype=np.float32)[None, None, :] * (1 - a)
    out[..., 3] = 1.0
    return out


def save_png_top(path, img_top):
    """Save an (h, w, 3|4) top-row-first array as PNG."""
    h, w = img_top.shape[:2]
    rgba = np.ones((h, w, 4), dtype=np.float32)
    rgba[..., :img_top.shape[2]] = np.clip(img_top, 0, 1)
    return ml.save_png(path, np.ascontiguousarray(rgba[::-1]))
