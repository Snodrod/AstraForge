"""Helpers of the assemble_parts step (docs/forge/parts.md): fit the whole-body base to the front
reference, map part boxes (reference pixels) to world boxes, register generated part meshes (silhouette
fit in X / Z, depth from the base, optional trimmed similarity ICP), cut the joints, fill missing parts
from the base.

Blender 5.0.1, bundled Python 3.11 + numpy only. Frame: metres, character faces -Y, feet on z = 0,
character left = +X, Z up. The front reference camera is orthographic, looks along +Y (yaw = pitch = 0):
image u = s (X - cx) + tx, v = -s (Z - cz) + ty with pixel (i, j) covering [j, j+1) x [i, i+1).
"""
import json
import math
import os

import bmesh
import bpy
import mathutils
import numpy as np

import af_common as af
from steps import _mesh_lib as ml
from steps import _paint_lib as pl
from steps import _replace_lib as rl

PART_NAMES = ("head", "torso", "arm_l", "arm_r", "legs")


# --------------------------------------------------------------------------- spec
def read_parts(inputs, params, warnings):
    """Parts list [{name, mesh, box, subject, seed}] from inputs.parts (a parts.json written by
    scripts/parts/parts_prep.py + parts_generate.py) and / or params.parts (same entries). A part
    without a mesh file is skipped with a warning - the base fills its place."""
    out = []
    base_dir = None
    items = []
    path = (inputs or {}).get("parts")
    if path:
        path = os.path.abspath(str(path))
        if not os.path.isfile(path):
            raise FileNotFoundError("inputs.parts not found: %s" % path)
        base_dir = os.path.dirname(path)
        with open(path, "r", encoding="utf-8-sig") as fh:
            doc = json.load(fh)
        pick = (params.get("pick") or {})
        for name, p in (doc.get("parts") or {}).items():
            mesh = p.get("mesh")
            if not mesh and p.get("meshes"):
                first = next(iter(p["meshes"].values()))
                mesh = first.get("file") if isinstance(first, dict) else first
            sel = pick.get(name)
            if sel is not None:
                m = (p.get("meshes") or {}).get(str(sel))
                mesh = m["file"] if isinstance(m, dict) else (str(sel) if os.path.isfile(str(sel)) else mesh)
                if m is None and not os.path.isfile(str(sel)):
                    warnings.append("pick %s=%s: no such mesh in parts.json, using %s" % (name, sel, mesh))
            items.append({"name": name, "mesh": mesh, "box": p.get("front_box"), "subject": p.get("subject_front")})
    for p in (params.get("parts") or []):
        items.append({"name": p.get("name"), "mesh": p.get("mesh"), "box": p.get("box"), "subject": p.get("subject")})
    seen = set()
    for it in items:
        name = it["name"]
        if not name:
            raise ValueError("a part without a name")
        if name in seen:
            warnings.append("part %s is listed twice: the last entry wins" % name)
            out = [o for o in out if o["name"] != name]
        seen.add(name)
        if not it["box"] or len(it["box"]) != 4:
            raise ValueError("part %s: box [x0, y0, x1, y1] (front reference px) is required" % name)
        mesh = it["mesh"]
        if mesh and not os.path.isabs(str(mesh)) and base_dir:
            mesh = os.path.join(base_dir, str(mesh))
        if not mesh or not os.path.isfile(str(mesh)):
            warnings.append("part %s has no mesh (%s): the base mesh fills its box" % (name, mesh))
            mesh = None
        out.append({"name": name, "mesh": os.path.abspath(str(mesh)) if mesh else None,
                    "box": [int(v) for v in it["box"]], "subject": it.get("subject")})
    return out


# --------------------------------------------------------------------------- mesh arrays
def world_arrays(ob):
    """(vertices (n, 3), triangles (m, 3)) of a mesh object in world space."""
    return rl.tri_arrays(ob)


def face_centroids(V, T):
    return (V[T[:, 0]] + V[T[:, 1]] + V[T[:, 2]]) / 3.0


