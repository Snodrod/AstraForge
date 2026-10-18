"""Humanoid module library: automatic Mixamo-compatible rig for any humanoid mesh (local "auto-rigger").

Used by steps rig_humanoid, prepare_mixamo and import_mixamo_rig. Contract: docs/FORGE-CONTRACT.md
(sections 4-6), module doc: docs/forge/humanoid.md.

Pipeline (rig_humanoid):
  load -> weld/clean -> facing (auto) -> leftover yaw (shoulder-band PCA + feet) removed ->
  normalise (height by head top) -> AUTO LANDMARKS (height slices + 2D connected components per
  slice, linked between slices: feet/legs/crotch incl. coat hems, thin straight props and their
  shaft or hilt grips, shields (not wings/capes), trunk/neck/head, free arm chains with the shoulder
  joint taken from the arm itself, or T-pose X-slices; every prop bound to a bone; gross proportion
  and arm-symmetry failures stop the step) -> re-centre on the trunk axis and re-run if needed ->
  armature (25 or 65 mixamorig bones, documented rolls) -> bone heat on a welded/decimated proxy
  (fallback: voxel-remesh proxy, then nearest fill) -> Data Transfer to the full mesh -> rigid props
  / robe blend -> max 4 influences -> sockets (contract 6; gripping hands framed by the prop) -> QA
  (symmetry, ratios, deformation test on posed area) -> previews.

Bone frame convention (documented in humanoid.md, same as a Mixamo-rigged
reference zombie): local +Y runs head->tail; local +Z points to the character's
FRONT (world -Y) projected perpendicular to the bone for spine, neck, head, shoulders, arms, hands
and legs (Blender calculate_roll GLOBAL_NEG_Y); for Foot/ToeBase/Toe_End local +Z points UP
(GLOBAL_POS_Z). Fingers: +Z = the hand's +Z (thumb side) projected perpendicular to the finger.
When a bone is (nearly) parallel to its hint the secondary hint is used (world +Z for limbs that
point forward/backward, world -Y for feet that point straight down).

Ported pieces carry a "ported from <path>" note.
"""
import json
import math
import os
import time

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree
from mathutils.kdtree import KDTree

import af_common as af

PFX = "mixamorig:"
SIDES = ("Left", "Right")
SPINE = ["Hips", "Spine", "Spine1", "Spine2", "Neck", "Head", "HeadTop_End"]
ARM = ["Shoulder", "Arm", "ForeArm", "Hand"]
LEG = ["UpLeg", "Leg", "Foot", "ToeBase", "Toe_End"]
BONES25 = SPINE + [s + n for s in SIDES for n in ARM] + [s + n for s in SIDES for n in LEG]
FINGERS = ["Thumb", "Index", "Middle", "Ring", "Pinky"]
FINGER_BONES = [s + "Hand" + f + str(i) for s in SIDES for f in FINGERS for i in (1, 2, 3, 4)]
NON_DEFORM = {"HeadTop_End", "LeftToe_End", "RightToe_End"} | {
    s + "Hand" + f + "4" for s in SIDES for f in FINGERS}
# parent of every bone (short names)
PARENT = {"Hips": None, "Spine": "Hips", "Spine1": "Spine", "Spine2": "Spine1", "Neck": "Spine2",
          "Head": "Neck", "HeadTop_End": "Head"}
for _s in SIDES:
    PARENT.update({_s + "Shoulder": "Spine2", _s + "Arm": _s + "Shoulder",
                   _s + "ForeArm": _s + "Arm", _s + "Hand": _s + "ForeArm",
                   _s + "UpLeg": "Hips", _s + "Leg": _s + "UpLeg", _s + "Foot": _s + "Leg",
                   _s + "ToeBase": _s + "Foot", _s + "Toe_End": _s + "ToeBase"})
    for _f in FINGERS:
        PARENT[_s + "Hand" + _f + "1"] = _s + "Hand"
        for _i in (2, 3, 4):
            PARENT[_s + "Hand" + _f + str(_i)] = _s + "Hand" + _f + str(_i - 1)

# Proportion priors in units of body height H (head top = 1.0). Sources: Human Base Meshes male
# joints, zombie/militia reference marker files and the Mixamo auto-rig of an archer model; see humanoid.md.
PRIOR = {
    "crotch": 0.46, "upleg_dz": 0.05, "hips_dz": 0.08, "upleg_x": 0.052,
    "knee_frac": 0.49, "ankle_z": 0.05, "neck_below_top": 0.135, "head_above_neck": 0.03,
    "neckbase_below_neck": 0.015, "chin_below_head": 0.04, "shoulder_below_neck": 0.075, "shoulder_x": 0.105,
    "clav_x": 0.022, "upper_arm": 0.160, "forearm": 0.142, "hand": 0.100, "foot_len": 0.14,
    "spine_fracs": (0.17, 0.38, 0.61),
}
MARKER_KEYS = ("chin", "groin", "l_wrist", "r_wrist", "l_elbow", "r_elbow", "l_knee", "r_knee")
YAW_CORRECT_DEG = 5.0     # leftover yaw above this is rotated away (params.max_yaw_deg)
# gross proportion limits: outside them the landmarks are wrong, the rig is not built
GROSS = {"upper_fore": (0.6, 2.0), "forearm_min": 0.09, "leg_min": 0.31}   # stocky armored generations: 0.32-0.33 H
ARM_SYM_MAX = 0.10        # mirrored ForeArm/Hand joints further apart (H) -> fail, unless a hand holds a prop
ARM_CLAMP_H = None        # params.arm_clamp_h (extract chain: 0.47): a traced arm longer than this is clamped to the prior
# deformation gates on the POSED area share of triangles that grew > 3x (see deformation_test):
# clean meshes stay <= 0.4 % in arms forward / squat / stride and 2-5 % with arms straight up
TORN_LIMITS = {"main": 0.01, "arms_up": 0.08, "robe_stride": 0.04}


def b(name):
    return PFX + name


def short(name):
    return name.split(":")[-1]


def smoothstep(e0, e1, x):
    e0 = np.asarray(e0, dtype=np.float64)
    e1 = np.asarray(e1, dtype=np.float64)
    t = np.clip((np.asarray(x, dtype=np.float64) - e0) / np.maximum(e1 - e0, 1e-12), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def vec(a):
    return Vector((float(a[0]), float(a[1]), float(a[2])))


def rnd(v, n=4):
    return [round(float(x), n) for x in v]


# =================================================================== mesh helpers

def mesh_verts(ob, world=True):
    me = ob.data
    co = np.empty(len(me.vertices) * 3, dtype=np.float64)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    if world:
        M = np.array(ob.matrix_world, dtype=np.float64)
        co = co @ M[:3, :3].T + M[:3, 3]
    return co


def set_mesh_verts(ob, co):
    ob.data.vertices.foreach_set("co", np.asarray(co, dtype=np.float32).ravel())
    ob.data.update()


def mesh_tris(ob):
    me = ob.data
    me.calc_loop_triangles()
    t = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("vertices", t)
    return t.reshape(-1, 3)


def mesh_edges(ob):
    me = ob.data
    e = np.empty(len(me.edges) * 2, dtype=np.int64)
    me.edges.foreach_get("vertices", e)
    return e.reshape(-1, 2)


def tri_count(ob):
    return int(sum(len(p.vertices) - 2 for p in ob.data.polygons))


def sample_surface(verts, tris, n=200000, seed=7):
    """Area-weighted uniform surface samples (n,3) plus the triangle index of each sample."""
    T = verts[tris]
    area = np.linalg.norm(np.cross(T[:, 1] - T[:, 0], T[:, 2] - T[:, 0]), axis=1) * 0.5
    tot = area.sum()
    if tot <= 0:
        raise ValueError("mesh has zero surface area")
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(T), size=int(n), p=area / tot)
    u = rng.random((int(n), 2))
    flip = u.sum(1) > 1.0
    u[flip] = 1.0 - u[flip]
    P = T[idx, 0] + (T[idx, 1] - T[idx, 0]) * u[:, :1] + (T[idx, 2] - T[idx, 0]) * u[:, 1:]
    return P, idx


def select_only(objs):
    bpy.context.view_layer.update()   # drop stale entries of objects removed earlier
    af.select_only(objs if isinstance(objs, (list, tuple)) else [objs])


def apply_all_transforms(ob):
    select_only(ob)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)


def weld(ob, dist):
    """Merge vertices closer than dist (Tripo GLBs are split along UV seams). Returns merged count."""
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    n0 = len(bm.verts)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=dist)
    n1 = len(bm.verts)
    bm.to_mesh(ob.data)
    bm.free()
    ob.data.update()
    return n0 - n1


def append_objects(path, names):
    with bpy.data.libraries.load(os.path.abspath(path), link=False) as (src, dst):
        missing = [n for n in names if n not in src.objects]
        dst.objects = [n for n in names if n in src.objects]
    if missing:
        raise ValueError("objects not found in %s: %s" % (os.path.basename(path), missing))
    objs = [o for o in dst.objects if o is not None]
    for o in objs:
        bpy.context.scene.collection.objects.link(o)
    return objs


def load_body(req, warnings, name="Body", sockets=None):
    """Load inputs.mesh (or the scene opened from inputs.blend), keep only meshes (params.objects
    filters by name), unparent, drop rigs/modifiers/shape keys, apply transforms, join into one mesh.
    sockets (list): socket empties of the input (socket_* / game_socket) are recorded into it
    (_mesh_lib.socket_records, world matrices) before the empties are deleted.
    Returns the body object (world transform = identity)."""
    inputs, params = req.get("inputs", {}), req.get("params", {})
    names = params.get("objects")
    if inputs.get("mesh"):
        af.reset_scene()
        path = inputs["mesh"]
        if not os.path.exists(path):
            raise FileNotFoundError("inputs.mesh not found: %s" % path)
        if path.lower().endswith(".blend") and names:
            objs = append_objects(path, names)
        else:
            objs = af.import_any(path)
    elif inputs.get("blend"):
        objs = list(bpy.context.scene.objects)
    else:
        raise ValueError("inputs.mesh or inputs.blend is required")
    bpy.context.view_layer.update()
    ms = [o for o in objs if o.type == "MESH" and (not names or o.name in names)]
    if not ms:
        raise ValueError("no mesh objects in the input")
    if sockets is not None:
        from steps import _mesh_lib as ml
        sockets.extend(ml.socket_records(objs))
    # unparent keeping world transforms, single user data
    for o in ms:
        mw = o.matrix_world.copy()
        o.parent = None
        o.matrix_world = mw
        if o.data.users > 1:
            o.data = o.data.copy()
    for o in list(bpy.context.scene.objects):
        if o not in ms:
            bpy.data.objects.remove(o, do_unlink=True)
    bpy.context.view_layer.update()
    dropped = []
    for o in ms:
        for m in list(o.modifiers):
            if m.type in ("ARMATURE", "MULTIRES", "SUBSURF"):
                dropped.append("%s:%s" % (o.name, m.type))
                o.modifiers.remove(m)
        if o.data.shape_keys:
            dropped.append("%s:shape_keys" % o.name)
            o.shape_key_clear()
        o.vertex_groups.clear()
        if o.modifiers:  # bake the remaining generative modifiers (mirror, solidify...)
            dg = bpy.context.evaluated_depsgraph_get()
            me = bpy.data.meshes.new_from_object(o.evaluated_get(dg))
            o.modifiers.clear()
            o.data = me
    if dropped:
        warnings.append("dropped from input: " + ", ".join(sorted(set(dropped))[:12]))
    for o in ms:
        o.rotation_mode = "XYZ"
    select_only(ms)
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    if len(ms) > 1:
        select_only(ms)
        bpy.context.view_layer.objects.active = max(ms, key=lambda o: len(o.data.vertices))
        bpy.ops.object.join()
    body = bpy.context.view_layer.objects.active
    body.name = name
    body.data.name = name
    return body


def transform_body(body, M, P=None):
    """Apply a 4x4 world transform to the body mesh data (and to sample points P)."""
    body.data.transform(M)
    body.data.update()
    if P is not None:
        A = np.array(M, dtype=np.float64)
        P = P @ A[:3, :3].T + A[:3, 3]
    return P


# =================================================================== slicing

class Comp:
    __slots__ = ("k", "idx", "n", "c", "lo", "hi", "cells", "id")

    def __init__(self, k, idx, pts, cells):
        self.k = k
        self.idx = idx
        self.n = len(idx)
        self.c = pts.mean(0)
        self.lo = pts.min(0)
        self.hi = pts.max(0)
        self.cells = cells
        self.id = -1

    def ext(self, ax):
        return float(self.hi[ax] - self.lo[ax])


def components_2d(P, idx, ab, cell, k, min_n=3):
    """Connected components (8-neighbourhood on a 2D grid of size `cell`) of points P[idx]
    projected on axes ab."""
    if len(idx) == 0:
        return []
    q = np.floor(P[idx][:, ab] / cell).astype(np.int64)
    uq, inv = np.unique(q, axis=0, return_inverse=True)
    inv = inv.ravel()
    keys = [tuple(r) for r in uq.tolist()]
    pos = {key: i for i, key in enumerate(keys)}
    label = [-1] * len(keys)
    nl = 0
    for s in range(len(keys)):
        if label[s] >= 0:
            continue
        label[s] = nl
        stack = [s]
        while stack:
            t = stack.pop()
            i, j = keys[t]
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    u = pos.get((i + di, j + dj))
                    if u is not None and label[u] < 0:
                        label[u] = nl
                        stack.append(u)
        nl += 1
    lab = np.array(label, dtype=np.int64)
    plab = lab[inv]
    out = []
    order = np.argsort(plab, kind="stable")
    bounds = np.searchsorted(plab[order], np.arange(nl + 1))
    for l_ in range(nl):
        sel = order[bounds[l_]:bounds[l_ + 1]]
        if len(sel) < min_n:
            continue
        cells = {keys[i] for i in np.nonzero(lab == l_)[0].tolist()}
        out.append(Comp(k, idx[sel], P[idx[sel]], cells))
    return out


class Slices:
    """Point cloud cut into slabs along `axis`; per slab 2D components; links between neighbours."""

    def __init__(self, P, axis=2, step=0.005, thick=0.006, cell=1.0 / 160, lo=None, hi=None,
                 mask=None):
        self.P, self.axis, self.step, self.cell = P, axis, step, cell
        self.ab = [i for i in range(3) if i != axis]
        coord = P[:, axis]
        lo = float(coord.min()) if lo is None else lo
        hi = float(coord.max()) if hi is None else hi
        self.levels = np.arange(lo, hi, step)
        order = np.argsort(coord, kind="stable")
        cs = coord[order]
        self.comps = []
        allc = []
        for k, z in enumerate(self.levels):
            i0, i1 = np.searchsorted(cs, z), np.searchsorted(cs, z + thick)
            idx = order[i0:i1]
            if mask is not None:
                idx = idx[mask[idx]]
            cl = components_2d(P, idx, self.ab, cell, k)
            for c in cl:
                c.id = len(allc)
                allc.append(c)
            self.comps.append(cl)
        self.all = allc
        self.up = {c.id: [] for c in allc}
        self.down = {c.id: [] for c in allc}
        for k in range(len(self.levels) - 1):
            cellmap = {}
            for c in self.comps[k]:
                for key in c.cells:
                    cellmap[key] = c.id
            for c2 in self.comps[k + 1]:
                hit = set()
                for (i, j) in c2.cells:
                    for di in (-1, 0, 1):
                        for dj in (-1, 0, 1):
                            u = cellmap.get((i + di, j + dj))
                            if u is not None:
                                hit.add(u)
                for u in hit:
                    self.up[u].append(c2.id)
                    self.down[c2.id].append(u)

    def k_of(self, z):
        return int(np.clip(round((z - self.levels[0]) / self.step), 0, len(self.levels) - 1))

    def z_of(self, k):
        return float(self.levels[k] + self.step * 0.5)


# =================================================================== facing

def detect_facing(P):
    """Detect where the character faces. P: samples normalised so z in [0,1] (bbox height).
    Returns (front, confidence, cues) with front in {'-Y','+Y','-X','+X'}.
    Cues: lateral axis = wider of X/Y in the shoulder band; sign from feet (toes extend forward of
    the shin) and from the head centroid being in front of the trunk centroid."""
    z = P[:, 2]
    band = (z > 0.55) & (z < 0.8)
    ex = float(np.percentile(P[band, 0], 95) - np.percentile(P[band, 0], 5))
    ey = float(np.percentile(P[band, 1], 95) - np.percentile(P[band, 1], 5))
    lat, dep = (0, 1) if ex >= ey else (1, 0)
    ratio = max(ex, ey) / max(min(ex, ey), 1e-6)
    cues = {"lateral": "XY"[lat], "width_ratio": round(ratio, 3)}
    lc = float(np.median(P[band, lat]))
    score, weight = 0.0, 0.0
    # feet: two separate blobs at the bottom, each with a shin above it
    foot_vals = []
    for side in (-1, 1):
        sel_f = (z < 0.025) & ((P[:, lat] - lc) * side > 0.02)
        sel_s = (z > 0.07) & (z < 0.13) & ((P[:, lat] - lc) * side > 0.02) & \
            (np.abs(P[:, lat] - lc) < 0.25)
        if sel_f.sum() < 30 or sel_s.sum() < 30:
            continue
        fd, sd = P[sel_f, dep], float(np.median(P[sel_s, dep]))
        toe_neg = sd - float(np.percentile(fd, 3))
        toe_pos = float(np.percentile(fd, 97)) - sd
        sw = float(np.percentile(P[sel_s, lat], 95) - np.percentile(P[sel_s, lat], 5))
        flen = float(np.percentile(fd, 97) - np.percentile(fd, 3))
        if sw > 0.2 or flen < 0.06:   # robe hem / skirt / cloth strips, not a foot under a shin
            continue
        foot_vals.append(float(np.clip((toe_neg - toe_pos) / max(abs(toe_neg) + abs(toe_pos), 1e-6),
                                       -1, 1)))
    if len(foot_vals) == 2 and np.sign(foot_vals[0]) == np.sign(foot_vals[1]):
        f1 = float(np.mean(foot_vals))
        score += 0.7 * f1
        weight += 0.7
        cues["feet"] = round(f1, 3)
    def mid(sel):
        return float(np.percentile(P[sel, dep], 3) + np.percentile(P[sel, dep], 97)) * 0.5
    head = (z > 0.86) & (z < 0.97) & (np.abs(P[:, lat] - lc) < 0.1)
    trunk = (z > 0.55) & (z < 0.75) & (np.abs(P[:, lat] - lc) < 0.1)
    if head.sum() > 50 and trunk.sum() > 50:
        dt = mid(trunk)
        f2 = float(np.clip((dt - mid(head)) / 0.03, -1, 1))
        score += 0.3 * f2
        weight += 0.3
        cues["head"] = round(f2, 3)
        # hands/forearms hang or reach in front of the trunk centre in T, A and arms-down poses
        hands = (z > 0.35) & (z < 0.65) & (np.abs(P[:, lat] - lc) > 0.15) & (np.abs(P[:, lat] - lc) < 0.5)
        if hands.sum() > 50:
            f3 = float(np.clip(-(np.median(P[hands, dep]) - dt) / 0.04, -1, 1))
            score += 0.3 * f3
            weight += 0.3
            cues["hands"] = round(f3, 3)
    if weight > 0:
        score /= weight
    conf = min(1.0, abs(score) / 0.4) * (1.0 if ratio > 1.25 else 0.5)
    axis = "XY"[dep]
    if weight == 0 or abs(score) < 0.1:
        front = "-Y" if axis == "Y" else "-X"
        conf = 0.2
    else:
        front = ("-" if score > 0 else "+") + axis
    cues["score"] = round(score, 3)
    return front, round(conf, 3), cues


FRONT_TURN = {"-Y": 0.0, "+Y": 180.0, "+X": -90.0, "-X": 90.0}   # deg about Z to face -Y
FRONT_OPPOSITE = {"-Y": "+Y", "+Y": "-Y", "+X": "-X", "-X": "+X"}


def _wrap90(a):
    """Angle in degrees wrapped to (-90, 90] (an axis, not a direction)."""
    a = (a + 90.0) % 180.0 - 90.0
    return 90.0 if a == -90.0 else a


def estimate_yaw(Pn):
    """Residual yaw of a figure that already faces roughly -Y. Pn: samples normalised so z is in
    [0, 1] (bbox height) and the bbox is centred. Returns (yaw_deg, confidence, cues): the angle of
    the character's left-right axis from world X (positive = turned counter-clockwise seen from
    above). Cues: PCA of the trunk core in the shoulder band (XY), and the line between the two feet
    (used only when it agrees, a stride puts the feet on a diagonal)."""
    z = Pn[:, 2]
    cues = {}
    band = (z > 0.62) & (z < 0.80)
    if band.sum() < 200:
        return 0.0, 0.0, {"reason": "no shoulder band"}
    Q = Pn[band][:, :2]
    c = np.median(Q, axis=0)
    Q = Q[np.linalg.norm(Q - c, axis=1) < 0.22]
    ev, evec = np.linalg.eigh(np.cov((Q - Q.mean(0)).T))
    ratio = float(math.sqrt(max(ev[1], 1e-12) / max(ev[0], 1e-12)))
    yaw_sh = _wrap90(math.degrees(math.atan2(evec[1, 1], evec[0, 1])))
    cues.update({"shoulder_pca_deg": round(yaw_sh, 2), "shoulder_pca_ratio": round(ratio, 3)})
    yaw, conf = yaw_sh, float(np.clip((ratio - 1.15) / 0.5, 0.0, 1.0))
    feet = Pn[z < 0.03][:, :2]
    if len(feet) > 60:
        # 2-means seeded at the lateral extremes along the shoulder axis
        ax = np.array([math.cos(math.radians(yaw_sh)), math.sin(math.radians(yaw_sh))])
        s_ = feet @ ax
        ca, cb = feet[np.argmin(s_)], feet[np.argmax(s_)]
        for _ in range(8):
            lab = np.linalg.norm(feet - ca, axis=1) > np.linalg.norm(feet - cb, axis=1)
            if lab.all() or (~lab).all():
                break
            ca, cb = feet[~lab].mean(0), feet[lab].mean(0)
        dv = cb - ca
        if np.linalg.norm(dv) > 0.08:
            yaw_ft = _wrap90(math.degrees(math.atan2(dv[1], dv[0])))
            cues["feet_deg"] = round(yaw_ft, 2)
            if abs(_wrap90(yaw_ft - yaw_sh)) < 10.0:
                yaw = _wrap90(yaw_sh + 0.3 * _wrap90(yaw_ft - yaw_sh))
                conf = max(conf, 0.7)
            elif abs(_wrap90(yaw_ft - yaw_sh)) > 20.0 and ratio < 1.6:
                # a weak shoulder axis that the feet contradict: a big shield or a cape widens the
                # shoulder band along another axis (generated armored warrior with a tower shield:
                # shoulders 38.7 deg, feet -1.5 deg) - no confident yaw, the input is left as it is
                conf = min(conf, 0.2)
                cues["feet_disagree"] = True
    cues["yaw_deg"] = round(yaw, 2)
    return float(yaw), round(conf, 3), cues


# =================================================================== landmarks

def _uf_groups(ids, links):
    parent = {i: i for i in ids}

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in ids:
        for j in links.get(i, ()):
            if j in parent:
                ri, rj = find(i), find(j)
                if ri != rj:
                    parent[ri] = rj
    groups = {}
    for i in ids:
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def _line_fit(pts):
    c = pts.mean(0)
    _, _, vt = np.linalg.svd(pts - c, full_matrices=False)
    d = vt[0] / np.linalg.norm(vt[0])
    if d[2] < 0 or (abs(d[2]) < 1e-6 and d[0] < 0):
        d = -d
    rel = pts - c
    t = rel @ d
    resid = np.linalg.norm(rel - np.outer(t, d), axis=1)
    return c, d, t, resid


PROP_TRIM = 0.15        # share of slice levels a shaft fit may drop (finger tips, split cross-sections)
PROP_SPLIT_MAX = 0.25   # chains with a second real component on more levels are not one shaft

# curved held props (bow limb + string, a curved blade): Landmarks.find_bows. H units.
BOW_THIN = 0.042        # widest horizontal cross-section of a limb/string slice component
BOW_SIDE = 0.12         # beside the body: |x - X0| of the component centre at least this ...
BOW_TRUNK_GAP = 0.10    # ... and no point of it closer than this to the midline
BOW_MIN_EXTENT = 0.35   # vertical extent of the whole curved prop
BOW_PLANE_RMS = 0.012   # centroids within this of one plane (limb and string share the bow plane)
BOW_PLANE_NZ = 0.5      # |plane normal . Z| below this: the plane is roughly vertical
BOW_CURVE_RMS = 0.008   # in-plane straight-line rms above this (or limb + string levels): curved
BOW_FIST_PTS = 25       # body samples next to the curve (the fist) needed to call it held

# straight props held at an angle (a sword / mace / axe / spear / scythe / banner pole held forward, diagonally
# or down-and-out): Landmarks.find_props(oblique) + Landmarks.find_blades. H units.
OBLIQUE_DIRS = [np.array(v, dtype=np.float64) / np.linalg.norm(v) for v in (
    (1, 0, 1), (-1, 0, 1), (0, 1, 1), (0, -1, 1), (1, 1, 0), (1, -1, 0),
    (1, 1, 1), (-1, 1, 1), (1, -1, 1), (-1, -1, 1), (0, 1, 0))]   # face + body diagonals (every line within ~27 deg
# of one) and the forward axis (a mace or sword held straight forward; T-pose arms lie along X, never taken)
BLADE_THIN = 0.055      # widest cross-section of a slice component of an oblique blade / haft (a flat blade
                        # cut a little off its normal is ~0.045-0.05 H wide on the batch generations)
BLADE_FLAT = (0.08, 0.03)  # ... or a flat cross-section: wider (a greatsword, 0.06 H) but thinner than this
BLADE_NORMAL_COS = 0.8  # the chain runs within ~37 deg of the slicing normal (cross-sections stay near true)
BLADE_MIN_LEN = 0.25    # straight thin length (longer than a hand; a sword blade is 0.3-0.65 H)
BLADE_REACH = 0.45      # the far tip lies beyond a bare arm's reach from its shoulder (upper arm + forearm + hand)
BLADE_FIST_REACH = 0.47 # ... while the fist on the prop is within that reach
BLADE_FIST_PTS = 30     # body samples of the fist next to the axis
BLADE_TIP_FREE = 12     # at most this many body samples just beyond the free tip (it sticks out into the air)
BLADE_GRIP_TO_TIP = 0.20  # the fist is at least this far from the free tip along the axis


