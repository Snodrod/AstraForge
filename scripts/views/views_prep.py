"""Image helpers for the "views" (views) feature. Runs in the ComfyUI venv (PIL, numpy, scipy, rembg).

    views_prep.py normalize --out DIR --view front=a.png --view left=b.png ... [--ref front] [--canvas 1536] [--long 1280]
    views_prep.py mvaref    --in front_norm_or_cut.png --out ref.png [--size 768]
    views_prep.py codexref  --in front.png --out ref.png            (optional clean-up of the codex input)

`normalize` cuts every view out of its background (rembg u2net + a colour mask that also catches grey
studio backdrops), scales every view so its figure has the height of the reference view, and puts all
views in ONE framing: the common bounding frame, long side `long` px, centred on a white `canvas`² square.
That is exactly what the shape generator was prepared with (scripts/prepare_concept.py / prep_mv.py), so the
single-view and the multi-view jobs see the same kind of input.

Outputs in DIR for every view: norm_<slot>.png (white canvas, generator input), cut_<slot>.png (RGBA, one
shared frame), thumb_<slot>.jpg (384 px preview), plus frame.json. The last stdout line is the JSON report.
"""
import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
APP = HERE.parent.parent
os.environ.setdefault("U2NET_HOME", str(APP / "data" / "models" / "background"))
os.environ.setdefault("OMP_NUM_THREADS", "4")

THRESH = 5  # alpha above this counts as "figure"