def face_samples(V, T, n, seed=0, face_mask=None):
    """Area-weighted random points on the triangles (optionally only on masked faces)."""
    if face_mask is not None:
        T = T[face_mask]
    if not len(T):
        return np.zeros((0, 3))
    a, b, c = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
    area = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
    tot = float(area.sum())
    if tot <= 0:
        return np.zeros((0, 3))
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(T), size=int(n), p=area / tot)
    r1 = np.sqrt(rng.random(int(n)))
    r2 = rng.random(int(n))
    w0, w1, w2 = 1 - r1, r1 * (1 - r2), r1 * r2
    return a[idx] * w0[:, None] + b[idx] * w1[:, None] + c[idx] * w2[:, None]


# --------------------------------------------------------------------------- camera
def silhouette(V, T, cam, shape, factor=2):
    """Exact triangle raster of the mesh through the front camera, bool mask at 1 / factor of `shape`."""
    h, w = shape
    f = max(1, int(factor))
    hh, ww = max(1, h // f), max(1, w // f)
    u, v, z = cam.project(V)
    zb = pl.zbuffer(u / f, v / f, z, T, ww, hh)
    return np.isfinite(zb)


def mask_at(mask, shape, factor=2):
    h, w = shape
    f = max(1, int(factor))
    hh = max(1, h // f)
    m, _ = pl.resize_mask(mask, hh)
    return m


def iou_front(V, T, cam, mask, factor=2):
    """IoU of the mesh silhouette through `cam` with the reference mask (both at 1 / factor of its size)."""
    sil = silhouette(V, T, cam, mask.shape, factor)
    ref = mask_at(mask, mask.shape, factor)
    hh, ww = min(sil.shape[0], ref.shape[0]), min(sil.shape[1], ref.shape[1])
    return pl.mask_iou(sil[:hh, :ww], ref[:hh, :ww])


def fit_front_camera(V, T, mask, work_h=(200, 400, 800), seed=1):
    """Orthographic front camera (yaw = pitch = 0) that best overlays the mesh silhouette on `mask`:
    scale + offset by Nelder-Mead on the IoU of area-sampled surface points splatted at 200 / 400 /
    800 px (the fit of paint_from_concept with the rotation locked: a Hunyuan mesh generated from the
    reference is front aligned). Returns (OrthoCam at the mask resolution, IoU at full resolution / 2)."""
    H0, W0 = mask.shape
    pts = pl.area_samples(V, T, min(200000, max(60000, 3 * len(T))), seed=seed)
    centre = (pts.min(axis=0) + pts.max(axis=0)) / 2.0
    ys, xs = np.nonzero(mask)
    s0 = (ys.max() - ys.min() + 1) / max(float(pts[:, 2].max() - pts[:, 2].min()), 1e-9)
    q = np.array([math.log(s0), (xs.min() + xs.max() + 1) / 2.0, (ys.min() + ys.max() + 1) / 2.0])
    prev = (1.0, 1.0)
    for wh in work_h:
        m, (fx, fy) = pl.resize_mask(mask, min(int(wh), H0))
        hw, ww = m.shape
        q[0] += math.log(fx / prev[0])
        q[1] *= fx / prev[0]
        q[2] *= fy / prev[1]
        prev = (fx, fy)

        def obj(p, m=m, ww=ww, hw=hw):
            cam = pl.OrthoCam(0.0, 0.0, math.exp(p[0]), p[1], p[2], (ww, hw), 0.0, centre)
            u, v, _ = cam.project(pts)
            return 1.0 - pl.mask_iou(pl.splat_mask(u, v, ww, hw), m)
        q, _f, _ev = pl.nelder_mead(obj, q, np.array([0.01, 2.0, 2.0]), iters=160)
    cam = pl.OrthoCam(0.0, 0.0, math.exp(q[0]) / prev[0], q[1] / prev[0], q[2] / prev[1], (W0, H0), 0.0, centre)
    return cam, iou_front(V, T, cam, mask)


def px_box_to_world(cam, box):
    """Inclusive pixel box [x0, y0, x1, y1] -> world (x0, x1, z0, z1) through the front camera."""
    cx, cz = float(cam.centre[0]), float(cam.centre[2])
    xa = (box[0] - cam.tx) / cam.s + cx
    xb = (box[2] + 1 - cam.tx) / cam.s + cx
    zb = cz - (box[1] - cam.ty) / cam.s
    za = cz - (box[3] + 1 - cam.ty) / cam.s
    return xa, xb, za, zb


def inside_xz(P, wb, expand=0.0):
    return ((P[:, 0] >= wb[0] - expand) & (P[:, 0] <= wb[1] + expand) &
            (P[:, 2] >= wb[2] - expand) & (P[:, 2] <= wb[3] + expand))


def subject_world_box(cam, mask, box):
    """World (x0, x1, z0, z1) of the tight bbox of the reference silhouette inside the pixel box."""
    x0, y0, x1, y1 = box
    sub = mask[max(y0, 0):y1 + 1, max(x0, 0):x1 + 1]
    ys, xs = np.nonzero(sub)
    if not len(xs):
        return px_box_to_world(cam, box)
    b = [max(x0, 0) + int(xs.min()), max(y0, 0) + int(ys.min()), max(x0, 0) + int(xs.max()), max(y0, 0) + int(ys.max())]
    return px_box_to_world(cam, b)


def slice_y(V, cam, mask, box):
    """Depth (Y) values of the base vertices that project into the subject silhouette inside the box."""
    u, v, _ = cam.project(V)
    j = np.floor(u).astype(np.int64)
    i = np.floor(v).astype(np.int64)
    h, w = mask.shape
    ok = (j >= max(box[0], 0)) & (j <= min(box[2], w - 1)) & (i >= max(box[1], 0)) & (i <= min(box[3], h - 1))
    ok &= mask[np.clip(i, 0, h - 1), np.clip(j, 0, w - 1)]
    return V[ok, 1]


# --------------------------------------------------------------------------- registration
# A generated part is registered in three steps (measured on the militia, docs/forge/parts.md): the
# front reference is the truth for X / Z (the part was generated from exactly that crop; the whole-body
# base is the less accurate one there - its helmet is smaller and blobbier), so scale + shift in X / Z
# come from the silhouette; the depth (Y), which no image pins, comes from the base (front / back depth
# maps); an ICP against the base surface (rigid + uniform scale) is optional and measures the residual.
def crop_mask(mask, box):
    x0, y0, x1, y1 = box
    h, w = mask.shape
    out = np.zeros((y1 - y0 + 1, x1 - x0 + 1), bool)
    sx0, sy0, sx1, sy1 = max(x0, 0), max(y0, 0), min(x1, w - 1), min(y1, h - 1)
    if sx1 >= sx0 and sy1 >= sy0:
        out[sy0 - y0:sy1 - y0 + 1, sx0 - x0:sx1 - x0 + 1] = mask[sy0:sy1 + 1, sx0:sx1 + 1]
    return out


def bbox_init(Vp, subj, y_mid):
    """Initial placement of a generated part (its own frame, bbox centred, long side 2): scale per axis
    from the silhouette extents (X, Z; the generator's aspect is only roughly right: measured up to 15 %
    off on a torso with sleeve stubs), Y scale = geometric mean of both, centred on the subject box in
    X / Z and on the base slice in Y. Returns (scale xyz, translation xyz, info)."""
    lo, hi = Vp.min(axis=0), Vp.max(axis=0)
    ex, ez = float(hi[0] - lo[0]), float(hi[2] - lo[2])
    sx = (subj[1] - subj[0]) / max(ex, 1e-9)
    sz = (subj[3] - subj[2]) / max(ez, 1e-9)
    sy = math.sqrt(sx * sz)
    c = (lo + hi) / 2.0
    py = float(np.percentile(Vp[:, 1], [2, 98]).mean())
    t = np.array([(subj[0] + subj[1]) / 2.0 - sx * c[0], y_mid - sy * py, (subj[2] + subj[3]) / 2.0 - sz * c[2]])
    return np.array([sx, sy, sz]), t, {"scale_x": round(sx, 5), "scale_z": round(sz, 5), "aspect": round(sx / sz, 4)}


def fit_silhouette(S, scale, t, cam, mask, box, stages=(4, 2)):
    """Refine (sx, sz, tx, tz) of the part samples S (part frame) by the IoU of their front projection
    with the reference silhouette inside the pixel box. Returns (sx, sz, tx, tz, iou)."""
    crop = crop_mask(mask, box)
    q = np.array([scale[0], scale[2], t[0], t[2]], float)
    iou = 0.0
    for f in stages:
        m, (fx, fy) = pl.resize_mask(crop, max(8, int(round(crop.shape[0] / float(f)))))
        h, w = m.shape

        def obj(p, m=m, fx=fx, fy=fy, w=w, h=h):
            P = np.stack([p[0] * S[:, 0] + p[2], np.zeros(len(S)), p[1] * S[:, 2] + p[3]], axis=1)
            u, v, _ = cam.project(P)
            return 1.0 - pl.mask_iou(pl.splat_mask((u - box[0]) * fx, (v - box[1]) * fy, w, h), m)
        q, fq, _ev = pl.nelder_mead(obj, q, np.array([0.01 * q[0], 0.01 * q[1], 0.003, 0.003]), iters=160)
        iou = 1.0 - fq
    return float(q[0]), float(q[1]), float(q[2]), float(q[3]), float(iou)


def depth_maps(P, cam, box, f=2):
    """Per pixel (box grid, f px cells) nearest and farthest depth (world Y) of the points seen from the front."""
    u, v, z = cam.project(P)
    w, h = (box[2] - box[0] + 1) // f + 1, (box[3] - box[1] + 1) // f + 1
    j = np.floor((u - box[0]) / f).astype(np.int64)
    i = np.floor((v - box[1]) / f).astype(np.int64)
    ok = (j >= 0) & (j < w) & (i >= 0) & (i < h)
    flat = i[ok] * w + j[ok]
    zmin = np.full(w * h, np.inf)
    zmax = np.full(w * h, -np.inf)
    np.minimum.at(zmin, flat, z[ok].astype(np.float64))
    np.maximum.at(zmax, flat, z[ok].astype(np.float64))
    return zmin, zmax


def fit_depth(P_part, P_base, cam, box, f=2):
    """Y shift that lays the part's front and back surface depth on the base's (median over the pixels
    both cover) and the thickness ratio base / part. Returns (dy, thickness_ratio, pixels)."""
    pmin, pmax = depth_maps(P_part, cam, box, f)
    bmin, bmax = depth_maps(P_base, cam, box, f)
    both = np.isfinite(pmin) & np.isfinite(bmin) & np.isfinite(pmax) & np.isfinite(bmax)
    if both.sum() < 20:
        return 0.0, 1.0, int(both.sum())
    d = np.concatenate([bmin[both] - pmin[both], bmax[both] - pmax[both]])
    th_p = np.maximum(pmax[both] - pmin[both], 1e-6)
    return float(np.median(d)), float(np.median((bmax[both] - bmin[both]) / th_p)), int(both.sum())


def residual(src, tgt, trim=0.8):
    """Symmetric nearest-sample distances part <-> base: (trimmed RMS, median, share of part samples
    within 5 mm of the base)."""
    kd_t = rl.kdtree(tgt)
    _i, d_f = rl.nearest(kd_t, src)
    _i, d_b = rl.nearest(rl.kdtree(src), tgt)
    d = np.concatenate([d_f, d_b])
    keep = np.sort(d)[: max(1, int(len(d) * trim))]
    return float(np.sqrt(np.mean(keep ** 2))), float(np.median(d)), float((d_f < 0.005).mean())


def matrix_of(scale, t):
    M = np.eye(4)
    M[0, 0], M[1, 1], M[2, 2] = scale
    M[:3, 3] = t
    return M


def sim_to_matrix(sim):
    s, R, t = sim
    M = np.eye(4)
    M[:3, :3] = s * R
    M[:3, 3] = t
    return M


def sil_iou(S, M, cam, mask, box, f=2):
    """IoU of the front projection of the points S (part frame) under the 4 x 4 matrix M with the
    reference silhouette inside the pixel box (both at 1 / f)."""
    crop = crop_mask(mask, box)
    m, (fx, fy) = pl.resize_mask(crop, max(8, int(round(crop.shape[0] / float(f)))))
    h, w = m.shape
    P = S @ M[:3, :3].T + M[:3, 3]
    u, v, _ = cam.project(P)
    return pl.mask_iou(pl.splat_mask((u - box[0]) * fx, (v - box[1]) * fy, w, h), m)


def _fit_once(Vp, S, base_pts, cam, mask, box, subj, y_mid, sign, depth_scale="base"):
    """bbox init -> silhouette fit (X / Z) -> depth fit (Y) for one orientation of the part
    (sign = (+-1, +-1, 1): a part generated turned 180 deg about Z has (-1, -1, 1))."""
    sg = np.array(sign, float)
    scale0, t0, ii = bbox_init(Vp * sg, subj, y_mid)
    Ss = S * sg
    iou0 = sil_iou(Ss, matrix_of(scale0, t0), cam, mask, box)
    sx, sz, tx, tz, iou1 = fit_silhouette(Ss, scale0, t0, cam, mask, box)
    sy = math.sqrt(sx * sz)                       # depth scale of the part's own proportions
    Pw = Ss * np.array([sx, sy, sz]) + np.array([tx, 0.0, tz])
    dy, thick, npx = fit_depth(Pw, base_pts, cam, box)
    thick0 = thick
    if depth_scale == "base":
        # no image pins the depth: take the base's front-to-back thickness (clamped), so that neighbouring
        # parts share one depth statistic and meet without ledges at the joints
        sy *= min(max(thick, 0.8), 1.25)
        Pw = Ss * np.array([sx, sy, sz]) + np.array([tx, 0.0, tz])
        dy, thick, npx = fit_depth(Pw, base_pts, cam, box)
    elif isinstance(depth_scale, (int, float)) and depth_scale > 0:
        sy *= float(depth_scale)
        Pw = Ss * np.array([sx, sy, sz]) + np.array([tx, 0.0, tz])
        dy, thick, npx = fit_depth(Pw, base_pts, cam, box)
    M = matrix_of([sx, sy, sz], [tx, dy, tz]) @ matrix_of(sg, [0, 0, 0])
    info = {"scale_init": [round(float(v), 6) for v in scale0], "aspect_init": ii["aspect"], "iou_init": round(iou0, 4),
            "iou_silhouette": round(iou1, 4), "depth_shift_m": round(dy, 5), "thickness_ratio": round(thick0, 4),
            "thickness_ratio_after": round(thick, 4), "depth_pixels": npx}
    return M, info


def register_part(Vp, Tp, Vb, Tb, c_base, cam, mask, box, wb, subj, y_mid, opt):
    """Place one generated part on the world frame. Returns (4 x 4 matrix for the part's vertices, info).
    opt: samples, expand (m), refine ('silhouette' | 'icp' | 'none'), iters, trim, max_rot_deg,
    scale_range, try_flip, flip_rms_m."""
    n = int(opt.get("samples", 15000))
    S = face_samples(Vp, Tp, max(250000, 2 * n), seed=5)
    base_pts = face_samples(Vb, Tb, 300000, seed=3, face_mask=inside_xz(c_base, wb, 0.0))
    tgt = face_samples(Vb, Tb, 2 * n, seed=3, face_mask=inside_xz(c_base, wb, opt.get("expand", 0.0)))
    if len(tgt) < 200 or len(base_pts) < 200:
        raise ValueError("the base has no geometry inside the box of this part")
    mode = opt.get("refine", "silhouette")
    dsc = opt.get("depth_scale", "base")
    M, info = _fit_once(Vp, S, base_pts, cam, mask, box, subj, y_mid, (1, 1, 1), dsc)
    rms, med, inl = residual((S[:n] @ M[:3, :3].T + M[:3, 3]), tgt)
    info["flipped"] = False
    if mode != "none" and opt.get("try_flip", True) and rms >= float(opt.get("flip_rms_m", 0.0)):
        M2, info2 = _fit_once(Vp, S, base_pts, cam, mask, box, subj, y_mid, (-1, -1, 1), dsc)
        rms2, med2, inl2 = residual((S[:n] @ M2[:3, :3].T + M2[:3, 3]), tgt)
        if rms2 < 0.6 * rms:
            M, info, rms, med, inl = M2, info2, rms2, med2, inl2
            info["flipped"] = True
    if mode == "icp":
        src_w = S[:n] @ M[:3, :3].T + M[:3, 3]
        sim, ic = icp_part(src_w, tgt, (1.0, np.eye(3), np.zeros(3)), iters=int(opt.get("iters", 40)),
                           trim=float(opt.get("trim", 0.8)), max_rot_deg=float(opt.get("max_rot_deg", 6.0)),
                           scale_range=tuple(opt.get("scale_range", (0.8, 1.25))))
        M = sim_to_matrix(sim) @ M
        rms, med, inl = ic["rms_m"], ic["median_m"], ic["inlier_5mm"]
        info["icp"] = {"iterations": ic["iterations"], "rotation_deg": ic["rotation_deg"], "scale": round(sim[0], 5)}
    info["iou_final"] = round(sil_iou(S, M, cam, mask, box), 4)
    info.update({"mode": mode, "residual_rms_m": rms, "residual_median_m": med, "inlier_5mm": inl,
                 "samples": [int(min(n, len(S))), int(len(tgt))]})
    return M, info


# --------------------------------------------------------------------------- base remainder
def cuboid(name, x0, x1, z0, z1, y0=-5.0, y1=5.0):
    """Axis-aligned box object (a cutter for the base: the region of a part, full depth)."""
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    ob.location = ((x0 + x1) / 2.0, (y0 + y1) / 2.0, (z0 + z1) / 2.0)
    ob.scale = (x1 - x0, y1 - y0, z1 - z0)
    bpy.context.view_layer.update()
    return ob


def is_closed(ob):
    """Every edge shared by exactly two faces (the Manifold Boolean solver needs closed manifold inputs;
    otherwise Blender only logs "non-manifold inputs" and leaves the mesh as it was)."""
    a = ml.mesh_arrays(ob.data)
    return bool((ml.edge_face_counts(a) == 2).all()) if len(a["ev"]) else False


def boolean(ob, other, operation="INTERSECT"):
    """Apply a Boolean modifier: the exact Manifold solver on closed inputs, the (slow) Exact solver on
    anything else or when Manifold fails. Returns the solver used."""
    solvers = ("MANIFOLD", "EXACT") if (is_closed(ob) and is_closed(other)) else ("EXACT",)
    last = None
    for solver in solvers:
        cp = ob.data.copy()
        mod = ob.modifiers.new("af_bool", "BOOLEAN")
        mod.operation = operation
        mod.operand_type = "OBJECT"
        mod.object = other
        mod.solver = solver
        try:
            ml._apply_modifier(ob, mod)
            bpy.data.meshes.remove(cp)
            return solver
        except RuntimeError as exc:                      # pragma: no cover - solver specific
            last = exc
            for m in list(ob.modifiers):
                ob.modifiers.remove(m)
            ob.data = cp
    raise RuntimeError("boolean %s failed: %s" % (operation, last))


def _section(P, k, c, delta, slab):
    """Robust extents (2nd / 98th percentile) of the points in a thin cross-section of a joint slab:
    returns an array [[lo, hi] per remaining axis] or None when the section is almost empty."""
    x0, x1, z0, z1 = slab
    sel = (P[:, 0] >= x0) & (P[:, 0] <= x1) & (P[:, 2] >= z0) & (P[:, 2] <= z1) & (np.abs(P[:, k] - c) <= delta)
    if int(sel.sum()) < 40:
        return None
    others = [1] + [a for a in (0, 2) if a != k]
    return np.percentile(P[sel][:, others], [2, 98], axis=0)


def pick_joint_line(PA, PB, k, lo, hi, slab, delta, n=13):
    """Position of the cut between two parts along axis k inside [lo, hi] where their cross-sections agree
    best (sum of the differences of the extents in the other two axes), in the inner 70 % of the slab;
    the middle when a section is empty. Returns (c, mismatch_m or None)."""
    mid = 0.5 * (lo + hi)
    best = (None, mid)
    span = hi - lo
    for t in np.linspace(0.15, 0.85, n):
        c = lo + t * span
        qa, qb = _section(PA, k, c, delta, slab), _section(PB, k, c, delta, slab)
        if qa is None or qb is None:
            continue
        d = float(np.abs(qa - qb).sum()) + 0.02 * abs(c - mid)
        if best[0] is None or d < best[0]:
            best = (d, c)
    return best[1], best[0]


def joint_cuts(boxes, eps, height, points=None, delta=0.008):
    """Where two part boxes overlap in a slab (a joint) the parts cross it by a margin; cut each part at the
    middle of the slab so that they overlap by 2 eps there and nowhere else (the full overlap would show
    every difference of the two surfaces: measured as a flat plate where the torso's sleeve stub sticks out
    of the arm). boxes = {name: (x0, x1, z0, z1)} in metres. A joint is a pair whose overlap has thickness
    t (the smaller side) between 5 % and 20 % of the height and the other side at least 1.2 t (corner
    touches such as a helmet bust against a sleeve are no joints). Returns ({name: [cut boxes]}, [joints]):
    the cut box of a part is the half of the slab beyond the joint line (+ eps), only inside the slab -
    the rest of the part is untouched (a tunic hem wider than the chest stays)."""
    cuts = {k: [] for k in boxes}
    joints = []
    names = sorted(boxes)
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            A, B = boxes[a], boxes[b]
            x0, x1 = max(A[0], B[0]), min(A[1], B[1])
            z0, z1 = max(A[2], B[2]), min(A[3], B[3])
            ox, oz = x1 - x0, z1 - z0
            if ox <= 0 or oz <= 0:
                continue
            t, L = min(ox, oz), max(ox, oz)
            if not (0.05 * height <= t <= 0.2 * height and L >= 1.2 * t):
                continue
            k = 0 if ox <= oz else 2             # slab thickness along X (0) or Z (2)
            lo, hi = (x0, x1) if k == 0 else (z0, z1)
            c, mism = 0.5 * (lo + hi), None
            if points and a in points and b in points:
                c, mism = pick_joint_line(points[a], points[b], k, lo, hi, (x0, x1, z0, z1), delta)
            first = 0.5 * (A[k] + A[k + 1]) < 0.5 * (B[k] + B[k + 1])    # A on the low side of the joint

            def slab(lo_k, hi_k):
                return (lo_k, hi_k, z0, z1) if k == 0 else (x0, x1, lo_k, hi_k)
            if first:
                cuts[a].append(slab(c + eps, hi))      # A gives up what lies beyond the line on B's side
                cuts[b].append(slab(lo, c - eps))
            else:
                cuts[a].append(slab(lo, c - eps))
                cuts[b].append(slab(c + eps, hi))
            joints.append({"parts": [a, b], "axis": "xz"[k // 2], "at_m": round(c, 5), "thickness_m": round(t, 4),
                           "slab_m": [round(v, 5) for v in (x0, x1, z0, z1)],
                           "section_mismatch_m": None if mism is None else round(mism, 5)})
    return cuts, joints


def trim_part(ob, box, cuts, min_island=0.002):
    """Clip a closed part mesh to its box, subtract the joint cuts (planar caps), drop floating debris.
    Returns (solver, islands dropped)."""
    solver = "none"
    if box is not None:
        cut = cuboid(ob.name + "_clip", *box)
        solver = boolean(ob, cut, "INTERSECT")
        bpy.data.objects.remove(cut, do_unlink=True)
    for k, c in enumerate(cuts):
        if c[1] - c[0] <= 0 or c[3] - c[2] <= 0:
            continue
        cut = cuboid("%s_cut%d" % (ob.name, k), *c)
        solver = boolean(ob, cut, "DIFFERENCE")
        bpy.data.objects.remove(cut, do_unlink=True)
    if not ml.tris_of(ob) and (box is not None or cuts):
        raise RuntimeError("nothing is left of %s after the cut" % ob.name)
    islands = 0
    if min_island > 0 and ml.tris_of(ob):
        islands = ml.remove_small_islands(ob, min_island)[0]
    return solver, islands


def base_piece(base, wb, cuts, name, min_tris):
    """The part of the base inside the world box wb = (x0, x1, z0, z1) (full depth, planar caps) minus the
    joint cuts, None when almost nothing is there. Returns (object | None, tris, solver)."""
    piece = ml.duplicate(base, name)
    solver, _isl = trim_part(piece, wb, cuts, 0.0)
    nt = ml.tris_of(piece)
    if nt:
        ml.remove_small_islands(piece, min(0.5, 0.5 * min_tris / max(len(piece.data.vertices), 1)))
        nt = ml.tris_of(piece)
    if nt < min_tris:
        ml.remove_object(piece)
        return None, 0, solver
    return piece, nt, solver


# --------------------------------------------------------------------------- ICP (refine "icp")
def _rot_z(deg):
    a = math.radians(deg)
    return np.array([[math.cos(a), -math.sin(a), 0.0], [math.sin(a), math.cos(a), 0.0], [0.0, 0.0, 1.0]])


def icp_part(src, tgt, init, iters=40, trim=0.8, max_rot_deg=6.0, scale_range=(0.8, 1.25), tol=1e-7):
    """Symmetric trimmed ICP (rigid + uniform scale) of the part samples `src` onto the base samples `tgt`
    (both world space, src already placed by the silhouette / depth fit). Every iteration pairs both ways
    (src -> tgt, tgt -> src), drops the worst (1 - trim) of each, solves the similarity in closed form
    (Umeyama). The rotation is limited to max_rot_deg (a part is generated upright and facing the camera
    like the base; a larger turn is dropped and only scale + shift are fitted), the scale to scale_range
    x the initial one. Returns (sim (s, R, t), info)."""
    kd_t = rl.kdtree(tgt)
    sim = init
    s_lo, s_hi = init[0] * scale_range[0], init[0] * scale_range[1]
    hist = []
    prev = None
    for _ in range(int(iters)):
        S = rl.apply_sim(src, sim)
        i_f, d_f = rl.nearest(kd_t, S)
        i_b, d_b = rl.nearest(rl.kdtree(S), tgt)
        cf = d_f <= np.percentile(d_f, 100 * trim)
        cb = d_b <= np.percentile(d_b, 100 * trim)
        P = np.concatenate([src[cf], src[i_b[cb]]])
        Q = np.concatenate([tgt[i_f[cf]], tgt[cb]])
        s, R, t = rl.umeyama(P, Q, with_scale=True, with_rotation=True)
        if rl.rotation_deg(R @ init[1].T) > max_rot_deg:   # too much turn: keep the start rotation, fit scale + shift
            R = init[1]
            s, _I, _t = rl.umeyama(P @ R.T, Q, with_scale=True, with_rotation=False)
            t = Q.mean(axis=0) - s * (R @ P.mean(axis=0))
        if not (s_lo <= s <= s_hi):
            s = min(max(s, s_lo), s_hi)
            t = Q.mean(axis=0) - s * (R @ P.mean(axis=0))
        sim = (float(s), R, t)
        rms = float(np.sqrt(np.mean(np.concatenate([d_f[cf], d_b[cb]]) ** 2)))
        hist.append(rms)
        if prev is not None and abs(prev - rms) < tol:
            break
        prev = rms
    S = rl.apply_sim(src, sim)
    _i, d_f = rl.nearest(kd_t, S)
    _i, d_b = rl.nearest(rl.kdtree(S), tgt)
    d = np.concatenate([d_f, d_b])
    keep = np.sort(d)[: max(1, int(len(d) * trim))]
    info = {"iterations": len(hist), "rms_m": float(np.sqrt(np.mean(keep ** 2))), "median_m": float(np.median(d)),
            "inlier_5mm": float((d_f < 0.005).mean()),
            "rotation_deg": round(rl.rotation_deg(sim[1] @ init[1].T), 3)}
    return sim, info


# --------------------------------------------------------------------------- Hunyuan-style export
def export_hunyuan(ob, path, long_side=2.0):
    """GLB of `ob` in the normalisation of the whole-body generator's meshes: bbox centred at the
    origin on every axis, longest side `long_side` (mesh_mv_703.glb: X +-0.968, Z +-0.999)."""
    cp = ml.duplicate(ob, ob.name + "_mv")
    lo, hi = af.world_bbox([cp])
    size = max(hi - lo) if hi != lo else 1.0
    k = long_side / float(size)
    c = (lo + hi) / 2.0
    M = mathutils.Matrix.Scale(k, 4) @ mathutils.Matrix.Translation(-c)
    cp.data.transform(M)
    cp.data.update()
    ml.shade_smooth(cp)          # no sharp edges: the exporter must not split vertices (the generator's GLBs are plain indexed meshes)
    af.export_glb(path, objects=[cp], animations=False, skins=False, export_normals=False)
    ml.remove_object(cp)
    return {"scale": round(k, 6), "shift": [round(float(x), 6) for x in (-c)]}
