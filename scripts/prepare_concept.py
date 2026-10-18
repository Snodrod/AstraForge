"""CPU background removal using the existing ComfyUI environment, with an exhaust-haze filter.

Usage (run by server/preprocess.mjs with the ComfyUI venv python):
    prepare_concept.py <source> <destination> <mode> [<reference.png> [<metadata.json>]]
    prepare_concept.py --selftest

<mode> is `<background>[:haze=<auto|on|off>]`:
    background  auto  - rembg u2net unless the PNG already has transparency (unchanged)
                alpha - the PNG must already have transparency (unchanged)
                keep  - keep the source pixels and alpha as they are (unchanged)
    haze        auto  - (default) filter only when the subject looks like a vehicle/ship; skipped for 'keep'
                on    - always apply the filter
                off   - never apply the filter
Examples: 'auto', 'keep', 'auto:haze=on', 'alpha:haze=off'. Plain 'auto'/'alpha'/'keep' behave as before
and use haze=auto.

Haze filter (filter_haze). rembg u2net keeps semi-transparent engine exhaust (haze, plumes, glow) in the
alpha, and the 3D shape generator then models it as long open "petal" nozzles. The filter works on the
alpha only, after background removal (or on the alpha of an input PNG) and before the crop/bounds:
  1. core = alpha >= 200, only the large 8-connected components (>= 2 % of the largest; specks dropped);
     soft = alpha > 5 and not core.
  2. Soft pixels are opened with a disc of radius r = 0.6 % of sqrt(core area): thin anti-aliasing halos
     around the hull disappear, wide soft blobs remain. Each blob is a haze candidate.
  3. A candidate is haze when ALL hold: it is large (>= 0.3 % of the core area); its soft component touches
     the core; its centroid lies in the outer 20 % of the core along a principal axis of the core (or
     beyond the core); at least half of it lies outside the convex hull of the core (it protrudes); and
     it is haze-coloured (>= 60 % of its alpha-weighted pixels are dark/smoky, grey, warm exhaust
     orange/red/yellow or bright cool glow).
  4. Haze pixels (the blob, a small margin and the thin tails of the same plume) become alpha 0. Only
     pixels with alpha < 200 farther than 2 px from the core are ever touched, so the opaque core and its
     anti-aliased edge are never modified.
  mode 'auto' applies the filter only when the core's bounding box is at least 1.25 times wider than tall
  (ships, vehicles lying horizontally); portrait or squarish subjects (characters) are never touched.
Limits: haze that rembg made fully opaque (alpha >= 200) is not removed; a vertical vehicle needs haze=on;
a soft translucent part at the end of a ship (glass fin, force field) looks the same as haze to the filter.

The statistics go to the metadata JSON under "haze": {mode, applied, removed_px, components, reason}.
"""
import json
import os
import re
import sys
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "4")
import numpy as np
from PIL import Image, ImageDraw

BACKGROUNDS = ("auto", "alpha", "keep")
HAZE_MODES = ("auto", "on", "off")
# Tunable thresholds of the haze filter (all sizes are relative, so the filter is resolution independent).
HAZE_PARAMS = {
    "core_alpha": 200,         # alpha >= this is the opaque core
    "soft_alpha": 5,           # alpha <= this is empty (same threshold as the crop bounds below)
    "core_min_frac": 0.02,     # core components smaller than this share of the largest one are specks
    "thick_frac": 0.006,       # opening radius as a share of sqrt(core area): wide soft blobs only
    "min_area_frac": 0.003,    # candidate area as a share of the core area
    "extremity": 0.20,         # outer share of the core extent along a principal axis
    "protrude": 0.5,           # share of the candidate lying outside the convex hull of the core
    "colour": 0.6,             # share of haze-coloured pixels in the candidate
    "margin_mult": 2,          # removal margin in opening radii
    "auto_aspect": 1.25,       # core bbox width / height needed for haze=auto
}


def parse_mode(text):
    """'<background>[:haze=<auto|on|off>]' -> (background, haze). Raises ValueError for anything else."""
    match = re.fullmatch(r"(auto|alpha|keep)(?::haze=(auto|on|off))?", (text or "auto").strip())
    if not match:
        raise ValueError("Unknown mode %r: expected <auto|alpha|keep>[:haze=<auto|on|off>]" % (text,))
    return match.group(1), match.group(2) or "auto"


