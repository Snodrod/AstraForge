"""One command for "generate by parts" (docs/forge/parts.md): prep -> generate -> assemble.

    <ComfyUI>/.venv/Scripts/python.exe build_parts.py --front ref_front.png --back ref_back.png \\
        --base mesh_mv_703.glb --out parts/ [--seeds 703] [--mv-out mesh_mv_parts.glb]

1. parts_prep.py      cut the references into head / torso / arms / legs, write parts/<part>/mv_*.png + parts.json
2. parts_generate.py  Hunyuan3D-2mv per part through ComfyUI (one prompt at a time, waits for an empty queue)
3. assemble_parts     Blender step (scripts/blender/steps/assemble_parts.py): place on the base, trim the joints,
                      fuse (voxel union) -> <out>/assemble/assembled.glb, assembled_mv.glb, preview.png (= compare.png)

Prints the path of the assembled GLB and a short report. Existing meshes are reused (--force regenerates);
--skip-prep / --skip-generate re-run only the later stages (e.g. with other assemble params: --param key=json).
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP = HERE.parent.parent
sys.path.insert(0, str(HERE))

import parts_generate  # noqa: E402
import parts_prep  # noqa: E402

DEFAULT_BLENDER = os.environ.get("ASTRA_BLENDER") or "C:/Program Files/Blender Foundation/Blender 5.0/blender.exe"


def run_assemble(blender, base, front, parts_json, out_dir, params, log=print):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    req = {"step": "assemble_parts", "inputs": {"mesh": str(Path(base).resolve()), "concept": str(Path(front).resolve()),
                                                "parts": str(Path(parts_json).resolve())},
           "params": params, "out_dir": str(out_dir.resolve())}
    req_path = out_dir / "request.json"
    req_path.write_text(json.dumps(req, indent=1), encoding="utf-8")
    cmd = [blender, "-b", "--factory-startup", "--python", str(APP / "scripts" / "blender" / "af_run.py"), "--",
           "assemble_parts", str(req_path)]
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    (out_dir / "log.txt").write_text(proc.stdout + "\n" + proc.stderr, encoding="utf-8")
    res_path = out_dir / "result.json"
    if not res_path.exists():
        raise RuntimeError("Blender produced no result.json (exit %s); see %s" % (proc.returncode, out_dir / "log.txt"))
    res = json.loads(res_path.read_text(encoding="utf-8"))
    res["wall_seconds"] = round(time.time() - t0, 1)
    return res


def report(res, log=print):
    st = res.get("stats", {})
    log("assemble_parts: ok=%s  %.0f s" % (res.get("ok"), res.get("wall_seconds", 0)))
    if res.get("error"):
        log("  error: " + str(res["error"]))
    iou = st.get("iou", {})
    log("  silhouette IoU vs front reference: base %s  assembled %s  (assembled vs base %s)" % (
        iou.get("base_vs_reference"), iou.get("assembled_vs_reference"), iou.get("assembled_vs_base")))
    log("  %-6s %-26s %-9s %-9s %-9s %-8s" % ("part", "scale xyz", "IoU box", "rms mm", "median mm", "thick b/p"))
    for p in st.get("parts", []):
        log("  %-6s %-26s %-9s %-9.2f %-9.2f %-8s" % (
            p["name"], ",".join("%.4f" % v for v in p["scale"]), p["iou_final"], 1000 * p["residual_rms_m"],
            1000 * p["residual_median_m"], p["thickness_ratio_base_part"]))
    log("  tris %s, voxel %.2f mm, boundary edges %s, non-manifold %s, loose parts %s" % (
        st.get("tris"), 1000 * st.get("voxel_m", 0), st.get("boundary_edges"), st.get("non_manifold_edges"), st.get("loose_parts")))
    for w in res.get("warnings", []):
        log("  warning: " + w)
    for c in res.get("checks", []):
        if not c.get("ok"):
            log("  check %s (%s): value %s limit %s" % (c["name"], c.get("severity"), c.get("value"), c.get("limit")))


def annotate_compare(src, dst, size=384, rows=("base (whole-body mesh)", "assembled (by parts)"),
                     cols=("front", "back", "side", "head front", "head 3/4")):
    """compare.png = the step's preview + a header with the column names and the row names."""
    from PIL import Image, ImageDraw, ImageFont
    im = Image.open(src).convert("RGB")
    try:
        font = ImageFont.truetype("arial.ttf", 18)
    except OSError:
        font = ImageFont.load_default()
    head = 30
    out = Image.new("RGB", (im.width, im.height + head), (210, 210, 214))
    out.paste(im, (0, head))
    d = ImageDraw.Draw(out)
    tile_w = im.width / float(len(cols)) if cols else im.width
    for i, c in enumerate(cols):
        d.text((int(i * tile_w) + 8, 6), c, fill=(30, 30, 30), font=font)
    half = im.height // 2
    for i, r in enumerate(rows):
        d.text((8, head + i * half + half - 26), r, fill=(160, 30, 30) if i else (30, 30, 160), font=font)
    out.save(dst)
    return dst


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--front", required=True)
    ap.add_argument("--back", required=True)
    ap.add_argument("--base", required=True, help="whole-body mesh (Hunyuan mesh_mv_<seed>.glb)")
    ap.add_argument("--out", required=True, help="working folder (parts/)")
    ap.add_argument("--seeds", nargs="*", help="e.g. 703 or head=703,11 (default 703)")
    ap.add_argument("--boxes", help="front boxes override (parts_prep --boxes)")
    ap.add_argument("--margin", type=float, default=0.045)
    ap.add_argument("--url", default=parts_generate.DEFAULT_URL)
    ap.add_argument("--blender", default=DEFAULT_BLENDER)
    ap.add_argument("--force", action="store_true", help="regenerate existing part meshes")
    ap.add_argument("--skip-prep", action="store_true")
    ap.add_argument("--skip-generate", action="store_true")
    ap.add_argument("--mv-out", help="copy assembled_mv.glb here (Hunyuan-style seed mesh)")
    ap.add_argument("--param", action="append", default=[], help="assemble_parts param key=json (voxel_m=0.003, target_tris=60000, ...)")
    a = ap.parse_args(argv)
    out = Path(a.out)
    t0 = time.time()
    timings = {}
    if not a.skip_prep:
        t = time.time()
        parts_prep.run(a.front, a.back, out, a.boxes, a.margin)
        timings["prep"] = round(time.time() - t, 1)
    if not a.skip_generate:
        t = time.time()
        parts_generate.generate(out, a.seeds, None, a.url, a.force, log=lambda s: print(s, flush=True))
        timings["generate"] = round(time.time() - t, 1)
    params = {"export_mv": True}
    for kv in a.param:
        k, v = kv.split("=", 1)
        try:
            params[k] = json.loads(v)
        except ValueError:
            params[k] = v
    res = run_assemble(a.blender, a.base, a.front, out / "parts.json", out / "assemble", params)
    timings["assemble"] = res["wall_seconds"]
    report(res, print)
    outs = res.get("outputs", {})
    glb = outs.get("glb")
    if outs.get("preview"):
        try:
            annotate_compare(outs["preview"], out / "compare.png", int(params.get("preview_size", 384)))
        except Exception as exc:                                  # the plain preview is still good
            print("annotation failed (%s), copying the plain preview" % exc)
            shutil.copyfile(outs["preview"], out / "compare.png")
    if a.mv_out and outs.get("glb_mv"):
        Path(a.mv_out).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(outs["glb_mv"], a.mv_out)
        print("Hunyuan-style mesh:", a.mv_out)
    timings["total"] = round(time.time() - t0, 1)
    print("assembled GLB:", glb)
    print("timings s:", json.dumps(timings))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
