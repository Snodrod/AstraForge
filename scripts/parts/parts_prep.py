"""Part prep for "generate by parts" (docs/forge/parts.md).

Cuts a T-pose character reference pair (front + back RGBA, same framing, alpha = subject) into body
parts - head, torso, left / right arm, legs - and writes, for every part, the multi-view input of
Hunyuan3D-2mv in the format of the whole-body generator (white 1536 px canvas, subject long side
1280 px, front and back share ONE framing):

    <out>/<part>/mv_front.png, mv_back.png
    <out>/parts.json           boxes in reference pixels (front, back), landmarks, crop -> canvas transform
    <out>/boxes_preview.png    the boxes over both references

Boxes come from the FRONT silhouette (override: --boxes). The back boxes are the front boxes mirrored
in X about the trunk centre line of each view (character left is the viewer's right in front view and
the viewer's left in back view), then shifted by a small search so the mirrored front crop and the
back crop overlap best.  Every joint is crossed by `margin` (default 4.5 % of the height) on both sides,
so neighbouring parts overlap and the assembler has material to register and to fuse.

    <ComfyUI>/.venv/Scripts/python.exe parts_prep.py --front ref_front.png --back ref_back.png --out parts/
"""
import argparse
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage

FORMAT = "astraforge.parts/1"
PART_ORDER = ("head", "torso", "arm_l", "arm_r", "legs")
CANVAS = 1536
LONG_SIDE = 1280
COLORS = {"head": (255, 80, 80), "torso": (60, 200, 90), "arm_l": (70, 130, 255), "arm_r": (255, 190, 40),
          "legs": (200, 80, 220)}


# --------------------------------------------------------------------------- image basics
def load_ref(path):
    im = Image.open(path).convert("RGBA")
    return np.array(im)


def silhouette(rgba, thr=128):
    """Boolean subject mask (alpha >= thr), specks below 2 % of the largest piece dropped."""
    m = rgba[..., 3] >= thr
    lab, n = ndimage.label(m)
    if n == 0:
        raise SystemExit("the reference has no subject (alpha is empty)")
    sizes = ndimage.sum(m, lab, range(1, n + 1))
    keep = 1 + np.flatnonzero(sizes >= sizes.max() * 0.02)
    return np.isin(lab, keep)


def runs_of(row, min_len=1):
    d = np.diff(np.concatenate([[0], row.astype(np.int8), [0]]))
    st, en = np.flatnonzero(d == 1), np.flatnonzero(d == -1)
    return [(int(a), int(b) - 1) for a, b in zip(st, en) if b - a >= min_len]


def bbox_of(m):
    ys, xs = np.nonzero(m)
    return int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())