def _haze_coloured(rgb):
    """Per-pixel test (rgb float 0..255, shape (n, 3)): smoke/dark, grey, warm exhaust or bright cool glow."""
    mx = rgb.max(axis=1)
    mn = rgb.min(axis=1)
    value = mx / 255.0
    sat = np.where(mx > 0, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    r, g, b = rgb[:, 0], rgb[:, 1], rgb[:, 2]
    delta = np.maximum(mx - mn, 1e-6)
    hue = np.where(mx == r, ((g - b) / delta) % 6, np.where(mx == g, (b - r) / delta + 2, (r - g) / delta + 4)) * 60.0
    dark = value < 0.30
    grey = sat < 0.30
    warm = ((hue <= 70) | (hue >= 340)) & (sat >= 0.30)
    cool = (hue >= 170) & (hue <= 260) & (value >= 0.55)
    return dark | grey | warm | cool


def _hull_mask(core, ndimage):
    """Filled convex hull of a boolean mask (PIL polygon; bounding box if the hull is degenerate)."""
    h, w = core.shape
    edge = core & ~ndimage.binary_erosion(core)
    ys, xs = np.nonzero(edge)
    if len(xs) > 40000:
        pick = np.linspace(0, len(xs) - 1, 40000).astype(int)
        ys, xs = ys[pick], xs[pick]
    try:
        from scipy.spatial import ConvexHull
        points = np.stack([xs, ys], axis=1).astype(float)
        vertices = points[ConvexHull(points).vertices]
        canvas = Image.new("L", (w, h), 0)
        ImageDraw.Draw(canvas).polygon([tuple(p) for p in vertices], fill=1)
        return np.array(canvas, dtype=bool) | core
    except Exception:
        mask = np.zeros_like(core)
        mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1] = True
        return mask


