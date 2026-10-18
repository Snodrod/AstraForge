"""
Normalise a generated character mesh like the other AstraForge seed meshes (Hunyuan-style, mesh_mv_703.glb):

  * glTF convention: +Y up, the character looks toward +Z (= -Y after Blender's glTF import), its left side is +X
  * bounding box centred on the origin, HEIGHT (Y extent) = 2.0
  * geometry only, no texture

The orientation is found automatically: all 24 proper axis-aligned rotations are tried and the one whose four
orthographic silhouettes (front / left / back / right, the same camera convention as AstraForge compare_render.py)
best match the cut-outs <views-dir>/cut_<view>.png (RGBA, alpha = silhouette) wins. The score is printed for every candidate so the
choice can be audited; the profile views (left/right) are what separates front from back.

    python_embeded\\python.exe -I tools\\normalize_seed_mesh.py <in.glb> <out.glb> --views-dir <dir with cut_front/left/back/right.png> [--check <reference.glb>]
"""
import argparse
import itertools
import json
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image

NORM = None   # folder with cut_<view>.png, set from --views-dir in main()
SIZE = 384


def rotations24():
    mats = []
    for perm in itertools.permutations(range(3)):
        for signs in itertools.product((1, -1), repeat=3):
            m = np.zeros((3, 3))
            for i, (p, s) in enumerate(zip(perm, signs)):
                m[i, p] = s
            if np.linalg.det(m) > 0:
                mats.append(m)
    return mats


def load_points(path, n=400000):
    sc = trimesh.load(path, force="scene")
    if hasattr(sc, "to_geometry"):
        mesh = sc.to_geometry()
    else:
        mesh = trimesh.util.concatenate([g for g in sc.dump()]) if len(sc.geometry) > 1 else sc.dump(concatenate=True)
    pts, _ = trimesh.sample.sample_surface(mesh, n, seed=0)
    return mesh, np.asarray(pts, dtype=np.float64)


def ref_mask(view):
    a = np.asarray(Image.open(NORM / f"cut_{view}.png").convert("RGBA").getchannel("A")) > 40
    return bbox_fit(a)


def bbox_fit(m, size=SIZE):
    ys, xs = np.where(m)
    m = m[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    im = Image.fromarray((m * 255).astype(np.uint8))
    k = size / max(im.size)
    im = im.resize((max(1, round(im.width * k)), max(1, round(im.height * k))), Image.Resampling.BILINEAR)
    c = Image.new("L", (size, size), 0)
    c.paste(im, ((size - im.width) // 2, (size - im.height) // 2))
    return np.asarray(c) > 127


def splat(u, v, res=1024):
    """Occupancy image from projected points (u right, v up), then closed so surface samples form a solid silhouette."""
    from scipy import ndimage
    u0, u1, v0, v1 = u.min(), u.max(), v.min(), v.max()
    k = (res - 1) / max(u1 - u0, v1 - v0)
    x = np.clip(((u - u0) * k).astype(int), 0, res - 1)
    y = np.clip(((v1 - v) * k).astype(int), 0, res - 1)
    img = np.zeros((res, res), bool)
    img[y, x] = True
    img = ndimage.binary_closing(img, iterations=3)
    img = ndimage.binary_fill_holes(img)
    return bbox_fit(img)


# camera conventions (glTF world): (right axis, up axis) as vectors, see module docstring
VIEW_AXES = {
    "front": (np.array([1, 0, 0.0]), np.array([0, 1, 0.0])),     # camera at +Z looking -Z
    "left":  (np.array([0, 0, -1.0]), np.array([0, 1, 0.0])),    # camera at +X looking -X (face to the image left)
    "back":  (np.array([-1, 0, 0.0]), np.array([0, 1, 0.0])),    # camera at -Z looking +Z
    "right": (np.array([0, 0, 1.0]), np.array([0, 1, 0.0])),     # camera at -X looking +X
}


def iou(a, b):
    u = (a | b).sum()
    return float((a & b).sum() / u) if u else 0.0


def best_rotation(pts):
    refs = {v: ref_mask(v) for v in VIEW_AXES}
    # subsample for the search; the final transform is applied to the full mesh
    sub = pts[:: max(1, len(pts) // 120000)]
    results = []
    for i, R in enumerate(rotations24()):
        p = sub @ R.T
        s = {}
        for v, (r, up) in VIEW_AXES.items():
            s[v] = iou(splat(p @ r, p @ up), refs[v])
        results.append((float(np.mean(list(s.values()))), i, R, s))
    results.sort(key=lambda t: -t[0])
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--views-dir", required=True,
                    help="folder with cut_front.png, cut_left.png, cut_back.png, cut_right.png (RGBA cut-outs)")
    ap.add_argument("--check", help="reference seed mesh whose bounds/orientation are reported for comparison")
    a = ap.parse_args()
    global NORM
    NORM = Path(a.views_dir)
    mesh, pts = load_points(a.src)
    res = best_rotation(pts)
    for score, i, R, s in res[:4]:
        print(f"candidate {i:2d} mean IoU {score:.3f}  " + " ".join(f"{k}={v:.3f}" for k, v in s.items()))
    score, i, R, s = res[0]
    verts = np.asarray(mesh.vertices) @ R.T
    lo, hi = verts.min(0), verts.max(0)
    height = hi[1] - lo[1]
    verts = (verts - (lo + hi) / 2) * (2.0 / height)
    faces = np.asarray(mesh.faces)
    if np.linalg.det(R) < 0:
        faces = faces[:, ::-1]
    out = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
    out.export(a.dst)
    b = out.bounds
    rep = {"rotation_index": i, "R": R.tolist(), "iou": s, "faces": int(len(faces)),
           "bounds_min": b[0].round(4).tolist(), "bounds_max": b[1].round(4).tolist(),
           "extent": (b[1] - b[0]).round(4).tolist()}
    if a.check:
        ref = trimesh.load(a.check, force="scene")
        rb = ref.bounds
        rep["reference_extent"] = (rb[1] - rb[0]).round(4).tolist()
        rep["reference_centre"] = ((rb[0] + rb[1]) / 2).round(4).tolist()
    print(json.dumps(rep))


if __name__ == "__main__":
    main()
