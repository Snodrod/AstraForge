"""Contact sheet of view sets: one row per views folder, columns front / left / back / right.

    python contact_sheet.py <out.jpg> "<row title>=<views_dir>" ["<row title>=<views_dir>" ...]

Uses norm/thumb_<slot>.jpg of every folder (the images the shape generator gets), so what you see is what it sees.
ComfyUI venv Python (PIL).
"""
import json
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

COLS = [("front", "front"), ("left", "left"), ("back", "back"), ("right", "right")]
CELL = 384


def font(size):
    for name in ("arial.ttf", "segoeui.ttf"):
        try:
            return ImageFont.truetype("C:/Windows/Fonts/" + name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def main(argv):
    out = Path(argv[1])
    rows = [a.split("=", 1) for a in argv[2:]]
    label_w, head = 270, 34
    sheet = Image.new("RGB", (label_w + CELL * len(COLS), head + CELL * len(rows)), (28, 30, 34))
    d = ImageDraw.Draw(sheet)
    big, small = font(20), font(14)
    for i, (_, name) in enumerate(COLS):
        d.text((label_w + i * CELL + 10, 7), name, fill=(220, 224, 230), font=big)
    for r, (title, folder) in enumerate(rows):
        folder = Path(folder)
        views = json.loads((folder / "views.json").read_text(encoding="utf8"))
        y = head + r * CELL
        d.text((10, y + 12), title, fill=(240, 242, 246), font=big)
        for i, (slot, _) in enumerate(COLS):
            thumb = folder / "norm" / ("thumb_%s.jpg" % slot)
            if thumb.exists():
                sheet.paste(Image.open(thumb).convert("RGB").resize((CELL, CELL)), (label_w + i * CELL, y))
            info = views["slots"].get(slot, {})
            src = {"input": "source", "codex": "ChatGPT", "mvadapter": "MV-Adapter", "manual": "your file"}.get(info.get("source"), "")
            sec = info.get("seconds")
            if src:
                d.text((label_w + i * CELL + 8, y + CELL - 22), src + (" · %d s" % round(sec) if sec else ""), fill=(120, 126, 136), font=small)
    sheet.save(out, quality=90)
    print("RESULT " + json.dumps({"out": str(out), "rows": len(rows)}))


if __name__ == "__main__":
    main(sys.argv)