# --------------------------------------------------------------------------- landmarks
def landmarks(m):
    """Row-profile landmarks of a T-pose silhouette (pixels): head / neck / arms / chest / crotch."""
    x0, top, x1, bot = bbox_of(m)
    H = bot - top + 1
    w = m.sum(axis=1).astype(float)
    ws = ndimage.uniform_filter1d(w, 5, mode="nearest")
    # shoulders: the first row clearly wider than everything above it (the head, a helmet included)
    y_sh = None
    back_off = max(int(0.04 * H), 2)       # compare with rows above the neck ramp, not with the ramp itself
    for y in range(top + max(int(0.1 * H), 3), top + int(0.4 * H)):
        head_w = float(ws[top:y - back_off].max())
        if ws[y] > 1.45 * head_w:
            y_sh = y
            break
    if y_sh is None:
        y_sh = top + int(0.2 * H)
    # head: the widest row above the shoulders; neck: the narrowest row between it and the shoulders
    seg = ws[top:max(y_sh - back_off, top + 2)]
    w_head = float(seg.max())
    y_hm = top + int(np.argmax(seg >= 0.97 * w_head))
    y_neck = y_hm + int(np.argmin(ws[y_hm:max(y_sh, y_hm + 2)]))
    # arms (T-pose): rows far wider than the trunk
    w_max = float(w.max())
    rough = np.flatnonzero(w > 0.45 * w_max)
    if not len(rough):
        raise SystemExit("T-pose arms not found: pass --boxes")
    y_r1 = int(rough.max())
    below = np.arange(y_r1 + int(0.03 * H), min(y_r1 + int(0.12 * H), bot))
    w_trunk = float(np.median(w[below])) if len(below) else float(w[y_r1])
    arm_rows = np.flatnonzero(w[y_sh - 2:y_sh + int(0.25 * H)] > 1.5 * w_trunk) + (y_sh - 2)
    if not len(arm_rows):
        raise SystemExit("T-pose arms not found: pass --boxes")
    y_a0, y_a1 = int(arm_rows.min()), int(arm_rows.max())
    # trunk centre line and edges at chest height (just below the arms)
    hx0 = int(np.flatnonzero(m[top:y_neck + 1].any(axis=0))[0])
    hx1 = int(np.flatnonzero(m[top:y_neck + 1].any(axis=0))[-1])
    xc0 = 0.5 * (hx0 + hx1)
    y_ch = min(y_a1 + int(0.05 * H), bot)
    rr = runs_of(m[y_ch], 3)
    if not rr:
        raise SystemExit("no trunk found at chest height: pass --boxes")
    chest = min(rr, key=lambda r: max(r[0] - xc0, xc0 - r[1], 0.0))   # the run holding the head centre
    xc = 0.5 * (chest[0] + chest[1])
    # crotch / hem: going up from the feet, the last row with two separate legs
    min_len = max(3, int(0.015 * H))
    y = bot - int(0.04 * H)
    while y > top + int(0.4 * H) and len(runs_of(m[y], min_len)) < 2:
        y -= 1                                   # toes / boots may touch: first find a gap
    y_c = None
    while y > top + int(0.3 * H):
        if len(runs_of(m[y], min_len)) < 2:
            y_c = y + 1
            break
        y -= 1
    if y_c is None:
        y_c = top + int(0.52 * H)
    return {"x_min": x0, "x_max": x1, "y_top": top, "y_bottom": bot, "height_px": H, "head_width_px": w_head,
            "y_head_widest": y_hm, "y_shoulder": y_sh, "y_neck": y_neck, "y_arm_top": y_a0, "y_arm_bottom": y_a1,
            "y_chest": y_ch, "chest_left": chest[0], "chest_right": chest[1], "centre_x": xc, "y_crotch": y_c,
            "trunk_width_px": w_trunk}