def colour_mask(rgb):
    """255 where a pixel clearly is not the studio backdrop; only the part connected to the figure."""
    from scipy import ndimage
    h, w = rgb.shape[:2]
    f = rgb.astype(np.float64) + 1.0
    yy, xx = np.mgrid[0:h, 0:w]
    yy, xx = yy / h, xx / w
    band = np.zeros((h, w), bool)
    m = max(4, w // 25)
    band[:, :m] = band[:, -m:] = band[:m, :] = True
    band[-m:, :] = False                      # the bottom edge is floor with shadows: no backdrop model there
    A = np.stack([np.ones_like(xx), xx, yy, xx * xx, yy * yy, xx * yy], -1)
    coef, *_ = np.linalg.lstsq(A[band], f[band], rcond=None)
    bg = A @ coef
    r = f / bg
    lum = r.mean(-1)
    tint = np.abs(r - lum[..., None]).max(-1)  # coloured differently than the backdrop
    fg = (tint > 0.05) | (lum > 1.06) | ((lum < 0.55) & (tint > 0.02))
    shadow = (lum < 0.97) & (r[..., 2] - r[..., 0] > 0.015)   # floor shadow: darker than the backdrop and cooler
    fg &= ~shadow
    fg = ndimage.binary_opening(fg, iterations=2)
    lab, n = ndimage.label(fg)
    if n == 0:
        return np.zeros((h, w), np.uint8)
    sizes = ndimage.sum(fg, lab, range(1, n + 1))
    keep = np.isin(lab, 1 + np.flatnonzero(sizes >= sizes.max() * 0.02))
    return (keep * 255).astype(np.uint8)


_SESSION = None


def rembg_session():
    global _SESSION
    if _SESSION is None:
        from rembg import new_session
        _SESSION = new_session("u2net", providers=["CPUExecutionProvider"])
    return _SESSION


def cutout(path, target_height=None, cache=None):
    """RGBA of the figure. A PNG that already has transparency is trusted as it is.

    cache: optional folder; the cut-out of an identical file is reused (rembg is the slow part)."""
    key = None
    if cache:
        import hashlib
        key = Path(cache) / (hashlib.sha1(Path(path).read_bytes()).hexdigest()[:20] + "_%s.png" % (target_height or 0))
        if key.exists():
            return Image.open(key).convert("RGBA")
    out = _cutout(path, target_height)
    if key is not None:
        key.parent.mkdir(parents=True, exist_ok=True)
        out.save(key)
    return out


def _cutout(path, target_height=None):
    im = Image.open(path)
    has_alpha = im.mode in ("RGBA", "LA") and im.getchannel("A").getextrema()[0] < 250
    im = im.convert("RGBA")
    if target_height and im.height != target_height:
        im = im.resize((round(im.width * target_height / im.height), target_height), Image.Resampling.LANCZOS)
    if has_alpha:
        return im
    from rembg import remove
    c = remove(im, session=rembg_session(), post_process_mask=False)
    a = np.array(c.getchannel("A"))
    a[a < 12] = 0                                  # the faint column of backdrop rembg leaves between legs / under arms
    a = np.maximum(a, colour_mask(np.asarray(im)[..., :3]))
    rgba = np.array(im)                            # original colour, rembg alpha (rembg darkens edges)
    rgba[..., 3] = a
    return Image.fromarray(rgba)


def bbox_of(img):
    return img.getchannel("A").point(lambda x: 255 if x > THRESH else 0).getbbox()


def normalize(views, out, ref="front", canvas=1536, long_side=1280, cache=None):
    out.mkdir(parents=True, exist_ok=True)
    if ref not in views:
        ref = next(iter(views))
    cut = {}
    boxes = {}
    for slot, path in views.items():
        # one working height for every view; the reference view keeps its own resolution
        h = 1448
        c = cutout(path, h, cache)
        b = bbox_of(c)
        if not b:
            raise SystemExit("Could not segment the figure in view \"%s\"." % slot)
        cut[slot], boxes[slot] = c, b
    hf = boxes[ref][3] - boxes[ref][1]
    scales = {}
    for slot in list(cut):
        k = hf / (boxes[slot][3] - boxes[slot][1])
        scales[slot] = round(k, 4)
        if abs(k - 1) > 0.01:
            c = cut[slot]
            cut[slot] = c.resize((round(c.width * k), round(c.height * k)), Image.Resampling.LANCZOS)
            boxes[slot] = bbox_of(cut[slot])
    bw = max(b[2] - b[0] for b in boxes.values())
    bh = max(b[3] - b[1] for b in boxes.values())
    s = long_side / max(bw, bh)
    report = {"frame": [bw, bh], "scale": s, "canvas": canvas, "long": long_side, "ref": ref, "views": {}}
    for slot, c in cut.items():
        b = boxes[slot]
        cx, cy = (b[0] + b[2]) / 2, (b[1] + b[3]) / 2
        x0, y0 = round(cx - bw / 2), round(cy - bh / 2)
        fr = Image.new("RGBA", (bw, bh), (0, 0, 0, 0))
        fr.paste(c.crop((max(x0, 0), max(y0, 0), min(x0 + bw, c.width), min(y0 + bh, c.height))), (max(-x0, 0), max(-y0, 0)))
        g = fr.resize((max(1, round(bw * s)), max(1, round(bh * s))), Image.Resampling.LANCZOS)
        cv = Image.new("RGB", (canvas, canvas), "white")
        cv.paste(g, ((canvas - g.width) // 2, (canvas - g.height) // 2), g)
        cv.save(out / ("norm_%s.png" % slot))
        rgba = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
        rgba.paste(g, ((canvas - g.width) // 2, (canvas - g.height) // 2))
        rgba.save(out / ("cut_%s.png" % slot))
        th = cv.copy()
        th.thumbnail((384, 384), Image.Resampling.LANCZOS)
        th.save(out / ("thumb_%s.jpg" % slot), quality=88)
        area = int((np.asarray(g.getchannel("A")) > THRESH).sum())
        report["views"][slot] = {"bbox": list(b), "scale": scales[slot], "area": area}
    (out / "frame.json").write_text(json.dumps(report, indent=2), encoding="utf8")
    return report


def mva_reference(src, dst, size=768):
    """Reference image for MV-Adapter: the figure's longest side is 90 % of `size`, grey (0.5) background."""
    im = cutout(src)
    a = np.array(im.convert("RGBA"))
    ys, xs = np.nonzero(a[..., 3] > 0)
    y0, y1 = max(ys.min() - 1, 0), min(ys.max() + 1, a.shape[0])
    x0, x1 = max(xs.min() - 1, 0), min(xs.max() + 1, a.shape[1])
    crop = a[y0:y1, x0:x1]
    h, w = crop.shape[:2]
    if h > w:
        w, h = int(w * size * 0.9 / h), int(size * 0.9)
    else:
        h, w = int(h * size * 0.9 / w), int(size * 0.9)
    crop = np.array(Image.fromarray(crop).resize((max(w, 1), max(h, 1)), Image.Resampling.LANCZOS))
    canvas = np.zeros((size, size, 4), np.uint8)
    sy, sx = (size - h) // 2, (size - w) // 2
    canvas[sy:sy + h, sx:sx + w] = crop
    f = canvas.astype(np.float32) / 255.0
    f = f[..., :3] * f[..., 3:4] + (1 - f[..., 3:4]) * 0.5
    Image.fromarray((f * 255).clip(0, 255).astype(np.uint8)).save(dst)
    return {"size": size, "figure": [w, h]}


def main(argv):
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)
    n = sub.add_parser("normalize")
    n.add_argument("--out", required=True)
    n.add_argument("--view", action="append", default=[], help="slot=path")
    n.add_argument("--ref", default="front")
    n.add_argument("--canvas", type=int, default=1536)
    n.add_argument("--long", type=int, default=1280)
    n.add_argument("--cache", default=None)
    m = sub.add_parser("mvaref")
    m.add_argument("--in", dest="src", required=True)
    m.add_argument("--out", required=True)
    m.add_argument("--size", type=int, default=768)
    t = sub.add_parser("topng")
    t.add_argument("--in", dest="src", required=True)
    t.add_argument("--out", required=True)
    c = sub.add_parser("cutout")
    c.add_argument("--in", dest="src", required=True)
    c.add_argument("--out", required=True)
    a = p.parse_args(argv[1:])
    if a.cmd == "normalize":
        views = {}
        for item in a.view:
            slot, _, path = item.partition("=")
            views[slot] = path
        res = normalize(views, Path(a.out), a.ref, a.canvas, a.long, a.cache)
    elif a.cmd == "mvaref":
        res = mva_reference(a.src, a.out, a.size)
    elif a.cmd == "topng":
        im = Image.open(a.src)
        im.convert("RGBA" if im.mode in ("RGBA", "LA", "P") else "RGB").save(a.out)
        res = {"out": a.out}
    else:
        cutout(a.src).save(a.out)
        res = {"out": a.out}
    print("RESULT " + json.dumps(res, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
