"""Flat low-poly colouring for paint_from_concept (flat: true, flat_palette: N).

A flat-shaded low-poly reference has ONE colour per facet. Projected colours of a mesh that is not exactly the
concept smear across facets; collapsing every triangle to a robust colour gives crisp facets:

  * per triangle: weighted marginal median of the colours of its texels (the barycentric samples), weights =
    the confidence that the texel was coloured from a concept (facing x visibility x edge fade); when a
    triangle has confident samples, pure fill (guessed) samples are ignored;
  * optional palette: numpy k-means (k-means++ start, fixed seed) on the subject pixels of the concept(s) in
    CIE Lab, every triangle colour snaps to the nearest palette colour.

numpy only, no bpy: testable outside Blender. Colours are sRGB floats 0..1.
"""
import numpy as np

_D65 = np.array([0.95047, 1.0, 1.08883])
_M = np.array([[0.4124564, 0.3575761, 0.1804375],
               [0.2126729, 0.7151522, 0.0721750],
               [0.0193339, 0.1191920, 0.9503041]])


def srgb_to_lab(rgb):
    """(n, 3) sRGB 0..1 -> CIE Lab (D65), float64."""
    c = np.clip(np.asarray(rgb, dtype=np.float64), 0.0, 1.0)
    lin = np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)
    xyz = (lin @ _M.T) / _D65
    f = np.where(xyz > 216.0 / 24389.0, np.cbrt(xyz), (24389.0 / 27.0 * xyz + 16.0) / 116.0)
    return np.stack([116.0 * f[:, 1] - 16.0, 500.0 * (f[:, 0] - f[:, 1]), 200.0 * (f[:, 1] - f[:, 2])], axis=1)


def lab_to_srgb(lab):
    lab = np.asarray(lab, dtype=np.float64)
    fy = (lab[:, 0] + 16.0) / 116.0
    fx = fy + lab[:, 1] / 500.0
    fz = fy - lab[:, 2] / 200.0
    f = np.stack([fx, fy, fz], axis=1)
    xyz = np.where(f ** 3 > 216.0 / 24389.0, f ** 3, (116.0 * f - 16.0) * 27.0 / 24389.0) * _D65
    lin = xyz @ np.linalg.inv(_M).T
    lin = np.clip(lin, 0.0, 1.0)
    return np.where(lin <= 0.0031308, lin * 12.92, 1.055 * np.power(lin, 1 / 2.4) - 0.055)


def kmeans_palette(rgb, k, seed=1, iters=24, max_samples=40000):
    """Deterministic k-means in Lab on (n, 3) sRGB samples. Returns (palette (k', 3) sRGB sorted by population
    descending, population share per entry); k' <= k (fewer when the samples have fewer distinct colours)."""
    rgb = np.asarray(rgb, dtype=np.float64).reshape(-1, 3)
    if len(rgb) == 0:
        return np.zeros((0, 3), dtype=np.float32), np.zeros(0)
    rng = np.random.default_rng(seed)
    if len(rgb) > max_samples:
        rgb = rgb[rng.choice(len(rgb), size=max_samples, replace=False)]
    lab = srgb_to_lab(rgb)
    k = int(max(1, min(k, len(np.unique(np.round(lab, 2), axis=0)))))
    cen = np.empty((k, 3))
    cen[0] = lab[rng.integers(len(lab))]
    d2 = ((lab - cen[0]) ** 2).sum(1)
    for i in range(1, k):                                   # k-means++ start
        tot = d2.sum()
        pick = rng.choice(len(lab), p=d2 / tot) if tot > 1e-12 else rng.integers(len(lab))
        cen[i] = lab[pick]
        d2 = np.minimum(d2, ((lab - cen[i]) ** 2).sum(1))
    for _ in range(int(iters)):
        D = ((lab[:, None, :] - cen[None, :, :]) ** 2).sum(-1)
        lab_id = D.argmin(axis=1)
        new = cen.copy()
        for i in range(k):
            sel = lab_id == i
            if sel.any():
                new[i] = lab[sel].mean(axis=0)
        if np.abs(new - cen).max() < 1e-3:
            cen = new
            break
        cen = new
    D = ((lab[:, None, :] - cen[None, :, :]) ** 2).sum(-1)
    pop = np.bincount(D.argmin(axis=1), minlength=k).astype(np.float64)
    order = np.argsort(-pop, kind="stable")
    pal = lab_to_srgb(cen[order]).astype(np.float32)
    return pal, pop[order] / max(pop.sum(), 1.0)


def snap_to_palette(colors, palette):
    """Nearest palette colour (Lab distance) for every row of (n, 3) sRGB colours."""
    if len(palette) == 0 or len(colors) == 0:
        return np.asarray(colors, dtype=np.float32)
    a = srgb_to_lab(colors)
    b = srgb_to_lab(palette)
    out = np.empty(len(a), dtype=np.int64)
    for s in range(0, len(a), 65536):
        out[s:s + 65536] = (((a[s:s + 65536, None, :] - b[None, :, :]) ** 2).sum(-1)).argmin(axis=1)
    return np.asarray(palette, dtype=np.float32)[out]


def group_weighted_median(gid, val, wgt):
    """Weighted median of val per group id (all arrays 1D). Returns (unique ids sorted, median per id)."""
    order = np.lexsort((val, gid))
    g, v, w = gid[order], val[order], wgt[order]
    cw = np.cumsum(w.astype(np.float64))
    starts = np.flatnonzero(np.concatenate(([True], g[1:] != g[:-1])))
    ends = np.concatenate((starts[1:], [len(g)]))
    base = np.concatenate(([0.0], cw))[starts]
    total = cw[ends - 1] - base
    idx = np.searchsorted(cw, base + 0.5 * total, side="left")
    idx = np.clip(idx, starts, ends - 1)
    return g[starts], v[idx]


def triangle_colors(tri, colors, conf, n_tris, conf_min=0.5):
    """One robust colour per triangle.

    tri: (n,) triangle index of every texel, colors: (n, 3) the texel colours, conf: (n,) confidence 0..1 that a
    texel was coloured from a concept (not guessed). Triangles with a texel of conf >= conf_min use only the
    confident texels (weight conf); the others use all texels (weight 0.05 + conf). Triangles without any
    texel get NaN. Returns (colors (n_tris, 3) float32, info)."""
    tri = np.asarray(tri, dtype=np.int64)
    conf = np.asarray(conf, dtype=np.float64)
    out = np.full((n_tris, 3), np.nan, dtype=np.float32)
    if len(tri) == 0:
        return out, {"triangles_with_texels": 0, "confident_share": 0.0}
    has_conf = np.bincount(tri, weights=(conf >= conf_min).astype(np.float64), minlength=n_tris) > 0
    w = np.where(has_conf[tri], np.where(conf >= conf_min, conf, 0.0), 0.05 + conf)
    for c in range(3):
        ids, med = group_weighted_median(tri, np.asarray(colors[:, c], dtype=np.float64), w)
        out[ids, c] = med
    n_tex = int(np.count_nonzero(np.bincount(tri, minlength=n_tris)))
    return out, {"triangles_with_texels": n_tex,
                 "confident_share": round(float(has_conf.sum()) / max(n_tex, 1), 4)}