def filter_haze(alpha, rgb, mode="auto", params=None):
    """Remove semi-transparent exhaust haze from an alpha channel.

    alpha: uint8 (h, w); rgb: uint8 (h, w, 3) or None (then colour is not tested);
    mode: 'auto' | 'on' | 'off'. Returns (new_alpha, info) with info = {mode, applied, removed_px,
    components, reason}. new_alpha is `alpha` itself (not a copy) when nothing was removed.
    """
    p = dict(HAZE_PARAMS, **(params or {}))
    info = {"mode": mode, "applied": False, "removed_px": 0, "components": 0, "reason": ""}
    if mode == "off":
        info["reason"] = "haze=off"
        return alpha, info
    try:
        from scipy import ndimage
    except ImportError:
        info["reason"] = "scipy is not available"
        return alpha, info
    a = np.asarray(alpha)
    core_all = a >= p["core_alpha"]
    if not core_all.any():
        info["reason"] = "no opaque core (alpha >= %d)" % p["core_alpha"]
        return alpha, info
    eight = np.ones((3, 3), dtype=bool)
    lab, n = ndimage.label(core_all, structure=eight)
    sizes = ndimage.sum(core_all, lab, range(1, n + 1))
    keep = 1 + np.nonzero(sizes >= p["core_min_frac"] * sizes.max())[0]
    core = np.isin(lab, keep)
    core_area = int(core.sum())
    ys, xs = np.nonzero(core)
    bw, bh = int(xs.max() - xs.min() + 1), int(ys.max() - ys.min() + 1)
    aspect = bw / max(bh, 1)
    if mode == "auto" and aspect < p["auto_aspect"]:
        info["reason"] = "auto: subject is not vehicle-like (core bbox %dx%d, width/height %.2f < %.2f)" % (bw, bh, aspect, p["auto_aspect"])
        return alpha, info
    soft = (a > p["soft_alpha"]) & ~core
    if not soft.any():
        info["reason"] = "no soft pixels outside the core"
        return alpha, info
    scale = float(np.sqrt(core_area))
    radius = max(2.0, round(p["thick_frac"] * scale))
    # Wide soft blobs: morphological opening of the soft mask with a disc (via distance transforms).
    eroded = ndimage.distance_transform_edt(soft) > radius
    if not eroded.any():
        info["reason"] = "only thin soft edges outside the core, no haze"
        return alpha, info
    blobs = (ndimage.distance_transform_edt(~eroded) <= radius) & soft
    blob_lab, blob_n = ndimage.label(blobs, structure=eight)
    min_area = p["min_area_frac"] * core_area
    areas = np.bincount(blob_lab.ravel(), minlength=blob_n + 1)
    candidates = [i for i in range(1, blob_n + 1) if areas[i] >= min_area]
    if not candidates:
        info["reason"] = "no wide soft blob of at least %d px outside the core" % int(min_area)
        return alpha, info
    soft_lab, _ = ndimage.label(soft, structure=eight)
    touching = np.unique(soft_lab[ndimage.binary_dilation(core, iterations=2) & soft])
    hull = _hull_mask(core, ndimage)
    # Principal axes of the core. The long one is where exhaust sits; a near-isotropic core tests both.
    centre = np.array([xs.mean(), ys.mean()])
    coords = np.stack([xs, ys], axis=1).astype(float) - centre
    evals, evecs = np.linalg.eigh(np.cov(coords.T))
    axes = [evecs[:, 1]]
    if evals[0] > 0.64 * evals[1]:
        axes.append(evecs[:, 0])
    spans = []
    for axis in axes:
        t = coords @ axis
        spans.append((float(t.min()), float(t.max())))
    haze = np.zeros_like(soft)
    found = []
    for idx in candidates:
        comp = blob_lab == idx
        area = int(areas[idx])
        cy, cx = ndimage.center_of_mass(comp)
        if not np.isin(soft_lab[comp], touching).any():
            continue
        end = None
        for axis, (tmin, tmax) in zip(axes, spans):
            frac = (((cx - centre[0]) * axis[0] + (cy - centre[1]) * axis[1]) - tmin) / max(tmax - tmin, 1.0)
            if frac >= 1 - p["extremity"] or frac <= p["extremity"]:
                direction = axis if frac >= 0.5 else -axis
                end = ("right" if direction[0] > 0 else "left") if abs(direction[0]) >= abs(direction[1]) else ("bottom" if direction[1] > 0 else "top")
                break
        if end is None:
            continue
        outside = float((comp & ~hull).sum()) / area
        if outside < p["protrude"]:
            continue
        coloured = 1.0
        if rgb is not None:
            ys_c, xs_c = np.nonzero(comp)
            weights = a[ys_c, xs_c].astype(float)
            ok = _haze_coloured(np.asarray(rgb)[ys_c, xs_c, :3].astype(float))
            coloured = float((weights * ok).sum() / max(weights.sum(), 1.0))
        if coloured < p["colour"]:
            continue
        haze |= comp
        found.append("area %d px (%.1f%% of core), %.0f%% outside hull, %.0f%% haze-coloured, at the %s end of the subject"
                     % (area, 100.0 * area / core_area, 100 * outside, 100 * coloured, end))
    if not found:
        info["reason"] = "no haze: %d wide soft blob(s), none qualifies (contact, axis end, protrusion, colour)" % len(candidates)
        return alpha, info
    # Removal: the haze blobs, a small margin around them, and the thin tails of the same plume that the
    # opening dropped (reached through soft pixels farther than the halo band from the core, never
    # through another wide soft blob, so a glow around the hull is not eaten).
    other = blobs & ~haze
    band = p["margin_mult"] * radius
    free = soft & ~other & (ndimage.distance_transform_edt(~core) > band)
    remove = ndimage.binary_propagation(haze, mask=free | haze, structure=eight)
    remove |= (ndimage.distance_transform_edt(~haze) <= radius + 1) & soft & ~other
    remove &= ~ndimage.binary_dilation(core, iterations=2)   # keep the anti-aliased edge of the core itself
    out = a.copy()
    out[remove] = 0
    info.update(applied=True, removed_px=int((a[remove] > 0).sum()), components=len(found),
                reason="removed %d haze component(s): %s" % (len(found), "; ".join(found)))
    return out, info