def _slice_frame(n):
    """Rows: an orthonormal frame whose third axis is the unit vector n (q = R @ p slices along n)."""
    n = np.asarray(n, dtype=np.float64)
    n = n / np.linalg.norm(n)
    t = np.array([1.0, 0.0, 0.0]) if abs(n[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u = np.cross(n, t)
    u /= np.linalg.norm(u)
    return np.stack([u, np.cross(n, u), n])


def _chain_axis(S, g, trim=PROP_TRIM, split_max=PROP_SPLIT_MAX):
    """Axis and straightness (rms, H) of a chain `g` of thin slice components of Slices `S`.

    Robust to decimation (smoke 10.10: after cleanup to 40k / 20k tris a robed caster's staff
    shaft picked up small off-axis slice fragments - finger tips at the grip, a cross-section split
    by the coarser surface - and the plain centroid rms went 0.005 -> 0.007, over the 0.006
    straightness limit, so the staff was lost and the arms failed the symmetry check): one
    cross-section per slice level (its largest component; small fragments within 0.03 H of it
    are ignored), then a least-trimmed line fit that may drop `trim` of the levels. A chain whose
    levels often hold a second real component (a bow limb and its string: more than `split_max`
    of the levels) is not one shaft and keeps the strict fit over every component centroid.
    Returns (c, d, rms, cents): cents are the centroids the line was fitted to."""
    main = {}
    for i in g:
        ci = S.all[i]
        j = main.get(ci.k)
        if j is None or ci.n > S.all[j].n:
            main[ci.k] = i
    split = set()
    for i in g:
        ci = S.all[i]
        m = S.all[main[ci.k]]
        if i != main[ci.k] and (ci.n > 0.5 * m.n or float(np.linalg.norm((ci.c - m.c)[S.ab])) > 0.03):
            split.add(ci.k)
    if len(split) > split_max * len(main):
        cents = np.array([S.all[i].c for i in g])
        c, d, _, r = _line_fit(cents)
        return c, d, float(np.sqrt(np.mean(r ** 2))), cents
    cents = np.array([S.all[main[k]].c for k in sorted(main)])
    c, d, _, r = _line_fit(cents)
    keep = np.ones(len(cents), dtype=bool)
    if trim > 0 and len(cents) >= 8:
        for _ in range(3):
            keep = r <= np.quantile(r, 1.0 - trim)
            c, d, _, _ = _line_fit(cents[keep])
            rel = cents - c
            r = np.linalg.norm(rel - np.outer(rel @ d, d), axis=1)
    return c, d, float(np.sqrt(np.mean(r[keep] ** 2))), cents[keep]


def parse_markers(mk, H):
    """params.markers -> {key: np.array 2D (x,z) or 3D (x,y,z)} in H units, joints3d separately."""
    out, joints = {}, {}
    if not mk:
        return out, joints
    for k in MARKER_KEYS:
        v = mk.get(k)
        if v is None:
            continue
        a = np.asarray(v, dtype=np.float64) / H
        if a.shape not in ((2,), (3,)):
            raise ValueError("params.markers.%s must be [x, z] or [x, y, z]" % k)
        out[k] = a
    for name, v in (mk.get("joints") or mk.get("joints3d") or {}).items():
        nm = short(name)
        if nm not in PARENT and nm not in ("LeftHandTip", "RightHandTip"):
            raise ValueError("unknown joint in params.markers.joints: %s" % name)
        joints[nm] = np.asarray(v, dtype=np.float64) / H
    return out, joints


class Landmarks:
    """Automatic humanoid landmarks from geometry alone. All internal units: body height H = 1
    (head top), X = character left, -Y = front, Z up, feet at 0. See module docstring."""

    def __init__(self, P_m, H, params=None, warnings=None):
        self.H = float(H)
        self.P = np.asarray(P_m, dtype=np.float64) / self.H
        self.params = params or {}
        self.warn = warnings if warnings is not None else []
        self.ov, self.ov_joints = parse_markers(self.params.get("markers"), self.H)
        self.conf, self.src, self.info = {}, {}, {}
        self.props = []
        self.bow_props = []
        self.oblique_cands = []
        self.prop_mask = np.zeros(len(self.P), dtype=bool)
        self.fail = None

    # ------------------------------------------------------------- utilities
    def mid_y(self, x, z, r=0.03, default=None):
        P = self.Pb
        sel = (np.abs(P[:, 0] - x) < r) & (np.abs(P[:, 2] - z) < r)
        if sel.sum() < 5:
            sel = (np.abs(P[:, 0] - x) < 2 * r) & (np.abs(P[:, 2] - z) < 2 * r)
        if sel.sum() < 5:
            return self.Y0 if default is None else default
        ys = P[sel, 1]
        return float((np.percentile(ys, 5) + np.percentile(ys, 95)) * 0.5)

    def set_conf(self, key, value, src):
        self.conf[key] = round(float(np.clip(value, 0.0, 1.0)), 3)
        self.src[key] = src

    @staticmethod
    def side_sign(side):
        return 1.0 if side == "Left" else -1.0

    # ------------------------------------------------------------- pipeline
    def run(self):
        t0 = time.time()
        self.find_props()
        self.Pb = self.P[~self.prop_mask]
        self.midline()
        self.head_top()
        self.ground()
        self.S = Slices(self.P, axis=2, step=0.005, thick=0.006, cell=1.0 / 160, lo=0.0,
                        hi=self.top + 0.01, mask=~self.prop_mask)
        self.legs()
        self.trunk()
        # straight props held at an angle (a sword held forward / diagonally / down-and-out, a banner
        # pole leaning out): the slice chains along the vertical/X/Y axes miss them (the blade crosses
        # the slices obliquely and reads as part of the arm, which then reaches 0.6-0.95 H)
        if self.find_blades():
            self.Pb = self.P[~self.prop_mask]
            self.S = Slices(self.P, axis=2, step=0.005, thick=0.006, cell=1.0 / 160, lo=0.0,
                            hi=self.top + 0.01, mask=~self.prop_mask)
            self.legs()
            self.trunk()
        # bows (curved limb + string held in one hand) before the plates: a bow seen as a "plate"
        # pulled the sleeve with it (a scout model), an undetected one put the wrist on the limb (archer)
        if self.find_bows():
            self.Pb = self.P[~self.prop_mask]
            self.S = Slices(self.P, axis=2, step=0.005, thick=0.006, cell=1.0 / 160, lo=0.0,
                            hi=self.top + 0.01, mask=~self.prop_mask)
            self.legs()
            self.trunk()
        if self.find_plates():      # shields: rigid props; re-slice without them
            self.Pb = self.P[~self.prop_mask]
            self.S = Slices(self.P, axis=2, step=0.005, thick=0.006, cell=1.0 / 160, lo=0.0,
                            hi=self.top + 0.01, mask=~self.prop_mask)
            self.legs()
            self.trunk()
        if self.plate_props or self.bow_props or any(p.get("oblique") for p in self.props):
            # the midline was measured with the shield / bow / oblique blade still in the cloud: a big
            # shield beside the trunk pulls the band median toward it (generated knights: the spine
            # ran 0.09-0.1 H off the head and the head-band check, limited to 0.08 H, could not pull it
            # back); measure it again without them
            # only a real pull counts (>= 0.03 H): small changes flip with the re-centring passes
            x_old, y_old, top_old = self.X0, self.Y0, self.top
            self.midline()
            self.head_top()
            self.info["midline_after_props"] = round(self.X0 - x_old, 4)
            if abs(self.X0 - x_old) < 0.03:
                self.X0, self.Y0, self.top = x_old, y_old, top_old
            else:
                self.S = Slices(self.P, axis=2, step=0.005, thick=0.006, cell=1.0 / 160, lo=0.0,
                                hi=self.top + 0.01, mask=~self.prop_mask)
                self.legs()
                self.trunk()
        self.grips()
        self.arms()
        self.feet_knees()
        self.assemble()
        self.bind_props()
        self.sanity()
        self.info["seconds"] = round(time.time() - t0, 2)
        return self

    # ------------------------------------------------------------- props
    def find_props(self, oblique=True):
        """Long thin straight held props (staff, spear, sword): chains of thin slice components
        along Z (and X/Y for horizontal props), merged when collinear. oblique: also collect straight
        thin chains along the face/body diagonals (OBLIQUE_DIRS) as candidates for find_blades - they
        are not props until find_blades sees a fist on them and a free tip beyond the arm's reach."""
        P = self.P
        self.oblique_cands = self._oblique_candidates() if oblique else []
        cand = []
        for axis in (2, 0, 1):
            S = Slices(P, axis=axis, step=0.005, thick=0.006, cell=1.0 / 200)
            # props are 2-5 cm thick (knobby staffs up to ~0.045 H); along X/Y (horizontal props)
            # the limit stays below slim T-pose forearms (~0.04 H)
            lim = 0.045 if axis == 2 else 0.032
            thin = [c.id for c in S.all
                    if max(c.ext(S.ab[0]), c.ext(S.ab[1])) < lim and c.n >= 4]
            tset = set(thin)
            links = {i: [j for j in S.up[i] if j in tset] for i in thin}
            for g in _uf_groups(thin, links):
                ks = [S.all[i].k for i in g]
                span = (max(ks) - min(ks) + 1) * S.step
                if span < 0.10:
                    continue
                idx = np.concatenate([S.all[i].idx for i in g])
                # straightness from the slice centroids (surface samples sit at the shaft radius);
                # one cross-section per level and a trimmed fit keep it valid on decimated meshes
                c, d, rms, cents = _chain_axis(S, g)
                t = (P[idx] - c) @ d
                length = float(np.percentile(t, 99) - np.percentile(t, 1))
                if rms > 0.006 or length < 0.10 or abs(d[axis]) < 0.6:
                    continue
                rad = float(np.median([max(S.all[i].ext(S.ab[0]), S.all[i].ext(S.ab[1]))
                                       for i in g])) * 0.5
                cand.append({"idx": idx, "c": c, "d": d, "rms": rms, "axis": axis, "rad": rad,
                             "cents": cents})
        # merge collinear candidates (the grip interrupts the chain; several axes see one prop)
        n = len(cand)
        links = {i: [] for i in range(n)}
        for i in range(n):
            for j in range(i + 1, n):
                a, bb = cand[i], cand[j]
                ang = math.degrees(math.acos(min(1.0, abs(float(a["d"] @ bb["d"])))))
                if ang > 12:
                    continue
                rel = bb["c"] - a["c"]
                da = np.linalg.norm(rel - (rel @ a["d"]) * a["d"])
                rel2 = a["c"] - bb["c"]
                db = np.linalg.norm(rel2 - (rel2 @ bb["d"]) * bb["d"])
                if min(da, db) < 0.02:
                    links[i].append(j)
        props = []
        for g in _uf_groups(list(range(n)), links):
            idx = np.unique(np.concatenate([cand[i]["idx"] for i in g]))
            cents = np.concatenate([cand[i]["cents"] for i in g])
            c, d, _, resid = _line_fit(cents)
            t = (P[idx] - c) @ d
            t_lo, t_hi = float(np.percentile(t, 0.5)), float(np.percentile(t, 99.5))
            if t_hi - t_lo < 0.30:
                continue
            rad = float(np.median([cand[i]["rad"] for i in g]))
            props.append({"c": c, "d": d, "t": [t_lo, t_hi], "rad": rad,
                          "rms": float(np.sqrt(np.mean(resid ** 2)))})
        # two mirrored ground-touching vertical "props" are skinny legs, not props; a straight
        # line through the trunk from one side to the other is a pair of T-pose arms
        xmed = float(np.median(P[:, 0]))
        ymed = float(np.median(P[(P[:, 2] > 0.5) & (P[:, 2] < 0.8), 1]))
        drop = set()
        for i, p in enumerate(props):
            ends = [p["c"] + p["d"] * p["t"][0], p["c"] + p["d"] * p["t"][1]]
            if (ends[0][0] - xmed) * (ends[1][0] - xmed) < 0:
                tt = np.linspace(p["t"][0], p["t"][1], 200)
                q = p["c"] + np.outer(tt, p["d"])
                trunk = (q[:, 2] > 0.35) & (q[:, 2] < 0.9)
                if trunk.any() and np.min(np.hypot(q[trunk, 0] - xmed, q[trunk, 1] - ymed)) < 0.06:
                    drop.add(i)

        def zlow(p):
            return min(p["c"][2] + p["d"][2] * p["t"][0], p["c"][2] + p["d"][2] * p["t"][1])
        for i in range(len(props)):
            for j in range(i + 1, len(props)):
                pi_, pj = props[i], props[j]
                if zlow(pi_) < 0.04 and zlow(pj) < 0.04 and \
                        abs(pi_["c"][0] + pj["c"][0] - 2 * xmed) < 0.06 and \
                        abs(pi_["c"][0] - pj["c"][0]) < 0.35:
                    drop |= {i, j}
        props = [p for i, p in enumerate(props) if i not in drop]
        mask = np.zeros(len(P), dtype=bool)
        for p in props:
            rel = P - p["c"]
            t = rel @ p["d"]
            dist = np.linalg.norm(rel - np.outer(t, p["d"]), axis=1)
            r_cap = max(0.012, 1.8 * p["rad"])
            m = (dist < r_cap) & (t > p["t"][0] - 0.01) & (t < p["t"][1] + 0.01)
            p["r_cap"] = r_cap
            mask |= m
        if props:
            mask = self._extend_props(mask, props)
            # report the full extent (ornaments / blade tips grown at the ends included)
            for p in props:
                rel = P[mask] - p["c"]
                t = rel @ p["d"]
                near = np.linalg.norm(rel - np.outer(t, p["d"]), axis=1) < 0.1
                if near.sum() > 10:
                    p["extent"] = [float(np.percentile(t[near], 0.2)), float(np.percentile(t[near], 99.8))]
        self.props = props
        self.prop_mask = mask
        self.info["props"] = len(props)
        if self.oblique_cands:
            # a prop the axis passes already found is not an oblique candidate again
            keep = []
            for q in self.oblique_cands:
                dup = False
                for p in props:
                    ang = math.degrees(math.acos(min(1.0, abs(float(q["d"] @ p["d"])))))
                    rel = q["c"] - p["c"]
                    off = float(np.linalg.norm(rel - (rel @ p["d"]) * p["d"]))
                    if (ang < 15 and off < 0.03) or mask[q["idx"]].mean() > 0.5:
                        dup = True
                        break
                if not dup:
                    keep.append(q)
            self.oblique_cands = keep
        self.info["oblique_candidates"] = len(self.oblique_cands)

    def _oblique_candidates(self):
        """Straight thin chains along the face/body diagonals: per direction n the samples are sliced
        across n (a frame whose third axis is n), thin components (< BLADE_THIN) are chained across
        neighbouring slices, and a chain counts when it is straight (the decimation-robust _chain_axis,
        rms <= 0.006 H), runs within ~37 deg of n and is at least 0.10 H long. Collinear chains of
        different directions (one blade seen by several slicings, a blade interrupted by the crossguard)
        are merged. Returns candidates {idx, c, d, t, rad, rms, dirs} in the body frame."""
        P = self.P
        cand = []
        for n in OBLIQUE_DIRS:
            Rm = _slice_frame(n)
            Q = P @ Rm.T
            S = Slices(Q, axis=2, step=0.005, thick=0.006, cell=1.0 / 200)
            thin = [c.id for c in S.all if c.n >= 4 and (max(c.ext(0), c.ext(1)) < BLADE_THIN or (
                max(c.ext(0), c.ext(1)) < BLADE_FLAT[0] and min(c.ext(0), c.ext(1)) < BLADE_FLAT[1]))]
            tset = set(thin)
            links = {i: [j for j in S.up[i] if j in tset] for i in thin}
            for g in _uf_groups(thin, links):
                ks = [S.all[i].k for i in g]
                if (max(ks) - min(ks) + 1) * S.step < 0.10:
                    continue
                c, d, rms, cents = _chain_axis(S, g)
                if rms > 0.006 or abs(float(d[2])) < BLADE_NORMAL_COS:
                    continue
                idx = np.concatenate([S.all[i].idx for i in g])
                t = (Q[idx] - c) @ d
                if float(np.percentile(t, 99) - np.percentile(t, 1)) < 0.10:
                    continue
                wid = float(np.median([max(S.all[i].ext(0), S.all[i].ext(1)) for i in g]))
                cand.append({"idx": idx, "c": c @ Rm, "d": d @ Rm, "rms": rms, "rad": 0.5 * wid,
                             "cents": cents @ Rm, "dir": rnd(n, 3)})
        n = len(cand)
        links = {i: [] for i in range(n)}
        for i in range(n):
            for j in range(i + 1, n):
                a, bb = cand[i], cand[j]
                ang = math.degrees(math.acos(min(1.0, abs(float(a["d"] @ bb["d"])))))
                if ang > 12:
                    continue
                rel = bb["c"] - a["c"]
                if min(np.linalg.norm(rel - (rel @ a["d"]) * a["d"]),
                       np.linalg.norm(rel - (rel @ bb["d"]) * bb["d"])) < 0.02:
                    links[i].append(j)
        out = []
        for g in _uf_groups(list(range(n)), links):
            idx = np.unique(np.concatenate([cand[i]["idx"] for i in g]))
            cents = np.concatenate([cand[i]["cents"] for i in g])
            c, d, _, resid = _line_fit(cents)
            t = (P[idx] - c) @ d
            t_lo, t_hi = float(np.percentile(t, 0.5)), float(np.percentile(t, 99.5))
            if t_hi - t_lo < 0.15:
                continue
            out.append({"idx": idx, "c": c, "d": d, "t": [t_lo, t_hi],
                        "rad": float(np.median([cand[i]["rad"] for i in g])),
                        "rms": float(np.sqrt(np.mean(resid ** 2))), "dirs": [cand[i]["dir"] for i in g]})
        return out

    def find_blades(self):
        """Straight props held at an angle - a sword, mace, axe, spear, scythe or banner pole held forward,
        diagonally or down-and-out. The axis passes of find_props see only props close to vertical
        (or thin horizontal shafts); a blade crossing those slices obliquely is wider than their limit and
        becomes part of the arm (the "arm" then reaches 0.6-0.95 H and the rig is refused).

        Each straight thin chain of the diagonal slicings (find_props, oblique) is a held prop when:
          - it is at least BLADE_MIN_LEN long and not behind the back (cape strips, quiver);
          - a fist sits on it: the densest 0.08 H stretch of body mass within 0.07 H of the axis
            (BLADE_FIST_PTS samples), above 0.28 H, within reach of its side's shoulder (BLADE_FIST_REACH)
            and on a free arm chain that climbs toward the armpit;
          - the end away from the fist is a free tip (at most BLADE_TIP_FREE body samples just beyond
            it) at least BLADE_GRIP_TO_TIP from the fist and BEYOND a bare arm's reach from the shoulder
            (BLADE_REACH): a forearm or a skinny limb never reaches that far, a cloth strip hanging from
            the belt has no fist on a free arm chain.
        Its samples join the prop mask (the wrist is found on the arm, not at the blade tip); the hand
        holding it gets the grip (an end grip at the hilt or a shaft grip), the prop is rigid to that
        hand and the hand socket +Z runs toward the far end (prop_grip_dir). Returns True if the prop
        mask changed."""
        self.info["blades"] = report = []
        if not self.oblique_cands:
            return False
        P, X0 = self.P, self.X0
        body = ~self.prop_mask
        chains = {}
        changed = False
        for q in self.oblique_cands:
            c, d = q["c"], q["d"]
            t_lo, t_hi = q["t"]
            rec = {"from": rnd((c + d * t_lo) * self.H), "to": rnd((c + d * t_hi) * self.H),
                   "length_h": round(t_hi - t_lo, 4), "width_h": round(2 * q["rad"], 4), "rms": round(q["rms"], 4)}
            report.append(rec)
            # the slice chain can stop short of the fist (a fuller, a guard ornament or a decimation
            # fragment widens one cross-section): follow the blade along its own axis while samples
            # stay on the axis and nothing surrounds them
            rel = P - c
            t = rel @ d
            dist = np.linalg.norm(rel - np.outer(t, d), axis=1)
            r_in = max(1.6 * q["rad"], 0.02)
            core = body & (dist < r_in)
            ring = body & (dist >= r_in) & (dist < 0.07)
            per = np.histogram(t[core & (t > t_lo) & (t < t_hi)], bins=max(1, int((t_hi - t_lo) / 0.01)))[0]
            ref = float(np.median(per)) if len(per) else 0.0
            grown = [0.0, 0.0]
            for k_, sgn in ((0, -1.0), (1, 1.0)):
                end = t_lo if sgn < 0 else t_hi
                for _ in range(60):
                    a0, a1 = (end - 0.01, end) if sgn < 0 else (end, end + 0.01)
                    na = int((core & (t >= a0) & (t < a1)).sum())
                    nb = int((ring & (t >= a0) & (t < a1)).sum())
                    if na < 0.4 * ref or nb > 0.25 * max(na, 1):
                        break
                    end += 0.01 * sgn
                if sgn < 0:
                    grown[0], t_lo = round(t_lo - end, 3), end
                else:
                    grown[1], t_hi = round(end - t_hi, 3), end
            if grown[0] or grown[1]:
                q = dict(q, idx=np.unique(np.concatenate([q["idx"], np.nonzero(core & (t > t_lo) & (t < t_hi))[0]])))
                rec.update({"grown_h": grown, "length_h": round(t_hi - t_lo, 4)})
            # params.short_headed_min_len (extract_props): a short haft (a mace or an axe at the hip, 0.17-0.25 H)
            # still counts when a weapon head sits at its far end - checked below
            short_min = self.params.get("short_headed_min_len")
            need_head = False
            if t_hi - t_lo < BLADE_MIN_LEN:
                if not (short_min and t_hi - t_lo >= float(short_min)):
                    rec["reason"] = "short"
                    continue
                need_head = True
                rec["short_headed_candidate"] = True
            mid = c + d * 0.5 * (t_lo + t_hi)
            if mid[1] > max(self.trunk_at(float(mid[2]))["yback"], self.Y0 + 0.05) - 0.02:
                rec["reason"] = "behind the back"
                continue
            own = np.zeros(len(P), dtype=bool)
            own[q["idx"]] = True
            rel = P - c
            t = rel @ d
            dist = np.linalg.norm(rel - np.outer(t, d), axis=1)
            near = np.nonzero(body & ~own & (dist < 0.07) & (t > t_lo - 0.10) & (t < t_hi + 0.10))[0]
            fist, best_n = np.zeros(0, dtype=np.int64), 0
            if len(near):
                tn = t[near]
                for a0 in np.arange(float(tn.min()), float(tn.max()) + 0.005, 0.005):
                    m = (tn >= a0) & (tn < a0 + 0.08)
                    if int(m.sum()) > best_n:
                        best_n, fist = int(m.sum()), near[m]
            rec["fist_samples"] = int(len(fist))
            if len(fist) < BLADE_FIST_PTS:
                rec["reason"] = "no fist on it"
                continue
            G = P[fist].mean(0)
            tg = float(t[fist].mean())
            side = "Left" if G[0] > X0 else "Right"
            Sh = self.shoulder_prior(side)
            t_tip = t_hi if (t_hi - tg) >= (tg - t_lo) else t_lo
            tip = c + d * t_tip
            out = 1.0 if t_tip > tg else -1.0
            # (the first 0.03 H past the end is the tapering point itself)
            beyond = body & ~own & ((t - t_tip) * out > 0.03) & ((t - t_tip) * out < 0.09) & (dist < 0.06)
            rec.update({"side": side, "fist": rnd(G * self.H), "tip_beyond_samples": int(beyond.sum()),
                        "fist_reach_h": round(float(np.linalg.norm(G - Sh)), 4),
                        "tip_reach_h": round(float(np.linalg.norm(tip - Sh)), 4),
                        "fist_to_tip_h": round(abs(t_tip - tg), 4)})
            if G[2] < 0.28:
                rec["reason"] = "fist too low (a leg or a hem, not a hand)"
                continue
            head = np.zeros(0, dtype=np.int64)
            if beyond.sum() > BLADE_TIP_FREE:
                # a weapon head past the shaft (mace flanges, an axe blade, a banner finial): a compact blob within
                # 0.2 H past the end, off the floor, all of it beyond a bare arm's reach - part of the prop
                hm = body & ~own & ((t - t_tip) * out > 0.0) & ((t - t_tip) * out < 0.20) & (dist < 0.12)
                hp = P[hm]
                ok_head = len(hp) > 0 and float(hp[:, 2].min()) > 0.06 and                     float(np.max(np.linalg.norm(hp - hp.mean(0), axis=1))) < 0.16 and                     float(np.min(np.linalg.norm(hp - Sh, axis=1))) > BLADE_REACH - 0.05
                if not ok_head:
                    rec["reason"] = "the far end is not a free tip"
                    continue
                head = np.nonzero(hm)[0]
                rec["head_samples"] = int(len(head))
                far = float(((t[head] - t_tip) * out).max())
                if out > 0:
                    t_hi = t_tip + far
                else:
                    t_lo = t_tip - far
                tip = c + d * (t_tip + out * far)
                rec["tip_reach_h"] = round(float(np.linalg.norm(tip - Sh)), 4)
                rec["fist_to_tip_h"] = round(abs(t_tip + out * far - tg), 4)
            elif need_head:
                rec["reason"] = "short and no weapon head at the far end"
                continue
            if rec["fist_reach_h"] > BLADE_FIST_REACH:
                rec["reason"] = "the fist is out of the arm's reach"
                continue
            if rec["tip_reach_h"] < BLADE_REACH or rec["fist_to_tip_h"] < BLADE_GRIP_TO_TIP:
                rec["reason"] = "within a bare arm's reach (a limb, not a prop)"
                continue
            # the fist is a hand: on a free arm chain that climbs toward the armpit, or (an arm touching a
            # cape or the hip merges with it in the slices) clear of the trunk core - beside it or in front
            if side not in chains:
                chains[side] = self.free_chains(side)
            arm = False
            for ch in chains[side]:
                hit = np.isin(fist, ch["idx"]).mean() if len(ch["idx"]) else 0.0
                if hit >= 0.3 and ch["zmax"] > self.z_st - 0.25:
                    arm = "chain"
                    break
            if not arm:
                tp = self.trunk_at(float(G[2]))
                if abs(float(G[0]) - X0) > max(0.5 * tp["W"], 0.10) or float(G[1]) < tp["yfront"] - 0.03:
                    arm = "clear_of_trunk"
            rec["fist_on"] = arm or None
            if not arm and need_head and self.params.get("belt_items"):
                # extract_props: a short headed weapon whose "fist" is the hip itself hangs at the belt (a mace at
                # the hip the hand rests on): a prop of the Hips, never a grip of the hand
                arm = "belt"
                rec["fist_on"] = "belt"
            if not arm:
                rec["reason"] = "the fist is neither on a free arm chain nor clear of the trunk"
                continue
            on_shaft = t_lo + 0.03 < tg < t_hi - 0.03
            if on_shaft:
                # a thin forearm collinear with the blade can be merged into the chain across the fist:
                # a part on the shoulder side of the fist that stops short of the shoulder, or one clearly
                # thicker than the blade (a forearm behind a mace held straight forward) and no longer than a
                # forearm, is the arm - the prop starts at the fist
                t_sh = t_lo if out > 0 else t_hi
                e_sh = c + d * t_sh
                seg = abs(tg - t_sh)
                near_ax = dist < 0.07

                def r90(a, b_):
                    sel = near_ax & (t > min(a, b_)) & (t < max(a, b_))
                    return float(np.percentile(dist[sel], 90)) if sel.sum() > 10 else 0.0
                r_back = r90(tg - out * 0.05, t_sh)
                r_blade = r90(tg + out * 0.08, t_tip)
                rec["radius_behind_fist_h"], rec["radius_blade_h"] = round(r_back, 4), round(r_blade, 4)
                closer = np.linalg.norm(e_sh - Sh) < np.linalg.norm(G - Sh) and seg < np.linalg.norm(G - Sh) - 0.05
                thicker = r_blade > 0 and r_back > 1.5 * r_blade and seg < 0.25
                if (closer or thicker) and seg > 0.06:
                    if out > 0:
                        t_lo = tg - 0.04
                    else:
                        t_hi = tg + 0.04
                    own &= (t > t_lo - 0.01) & (t < t_hi + 0.01)
                    on_shaft = False
                    rec["forearm_trimmed_h"] = round(seg - 0.04, 4)
            r_cap = max(0.012, 1.8 * q["rad"])
            fm = np.zeros(len(P), dtype=bool)
            fm[fist] = True
            e_lo, e_hi = (0.03, 0.01) if t_tip < tg else (0.01, 0.03)    # the tapering point past the tip end
            m = own | ((dist < r_cap) & (t > t_lo - e_lo) & (t < t_hi + e_hi) & ~fm)
            m[head] = True
            self.prop_mask |= m
            # the straight part is the extent (the fist stays body); the grip window is the fist stretch
            tf = t[fist]
            p = {"c": c, "d": d, "t": [t_lo, t_hi], "extent": [t_lo, t_hi], "rad": q["rad"], "rms": q["rms"], "r_cap": r_cap,
                 "held": True, "side": side, "grip": G, "grip_t": (float(tf.min()), float(tf.max())),
                 "grip_mode": "shaft" if on_shaft else "end", "fixed_grip": True, "oblique": True,
                 "fist_samples": int(len(fist))}
            if arm == "belt":
                p.update({"held": False, "fixed_grip": False, "belt": True, "bind": "Hips", "bind_mode": "belt",
                          "grip_mode": "end"})
            self.props.append(p)
            rec.update({"accepted": True, "grip_mode": p["grip_mode"]})
            changed = True
        if changed:
            self.info["props"] = len(self.props)
        return changed

    def _extend_props(self, mask, props):
        """Grow props at their ends with free slice components (staff head ornaments)."""
        P = self.P
        S = Slices(P, axis=2, step=0.005, thick=0.006, cell=1.0 / 160)
        xmed = float(np.median(P[(P[:, 2] > 0.5) & (P[:, 2] < 0.8) & ~mask, 0]))
        seeds = [c.id for c in S.all if mask[c.idx].mean() > 0.5]
        accepted = set(seeds)
        stack = list(seeds)
        while stack:
            i = stack.pop()
            for j in S.up[i] + S.down[i]:
                if j in accepted:
                    continue
                cj = S.all[j]
                if max(cj.ext(0), cj.ext(1)) > 0.12:
                    continue
                if np.min(np.abs(P[cj.idx, 0] - xmed)) < 0.06:
                    continue
                ok = False
                for p in props:
                    rel = cj.c - p["c"]
                    t = float(rel @ p["d"])
                    dist = float(np.linalg.norm(rel - t * p["d"]))
                    if dist < 0.08 and (t < p["t"][0] + 0.02 or t > p["t"][1] - 0.02):
                        ok = True
                if ok:
                    accepted.add(j)
                    stack.append(j)
        for i in accepted:
            if i not in seeds:
                mask[S.all[i].idx] = True
        return mask

    # ------------------------------------------------------------- midline / top / ground
    def midline(self):
        P = self.Pb
        z = P[:, 2]
        sel = (z > 0.5) & (z < 0.8)
        x0 = float(np.median(P[sel, 0]))
        for _ in range(2):
            x0 = float(np.median(P[sel & (np.abs(P[:, 0] - x0) < 0.15), 0]))
        # the head band is the most reliable symmetry cue: shields, props and sleeves rarely reach it
        col = (np.abs(P[:, 0] - x0) < 0.15) & (z > 0.5)
        if col.sum() > 50:
            top = float(P[col, 2].max())
            hb = col & (z > top - 0.09) & (z < top - 0.02)
            if hb.sum() > 50:
                xh = float(np.percentile(P[hb, 0], 5) + np.percentile(P[hb, 0], 95)) * 0.5
                if abs(xh - x0) < 0.08:
                    x0 = xh
        self.info["midline_head"] = round(x0, 4)
        # mirror-symmetry refinement of the trunk core
        band = sel & (np.abs(P[:, 0] - x0) < 0.10)
        cell = 0.004
        zi = np.floor(P[band, 2] / cell).astype(np.int64)
        best, best_s = -1.0, 0.0
        for s in np.linspace(-0.02, 0.02, 17):
            xi = np.floor((P[band, 0] - x0 - s) / cell).astype(np.int64)
            a = set(zip(xi.tolist(), zi.tolist()))
            m = set(zip((-xi - 1).tolist(), zi.tolist()))
            iou = len(a & m) / max(1, len(a | m))
            if iou > best:
                best, best_s = iou, float(s)
        self.X0 = x0 + best_s
        if "chin" in self.ov and "groin" in self.ov:
            self.X0 = float(self.ov["chin"][0] + self.ov["groin"][0]) * 0.5
        trunk = sel & (np.abs(P[:, 0] - self.X0) < 0.08)
        # extent midpoint, not the median: a slice is a ring and its median falls on the front or
        # the back cluster of points
        self.Y0 = float(np.percentile(P[trunk, 1], 3) + np.percentile(P[trunk, 1], 97)) * 0.5
        self.info["symmetry_iou"] = round(best, 3)

    def head_top(self):
        P = self.Pb
        sel = (np.abs(P[:, 0] - self.X0) < 0.1) & (P[:, 2] > 0.5)
        self.top = float(P[sel, 2].max()) if sel.any() else float(P[:, 2].max())

    def ground(self):
        P = self.Pb
        z = P[:, 2]
        g = z < 0.02
        body = (z > 0.3) & (z < 0.7)
        dens = (g.sum() / 0.02) / max(1.0, body.sum() / 0.4)
        ex = float(np.percentile(P[g, 0], 98) - np.percentile(P[g, 0], 2)) if g.sum() > 10 else 0.0
        ey = float(np.percentile(P[g, 1], 98) - np.percentile(P[g, 1], 2)) if g.sum() > 10 else 0.0
        self.ground_ok = bool(g.sum() >= 50 and dens >= 0.12 and ex >= 0.07 and ey >= 0.045)
        self.info["ground"] = {"samples": int(g.sum()), "density_ratio": round(float(dens), 3),
                               "extent_x": round(ex, 3), "extent_y": round(ey, 3),
                               "ok": self.ground_ok}

    # ------------------------------------------------------------- legs
    def legs(self):
        S, X0 = self.S, self.X0
        kmax = S.k_of(0.66)
        kg = S.k_of(0.025)
        grounded = set()
        for k in range(0, kg + 1):
            grounded |= {c.id for c in S.comps[k]}
        for k in range(kg + 1, kmax + 1):
            for c in S.comps[k]:
                if any(d in grounded for d in S.down[c.id]):
                    grounded.add(c.id)
        self.grounded = grounded
        rows = []
        for k in range(0, kmax + 1):
            cs = [c for c in S.comps[k] if c.id in grounded]
            L = [c for c in cs if c.lo[0] > X0 - 0.006 and c.c[0] - X0 < 0.25 and c.n >= 6]
            R = [c for c in cs if c.hi[0] < X0 + 0.006 and X0 - c.c[0] < 0.25 and c.n >= 6]
            M = [c for c in cs if c.lo[0] <= X0 - 0.006 and c.hi[0] >= X0 + 0.006]
            pinch = 1.0
            if M:
                m = max(M, key=lambda c: c.n)
                pts = S.P[m.idx]

                def depth(x0, w=0.012):
                    s_ = np.abs(pts[:, 0] - x0) < w
                    return float(np.ptp(pts[s_, 1])) if s_.sum() > 3 else 0.0
                side = max([depth(X0 + dx) for dx in (-0.08, -0.06, -0.04, 0.04, 0.06, 0.08)] + [1e-6])
                pinch = depth(X0) / side
            rows.append({"k": k, "z": S.z_of(k), "L": max(L, key=lambda c: c.n) if L else None,
                         "R": max(R, key=lambda c: c.n) if R else None,
                         "M": max(M, key=lambda c: c.n) if M else None,
                         "sep": bool(L and R and not M), "pinch": pinch})
        self.leg_rows = rows
        k0 = S.k_of(0.04)
        k1 = S.k_of(0.25)
        crotch_k, mode = None, None
        sep_frac = float(np.mean([r["sep"] for r in rows[k0:k1]])) if len(rows) > k1 else 0.0
        self.tunic = None
        if sep_frac > 0.6:
            mode = "separate"
            for i in range(k0, len(rows) - 3):
                if not rows[i]["sep"] and not rows[i + 1]["sep"] and not rows[i + 2]["sep"]:
                    crotch_k = i
                    break
            # a coat/tunic hem: above the first merge the legs stay joined only by a thin web
            # (pinched section) and then the section jumps to a full ring within 0.01 H. A real
            # crotch fills in gradually (pinch 0.4 -> 0.7 -> 0.9 over 0.03 H). The crotch is then
            # hidden: proportion prior (the open skirt keeps bone-heat weights; robe blending made
            # tabards and coats tear in the stride).
            if crotch_k is not None:
                i_full = next((i for i in range(crotch_k, min(len(rows), crotch_k + 40))
                               if rows[i]["M"] is not None and rows[i]["pinch"] >= 0.8), None)
                if i_full is not None and i_full - crotch_k >= 4:
                    pinched = sum(1 for i in range(crotch_k, i_full)
                                  if rows[i]["M"] is not None and rows[i]["pinch"] < 0.3)
                    lowp = max(rows[i]["pinch"] for i in (i_full - 1, i_full - 2))
                    if pinched >= 4 and lowp <= 0.25:
                        self.tunic = {"merge_z": round(rows[crotch_k]["z"], 4),
                                      "hem_z": round(rows[i_full]["z"], 4)}
        else:
            pinched = float(np.mean([r["M"] is not None and r["pinch"] < 0.5 for r in rows[k0:k1]])) \
                if len(rows) > k1 else 0.0
            if pinched > 0.6:
                mode = "together"
                for i in range(k0, len(rows) - 3):
                    if all(rows[j]["M"] is not None and rows[j]["pinch"] > 0.65 for j in (i, i + 1, i + 2)):
                        crotch_k = i
                        break
            else:
                mode = "robe"
        robe_param = self.params.get("robe", "auto")
        self.robe = (mode == "robe") if robe_param in ("auto", None) else bool(robe_param)
        self.leg_mode = mode
        if crotch_k is not None and self.tunic is not None:
            self.crotch = max(self.tunic["hem_z"], PRIOR["crotch"])
            self.set_conf("groin", 0.4, "prior")
            msg = ("legs merge under a coat/tunic hem at %.2f H; the crotch is hidden and placed by "
                   "proportion (%.2f H)" % (self.tunic["hem_z"], self.crotch))
            if msg not in self.warn:
                self.warn.append(msg)
        elif crotch_k is not None:
            crotch = rows[crotch_k]["z"]
            ok = 0.3 < crotch < 0.62
            self.crotch = crotch if ok else PRIOR["crotch"]
            self.set_conf("groin", 0.9 if (ok and mode == "separate") else (0.7 if ok else 0.3),
                          "auto" if ok else "prior")
        else:
            self.crotch = PRIOR["crotch"]
            self.set_conf("groin", 0.35 if mode == "robe" else 0.2, "prior")
        if "groin" in self.ov:
            self.crotch = float(self.ov["groin"][-1])
            self.set_conf("groin", 1.0, "user")
        # leg tracks below the crotch
        self.tracks = {"Left": [], "Right": []}
        if mode in ("separate", "together"):
            for r in rows:
                if r["z"] >= self.crotch - 0.01:
                    break
                for side, key in (("Left", "L"), ("Right", "R")):
                    c = r[key]
                    if c is not None and mode == "separate":
                        pts = S.P[c.idx]
                    elif r["M"] is not None:
                        pts = S.P[r["M"].idx]
                        pts = pts[(pts[:, 0] > X0) if side == "Left" else (pts[:, 0] < X0)]
                    else:
                        continue
                    if len(pts) < 5:
                        continue
                    self.tracks[side].append((r["z"], pts.mean(0), float(np.ptp(pts[:, 0])),
                                              float(np.ptp(pts[:, 1])), pts))
        self.info["legs"] = {"mode": mode, "robe": self.robe, "crotch": round(self.crotch, 4),
                             "sep_fraction": round(sep_frac, 3), "tunic": self.tunic}

    # ------------------------------------------------------------- trunk, neck, shoulders
    def trunk(self):
        S, X0 = self.S, self.X0
        prof = []
        for k in range(S.k_of(0.3), S.k_of(self.top) + 1):
            core = [c for c in S.comps[k] if np.min(np.abs(S.P[c.idx, 0] - X0)) < 0.03]
            if not core:
                continue
            pts = np.concatenate([S.P[c.idx] for c in core])
            col = pts[np.abs(pts[:, 0] - X0) < 0.12]
            if len(col) < 5:
                continue
            W = float(np.percentile(col[:, 0], 98) - np.percentile(col[:, 0], 2))
            D = float(np.percentile(col[:, 1], 98) - np.percentile(col[:, 1], 2))
            wide = pts[np.abs(pts[:, 0] - X0) < 0.3]
            hw = float(np.percentile(np.abs(wide[:, 0] - X0), 99))
            yc = col[np.abs(col[:, 0] - X0) < 0.05]
            ymid = (float(np.percentile(yc[:, 1], 3) + np.percentile(yc[:, 1], 97)) * 0.5
                    if len(yc) > 3 else self.Y0)
            prof.append({"z": S.z_of(k), "W": W, "D": D, "A": W * D, "hw": hw, "ymid": ymid,
                         "yfront": float(np.percentile(col[:, 1], 2)),
                         "yback": float(np.percentile(col[:, 1], 98))})
        if len(prof) < 20:
            raise RuntimeError("no trunk found along the midline (is this a single standing figure?)")
        self.prof = pr = prof
        zs = np.array([p["z"] for p in pr])
        A = np.array([p["A"] for p in pr])
        A_s = np.array([np.median(A[max(0, i - 1):i + 2]) for i in range(len(A))])
        lo_z, hi_z = self.top - 0.26, self.top - 0.06
        rng = np.nonzero((zs > lo_z) & (zs < hi_z))[0]
        neck_ok = False
        neck_z = self.top - PRIOR["neck_below_top"]
        if len(rng):
            i = rng[np.argmin(A_s[rng])]
            cand = float(zs[i])
            head = A_s[(zs > cand) & (zs < self.top - 0.02)]
            below = A_s[(zs < cand - 0.05) & (zs > cand - 0.09)]
            neck_ok = bool(len(head) and A_s[i] < 0.75 * head.max() and len(below)
                           and below.max() > 1.5 * A_s[i] and 0.07 < self.top - cand < 0.26)
            if neck_ok:
                neck_z = cand
        self.neck_z = neck_z
        head_z = min(neck_z + PRIOR["head_above_neck"], self.top - 0.07)
        self.set_conf("chin", 0.9 if neck_ok else 0.3, "auto" if neck_ok else "prior")
        if "chin" in self.ov:     # Mixamo-style chin marker: on the chin, below the Head joint
            head_z = float(self.ov["chin"][-1]) + PRIOR["chin_below_head"]
            self.neck_z = neck_z = head_z - PRIOR["head_above_neck"]
            self.set_conf("chin", 1.0, "user")
        self.head_z = head_z
        self.neckbase_z = neck_z - PRIOR["neckbase_below_neck"]
        # shoulder line: first slice below the neck where the core gets much wider
        Wn = float(np.interp(neck_z, zs, [p["W"] for p in pr]))
        z_st = None
        for p in sorted(pr, key=lambda p: -p["z"]):
            if p["z"] < neck_z and p["W"] > max(2.2 * Wn, 0.16):
                z_st = p["z"]
                break
        if z_st is None or not (neck_z - 0.12 < z_st < neck_z - 0.01):
            z_st = neck_z - PRIOR["shoulder_below_neck"] + 0.03
        self.z_st = z_st
        # shoulder line for T-poses (the arm is horizontal at the top of the shoulders)
        self.shoulder_z = float(np.clip(z_st - 0.03, neck_z - 0.11, neck_z - 0.04))
        # acromion half width from the slices at the top of the shoulders
        hws = [p["hw"] for p in pr if z_st - 0.035 < p["z"] < z_st + 0.005]
        hw = float(np.median(hws)) if hws else PRIOR["shoulder_x"] / 0.78
        self.t_pose_hint = hw > 0.2
        if self.t_pose_hint:
            chest = [p["hw"] for p in pr if z_st - 0.16 < p["z"] < z_st - 0.10]
            hw = min(float(np.median(chest)) if chest else 0.13, 0.17)
        self.acromion_hw = hw
        self.shoulder_x = float(np.clip(0.78 * hw, 0.07, 0.14))
        # prior shoulder joint for hanging / A-pose arms when the arm itself is not visible: Mixamo
        # puts the Arm head about 0.11-0.13 H below the narrowest neck, inside the deltoid
        self.shoulder_z_prior = float(np.clip(z_st - 0.06, neck_z - 0.15, neck_z - 0.06))
        self.shoulder_x_prior = float(np.clip(0.85 * hw, 0.09, 0.15))
        self.info["trunk"] = {"neck_z": round(neck_z, 4), "neck_ok": neck_ok,
                              "shoulder_top": round(z_st, 4), "acromion_hw": round(hw, 4),
                              "t_pose_hint": bool(self.t_pose_hint)}

    def trunk_at(self, z):
        zs = np.array([p["z"] for p in self.prof])
        return self.prof[int(np.argmin(np.abs(zs - z)))]

    def spine_y(self, z):
        p = self.trunk_at(z)
        return p["ymid"] + 0.1 * (p["yback"] - p["yfront"])

    # ------------------------------------------------------------- curved held props (bows)
    def find_bows(self):
        """Curved props held in one hand: a bow (limb + string) or a curved blade.

        Thin slice components (widest cross-section < BOW_THIN) beside the body (centre |x - X0| >=
        BOW_SIDE, no point within BOW_TRUNK_GAP of the midline) and in front of the back (a quiver or
        a cape behind the trunk is never taken) are chained across neighbouring levels. Per side, the
        chains that share one plane form the candidate (the fist on the grip splits a bow into an
        upper and a lower limb; chains off the plane are dropped one by one). It is a held curved
        prop when it is long (vertical extent >= BOW_MIN_EXTENT), the plane is roughly vertical and
        holds the centroids (rms <= BOW_PLANE_RMS), it is curved (in-plane straight-line rms of the
        main cross-sections > BOW_CURVE_RMS, or limb and string side by side on >= 30 % of the
        levels) and a fist of body mass touches it between its ends. Its samples join the prop mask,
        so the wrist is found on the arm and not on the limb; the gripping hand gets a shaft-like grip
        along the chord (the arm is solved like a hand on a staff) and the arm-symmetry check knows
        the hand holds a prop. Returns True if the prop mask changed."""
        S, P, X0 = self.S, self.P, self.X0
        self.bow_props = []
        cand = []
        for c in S.all:
            if c.n < 3 or max(c.ext(S.ab[0]), c.ext(S.ab[1])) > BOW_THIN:
                continue
            if abs(float(c.c[0] - X0)) < BOW_SIDE or float(np.min(np.abs(P[c.idx, 0] - X0))) < BOW_TRUNK_GAP:
                continue
            # behind the back (quiver, cape): below the trunk profile (legs, coat hem) the profile is
            # not a back, so the trunk centre + 0.05 H is the least depth taken as "behind"
            if c.c[1] > max(self.trunk_at(float(c.c[2]))["yback"], self.Y0 + 0.05) - 0.02:
                continue
            cand.append(c.id)
        cset = set(cand)
        links = {i: [j for j in S.up[i] if j in cset] for i in cand}
        groups = []
        for g in _uf_groups(cand, links):
            ks = [S.all[i].k for i in g]
            if (max(ks) - min(ks) + 1) * S.step < 0.06:
                continue
            xs = np.array([S.all[i].c[0] - X0 for i in g])
            if (xs > 0).any() and (xs < 0).any():
                continue
            groups.append(g)
        report = []
        changed = False
        for side in SIDES:
            s = self.side_sign(side)
            gs = [g for g in groups if (S.all[g[0]].c[0] - X0) * s > 0]
            if not gs:
                continue
            # one plane through the chains: drop the worst-fitting chain until the rest agree
            while len(gs) > 1:
                cents = np.array([S.all[i].c for g in gs for i in g])
                c0 = cents.mean(0)
                n = np.linalg.svd(cents - c0, full_matrices=False)[2][2]
                per = [float(np.median(np.abs((np.array([S.all[i].c for i in g]) - c0) @ n))) for g in gs]
                if max(per) <= BOW_PLANE_RMS:
                    break
                gs.pop(int(np.argmax(per)))
            ids = [i for g in gs for i in g]
            cents = np.array([S.all[i].c for i in ids])
            zlo, zhi = float(cents[:, 2].min()), float(cents[:, 2].max())
            rec = {"side": side, "chains": len(gs), "components": len(ids), "extent": round(zhi - zlo, 4)}
            report.append(rec)
            if zhi - zlo < BOW_MIN_EXTENT:
                rec["reason"] = "short"
                continue
            c0 = cents.mean(0)
            n = np.linalg.svd(cents - c0, full_matrices=False)[2][2]
            plane_rms = float(np.sqrt(np.mean(((cents - c0) @ n) ** 2)))
            rec.update({"plane_rms": round(plane_rms, 4), "plane_normal": rnd(n, 3)})
            if plane_rms > BOW_PLANE_RMS or abs(float(n[2])) > BOW_PLANE_NZ:
                rec["reason"] = "not one vertical plane"
                continue
            # the main cross-section of every level (the limb), and levels with limb + string
            main, per_level = {}, {}
            for i in ids:
                ci = S.all[i]
                per_level.setdefault(ci.k, []).append(ci)
                if ci.k not in main or ci.n > main[ci.k].n:
                    main[ci.k] = ci
            two = sum(1 for k, cs in per_level.items()
                      if any(float(np.linalg.norm((q.c - main[k].c)[S.ab])) > 0.015 for q in cs))
            two_frac = two / max(len(per_level), 1)
            mc = np.array([main[k].c for k in sorted(main)])
            _, dl, _, resid = _line_fit(mc)
            line_rms = float(np.sqrt(np.mean(resid ** 2)))
            rec.update({"line_rms": round(line_rms, 4), "limb_string_levels": round(two_frac, 3)})
            if line_rms <= BOW_CURVE_RMS and two_frac < 0.3:
                rec["reason"] = "straight"
                continue
            # held: a fist of body mass right next to the curve - below the upper arm (a limb passing
            # the shoulder is not a grip), above the knees, away from the trunk. The densest 0.08 H
            # stretch along the curve is the fist (a hand on the riser, or at the end of the one limb
            # that could be separated from a leg)
            body = ~self.prop_mask
            body[np.concatenate([S.all[i].idx for i in ids])] = False
            near = body & (P[:, 2] > max(zlo - 0.03, 0.25)) & (P[:, 2] < min(zhi + 0.03, self.z_st - 0.12)) & \
                (np.abs(P[:, 0] - X0) > BOW_TRUNK_GAP)
            cand_pts = np.nonzero(near)[0]
            seg_len = np.linalg.norm(np.diff(mc, axis=0), axis=1)
            arc0 = np.concatenate([[0.0], np.cumsum(seg_len)])
            dmin = np.full(len(cand_pts), np.inf)
            arc = np.zeros(len(cand_pts))
            for si, (a_, b_) in enumerate(zip(mc[:-1], mc[1:])):
                dd, tt = _seg_dist(P[cand_pts], a_, b_)
                better = dd < dmin
                dmin = np.where(better, dd, dmin)
                arc = np.where(better, arc0[si] + tt * seg_len[si], arc)
            close = dmin < 0.05
            fist, best_n = np.zeros(0, dtype=np.int64), 0
            if close.any():
                ac = arc[close]
                for a0 in np.arange(float(ac.min()), float(ac.max()) + 0.01, 0.01):
                    m = (ac >= a0) & (ac < a0 + 0.08)
                    if int(m.sum()) > best_n:
                        best_n, fist = int(m.sum()), cand_pts[close][m]
            rec["fist_samples"] = int(len(fist))
            if len(fist) < BOW_FIST_PTS:
                rec["reason"] = "not held (no fist on it)"
                continue
            G = P[fist].mean(0)
            reach = float(np.linalg.norm(G - self.shoulder_prior(side)))
            rec["grip_reach"] = round(reach, 4)
            if reach > 0.42:
                rec["reason"] = "the fist is out of the arm's reach"
                continue
            lo_tip, hi_tip = mc[0], mc[-1]
            d = hi_tip - lo_tip
            d = d / max(float(np.linalg.norm(d)), 1e-9)
            frac = float((G[2] - zlo) / max(zhi - zlo, 1e-9))
            # a string beside the limb makes it a bow even when only one limb could be separated
            # from the leg (the grip then sits at that limb's end)
            kind = "bow" if (0.2 < frac < 0.8 or two_frac >= 0.3) else "curved_blade"
            far = hi_tip if frac < 0.5 else lo_tip
            grip_dir = d if kind == "bow" else (far - G) / max(float(np.linalg.norm(far - G)), 1e-9)
            sag = float(np.max(np.linalg.norm(np.cross(mc - lo_tip, d), axis=1)))
            idx = np.unique(np.concatenate([S.all[i].idx for i in ids]))
            self.prop_mask[idx] = True
            changed = True
            rec.update({"kind": kind, "grip_frac": round(frac, 3), "sagitta": round(sag, 4)})
            self.bow_props.append({
                "kind": kind, "side": side, "idx": idx, "c": c0, "d": d, "normal": n,
                "tips": [lo_tip, hi_tip], "grip": G, "grip_dir": grip_dir, "grip_frac": frac,
                "grip_mode": kind, "held": True, "bind": side + "Hand", "bind_mode": "grip_" + kind,
                "fist_samples": int(len(fist)), "curve": mc, "length": float(np.linalg.norm(hi_tip - lo_tip)),
                "sagitta": sag, "plane_rms": plane_rms, "line_rms": line_rms, "limb_string_levels": two_frac})
        self.info["bows"] = report
        return changed

    # ------------------------------------------------------------- plates (shields)
    def find_plates(self):
        """Plate-sized free chains (a shield touching the arm) -> RANSAC plane -> the disc
        becomes a rigid prop of that side's hand. Returns True if the prop mask changed."""
        P = self.P
        rng = np.random.default_rng(3)
        self.plate_props = []
        changed = False
        for side in SIDES:
            for c in self.free_chains(side):
                sizes = np.array([max(l_[3], l_[4]) for l_ in c["line"]])
                if np.percentile(sizes, 70) <= 0.11:
                    continue
                idx = c["idx"]
                Q = P[idx]
                n = len(Q)
                tri = rng.integers(0, n, size=(400, 3))
                a, b_, cc = Q[tri[:, 0]], Q[tri[:, 1]], Q[tri[:, 2]]
                nrm = np.cross(b_ - a, cc - a)
                ln = np.linalg.norm(nrm, axis=1)
                ok = ln > 1e-6
                nrm, a = nrm[ok] / ln[ok, None], a[ok]
                sub = Q[rng.integers(0, n, size=min(n, 3000))]
                dist = np.abs(np.einsum("kij,kj->ki", sub[None, :, :] - a[:, None, :], nrm))
                cnt = (dist < 0.015).sum(1)
                best = int(np.argmax(cnt))
                nb, ab = nrm[best], a[best]
                d_all = np.abs((Q - ab) @ nb)
                inl = d_all < 0.015
                if inl.sum() < 0.3 * n:
                    continue
                cen = Q[inl].mean(0)
                rel = Q[inl] - cen
                rel -= np.outer(rel @ nb, nb)
                R95 = float(np.percentile(np.linalg.norm(rel, axis=1), 95))
                _, sv, _ = np.linalg.svd(rel, full_matrices=False)
                s1, s2 = sv[0] / math.sqrt(len(rel)), sv[1] / math.sqrt(len(rel))
                # a disc: comparable in-plane spreads (an inclined arm is long and narrow) and a
                # radius of a buckler or larger (uniform disc: std = R / 2)
                if s1 / max(s2, 1e-9) > 2.2 or 2.0 * s2 < 0.075:
                    continue
                # a held shield hangs at the side or in front of the body below the shoulders;
                # wings and capes are planar too but sit behind the back or rise above the shoulders
                tp = self.trunk_at(float(cen[2]))
                if cen[1] > tp["yback"] - 0.02 or cen[2] > self.z_st + 0.02:
                    self.info.setdefault("plates_rejected", []).append(
                        {"side": side, "centre": rnd(cen), "radius": round(R95, 3),
                         "reason": "behind the back or above the shoulders (wing/cape)"})
                    continue
                # a shield held at the side faces outward, one held in front is in front of the trunk; a flat
                # panel in the frontal plane beside the body at trunk depth is a cape/tabard flap (generated bone
                # swordsman: such a flap rigid to the hand tore at the shoulder on every clip)
                if abs(float(nb[1])) > 0.8 and cen[1] > tp["yfront"] + 0.03 and self._plate_prop(cen) is None:
                    self.info.setdefault("plates_rejected", []).append(
                        {"side": side, "centre": rnd(cen), "radius": round(R95, 3), "normal": rnd(nb, 3),
                         "reason": "frontal-plane flap beside the body (cape/tabard), not a shield"})
                    continue
                relq = Q - cen
                inplane = np.linalg.norm(relq - np.outer(relq @ nb, nb), axis=1)
                m = (d_all < 0.06) & (inplane < R95 * 1.08)
                self.prop_mask[idx[m]] = True
                self.plate_props.append({"side": side, "centre": cen, "normal": nb,
                                         "radius": R95, "points": int(m.sum())})
                changed = True
        self.info["plates"] = len(self.plate_props)
        return changed

    # ------------------------------------------------------------- grips
    def grips(self):
        """Grip = a stretch of the prop axis where body mass sticks to the shaft away from the
        midline. Along the straight shaft any such mass counts; just beyond the ends of the straight
        part (a hand closed on a sword hilt above the crossguard, a mace head below the fist) only
        mass that belongs to a free arm chain ending there (the hand end of the arm) counts."""
        self.grip = {}
        # a hand on a bow (find_bows) holds it like a staff: shaft-like grip along the bow's chord
        for bw in self.bow_props:
            self.grip[bw["side"]] = {"G": bw["grip"], "mass": float(bw["fist_samples"]), "prop": bw, "bow": True}
        # known grips (extract_props cut the prop out; its placement socket says where the fist held it): the arm
        # is solved as on that prop - without them a fist raised forward empty-handed is traced into a book at the
        # hip or a cape hem
        for kg in self.params.get("known_grips") or []:
            sd = kg.get("side")
            if sd in SIDES and sd not in self.grip:
                d = np.asarray(kg["d"], dtype=np.float64)
                self.grip[sd] = {"G": np.asarray(kg["G"], dtype=np.float64), "mass": 1e9, "known": True,
                                 "prop": {"d": d / max(np.linalg.norm(d), 1e-9),
                                          "grip_mode": kg.get("grip_mode", "shaft")}}
        # an oblique prop of find_blades already knows its fist
        for p in self.props:
            if p.get("fixed_grip"):
                g = self.grip.get(p["side"])
                if g is None or (not g.get("bow") and p["fist_samples"] > g["mass"]):
                    self.grip[p["side"]] = {"G": p["grip"], "mass": float(p["fist_samples"]), "prop": p}
        if not self.props:
            return
        P, X0 = self.P, self.X0
        body = ~self.prop_mask
        chains = {side: None for side in SIDES}
        idx_of = np.full(len(P), -1, dtype=np.int64)
        for p in self.props:
            if p.get("fixed_grip") or p.get("belt"):
                continue
            p["held"] = False
            rel = P - p["c"]
            t = rel @ p["d"]
            dist = np.linalg.norm(rel - np.outer(t, p["d"]), axis=1)
            # window: the straight part plus 0.10 H beyond the full extent (a fist grown into the
            # prop with the hilt ends the extent; the forearm touches just beyond it)
            ex_ = p.get("extent", p["t"])
            w0, w1 = min(p["t"][0], ex_[0]) - 0.10, max(p["t"][1], ex_[1]) + 0.10
            on_shaft = (t > p["t"][0]) & (t < p["t"][1])
            # beyond the straight part a forearm leaving the fist at an angle is a little further
            # from the axis: wider radius there (the arm-chain test below guards it)
            near = body & (t > w0) & (t < w1) & (((dist < 0.05) & on_shaft) | ((dist < 0.08) & ~on_shaft))
            ts = np.arange(w0, w1, 0.005)
            if len(ts) < 3:
                continue
            hist = np.histogram(t[near], bins=np.append(ts, ts[-1] + 0.005))[0].astype(float)
            inside = (ts > p["t"][0]) & (ts < p["t"][1])
            thr = max(8.0, 2.5 * float(np.median(hist[inside]) if inside.any() else 0.0))
            runs, cur = [], []
            for i, flag in enumerate(hist > thr):
                if flag:
                    cur.append(i)
                elif cur:
                    runs.append(cur)
                    cur = []
            if cur:
                runs.append(cur)
            best = None
            for r in runs:
                t0, t1 = ts[r[0]], ts[r[-1]] + 0.005
                tm = 0.5 * (t0 + t1)
                q = p["c"] + p["d"] * tm
                if q[2] < 0.22 or abs(q[0] - X0) < 0.06:
                    continue
                sel = near & (t >= t0) & (t <= t1)
                G = P[sel].mean(0)
                end_grip = not (p["t"][0] + 0.01 < tm < p["t"][1] - 0.01)
                if end_grip:
                    side = "Left" if G[0] > X0 else "Right"
                    if chains[side] is None:
                        chains[side] = self.free_chains(side)
                    upper = tm > p["t"][1]
                    t_c = t0 if upper else t1            # contact boundary nearest the shaft end
                    zc = float(p["c"][2] + p["d"][2] * t_c)
                    ok = False
                    for ci, ch in enumerate(chains[side]):
                        idx_of[:] = -1
                        idx_of[ch["idx"]] = ci
                        frac = float((idx_of[np.nonzero(sel)[0]] == ci).mean())
                        # the arm chain ends at the contact (hand end) and continues away from it
                        if frac >= 0.5 and ch["zmin"] > zc - 0.06 and ch["zmax"] > zc + 0.05:
                            ok = True
                            break
                    if not ok:
                        continue
                    # the fist itself was grown into the prop (hilt + fist between the crossguard
                    # and the forearm): the grip is half way from the end of the straight part to
                    # the arm contact, a little toward the body side of the axis
                    t_end = p["t"][1] if upper else p["t"][0]
                    if abs(t_c - t_end) > 0.04:
                        t_g = 0.5 * (t_end + t_c)
                        q_run = p["c"] + p["d"] * float((G - p["c"]) @ p["d"])
                        off = G - q_run
                        off = off * min(1.0, 0.03 / max(float(np.linalg.norm(off)), 1e-9))
                        G = p["c"] + p["d"] * t_g + off
                    else:
                        # the fist is body mass right at the end of the blade: its first 0.05 H
                        fist = sel & (np.abs(t - t_c) < 0.05)
                        if fist.sum() >= 5:
                            G = P[fist].mean(0)
                mass = float(hist[r].sum())
                # a grip on the straight shaft wins over one beyond its ends (larger mass first);
                # beyond the ends the fist sits next to the crossguard, the forearm passing a
                # pommel is further away: nearest to the end of the straight part first
                if end_grip:
                    rank = (0, -min(abs(tm - p["t"][0]), abs(tm - p["t"][1])))
                else:
                    rank = (1, mass)
                if best is None or rank > best[4]:
                    best = (mass, G, (float(t0), float(t1)), end_grip, rank)
            if best is None:
                continue
            G = best[1]
            side = "Left" if G[0] > X0 else "Right"
            p["held"], p["side"], p["grip"], p["grip_t"] = True, side, G, best[2]
            p["grip_mode"] = "end" if best[3] else "shaft"
            if side not in self.grip or (not self.grip[side].get("bow") and best[0] > self.grip[side]["mass"]):
                self.grip[side] = {"G": G, "mass": best[0], "prop": p}

    # ------------------------------------------------------------- arms
    def free_chains(self, side):
        S, X0 = self.S, self.X0
        s = self.side_sign(side)
        ids = []
        for k in range(S.k_of(0.22), S.k_of(self.z_st - 0.015) + 1):
            for c in S.comps[k]:
                if c.id in self.grounded:
                    continue
                d = (S.P[c.idx, 0] - X0) * s
                if d.min() < 0.04:
                    continue
                ids.append(c.id)
        iset = set(ids)
        links = {i: [j for j in S.up[i] if j in iset] for i in ids}
        chains = []
        for g in _uf_groups(ids, links):
            ks = sorted({S.all[i].k for i in g})
            if (ks[-1] - ks[0] + 1) * S.step < 0.03:
                continue
            cidx = np.concatenate([S.all[i].idx for i in g])
            pts = S.P[cidx]
            cl = []
            for k in ks:
                cc = [S.all[i] for i in g if S.all[i].k == k]
                pp = np.concatenate([S.P[c.idx] for c in cc])
                cl.append((S.z_of(k), pp.mean(0), float(np.ptp(pp[:, 0]) * np.ptp(pp[:, 1])) + 1e-6,
                           float(np.ptp(pp[:, 0])), float(np.ptp(pp[:, 1]))))
            chains.append({"ids": g, "idx": cidx, "pts": pts, "zmin": float(pts[:, 2].min()),
                           "zmax": float(pts[:, 2].max()), "line": cl})
        return chains

    def tpose_chain(self, side):
        """T-pose arm: slice along X outward from the shoulder; follow the component nearest to
        the previous centre."""
        X0, s = self.X0, self.side_sign(side)
        P = self.Pb
        sel = ((P[:, 0] - X0) * s > 0.10) & (P[:, 2] > self.shoulder_z - 0.25) & \
            (P[:, 2] < self.shoulder_z + 0.12)
        idx = np.nonzero(sel)[0]
        if len(idx) < 50:
            return None
        Q = P[idx]
        xs = (Q[:, 0] - X0) * s
        cur = np.array([self.Y0, self.shoulder_z])
        line = []
        for x in np.arange(0.10, 0.75, 0.006):
            sl = np.nonzero((xs >= x) & (xs < x + 0.007))[0]
            if len(sl) < 3:
                break
            comps = components_2d(Q, sl, [1, 2], 1.0 / 160, 0)
            if not comps:
                break
            c = min(comps, key=lambda c: np.linalg.norm(c.c[[1, 2]] - cur))
            if np.linalg.norm(c.c[[1, 2]] - cur) > 0.06:
                break
            cur = c.c[[1, 2]]
            pts = Q[c.idx]
            line.append((x, pts.mean(0), float(np.ptp(pts[:, 1]) * np.ptp(pts[:, 2])) + 1e-6,
                         float(np.ptp(pts[:, 1])), float(np.ptp(pts[:, 2])), pts))
        if len(line) < 10:
            return None
        tip_pts = line[-1][5]
        tip = tip_pts[np.argmax((tip_pts[:, 0] - X0) * s)]
        return {"line": [l_[:5] for l_ in line], "tip": tip}

    def solve_elbow_ik(self, S_, W_, side, L1=None, L2=None):
        """Elbow from shoulder/wrist and length priors; the bend points back/down/out."""
        L1 = L1 or PRIOR["upper_arm"]
        L2 = L2 or PRIOR["forearm"]
        d = W_ - S_
        dist = float(np.linalg.norm(d))
        u = d / max(dist, 1e-9)
        if dist >= (L1 + L2) * 0.999:
            return S_ + u * dist * L1 / (L1 + L2)
        pref = np.array([0.3 * self.side_sign(side), 0.45, -1.0])
        n = pref - (pref @ u) * u
        if np.linalg.norm(n) < 1e-6:
            n = np.array([0.0, 1.0, 0.0]) - u[1] * u
        n /= np.linalg.norm(n)
        ca = (L1 * L1 + dist * dist - L2 * L2) / (2 * L1 * dist)
        a = math.acos(float(np.clip(ca, -1, 1)))
        return S_ + L1 * (math.cos(a) * u + math.sin(a) * n)

    def shoulder_prior(self, side):
        s = self.side_sign(side)
        z = self.shoulder_z_prior
        return np.array([self.X0 + s * self.shoulder_x_prior, self.spine_y(z) - 0.005, z])

    def _shoulder_from_chain(self, ch, side):
        """Shoulder joint (Arm head) from the visible arm, Mixamo-style. Height: the armpit (top of
        the free arm chain, where the arm separates from the trunk) plus an offset that grows as the
        arm hangs closer to the body (0.035 H at >= 43 deg abduction ... 0.11 H for a hanging arm;
        calibrated on a zombie reference rig, the Human Base Mesh joints, the Mixamo auto-rig of an
        archer model and the procedural mannequin). Side: the lateral extreme of the shoulder at that
        height minus the arm radius (at the joint height a horizontal slab cuts the deltoid cap, a
        ball around the joint), clamped to 0.09..0.15 H. Returns (joint | None, info)."""
        s, X0 = self.side_sign(side), self.X0
        ztop = float(ch["zmax"])
        info = {"armpit_z": round(ztop, 4)}
        if not (self.neck_z - 0.25 < ztop < self.neck_z - 0.04):
            info["reason"] = "chain top is not an armpit (sleeve or prop hides the upper arm)"
            return None, info
        line = ch["line"]
        pts = np.array([l_[1] for l_ in line])
        sel = (pts[:, 2] > ztop - 0.11) & (pts[:, 2] < ztop - 0.012)
        if sel.sum() < 5:
            info["reason"] = "upper arm too short in the chain"
            return None, info
        c, d, _, _ = _line_fit(pts[sel])
        theta = math.degrees(math.acos(float(np.clip(d[2], -1.0, 1.0))))
        info["upper_arm_deg_from_vertical"] = round(theta, 1)
        if theta > 75:
            info["reason"] = "near-horizontal arm"
            return None, info
        off = float(np.clip(0.045 + 0.0035 * (40.0 - theta), 0.035, 0.11))
        z = float(np.clip(ztop + off, self.neck_z - 0.17, self.neck_z - 0.06))
        rad = float(np.median([0.5 * min(l_[3], l_[4]) for l_, m in zip(line, sel) if m]))
        rad = float(np.clip(rad, 0.012, 0.05))
        P = self.Pb
        slab = (np.abs(P[:, 2] - z) < 0.008) & ((P[:, 0] - X0) * s > 0.03) & \
            (np.abs(P[:, 1] - c[1]) < 0.12)
        x_axis = float((c[0] + d[0] * (z - c[2]) / max(d[2], 1e-6) - X0) * s)
        if slab.sum() >= 10:
            # at the joint height the slab cuts the deltoid cap around the joint: joint = extreme - r
            x_ext = float(np.percentile((P[slab, 0] - X0) * s, 99))
            dx = x_ext - rad
            info["lateral_extreme"] = round(x_ext, 4)
        else:
            dx = x_axis
        dx = float(np.clip(dx, 0.09, 0.15))
        y = self.mid_y(X0 + s * dx, z, r=0.025)
        info.update({"offset": round(off, 4), "arm_radius": round(rad, 4), "x": round(dx, 4),
                     "z": round(z, 4), "x_axis_line": round(x_axis, 4)})
        return np.array([X0 + s * dx, y, z]), info

    def _plate_prop(self, centre, near=0.20):
        """The straight prop whose axis (over its extent) passes within `near` of a plate centre: a banner or a
        standard hanging from its pole, a finial on a staff. None if no prop is that close."""
        best = None
        for p in self.props:
            if p.get("oblique") or abs(float(p["d"][2])) < 0.6:
                continue                       # poles and staffs stand upright; a blade carries no banner
            ex = p.get("extent", p["t"])
            a, b_ = p["c"] + p["d"] * ex[0], p["c"] + p["d"] * ex[1]
            dd, tt = _seg_dist(np.asarray(centre, dtype=np.float64)[None, :], a, b_)
            dd = float(dd[0])
            z_on = float((a + (b_ - a) * float(tt[0]))[2])
            if z_on < 0.5 * (a[2] + b_[2]):
                continue                       # a banner hangs from the upper half of its pole
            if dd < near and (best is None or dd < best[0]):
                best = (dd, p)
        return None if best is None else best[1]

    def _plate_of(self, side):
        pl = [p for p in getattr(self, "plate_props", []) if p["side"] == side]
        return max(pl, key=lambda p: p["points"]) if pl else None

    def arms(self):
        self.arm = {}
        self.info["shoulders"] = {}
        X0 = self.X0
        poses = []
        for side in SIDES:
            s = self.side_sign(side)
            key = side[0].lower()
            Sj = self.shoulder_prior(side)
            res = {"S": Sj, "mode": None, "S_src": "prior"}
            grip = self.grip.get(side)
            chains = self.free_chains(side)   # shields were removed by find_plates
            line, tip, arm_chain = None, None, None
            hanging = [c for c in chains if c["zmax"] > self.shoulder_z - 0.2]
            if grip is not None:
                G = grip["G"]
                res["mode"] = "grip"
                above = [c for c in chains if c["zmin"] > G[2] - 0.03]
                if above:
                    ch = min(above, key=lambda c: np.linalg.norm(c["line"][0][1] - G))
                    line = ch["line"]
                # the arm chain that reaches the grip (hand on a hilt: the chain contains the fist)
                reach = [c for c in chains if c["zmax"] > G[2] + 0.08 and
                         min(np.linalg.norm(l_[1] - G) for l_ in c["line"]) < 0.08]
                if reach:
                    arm_chain = max(reach, key=lambda c: c["zmax"])
                    if line is None:
                        line = [l_ for l_ in arm_chain["line"] if l_[0] > G[2] + 0.02] or None
            elif self.t_pose_hint and not hanging:
                tc = self.tpose_chain(side)
                if tc is not None:
                    line, tip = tc["line"], tc["tip"]
                    res["mode"] = "tpose"
            if res["mode"] is None and chains:
                zt = max(c["zmax"] for c in chains)
                top_ok = [c for c in chains if c["zmax"] > zt - 0.08]
                ch = min(top_ok, key=lambda c: (round(c["zmin"] / 0.02),
                                                -abs(float(c["pts"][:, 0].mean()) - X0)))
                arm_chain = ch
                full = ch["line"]
                line = self._cut_plate(full)
                if len(line) < len(full):      # an attached plate (shield) was cut off
                    tip = np.array(line[0][1])
                    res["plate_cut"] = round(float(line[0][0]), 4)
                else:
                    tip = ch["pts"][np.argmin(ch["pts"][:, 2])]
                res["mode"] = "chain"
            conf_w, conf_e = 0.1, 0.3
            if arm_chain is not None and res["mode"] in ("chain", "grip"):
                S2, sinfo = self._shoulder_from_chain(arm_chain, side)
                self.info["shoulders"][side] = sinfo
                if S2 is not None:
                    Sj = S2
                    res["S"], res["S_src"] = Sj, "arm"
            elif line is not None and res["mode"] == "tpose":
                Sj = np.array([X0 + s * self.shoulder_x, self.spine_y(self.shoulder_z) - 0.005,
                               self.shoulder_z])
                Sj = self._refine_shoulder(Sj, line, side)
                res["S"], res["S_src"] = Sj, "tpose"
            plate = self._plate_of(side) if res["mode"] == "chain" else None
            hidden = False
            if plate is not None and tip is not None:
                # the hand is behind the shield when the visible arm ends at the plate well short of
                # a full arm (the plate itself was masked out of the chain by find_plates)
                rel = tip - np.asarray(plate["centre"])
                n_ = np.asarray(plate["normal"])
                inplane = float(np.linalg.norm(rel - (rel @ n_) * n_))
                at_plate = inplane < plate["radius"] + 0.06 and abs(float(rel @ n_)) < 0.12
                vis = float(np.linalg.norm(tip - Sj))
                hidden = at_plate and vis < 0.8 * (PRIOR["upper_arm"] + PRIOR["forearm"])
                res["visible_arm"] = round(vis, 4)
            if res["mode"] == "grip":
                G = grip["G"]
                dprop = np.asarray(grip["prop"]["d"], dtype=np.float64)

                def hand_dir(u_):
                    # power grip: the knuckle line is perpendicular to the shaft
                    up = u_ - (u_ @ dprop) * dprop
                    return up / np.linalg.norm(up) if np.linalg.norm(up) > 0.3 else u_
                u = (G - Sj) / np.linalg.norm(G - Sj)
                hd = hand_dir(u)
                E = self.solve_elbow_ik(Sj, G - 0.045 * hd, side)
                for _ in range(2):
                    u = (G - E) / max(np.linalg.norm(G - E), 1e-9)
                    hd = hand_dir(u)
                    E = self.solve_elbow_ik(Sj, G - 0.045 * hd, side)
                W = G - 0.045 * hd
                tipp = G + 0.05 * hd
                if grip["prop"].get("grip_mode") == "end":
                    # fist on a hilt with the blade hanging away from the forearm: the wrist sits
                    # up the forearm from the fist, the hand runs across the fist
                    W = G - 0.045 * u
                    tipp = G + 0.05 * hd
                conf_w, conf_e = 0.8, 0.4
                if line and len(line) >= 6:
                    E2 = self._elbow_from_line(Sj, W, line)
                    if E2 is not None:
                        E, conf_e = E2, 0.6
            elif hidden:
                # hand on the shield grip: behind the plate centre (on the body side), reached by
                # a two-bone IK with prior lengths; the visible upper arm still gives the elbow
                C, n = np.asarray(plate["centre"]), np.asarray(plate["normal"])
                if (Sj - C) @ n < 0:
                    n = -n
                Wt = C + 0.03 * n
                L12 = (PRIOR["upper_arm"] + PRIOR["forearm"]) * 0.97
                if np.linalg.norm(Wt - Sj) > L12:
                    Wt = Sj + (Wt - Sj) / np.linalg.norm(Wt - Sj) * L12
                E2 = self._elbow_from_line(Sj, Wt, line) if line and len(line) >= 6 else None
                E = E2 if E2 is not None else self.solve_elbow_ik(Sj, Wt, side)
                # forearm along the back of the shield toward its centre, prior length
                u = (Wt - E) / max(np.linalg.norm(Wt - E), 1e-9)
                L2 = PRIOR["forearm"]
                W = E + u * float(np.clip(np.linalg.norm(Wt - E), 0.85 * L2, 1.15 * L2))
                tipp = W + u * PRIOR["hand"] * 0.8
                conf_w, conf_e = 0.4, (0.6 if E2 is not None else 0.35)
                res["mode"] = "shield"
            elif tip is not None:
                W, tipp, conf_w = self._wrist_from_line(line, tip)
                E2 = self._elbow_from_line(Sj, W, line)
                E = E2 if E2 is not None else self.solve_elbow_ik(Sj, W, side)
                conf_e = 0.75 if E2 is not None else 0.35
                L_arm = float(np.linalg.norm(E - Sj) + np.linalg.norm(W - E))
                clamp = self.params.get("arm_clamp_h", ARM_CLAMP_H)
                if clamp and L_arm > float(clamp):
                    # the free chain ran on past the hand (a cape panel, a robe hem or a fused prop below it): the
                    # wrist goes to a plausible arm length toward where the chain pointed, the elbow by IK - a
                    # low-confidence guess instead of a refused rig (arm > 0.50 H)
                    L_t = (PRIOR["upper_arm"] + PRIOR["forearm"]) * 1.02
                    u_ = (W - Sj) / max(float(np.linalg.norm(W - Sj)), 1e-9)
                    W = Sj + u_ * L_t
                    E = self.solve_elbow_ik(Sj, W, side)
                    tipp = W + u_ * PRIOR["hand"]
                    conf_w, conf_e = 0.3, 0.3
                    res["arm_clamped_from_h"] = round(L_arm, 4)
                    self.warn.append("%s arm chain is %.2f H long (runs on past the hand into a cape / robe / prop): "
                                     "wrist placed at the prior arm length along it" % (side.lower(), L_arm))
            else:
                W = Sj + np.array([0.03 * s, 0.01, -(PRIOR["upper_arm"] + PRIOR["forearm"]) * 0.98])
                E = self.solve_elbow_ik(Sj, W, side)
                tipp = W + np.array([0.0, 0.0, -PRIOR["hand"]])
                res["mode"] = "prior"
                self.warn.append("%s arm not found in the geometry; placed by proportion priors"
                                 % side.lower())
            if key + "_wrist" in self.ov:
                W = self._ov_point(key + "_wrist")
                conf_w = 1.0
                if key + "_elbow" not in self.ov:
                    E = self.solve_elbow_ik(Sj, W, side)
                tipp = W + (W - E) / max(np.linalg.norm(W - E), 1e-9) * PRIOR["hand"]
            if key + "_elbow" in self.ov:
                E = self._ov_point(key + "_elbow")
                conf_e = 1.0
            res.update({"E": E, "W": W, "tip": tipp, "line": line})
            self.arm[side] = res
            self.set_conf(key + "_wrist", conf_w, "user" if key + "_wrist" in self.ov else
                          ("auto" if conf_w >= 0.5 else "prior"))
            self.set_conf(key + "_elbow", conf_e, "user" if key + "_elbow" in self.ov else
                          ("auto" if conf_e >= 0.5 else "prior"))
            v = W - Sj
            nv = max(float(np.linalg.norm(v)), 1e-9)
            down = math.degrees(math.acos(float(np.clip(-v[2] / nv, -1, 1))))
            poses.append((down, -v[1] / nv))
        mean_down = float(np.mean([p[0] for p in poses]))
        if max(p[1] for p in poses) > 0.75:
            pose = "forward"
        elif mean_down < 30:
            pose = "arms_down"
        elif mean_down < 65:
            pose = "A"
        elif mean_down < 115:
            pose = "T"
        else:
            pose = "up"
        self.pose = pose
        self.info["arms"] = {s: {"mode": self.arm[s]["mode"], "angle_from_down_deg": round(d, 1)}
                             for s, (d, _) in zip(SIDES, poses)}
        if pose in ("forward", "up"):
            self.warn.append("pose class '%s' is outside the supported T/A/arms-down set" % pose)

    @staticmethod
    def _cut_plate(line):
        """line is ordered bottom -> top. Walk down from the armpit; cut where the cross-section
        jumps to plate size (shield touching the arm) for 3+ slices. Returns the kept part."""
        if len(line) < 8:
            return line
        sizes = [max(l_[3], l_[4]) for l_ in line]
        n = len(line)
        ref = float(np.median(sizes[int(n * 0.7):]))
        lim = max(0.14, 2.5 * ref)
        run = 0
        for i in range(n - 1, -1, -1):
            run = run + 1 if sizes[i] > lim else 0
            if run >= 3:
                keep = line[i + 3:]
                return keep if len(keep) >= 6 else line
        return line

    def _ov_point(self, key):
        a = self.ov[key]
        if len(a) == 3:
            return a.copy()
        return np.array([a[0], self.mid_y(a[0], a[1]), a[1]])

    def _wrist_from_line(self, line, tip):
        """Wrist = narrowest cross-section 0.06..0.14 H along the arm from the fingertip."""
        pts = np.array([l_[1] for l_ in line])
        area = np.array([l_[2] for l_ in line])
        d = np.linalg.norm(pts - tip, axis=1)
        sel = np.nonzero((d > 0.06) & (d < 0.14))[0]
        if len(sel) == 0:
            far = int(np.argmax(d))
            return pts[far], tip, 0.55
        sm = np.array([np.median(area[max(0, i - 1):i + 2]) for i in range(len(area))])
        i = sel[np.argmin(sm[sel])]
        conf = 0.85 if 0.07 < d[i] < 0.13 else 0.6
        return pts[i], tip, conf

    def _elbow_from_line(self, S_, W_, line):
        """Elbow on the visible arm centreline. A two-segment polyline S-E-W is fitted to the
        centreline (E restricted to the middle of the arm); if it bends by more than 12 degrees and
        fits much better than the straight chord, E is the bend, else E is the centreline point at
        the upper-arm/forearm length ratio. The 20% next to the shoulder is ignored (armpit skin
        biases the slice centroids there)."""
        pts = np.array([l_[1] for l_ in line])
        chord = W_ - S_
        Lc = float(np.linalg.norm(chord))
        if Lc < 1e-6:
            return None
        u = chord / Lc
        t = (pts - S_) @ u
        sel = (t > 0.2 * Lc) & (t < 0.95 * Lc)
        if sel.sum() < 6:
            return None
        Q, tt = pts[sel], t[sel]

        def seg_d2(P_, a, b_):
            ab = b_ - a
            s = np.clip(((P_ - a) @ ab) / max(float(ab @ ab), 1e-12), 0, 1)
            return np.sum((P_ - (a + np.outer(s, ab))) ** 2, axis=1)
        straight = float(seg_d2(Q, S_, W_).sum())
        best = None
        # the bend is searched where upper/forearm stays in about 0.8..1.5 (a cuff flare or a
        # gauntlet edge near the wrist must not become the elbow)
        for i in np.nonzero((tt > 0.44 * Lc) & (tt < 0.60 * Lc))[0]:
            E = Q[i]
            m = tt < tt[i]
            cost = float(seg_d2(Q[m], S_, E).sum() + seg_d2(Q[~m], E, W_).sum())
            if best is None or cost < best[0]:
                best = (cost, E)
        frac = PRIOR["upper_arm"] / (PRIOR["upper_arm"] + PRIOR["forearm"])
        if best is not None:
            E = best[1]
            a1, a2 = E - S_, W_ - E
            ang = math.degrees(math.acos(float(np.clip(a1 @ a2 / max(np.linalg.norm(a1) * np.linalg.norm(a2), 1e-12), -1, 1))))
            if ang > 12 and best[0] < 0.5 * straight:
                return E
        i = int(np.argmin(np.abs(tt - frac * Lc)))
        if abs(tt[i] - frac * Lc) > 0.04:
            return None
        return Q[i]

    def _refine_shoulder(self, Sj, line, side):
        """T-pose only: move the shoulder joint half way toward the upper-arm axis (fit to the
        centreline points nearest the shoulder) so the rest bone runs down the middle of the arm."""
        if not line or len(line) < 6:
            return Sj
        pts = np.array([l_[1] for l_ in line])
        d = np.linalg.norm(pts - Sj, axis=1)
        order = np.argsort(d)
        up = pts[order[:max(4, len(pts) // 3)]]
        c, dvec, _, _ = _line_fit(up)
        t = float((Sj - c) @ dvec)
        q = c + t * dvec
        s = self.side_sign(side)
        S2 = 0.5 * (Sj + q)
        S2[0] = self.X0 + s * float(np.clip((S2[0] - self.X0) * s, 0.07, 0.14))
        S2[2] = float(np.clip(S2[2], self.shoulder_z - 0.03, self.shoulder_z + 0.03))
        return S2

    # ------------------------------------------------------------- legs: knees, ankles, feet
    def feet_knees(self):
        X0 = self.X0
        self.leg = {}
        for side in SIDES:
            s = self.side_sign(side)
            key = side[0].lower()
            tr = self.tracks.get(side) or []
            hip = np.array([X0 + s * PRIOR["upleg_x"], self.Y0, self.crotch + PRIOR["upleg_dz"]])
            if len(tr) > 20 and not self.robe:
                zs = np.array([t_[0] for t_ in tr])
                cs = np.array([t_[1] for t_ in tr])
                wa = np.array([t_[2] * t_[3] for t_ in tr])
                th = (zs > self.crotch - 0.07) & (zs < self.crotch - 0.02)
                if th.any():
                    hx = abs(float(np.median(cs[th, 0])) - X0) * 0.8
                    hip[0] = X0 + s * float(np.clip(hx, 0.035, 0.09))
                hip[1] = self.spine_y(hip[2])
                # ankle: narrowest leg section 0.035..0.075 H; a flat profile -> 0.05 H
                sa = np.nonzero((zs > 0.035) & (zs < 0.075))[0]
                if len(sa) and (wa[sa].max() - wa[sa].min()) > 0.1 * wa[sa].max():
                    ia = sa[np.argmin(wa[sa])]
                else:
                    ia = int(np.argmin(np.abs(zs - PRIOR["ankle_z"])))
                az = float(zs[ia])
                above = (zs > az + 0.01) & (zs < az + 0.04)
                ac = cs[above].mean(0) if above.any() else cs[ia]
                ankle = np.array([cs[ia][0], ac[1], az])
                # knee by the thigh/shin ratio (width minima are unreliable under clothing);
                # the joint sits on the leg-slice centre at that height
                kz0 = az + PRIOR["knee_frac"] * (hip[2] - az)
                ik = int(np.argmin(np.abs(zs - kz0)))
                near_k = np.abs(zs - kz0) < 0.012
                knee = cs[near_k].mean(0) if near_k.any() else cs[ik].copy()
                knee[2] = kz0
                kconf = 0.7 if abs(zs[ik] - kz0) < 0.01 else 0.45
                foot = np.concatenate([t_[4] for t_ in tr if t_[0] < az] or [np.zeros((0, 3))])
                if len(foot):
                    foot = foot[np.linalg.norm(foot[:, :2] - ankle[:2], axis=1) < 0.2]
                if len(foot) > 20:
                    ytoe = float(np.percentile(foot[:, 1], 1.5))
                    yheel = float(np.percentile(foot[:, 1], 98.5))
                    toes = foot[foot[:, 1] < ytoe + 0.02]
                    xt = float(np.median(toes[:, 0]))
                    ball_y = ytoe + 0.3 * (yheel - ytoe)
                    ball = foot[np.abs(foot[:, 1] - ball_y) < 0.015]
                    bx = float(np.median(ball[:, 0])) if len(ball) else (ankle[0] + xt) * 0.5
                    toebase = np.array([bx, ball_y, min(0.03, az * 0.6)])
                    toe_end = np.array([xt, ytoe, min(0.025, az * 0.5)])
                    fconf = 0.8
                else:
                    toebase = ankle + np.array([0.0, -0.09, -0.025])
                    toe_end = ankle + np.array([0.0, -0.13, -0.03])
                    fconf = 0.3
                lconf = 0.85
            else:
                hip[1] = self.spine_y(hip[2])
                az = PRIOR["ankle_z"]
                ankle = np.array([X0 + s * (PRIOR["upleg_x"] + 0.015), self.Y0 + 0.01, az])
                kz = az + PRIOR["knee_frac"] * (hip[2] - az)
                knee = np.array([X0 + s * (PRIOR["upleg_x"] + 0.008), self.Y0 - 0.005, kz])
                toebase = ankle + np.array([s * 0.005, -0.075, -0.025])
                toe_end = ankle + np.array([s * 0.01, -0.12, -0.03])
                kconf, fconf, lconf = 0.3, 0.25, 0.3
                P = self.Pb
                gsel = (P[:, 2] < 0.03) & ((P[:, 0] - X0) * s > 0.01) & ((P[:, 0] - X0) * s < 0.15)
                if gsel.sum() > 20:
                    yf = float(np.percentile(P[gsel, 1], 2))
                    if yf < ankle[1] - 0.06:
                        toe_end[1] = max(yf + 0.01, ankle[1] - 0.16)
                        toebase[1] = ankle[1] + (toe_end[1] - ankle[1]) * 0.65
            if key + "_knee" in self.ov:
                knee = self._ov_point(key + "_knee")
                kconf = 1.0
            self.leg[side] = {"UpLeg": hip, "Leg": knee, "Foot": ankle, "ToeBase": toebase,
                              "Toe_End": toe_end}
            self.set_conf(key + "_knee", kconf, "user" if key + "_knee" in self.ov else
                          ("auto" if kconf >= 0.5 else "prior"))
            self.conf[side.lower() + "_foot"] = round(fconf, 3)

    # ------------------------------------------------------------- assemble
    def assemble(self):
        X0 = self.X0
        j = {}
        hips_z = self.crotch + PRIOR["hips_dz"]
        j["Hips"] = np.array([X0, self.spine_y(hips_z), hips_z])
        j["Neck"] = np.array([X0, self.spine_y(self.neckbase_z), self.neckbase_z])
        for name, f in zip(("Spine", "Spine1", "Spine2"), PRIOR["spine_fracs"]):
            z = hips_z + f * (self.neckbase_z - hips_z)
            j[name] = np.array([X0, self.spine_y(z), z])
        hp = self.trunk_at(self.head_z)
        j["Head"] = np.array([X0, hp["ymid"], self.head_z])
        tp = self.trunk_at(self.top - 0.03)
        j["HeadTop_End"] = np.array([X0, 0.5 * (tp["ymid"] + hp["ymid"]), self.top])
        for side in SIDES:
            a = self.arm[side]
            j[side + "Arm"] = a["S"].copy()
            j[side + "ForeArm"] = a["E"]
            j[side + "Hand"] = a["W"]
            j[side + "HandTip"] = a["tip"]
            for n, p in self.leg[side].items():
                j[side + n] = p
        # symmetric shoulders and hip joints (a crooked rest pose leaks into every clip); a shoulder
        # measured on the visible arm wins over a prior one
        srcL, srcR = self.arm["Left"].get("S_src"), self.arm["Right"].get("S_src")
        for a_, b_ in (("LeftArm", "RightArm"), ("LeftUpLeg", "RightUpLeg")):
            pa, pb = j[a_], j[b_]
            if a_ == "LeftArm" and (srcL == "arm") != (srcR == "arm"):
                pa = pb = (pa if srcL == "arm" else pb)
            dx = (abs(pa[0] - X0) + abs(pb[0] - X0)) * 0.5
            y = (pa[1] + pb[1]) * 0.5
            z = (pa[2] + pb[2]) * 0.5
            j[a_], j[b_] = np.array([X0 + dx, y, z]), np.array([X0 - dx, y, z])
        # clavicle (Shoulder bone) head: next to the spine, a little above the shoulder joint, as in
        # Mixamo's auto-rig; never above the neck base
        zc = min(j["LeftArm"][2] + 0.035, self.neckbase_z - 0.005)
        for side in SIDES:
            s = self.side_sign(side)
            j[side + "Shoulder"] = np.array([X0 + s * PRIOR["clav_x"], j["Spine2"][1] - 0.005, zc])
        for name, p in self.ov_joints.items():
            j[name] = p.copy()
        self.j = j

    # ------------------------------------------------------------- props -> bones
    def bind_props(self):
        """No detected prop is left to bone heat (it would bend): the gripping hand; else the
        nearest hand when the prop axis passes within 0.08 H of the hand joint or the middle of the
        hand; else the nearest trunk bone (a staff leaning on the body, a sheathed sword)."""
        j = self.j
        # a "plate" is a held shield only next to that side's forearm/hand (the forearm runs behind it); a flat
        # cape flap or pauldron hanging from the shoulder far from the hand is left to the skin weights - made
        # rigid to the hand it tore into a sheet whenever the arm moved (generated bone swordsman: 12-21 % of
        # the posed surface on every clip)
        kept = []
        for pl in getattr(self, "plate_props", []):
            sd = pl["side"]
            dist = float(_seg_dist(np.asarray(pl["centre"])[None, :], j[sd + "ForeArm"], j[sd + "HandTip"])[0][0])
            pl["forearm_dist"] = round(dist, 4)
            pp = self._plate_prop(pl["centre"])
            if pp is not None and pp.get("held") and pp.get("side"):
                # a banner on its pole: rigid to the hand holding the pole, however far above the hand it hangs
                pl["side"], pl["on_prop"] = pp["side"], True
                kept.append(pl)
                continue
            if dist > 0.15:
                self.info.setdefault("plates_rejected", []).append(
                    {"side": sd, "centre": rnd(pl["centre"]), "radius": round(float(pl["radius"]), 3),
                     "reason": "not held: centre %.2f H from the %s forearm/hand" % (dist, sd.lower())})
                self.warn.append("flat part beside the %s arm (%.2f H from the forearm) is not a held shield; "
                                 "left to the skin weights" % (sd.lower(), dist))
                continue
            kept.append(pl)
        if len(kept) != len(getattr(self, "plate_props", [])):
            self.plate_props = kept
            self.info["plates"] = len(kept)
        for p in self.props:
            ex = p.get("extent", p["t"])
            if p.get("belt"):
                continue                       # hangs at the belt: bound to Hips by find_blades
            if p.get("held"):
                p["bind"], p["bind_mode"] = p["side"] + "Hand", "grip_" + p.get("grip_mode", "shaft")
                continue
            best = None
            for side in SIDES:
                for q in (j[side + "Hand"], 0.5 * (j[side + "Hand"] + j[side + "HandTip"])):
                    rel = q - p["c"]
                    t = float(np.clip(rel @ p["d"], ex[0], ex[1]))
                    dist = float(np.linalg.norm(rel - t * p["d"]))
                    if best is None or dist < best[0]:
                        best = (dist, side, t)
            length = float(ex[1] - ex[0]) * self.H
            if best[0] < 0.08:
                side = best[1]
                p.update({"held": True, "side": side, "grip": p["c"] + p["d"] * best[2],
                          "grip_mode": "near_hand", "bind": side + "Hand", "bind_mode": "near_hand"})
                self.warn.append("prop %.2f m: no visible grip; its axis passes %.3f H from the %s "
                                 "hand, bound rigid to that hand" % (length, best[0], side.lower()))
                continue
            mid = p["c"] + p["d"] * 0.5 * (ex[0] + ex[1])
            trunk = [("Hips", "Spine"), ("Spine", "Spine1"), ("Spine1", "Spine2"), ("Spine2", "Neck")]
            dists = [(float(_seg_dist(mid[None, :], j[a_], j[b_])[0][0]), a_) for a_, b_ in trunk]
            bone = min(dists)[1]
            p.update({"bind": bone, "bind_mode": "trunk"})
            self.warn.append("prop %.2f m is not held (nearest hand %.2f H away); bound rigid to %s "
                             "so it never bends" % (length, best[0], bone))

    # ------------------------------------------------------------- sanity
    def sanity(self):
        j, issues = self.j, []

        def L(a, b_):
            return float(np.linalg.norm(j[a] - j[b_]))
        ratios = {}
        for side in SIDES:
            key = side[0].lower()
            ua, fa = L(side + "Arm", side + "ForeArm"), L(side + "ForeArm", side + "Hand")
            th, sh = L(side + "UpLeg", side + "Leg"), L(side + "Leg", side + "Foot")
            r = {"upper_arm": ua, "forearm": fa, "thigh": th, "shin": sh, "arm": ua + fa,
                 "leg": th + sh, "upper_fore": ua / max(fa, 1e-6), "thigh_shin": th / max(sh, 1e-6)}
            ratios[side] = {k: round(v, 4) for k, v in r.items()}
            if not (0.20 <= r["arm"] <= 0.45):
                issues.append("%s arm length %.2f H outside 0.20..0.45" % (side, r["arm"]))
                self.conf[key + "_wrist"] = min(self.conf.get(key + "_wrist", 0), 0.3)
            if not (0.7 <= r["upper_fore"] <= 1.7):
                issues.append("%s upper/forearm %.2f outside 0.7..1.7" % (side, r["upper_fore"]))
                self.conf[key + "_elbow"] = min(self.conf.get(key + "_elbow", 0), 0.35)
            if not (0.35 <= r["leg"] <= 0.60):
                issues.append("%s leg length %.2f H outside 0.35..0.60" % (side, r["leg"]))
                self.conf[key + "_knee"] = min(self.conf.get(key + "_knee", 0), 0.3)
            if not (0.7 <= r["thigh_shin"] <= 1.45):
                issues.append("%s thigh/shin %.2f outside 0.7..1.45" % (side, r["thigh_shin"]))
                self.conf[key + "_knee"] = min(self.conf.get(key + "_knee", 0), 0.4)
        head = self.top - self.head_z
        if not (0.06 <= head <= 0.26):
            issues.append("head height %.2f H outside 0.06..0.26" % head)
            self.conf["chin"] = min(self.conf.get("chin", 0), 0.3)
        sw = 2 * self.shoulder_x
        if not (0.12 <= sw <= 0.30):
            issues.append("shoulder width %.2f H outside 0.12..0.30" % sw)
        self.ratios = ratios
        self.issues = issues
        for msg in issues:
            self.warn.append("proportions: " + msg)
        w = {"chin": 1.0, "groin": 1.5, "l_wrist": 1.0, "r_wrist": 1.0, "l_elbow": 0.6,
             "r_elbow": 0.6, "l_knee": 0.8, "r_knee": 0.8}
        overall = sum(self.conf.get(k, 0) * v for k, v in w.items()) / sum(w.values())
        g = self.info["ground"]
        if self.ground_ok and self.leg_mode == "robe" and g["extent_x"] < 0.18 and "groin" not in self.ov:
            self.ground_ok = False
            g["ok"] = False
            g["note"] = "no separable legs and no robe hem (footprint narrower than 0.18 H)"
        if not self.ground_ok and "groin" not in self.ov:
            overall = min(overall, 0.15)
            g = self.info["ground"]
            self.fail = ("no ground support: the lowest 2%% of the mesh covers only %.2f x %.2f H "
                         "(%d samples) - a floating or legless figure, not a standing biped. Pass "
                         "params.markers (groin, knees) to force a biped rig, or use a creature "
                         "template." % (g["extent_x"], g["extent_y"], g["samples"]))
        hard = []
        sh_ratio = self.shoulder_z
        if not (0.70 <= sh_ratio <= 0.88):
            # a crown, a halo of spikes, a crest or horns above the head count into the head top: the shoulders are
            # also measured against the solid top of the head (the highest slice whose widest component near the
            # midline is at least 0.045 H across - spikes and plumes are thinner)
            solid = self.solid_head_top()
            if solid and solid < self.top - 0.01:
                sh_ratio = self.shoulder_z / solid
                self.info["solid_head_top"] = {"z": round(solid, 4), "shoulders_vs_solid_top": round(sh_ratio, 4)}
        if not (0.70 <= sh_ratio <= 0.88) and "chin" not in self.ov:
            hard.append("shoulders at %.2f H (humanoid range 0.70..0.88)" % self.shoulder_z)
        for side in SIDES:
            if not (0.15 <= ratios[side]["arm"] <= 0.5) and not any(k in self.ov for k in ("l_wrist", "r_wrist")):
                hard.append("%s arm %.2f H (humanoid range 0.15..0.50)" % (side.lower(), ratios[side]["arm"]))
        if hard and self.fail is None:
            overall = min(overall, 0.2)
            self.fail = "not a humanoid of supported proportions: " + "; ".join(hard)
        # gross proportion failures: the landmarks are wrong (a wrist on a bow limb, a shoulder on
        # the collar, a crotch at a tunic hem) - no rig rather than a visibly broken one
        gross = []
        for side in SIDES:
            key = side[0].lower()
            r = ratios[side]
            user_arm = any(k in self.ov for k in (key + "_wrist", key + "_elbow")) or \
                any(n in self.ov_joints for n in (side + "Arm", side + "ForeArm", side + "Hand"))
            user_leg = any(k in self.ov for k in ("groin", key + "_knee")) or \
                any(n in self.ov_joints for n in (side + "UpLeg", side + "Leg", side + "Foot"))
            lo, hi = GROSS["upper_fore"]
            if not user_arm and not (lo <= r["upper_fore"] <= hi):
                gross.append("%s upper/forearm %.2f (limit %.1f..%.1f)" % (side.lower(), r["upper_fore"], lo, hi))
            if not user_arm and r["forearm"] < GROSS["forearm_min"]:
                gross.append("%s forearm %.3f H (< %.2f)" % (side.lower(), r["forearm"], GROSS["forearm_min"]))
            if not user_leg and r["leg"] < GROSS["leg_min"]:
                gross.append("%s leg %.3f H (< %.2f)" % (side.lower(), r["leg"], GROSS["leg_min"]))
        # mirrored arm joints: a rest pose is near-symmetric unless a hand holds something
        # held_hands: hands whose prop was cut out before the rig (extract_props placement sockets): the
        # asymmetric pose of a hand that held a sword is still a held pose
        held_in = set(self.params.get("held_hands") or [])
        holds = {side: bool(side in self.grip or self._plate_of(side) is not None or side in held_in or
                            any(p.get("held") and p.get("side") == side for p in self.props))
                 for side in SIDES}
        self.arm_sym = {}
        for n in ("ForeArm", "Hand"):
            Lj, Rj = j["Left" + n], j["Right" + n]
            m = np.array([2 * self.X0 - Rj[0], Rj[1], Rj[2]])
            self.arm_sym[n] = round(float(np.linalg.norm(Lj - m)), 4)
        self.arm_sym_max = max(self.arm_sym.values())
        user_any = any(k in self.ov for k in ("l_wrist", "r_wrist", "l_elbow", "r_elbow"))
        self.arm_sym_exempt = holds["Left"] or holds["Right"] or user_any
        if self.arm_sym_max > ARM_SYM_MAX and not self.arm_sym_exempt:
            gross.append("arm landmarks are asymmetric: mirrored %s %.3f H apart (limit %.2f) and no "
                         "hand holds a detected prop - an undetected prop (bow?) or a turned/crooked "
                         "pose; correct params.markers" % (
                             max(self.arm_sym, key=self.arm_sym.get), self.arm_sym_max, ARM_SYM_MAX))
            for k in ("l_wrist", "r_wrist", "l_elbow", "r_elbow"):
                self.conf[k] = min(self.conf.get(k, 0), 0.3)
        self.gross = gross
        if gross and self.fail is None:
            overall = min(overall, 0.25)
            self.fail = "landmarks fail the proportion checks: " + "; ".join(gross)
        self.overall = round(float(overall), 3)
        min_c = float(self.params.get("min_confidence", 0.35))
        if self.fail is None and self.overall < min_c:
            self.fail = ("landmark confidence %.2f below %.2f: %s" %
                         (self.overall, min_c, "; ".join(issues[:4]) or "weak landmarks"))

    def solid_head_top(self, min_fill=0.4):
        """Highest slice level where the largest 2D component within 0.12 H of the midline covers at least
        min_fill of the fullest head cross-section (cells of the slice grid) above the neck: the skull or
        helmet, not the thin spikes of a crown, a halo or a crest above it (they cover few cells even where they
        touch into one ring). None without slices."""
        S = getattr(self, "S", None)
        if S is None:
            return None
        z_lo = max(getattr(self, "neck_z", self.top - 0.2) + 0.02, self.top - 0.3)   # the head only, above the neck
        ks = list(range(S.k_of(self.top), max(S.k_of(z_lo), 0) - 1, -1))
        cnt = {}
        for k in ks:
            cs = [len(c.cells) for c in S.comps[k] if abs(float(c.c[0]) - self.X0) < 0.12]
            cnt[k] = max(cs) if cs else 0
        full = max(cnt.values()) if cnt else 0
        if full <= 0:
            return None
        for k in ks:
            if cnt[k] >= min_fill * full:
                return S.z_of(k)
        return None

    # ------------------------------------------------------------- output
    def joints_m(self):
        return {k: v * self.H for k, v in self.j.items()}

    def markers_dict(self):
        H, j = self.H, self.j

        def xz(p):
            return [round(float(p[0] * H), 4), round(float(p[2] * H), 4)]
        return {"chin": [round(self.X0 * H, 4), round((self.head_z - PRIOR["chin_below_head"]) * H, 4)],
                "groin": [round(self.X0 * H, 4), round(self.crotch * H, 4)],
                "l_wrist": xz(j["LeftHand"]), "r_wrist": xz(j["RightHand"]),
                "l_elbow": xz(j["LeftForeArm"]), "r_elbow": xz(j["RightForeArm"]),
                "l_knee": xz(j["LeftLeg"]), "r_knee": xz(j["RightLeg"])}


# =================================================================== preparation

def prepare_body(req, warnings, samples=200000, sockets=None):
    """Load, orient to face -Y, scale so the body (head top, props excluded) is height_m tall,
    feet on z=0, trunk midline at x=0 and trunk centre at y=0. Returns (body, P, info);
    info["matrix"] is the 4x4 input -> rig frame (run_landmarks adds its re-centring), so input
    sockets (sockets list, see load_body) can follow the body."""
    params = req.get("params", {})
    H = float(params.get("height_m", 1.8))
    body = load_body(req, warnings, sockets=sockets)
    verts = mesh_verts(body)
    tris = mesh_tris(body)
    if len(tris) == 0:
        raise ValueError("input mesh has no faces")
    P, _ = sample_surface(verts, tris, n=int(params.get("samples", samples)))
    lo, hi = verts.min(0), verts.max(0)
    h0 = float(hi[2] - lo[2])
    base = np.array([(lo[0] + hi[0]) * 0.5, (lo[1] + hi[1]) * 0.5, lo[2]])
    Pn = (P - base) / max(h0, 1e-9)
    front = params.get("front", "auto")
    info = {"source_height": round(h0, 4), "source_bbox": [rnd(lo), rnd(hi)]}
    if front in (None, "auto"):
        front, fconf, cues = detect_facing(Pn)
        info["facing"] = {"detected": front, "confidence": fconf, "cues": cues}
        # front_prior: where the generator puts the face (Hunyuan / Tripo concepts are front views: -Y).
        # The detector's back-to-front answer (a cape or a big shield behind / in front pulls head and
        # hands) or an uncertain one (< 0.3) loses to the prior; a confident side view (+-X: an archer
        # standing sideways) is kept
        prior = params.get("front_prior")
        if prior in FRONT_TURN and front != prior and (fconf < 0.3 or front == FRONT_OPPOSITE[prior]):
            info["facing"].update({"prior": prior, "overridden": front})
            warnings.append("facing detected %s (confidence %.2f) contradicts front_prior %s; the prior is used"
                            % (front, fconf, prior))
            front, fconf = prior, 1.0
            info["facing"]["detected"] = front
        elif prior in FRONT_TURN and front == prior:
            fconf = max(fconf, 0.5)      # the prior agrees: not uncertain
        if fconf < 0.3:
            warnings.append("facing is uncertain (%s, confidence %.2f); pass params.front to fix"
                            % (front, fconf))
    else:
        if front not in FRONT_TURN:
            raise ValueError("params.front must be auto, -Y, +Y, +X or -X")
        info["facing"] = {"detected": front, "confidence": 1.0, "cues": "params.front"}
    turn = FRONT_TURN[front]
    # leftover yaw (an input turned by e.g. 30 deg): the 90-degree turn above leaves it in +-45 deg;
    # it is measured after that turn and removed, otherwise the skeleton comes out skewed
    R0 = Matrix.Rotation(math.radians(turn), 3, "Z")
    Pt = Pn @ np.array(R0, dtype=np.float64).T
    yaw, yconf, ycues = estimate_yaw(Pt)
    info["yaw"] = {"residual_deg": round(yaw, 2), "confidence": yconf, "cues": ycues,
                   "corrected": False}
    yaw_lim = float(params.get("max_yaw_deg", YAW_CORRECT_DEG))
    if params.get("fix_yaw", True) and abs(yaw) > yaw_lim and yconf >= 0.3:
        turn -= yaw
        info["yaw"]["corrected"] = True
        warnings.append("the input is turned %.1f deg about Z from an axis-aligned pose; rotated to "
                        "face -Y before the landmarks (pass params.fix_yaw=false to keep it)" % yaw)
    elif abs(yaw) > yaw_lim:
        warnings.append("the input seems turned %.1f deg about Z (confidence %.2f) and was not "
                        "corrected; landmarks assume an axis-aligned figure" % (yaw, yconf))
    M = (Matrix.Scale(H / h0, 4) @ Matrix.Rotation(math.radians(turn), 4, "Z") @
         Matrix.Translation(-Vector(base)))
    P = transform_body(body, M, P)
    info["turn_deg"] = round(turn, 3)
    # body height by the head top (a staff or spear above the head must not shrink the body)
    probe = Landmarks(P, H, params, [])
    probe.find_props(oblique=False)
    probe.Pb = probe.P[~probe.prop_mask]
    probe.midline()
    probe.head_top()
    k = 1.0
    if params.get("height_ref", "head") == "head" and probe.top < 0.985:
        k = 1.0 / probe.top
        warnings.append("bbox top is %.0f%% above the head (prop/hair/weapon); the body is scaled so "
                        "the head top is %.2f m" % ((1 / probe.top - 1) * 100, H))
    shift = Vector((-probe.X0 * H * k, -probe.Y0 * H * k, 0.0))
    M2 = Matrix.Translation(shift) @ Matrix.Scale(k, 4)
    P = transform_body(body, M2, P)
    info["scale"] = round(H / h0 * k, 6)
    info["matrix"] = M2 @ M
    info["height_ref"] = params.get("height_ref", "head")
    info["centre_shift_m"] = rnd(shift)
    v2 = mesh_verts(body)
    info["bbox"] = [rnd(v2.min(0)), rnd(v2.max(0))]
    return body, P, info


def run_landmarks(body, P, H, params, warnings, prep=None, tol=0.003):
    """Landmarks plus re-centring (contract 4: x = 0 is the trunk/head symmetry axis). The first
    centre comes from a probe that still sees shields and held props; when the full detector puts
    the midline or trunk centre off x = y = 0 by more than tol (H units), the body and the samples
    are shifted and the landmarks re-run. Returns (landmarks, P)."""
    def with_grips(prm):
        # known_grips_in: [{side, matrix (input frame 4x4), grip_mode}] -> known_grips in the current rig frame (H)
        kin = prm.get("known_grips_in")
        if not kin or prep is None or prep.get("matrix") is None:
            return prm
        M = prep["matrix"]
        out = []
        for kg in kin:
            Mk = M @ kg["matrix"]
            z = Mk.col[2].to_3d().normalized()
            out.append({"side": kg["side"], "G": [x / H for x in Mk.translation], "d": list(z),
                        "grip_mode": kg.get("grip_mode", "shaft")})
        return dict(prm, known_grips=out)
    w1 = []
    lm = Landmarks(P, H, with_grips(params), w1).run()
    # the midline measured again without a shield/banner/blade can move once more after a shift: up to 3 passes
    for _ in range(3):
        if not (abs(lm.X0) > tol or abs(lm.Y0) > tol):
            break
        d = Vector((-lm.X0 * H, -lm.Y0 * H, 0.0))
        P = transform_body(body, Matrix.Translation(d), P)
        if prep is not None:
            prev = prep.get("recentred_m") or [0.0, 0.0, 0.0]
            prep["recentred_m"] = rnd(Vector(prev) + d)
            if "matrix" in prep:
                prep["matrix"] = Matrix.Translation(d) @ prep["matrix"]
            v2 = mesh_verts(body)
            prep["bbox"] = [rnd(v2.min(0)), rnd(v2.max(0))]
        w1 = []
        lm = Landmarks(P, H, with_grips(params), w1).run()
    warnings.extend(w1)
    return lm, P


# =================================================================== preview helpers

def _mat(name, rgba):
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.diffuse_color = rgba
    return m


def conf_color(c):
    if c is None:
        return (0.25, 0.55, 1.0, 1.0)
    if c >= 0.7:
        return (0.1, 0.85, 0.2, 1.0)
    if c >= 0.45:
        return (1.0, 0.8, 0.05, 1.0)
    return (1.0, 0.12, 0.08, 1.0)


def _sphere(name, loc, r, mat):
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=12, v_segments=8, radius=r)
    bm.to_mesh(me)
    bm.free()
    me.materials.append(mat)
    ob = bpy.data.objects.new(name, me)
    ob.location = loc
    bpy.context.scene.collection.objects.link(ob)
    return ob


def _stick(name, a, b_, r, mat):
    a, b_ = Vector(a), Vector(b_)
    L = (b_ - a).length
    if L < 1e-6:
        return None
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_cone(bm, cap_ends=True, segments=8, radius1=r, radius2=r * 0.35, depth=L)
    bm.to_mesh(me)
    bm.free()
    me.materials.append(mat)
    ob = bpy.data.objects.new(name, me)
    ob.location = (a + b_) * 0.5
    ob.rotation_mode = "QUATERNION"
    ob.rotation_quaternion = Vector((0, 0, 1)).rotation_difference((b_ - a).normalized())
    bpy.context.scene.collection.objects.link(ob)
    return ob


JOINT_MARKER = {"Head": "chin", "Hips": "groin", "LeftHand": "l_wrist", "RightHand": "r_wrist",
                "LeftForeArm": "l_elbow", "RightForeArm": "r_elbow", "LeftLeg": "l_knee",
                "RightLeg": "r_knee"}


def concat_images(paths, out, vertical=False):
    tiles = []
    for p in paths:
        img = bpy.data.images.load(p, check_existing=False)
        tiles.append(np.array(img.pixels[:], dtype=np.float32).reshape(img.size[1], img.size[0], 4))
        bpy.data.images.remove(img)
    if vertical:
        w = max(t.shape[1] for t in tiles)
        tiles = [np.pad(t, ((0, 0), (0, w - t.shape[1]), (0, 0))) for t in tiles]
        sheet = np.concatenate(tiles[::-1], axis=0)   # Blender images are bottom-up
    else:
        h = max(t.shape[0] for t in tiles)
        tiles = [np.pad(t, ((h - t.shape[0], 0), (0, 0), (0, 0))) for t in tiles]
        sheet = np.concatenate(tiles, axis=1)
    img = bpy.data.images.new("af_concat", width=sheet.shape[1], height=sheet.shape[0], alpha=True)
    img.pixels = sheet.ravel()
    img.filepath_raw = out
    img.file_format = "PNG"
    img.save()
    bpy.data.images.remove(img)
    return out


MARKER_MARGIN = 1.08   # af.render_views framing margin used by markers.png (projection block)


def render_markers(path, body, joints, conf, H, extra=None, size=640, projection=None, show=None):
    """Front + side view of the mesh with joints as coloured spheres (green >= 0.7 confidence,
    yellow >= 0.45, red below, blue = derived) and bones as sticks, drawn in front of the mesh.

    projection (dict, optional) receives the front-view projection of the written PNG for
    markers.json (docs/forge/ui.md, src/forge/forge-core.js markerProjection):
      image {width, height} = the whole PNG, center [x, z] = metres at the centre of the FRONT tile,
      meters_per_px, center_px [u, v] = that point in PNG pixels (origin = top-left corner, u right,
      v down, pixel (i, j) spans [i, i+1) x [j, j+1)), so u = cu + (x - cx)/mpp, v = cv - (z - cz)/mpp.
    The values come from the same bbox and margin af.render_views frames the front tile with.
    show (set of joint names, optional): check render - only those spheres are visible (body and
    sticks hidden) while every object still defines the framing, so the camera is unchanged."""
    keep = list(bpy.context.scene.objects)
    v = mesh_verts(body)
    lo, hi = v.min(0), v.max(0)
    r = 0.012 * H
    sets = {}
    for view, axis, plane in (("front", 1, lo[1] - 0.08 * H), ("left", 0, lo[0] - 0.08 * H)):
        objs = []
        for name, p in joints.items():
            q = np.array(p, dtype=np.float64).copy()
            q[axis] = plane
            mk = JOINT_MARKER.get(name)
            col = conf_color(conf.get(mk) if mk else None)
            objs.append(_sphere("mk_%s_%s" % (view, name), q, r * (1.4 if mk else 1.0),
                                _mat("mk_c_%s" % str(col), col)))
        for name, par in PARENT.items():
            if par and name in joints and par in joints:
                a = np.array(joints[par], dtype=np.float64)
                c = np.array(joints[name], dtype=np.float64)
                a[axis] = c[axis] = plane + (0.01 * H if axis == 1 else 0.01 * H)
                st = _stick("mk_%s_b_%s" % (view, name), a, c, r * 0.45, _mat("mk_bone", (0.95, 0.95, 0.95, 1)))
                if st:
                    objs.append(st)
        for name in ("LeftHand", "RightHand"):
            tip = name + "Tip"
            if tip in joints:
                a = np.array(joints[name], dtype=np.float64)
                c = np.array(joints[tip], dtype=np.float64)
                a[axis] = c[axis] = plane + 0.01 * H
                st = _stick("mk_%s_b_%s" % (view, tip), a, c, r * 0.45, _mat("mk_bone", (0.95, 0.95, 0.95, 1)))
                if st:
                    objs.append(st)
        for o in extra or []:
            objs.append(o)
        sets[view] = objs
    tmp = []
    visible = None
    if show is not None:
        visible = {"mk_%s_%s" % (view, n) for view in sets for n in show}
    for view in ("front", "left"):
        for o in bpy.context.scene.objects:
            if visible is None:
                o.hide_render = not (o is body or o in sets[view])
            else:
                o.hide_render = o.name not in visible
        p = os.path.join(os.path.dirname(path), "_mk_%s.png" % view)
        if view == "front" and projection is not None:
            # the framing af.render_views uses: world bbox of the passed objects, square ortho tile,
            # ortho_scale = |bbox diagonal| * margin, camera on -Y looking +Y (x right, z up)
            bpy.context.view_layer.update()
            lo3, hi3 = af.world_bbox([body] + sets[view])
            c3 = (lo3 + hi3) / 2
            ortho = max((hi3 - lo3).length, 1e-3) * MARKER_MARGIN
            projection.update({
                "view": "front", "camera": "orthographic, from -Y looking +Y; x right, z up",
                "image": {"width": 2 * int(size), "height": int(size)},
                "front_tile": {"x": 0, "y": 0, "width": int(size), "height": int(size)},
                "center": [round(float(c3.x), 6), round(float(c3.z), 6)],
                "meters_per_px": round(float(ortho / size), 9),
                "center_px": [size / 2.0, size / 2.0],
                "pixel_origin": "top-left corner; u right, v down; pixel (i, j) covers [i, i+1) x [j, j+1)",
                "formula": "u = center_px[0] + (x - center[0]) / meters_per_px; "
                           "v = center_px[1] - (z - center[1]) / meters_per_px",
                "margin": MARKER_MARGIN})
        af.render_views(p, [body] + sets[view], views=(view,), size=size, margin=MARKER_MARGIN)
        tmp.append(p)
    concat_images(tmp, path)
    for p in tmp:
        try:
            os.remove(p)
        except OSError:
            pass
    for view in sets:
        for o in sets[view]:
            if o.name.startswith("mk_"):
                me = o.data
                bpy.data.objects.remove(o, do_unlink=True)
                if me and me.users == 0:
                    bpy.data.meshes.remove(me)
    bpy.context.view_layer.update()
    for o in keep:
        o.hide_render = False
    return path


# =================================================================== armature

def _roll_hint(name):
    """Primary/secondary Z hints of the documented roll convention (module docstring)."""
    s = short(name)
    if any(k in s for k in ("Foot", "ToeBase", "Toe_End")):
        return Vector((0, 0, 1)), Vector((0, -1, 0))
    return Vector((0, -1, 0)), Vector((0, 0, 1))


BLADE_MAX_H = 0.75    # props shorter than this (H) held near one end are blades/hafts: +Z to the far end


def prop_grip_dir(p, H=None):
    """+Z of the hand frame for a hand holding prop p (contract 6: along the blade/shaft from the
    hand). Returns (unit vector, rule, reference vector for QA).
      - grip in the outer 35 % of a prop shorter than 0.75 H (sword, axe, mace held at the hilt or
        haft end): toward the far end (a blade hanging down -> +Z down);
      - otherwise (staff, spear, a long shaft gripped mid-way or near its top): up along the shaft,
        a near-horizontal shaft: toward its longer part.
    The reference vector is geometric: grip -> far end of the prop extent for the blade rule, the
    upward shaft axis for the shaft rule."""
    d = np.asarray(p["d"], dtype=np.float64)
    ex = p.get("extent", p["t"])
    g = float((np.asarray(p["grip"]) - p["c"]) @ d)
    L = max(float(ex[1] - ex[0]), 1e-9)
    frac = (g - ex[0]) / L
    if L < BLADE_MAX_H and (frac < 0.35 or frac > 0.65):
        far = p["c"] + d * (ex[1] if frac < 0.5 else ex[0])
        ref = far - np.asarray(p["grip"])
        ref = ref / max(np.linalg.norm(ref), 1e-9)
        return (d if frac < 0.5 else -d), "blade_to_far_end", ref, round(frac, 3)
    if abs(d[2]) < 0.3:
        dd = d if (ex[1] - g) >= (g - ex[0]) else -d
        return dd, "shaft_longer_part", dd, round(frac, 3)
    dd = d if d[2] >= 0 else -d
    return dd, "shaft_up", dd, round(frac, 3)


def grip_roll_hints(lm):
    """Hands that hold (or were bound to) a detected prop: their +Z (thumb side) follows
    prop_grip_dir, so socket_mount_hand_* matches the grip frame of contract 6."""
    hints = {}
    for p in lm.props:
        if not p.get("held") or not str(p.get("bind", "")).endswith("Hand"):
            continue
        d, rule, ref, frac = prop_grip_dir(p)
        p["grip_rule"], p["grip_frac"], p["grip_ref"] = rule, frac, ref
        hints[p["side"] + "Hand"] = d
    # a bow: +Z up along the riser (its chord); a curved blade held at one end: toward the far tip
    for bw in getattr(lm, "bow_props", []):
        d = np.asarray(bw["grip_dir"], dtype=np.float64)
        bw["grip_rule"] = "bow_riser_up" if bw["kind"] == "bow" else "curved_blade_to_far_tip"
        bw["grip_ref"] = d
        hints[bw["side"] + "Hand"] = d
    return hints


def build_armature(joints, H, fingers=None, name="Armature", roll_hints=None):
    """Create the mixamorig armature from joint heads (metres). fingers: {side: {finger: [4 pts]}}.
    roll_hints: {short bone name: world vector} overriding the +Z hint of the roll convention."""
    roll_hints = roll_hints or {}
    j = {k: Vector(np.asarray(v, dtype=np.float64)) for k, v in joints.items()}
    up = Vector((0, 0, 1))
    heads, tails = {}, {}
    chain = [("Hips", "Spine"), ("Spine", "Spine1"), ("Spine1", "Spine2"), ("Spine2", "Neck"),
             ("Neck", "Head"), ("Head", "HeadTop_End")]
    for a_, b_ in chain:
        heads[a_], tails[a_] = j[a_], j[b_]
    heads["HeadTop_End"] = j["HeadTop_End"]
    tails["HeadTop_End"] = j["HeadTop_End"] + up * 0.05 * H
    for s in SIDES:
        for a_, b_ in ((s + "Shoulder", s + "Arm"), (s + "Arm", s + "ForeArm"),
                       (s + "ForeArm", s + "Hand"), (s + "UpLeg", s + "Leg"), (s + "Leg", s + "Foot"),
                       (s + "Foot", s + "ToeBase"), (s + "ToeBase", s + "Toe_End")):
            heads[a_], tails[a_] = j[a_], j[b_]
        heads[s + "Hand"] = j[s + "Hand"]
        tails[s + "Hand"] = j.get(s + "HandTip", j[s + "Hand"] + (j[s + "Hand"] - j[s + "ForeArm"]).normalized() * 0.1 * H)
        if fingers and s in fingers:
            mids = fingers[s].get("Middle")
            if mids:
                tails[s + "Hand"] = Vector(mids[0])
        d = (j[s + "Toe_End"] - j[s + "ToeBase"])
        d = d.normalized() if d.length > 1e-6 else Vector((0, -1, 0))
        heads[s + "Toe_End"] = j[s + "Toe_End"]
        tails[s + "Toe_End"] = j[s + "Toe_End"] + d * 0.03 * H
        if fingers and s in fingers:
            for f, pts in fingers[s].items():
                pts = [Vector(p) for p in pts]
                for i in range(3):
                    heads[s + "Hand" + f + str(i + 1)] = pts[i]
                    tails[s + "Hand" + f + str(i + 1)] = pts[i + 1]
                dd = (pts[3] - pts[2])
                dd = dd.normalized() if dd.length > 1e-6 else Vector((0, 0, -1))
                heads[s + "Hand" + f + "4"] = pts[3]
                tails[s + "Hand" + f + "4"] = pts[3] + dd * 0.012 * H
    for k in list(heads):
        if (tails[k] - heads[k]).length < 1e-4 * H:
            tails[k] = heads[k] + up * 0.01 * H
    data = bpy.data.armatures.new(name)
    rig = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(rig)
    select_only(rig)
    bpy.ops.object.mode_set(mode="EDIT")
    eb = data.edit_bones
    order = [n for n in BONES25] + [n for n in FINGER_BONES if n in heads]
    for n in order:
        bb = eb.new(b(n))
        bb.head, bb.tail = heads[n], tails[n]
        bb.use_deform = n not in NON_DEFORM
    for n in order:
        par = PARENT[n]
        if par:
            bb = eb[b(n)]
            bb.parent = eb[b(par)]
            bb.use_connect = (bb.parent.tail - bb.head).length < 1e-5
    # rolls: +Z to the front (feet/toes: up); hands first so fingers can follow them
    for n in order:
        if "Hand" in n and n[-1].isdigit():
            continue
        bb = eb[b(n)]
        h1, h2 = _roll_hint(n)
        if n in roll_hints:
            h1 = Vector(roll_hints[n]).normalized()
        y = bb.y_axis
        hint = h1 if abs(y.dot(h1)) < 0.92 else h2
        bb.align_roll(hint)
    for n in order:
        if not ("Hand" in n and n[-1].isdigit()):
            continue
        side = "Left" if n.startswith("Left") else "Right"
        hz = eb[b(side + "Hand")].z_axis.copy()
        bb = eb[b(n)]
        if abs(bb.y_axis.dot(hz)) > 0.92:
            hz = eb[b(side + "Hand")].x_axis.copy()
        bb.align_roll(hz)
    bpy.ops.object.mode_set(mode="OBJECT")
    for pb in rig.pose.bones:
        pb.rotation_mode = "QUATERNION"
    data.display_type = "OCTAHEDRAL"
    rig.show_in_front = True
    return rig


# =================================================================== weights

def deform_names(rig):
    return [bn.name for bn in rig.data.bones if bn.use_deform]


def read_weights(ob, names):
    col = {n: i for i, n in enumerate(names)}
    gmap = {g.index: col.get(g.name) for g in ob.vertex_groups}
    W = np.zeros((len(ob.data.vertices), len(names)), dtype=np.float64)
    for v in ob.data.vertices:
        for g in v.groups:
            c = gmap.get(g.group)
            if c is not None:
                W[v.index, c] = g.weight
    return W


def write_weights(ob, names, W, eps=1e-4):
    for g in list(ob.vertex_groups):
        ob.vertex_groups.remove(g)
    for c, n in enumerate(names):
        vg = ob.vertex_groups.new(name=n)
        nz = np.nonzero(W[:, c] > eps)[0]
        w = W[nz, c]
        # group equal weights (rigid sets, 1.0) into one call
        ones = nz[w >= 0.9999]
        if len(ones):
            vg.add(ones.tolist(), 1.0, "REPLACE")
        for i, wi in zip(nz[w < 0.9999].tolist(), w[w < 0.9999].tolist()):
            vg.add([i], float(wi), "REPLACE")


def limit_normalize(W, k=4):
    W = np.where(W > 1e-6, W, 0.0)
    if W.shape[1] > k:
        part = np.argpartition(-W, k, axis=1)[:, k:]
        np.put_along_axis(W, part, 0.0, axis=1)
    s = W.sum(1, keepdims=True)
    ok = s[:, 0] > 1e-9
    W[ok] /= s[ok]
    return W


def fill_unweighted(verts, W):
    """Vertices without weights copy the weights of the nearest weighted vertex (islands such as
    eyes, buttons, loose props)."""
    empty = W.sum(1) < 1e-6
    if not empty.any() or empty.all():
        return int(empty.sum())
    good = np.nonzero(~empty)[0]
    kd = KDTree(len(good))
    for i, gi in enumerate(good.tolist()):
        kd.insert(verts[gi], i)
    kd.balance()
    for vi in np.nonzero(empty)[0].tolist():
        _, i, _ = kd.find(verts[vi])
        W[vi] = W[good[i]]
    return int(empty.sum())


def _union_labels(n, pairs):
    parent = np.arange(n)

    def find(a):
        r = a
        while parent[r] != r:
            r = parent[r]
        while parent[a] != r:
            parent[a], a = r, parent[a]
        return r
    for a, b_ in pairs:
        ra, rb = find(int(a)), find(int(b_))
        if ra != rb:
            parent[ra] = rb
    return np.array([find(i) for i in range(n)])


def small_islands(body, H, max_frac=0.01, link=0.006):
    """Small loose pieces of the body (bone_kit finger bones, clasps, beads, buttons, eye spheres):
    connected components (vertices closer than 1e-4 H count as joined - glTF splits along UV seams)
    with fewer than max_frac of all vertices. Blender's bone heat solver fails on such islands (a
    closed piece inside a fist sees no bone and the whole solve returns nothing), so they are kept
    out of the proxy and bound afterwards. Islands closer than link x H to each other form one
    cluster (a skeletal hand of 27 bones is one cluster). Returns (mask, [cluster vertex index
    arrays])."""
    co = mesh_verts(body)
    n = len(co)
    mask = np.zeros(n, dtype=bool)
    if n == 0 or max_frac <= 0:
        return mask, []
    ed = mesh_edges(body)
    key = np.round(co / (1e-4 * H)).astype(np.int64)
    _, inv = np.unique(key, axis=0, return_inverse=True)
    inv = inv.ravel()
    order = np.argsort(inv, kind="stable")
    same = order[1:][inv[order[1:]] == inv[order[:-1]]]
    prev = order[:-1][inv[order[1:]] == inv[order[:-1]]]
    pairs = np.concatenate([ed, np.stack([prev, same], 1)]) if len(same) else ed
    lab = _union_labels(n, pairs.tolist())
    uniq, li, counts = np.unique(lab, return_inverse=True, return_counts=True)
    small_ids = np.nonzero(counts < max_frac * n)[0]
    if not len(small_ids) or len(small_ids) == len(uniq):
        return mask, []
    mask = np.isin(li, small_ids)
    # cluster the small islands by proximity (bbox gap < link * H)
    boxes = []
    for k in small_ids.tolist():
        pts = co[li == k]
        boxes.append((k, pts.min(0), pts.max(0)))
    gap = link * H
    pairs2 = []
    if len(boxes) > 1500:   # very fragmented input: every island is its own cluster
        boxes_iter = []
    else:
        boxes_iter = range(len(boxes))
    for i in boxes_iter:
        for j in range(i + 1, len(boxes)):
            a, b_ = boxes[i], boxes[j]
            if np.all(a[1] - gap <= b_[2]) and np.all(b_[1] - gap <= a[2]):
                pairs2.append((i, j))
    cl = _union_labels(len(boxes), pairs2)
    clusters = []
    for c in np.unique(cl).tolist():
        ids = [boxes[i][0] for i in np.nonzero(cl == c)[0].tolist()]
        clusters.append(np.nonzero(np.isin(li, ids))[0])
    return mask, clusters


def bind_islands(W, names, rig, verts, clusters):
    """Each small-island cluster rides rigidly on the deform bone nearest to its centroid
    (a skeletal hand on Hand, a clasp on the spine, an eye on Head). Returns per-cluster info."""
    col = {n: i for i, n in enumerate(names)}
    segs = []
    for n in names:
        bn = rig.data.bones[n]
        segs.append((n, np.array(rig.matrix_world @ bn.head_local), np.array(rig.matrix_world @ bn.tail_local)))
    out = []
    for idx in clusters:
        c = verts[idx].mean(0)
        best = min(segs, key=lambda s_: float(_seg_dist(c[None], s_[1], s_[2])[0][0]))
        W[idx] = 0.0
        W[idx, col[best[0]]] = 1.0
        out.append({"vertices": int(len(idx)), "centroid": rnd(c, 3), "bone": short(best[0])})
    return out


def make_proxy(body, H, target_tris, voxel=False, drop=None):
    """Welded (and decimated / voxel-remeshed) copy of the body used for bone heat. drop: vertex
    mask of the body to leave out (small islands)."""
    me = body.data.copy()
    proxy = bpy.data.objects.new("AF_Proxy", me)
    bpy.context.scene.collection.objects.link(proxy)
    proxy.vertex_groups.clear()
    if drop is not None and drop.any():
        bm = bmesh.new()
        bm.from_mesh(me)
        bm.verts.ensure_lookup_table()
        bmesh.ops.delete(bm, geom=[bm.verts[i] for i in np.nonzero(drop)[0].tolist()], context="VERTS")
        bm.to_mesh(me)
        bm.free()
        me.update()
    weld(proxy, 1e-4 * H)
    if voxel:
        mod = proxy.modifiers.new("AF_Remesh", "REMESH")
        mod.mode = "VOXEL"
        mod.voxel_size = H / 170.0
        mod.adaptivity = 0.0
        select_only(proxy)
        bpy.ops.object.modifier_apply(modifier=mod.name)
    tris = tri_count(proxy)
    if tris > target_tris:
        mod = proxy.modifiers.new("AF_Decimate", "DECIMATE")
        mod.ratio = max(0.02, target_tris / tris)
        select_only(proxy)
        bpy.ops.object.modifier_apply(modifier=mod.name)
    proxy["af_proxy_repair"] = repair_for_heat(proxy, H)
    return proxy


def repair_for_heat(ob, H):
    """Blender's heat solver gives up on the whole mesh (every vertex unweighted) when a few edges
    have 3+ faces or faces are degenerate (AI meshes, welded touching layers, slit caps meeting the
    source's own non-manifold spots). The proxy only has to carry weights for the Data Transfer, so
    the faces around such edges are dropped (up to 3 passes), degenerate geometry is dissolved and
    loose vertices removed. Returns the counts."""
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    out = {"dissolved": 0, "nonmanifold_faces": 0, "loose_verts": 0}
    n0 = len(bm.faces)
    bmesh.ops.dissolve_degenerate(bm, dist=1e-6 * H, edges=bm.edges[:])
    tiny = [f for f in bm.faces if f.calc_area() < (1e-6 * H) ** 2]
    if tiny:
        bmesh.ops.delete(bm, geom=tiny, context="FACES")
    out["dissolved"] = n0 - len(bm.faces)
    for _ in range(3):
        nm = [e for e in bm.edges if len(e.link_faces) > 2]
        if not nm:
            break
        faces = list({f for e in nm for f in e.link_faces})
        out["nonmanifold_faces"] += len(faces)
        bmesh.ops.delete(bm, geom=faces, context="FACES")
    lone = [v for v in bm.verts if not v.link_faces]
    if lone:
        out["loose_verts"] = len(lone)
        bmesh.ops.delete(bm, geom=lone, context="VERTS")
    bm.to_mesh(ob.data)
    bm.free()
    ob.data.update()
    return out


def bone_heat(proxy, rig):
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    proxy.select_set(True)
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.parent_set(type="ARMATURE_AUTO")


def transfer_weights(src, dst, names):
    """Data Transfer modifier (nearest face, interpolated) src -> dst vertex groups."""
    dst.vertex_groups.clear()
    mod = dst.modifiers.new("AF_WeightXfer", "DATA_TRANSFER")
    mod.object = src
    mod.use_object_transform = True
    mod.use_vert_data = True
    mod.data_types_verts = {"VGROUP_WEIGHTS"}
    mod.vert_mapping = "POLYINTERP_NEAREST"
    mod.layers_vgroup_select_src = "ALL"
    mod.layers_vgroup_select_dst = "NAME"
    select_only(dst)
    bpy.ops.object.datalayout_transfer(modifier=mod.name)
    bpy.ops.object.modifier_apply(modifier=mod.name)


def smooth_weights(edges, W, factor=0.5, iters=2):
    """Uniform Laplacian smoothing of all weight columns over mesh edges (removes the noisy
    fringes bone heat leaves on cloth and AI geometry)."""
    n = len(W)
    deg = np.zeros(n)
    np.add.at(deg, edges[:, 0], 1.0)
    np.add.at(deg, edges[:, 1], 1.0)
    has = deg > 0
    for _ in range(int(iters)):
        acc = np.zeros_like(W)
        np.add.at(acc, edges[:, 0], W[edges[:, 1]])
        np.add.at(acc, edges[:, 1], W[edges[:, 0]])
        avg = np.where(has[:, None], acc / np.maximum(deg, 1)[:, None], W)
        W = (1.0 - factor) * W + factor * avg
    return W


def _seg_dist(P_, a, b_):
    ab = b_ - a
    t = np.clip(((P_ - a) @ ab) / max(float(ab @ ab), 1e-12), 0.0, 1.0)
    return np.linalg.norm(P_ - (a + np.outer(t, ab)), axis=1), t


def arm_radii(lm):
    """Arm tube radii (metres) from the free arm-chain cross-sections, when the arm is visible."""
    out = {}
    for side in SIDES:
        a = lm.arm.get(side) or {}
        line = a.get("line")
        if not line or a.get("mode") not in ("chain", "tpose", "grip", "shield"):
            continue
        S_, E_, W_ = a["S"], a["E"], a["W"]
        pts = np.array([l_[1] for l_ in line])
        rad = np.array([0.5 * min(l_[3], l_[4]) for l_ in line])
        d_up, t_up = _seg_dist(pts, S_, E_)
        d_fo, t_fo = _seg_dist(pts, E_, W_)
        up = (d_up < d_fo) & (t_up > 0.2) & (t_up < 0.9)
        fo = (d_fo <= d_up) & (t_fo > 0.1) & (t_fo < 0.9)
        r = {}
        if up.sum() >= 4:
            r["Arm"] = float(np.median(rad[up])) * lm.H
        if fo.sum() >= 4:
            r["ForeArm"] = float(np.median(rad[fo])) * lm.H
        out[side] = r
    return out


def welded_graph(body, H, tol=1e-4):
    """Vertex graph of the body with coincident vertices (closer than tol x H: glTF splits along UV
    seams) merged. Returns (rep: unique index per vertex, unique positions, adjacency lists of
    (neighbour, edge length))."""
    co = mesh_verts(body)
    key = np.round(co / (tol * H)).astype(np.int64)
    _, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    inv = inv.ravel()
    U = co[first]
    ed = mesh_edges(body)
    e2 = inv[ed]
    e2 = e2[e2[:, 0] != e2[:, 1]]
    e2 = np.unique(np.sort(e2, axis=1), axis=0)
    L = np.linalg.norm(U[e2[:, 0]] - U[e2[:, 1]], axis=1)
    adj = [[] for _ in range(len(U))]
    for (a, b_), l_ in zip(e2.tolist(), L.tolist()):
        adj[a].append((b_, l_))
        adj[b_].append((a, l_))
    return inv, U, adj


def surface_distance(adj, seeds, init=None, max_d=float("inf")):
    """Multi-source Dijkstra over the vertex graph (surface distance in metres; inf = unreached)."""
    import heapq
    n = len(adj)
    dist = np.full(n, np.inf)
    pq = []
    for i in np.nonzero(seeds)[0].tolist():
        d0 = 0.0 if init is None else float(init[i])
        dist[i] = d0
        pq.append((d0, i))
    heapq.heapify(pq)
    while pq:
        d, u = heapq.heappop(pq)
        if d > dist[u] or d > max_d:
            continue
        for v, w in adj[u]:
            nd = d + w
            if nd < dist[v]:
                dist[v] = nd
                heapq.heappush(pq, (nd, v))
    return dist


def limb_surface_distances(body, rig, H, skip=None, clear=0.6, cap=0.15):
    """Surface (geodesic) distances of every vertex to the arm of each side, the torso and the legs.
    Seeds of a bone chain are the vertices that clearly belong to it: closer to it than clear x the
    distance to any other chain, and at most cap x H away; they start with their straight distance
    to the bones and Dijkstra over the welded vertex graph does the rest. A bell sleeve slit off
    the robe side (separate kind "slit") is then far from the torso and the thighs along the
    surface although it hangs right next to them; on fused meshes the surface path crosses the
    fusion and the numbers stay close to the straight ones. skip: vertex mask kept out of the seeds
    (small islands bound separately).
    Returns ({"Left", "Right", "torso", "legs": per-vertex distance, inf where unreached}, info)."""
    t0 = time.time()
    inv, U, adj = welded_graph(body, H)
    keep_u = np.ones(len(U), dtype=bool)
    if skip is not None and skip.any():
        keep_u[inv[skip]] = False
        keep_u[inv[~skip]] = True

    def dist_to(parts, sides):
        e = np.full(len(U), np.inf)
        for sd in sides:
            for p_ in parts:
                n = b(sd + p_) if sd else b(p_)
                if n in rig.data.bones:
                    bn = rig.data.bones[n]
                    e = np.minimum(e, _seg_dist(U, np.array(rig.matrix_world @ bn.head_local),
                                                np.array(rig.matrix_world @ bn.tail_local))[0])
        return e
    chains = {"Left": dist_to(("Arm", "ForeArm", "Hand"), ("Left",)),
              "Right": dist_to(("Arm", "ForeArm", "Hand"), ("Right",)),
              "torso": dist_to(("Hips", "Spine", "Spine1", "Spine2", "Neck"), ("",)),
              "legs": dist_to(("UpLeg", "Leg", "Foot"), SIDES)}
    out, info = {}, {"seeds": {}}
    for k, e in chains.items():
        if not np.isfinite(e).any():
            continue
        other = np.min(np.stack([chains[o] for o in chains if o != k]), axis=0)
        seeds = keep_u & (e <= clear * other) & (e <= cap * H)
        info["seeds"][k] = int(seeds.sum())
        if not seeds.any():
            continue
        out[k] = surface_distance(adj, seeds, init=e)[inv]
    info["seconds"] = round(time.time() - t0, 2)
    info["graph_vertices"] = int(len(U))
    return out, info


def prune_limb_weights(W, names, verts, rig, H, radii=None, geo=None, geo_margin=0.05, geo_band=0.03):
    """Bone heat lets the upper arm bleed into the chest/lats and the forearm into the hips when
    arms hang near the torso. Each arm bone keeps full weight inside its own tube (radius from the
    arm cross-sections, else from the vertices it dominates) and inside the blend sphere of its
    proximal joint, fading smoothly to zero outside. geo (limb_surface_distances): a vertex that is
    closer to its arm than to the torso along the surface by more than geo_margin x H keeps its arm
    weight even outside the tube (a bell sleeve hanging from the forearm), with a smooth band;
    near a fused arm/torso contact the surface distances are similar and the tube rule applies.
    The other way round, a vertex that is closer to the torso than to its arm along the surface by
    more than geo_margin x H loses its arm weight (bleed of a hidden arm into the robe side under a
    slit sleeve: the tube rule cannot see a hidden arm); rows emptied that way take the weights of
    the nearest vertex later (skin: refilled_after_prune)."""
    col = {n: i for i, n in enumerate(names)}
    W0 = W.copy()
    info = {}
    radii = radii or {}
    cut_rows = np.zeros(len(W), dtype=bool)
    for side in SIDES:
        f_cut = None
        if geo and side in geo and "torso" in geo:
            dd = geo[side] - geo["torso"]
            dd = np.where(np.isfinite(dd), dd, -1.0)
            f_cut = 1.0 - smoothstep(geo_margin * H, (geo_margin + geo_band) * H, dd)
        for part, r_min in (("Arm", 0.07), ("ForeArm", 0.05), ("Hand", 0.045)):
            n = b(side + part)
            if n not in col:
                continue
            c = col[n]
            bn = rig.data.bones[n]
            a = np.array(rig.matrix_world @ bn.head_local)
            e = np.array(rig.matrix_world @ bn.tail_local)
            d, _ = _seg_dist(verts, a, e)
            R = radii.get(side, {}).get(part)
            if part == "Hand" and R is None:
                R = radii.get(side, {}).get("ForeArm")
            if R is None:
                # arm hidden (sleeve merged with a robe): no reliable radius - only drop absurd
                # influences far from the bone (loose islands bone heat gave to a hand)
                far = (d > 0.3 * H) & (W[:, c] > 1e-4)
                W[far, c] = 0.0
                rec = {"radius_from": "none (arm not visible)", "dropped_far": int(far.sum())}
                if f_cut is not None:
                    cut = (f_cut < 0.999) & (W[:, c] > 1e-4)
                    cut_rows |= (f_cut < 0.5) & (W[:, c] > 1e-4)
                    W[:, c] *= f_cut
                    rec["surface_cut"] = int(cut.sum())
                info[short(n)] = rec
                continue
            R = float(np.clip(R, 0.015 * H, 0.09 * H))
            rj = max(r_min * H, 2.0 * R)
            f_tube = 1.0 - smoothstep(1.2 * R + 0.005 * H, 2.0 * R + 0.02 * H, d)
            f_joint = 1.0 - smoothstep(rj, 1.6 * rj, np.linalg.norm(verts - a, axis=1))
            f = np.maximum(f_tube, f_joint)
            if geo and side in geo and "torso" in geo:
                diff = geo["torso"] - geo[side]
                diff = np.where(np.isfinite(diff), diff, -1.0)
                f = np.maximum(f, smoothstep(geo_margin * H, (geo_margin + geo_band) * H, diff))
            if f_cut is not None:
                cut_rows |= (f_cut < 0.5) & (W[:, c] > 1e-4)
                f = f * f_cut
            touched = (f < 0.999) & (W[:, c] > 1e-4)
            W[:, c] *= f
            info[short(n)] = {"radius_m": round(R, 4), "radius_from": "geometry", "reduced": int(touched.sum())}
    # rows emptied by the falloff were arm-only (wide sleeve, cuff): give them back their weights
    # unless those came from a far bone
    lost = W.sum(1) < 1e-6
    if lost.any():
        ok = np.ones(len(W), dtype=bool)
        for n, c in col.items():
            bn = rig.data.bones[n]
            dd, _ = _seg_dist(verts, np.array(rig.matrix_world @ bn.head_local),
                              np.array(rig.matrix_world @ bn.tail_local))
            ok &= ~((W0[:, c] > 1e-4) & (dd > 0.3 * H))
        back = lost & ok & ~cut_rows
        W[back] = W0[back]
        info["restored_rows"] = int(back.sum())
        info["rows_left_to_refill"] = int((lost & ~back).sum())
    return W, info


def squash_arm_bleed(W, names, lo=0.10, hi=0.35, sides=SIDES):
    """Residual arm weight on torso-dominated vertices (bone heat diffusing out of the armpit into a
    robe side or a chest) is faded with a soft ramp: an arm chain share a of a vertex whose torso
    share is larger is multiplied by smoothstep(lo, hi, a). Shares above hi (the real shoulder and
    armpit blend) are untouched and the ramp is continuous, so no new edge appears; the removed
    weight goes to the torso bones at the final normalisation. Only for `sides` - skin() passes the
    arms whose upper arm is hidden (no radius from the arm cross-sections: a sleeve fused with a robe);
    a visible arm is cleaned by the tube prune and keeps its smooth shoulder blend. Returns the
    vertices changed."""
    col = {n: i for i, n in enumerate(names)}
    torso_cols = [col[n] for n in names if short(n) in ("Hips", "Spine", "Spine1", "Spine2", "Neck", "Head") or
                  any(short(n) == sd + p_ for sd in SIDES for p_ in ("UpLeg", "Leg", "Foot", "ToeBase"))]
    t = W[:, torso_cols].sum(1)
    changed = 0
    for side in sides:
        ac = [col[b(side + p_)] for p_ in ("Arm", "ForeArm", "Hand") if b(side + p_) in col]
        if not ac:
            continue
        a = W[:, ac].sum(1)
        f = np.where((t > a) & (a > 1e-6), smoothstep(lo, hi, a), 1.0)
        changed += int((f < 0.999).sum())
        W[:, ac] *= f[:, None]
    return changed


def separate_arms_legs(W, names, verts, rig, H, band=0.02, geo=None):
    """A vertex should not be driven by an arm and a leg at once (heat diffuses through a hand,
    sleeve or forearm that touches the thigh/robe on fused AI meshes). Where both chains carry
    weight, the chain whose bones are farther away is faded out over +-band H around the
    equidistant surface."""
    col = {n: i for i, n in enumerate(names)}

    def chain_dist(parts, sides=SIDES):
        d = np.full(len(verts), 1e9)
        for sd in sides:
            for p in parts:
                n = b(sd + p)
                if n in rig.data.bones:
                    bn = rig.data.bones[n]
                    dd, _ = _seg_dist(verts, np.array(bn.head_local), np.array(bn.tail_local))
                    d = np.minimum(d, dd)
        return d
    leg_cols = [col[b(sd + p)] for sd in SIDES for p in ("UpLeg", "Leg", "Foot", "ToeBase") if b(sd + p) in col]
    d_leg = chain_dist(("UpLeg", "Leg", "Foot"))
    changed = 0
    for side in SIDES:
        arm_cols = [col[n] for n in names if n.startswith(PFX + side) and
                    any(k in n for k in ("Arm", "ForeArm", "Hand"))]
        A = W[:, arm_cols].sum(1)
        Lg = W[:, leg_cols].sum(1)
        both = (A > 0.03) & (Lg > 0.03)
        if not both.any():
            continue
        d_arm = chain_dist(("ForeArm", "Hand"), (side,))
        if geo and side in geo and "legs" in geo:
            # along the surface: a sleeve slit off the robe is far from the thighs although it hangs
            # next to them (unreached vertices keep the straight distances)
            ga, gl = geo[side], geo["legs"]
            ok = np.isfinite(ga) & np.isfinite(gl)
            d_arm = np.where(ok, np.maximum(ga, d_arm), d_arm)
            dl = np.where(ok, np.maximum(gl, d_leg), d_leg)
        else:
            dl = d_leg
        f_arm = smoothstep(-band * H, band * H, dl - d_arm)
        fa = np.where(both, f_arm, 1.0)
        fl = np.where(both, 1.0 - f_arm, 1.0)
        W[:, arm_cols] *= fa[:, None]
        W[:, leg_cols] *= fl[:, None]
        changed += int(both.sum())
    return changed


def weight_report(W, names, verts=None):
    empty = int((W.sum(1) < 1e-6).sum())
    col = W.sum(0)
    missing = [n for n, c in zip(names, col) if c < 1e-6]
    return {"unweighted": empty, "unweighted_frac": empty / max(1, len(W)), "empty_bones": missing}


CORE_BONES = {b(n) for n in ("Hips", "Spine", "Spine1", "Spine2", "Neck", "Head") +
              tuple(s + p for s in SIDES for p in ("Arm", "ForeArm", "Hand", "UpLeg", "Leg"))}


def skin(body, rig, H, params, warnings, log=None, radii=None):
    """Bone heat on a welded proxy (<= proxy_tris), fallback voxel-remesh proxy, Data Transfer
    to the full mesh, nearest fill. Returns (names, W, info) - W not yet limited/normalised."""
    names = deform_names(rig)
    target = int(params.get("proxy_tris", 60000))
    info = {"proxy_tris_target": target}
    island_frac = float(params.get("island_max_frac", 0.01))
    drop, clusters = small_islands(body, H, island_frac)
    info["small_islands"] = {"vertices": int(drop.sum()), "clusters": len(clusters), "max_frac": island_frac}
    # Blender's heat solver sometimes fails on dense AI meshes and succeeds on a coarser proxy of
    # the same surface; the voxel remesh (watertight, but fuses parts closer than ~1 cm) is last
    tries = [("bone_heat_proxy", False, target), ("bone_heat_proxy_1/2", False, target // 2),
             ("bone_heat_proxy_1/4", False, target // 4), ("bone_heat_voxel_proxy", True, target)]
    best = None
    for label, vox, tgt in tries:
        t0 = time.time()
        if not vox and tgt < target and tri_count(body) <= tgt:
            continue
        proxy = make_proxy(body, H, tgt, voxel=vox, drop=drop)
        try:
            bone_heat(proxy, rig)
        except RuntimeError as exc:
            warnings.append("%s failed: %s" % (label, exc))
        Wp = read_weights(proxy, names)
        rep = weight_report(Wp, names)
        missing_core = [n for n in rep["empty_bones"] if n in CORE_BONES]
        rep.update({"method": label, "proxy_tris": tri_count(proxy), "proxy_verts": len(proxy.data.vertices),
                    "repair": dict(proxy.get("af_proxy_repair") or {}),
                    "seconds": round(time.time() - t0, 1), "missing_core": missing_core})
        if log:
            log("bone heat", **{k: v for k, v in rep.items() if k != "empty_bones"})
        good = rep["unweighted_frac"] < 0.002 and not missing_core
        score = rep["unweighted_frac"] + 0.1 * len(missing_core)
        if best is None or score < best[0]:
            if best is not None:
                bpy.data.objects.remove(best[1], do_unlink=True)
            best = (score, proxy, rep)
        else:
            bpy.data.objects.remove(proxy, do_unlink=True)
        info.setdefault("attempts", []).append({k: v for k, v in rep.items() if k != "empty_bones"})
        if good:
            break
        warnings.append("%s left %.2f%% vertices unweighted%s; trying fallback" % (
            label, rep["unweighted_frac"] * 100,
            (" and bones without weights: " + ", ".join(short(n) for n in missing_core)) if missing_core else ""))
    _, proxy, rep = best
    info["method"] = rep["method"]
    # smooth on the welded proxy, then proxy -> full mesh
    proxy.parent = None
    for m in list(proxy.modifiers):
        proxy.modifiers.remove(m)
    it = int(params.get("smooth_iterations", 0))
    if it > 0:
        Wp = read_weights(proxy, names)
        Wp = limit_normalize(smooth_weights(mesh_edges(proxy), Wp, 0.5, it), len(names))
        write_weights(proxy, names, Wp)
        info["smooth_iterations"] = it
    transfer_weights(proxy, body, names)
    bpy.data.objects.remove(proxy, do_unlink=True)
    W = read_weights(body, names)
    verts = mesh_verts(body)
    if clusters:
        W[drop] = 0.0
    info["filled_from_nearest"] = fill_unweighted(verts, W)
    if params.get("prune_limbs", True):
        geo = None
        if params.get("surface_aware", True):
            geo, info["surface_distances"] = limb_surface_distances(body, rig, H, skip=drop if clusters else None)
            if log:
                log("surface distances", **info["surface_distances"])
        W, info["limb_prune"] = prune_limb_weights(W, names, verts, rig, H, radii, geo=geo,
                                                   geo_margin=float(params.get("surface_margin_h", 0.05)))
        info["arm_leg_separated"] = separate_arms_legs(W, names, verts, rig, H, geo=geo)
        bleed = params.get("arm_bleed", (0.10, 0.35))
        if bleed:
            hidden = [sd for sd in SIDES if not (radii or {}).get(sd, {}).get("Arm")]
            info["arm_bleed_squashed"] = squash_arm_bleed(W, names, float(bleed[0]), float(bleed[1]),
                                                          sides=hidden) if hidden else 0
            info["arm_bleed_sides"] = hidden
        # rows emptied by the pruning (e.g. a loose island bone heat gave to a far hand) take the
        # weights of the nearest valid vertex
        info["refilled_after_prune"] = fill_unweighted(verts, W)
    if clusters:
        info["small_islands"]["bound"] = bind_islands(W, names, rig, verts, clusters)
    return names, W, info


# ---------------------------------------------------------------- rigid props

def prop_vertex_sets(lm, verts, P_m, H):
    """Rigid sets for every auto-detected prop (bone from Landmarks.bind_props: gripping hand,
    nearest hand or nearest trunk bone) and plate: [(bone, vertex mask, info)]."""
    out = []
    if not lm.props and not getattr(lm, "plate_props", None) and not getattr(lm, "bow_props", None):
        return out
    P = P_m / H
    labels = np.full(len(P), -1, dtype=np.int64)
    items = []
    # bows first: their samples are exactly known (find_bows), nothing else may claim them
    for bw in getattr(lm, "bow_props", []):
        m = np.zeros(len(P), dtype=bool)
        m[bw["idx"]] = True
        items.append(("bow", bw["bind"], m, bw))
    for p in lm.props:
        rel = P - p["c"]
        t = rel @ p["d"]
        dist = np.linalg.norm(rel - np.outer(t, p["d"]), axis=1)
        m = lm.prop_mask & (dist < 0.12) & (t > p["t"][0] - 0.2) & (t < p["t"][1] + 0.2)
        items.append(("prop", p.get("bind") or (p.get("side", "Right") + "Hand"), m, p))
    for pl in getattr(lm, "plate_props", []):
        rel = P - pl["centre"]
        dn = np.abs(rel @ pl["normal"])
        inp = np.linalg.norm(rel - np.outer(rel @ pl["normal"], pl["normal"]), axis=1)
        m = lm.prop_mask & (dn < 0.07) & (inp < pl["radius"] * 1.15)
        items.append(("plate", pl["side"] + "Hand", m, pl))
    for k, (_, _, m, _) in enumerate(items):
        labels[m & (labels < 0)] = k
    kd = KDTree(len(P_m))
    for i, p in enumerate(P_m.tolist()):
        kd.insert(p, i)
    kd.balance()
    vlab = np.full(len(verts), -1, dtype=np.int64)
    for vi, v in enumerate(verts.tolist()):
        near = kd.find_n(v, 6)
        if not near or near[0][2] > 0.006 * H:
            continue
        labs = [labels[i] for _, i, _ in near]
        best = max(set(labs), key=labs.count)
        if best >= 0 and labs.count(best) >= 4:
            vlab[vi] = best
    # along the shaft a vertex must also lie inside the shaft radius (robe/sleeve skin next to a
    # staff is not part of it); beyond the shaft ends (ornaments) the sample vote decides
    V = verts / H
    for k, (kind, side, _, p) in enumerate(items):
        if kind != "prop":
            continue
        rel = V - p["c"]
        t = rel @ p["d"]
        dist = np.linalg.norm(rel - np.outer(t, p["d"]), axis=1)
        along = (t > p["t"][0] + 0.02) & (t < p["t"][1] - 0.02)
        vlab[(vlab == k) & along & (dist > p["r_cap"] + 0.006)] = -1
        # vertices inside the shaft capsule that no sample voted for (long thin blade triangles have
        # few samples near their corners; a blade tip left out would stay behind as a sliver),
        # except around the grip where the fist wraps the shaft
        ex = p.get("extent", p["t"])
        lo_end, hi_end = min(p["t"][0], ex[0]), max(p["t"][1], ex[1])
        e_lo = e_hi = 0.02          # the tapering tip beyond the 0.2 % extent percentile
        if p.get("grip") is not None:
            tg = float((np.asarray(p["grip"]) - p["c"]) @ p["d"])
            if tg > 0.5 * (lo_end + hi_end):
                e_hi = 0.005        # the grip end: the wrist is right there
            else:
                e_lo = 0.005
        inside = (dist < p["r_cap"]) & (t > lo_end - e_lo) & (t < hi_end + e_hi) & (vlab < 0)
        if p.get("grip") is not None:
            inside &= np.abs(t - tg) > 0.05
        vlab[inside] = k
    # a plate whose plane contains the arm (a bow held at the side) must not swallow the arm: its
    # vertices inside the upper-arm / forearm tube stay with the arm
    J = getattr(lm, "j", None)
    for k, (kind, bone, _, p) in enumerate(items):
        if kind != "plate" or J is None:
            continue
        sd = p["side"]
        tube = np.zeros(len(V), dtype=bool)
        for a_, b_ in ((sd + "Arm", sd + "ForeArm"), (sd + "ForeArm", sd + "Hand")):
            dd, tt = _seg_dist(V, np.asarray(J[a_]), np.asarray(J[b_]))
            tube |= (dd < 0.04) & (tt < 0.97)
        vlab[(vlab == k) & tube] = -1
    for k, (kind, bone, _, p) in enumerate(items):
        vm = vlab == k
        side = p.get("side")
        rec = {"kind": kind, "side": side, "bone": b(bone), "vertices": int(vm.sum())}
        if kind == "prop":
            ex = p.get("extent", p["t"])
            ends = [p["c"] + p["d"] * ex[0], p["c"] + p["d"] * ex[1]]
            rec.update({"axis_from": rnd(ends[0] * H), "axis_to": rnd(ends[1] * H),
                        "length_m": round(float(ex[1] - ex[0]) * H, 3),
                        "bind_mode": p.get("bind_mode"),
                        "grip": rnd(np.asarray(p["grip"]) * H) if p.get("grip") is not None else None})
        elif kind == "bow":
            rec.update({"shape": p["kind"], "tip_low": rnd(p["tips"][0] * H), "tip_high": rnd(p["tips"][1] * H),
                        "length_m": round(float(p["length"]) * H, 3), "sagitta_m": round(float(p["sagitta"]) * H, 3),
                        "bind_mode": p.get("bind_mode"), "grip": rnd(np.asarray(p["grip"]) * H)})
        else:
            rec.update({"centre": rnd(p["centre"] * H), "radius_m": round(float(p["radius"]) * H, 3)})
        out.append((b(bone), vm, rec))
    return out


def rigid_rule_sets(verts, rules):
    """Manual rigid rules (ported from an earlier in-house rig script):
      {"bone": "RightHand", "axis": [x, y], "r": 0.03, "r_top": 0.05, "z_top": 1.5}
      {"bone": "LeftForeArm", "x_min": 0.5, "merge": ["LeftHand"], "z_min": .., "z_max": ..}
    Metres in the normalised frame (faces -Y, feet on 0, character left = +X)."""
    out = []
    for rule in rules or []:
        p = verts
        if "axis" in rule:
            r = np.where(p[:, 2] >= rule.get("z_top", 1e9), rule.get("r_top", rule["r"]), rule["r"])
            ok = np.hypot(p[:, 0] - rule["axis"][0], p[:, 1] - rule["axis"][1]) < r
            ok &= (p[:, 2] > rule.get("z_min", -1e9)) & (p[:, 2] < rule.get("z_max", 1e9))
        else:
            ok = ((p[:, 0] > rule.get("x_min", -1e9)) & (p[:, 0] < rule.get("x_max", 1e9)) &
                  (p[:, 1] > rule.get("y_min", -1e9)) & (p[:, 1] < rule.get("y_max", 1e9)) &
                  (p[:, 2] > rule.get("z_min", -1e9)) & (p[:, 2] < rule.get("z_max", 1e9)))
        out.append((rule, ok))
    return out


def apply_rigid(W, names, sets):
    """Make each set rigid (weight 1.0) to its bone. Ported from an earlier in-house rig script:
    'merge' bones pour their weights into the rule bone first."""
    col = {n: i for i, n in enumerate(names)}
    counts = []
    for bone, mask, rec in sets:
        bi = col[bone]
        for mname in rec.get("merge", []):
            mi = col.get(b(short(mname)))
            if mi is not None:
                W[:, bi] += W[:, mi]
                W[:, mi] = 0.0
        W[mask] = 0.0
        W[mask, bi] = 1.0
        counts.append(dict(rec, vertices=int(mask.sum())))
    return counts


def robe_blend(W, names, verts, lm, rig, H, hips_share=0.35, feet=False):
    """Robe/skirt. Cloth away from the legs (farther than ~5-9 % H from the leg bones) is driven
    only by Hips and the two thighs: all its leg weight (thigh, shin, foot) is redistributed as
    hips_share to Hips and the rest to Left/RightUpLeg with a smooth left/right blend whose band is
    0.7 x the robe half-width at that height. The cloth then swings with the thighs and stretches
    across the front/back panels instead of tearing between the feet in a stride. Vertices tight
    on a leg (shoes poking out, trousers) keep their bone-heat weights."""
    col = {n: i for i, n in enumerate(names)}
    x = verts[:, 0] - lm.X0 * H
    z = verts[:, 2]
    ztop = (lm.crotch + 0.07) * H
    zone = z < ztop
    fade = np.clip((ztop - z) / (0.05 * H), 0.0, 1.0)
    # "tight" = on a leg (trousers, shoes poking out): within a part-specific radius of the bone
    tight = np.zeros(len(verts))
    # robe_feet=false: toes fused with the hem ride with the cloth instead of tearing it
    radii = (("UpLeg", 0.06), ("Leg", 0.04), ("Foot", 0.03), ("ToeBase", 0.025)) if feet else \
        (("UpLeg", 0.05), ("Leg", 0.03))
    for side in SIDES:
        for part, r0 in radii:
            bn = rig.data.bones[b(side + part)]
            d, _ = _seg_dist(verts, np.array(bn.head_local), np.array(bn.tail_local))
            tight = np.maximum(tight, 1.0 - smoothstep(r0 * H, (r0 + (0.03 if feet else 0.015)) * H, d))
    cloth = (1.0 - tight) * fade
    # robe half width per height band
    hw = np.full(len(verts), 0.1 * H)
    edges = np.linspace(0.0, ztop, 15)
    for z0, z1 in zip(edges[:-1], edges[1:]):
        m = (z >= z0) & (z < z1 + 1e-9)
        if m.sum() > 10:
            hw[m] = max(0.05 * H, float(np.percentile(np.abs(x[m]), 95)))
    band = 0.7 * hw
    s = smoothstep(-band, band, x)
    leg_cols = [col[b(sd + p)] for sd in SIDES for p in ("UpLeg", "Leg", "Foot", "ToeBase") if b(sd + p) in col]
    T = W[:, leg_cols].sum(1)
    new = np.zeros_like(W)
    new[:, col[b("Hips")]] = T * hips_share
    new[:, col[b("LeftUpLeg")]] = T * (1 - hips_share) * s
    new[:, col[b("RightUpLeg")]] = T * (1 - hips_share) * (1 - s)
    old_leg = np.zeros_like(W)
    old_leg[:, leg_cols] = W[:, leg_cols]
    w = cloth[:, None]
    W[:] = W - w * old_leg + w * new
    return {"vertices_blended": int((cloth > 0.01).sum()), "hips_share": hips_share,
            "band_m_at_hem": round(float(2 * band[z < 0.1 * H].mean()) if (z < 0.1 * H).any() else 0.0, 3)}


# =================================================================== hard-surface armour

# bones whose plates ride rigidly (helmet, breastplate, faulds, pauldron/rerebrace, vambrace, gauntlet,
# cuisse, greave, sabaton); Neck / Shoulder (clavicle) areas are joints and keep smooth weights
HARD_BONES = ("Head", "Spine", "Spine1", "Spine2", "Hips") + tuple(
    s + p for s in SIDES for p in ("Arm", "ForeArm", "Hand", "UpLeg", "Leg", "Foot"))
HARD = {"sat_max": 0.28, "val_min": 0.22, "val_max": 0.97, "core_w": 0.78, "min_region": 30,
        "auto_metal_frac": 0.35, "band_rings": 8, "relax_iters": 40}


def vertex_colors_from_texture(body):
    """Per-vertex sRGB colour of the painted base colour (texture at each face's mean UV, area-weighted over
    the faces around the vertex). None without a texture."""
    from steps import _props_lib as PL
    if PL.base_color_image(body) is None:
        return None
    col, area = PL.face_colors(body)
    me = body.data
    nv = len(me.vertices)
    acc = np.zeros((nv, 3))
    wsum = np.zeros(nv)
    for fi, poly in enumerate(me.polygons):
        for vi in poly.vertices:
            acc[vi] += col[fi] * area[fi]
            wsum[vi] += area[fi]
    return acc / np.maximum(wsum, 1e-12)[:, None]


def metal_mask(cols):
    """Grey / steel / silver (low saturation, mid-to-high value) - painted plate armour (and bone)."""
    mx = cols.max(1)
    mn = cols.min(1)
    sat = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    return (sat < HARD["sat_max"]) & (mx > HARD["val_min"]) & (mx < HARD["val_max"])


def hard_surface(body, W, names, H, params, exclude=None, log=None):
    """Rigid plate armour. params.hard_surface: auto | on | off. The metal mask comes from the painted base
    colour; 'auto' turns on for build armored / heavy or when metal covers > 35 % of the surface (robed /
    light builds: > 50 %). Plate regions = connected metal vertices (welded graph) whose dominant bone is one
    of HARD_BONES with weight >= 0.72: each region (>= 30 vertices) gets weight 1.0 on that bone. The joint
    gaps (metal vertices between plates, up to 6 rings from them) are relaxed harmonically with the plates
    fixed (no weight jump at a plate edge); cloth, skin and everything else keep their smooth weights;
    exclude (rigid props) is never touched. Modifies W in place; returns the report."""
    mode = str(params.get("hard_surface", "auto")).lower()
    rep_ = {"mode": mode, "enabled": False}
    if mode == "off":
        return rep_
    cols = vertex_colors_from_texture(body)
    if cols is None:
        rep_["reason"] = "no painted base colour texture"
        return rep_
    me = body.data
    area = np.zeros(len(me.vertices))
    for poly in me.polygons:
        a = poly.area / max(len(poly.vertices), 1)
        for vi in poly.vertices:
            area[vi] += a
    metal = metal_mask(cols)
    if exclude is not None:
        metal &= ~exclude
    frac = float(area[metal].sum() / max(area.sum(), 1e-12))
    build = str(params.get("build") or "").lower()
    need = 0.5 if build in ("robed", "light") else HARD["auto_metal_frac"]
    rep_.update({"metal_area_frac": round(frac, 3), "build": build or None, "_metal": metal})
    if mode == "auto" and not (build in ("armored", "heavy") or frac > need):
        rep_["reason"] = "auto: build %s, metal %.0f %% of the surface (needs armored/heavy or > %.0f %%)" % (
            build or "-", 100 * frac, 100 * need)
        return rep_
    inv, U, adj = welded_graph(body, H)
    nU = len(U)
    # unique-vertex domain: metal if any copy is metal; weights of the first copy
    first = np.full(nU, -1, dtype=np.int64)
    for v, u in enumerate(inv.tolist()):
        if first[u] < 0:
            first[u] = v
    um = np.zeros(nU, dtype=bool)
    um[inv[metal]] = True
    Wu = W[first].copy()
    hard_cols = [names.index(b(n)) for n in HARD_BONES if b(n) in names]
    dom = np.argmax(Wu, axis=1)
    wmax = Wu[np.arange(nU), dom]
    core = um & (wmax >= HARD["core_w"]) & np.isin(dom, hard_cols)
    ex_u = np.zeros(nU, dtype=bool)
    if exclude is not None:
        ex_u[inv[exclude]] = True
        core &= ~ex_u
    lab = np.full(nU, -1, dtype=np.int64)
    regions = []
    for st in np.nonzero(core)[0].tolist():
        if lab[st] >= 0:
            continue
        rid = len(regions)
        stack, comp = [st], []
        lab[st] = rid
        while stack:
            u = stack.pop()
            comp.append(u)
            for v, _ in adj[u]:
                if lab[v] < 0 and core[v] and dom[v] == dom[st]:
                    lab[v] = rid
                    stack.append(v)
        regions.append((int(dom[st]), comp))
    plates = [(bi, comp) for bi, comp in regions if len(comp) >= HARD["min_region"]]
    rigid_u = np.zeros(nU, dtype=bool)
    by_bone = {}
    for bi, comp in plates:
        Wu[comp] = 0.0
        Wu[comp, bi] = 1.0
        rigid_u[comp] = True
        nm = short(names[bi])
        by_bone[nm] = by_bone.get(nm, 0) + len(comp)
    # joint gaps: metal vertices near the plates, relaxed with the plates fixed
    band = np.zeros(nU, dtype=bool)
    front = set(np.nonzero(rigid_u)[0].tolist())
    for _ in range(HARD["band_rings"]):
        nxt = set()
        for u in front:
            for v, _ in adj[u]:
                if not rigid_u[v] and not band[v] and um[v] and not ex_u[v]:
                    band[v] = True
                    nxt.add(v)
        front = nxt
    bidx = np.nonzero(band)[0]
    if len(bidx):
        nbs = [[v for v, _ in adj[u]] for u in bidx.tolist()]
        for _ in range(HARD["relax_iters"]):
            new = Wu[bidx].copy()
            for k, u in enumerate(bidx.tolist()):
                if nbs[k]:
                    new[k] = 0.5 * Wu[u] + 0.5 * Wu[nbs[k]].mean(0)
            Wu[bidx] = new
        Wu[bidx] /= np.maximum(Wu[bidx].sum(1, keepdims=True), 1e-12)
    changed = rigid_u | band
    W[changed[inv]] = Wu[inv][changed[inv]]
    rep_["_metal"] = rigid_u[inv].copy()     # the rubber metric measures the plates themselves
    rep_.update({"enabled": True, "plates": len(plates), "regions_too_small": len(regions) - len(plates),
                 "vertices_rigid": int(rigid_u[inv].sum()), "vertices_relaxed": int(band[inv].sum()),
                 "rigid_area_frac": round(float(area[rigid_u[inv]].sum() / max(area.sum(), 1e-12)), 3),
                 "by_bone": dict(sorted(by_bone.items(), key=lambda kv: -kv[1]))})
    if log:
        log("hard surface", plates=len(plates), rigid=rep_["vertices_rigid"], metal=rep_["metal_area_frac"])
    return rep_


# =================================================================== sockets

def add_human_sockets(rig, body, names, W, H, rigid_mask=None, thumb="z", grip_dirs=None, shields=True,
                      info=None, existing=None):
    """Contract 6 human sockets on the rest pose.

    socket_mount_hand_<l|r>: _items_lib.hand_socket_frame - the one hand frame of the forge, the
    same function attach_prop uses when a rig has no hand socket, so attach_prop reuses these
    sockets as they are (+Z out of the fist on the thumb side = character front projected
    perpendicular to the hand axis, -Y along the knuckles, X = Y x Z, origin on the palm side of
    the hand axis at 0.42 of the hand length). It never depends on the bone roll, so `thumb` (kept
    for callers: "z" | "auto") changes nothing. grip_dirs {"Left"|"Right": vector}: a hand holding
    a detected prop gets +Z exactly along it (prop_grip_dir); rigid_mask (props rigid to a bone)
    keeps prop vertices out of the hand measurements.
    socket_mount_shield_<l|r> (shields=True): _items_lib.rest_shield_mounts - the canonical shield
    fit of attach_prop on the rest pose (contract 6), child of the hand bone. A failure of the fit
    only skips the shield mounts (reported in info["shield_mounts_error"]).
    info (dict, optional) receives per-socket build details.
    existing {name: (world Matrix, extras dict)}: sockets of the same name brought by the input (e.g.
    socket_mount_hand_l of a placed bone_kit hand, on its grip axis) are used as they are - their
    frame and extras - instead of being rebuilt (info[name]["source"] = "input")."""
    existing = existing or {}
    from steps import _items_lib as IL
    grip_dirs = grip_dirs or {}
    info = info if info is not None else {}
    verts = mesh_verts(body)
    socks = []
    hand_emps = {}
    for side, tag in (("Left", "l"), ("Right", "r")):
        bn = b(side + "Hand")
        gd = grip_dirs.get(side)
        sname = "socket_mount_hand_" + tag
        if sname in existing:
            Mw, extra = existing[sname]
            hinfo = {"source": "input", "thumb_source": "input socket"}
        else:
            extra = {}
            Mw, hinfo = IL.hand_socket_frame(rig, body, tag, grip_dir=None if gd is None else vec(gd),
                                             exclude=rigid_mask if rigid_mask is not None and rigid_mask.any() else None)
            if gd is not None:
                hinfo["grip_dir"] = rnd(vec(gd), 4)
        info[sname] = hinfo
        e = af.add_socket(sname, parent=rig, bone=bn, kind="mount", size=0.04 * H / 1.8,
                          **{k: v for k, v in extra.items() if k not in ("kind", "game_socket")})
        e.rotation_mode = "QUATERNION"
        hand_emps[tag] = e
        socks.append((e, Mw))
    # back, hips, head, chest
    j = {short(bn.name): rig.matrix_world @ bn.head_local for bn in rig.data.bones}
    def surf(x, z, dz=0.03, front=False):
        sel = (np.abs(verts[:, 0] - x) < 0.05 * H) & (np.abs(verts[:, 2] - z) < dz * H)
        if sel.sum() < 5:
            return None
        return float(verts[sel, 1].min() if front else verts[sel, 1].max())
    zb = j["Spine2"].z + 0.04 * H
    yb = surf(0.0, zb)
    yb = (yb if yb is not None else j["Spine2"].y + 0.08 * H) + 0.02 * H
    Rback = Matrix(((-1, 0, 0), (0, -1, 0), (0, 0, 1))).transposed().to_4x4()
    socks.append((af.add_socket("socket_mount_back", parent=rig, bone=b("Spine2"), kind="mount",
                                size=0.06), Matrix.Translation((0.0, yb, zb)) @ Rback))
    zh = j["Hips"].z - 0.02 * H
    for side, tag, sgn in (("Left", "l", 1.0), ("Right", "r", -1.0)):
        sel = (np.abs(verts[:, 2] - zh) < 0.03 * H) & (np.abs(verts[:, 1] - j["Hips"].y) < 0.1 * H) & \
            (verts[:, 0] * sgn > 0) & (np.abs(verts[:, 0]) < 0.2 * H)
        xh = float(np.percentile(np.abs(verts[sel, 0]), 95)) if sel.sum() > 10 else 0.11 * H
        out = Vector((sgn, 0, 0))
        Y = -out
        Z = Vector((0, 0, 1))
        X = Y.cross(Z)
        R = Matrix((X, Y, Z)).transposed().to_4x4()
        socks.append((af.add_socket("socket_mount_hip_" + tag, parent=rig, bone=b("Hips"),
                                    kind="mount", size=0.05),
                      Matrix.Translation((sgn * (xh + 0.015 * H), j["Hips"].y, zh)) @ R))
    hc = (j["Head"] + j["HeadTop_End"]) * 0.5
    socks.append((af.add_socket("socket_fx_head", parent=rig, bone=b("Head"), kind="fx", size=0.05),
                  Matrix.Translation(hc)))
    zc = j["Spine2"].z
    yc = surf(0.0, zc, front=True)
    yc = (yc if yc is not None else j["Spine2"].y - 0.08 * H) - 0.01 * H
    socks.append((af.add_socket("socket_fx_chest", parent=rig, bone=b("Spine2"), kind="fx", size=0.05),
                  Matrix.Translation((0.0, yc, zc))))
    bpy.context.view_layer.update()
    out = {}
    for e, Mw in socks:
        if e.name in existing and not e.name.startswith("socket_mount_hand_"):
            Mw = existing[e.name][0]
            info[e.name] = {"source": "input"}
        e.matrix_world = Mw
        out[e.name] = e
    bpy.context.view_layer.update()
    if shields:
        before = set(bpy.data.objects)
        t_fit = time.time()
        try:
            mounts = IL.rest_shield_mounts(rig, [body], hand_emps)
            info["shield_mounts_seconds"] = round(time.time() - t_fit, 2)
            for tag, (emp, O, finfo) in sorted(mounts.items()):
                out[emp.name] = emp
                info[emp.name] = {"fit": finfo, "socket_space_matrix": IL.matrix_rows(O)}
        except Exception as exc:  # the rig stays valid without shield mounts
            info["shield_mounts_error"] = "%s: %s" % (type(exc).__name__, exc)
            for o in [o for o in bpy.data.objects if o not in before]:
                bpy.data.objects.remove(o, do_unlink=True)
            for e in hand_emps.values():
                for k in ("shield_mount_matrix", "shield_mount_socket"):
                    if k in e:
                        del e[k]
    return out


def bones_for_points(body, names, W, points, k=8):
    """Bone for a socket at each point: the bone whose skin weight dominates the k nearest body
    vertices (an eye socket in the skull -> Head, a clasp socket -> the spine bone of the robe there).
    Returns [(bone name, how)]."""
    verts = mesh_verts(body)
    kd = KDTree(len(verts))
    for i, v in enumerate(verts.tolist()):
        kd.insert(v, i)
    kd.balance()
    out = []
    for p_ in points:
        near = kd.find_n(tuple(p_), k)
        idx = [i for _, i, _ in near]
        if not idx:
            out.append((b("Hips"), "fallback"))
            continue
        acc = W[idx].sum(0)
        out.append((names[int(np.argmax(acc))], "skin weights of the %d nearest vertices" % len(idx)))
    return out


# =================================================================== QA: deformation test

TEST_POSES = {
    # world directions of bones (parents first); character faces -Y, left = +X
    # hands are not aimed: they keep their rest angle to the forearm (a hand closed on a hilt rests
    # across the forearm; aiming it too would bend the wrist 90 degrees, which no clip does)
    "arms_forward": [("LeftArm", (0, -1, 0)), ("LeftForeArm", (0, -1, 0)),
                     ("RightArm", (0, -1, 0)), ("RightForeArm", (0, -1, 0))],
    "arms_up": [("LeftShoulder", (0.88, 0, 0.47)), ("RightShoulder", (-0.88, 0, 0.47)),
                ("LeftArm", (0.17, 0, 0.985)), ("LeftForeArm", (0.17, 0, 0.985)),
                ("RightArm", (-0.17, 0, 0.985)), ("RightForeArm", (-0.17, 0, 0.985))],
    "squat": [("Spine", (0, -0.34, 0.94)), ("LeftUpLeg", (0.08, -0.8, -0.6)), ("LeftLeg", (0.02, 0.25, -0.97)),
              ("RightUpLeg", (-0.08, -0.8, -0.6)), ("RightLeg", (-0.02, 0.25, -0.97))],
    "stride": [("LeftUpLeg", (0.0, -0.5, -0.866)), ("LeftLeg", (0.0, -0.26, -0.966)),
               ("RightUpLeg", (0.0, 0.423, -0.906)), ("RightLeg", (0.0, 0.62, -0.785))],
}


def reset_pose(rig):
    for pb in rig.pose.bones:
        pb.rotation_mode = "QUATERNION"
        pb.rotation_quaternion = (1, 0, 0, 0)
        pb.location = (0, 0, 0)
        pb.scale = (1, 1, 1)
    bpy.context.view_layer.update()


def aim_bones(rig, targets):
    """Swing bones so their +Y points along the given world directions (rest roll kept)."""
    for name, d in targets:
        pb = rig.pose.bones.get(b(name))
        if pb is None:
            continue
        bpy.context.view_layer.update()
        cur = pb.matrix.copy()
        y = (cur.to_3x3().normalized() @ Vector((0, 1, 0))).normalized()
        R = y.rotation_difference(Vector(d).normalized()).to_matrix().to_4x4()
        T = Matrix.Translation(cur.translation)
        pb.matrix = T @ R @ T.inverted() @ cur
    bpy.context.view_layer.update()


def posed_verts(body):
    dg = bpy.context.evaluated_depsgraph_get()
    ev = body.evaluated_get(dg)
    me = ev.to_mesh()
    co = np.empty(len(me.vertices) * 3, dtype=np.float64)
    me.vertices.foreach_get("co", co)
    ev.to_mesh_clear()
    co = co.reshape(-1, 3)
    M = np.array(body.matrix_world, dtype=np.float64)
    return co @ M[:3, :3].T + M[:3, 3]


def self_hits(co, tris):
    bvh = BVHTree.FromPolygons(co.tolist(), tris.tolist(), all_triangles=True)
    pairs = np.array(bvh.overlap(bvh), dtype=np.int64)
    if len(pairs) == 0:
        return 0
    pairs = pairs[pairs[:, 0] < pairs[:, 1]]
    A, B = tris[pairs[:, 0]], tris[pairs[:, 1]]
    shared = (A[:, :, None] == B[:, None, :]).any(axis=(1, 2))
    return int((~shared).sum())


def deformation_test(body, rig, names, W, H, robe_zone=None, intersections=True, log=None, region=None):
    edges = mesh_edges(body)
    rest = mesh_verts(body)
    L0 = np.linalg.norm(rest[edges[:, 0]] - rest[edges[:, 1]], axis=1)
    # edges shorter than half the median are skipped: on sliver edges a tiny weight difference
    # gives huge ratios that say more about the triangulation than about the skinning
    valid = L0 > max(1e-6 * H, 0.5 * float(np.median(L0)))
    tiny = (L0 > 1e-6 * H) & ~valid
    # the floor contact (closing caps of AI meshes, soles) is not visible: reported separately
    floor = (rest[edges[:, 0], 2] < 0.006 * H) & (rest[edges[:, 1], 2] < 0.006 * H)
    valid &= ~floor
    tris = mesh_tris(body)
    T0 = rest[tris]
    A0 = np.linalg.norm(np.cross(T0[:, 1] - T0[:, 0], T0[:, 2] - T0[:, 0]), axis=1) * 0.5
    tri_ok = (A0 > 0.25 * float(np.median(A0))) & ~(T0[:, :, 2].max(1) < 0.006 * H)
    A_tot = float(A0[tri_ok].sum())
    base_hits = self_hits(rest, tris) if intersections else None
    res = {"rest_self_intersections": base_hits}
    for pose, targets in TEST_POSES.items():
        t0 = time.time()
        reset_pose(rig)
        aim_bones(rig, targets)
        co = posed_verts(body)
        L1 = np.linalg.norm(co[edges[:, 0]] - co[edges[:, 1]], axis=1)
        rall = L1 / np.maximum(L0, 1e-12)
        r = np.where(valid, rall, 1.0)
        k = int(np.argmax(r))
        vi = edges[k]
        worst_bones = [short(names[int(np.argmax(W[v]))]) for v in vi]
        item = {"max_stretch": round(float(r.max()), 3),
                "p999_stretch": round(float(np.percentile(r[valid], 99.9)), 3) if valid.any() else 1.0,
                "min_ratio": round(float(r[valid].min()), 3) if valid.any() else None,
                "edges_over_1_5": int((r > 1.5).sum()), "worst_at": worst_bones,
                "worst_pos": rnd(rest[vi].mean(0), 3),
                "max_stretch_sliver_edges": round(float(rall[tiny].max()), 3) if tiny.any() else None,
                "max_stretch_floor_edges": round(float(rall[floor].max()), 3) if floor.any() else None}
        T1 = co[tris]
        A1 = np.linalg.norm(np.cross(T1[:, 1] - T1[:, 0], T1[:, 2] - T1[:, 0]), axis=1) * 0.5
        ar = A1 / np.maximum(A0, 1e-15)
        torn = tri_ok & (ar > 3.0)
        A1_tot = max(float(A1[tri_ok].sum()), 1e-12)
        # share of the POSED surface made of triangles that grew > 3x: a thin fused strip that
        # becomes a big sheet (sleeve web, robe slab) counts with the area it covers in the pose
        item["torn_area_frac"] = round(float(A1[torn].sum()) / A1_tot, 5)
        item["torn_area_m2"] = round(float(A1[torn].sum()), 4)
        item["torn_area_h2"] = round(float(A1[torn].sum()) / (H * H), 5)
        item["torn_area_rest_frac"] = round(float(A0[torn].sum()) / max(A_tot, 1e-12), 5)
        item["stretched_area_frac"] = round(float(A1[tri_ok & (ar > 1.8)].sum()) / A1_tot, 5)
        if region is not None:
            # "rubber" inside a region (painted plate armour): rest area share of its triangles whose area changed
            # by more than 25 % (a rigid plate keeps every triangle; linear-blend skinning stretches it)
            tri_in = tri_ok & np.asarray(region, dtype=bool)[tris].all(1)
            rub = tri_in & ((ar > 1.25) | (ar < 0.8))
            item["region_rubber_frac"] = round(float(A0[rub].sum()) / max(float(A0[tri_in].sum()), 1e-12), 5)
        if torn.any():
            # where the tear is: bone dominating the torn triangles (by posed area)
            dom = np.argmax(W[tris[torn, 0]], axis=1)
            acc = {}
            for di, a in zip(dom.tolist(), A1[torn].tolist()):
                acc[short(names[di])] = acc.get(short(names[di]), 0.0) + a
            item["torn_by_bone_m2"] = {k: round(v, 4) for k, v in
                                       sorted(acc.items(), key=lambda kv: -kv[1])[:4]}
            wts = A1[torn]
            item["torn_rest_centroid"] = rnd((T0[torn].mean(1) * wts[:, None]).sum(0) / wts.sum(), 3)
        if robe_zone is not None:
            tz = robe_zone[tris].any(1) & tri_ok
            if tz.any():
                item["robe_torn_area_frac"] = round(float(A1[tz & (ar > 3.0)].sum()) /
                                                    max(float(A1[tz].sum()), 1e-12), 5)
        if robe_zone is not None:
            ez = (robe_zone[edges[:, 0]] | robe_zone[edges[:, 1]]) & valid
            if ez.any():
                item["robe_max_stretch"] = round(float(r[ez].max()), 3)
                item["robe_p999_stretch"] = round(float(np.percentile(r[ez], 99.9)), 3)
        if intersections:
            hits = self_hits(co, tris)
            item["self_intersections"] = hits
            item["new_self_intersections"] = hits - base_hits
            item["new_self_intersection_frac"] = round((hits - base_hits) / max(1, len(tris)), 5)
        item["seconds"] = round(time.time() - t0, 2)
        res[pose] = item
        if log:
            log("pose test", pose=pose, max_stretch=item["max_stretch"], p999=item["p999_stretch"])
    reset_pose(rig)
    return res


def render_poses(path, body, rig, size=256):
    """poses.png: rest + test poses; top row front views, bottom row three-quarter views."""
    tmp_dir = os.path.join(os.path.dirname(path), "_poses")
    os.makedirs(tmp_dir, exist_ok=True)
    rows = {"front": [], "three_quarter": []}
    poses = [("rest", [])] + list(TEST_POSES.items())
    hidden = []
    for o in bpy.context.scene.objects:
        if o is not body and o.type == "MESH" and not o.hide_render:
            o.hide_render = True
            hidden.append(o)
    for name, targets in poses:
        reset_pose(rig)
        aim_bones(rig, targets)
        for view in rows:
            p = os.path.join(tmp_dir, "%s_%s.png" % (name, view))
            af.render_views(p, [body], views=(view,), size=size)
            rows[view].append(p)
    reset_pose(rig)
    a = concat_images(rows["front"], os.path.join(tmp_dir, "row_front.png"))
    c = concat_images(rows["three_quarter"], os.path.join(tmp_dir, "row_tq.png"))
    concat_images([a, c], path, vertical=True)
    for o in hidden:
        o.hide_render = False
    import shutil
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return path


def symmetry_error(joints, X0, H, names):
    worst, per = 0.0, {}
    for n in names:
        L, R = joints.get("Left" + n), joints.get("Right" + n)
        if L is None or R is None:
            continue
        m = np.array([2 * X0 - R[0], R[1], R[2]])
        e = float(np.linalg.norm(np.asarray(L) - m)) / H
        per[n] = round(e, 4)
        worst = max(worst, e)
    return round(worst, 4), per


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(af._jsonable(data), fh, ensure_ascii=False, indent=1)
    return path


def markers_json(lm, prep, H, warnings, extra=None):
    J = lm.joints_m()
    d = {
        "format": "astraforge.humanoid.markers/1",
        "units": "m",
        "frame": "normalised: faces -Y, feet on z=0, trunk midline x=0, character left = +X, Z up",
        "height_m": H,
        "markers": lm.markers_dict(),
        "marker_note": "same keys as params.markers ([x, z] front view, Mixamo-style): chin on the "
                       "chin (Head joint = chin + 0.04 H), groin = crotch (where the legs merge)",
        "confidence": {k: lm.conf.get(k) for k in MARKER_KEYS},
        "confidence_feet": {k: lm.conf.get(k) for k in ("left_foot", "right_foot")},
        "source": {k: lm.src.get(k) for k in MARKER_KEYS},
        "overall_confidence": lm.overall,
        "joints": {k: rnd(v) for k, v in J.items()},
        "pose": lm.pose,
        "legs": lm.leg_mode,
        "robe": bool(lm.robe),
        "facing": prep.get("facing"),
        "props": [{"axis_from": rnd((p["c"] + p["d"] * p.get("extent", p["t"])[0]) * H),
                   "axis_to": rnd((p["c"] + p["d"] * p.get("extent", p["t"])[1]) * H),
                   "length_m": round(float(p.get("extent", p["t"])[1] - p.get("extent", p["t"])[0]) * H, 3),
                   "held_by": p.get("side") if p.get("held") else None,
                   "bound_to": b(p["bind"]) if p.get("bind") else None, "bind_mode": p.get("bind_mode"),
                   "grip": rnd(np.asarray(p["grip"]) * H) if p.get("held") else None,
                   "grip_mode": p.get("grip_mode"), "oblique": bool(p.get("oblique"))}
                  for p in lm.props],
        "arm_symmetry": {"per_joint_h": getattr(lm, "arm_sym", None),
                         "max_h": getattr(lm, "arm_sym_max", None), "limit_h": ARM_SYM_MAX,
                         "exempt_prop_in_hand": getattr(lm, "arm_sym_exempt", None)},
        "yaw": prep.get("yaw"),
        "plates": [{"side": p["side"], "centre": rnd(p["centre"] * H), "normal": rnd(p["normal"]),
                    "radius_m": round(float(p["radius"]) * H, 3)} for p in getattr(lm, "plate_props", [])],
        "bows": [{"kind": bw["kind"], "held_by": bw["side"], "bound_to": b(bw["bind"]),
                  "tip_low": rnd(bw["tips"][0] * H), "tip_high": rnd(bw["tips"][1] * H),
                  "length_m": round(float(bw["length"]) * H, 3), "sagitta_m": round(float(bw["sagitta"]) * H, 3),
                  "plane_normal": rnd(bw["normal"], 3), "grip": rnd(np.asarray(bw["grip"]) * H),
                  "grip_fraction": round(float(bw["grip_frac"]), 3), "samples": int(len(bw["idx"]))}
                 for bw in getattr(lm, "bow_props", [])],
        "proportions": lm.ratios,
        "issues": lm.issues,
        "gross_failures": getattr(lm, "gross", []),
        "fail": lm.fail,
        "detector": lm.info,
        "warnings": list(warnings),
    }
    if extra:
        d.update(extra)
    return d


# =================================================================== fingers

def _hand_frame(Wj, Tj):
    y = Tj - Wj
    L = float(np.linalg.norm(y))
    y = y / max(L, 1e-9)
    z = np.array([0.0, -1.0, 0.0])
    z = z - (z @ y) * y
    if np.linalg.norm(z) < 0.3:
        z = np.array([0.0, 0.0, 1.0]) - y[2] * y
    z /= np.linalg.norm(z)
    return y, z, np.cross(y, z), L


def _fingers_one_hand(P, Wj, Tj, H):
    """Cross-sections perpendicular to the hand axis; four separable long fingers ordered along
    the thumb-side axis plus a thumb branching on the thumb side. Returns ({finger: 4 points} or
    None, info)."""
    y, z, x, L = _hand_frame(Wj, Tj)
    rel = P - Wj
    t, a, c = rel @ y, rel @ x, rel @ z
    radial = np.hypot(a, c)
    sel = (t > -0.005 * H) & (t < L + 0.06 * H) & (radial < 0.07 * H)
    Q = np.column_stack([a[sel], c[sel], t[sel]])
    if len(Q) < 200:
        return None, {"reason": "too few samples around the hand"}
    step = 0.004 * H
    rows = []
    for t0 in np.arange(0.0, L + 0.06 * H, step):
        idx = np.nonzero(np.abs(Q[:, 2] - t0) < step * 0.5)[0]
        comps = [cc for cc in components_2d(Q, idx, [0, 1], 0.0022 * H, 0, min_n=4)
                 if max(cc.ext(0), cc.ext(1)) < 0.05 * H]
        rows.append((float(t0), comps))
    counts = [len(r[1]) for r in rows]
    # longest run with >= 4 small components in the finger zone
    best, cur = [], []
    for i, (t0, comps) in enumerate(rows):
        if 0.4 * L <= t0 <= L + 0.04 * H and len(comps) >= 4:
            cur.append(i)
        else:
            if len(cur) > len(best):
                best = cur
            cur = []
    if len(cur) > len(best):
        best = cur
    info = {"max_components": int(max(counts) if counts else 0), "finger_rows": len(best)}
    if len(best) < 3:
        info["reason"] = "fingers not separable (fused mitten/fist or touching)"
        return None, info
    seed = best[len(best) // 2]
    comps = sorted(rows[seed][1], key=lambda cc: -cc.c[1])     # thumb side first
    longf = comps[-4:] if len(comps) >= 4 else comps
    longf = sorted(longf, key=lambda cc: -cc.c[1])            # index, middle, ring, pinky

    def track(c0, k0):
        chain = [(rows[k0][0], c0.c.copy(), c0)]
        prev = c0.c[:2]
        for k in range(k0 + 1, len(rows)):
            cand = [cc for cc in rows[k][1] if np.linalg.norm(cc.c[:2] - prev) < 0.014 * H]
            if not cand:
                break
            cc = min(cand, key=lambda q: np.linalg.norm(q.c[:2] - prev))
            chain.append((rows[k][0], cc.c.copy(), cc))
            prev = cc.c[:2]
        tip_c = chain[-1][2]
        tip = Q[tip_c.idx[np.argmax(Q[tip_c.idx, 2])]]
        base_t = None
        prev = c0.c[:2]
        down = []
        for k in range(k0 - 1, -1, -1):
            cand = [cc for cc in rows[k][1] if np.linalg.norm(cc.c[:2] - prev) < 0.014 * H]
            if not cand:
                base_t = rows[k + 1][0]
                break
            cc = min(cand, key=lambda q: np.linalg.norm(q.c[:2] - prev))
            if max(cc.ext(0), cc.ext(1)) > 0.03 * H:
                base_t = rows[k][0]
                break
            down.append((rows[k][0], cc.c.copy(), cc))
            prev = cc.c[:2]
        if base_t is None:
            base_t = rows[0][0]
        pts = [p for _, p, _ in reversed(down)] + [p for _, p, _ in chain] + [tip]
        return np.array(pts), base_t, tip

    def to_world(q):
        return Wj + q[0] * x + q[1] * z + q[2] * y

    def phalanges(pts, base_t):
        mcp_t = max(0.2 * L, base_t - 0.01 * H)
        first = pts[0]
        mcp = np.array([first[0], first[1], mcp_t])
        poly = np.vstack([mcp, pts[pts[:, 2] > mcp_t]])
        seg = np.linalg.norm(np.diff(poly, axis=0), axis=1)
        s = np.concatenate([[0.0], np.cumsum(seg)])
        if s[-1] < 0.02 * H:
            return None
        out = [mcp]
        for f in (0.45, 0.75):
            out.append(np.array([np.interp(f * s[-1], s, poly[:, i]) for i in range(3)]))
        out.append(poly[-1])
        return [to_world(q) for q in out], float(s[-1])

    fingers, lens = {}, {}
    for name, cc in zip(("Index", "Middle", "Ring", "Pinky"), longf):
        pts, base_t, tip = track(cc, seed)
        r = phalanges(pts, base_t)
        if r is None:
            info["reason"] = "%s finger too short" % name
            return None, info
        fingers[name], lens[name] = r
    # thumb: a separate small component on the thumb side, proximal to the long fingers
    idx_c = longf[0].c[1]
    th = None
    for k in range(len(rows)):
        t0, comps_k = rows[k]
        if t0 < 0.1 * L or t0 > 0.75 * L:
            continue
        for cc in comps_k:
            if cc.c[1] > idx_c + 0.008 * H and max(cc.ext(0), cc.ext(1)) < 0.03 * H:
                th = (k, cc)
                break
        if th:
            break
    if th is None:
        info["reason"] = "thumb not found on the thumb side"
        return None, info
    pts, base_t, tip = track(th[1], th[0])
    web = np.array([pts[0][0], pts[0][1], base_t])
    cmc = np.array([0.5 * web[0], 0.5 * web[1], max(0.08 * L, base_t - 0.035 * H)])
    tipq = pts[-1]
    ip = web + 0.55 * (tipq - web)
    fingers["Thumb"] = [to_world(cmc), to_world(web), to_world(ip), to_world(tipq)]
    lens["Thumb"] = float(np.linalg.norm(tipq - cmc))
    info.update({"lengths_m": {k: round(v, 4) for k, v in lens.items()}})
    # sanity: middle finger is the longest of the four; lengths within human ranges
    four = [lens[n] for n in ("Index", "Middle", "Ring", "Pinky")]
    if not (0.025 * H < lens["Middle"] < 0.075 * H) or lens["Pinky"] > lens["Middle"] * 1.1:
        info["reason"] = "finger lengths implausible %s" % info["lengths_m"]
        return None, info
    return fingers, info


def detect_fingers(body, lm, J, H, warnings):
    """params.fingers: 3 deforming bones + 1 end bone per finger (40 bones), only when both hands
    show 4 separable fingers and a thumb; otherwise none (bones stay 25) with a warning."""
    info = {"built": False, "hands": {}}
    for side in SIDES:
        if lm.grip.get(side):
            info["hands"][side] = {"reason": "hand grips a prop (fist)"}
            warnings.append("fingers skipped: %s hand grips a prop" % side.lower())
            return None, info
    verts = mesh_verts(body)
    tris = mesh_tris(body)
    P, _ = sample_surface(verts, tris, n=400000, seed=11)
    out = {}
    for side in SIDES:
        f, hi = _fingers_one_hand(P, np.asarray(J[side + "Hand"]), np.asarray(J[side + "HandTip"]), H)
        info["hands"][side] = hi
        if f is None:
            warnings.append("fingers skipped (%s hand: %s); rig has 25 bones" % (side.lower(), hi.get("reason")))
            return None, info
        out[side] = f
    info["built"] = True
    return out, info


# =================================================================== synthetic mannequin (tests)

def _prim_capsule(bm, a, b_, r, seg=16):
    a, b_ = Vector(a), Vector(b_)
    d = b_ - a
    L = d.length
    q = Vector((0, 0, 1)).rotation_difference(d.normalized())
    M = Matrix.Translation((a + b_) * 0.5) @ q.to_matrix().to_4x4()
    bmesh.ops.create_cone(bm, cap_ends=True, segments=seg, radius1=r, radius2=r, depth=L, matrix=M)
    for p in (a, b_):
        bmesh.ops.create_uvsphere(bm, u_segments=seg, v_segments=seg // 2, radius=r,
                                  matrix=Matrix.Translation(p))


def _prim_ellipsoid(bm, c, radii, seg=20):
    M = Matrix.Translation(c) @ Matrix.Diagonal((radii[0], radii[1], radii[2], 1.0))
    bmesh.ops.create_uvsphere(bm, u_segments=seg, v_segments=seg // 2, radius=1.0, matrix=M)


def make_mannequin(pose="A", height=1.8, staff=False, robe=False, legless=False, voxel=0.012,
                   name="Mannequin", sword=False, bow=False, quiver=False):
    """Procedural humanoid made of capsules/ellipsoids, voxel-remeshed into one surface, facing
    -Y, feet on z=0, scaled to `height`. staff: a 1.95 m staff through the left fist; sword: a
    0.9 m blade hanging DOWN from the right fist (hilt + crossguard above/at the fist); sword="oblique": a flat
    8 cm wide, 0.85 m blade held forward, down and out from the right fist (the generated knights' pose: it
    crosses every horizontal/vertical slicing obliquely and is wider there than a shaft); bow: a 1.3 m
    bow held in the left fist beside the body (limb bulging 6 cm outward, string through the fist,
    both in the frontal plane); quiver: a quiver with three arrows on the back. Returns (object,
    ground-truth joints in metres)."""
    ang = {"down": 10.0, "A": 45.0, "T": 90.0}[pose]
    j = {}
    lift = 0.55 if legless else 0.0
    def P(x, y, z):
        return Vector((x, y, z + lift))
    j["Hips"] = P(0, 0.0, 0.95)
    j["Neck"] = P(0, 0.0, 1.50)
    j["Head"] = P(0, -0.005, 1.585)
    for s, sg in (("Left", 1.0), ("Right", -1.0)):
        S = P(sg * 0.19, 0.0, 1.44)
        d = Vector((sg * math.sin(math.radians(ang)), 0.0, -math.cos(math.radians(ang))))
        E = S + d * 0.29
        W = E + d * 0.255
        j[s + "Arm"], j[s + "ForeArm"], j[s + "Hand"], j[s + "HandTip"] = S, E, W, W + d * 0.17
        j[s + "UpLeg"] = P(sg * 0.095, 0.0, 0.91)
        j[s + "Leg"] = P(sg * 0.10, -0.01, 0.50)
        j[s + "Foot"] = P(sg * 0.105, 0.02, 0.085)
        j[s + "Toe_End"] = P(sg * 0.11, -0.14, 0.03)
    bm = bmesh.new()
    _prim_ellipsoid(bm, P(0, 0, 0.96), (0.17, 0.11, 0.13))
    _prim_ellipsoid(bm, P(0, 0, 1.13), (0.15, 0.10, 0.14))
    _prim_ellipsoid(bm, P(0, 0, 1.32), (0.19, 0.12, 0.15))
    _prim_capsule(bm, P(0, 0, 1.42), P(0, 0, 1.58), 0.055)
    _prim_ellipsoid(bm, P(0, -0.01, 1.685), (0.085, 0.10, 0.115))
    for s, sg in (("Left", 1.0), ("Right", -1.0)):
        _prim_capsule(bm, P(sg * 0.09, 0, 1.43), j[s + "Arm"], 0.055)
        _prim_capsule(bm, j[s + "Arm"], j[s + "ForeArm"], 0.048)
        _prim_capsule(bm, j[s + "ForeArm"], j[s + "Hand"], 0.038)
        hd = (j[s + "HandTip"] - j[s + "Hand"])
        _prim_ellipsoid(bm, j[s + "Hand"] + hd * 0.5, (0.03, 0.045, 0.03) if pose != "T" else (0.07, 0.045, 0.02))
        if not legless:
            _prim_capsule(bm, j[s + "UpLeg"], j[s + "Leg"], 0.075)
            _prim_capsule(bm, j[s + "Leg"], j[s + "Foot"], 0.055)
            _prim_ellipsoid(bm, P(sg * 0.105, -0.04, 0.04), (0.045, 0.12, 0.04))
    if staff:
        Wl = j["LeftHand"] + (j["LeftHandTip"] - j["LeftHand"]) * 0.45
        _prim_capsule(bm, Vector((Wl.x, Wl.y - 0.0, 0.02)), Vector((Wl.x, Wl.y, 1.95)), 0.016, seg=10)
    if sword == "oblique":
        F = j["RightHand"] + (j["RightHandTip"] - j["RightHand"]) * 0.45
        dv = Vector((-0.35, -0.62, -0.70)).normalized()
        side = dv.cross(Vector((0.0, 0.0, 1.0))).normalized()        # flat of the blade faces sideways ...
        flat = dv.cross(side).normalized()                            # ... the edge leads forward-down
        g0 = F + dv * 0.06                                            # crossguard just past the fist
        M = Matrix((side * 0.01, flat * 0.04, dv * 0.425)).transposed().to_4x4()
        M.translation = g0 + dv * 0.425
        bmesh.ops.create_cube(bm, size=2.0, matrix=M)
        _prim_capsule(bm, g0 - side * 0.09, g0 + side * 0.09, 0.013, seg=8)
        _prim_capsule(bm, g0, F - dv * 0.10, 0.013, seg=10)
    elif sword:
        F = j["RightHand"] + (j["RightHandTip"] - j["RightHand"]) * 0.45
        zg = F.z - 0.06                                   # crossguard just under the fist
        _prim_capsule(bm, Vector((F.x, F.y, zg)), Vector((F.x, F.y, zg - 0.80)), 0.015, seg=10)
        _prim_capsule(bm, Vector((F.x, F.y, zg)), Vector((F.x, F.y, F.z + 0.09)), 0.013, seg=10)
        _prim_capsule(bm, Vector((F.x, F.y - 0.09, zg)), Vector((F.x, F.y + 0.09, zg)), 0.013, seg=8)
    if bow:
        F = j["LeftHand"] + (j["LeftHandTip"] - j["LeftHand"]) * 0.45
        half = 0.65

        def limb(z):
            return Vector((F.x - 0.03 + 0.06 * (1.0 - ((z - F.z) / half) ** 2), F.y, z))
        zs = [F.z - half + 2 * half * i / 16 for i in range(17)]
        for za, zb in zip(zs[:-1], zs[1:]):
            _prim_capsule(bm, limb(za), limb(zb), 0.012, seg=10)
        _prim_capsule(bm, limb(zs[0]), limb(zs[-1]), 0.007, seg=8)       # the string
    if quiver:
        a, b_ = P(-0.07, 0.17, 0.95), P(0.05, 0.19, 1.40)
        _prim_capsule(bm, a, b_, 0.05, seg=12)
        for dx in (-0.02, 0.0, 0.02):
            _prim_capsule(bm, b_ + Vector((dx, 0.0, -0.05)), b_ + Vector((dx + 0.03, 0.01, 0.22)), 0.008, seg=8)
    if robe:
        bmesh.ops.create_cone(bm, cap_ends=True, segments=32, radius1=0.34, radius2=0.18, depth=0.92,
                              matrix=Matrix.Translation((0, 0.0, 0.47)))
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    mod = ob.modifiers.new("Remesh", "REMESH")
    mod.mode = "VOXEL"
    mod.voxel_size = voxel
    sm = ob.modifiers.new("Smooth", "SMOOTH")
    sm.iterations = 3
    sm.factor = 0.5
    select_only(ob)
    bpy.ops.object.modifier_apply(modifier=mod.name)
    bpy.ops.object.modifier_apply(modifier=sm.name)
    v = mesh_verts(ob)
    k = height / float(v[:, 2].max() - (0.0 if legless else v[:, 2].min()))
    zmin = 0.0 if legless else float(v[:, 2].min())
    ob.data.transform(Matrix.Scale(k, 4) @ Matrix.Translation((0, 0, -zmin)))
    ob.data.update()
    mat = bpy.data.materials.new(name + "_mat")
    mat.diffuse_color = (0.7, 0.62, 0.55, 1.0)
    ob.data.materials.append(mat)
    joints = {n: np.array((p - Vector((0, 0, zmin))) * k) for n, p in j.items()}
    return ob, joints
