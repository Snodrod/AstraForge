"""Part generation for "generate by parts" (docs/forge/parts.md).

For every part folder written by parts_prep.py calls the multi-view Hunyuan3D-2mv graph of the studio's
ComfyUI (127.0.0.1:8190) exactly like units_1013/gen_mv.py: ImageOnlyCheckpointLoader ->
CLIPVisionEncode(front / back) -> Hunyuan3Dv2ConditioningMultiView -> EmptyLatentHunyuan3Dv2(3072) ->
KSampler(45 steps, cfg 5) -> VAEDecodeHunyuan3D(octree 512) -> AstraMarchingCubes -> SaveGLB, and saves
<out>/<part>/mesh_<seed>.glb. parts.json gets the mesh paths (parts[name].meshes / .mesh).

GPU etiquette: one prompt at a time; before every prompt the queue of ComfyUI is polled and the script
waits while anything runs or is pending (another job may own the card). Never /free or /interrupt.

    <ComfyUI>/.venv/Scripts/python.exe parts_generate.py --parts-dir parts/ [--seeds 703] [--seeds head=703,11]
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

CKPT = "hunyuan3d-dit-v2-mv.safetensors"
DEFAULT_URL = "http://127.0.0.1:8190"


def http_json(url, data=None, timeout=60):
    req = urllib.request.Request(url, None if data is None else json.dumps(data).encode(),
                                 {"Content-Type": "application/json"} if data is not None else {})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def upload(url, path, name):
    boundary = uuid.uuid4().hex
    body = (("--%s\r\nContent-Disposition: form-data; name=\"image\"; filename=\"%s\"\r\n"
             "Content-Type: image/png\r\n\r\n") % (boundary, name)).encode() + Path(path).read_bytes() + \
        ("\r\n--%s\r\nContent-Disposition: form-data; name=\"overwrite\"\r\n\r\ntrue\r\n--%s--\r\n" % (boundary, boundary)).encode()
    req = urllib.request.Request(url + "/upload/image", body, {"Content-Type": "multipart/form-data; boundary=" + boundary})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["name"]


def workflow(front, back, seed, prefix, steps=45, cfg=5.0, octree=512, resolution=3072):
    """The graph of gen_mv.py / hunyuanWorkflow in server/schema.mjs with a two-view condition."""
    return {
        "1": {"class_type": "ImageOnlyCheckpointLoader", "inputs": {"ckpt_name": CKPT}},
        "2": {"class_type": "LoadImage", "inputs": {"image": front}},
        "3": {"class_type": "CLIPVisionEncode", "inputs": {"clip_vision": ["1", 1], "image": ["2", 0], "crop": "none"}},
        "12": {"class_type": "LoadImage", "inputs": {"image": back}},
        "13": {"class_type": "CLIPVisionEncode", "inputs": {"clip_vision": ["1", 1], "image": ["12", 0], "crop": "none"}},
        "4": {"class_type": "Hunyuan3Dv2ConditioningMultiView", "inputs": {"front": ["3", 0], "back": ["13", 0]}},
        "5": {"class_type": "EmptyLatentHunyuan3Dv2", "inputs": {"resolution": resolution, "batch_size": 1}},
        "6": {"class_type": "KSampler", "inputs": {"model": ["1", 0], "positive": ["4", 0], "negative": ["4", 1],
                                                    "latent_image": ["5", 0], "seed": seed, "steps": steps, "cfg": cfg,
                                                    "sampler_name": "euler", "scheduler": "simple", "denoise": 1.0}},
        "7": {"class_type": "VAEDecodeHunyuan3D", "inputs": {"samples": ["6", 0], "vae": ["1", 2], "num_chunks": 4000,
                                                              "octree_resolution": octree}},
        "8": {"class_type": "AstraMarchingCubes", "inputs": {"voxel": ["7", 0], "level": 0.5}},
        "9": {"class_type": "SaveGLB", "inputs": {"mesh": ["8", 0], "filename_prefix": prefix}},
    }


def wait_idle(url, poll=15, timeout=3 * 3600, log=print):
    """Block while ComfyUI runs or has queued prompts (GPU etiquette: the card may belong to another job)."""
    t0 = time.time()
    announced = False
    while True:
        try:
            q = http_json(url + "/queue", timeout=15)
            if not q.get("queue_running") and not q.get("queue_pending"):
                return
        except (urllib.error.URLError, OSError) as exc:
            log("queue unreachable (%s), retrying" % exc)
        if not announced:
            log("ComfyUI is busy, waiting for an empty queue ...")
            announced = True
        if time.time() - t0 > timeout:
            raise TimeoutError("ComfyUI stayed busy for %d s" % timeout)
        time.sleep(poll)


def run_prompt(url, graph, timeout=3600, poll=4):
    pid = http_json(url + "/prompt", {"prompt": graph})["prompt_id"]
    t0 = time.time()
    while True:
        time.sleep(poll)
        h = http_json(url + "/history/" + pid)
        if pid in h:
            break
        if time.time() - t0 > timeout:
            raise TimeoutError("prompt %s did not finish in %d s" % (pid, timeout))
    st = h[pid].get("status", {})
    if st.get("status_str") != "success":
        raise RuntimeError("ComfyUI error: " + json.dumps(st)[:1500])
    files = []
    for out in h[pid]["outputs"].values():
        for v in out.values():
            if isinstance(v, list):
                files += [x for x in v if isinstance(x, dict) and "filename" in x]
    if not files:
        raise RuntimeError("ComfyUI returned no file")
    f = files[-1]
    q = urllib.parse.urlencode({"filename": f["filename"], "subfolder": f.get("subfolder", ""), "type": f.get("type", "output")})
    with urllib.request.urlopen(url + "/view?" + q, timeout=600) as r:
        return r.read(), time.time() - t0


def parse_seeds(items, parts):
    """--seeds 703 11 / --seeds head=703,11 torso=5  ->  {part: [seed, ...]} (plain numbers apply to every part)."""
    default, per = [], {}
    for it in items or []:
        if "=" in it:
            name, vals = it.split("=", 1)
            per[name] = [int(v) for v in vals.split(",") if v]
        else:
            default += [int(v) for v in it.split(",") if v]
    default = default or [703]
    return {p: per.get(p, default) for p in parts}


def _choose(part, seeds):
    """parts[name].mesh = the mesh of the first requested seed (else the first one there is)."""
    if not part["meshes"] or part.get("mesh"):
        return
    first = part["meshes"].get(str(seeds[0])) or next(iter(part["meshes"].values()))
    part["mesh"] = first["file"]


def generate(parts_dir, seeds=None, only=None, url=DEFAULT_URL, force=False, log=print, tag=None):
    parts_dir = Path(parts_dir)
    doc_path = parts_dir / "parts.json"
    doc = json.loads(doc_path.read_text(encoding="utf-8"))
    names = [n for n in doc["parts"] if not only or n in only]
    plan = parse_seeds(seeds, names)
    tag = tag or parts_dir.resolve().parent.name
    report = []
    for name in names:
        part = doc["parts"][name]
        folder = parts_dir / part["dir"]
        names_up = {}
        for seed in plan[name]:
            dst = folder / ("mesh_%d.glb" % seed)
            if dst.exists() and dst.stat().st_size > 1000 and not force:
                log("%s seed %d: exists, skipped" % (name, seed))
                part["meshes"][str(seed)] = {"file": "%s/%s" % (part["dir"], dst.name),
                                             "seconds": part["meshes"].get(str(seed), {}).get("seconds")}
                continue
            wait_idle(url, log=log)
            if not names_up:
                names_up["front"] = upload(url, folder / "mv_front.png", "parts_%s_%s_front.png" % (tag, name))
                names_up["back"] = upload(url, folder / "mv_back.png", "parts_%s_%s_back.png" % (tag, name))
            prefix = "astra_mv/parts_%s_%s_%d" % (tag, name, seed)
            data, sec = run_prompt(url, workflow(names_up["front"], names_up["back"], seed, prefix))
            dst.write_bytes(data)
            part["meshes"][str(seed)] = {"file": "%s/%s" % (part["dir"], dst.name), "seconds": round(sec, 1)}
            log("%s seed %d: %.0f s, %d KB -> %s" % (name, seed, sec, len(data) // 1024, dst))
            report.append({"part": name, "seed": seed, "seconds": round(sec, 1), "bytes": len(data)})
            _choose(part, plan[name])
            doc_path.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")   # keep progress
        _choose(part, plan[name])
    doc_path.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--parts-dir", required=True, help="folder with parts.json (parts_prep.py output)")
    ap.add_argument("--seeds", nargs="*", help="seeds: 703 11 (all parts) and / or head=703,11 (one part); default 703")
    ap.add_argument("--only", help="only these parts, comma separated")
    ap.add_argument("--url", default=DEFAULT_URL)
    ap.add_argument("--force", action="store_true", help="regenerate existing meshes")
    ap.add_argument("--tag", help="name used in the ComfyUI output prefix (default: the parent folder name)")
    a = ap.parse_args(argv)
    t0 = time.time()
    rep = generate(a.parts_dir, a.seeds, set(a.only.split(",")) if a.only else None, a.url, a.force, tag=a.tag,
                   log=lambda s: print(s, flush=True))
    print("generated %d meshes in %.0f s" % (len(rep), time.time() - t0))


if __name__ == "__main__":
    sys.exit(main())