def _selftest():
    from scipy import ndimage
    results = []

    def check(name, ok, note=""):
        results.append(bool(ok))
        print("%s  %s%s" % ("PASS" if ok else "FAIL", name, ("  [" + note + "]") if note else ""), flush=True)

    def smooth(mask, sigma=1.4):
        return np.clip(ndimage.gaussian_filter(mask.astype(float), sigma) * 255, 0, 255).astype(np.uint8)

    def ship(plume):
        h, w = 360, 720
        yy, xx = np.mgrid[0:h, 0:w]
        hull = (np.abs(xx - 260) / 200.0) ** 4 + (np.abs(yy - 180) / 62.0) ** 2 <= 1   # wide rounded hull
        rim = ((xx - 470) ** 2 / 20.0 ** 2 + (yy - 150) ** 2 / 34.0 ** 2 <= 1) | ((xx - 470) ** 2 / 20.0 ** 2 + (yy - 215) ** 2 / 34.0 ** 2 <= 1)
        hull = hull | rim                       # two nozzle bells at the stern
        alpha = smooth(hull, 1.4)
        rgb = np.empty((h, w, 3), np.uint8)
        rgb[:] = (170, 175, 185)
        if plume:
            u = np.clip((xx - 480) / 200.0, 0, 1)                        # 0 at the bells, 1 at the tip
            centre = 182 + 18 * u
            half = 52 * (1 - 0.6 * u)
            profile = np.clip(1 - np.abs(yy - centre) / half, 0, 1) ** 0.7
            soft = (170 * (1 - u) ** 0.8 * profile * (xx >= 470)).astype(np.uint8)
            soft = np.where(hull, 0, soft)
            alpha = np.maximum(alpha, soft)
            warm = np.array([255, 150, 50.0])
            grey = np.array([140, 140, 145.0])
            mix = np.clip(u * 1.5, 0, 1)[..., None]
            plume_rgb = (warm * (1 - mix) + grey * mix).astype(np.uint8)
            rgb = np.where((soft > 0)[..., None], plume_rgb, rgb)
        return alpha, rgb, hull

    # (a) ship with a soft plume at the stern: plume removed, hull intact
    alpha, rgb, hull = ship(True)
    out, info = filter_haze(alpha, rgb, "auto")
    iou = ((out > 128) & hull).sum() / max(((out > 128) | hull).sum(), 1)
    plume_left = int((out[:, 500:] > 5).sum())
    check("a: soft plume at one end is removed", info["applied"] and plume_left == 0 and info["components"] >= 1,
          "removed %d px, %d components, pixels with alpha > 5 beyond x=500: %d" % (info["removed_px"], info["components"], plume_left))
    check("a: hull stays intact (IoU >= 0.98)", iou >= 0.98 and np.array_equal(out[alpha >= 200], alpha[alpha >= 200]), "IoU %.4f" % iou)
    # (b) portrait character with a soft hair edge and a soft glow at the feet: auto leaves it unchanged
    h, w = 700, 360
    yy, xx = np.mgrid[0:h, 0:w]
    body = ((xx - 180) ** 2 / 60.0 ** 2 + (yy - 440) ** 2 / 200.0 ** 2 <= 1) | ((xx - 180) ** 2 + (yy - 190) ** 2 <= 55 ** 2)
    alpha_b = smooth(body, 1.4)
    hair = ndimage.gaussian_filter(((xx - 180) ** 2 + (yy - 175) ** 2 <= 68 ** 2).astype(float), 7) * 255
    glow = 150 * np.exp(-(((xx - 180) / 90.0) ** 2 + ((yy - 655) / 28.0) ** 2))
    alpha_b = np.maximum(alpha_b, np.clip(np.maximum(hair * 0.8, glow), 0, 255).astype(np.uint8))
    rgb_b = np.empty((h, w, 3), np.uint8)
    rgb_b[:] = (200, 120, 70)
    out, info = filter_haze(alpha_b, rgb_b, "auto")
    check("b: portrait character is untouched in auto", np.array_equal(out, alpha_b) and not info["applied"], info["reason"])
    out_on, info_on = filter_haze(alpha_b, rgb_b, "on")
    glow_left = int((out_on[650:, :] > 5).sum())
    check("b2: haze=on removes the soft glow under the vertical subject, opaque body stays",
          info_on["applied"] and glow_left == 0 and (alpha_b[650:, :] > 5).sum() > 5000 and np.array_equal(out_on[alpha_b >= 200], alpha_b[alpha_b >= 200]),
          "glow px below the feet before/after: %d/%d" % ((alpha_b[650:, :] > 5).sum(), glow_left))
    # (c) haze off leaves everything
    alpha, rgb, hull = ship(True)
    out, info = filter_haze(alpha, rgb, "off")
    check("c: haze=off leaves the plume", np.array_equal(out, alpha) and not info["applied"], info["reason"])
    # (d) wide ship without a plume (only anti-aliasing halo and a soft rim): unchanged
    alpha, rgb, hull = ship(False)
    alpha = np.maximum(alpha, (ndimage.gaussian_filter(hull.astype(float), 5) * 120).astype(np.uint8))
    out, info = filter_haze(alpha, rgb, "auto")
    check("d: ship without plume is unchanged", np.array_equal(out, alpha) and not info["applied"], info["reason"])
    # (e) fully opaque input and mode parsing
    out, info = filter_haze(np.full((50, 80), 255, np.uint8), None, "auto")
    check("e: opaque image is unchanged", np.array_equal(out, np.full((50, 80), 255, np.uint8)) and not info["applied"], info["reason"])
    parsed = [parse_mode(x) for x in ("auto", "keep", "alpha", "auto:haze=on", "keep:haze=off", None)]
    check("f: mode argument parsing", parsed == [("auto", "auto"), ("keep", "auto"), ("alpha", "auto"), ("auto", "on"), ("keep", "off"), ("auto", "auto")], str(parsed))
    try:
        parse_mode("auto:haze=maybe")
        bad = False
    except ValueError:
        bad = True
    check("g: invalid haze mode is rejected", bad)
    print("%d/%d passed" % (sum(results), len(results)))
    return 0 if all(results) else 1


