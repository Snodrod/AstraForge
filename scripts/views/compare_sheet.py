"""Silhouette IoU and the contact sheet for the views comparison (ComfyUI venv Python: PIL + numpy).

    python compare_sheet.py <render_dir> <spec.json> <out.png> [out.json]

spec.json: [{"label": "codex_4v", "title": "ChatGPT, 4 views", "views_dir": ".../views/codex"}, ...]
render_dir holds <label>_<view>.png written by compare_render.py (Blender). For every row:
  IoU(front)  render of the mesh vs the FRONT image (the cut-out in norm/cut_front.png, same framing rules)
  IoU(left/back/right) render vs the generated left / back / right image of that row's views folder
The sheet: rows = meshes, columns = front, left side, back, 3/4; untextured, grey backdrop.
"""
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

RES = 1024
CELL = 380
BG = (186, 189, 196)


def mask_of(path):
    return np.asarray(Image.open(path).convert("RGBA").getchannel("A").resize((RES, RES), Image.Resampling.BILINEAR)) > 40


def iou(a, b):
    u = (a | b).sum()
    return float((a & b).sum() / u) if u else 0.0


def font(size):
    for name in ("arial.ttf", "segoeui.ttf", "DejaVuSans.ttf"):
        try:
            return ImageFont.truetype("C:/Windows/Fonts/" + name if not name.startswith("DejaVu") else name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def main(argv):
    render_dir, spec_file, out_png = Path(argv[1]), Path(argv[2]), Path(argv[3])
    out_json = Path(argv[4]) if len(argv) > 4 else out_png.with_suffix(".json")
    spec = json.loads(spec_file.read_text(encoding="utf8"))
    cols = [("front", "front"), ("left", "left"), ("back", "back"), ("q34", "3/4")]
    head, label_w = 34, 330
    sheet = Image.new("RGB", (label_w + CELL * len(cols), head + CELL * len(spec)), (28, 30, 34))
    d = ImageDraw.Draw(sheet)
    f_big, f_small = font(20), font(15)
    for i, (_, name) in enumerate(cols):
        d.text((label_w + i * CELL + 10, 7), name, fill=(220, 224, 230), font=f_big)
    results = {}
    for row, item in enumerate(spec):
        label, vdir = item["label"], Path(item["views_dir"])
        res = {}
        for view in ("front", "left", "back", "right"):
            mesh_png = render_dir / f"{label}_{view}.png"
            target = vdir / "norm" / f"cut_{view}.png"
            if mesh_png.exists() and target.exists():
                res[view] = round(iou(mask_of(mesh_png), mask_of(target)), 4)
        results[label] = {"title": item["title"], "iou": res}
        y = head + row * CELL
        d.text((10, y + 10), item["title"], fill=(240, 242, 246), font=f_big)
        lines = ["Silhouette IoU per view:"] + [f"  {n}: {res[k]:.3f}" for k, n in (("front", "front"), ("left", "left"), ("back", "back"), ("right", "right")) if k in res]
        for j, line in enumerate(lines):
            d.text((10, y + 48 + j * 21), line, fill=(190, 196, 206), font=f_small)
        for i, (view, _) in enumerate(cols):
            png = render_dir / f"{label}_{view}.png"
            if not png.exists():
                continue
            im = Image.open(png).convert("RGBA")
            bbox = im.getchannel("A").getbbox()
            tile = Image.new("RGB", (RES, RES), BG)
            tile.paste(im, (0, 0), im)
            sheet.paste(tile.resize((CELL, CELL), Image.Resampling.LANCZOS), (label_w + i * CELL, y))
    sheet.save(out_png)
    out_json.write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf8")
    print("RESULT " + json.dumps(results, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv)
