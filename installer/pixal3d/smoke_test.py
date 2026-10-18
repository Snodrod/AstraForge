"""Smoke test for the portable ComfyUI-Pixal3D engine (127.0.0.1:8191, shape only). Derived from run_pixal3d_eval.py.

    python_embeded\\python.exe -I tools\\smoke_test.py                       # default: check that the nodes loaded
    python_embeded\\python.exe -I tools\\smoke_test.py --generate --front front.png [--left l.png --back b.png --right r.png]
                                                    [--mode single|mv] [--out DIR] [--tag NAME] [--seed 1000]

Default (--nodes): asks the running server for /object_info and /af/vram and checks that every Trellis2 node used by
the shape graphs is registered. The installer runs this after starting ComfyUI for a few seconds, then stops it.

--generate: a real shape-only run (no texture stage, no nvdiffrast rendering node: nvdiffrast is non-commercial).
The inputs are RGBA cut-outs (alpha = silhouette). Single view needs --front; mv (multi-view) needs all four views
(front/left/back/right at azimuths 0/90/180/270). Writes <out>/<tag>.glb and <out>/<tag>.json (seconds, VRAM peak).
Needs the DINOv3 weights (installed by the setup) and about 7 GB (single) or 10 GB (mv) of free video memory.
"""
import argparse
import json
import shutil
import sys
import time
import urllib.request
import uuid
from pathlib import Path

SRV = "http://127.0.0.1:8191"
INSTALL = Path(__file__).resolve().parents[1]          # <install>\tools\smoke_test.py -> <install>
COMFY_IN = INSTALL / "ComfyUI" / "input"
COMFY_OUT = INSTALL / "ComfyUI" / "output"

# every node class referenced by build()/decode_chain() below
REQUIRED_NODES = [
    "Trellis2LoadModel", "Trellis2LoadImageWithTransparency", "Trellis2PreProcessImage", "Trellis2ImageCondGenerator",
    "Trellis2SparseGenerator", "Trellis2ShapeGenerator", "Trellis2ShapeCascadeGenerator", "Trellis2DecodeLatents",
    "Trellis2FillHolesWithCuMesh", "Trellis2ReconstructMeshWithQuad", "Trellis2SimplifyMesh",
    "Trellis2MeshWithVoxelToTrimesh", "Trellis2ExportMesh",   # meshlib is not installed (non-commercial licence)
    "Trellis2Pixal3DMultiViewConfig", "Trellis2FovMoGeCameraConfig",
]