def main(argv):
    if len(argv) > 1 and argv[1] == "--selftest":
        return _selftest()
    source, destination = map(Path, argv[1:3])
    image = Image.open(source).convert("RGBA")
    background, haze_mode = parse_mode(argv[3] if len(argv) > 3 else "auto")
    has_alpha = image.getchannel("A").getextrema()[0] < 250
    if background == "alpha" and not has_alpha:
        raise ValueError("The PNG has no transparency. Choose automatic removal or keep background.")
    if background == "keep" or has_alpha:
        cutout = image
    else:
        from rembg import new_session, remove
        session = new_session("u2net", providers=["CPUExecutionProvider"])
        cutout = remove(image, session=session, post_process_mask=False)
    # Exhaust haze filter on the alpha, before the crop. 'keep' means "do not touch the source", so there
    # haze=auto does nothing; an explicit haze=on still filters.
    if background == "keep" and haze_mode == "auto":
        haze = {"mode": haze_mode, "applied": False, "removed_px": 0, "components": 0, "reason": "background=keep"}
    else:
        alpha_array = np.array(cutout.getchannel("A"))
        new_alpha, haze = filter_haze(alpha_array, np.array(cutout.convert("RGB")), haze_mode)
        if haze["applied"]:
            cutout = cutout.copy()
            cutout.putalpha(Image.fromarray(new_alpha))
    print("Haze filter: " + json.dumps(haze, ensure_ascii=False), flush=True)
    alpha = cutout.getchannel("A")
    bounds = alpha.point(lambda x: 255 if x > 5 else 0).getbbox()
    if not bounds:
        raise ValueError("Background removal returned an empty object mask")
    cutout = cutout.crop(bounds)
    if len(argv) > 4:
        # Keep an independent, alpha-matted texture source. The geometry encoder's
        # white 1280-pixel input must never become the only surviving reference.
        reference_scale = min(1.0, 3410 / max(cutout.size))
        reference_cutout = cutout.resize((max(1, round(cutout.width*reference_scale)), max(1, round(cutout.height*reference_scale))), Image.Resampling.LANCZOS)
        side = min(4096, max(64, int(max(reference_cutout.size) / (5/6) + .999)))
        reference = Image.new('RGBA', (side, side), (0, 0, 0, 0))
        left, top = (side-reference_cutout.width)//2, (side-reference_cutout.height)//2
        reference.paste(reference_cutout, (left, top))
        reference.save(argv[4], 'PNG')
        if len(argv) > 5:
            Path(argv[5]).write_text(json.dumps({'originalSize': list(image.size), 'crop': list(bounds), 'size': side, 'coverage': max(reference_cutout.size)/side, 'contentRect': [left, top, reference_cutout.width, reference_cutout.height], 'method': 'original-rgb-with-rembg-alpha', 'upscaled': False, 'haze': haze}), encoding='utf8')
    scale = 1280 / max(cutout.size)
    cutout = cutout.resize((max(1, round(cutout.width*scale)), max(1, round(cutout.height*scale))), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (1536, 1536), "white")
    canvas.paste(cutout, ((1536-cutout.width)//2, (1536-cutout.height)//2), cutout)
    canvas.save(destination, "PNG")
    print("Concept prepared", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