def front_boxes(m, lm, margin, pad):
    """Part boxes [x0, y0, x1, y1] (inclusive px) of the FRONT view."""
    H = lm["height_px"]
    mg, pd = int(round(margin * H)), int(round(pad * H))
    h, w = m.shape
    top, bot, yn, yc = lm["y_top"], lm["y_bottom"], lm["y_neck"], lm["y_crotch"]
    xc = lm["centre_x"]
    cl, cr = lm["chest_left"], lm["chest_right"]
    # head: top .. neck + margin, width of the head / neck rows
    cols = np.flatnonzero(m[top:yn + 1].any(axis=0))
    head = [cols[0] - pd, top - pd, cols[-1] + pd, yn + mg]
    # arms: columns beyond the chest edges, rows they occupy there; crossing the chest edge by margin
    r0, r1 = lm["y_arm_top"] - pd, lm["y_arm_bottom"] + pd
    ys = np.flatnonzero(m[r0:r1 + 1, :cl].any(axis=1)) + r0
    arm_viewer_left = [lm["x_min"] - pd, int(ys.min()) - pd, cl + mg, int(ys.max()) + pd]
    ys = np.flatnonzero(m[r0:r1 + 1, cr + 1:].any(axis=1)) + r0
    arm_viewer_right = [cr - mg, int(ys.min()) - pd, lm["x_max"] + pd, int(ys.max()) + pd]
    # torso: neck - margin .. crotch + margin, trunk width at chest +- margin (and the hem if it is wider)
    rows = slice(lm["y_chest"], yc + 1)           # below the arms (their lower edge still reaches past y_arm_bottom)
    tx = np.flatnonzero(m[rows].any(axis=0))
    torso = [min(cl - mg, int(tx[0])) - pd // 2, yn - mg, max(cr + mg, int(tx[-1])) + pd // 2, yc + mg]
    # legs: crotch - margin .. soles
    lx = np.flatnonzero(m[yc:].any(axis=0))
    legs = [int(lx[0]) - pd, yc - mg, int(lx[-1]) + pd, bot + pd]
    # in the front view the character's left is the viewer's right
    boxes = {"head": head, "torso": torso, "arm_l": arm_viewer_right, "arm_r": arm_viewer_left, "legs": legs}
    out = {}
    for k, b in boxes.items():
        out[k] = [int(max(b[0], 0)), int(max(b[1], 0)), int(min(b[2], w - 1)), int(min(b[3], h - 1))]
    return out


def mirror_box(box, lm_f, lm_b):
    """Front box -> back box: mirrored about each view's trunk centre line, vertical position / size by
    the silhouette heights. Image x of the character's left is +X in front view and -X in back view."""
    k = lm_b["height_px"] / float(lm_f["height_px"])
    d0, d1 = (box[0] - lm_f["centre_x"]) * k, (box[2] - lm_f["centre_x"]) * k
    y0 = lm_b["y_top"] + (box[1] - lm_f["y_top"]) * k
    y1 = lm_b["y_top"] + (box[3] - lm_f["y_top"]) * k
    return [int(round(lm_b["centre_x"] - d1)), int(round(y0)), int(round(lm_b["centre_x"] - d0)), int(round(y1))]


def crop_mask(m, box):
    x0, y0, x1, y1 = box
    out = np.zeros((y1 - y0 + 1, x1 - x0 + 1), bool)
    sx0, sy0, sx1, sy1 = max(x0, 0), max(y0, 0), min(x1, m.shape[1] - 1), min(y1, m.shape[0] - 1)
    if sx1 >= sx0 and sy1 >= sy0:
        out[sy0 - y0:sy1 - y0 + 1, sx0 - x0:sx1 - x0 + 1] = m[sy0:sy1 + 1, sx0:sx1 + 1]
    return out


def align_back(front_m, back_m, f_box, b_box, radius):
    """Shift of the back box (dx, dy) that maximises the IoU between the mirrored front crop and the back
    crop; returns (dx, dy, iou_before, iou_after)."""
    fm = crop_mask(front_m, f_box)[:, ::-1]
    w, h = fm.shape[1], fm.shape[0]

    def iou(dx, dy):
        bm = crop_mask(back_m, [b_box[0] + dx, b_box[1] + dy, b_box[0] + dx + w - 1, b_box[1] + dy + h - 1])
        u = np.logical_or(fm, bm).sum()
        return float(np.logical_and(fm, bm).sum()) / u if u else 0.0

    base = iou(0, 0)
    best = (base, 0, 0)
    for dy in range(-radius, radius + 1, 2):
        for dx in range(-radius, radius + 1, 2):
            v = iou(dx, dy)
            if v > best[0]:
                best = (v, dx, dy)
    _, cx, cy = best
    for dy in range(cy - 1, cy + 2):
        for dx in range(cx - 1, cx + 2):
            v = iou(dx, dy)
            if v > best[0]:
                best = (v, dx, dy)
    return best[1], best[2], base, best[0]


# --------------------------------------------------------------------------- crops
def frame_crop(rgba, box_mask_box, frame):
    """RGBA of the frame rect (may reach outside the image), alpha multiplied by the part box."""
    fx0, fy0, fx1, fy1 = frame
    w, h = fx1 - fx0 + 1, fy1 - fy0 + 1
    out = np.zeros((h, w, 4), np.uint8)
    sx0, sy0, sx1, sy1 = max(fx0, 0), max(fy0, 0), min(fx1, rgba.shape[1] - 1), min(fy1, rgba.shape[0] - 1)
    if sx1 >= sx0 and sy1 >= sy0:
        out[sy0 - fy0:sy1 - fy0 + 1, sx0 - fx0:sx1 - fx0 + 1] = rgba[sy0:sy1 + 1, sx0:sx1 + 1]
    bx0, by0, bx1, by1 = box_mask_box
    inside = np.zeros((h, w), bool)
    inside[max(by0 - fy0, 0):max(by1 - fy0 + 1, 0), max(bx0 - fx0, 0):max(bx1 - fx0 + 1, 0)] = True
    out[..., 3] = np.where(inside, out[..., 3], 0)
    return out


def to_canvas(crop, scale):
    """Resize the frame by `scale` and centre it on the white generator canvas."""
    im = Image.fromarray(crop, "RGBA")
    g = im.resize((max(1, round(im.width * scale)), max(1, round(im.height * scale))), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (CANVAS, CANVAS), "white")
    off = ((CANVAS - g.width) // 2, (CANVAS - g.height) // 2)
    canvas.paste(g, off, g)
    return canvas, list(off)


def draw_preview(path, front, back, parts):
    def prep(rgba):
        im = Image.fromarray(rgba, "RGBA")
        bg = Image.new("RGBA", im.size, (55, 58, 66, 255))
        bg.alpha_composite(im)
        return bg.convert("RGB")
    ims = [prep(front), prep(back)]
    for im, key in zip(ims, ("front_box", "back_box")):
        d = ImageDraw.Draw(im)
        for name, p in parts.items():
            b = p[key]
            d.rectangle(b, outline=COLORS[name], width=3)
            d.text((b[0] + 6, b[1] + 4), name, fill=COLORS[name])
    w = sum(i.width for i in ims) + 10
    sheet = Image.new("RGB", (w, max(i.height for i in ims)), (20, 20, 24))
    sheet.paste(ims[0], (0, 0))
    sheet.paste(ims[1], (ims[0].width + 10, 0))
    sheet = sheet.resize((sheet.width // 2, sheet.height // 2), Image.Resampling.LANCZOS)
    sheet.save(path)


# --------------------------------------------------------------------------- main
def read_boxes(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    src = data.get("parts", data)
    out = {}
    for name, v in src.items():
        if name not in PART_ORDER:
            raise SystemExit("unknown part in --boxes: %s (allowed: %s)" % (name, ", ".join(PART_ORDER)))
        out[name] = (v if isinstance(v, list) else {"front": v.get("front_box"), "back": v.get("back_box")})
    return out


def run(front_path, back_path, out_dir, boxes_file=None, margin=0.045, pad=0.01, align=True, parts_only=None):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    front, back = load_ref(front_path), load_ref(back_path)
    mf, mb = silhouette(front), silhouette(back)
    lf, lb = landmarks(mf), landmarks(mb)
    boxes = front_boxes(mf, lf, margin, pad)
    given = read_boxes(boxes_file) if boxes_file else {}
    back_given = {}
    for name, v in given.items():
        if isinstance(v, list):
            boxes[name] = [int(x) for x in v]
        else:
            if v.get("front"):
                boxes[name] = [int(x) for x in v["front"]]
            if v.get("back"):
                back_given[name] = [int(x) for x in v["back"]]
    names = [n for n in PART_ORDER if (not parts_only or n in parts_only)]
    radius = max(2, int(round(0.015 * lf["height_px"])))
    parts = {}
    for name in names:
        fb = boxes[name]
        bb = back_given.get(name) or mirror_box(fb, lf, lb)
        # same box size in both views (the mirror may be off by one pixel)
        bb = [bb[0], bb[1], bb[0] + (fb[2] - fb[0]), bb[1] + (fb[3] - fb[1])]
        shift, iou0, iou1 = [0, 0], None, None
        if align and name not in back_given:
            dx, dy, iou0, iou1 = align_back(mf, mb, fb, bb, radius)
            bb = [bb[0] + dx, bb[1] + dy, bb[2] + dx, bb[3] + dy]
            shift = [dx, dy]
        sf = bbox_of(crop_mask(mf, fb))
        sb = bbox_of(crop_mask(mb, bb))
        subj_f = [fb[0] + sf[0], fb[1] + sf[1], fb[0] + sf[2], fb[1] + sf[3]]
        subj_b = [bb[0] + sb[0], bb[1] + sb[1], bb[0] + sb[2], bb[1] + sb[3]]
        bw = max(subj_f[2] - subj_f[0], subj_b[2] - subj_b[0]) + 1
        bh = max(subj_f[3] - subj_f[1], subj_b[3] - subj_b[1]) + 1
        scale = LONG_SIDE / float(max(bw, bh))
        frames, canvases = {}, {}
        for view, rgba, box, subj in (("front", front, fb, subj_f), ("back", back, bb, subj_b)):
            cx, cy = (subj[0] + subj[2]) / 2.0, (subj[1] + subj[3]) / 2.0
            fx0, fy0 = int(round(cx - bw / 2.0)), int(round(cy - bh / 2.0))
            frame = [fx0, fy0, fx0 + bw - 1, fy0 + bh - 1]
            img, off = to_canvas(frame_crop(rgba, box, frame), scale)
            d = out_dir / name
            d.mkdir(parents=True, exist_ok=True)
            img.save(d / ("mv_%s.png" % view))
            frames[view], canvases[view] = frame, off
        parts[name] = {
            "dir": name, "mv_front": "%s/mv_front.png" % name, "mv_back": "%s/mv_back.png" % name,
            "front_box": fb, "back_box": bb, "subject_front": subj_f, "subject_back": subj_b,
            "back_shift_px": shift, "mirror_iou_before": None if iou0 is None else round(iou0, 4),
            "mirror_iou_after": None if iou1 is None else round(iou1, 4),
            "frame": {"size_px": [bw, bh], "scale": round(scale, 5), "canvas": CANVAS, "long_side_px": LONG_SIDE,
                      "front_rect_px": frames["front"], "back_rect_px": frames["back"],
                      "front_canvas_offset": canvases["front"], "back_canvas_offset": canvases["back"]},
            "meshes": {}, "mesh": None,
        }
        print("%-6s front box %s  back box %s  frame %dx%d px  x%.2f  mirror IoU %s -> %s" % (
            name, fb, bb, bw, bh, scale, parts[name]["mirror_iou_before"], parts[name]["mirror_iou_after"]), flush=True)
    doc = {"format": FORMAT, "ref": {"front": str(Path(front_path).resolve()), "back": str(Path(back_path).resolve()),
                                     "size": [int(front.shape[1]), int(front.shape[0])]},
           "margin": margin, "pad": pad, "landmarks": {"front": lf, "back": lb}, "parts": parts}
    (out_dir / "parts.json").write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    draw_preview(out_dir / "boxes_preview.png", front, back, parts)
    return doc


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--front", required=True, help="front reference RGBA (alpha = subject)")
    ap.add_argument("--back", required=True, help="back reference RGBA, same framing / height")
    ap.add_argument("--out", required=True, help="output folder")
    ap.add_argument("--boxes", help="JSON with front boxes {part: [x0, y0, x1, y1]} (or a parts.json) overriding the automatic ones")
    ap.add_argument("--margin", type=float, default=0.045, help="joint overlap per side, fraction of the height")
    ap.add_argument("--pad", type=float, default=0.01, help="box padding around free ends, fraction of the height")
    ap.add_argument("--no-align-back", action="store_true", help="mirrored back boxes without the overlap search")
    ap.add_argument("--parts", help="only these parts, comma separated")
    a = ap.parse_args(argv)
    run(a.front, a.back, a.out, a.boxes, a.margin, a.pad, not a.no_align_back,
        set(a.parts.split(",")) if a.parts else None)
    print("parts.json:", Path(a.out).resolve() / "parts.json")


if __name__ == "__main__":
    sys.exit(main())