def http(method, path, data=None, timeout=60):
    req = urllib.request.Request(SRV + path, method=method,
                                 data=None if data is None else json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
        return json.loads(body) if body else {}


def check_nodes():
    info = http("GET", "/object_info", timeout=120)
    missing = [n for n in REQUIRED_NODES if n not in info]
    trellis = sorted(n for n in info if n.startswith("Trellis2"))
    print(f"ComfyUI answers; {len(info)} node classes, {len(trellis)} Trellis2 nodes")
    for n in missing:
        print("MISSING node:", n)
    try:
        v = http("GET", "/af/vram", timeout=15)
        print("VRAM probe ok: %.0f of %.0f MB in use" % (v["used_mb"], v["total_mb"]))
    except Exception as e:
        print("VRAM probe (af_vram_probe.py) not available:", e)
        missing.append("/af/vram")
    return 0 if not missing else 1


def decode_chain(src, prefix, target_faces, use_meshlib=True):
    """DecodeLatents -> fill holes -> quad reconstruct -> simplify -> meshlib holes -> simplify -> trimesh -> glb.

    use_meshlib=False drops the two meshlib nodes (24, 25): the PyPI package `meshlib` (AMV Consulting) is licensed for
    non-commercial / educational use only, see THIRD_PARTY_NOTICES.md. The result then has no second hole-filling pass.
    """
    g = {}
    g["20"] = {"class_type": "Trellis2DecodeLatents", "inputs": {
        "pipeline": src["pipeline"], "shape_slat": src["shape_slat"], "resolution": src["resolution"],
        "use_tiled_decoder": True}}
    g["21"] = {"class_type": "Trellis2FillHolesWithCuMesh", "inputs": {"mesh": ["20", 0], "max_permieters": 1.0}}
    g["22"] = {"class_type": "Trellis2ReconstructMeshWithQuad", "inputs": {
        "mesh": ["21", 0], "remesh_band": 1.0, "resolution": 1024, "remove_floaters": True, "remove_inner_faces": True}}
    g["23"] = {"class_type": "Trellis2SimplifyMesh", "inputs": {"mesh": ["22", 0], "target_face_num": target_faces, "method": "Cumesh"}}
    last = "23"
    if use_meshlib:
        g["24"] = {"class_type": "Trellis2FillHolesWithCuMesh", "inputs": {"mesh": ["23", 0], "max_permieters": 1.0}}
        g["25"] = {"class_type": "Trellis2SimplifyMesh", "inputs": {"mesh": ["24", 0], "target_face_num": target_faces, "method": "Cumesh"}}
        last = "25"
    g["26"] = {"class_type": "Trellis2MeshWithVoxelToTrimesh", "inputs": {"mesh": [last, 0], "reorient_vertices": "None"}}
    g["27"] = {"class_type": "Trellis2ExportMesh", "inputs": {"trimesh": ["26", 0], "filename_prefix": prefix, "file_format": "glb"}}
    return g


def build(mode, names, a):
    mv = mode == "mv"
    g = {}
    g["1"] = {"class_type": "Trellis2LoadModel", "inputs": {
        "modelname": "TencentARC/Pixal3D", "backend": "flash_attn", "device": "cuda", "low_vram": True,
        "keep_models_loaded": False, "conv_backend": "flex_gemm", "sparse_backend": "flash_attn",
        "use_reconviagen": False, "pixal3d_multiview": mv}}
    g["2"] = {"class_type": "Trellis2LoadImageWithTransparency", "inputs": {"image": names["front"]}}
    g["3"] = {"class_type": "Trellis2PreProcessImage", "inputs": {
        "image": ["2", 2], "padding": 5, "remove_background": False, "max_size": 1024}}
    g["4"] = {"class_type": "Trellis2ImageCondGenerator", "inputs": {"pipeline": ["1", 0], "image": ["3", 0], "max_views": 1}}

    sparse_in = {"pipeline": ["4", 2], "image_cond": ["4", 0], "seed": a.seed,
                 "sparse_structure_steps": 25, "sparse_structure_guidance_strength": 7.5,
                 "sparse_structure_guidance_rescale": 0.7, "sparse_structure_rescale_t": 5.0,
                 "sparse_structure_sampler": a.sampler, "sparse_structure_resolution": 32,
                 "sparse_structure_guidance_interval_start": 0.6, "sparse_structure_guidance_interval_end": 1.0,
                 "fill_holes": True, "hole_iterations": 1, "verbose": False, "dino_lock": 0.65, "dino_substeps": 2,
                 "hole_fill_algorithm": "flood_fill", "dino_foundation_cap": 0.92, "keep_only_shell": True}
    shape_common = {"shape_steps": 12, "shape_guidance_strength": 7.5, "shape_guidance_rescale": 0.5,
                    "shape_rescale_t": 3.0, "shape_sampler": a.sampler, "shape_guidance_interval_start": 0.6,
                    "shape_guidance_interval_end": 1.0, "verbose": False, "dino_lock": 0.65, "dino_substeps": 2,
                    "dino_foundation_cap": 0.92}
    if mv:
        for i, v in enumerate(("front", "left", "back", "right")):
            g[f"3{i}"] = {"class_type": "Trellis2LoadImageWithTransparency", "inputs": {"image": names[v]}}
        g["34"] = {"class_type": "ImageBatch", "inputs": {"image1": ["30", 2], "image2": ["31", 2]}}
        g["35"] = {"class_type": "ImageBatch", "inputs": {"image1": ["34", 0], "image2": ["32", 2]}}
        g["36"] = {"class_type": "ImageBatch", "inputs": {"image1": ["35", 0], "image2": ["33", 2]}}
        cfg_inputs = {"images": ["36", 0], "azimuths": "0,90,180,270", "elevations": "0,0,0,0",
                      "fov": a.fov_deg, "fov_unit": "deg", "mesh_scale": 1.0, "distance": 0.0,
                      "remove_background": False, "invert_mask": False, "framing": "auto"}
        if a.fov_rad is not None:
            g["37"] = {"class_type": "Trellis2FovMoGeCameraConfig", "inputs": {
                "fov": a.fov_rad, "unit": "rad", "extend_pixel": 0, "mesh_scale": 1.0}}
            cfg_inputs["moge_camera_config"] = ["37", 0]
        g["38"] = {"class_type": "Trellis2Pixal3DMultiViewConfig", "inputs": cfg_inputs}
        sparse_in["moge_camera_config"] = ["38", 1]
        sparse_in["pixal3d_mv_views"] = ["38", 0]
        sparse_in["image"] = ["3", 0]
        shape_extra = {"image": ["3", 0], "moge_camera_config": ["38", 1], "pixal3d_mv_views": ["38", 0]}
    else:
        sparse_in["image"] = ["3", 0]
        sparse_in["moge_camera_config"] = ["4", 3]
        shape_extra = {"image": ["3", 0], "moge_camera_config": ["4", 3]}
    g["5"] = {"class_type": "Trellis2SparseGenerator", "inputs": sparse_in}
    g["6"] = {"class_type": "Trellis2ShapeGenerator", "inputs": dict(
        shape_common, pipeline=["5", 2], image_cond=["4", 0], coords=["5", 0], resolution=512, **shape_extra)}
    g["7"] = {"class_type": "Trellis2ShapeCascadeGenerator", "inputs": dict(
        shape_common, pipeline=["6", 2], image_cond=["4", 1], shape_slat=["6", 0], from_resolution=["6", 1],
        to_resolution=1024, sparse_structure_resolution=["5", 1], max_num_tokens=999999, **shape_extra)}
    g.update(decode_chain({"pipeline": ["7", 2], "shape_slat": ["7", 0], "resolution": ["7", 1]},
                          "astraforge/" + a.tag, a.target_faces, not a.no_meshlib))
    return g


def generate(a):
    views = {"front": a.front, "left": a.left, "back": a.back, "right": a.right}
    need = ("front", "left", "back", "right") if a.mode == "mv" else ("front",)
    for v in need:
        if not views[v] or not Path(views[v]).is_file():
            sys.exit(f"--{v} <png> is required for mode '{a.mode}'")
    names = {}
    COMFY_IN.mkdir(parents=True, exist_ok=True)
    for v in need:
        dst = f"af_smoke_{v}.png"
        shutil.copyfile(views[v], COMFY_IN / dst)
        names[v] = dst
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    g = build(a.mode, names, a)
    (out / (a.tag + ".api.json")).write_text(json.dumps(g, indent=1))
    if a.dry:
        print("graph written:", out / (a.tag + ".api.json"))
        return 0
    d = COMFY_OUT / "astraforge"
    before = set(p.name for p in d.iterdir()) if d.is_dir() else set()
    try:
        http("POST", "/af/vram/start")
        probe = True
    except Exception:
        probe = False
    t0 = time.time()
    pid = http("POST", "/prompt", {"prompt": g, "client_id": str(uuid.uuid4())})["prompt_id"]
    print("queued", pid)
    while True:
        h = http("GET", f"/history/{pid}")
        if pid in h and h[pid].get("status", {}).get("completed") is not None:
            hist = h[pid]
            break
        time.sleep(2)
    secs = time.time() - t0
    vram = http("POST", "/af/vram/stop") if probe else None
    st = hist["status"]
    print("status:", st.get("status_str"), " seconds: %.1f" % secs, " vram:", vram)
    rec = {"tag": a.tag, "mode": a.mode, "seconds": round(secs, 1), "vram": vram, "status": st.get("status_str"),
           "messages": st.get("messages", [])[-6:], "glb": None}
    if d.is_dir():
        for p in sorted(d.iterdir()):
            if p.name not in before and p.suffix.lower() == ".glb":
                rec["glb"] = str(out / (a.tag + ".glb"))
                shutil.copyfile(p, rec["glb"])
    (out / (a.tag + ".json")).write_text(json.dumps(rec, indent=1))
    print("glb:", rec["glb"])
    if st.get("status_str") != "success":
        print(json.dumps(st, indent=1)[:3000])
        return 1
    return 0


def main():
    global SRV
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8191, help="port of the running ComfyUI-Pixal3D (default 8191)")
    ap.add_argument("--generate", action="store_true", help="real shape run instead of the node check")
    ap.add_argument("--mode", choices=["single", "mv"], default="single")
    ap.add_argument("--front")
    ap.add_argument("--left")
    ap.add_argument("--back")
    ap.add_argument("--right")
    ap.add_argument("--out", default=str(INSTALL / "smoke"))
    ap.add_argument("--tag")
    ap.add_argument("--seed", type=int, default=1000)
    ap.add_argument("--sampler", default="heun")
    ap.add_argument("--fov-rad", type=float, default=None, help="mv: fixed FOV in radians (the author's example uses 0.1)")
    ap.add_argument("--fov-deg", type=float, default=20.0)
    ap.add_argument("--target-faces", type=int, default=700000)
    ap.add_argument("--no-meshlib", action="store_true",
                    help="skip the meshlib hole-filling nodes (the meshlib package is non-commercial / educational only)")
    ap.add_argument("--dry", action="store_true", help="with --generate: only write the API graph json")
    a = ap.parse_args()
    SRV = f"http://127.0.0.1:{a.port}"
    a.tag = a.tag or f"smoke_{a.mode}"
    if a.generate:
        return generate(a)
    return check_nodes()


if __name__ == "__main__":
    sys.exit(main())
