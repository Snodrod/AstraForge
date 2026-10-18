"""_cloth_lib - real cloth for skinned characters (step cloth_sim, docs/forge/cloth_sim.md).

Generated characters (Hunyuan envelope, rig_humanoid weights) carry robes, capes, tabards, hoods, wide sleeves and
banner cloth skinned like the body: under linear blend skinning they stretch, slide and swim. This module

1. segments the loose garment regions of the bound body from geometry and skin (`segment`): robe / skirt / coat
   tails below the hips that stand off the legs, a cape hanging behind the back, a tabard panel in front, a hood
   standing off the head, sleeves wider than the arm, banner cloth on a pole held rigidly in a hand; optional
   region boxes from params.cloth; attachment (pin) weights where the garment hangs from (waist, shoulders,
   neck, elbow, the pole edge);
2. splits them off into garment objects (UVs / material / weights kept; the seam stays welded because the seam
   vertices are pinned to the skin; the disc closing a robe hem is dropped), builds the simulation mesh (the
   garment itself, or a QuadriFlow proxy), bone-parented collision capsules fitted to the garment (body, held
   poles) and a floor;
3. simulates every clip with Blender's cloth (pre-roll settles the first pose, loops get a warm-up cycle and a
   cross-faded seam) and bakes each clip into a per-clip shape-key action of the garment, paired with the
   armature action (render_sprites switches it together with the clip: _export_lib.bind_cloth_cache);
4. derives secondary cloth-bone chains for the GLB (bones weighted to the cloth, animated per clip from the
   simulation, or by a spring/lag chain where there is no simulation);
5. measures penetration, stretch, floor and timing.
"""
import heapq
import json
import math

import bpy
import bmesh
import numpy as np
from mathutils import Matrix, Quaternion, Vector
from mathutils.kdtree import KDTree

try:
    from steps import _anim_lib as A
except ImportError:  # pragma: no cover - imported as a plain module by tests
    import _anim_lib as A

# ----------------------------------------------------------------------------------------------- constants

CACHE_PROP = "af_cloth_cache"           # on a garment: JSON {armature action name: shape-key action name}
CLOTH_ROLE = "af_cloth_role"            # custom property on created objects: proxy | render | collider | glb
PIN_GROUP = "af_cloth_pin"
FREE_GROUP = "af_cloth_free"
REGION_KINDS = ("robe", "skirt", "tabard", "back_panel", "cape", "hood", "sleeve", "banner")

# anthropometric capsule radii, fraction of the standing height (head, tail of the bone) - a 1.8 m body:
# thigh 9 -> 6 cm, shin 5.6 -> 3.8 cm, upper arm 5 cm, torso 13.5 cm. Used for collision proxies (inflated a
# little) and as "the body under the clothes" when deciding what stands off the body.
LIMB_R = {
    "UpLeg": (0.050, 0.034), "Leg": (0.031, 0.021), "Foot": (0.024, 0.019), "ToeBase": (0.019, 0.015),
    "Arm": (0.028, 0.023), "ForeArm": (0.023, 0.017), "Hand": (0.020, 0.015), "Shoulder": (0.030, 0.028),
    "Hips": (0.074, 0.074), "Spine": (0.072, 0.074), "Spine1": (0.076, 0.080), "Spine2": (0.080, 0.070),
    "Neck": (0.030, 0.030), "Head": (0.056, 0.050),
}
CAPSULE_CHAIN = [  # (bone canon, tail canon or None = bone tail) - the bones that get a body capsule
    ("Hips", "Spine"), ("Spine", "Spine1"), ("Spine1", "Spine2"), ("Spine2", "Neck"), ("Neck", "Head"),
    ("Head", "HeadTop_End"),
] + [(s + b, (s + c) if c else None) for s in ("Left", "Right") for b, c in (
    ("UpLeg", "Leg"), ("Leg", "Foot"), ("Foot", "ToeBase"), ("ToeBase", "Toe_End"), ("Shoulder", "Arm"),
    ("Arm", "ForeArm"), ("ForeArm", "Hand"), ("Hand", None))]

# Blender cloth settings per garment (vertex mass kg, stiffness, damping). The proxy is ~1.5-3k vertices for the
# whole garment, so masses / stiffness are tuned for that density (Blender cloth masses are per vertex).
PRESETS = {
    "wool_robe": {"mass": 0.30, "air_damping": 1.5, "tension": 15.0, "compression": 15.0, "shear": 8.0,
                  "bending": 8.0, "shape_hold": 0.45, "tension_damping": 5.0, "compression_damping": 5.0, "shear_damping": 5.0,
                  "bending_damping": 0.5, "pin_stiffness": 1.0, "quality": 5, "collision_quality": 2,
                  "distance_min": 0.012, "friction": 5.0, "note": "wool / cotton robe, skirt, coat tails"},
    "heavy_cape": {"mass": 0.55, "air_damping": 1.2, "tension": 30.0, "compression": 30.0, "shear": 15.0,
                   "bending": 6.0, "shape_hold": 0.3, "tension_damping": 8.0, "compression_damping": 8.0, "shear_damping": 8.0,
                   "bending_damping": 1.0, "pin_stiffness": 1.0, "quality": 5, "collision_quality": 2,
                   "distance_min": 0.012, "friction": 5.0, "note": "heavy wool / leather-lined cape"},
    "tabard": {"mass": 0.40, "air_damping": 1.2, "tension": 30.0, "compression": 30.0, "shear": 15.0,
               "bending": 10.0, "shape_hold": 0.35, "tension_damping": 8.0, "compression_damping": 8.0, "shear_damping": 8.0,
               "bending_damping": 1.0, "pin_stiffness": 1.0, "quality": 5, "collision_quality": 2,
               "distance_min": 0.010, "friction": 5.0, "note": "stiff heraldic tabard / surcoat panel"},
    "light_silk": {"mass": 0.15, "air_damping": 2.0, "tension": 8.0, "compression": 8.0, "shear": 4.0,
                   "bending": 0.3, "shape_hold": 0.1, "tension_damping": 3.0, "compression_damping": 3.0, "shear_damping": 3.0,
                   "bending_damping": 0.3, "pin_stiffness": 1.0, "quality": 5, "collision_quality": 2,
                   "distance_min": 0.008, "friction": 2.0, "note": "light banner silk"},
}
REGION_PRESET = {"robe": "wool_robe", "skirt": "wool_robe", "back_panel": "heavy_cape", "cape": "heavy_cape",
                 "tabard": "tabard", "hood": "wool_robe", "sleeve": "wool_robe", "banner": "light_silk"}


def smoothstep(x):
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


# ----------------------------------------------------------------------------------------------- skeleton

class Skeleton:
    """Rest skeleton in world space with the canonical (Mixamo-like) names of _anim_lib."""

    def __init__(self, rig):
        self.rig = rig
        scheme, amap = A.map_armature(rig)
        self.amap = dict(amap)                     # bone name -> canonical
        self.cname = {c: n for n, c in amap.items()}
        W = rig.matrix_world
        self.W = W.copy()
        self.head = {}
        self.tail = {}
        for n, c in amap.items():
            b = rig.data.bones[n]
            self.head[c] = np.array(W @ b.head_local, dtype=np.float64)
            self.tail[c] = np.array(W @ b.tail_local, dtype=np.float64)
        top = self.head.get("HeadTop_End")
        if top is None:
            top = self.tail.get("Head", np.array([0.0, 0.0, 1.8]))
        feet = [self.head[c][2] for c in self.head if c.endswith(("Foot", "ToeBase", "Toe_End"))]
        self.floor = min(feet + [0.0]) if feet else 0.0
        self.H = max(float(top[2]) - min(0.0, self.floor), 0.5)
        hz = [self.head[s + "UpLeg"][2] for s in ("Left", "Right") if (s + "UpLeg") in self.head]
        self.hip_z = float(sum(hz) / len(hz)) if hz else float(self.head["Hips"][2])
        self.pelvis_z = float(self.head["Hips"][2])
        kz = [self.head[s + "Leg"][2] for s in ("Left", "Right") if (s + "Leg") in self.head]
        self.knee_z = float(sum(kz) / len(kz)) if kz else self.hip_z * 0.5
        sz = [self.head[s + "Arm"][2] for s in ("Left", "Right") if (s + "Arm") in self.head]
        self.shoulder_z = float(sum(sz) / len(sz)) if sz else self.hip_z + 0.3 * self.H
        self.neck_z = float(self.head["Neck"][2]) if "Neck" in self.head else self.shoulder_z + 0.03 * self.H
        F = A.char_frame({c: Vector(v.tolist()) for c, v in self.head.items()})
        self.left = np.array(F.col[0])
        self.fwd = -np.array(F.col[1])          # character forward (-Y for a contract-facing body)
        self.fwd[2] = 0.0
        self.fwd /= max(np.linalg.norm(self.fwd), 1e-9)
        self.back = -self.fwd
        self.up = np.array([0.0, 0.0, 1.0])
        self.centre = self.head["Hips"].copy()

    def name(self, canon):
        return self.cname.get(canon)

    def capsules(self, inflate=0.0):
        """[(canon, bone name, a, b, r_a, r_b)] world rest capsules of the body."""
        out = []
        for c, nxt in CAPSULE_CHAIN:
            if c not in self.head:
                continue
            a = self.head[c]
            b = self.head[nxt] if (nxt and nxt in self.head) else self.tail[c]
            if np.linalg.norm(b - a) < 1e-4:
                continue
            key = c.replace("Left", "").replace("Right", "")
            ra, rb = LIMB_R.get(key, (0.03, 0.03))
            out.append((c, self.cname[c], a.copy(), b.copy(), ra * self.H + inflate, rb * self.H + inflate))
        return out


def capsule_sd(P, a, b, ra, rb):
    """Signed distance of points P (N,3) to a tapered capsule, and the axis parameter t in 0..1."""
    ab = b - a
    L2 = max(float(ab @ ab), 1e-12)
    t = np.clip(((P - a) @ ab) / L2, 0.0, 1.0)
    d = np.linalg.norm(P - (a + t[:, None] * ab), axis=1)
    return d - (ra + (rb - ra) * t), t


# ----------------------------------------------------------------------------------------------- mesh data

def rest_coords(ob):
    """Bind-pose (reference shape key / base mesh) vertex positions in world space, (N,3)."""
    me = ob.data
    nv = len(me.vertices)
    co = np.empty(nv * 3)
    if me.shape_keys is not None and me.shape_keys.reference_key is not None:
        me.shape_keys.reference_key.data.foreach_get("co", co)
    else:
        me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    M = np.array(ob.matrix_world, dtype=np.float64)
    return co @ M[:3, :3].T + M[:3, 3]


def mesh_edges(me):
    e = np.empty(len(me.edges) * 2, dtype=np.int64)
    me.edges.foreach_get("vertices", e)
    return e.reshape(-1, 2)


def mesh_tris(me):
    me.calc_loop_triangles()
    t = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("vertices", t)
    return t.reshape(-1, 3)


def vertex_normals(ob):
    me = ob.data
    n = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("normal", n)
    n = n.reshape(-1, 3)
    R = np.array(ob.matrix_world.to_3x3(), dtype=np.float64)
    n = n @ R.T
    return n / np.maximum(np.linalg.norm(n, axis=1, keepdims=True), 1e-12)


def weight_matrix(ob, names):
    """(N, len(names)) weights of the given vertex groups."""
    idx = {}
    for j, n in enumerate(names):
        vg = ob.vertex_groups.get(n)
        if vg is not None:
            idx[vg.index] = j
    W = np.zeros((len(ob.data.vertices), len(names)))
    for v in ob.data.vertices:
        for g in v.groups:
            j = idx.get(g.group)
            if j is not None:
                W[v.index, j] = g.weight
    return W


def weld_edges(P, edges, tol=1e-5):
    """Mesh edges plus zero-length links between coincident vertices (glTF splits vertices at normal / UV seams; a
    flat-shaded import is a triangle soup): connectivity, smoothing and geodesics then run across the seams."""
    key = np.round(P / tol).astype(np.int64)
    _u, first, inv = np.unique(key, axis=0, return_index=True, return_inverse=True)
    inv = inv.ravel()
    rep_ = first[inv]
    idx = np.nonzero(rep_ != np.arange(len(P)))[0]
    if not len(idx):
        return edges
    return np.concatenate([edges, np.stack([idx, rep_[idx]], axis=1)])


def adjacency(nv, edges):
    nb = [[] for _ in range(nv)]
    for a, b in edges.tolist():
        nb[a].append(b)
        nb[b].append(a)
    return nb


def geodesic(nv, edges, P, seeds, limit=None):
    """Multi-source Dijkstra along mesh edges; inf where unreachable (or beyond limit)."""
    dist = np.full(nv, np.inf)
    nb = [[] for _ in range(nv)]
    L = np.linalg.norm(P[edges[:, 0]] - P[edges[:, 1]], axis=1)
    for (a, b), w in zip(edges.tolist(), L.tolist()):
        nb[a].append((b, w))
        nb[b].append((a, w))
    heap = []
    for s in np.nonzero(seeds)[0].tolist():
        dist[s] = 0.0
        heap.append((0.0, s))
    heapq.heapify(heap)
    while heap:
        d, v = heapq.heappop(heap)
        if d > dist[v]:
            continue
        if limit is not None and d > limit:
            continue
        for u, w in nb[v]:
            nd = d + w
            if nd < dist[u]:
                dist[u] = nd
                heapq.heappush(heap, (nd, u))
    return dist


def majority_filter(mask, nb, iters=2, keep=None):
    """Smooth a vertex mask over the mesh graph (a vertex takes the majority of itself + neighbours)."""
    m = mask.copy()
    for _ in range(iters):
        cnt = np.array([sum(1 for u in nb[v] if m[u]) for v in range(len(m))], dtype=np.float64)
        deg = np.array([len(nb[v]) for v in range(len(m))], dtype=np.float64)
        frac = (cnt + m) / np.maximum(deg + 1.0, 1.0)
        m = frac > 0.5
        if keep is not None:
            m |= keep
    return m


def components(mask, nb):
    """Connected components of masked vertices: list of index arrays, largest first."""
    seen = np.zeros(len(mask), dtype=bool)
    comps = []
    for s in np.nonzero(mask)[0].tolist():
        if seen[s]:
            continue
        stack, cur = [s], []
        seen[s] = True
        while stack:
            v = stack.pop()
            cur.append(v)
            for u in nb[v]:
                if mask[u] and not seen[u]:
                    seen[u] = True
                    stack.append(u)
        comps.append(np.array(cur, dtype=np.int64))
    comps.sort(key=len, reverse=True)
    return comps


# ----------------------------------------------------------------------------------------------- segmentation

def _hand_props(sk, ob, P, W_hand, names_hand, min_w=0.95):
    """Vertices on a hand (weight >= min_w) farther than 12 cm from the wrist: {side: index array}."""
    out = {}
    for j, s in enumerate(("Left", "Right")):
        if names_hand[j] is None:
            continue
        wrist = sk.head.get(s + "Hand")
        if wrist is None:
            continue
        m = (W_hand[:, j] >= min_w) & (np.linalg.norm(P - wrist, axis=1) > 0.12)
        if m.sum() >= 30:
            out[s] = np.nonzero(m)[0]
    return out


def _leg_axis_at(sk, side, zl):
    """Point of the leg axis (UpLeg -> Leg -> Foot heads) at height zl, and the anthropometric radius there."""
    pts = [sk.head.get(side + c) for c in ("UpLeg", "Leg", "Foot")]
    if any(p is None for p in pts):
        return None, None
    rads = [LIMB_R["UpLeg"], LIMB_R["Leg"]]
    for k in range(2):
        a, b = pts[k], pts[k + 1]
        lo, hi = min(a[2], b[2]), max(a[2], b[2])
        if lo - 1e-6 <= zl <= hi + 1e-6 and abs(a[2] - b[2]) > 1e-6:
            t = (zl - a[2]) / (b[2] - a[2])
            r = rads[k][0] + (rads[k][1] - rads[k][0]) * t
            return a + t * (b - a), r * sk.H
    if zl > max(pts[0][2], pts[1][2]):
        return pts[0].copy(), rads[0][0] * sk.H
    if zl < pts[2][2]:
        return pts[2].copy(), rads[1][1] * sk.H
    return None, None


def _coverage(ang_deg, bins=24):
    hist = np.histogram(ang_deg, bins=bins, range=(-180, 180))[0]
    return float((hist > 0).mean())


LEG_LABELS = ("unlabeled", "leg_Left", "leg_Right", "joined", "fused", "free", "thin")


def leg_slices(P, nb, sk):
    """Label the vertices between the ankles and the hip joints by their horizontal cross-section (slabs of 3 % H,
    connected pieces inside each slab): leg_<side> = the leg's own section (it encloses that leg's axis and its
    90th percentile radius is under 2.4 x the anthropometric radius: armour, boots, trousers - not cloth);
    joined = one section around both legs (robe, skirt, or fused thighs); fused = around one leg but much wider
    (a panel or robe half welded to the leg); free = a section around no leg and at least 8 % H across (a hanging
    panel, cape tail, sleeve tail); thin = everything else (poles, blades, specks); unlabeled = outside the slabs. Also the measured bulk
    of each leg (median ratio of its own sections to the anthropometric radius, 0.8..2.2)."""
    H = sk.H
    z = P[:, 2]
    nv = len(P)
    label = np.zeros(nv, dtype=np.int64)
    legs = [s for s in ("Left", "Right") if (s + "UpLeg") in sk.head and (s + "Foot") in sk.head]
    out = {"label": label, "k": {"Left": None, "Right": None}, "joined_frac": 0.0, "slices": 0}
    if len(legs) < 2:
        return out
    ankle = min(sk.head[s + "Foot"][2] for s in legs)
    step = 0.03 * H
    half = 0.035 * H          # connectivity window around each slab (long triangles cross thin slabs)
    z0 = ankle
    z1 = sk.hip_z + 0.01 * H
    ratios = {s: [] for s in legs}
    joined = 0
    nslab = int(max(0, math.floor((z1 - z0) / step)))
    for k in range(nslab):
        lo, hi = z0 + k * step, z0 + (k + 1) * step
        slab = (z >= lo) & (z < hi)
        if slab.sum() < 10:
            continue
        win = np.abs(z - 0.5 * (lo + hi)) < half
        zl = 0.5 * (lo + hi)
        axes = {}
        for s in legs:
            a, r = _leg_axis_at(sk, s, zl)
            if a is not None:
                axes[s] = (a, r)
        if len(axes) < 2:
            continue
        out["slices"] += 1
        any_joined = False
        for wc in components(win, nb):
            cidx = wc[slab[wc]]          # label only the slab's own vertices; shape and connectivity from the window
            if len(cidx) < 6:
                continue
            Q = P[wc, :2]
            enc = []
            for s, (a, r) in axes.items():
                v = Q - a[:2]
                dh = np.linalg.norm(v, axis=1)
                cov = _coverage(np.degrees(np.arctan2(v[:, 1], v[:, 0])))
                if cov >= 0.75 and dh.min() < 3.0 * r:
                    enc.append((s, dh, r))
            if len(enc) >= 2:
                label[cidx] = LEG_LABELS.index("joined")
                any_joined = True
            elif len(enc) == 1:
                s, dh, r = enc[0]
                rm = float(np.percentile(dh, 90))
                if rm < 2.4 * r:
                    label[cidx] = LEG_LABELS.index("leg_" + s)
                    ratios[s].append(rm / r)
                else:
                    label[cidx] = LEG_LABELS.index("fused")
            else:
                ext = float(np.linalg.norm(Q.max(0) - Q.min(0)))
                label[cidx] = LEG_LABELS.index("free" if ext >= 0.08 * H else "thin")
        joined += int(any_joined)
    out["joined_frac"] = round(joined / max(out["slices"], 1), 3)
    for s in legs:
        if len(ratios[s]) >= 2:
            out["k"][s] = round(float(np.clip(np.median(ratios[s]), 0.8, 2.2)), 3)
    return out


def _fib_dirs(n=900, max_tilt_deg=50.0):
    i = np.arange(n) + 0.5
    phi = np.arccos(1.0 - i / n)          # upper hemisphere
    theta = math.pi * (1.0 + 5 ** 0.5) * i
    d = np.stack([np.cos(theta) * np.sin(phi), np.sin(theta) * np.sin(phi), np.cos(phi)], axis=1)
    return d[np.degrees(phi) <= max_tilt_deg]


def find_pole(sk, P, side, body_sd, min_len=1.0):
    """A long straight pole through a hand (banner pole, staff, spear): the line through the fist (middle of the
    hand bone) whose 3.5 cm tube holds the longest run of off-body vertices (2 cm bins along the line), over 900
    directions within 50 deg of vertical, refined by PCA of the tube. Returns {c, d, r, length} or None."""
    hh, ht = sk.head.get(side + "Hand"), sk.tail.get(side + "Hand")
    if hh is None:
        return None
    g = 0.5 * (hh + ht)
    rel = P - g
    near = (np.linalg.norm(rel, axis=1) < 2.6) & ((body_sd > 0.012 * sk.H) | (np.linalg.norm(rel, axis=1) < 0.12))
    R = rel[near]
    if len(R) < 30:
        return None
    best = (0, None)
    dirs = _fib_dirs()
    for chunk in np.array_split(dirs, max(1, len(dirs) // 150)):
        t = R @ chunk.T                                   # (n, k)
        r2 = (R * R).sum(1)[:, None] - t * t
        inl = r2 < 0.035 ** 2
        for j in range(chunk.shape[0]):
            tt = t[inl[:, j], j]
            if len(tt) < 20:
                continue
            cov = len(np.unique(np.floor(tt / 0.02)))
            if cov > best[0]:
                best = (cov, chunk[j])
    if best[1] is None:
        return None
    d = best[1]
    for _ in range(2):
        t = R @ d
        r = np.linalg.norm(R - np.outer(t, d), axis=1)
        inl = r < 0.05
        if inl.sum() < 20:
            return None
        c = R[inl].mean(0)
        _, _, vt = np.linalg.svd(R[inl] - c, full_matrices=False)
        d = vt[0] if vt[0][2] >= 0 else -vt[0]
    t = (R - c) @ d
    r = np.linalg.norm((R - c) - np.outer(t, d), axis=1)
    inl = r < 0.05
    bins = np.unique(np.floor(t[inl] / 0.02))
    length = float(len(bins) * 0.02)
    if length < min_len:
        return None
    # a pole is a thin cylinder: its tube holds a closed ring of vertices around the axis along most of its length
    # (a straight fold or the edge of a hanging sleeve is one-sided)
    Rc = (R - c)[inl]
    tt = t[inl]
    e1 = np.cross(d, [1.0, 0.0, 0.0] if abs(d[0]) < 0.9 else [0.0, 1.0, 0.0])
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(d, e1)
    ang = np.degrees(np.arctan2(Rc @ e2, Rc @ e1))
    segs = np.floor(tt / 0.1)
    ring = [(_coverage(ang[segs == sgm], bins=12) >= 0.6) for sgm in np.unique(segs)]
    ring_frac = float(np.mean(ring)) if ring else 0.0
    if ring_frac < 0.5:
        return None
    return {"c": c + g, "d": d, "r": float(min(max(np.percentile(r[inl], 80) + 0.005, 0.012), 0.06)),
            "length": round(length, 3), "t_range": [float(t[inl].min()), float(t[inl].max())],
            "ring_frac": round(ring_frac, 3)}


def find_banners(sk, P, N, nb, Wh, body_sd, H, taken=None, poles=None):
    """Banner cloth on a pole held in a hand. find_pole gives the pole through the fist; the flag is grown from
    seeds on that hand (weight >= 0.3; generated banners are often skinned to the head or a foot as well) over
    vertices that are off the pole (> radius + 2 cm), within 1.3 m of it, more than 25 cm from the fist, off the
    body (> 3 % H from the capsules and > 30 cm horizontally from the body axis) and within 15 cm of the flag's
    plane. It is a flag when it is at least 25 x 20 cm, thin (3rd PCA axis < 35 % of the 2nd), its plane holds
    the pole direction (|n . pole| < 0.5) and it hangs from the pole away from the fist: >= 15 cm of contact
    along the pole more than 20 cm from the fist, or at the pole top (crossbar). A staff with a sleeve or a book
    next to the hand, or a sword and a shield, fail these tests. Returns [{side, idx, pole_c, pole_d, pole_r,
    size_m, ...}]."""
    out = []
    taken = np.zeros(len(P), dtype=bool) if taken is None else taken.copy()
    axis_xy = sk.centre[:2]
    hdist = np.linalg.norm(P[:, :2] - axis_xy, axis=1)
    for j, side in enumerate(("Left", "Right")):
        pole = poles.get(side) if poles is not None else find_pole(sk, P, side, body_sd)
        if pole is None:
            continue
        c, d, pr = pole["c"], pole["d"], pole["r"]
        g = 0.5 * (sk.head[side + "Hand"] + sk.tail[side + "Hand"])
        rel = P - c
        t = rel @ d
        r = np.linalg.norm(rel - np.outer(t, d), axis=1)
        t_hand = float((g - c) @ d)
        t_top = pole["t_range"][1]
        within = (t > pole["t_range"][0] - 0.1) & (t < t_top + 0.15)
        region = within & (r > pr + 0.02) & (r < 1.3) & (np.linalg.norm(P - g, axis=1) > 0.25) & \
            (body_sd > 0.03 * H) & (hdist > 0.30) & ~taken
        seed = region & (r > pr + 0.05) & (Wh[:, j] >= 0.3)
        if seed.sum() < 40:
            continue
        cq = P[seed].mean(0)
        _, sv, vt = np.linalg.svd(P[seed] - cq, full_matrices=False)
        n = vt[2]
        ok = region & (np.abs((P - cq) @ n) < 0.15)
        flag = seed.copy()
        stack = list(np.nonzero(flag)[0])
        while stack:
            v = stack.pop()
            for u in nb[v]:
                if not flag[u] and ok[u]:
                    flag[u] = True
                    stack.append(u)
        fidx = np.nonzero(flag)[0]
        if len(fidx) < 60:
            continue
        PQ = P[fidx]
        cq = PQ.mean(0)
        _, sv, vt = np.linalg.svd(PQ - cq, full_matrices=False)
        w, h = float(np.ptp(PQ @ vt[0])), float(np.ptp(PQ @ vt[1]))
        thick = float(sv[2] / max(sv[1], 1e-9))
        n_dot = float(abs(vt[2] @ d))
        near = r[fidx] < pr + 0.08
        tc = t[fidx][near]
        contact = float(len(np.unique(np.floor(tc[np.abs(tc - t_hand) > 0.2] / 0.02))) * 0.02) if len(tc) else 0.0
        at_top = bool(len(tc) and tc.max() > t_top - 0.15)
        reason = None
        if w < 0.25 or h < 0.2:
            reason = "too small"
        elif thick > 0.35:
            reason = "not thin"
        elif n_dot > 0.5:
            reason = "plane across the pole"
        elif contact < 0.15 and not at_top:
            reason = "not hanging from the pole"
        info = {"side": side, "size_m": [round(max(w, h), 3), round(min(w, h), 3)], "thickness_ratio": round(thick, 3),
                "pole_len_m": pole["length"], "pole_contact_m": round(contact, 3), "hangs_from_top": at_top,
                "plane_dot_pole": round(n_dot, 3), "vertices": int(len(fidx))}
        if reason:
            info["rejected"] = reason
            out.append(dict(info, idx=None))
            continue
        # the pole itself rides along (pinned, 100 % on the hand): generated poles are often skinned to the head or a
        # foot as well and would leave the flag behind
        pole_m = ((r < pr + 0.012) & (t > pole["t_range"][0] - 0.05) & (t < t_top + 0.1)
                  & (np.linalg.norm(P - g, axis=1) > 0.10) & ~flag & ~taken)
        pidx = np.nonzero(pole_m)[0]
        info["pole_vertices"] = int(len(pidx))
        out.append(dict(info, idx=np.concatenate([fidx, pidx]), flag_idx=fidx, pole_idx=pidx, pole_c=c, pole_d=d,
                        pole_r=pr))
        taken |= flag | pole_m
    return out


def segment(ob, sk, params=None):
    """Loose garment regions of the bound body `ob` (rest pose).

    Returns {"mask": cloth vertex mask, "kind": per-vertex region index (-1 none), "kinds": REGION_KINDS,
    "pin": attachment weight 0..1 per vertex, "cap": cloth vertices that ride along without being simulated
    (downward undersides of a hem, a floor disc), "stats", "banners"}.

    Rules (H = standing height, bind pose):
    - legs: horizontal sections between ankles and hips (leg_slices): a leg's own section (armour, boots,
      trousers) is never cloth, a free hanging section is; sections around both legs (robe) or welded to one leg
      take the standoff rule: off the leg capsules (anthropometric, scaled by the measured bulk of the leg) by more
      than 2 % H when the leg was measured, else leg_margin (3.5 % H; 2.5 % for build 'robed', 5 % for
      'armored'/'heavy'); never the feet;
    - lower garment: those vertices below the hip joints; it must reach below mid-thigh. Robe / skirt when its knee-height ring covers >= 75 % of the turn around the
      pelvis, else tabard (front panel; its back panel is back_panel) or back_panel (cape tail, coat tail);
      rig_humanoid's robe weights (Hips >= 0.3) count too when most slices join both legs (a robe);
    - cape: grown upward from the lower garment behind the back while standing off the torso capsules by > 3 % H
      (a cape lying on the back moves with the back);
    - hood: standing off the head capsule by > 3 % H above the neck, >= 300 vertices;
    - sleeve (robed characters: build 'robed' or a robe / skirt found; params.sleeves true forces): skinned mostly to
      Arm/ForeArm and standing off the forearm capsule by > 3.5 % H;
    - banner: find_banners (flag on a pole held in a hand);
    - boots / greaves: skin carried mostly (>= 0.5) by a shin or foot bone, and within 6.5 % H of the foot (Foot -> ToeBase -> Toe_End and a shaft 10 % H up from the ankle) the mesh
      stays skinned;
    - floor plates (upward faces within 1.5 % H of the floor and their rim below 3 % H) are never cloth; downward undersides of the garment below
      the knees (the hem's closing disc) are cloth that rides along (cap);
    - params.cloth = [{"kind": k, "min": [x,y,z], "max": [x,y,z]}] boxes (metres, bind pose) add vertices of a
      kind, kind "none" removes them, {"only": true} drops every automatic region; params.cloth false = none.
    The mask is smoothed over the mesh graph, islands under 60 vertices dropped, holes under 60 filled."""
    params = dict(params or {})
    me = ob.data
    nv = len(me.vertices)
    P = rest_coords(ob)
    N = vertex_normals(ob)
    E = weld_edges(P, mesh_edges(me))
    nb = adjacency(nv, E)
    H = sk.H
    build = str(params.get("build") or "").lower()
    leg_margin = float(params.get("leg_margin", {"robed": 0.025, "armored": 0.05, "heavy": 0.05}.get(build, 0.035))) * H
    kinds = list(REGION_KINDS)
    kind = np.full(nv, -1, dtype=np.int64)
    bulk = leg_slices(P, nb, sk)
    lab = bulk.pop("label")
    caps = sk.capsules()
    capd = {}
    for c, n, a, b, ra, rb in caps:
        side = "Left" if c.startswith("Left") else ("Right" if c.startswith("Right") else None)
        k = bulk["k"].get(side) if side and c.endswith(("UpLeg", "Leg")) else None
        capd[c] = capsule_sd(P, a, b, ra * (k or 1.0), rb * (k or 1.0))

    def sd(c):
        return capd[c][0] if c in capd else np.full(nv, np.inf)

    legs = [s for s in ("Left", "Right") if (s + "UpLeg") in capd]
    leg_sd = np.full(nv, np.inf)
    margin_v = np.full(nv, leg_margin)
    for s in legs:
        d = np.minimum(sd(s + "UpLeg"), sd(s + "Leg"))
        closer = d < leg_sd
        leg_sd = np.where(closer, d, leg_sd)
        if bulk["k"].get(s):
            margin_v = np.where(closer, 0.02 * H, margin_v)
        else:
            margin_v = np.where(closer, leg_margin, margin_v)
    foot_sd = np.min(np.stack([np.minimum(sd(s + "Foot"), sd(s + "ToeBase")) for s in legs]), axis=0) \
        if legs else np.full(nv, np.inf)
    body_sd = np.min(np.stack([v[0] for v in capd.values()]), axis=0) if capd else np.full(nv, np.inf)
    hand_names = [sk.name("LeftHand"), sk.name("RightHand")]
    Wh = weight_matrix(ob, [n or "" for n in hand_names])
    props = _hand_props(sk, ob, P, Wh, hand_names)              # rigid props (staff, sword, pole)
    prop_mask = np.zeros(nv, dtype=bool)
    for idx in props.values():
        prop_mask[idx] = True

    auto = params.get("cloth", "auto")
    boxes = auto if isinstance(auto, list) else []
    only_boxes = any(bool(b.get("only")) for b in boxes)
    off = auto in (False, "off", "none")
    stats = {"leg_margin_m": round(leg_margin, 4), "build": build or None, "leg_bulk": bulk}
    z = P[:, 2]
    rel = P - sk.centre
    back_off = rel @ sk.back
    lat_off = rel @ sk.left
    floor_h = (z < 0.015 * H) & (np.abs(N[:, 2]) > 0.7)

    # ---- banners first: a flag is not a robe even when it hangs low
    banners = []
    poles = {s: find_pole(sk, P, s, body_sd) for s in ("Left", "Right")} if not off else {}
    if params.get("banners", True) and not off:
        banners = find_banners(sk, P, N, nb, Wh, body_sd, H, poles=poles)
        if banners:
            stats["banner"] = [{k: v for k, v in b.items() if k not in ("idx", "flag_idx", "pole_idx", "pole_c", "pole_d")}
                               for b in banners]
        banners = [b for b in banners if b.get("idx") is not None]
        for bn in banners:
            kind[bn["idx"]] = kinds.index("banner")
    banner_mask = kind == kinds.index("banner")
    # a pole held in a hand (staff, spear, banner pole) is never garment cloth, even where the generator fused it to the
    # robe and the rig gave it robe weights (it would flop on the floor like a ribbon)
    # (only a real prop: the tube within 40 cm of the fist is mostly rigid on that hand - a straight sleeve edge is not)
    for j_, side in enumerate(("Left", "Right")):
        pole = (poles or {}).get(side)
        if pole is None:
            continue
        rel_ = P - pole["c"]
        t_ = rel_ @ pole["d"]
        r_ = np.linalg.norm(rel_ - np.outer(t_, pole["d"]), axis=1)
        zone = (r_ < pole["r"] + 0.02) & (t_ > pole["t_range"][0] - 0.05) & (t_ < pole["t_range"][1] + 0.05)
        fist = 0.5 * (sk.head[side + "Hand"] + sk.tail[side + "Hand"])
        near = zone & (np.linalg.norm(P - fist, axis=1) < 0.4)
        rigid = float((Wh[near, j_] >= 0.95).mean()) if near.any() else 0.0
        stats.setdefault("poles_held", {})[side] = {"vertices": int(zone.sum()), "rigid_near_fist": round(rigid, 3)}
        if rigid >= 0.6:
            prop_mask |= zone & ~banner_mask

    if not only_boxes and not off:
        # ---- lower garment
        below = z < sk.hip_z + 0.01 * H
        LL = {k: i for i, k in enumerate(LEG_LABELS)}
        standoff = (leg_sd > margin_v) & (foot_sd > 0.02 * H)
        by_rule = np.isin(lab, [LL["joined"], LL["fused"], LL["unlabeled"]])
        lower = below & ~prop_mask & ~banner_mask & (((lab == LL["free"]) & (foot_sd > 0.02 * H)) | (by_rule & standoff))
        # boots: within 6.5 % H of the foot (shaft from the ankle up 10 % H, foot to the toe tip) the mesh is the
        # boot, not a hem
        boot = np.zeros(nv, dtype=bool)
        for s_ in legs:
            pts = [sk.head.get(s_ + c_) for c_ in ("Foot", "ToeBase", "Toe_End")]
            pts = [p_ for p_ in pts if p_ is not None]
            if not pts:
                continue
            segs = [(pts[0], pts[0] + np.array([0.0, 0.0, 0.10 * H]))] + list(zip(pts[:-1], pts[1:]))
            for a_, b_ in segs:
                d_, _t = capsule_sd(P, a_, b_, 0.0, 0.0)
                boot |= d_ < 0.065 * H
        lower &= ~boot
        stats["boot_zone_vertices"] = int(boot.sum())
        stats["lower_candidates"] = int(lower.sum())
        stats["leg_sections"] = {k: int((lab == i).sum()) for k, i in LL.items()}
        hips_n = sk.name("Hips")
        if hips_n and bulk["joined_frac"] >= 0.5:
            wh = weight_matrix(ob, [hips_n])[:, 0]
            lower |= below & by_rule & (wh >= 0.3) & (leg_sd > 0.4 * leg_margin) & (foot_sd > 0.02 * H) & ~prop_mask & ~banner_mask
        # parts skinned mostly to an arm (sleeve tails hanging beside a robe) ride the arm: not lower-garment cloth
        allb = [g.name for g in ob.vertex_groups]
        arm_bones = {sk.name(sd_ + c_) for sd_ in ("Left", "Right") for c_ in ("Shoulder", "Arm", "ForeArm", "Hand")}
        arm_idx = [j for j, nm in enumerate(allb) if nm in arm_bones]
        on_arm = np.zeros(nv, dtype=bool)
        if arm_idx:
            Wall = weight_matrix(ob, allb)
            on_arm = np.isin(Wall.argmax(1), arm_idx) & (Wall.max(1) > 0)
        # skin carried mostly by a shin or a foot is a boot or a greave (rig_humanoid gives robes hips + thigh weights)
        shin_bones = {sk.name(sd_ + c_) for sd_ in ("Left", "Right") for c_ in ("Leg", "Foot", "ToeBase", "Toe_End")}
        shin_idx = [j for j, nm in enumerate(allb) if nm in shin_bones]
        on_shin = np.zeros(nv, dtype=bool)
        if shin_idx and arm_idx:
            on_shin = np.isin(Wall.argmax(1), shin_idx) & (Wall.max(1) >= 0.5)
        elif shin_idx:
            Wall = weight_matrix(ob, allb)
            on_shin = np.isin(Wall.argmax(1), shin_idx) & (Wall.max(1) >= 0.5)
        on_arm |= on_shin
        stats["shin_skinned_excluded"] = int((on_shin & lower).sum())
        # floor plates (a generator pedestal / ground disc) and the rim around them are never cloth
        plate = floor_h.copy()
        for _ in range(2):
            grow = plate.copy()
            for v in np.nonzero(plate)[0].tolist():
                for u in nb[v]:
                    if z[u] < 0.03 * H:
                        grow[u] = True
            plate = grow
        stats["floor_plate_vertices"] = int(plate.sum())
        lower &= ~on_arm & ~boot & ~plate
        lower = majority_filter(lower, nb, iters=2) & ~banner_mask & ~on_arm & ~boot & ~plate
        stats["arm_skinned_excluded"] = int((on_arm & below).sum())
        stats["lower_filtered"] = int(lower.sum())
        comps = components(lower, nb)
        lower = np.zeros(nv, dtype=bool)
        for cidx in comps:
            # a garment hangs from above the knees to below mid-thigh; flaps / pouches at the belt and boot heels
            # are not cloth to simulate
            if len(cidx) >= 60 and z[cidx].min() < 0.5 * (sk.hip_z + sk.knee_z) and z[cidx].max() > sk.knee_z:
                lower[cidx] = True
        # floor plates: a pedestal (wider than the garment) is never cloth
        if lower.any():
            fp = components(lower & (z < 0.02 * H), nb)
            gl = lower & (z > 0.03 * H)
            lo_xy = P[gl, :2].min(0) - 0.05 if gl.any() else None
            hi_xy = P[gl, :2].max(0) + 0.05 if gl.any() else None
            for cidx in fp:
                Q = P[cidx, :2]
                if lo_xy is None or (Q.min(0) < lo_xy).any() or (Q.max(0) > hi_xy).any():
                    lower[cidx] = False
                    stats["pedestal_vertices"] = stats.get("pedestal_vertices", 0) + int(len(cidx))
        if lower.any():
            ring = lower & (np.abs(z - sk.knee_z) < 0.06 * H)
            ang = np.degrees(np.arctan2(lat_off[ring], -back_off[ring])) if ring.any() else np.zeros(0)
            cover = _coverage(ang) if len(ang) else 0.0
            front_share = float((back_off[lower] < 0).mean())
            if cover >= 0.75:
                lk = "robe" if bulk["joined_frac"] >= 0.5 or z[lower].min() < 0.12 * H else "skirt"
            elif front_share >= 0.5:
                lk = "tabard"
            else:
                lk = "back_panel"
            kind[lower] = kinds.index(lk)
            if lk == "tabard":
                bk = lower & (back_off > 0.0)
                if bk.sum() >= 60:
                    kind[bk] = kinds.index("back_panel")
            elif lk == "back_panel":
                fr = lower & (back_off < 0.0)
                if fr.sum() >= 60:
                    kind[fr] = kinds.index("tabard")
            stats["lower"] = {"kind": lk, "vertices": int(lower.sum()), "knee_ring_coverage": round(cover, 3),
                              "front_share": round(front_share, 3)}
        # ---- cape: grow upward from the lower garment behind the back, only where it stands off the torso
        sp = [c for c in ("Spine", "Spine1", "Spine2") if c in capd]
        torso_sd = np.min(np.stack([sd(c) for c in sp]), axis=0) if sp else np.full(nv, np.inf)
        band = (z >= sk.hip_z - 0.02 * H) & (z < sk.shoulder_z - 0.04 * H)
        cand_cape = band & (back_off > 0.04 * H) & (torso_sd > 0.03 * H) & (N @ sk.back > 0.0) & ~prop_mask & \
            ~banner_mask & (kind < 0)
        seed = (kind >= 0) & ~banner_mask & (back_off > 0.02 * H)
        cape = np.zeros(nv, dtype=bool)
        if seed.any() and cand_cape.any():
            vis = seed.copy()
            stack = list(np.nonzero(seed)[0])
            while stack:
                v = stack.pop()
                for u in nb[v]:
                    if not vis[u] and cand_cape[u]:
                        vis[u] = True
                        cape[u] = True
                        stack.append(u)
            cape = majority_filter(cape, nb, iters=1) & (kind < 0)
            keep = np.zeros(nv, dtype=bool)
            for cidx in components(cape, nb):
                if len(cidx) >= 80:
                    keep[cidx] = True
            cape = keep
            if cape.any():
                kind[cape] = kinds.index("cape")
                stats["cape"] = {"vertices": int(cape.sum()), "top_z": round(float(z[cape].max()), 3)}
        # ---- hood
        if "Head" in capd and params.get("hood", True):
            hood = (z > sk.neck_z) & (sd("Head") > 0.03 * H) & (sd("Neck") > 0.02 * H) & ~prop_mask & ~banner_mask
            hood &= (rel @ sk.fwd) < 0.04 * H   # not a visor / face plate in front
            hood = majority_filter(hood, nb, iters=1)
            hm = np.zeros(nv, dtype=bool)
            for cidx in components(hood, nb):
                if len(cidx) >= 300:
                    hm[cidx] = True
            if hm.any():
                kind[hm & (kind < 0)] = kinds.index("hood")
                stats["hood"] = {"vertices": int(hm.sum())}
        # ---- sleeves wider than the arm (skinned to the arm by the rig)
        robed = build == "robed" or stats.get("lower", {}).get("kind") in ("robe", "skirt")
        if params.get("sleeves", "auto") is True or (params.get("sleeves", "auto") == "auto" and robed):
            arm_names = {s: [sk.name(s + c) for c in ("Arm", "ForeArm")] for s in ("Left", "Right")}
            allb = [g.name for g in ob.vertex_groups]
            Wall = weight_matrix(ob, allb)
            dom = np.array(allb, dtype=object)[Wall.argmax(1)] if len(allb) else np.array([None] * nv)
            for s in ("Left", "Right"):
                if (s + "ForeArm") not in capd:
                    continue
                fa = sd(s + "ForeArm")
                t = capd[s + "ForeArm"][1]
                on_arm = np.isin(dom, [n for n in arm_names[s] if n])
                sl = on_arm & (fa > 0.035 * H) & (fa < 0.15 * H) & (t > 0.2) & ~prop_mask & ~banner_mask & (kind < 0)
                if (s + "Hand") in capd:
                    sl &= sd(s + "Hand") > 0.015 * H
                sl = majority_filter(sl, nb, iters=1) & (kind < 0)
                m = np.zeros(nv, dtype=bool)
                for cidx in components(sl, nb):
                    if len(cidx) >= 80:
                        m[cidx] = True
                if m.any():
                    kind[m] = kinds.index("sleeve")
                    stats.setdefault("sleeve", {})[s] = int(m.sum())
    # ---- explicit boxes
    for bx in boxes:
        lo = np.array(bx.get("min", [-9, -9, -9]), dtype=np.float64)
        hi = np.array(bx.get("max", [9, 9, 9]), dtype=np.float64)
        inside = np.all((P >= lo) & (P <= hi), axis=1)
        k = bx.get("kind", "robe")
        if k == "none":
            kind[inside] = -1
        else:
            if k not in kinds:
                raise ValueError("params.cloth box kind must be one of %s or none: %r" % (kinds, k))
            kind[inside] = kinds.index(k)
    mask = kind >= 0
    # holes (small non-cloth islands inside cloth) and specks
    for cidx in components(~mask, nb):
        if len(cidx) < 60:
            nbk = [kind[u] for v in cidx.tolist() for u in nb[v] if kind[u] >= 0]
            if nbk:
                kind[cidx] = max(set(nbk), key=nbk.count)
    mask = kind >= 0
    for cidx in components(mask, nb):
        if len(cidx) < 60:
            kind[cidx] = -1
    mask = kind >= 0
    lower_k = np.isin(kind, [kinds.index(k) for k in ("robe", "skirt", "tabard", "back_panel")])
    cap = lower_k & (z < min(sk.knee_z, 0.2 * H)) & (N[:, 2] < -0.5)
    pin = attachment_weights(P, E, nv, nb, kind, kinds, sk, banners, cap, floor_h, foot_sd)
    regions = {}
    for j, k in enumerate(kinds):
        m = kind == j
        if m.any():
            regions[k] = {"vertices": int(m.sum()), "z": [round(float(z[m].min()), 3), round(float(z[m].max()), 3)],
                          "preset": REGION_PRESET[k]}
    stats["regions"] = regions
    stats["cloth_vertices"] = int(mask.sum())
    stats["cap_vertices"] = int(cap.sum())
    stats["poles"] = {s: {"length_m": p["length"], "radius_m": round(p["r"], 3)} for s, p in poles.items() if p}
    return {"mask": mask, "kind": kind, "kinds": kinds, "pin": pin, "cap": cap, "stats": stats,
            "banners": banners, "poles": {s: p for s, p in poles.items() if p}, "P": P, "edges": E}


def attachment_weights(P, E, nv, nb, kind, kinds, sk, banners, cap, floor_h=None, foot_sd=None):
    """Pin weight (1 = follows the skin, 0 = free cloth) per vertex: geodesic ramps from where each garment
    hangs from - the seam with the body above the knees (waist, back, shoulders), the waist band, the neck of a
    hood, the elbow side of a sleeve, the pole edge (and top edge) of a banner. Seams below the knees (hem welded to
    boots, shins or a floor disc by the generator) are not pinned: they are hidden under the garment and pinning
    them would hold the hem stiff."""
    H = sk.H
    z = P[:, 2]
    pin = np.ones(nv)
    cloth = kind >= 0
    pin[cloth] = 0.0
    if not cloth.any():
        return pin
    seam = np.zeros(nv, dtype=bool)
    for v in np.nonzero(cloth)[0].tolist():
        for u in nb[v]:
            if not cloth[u]:
                seam[v] = True
                break
    K = {k: kinds.index(k) for k in kinds}
    ramp = np.full(nv, 0.08 * H)
    seeds = np.zeros(nv, dtype=bool)
    for k in ("robe", "skirt", "tabard", "back_panel"):
        m = kind == K[k]
        if not m.any():
            continue
        top = float(z[m].max())
        # hangs from the waist: the seam above the knees and everything within 3 % H of the garment top
        seeds |= m & ((seam & (z > sk.knee_z + 0.05 * H)) | (z > top - 0.03 * H))
        ramp[m] = (0.07 if k != "tabard" else 0.06) * H
    for k in ("cape",):
        m = kind == K[k]
        if m.any():
            top = float(z[m].max())
            seeds |= m & ((seam & (z > sk.hip_z)) | (z > top - 0.04 * H))
            ramp[m] = 0.12 * H
    m = kind == K["hood"]
    if m.any():
        seeds |= m & (seam | (z < sk.neck_z + 0.04 * H))
        ramp[m] = 0.10 * H
    m = kind == K["sleeve"]
    if m.any():
        seeds |= m & seam
        ramp[m] = 0.08 * H
    for bn in banners:
        idx = bn["idx"]
        if len(bn.get("pole_idx", [])):
            seeds[bn["pole_idx"]] = True
        rel = P[idx] - bn["pole_c"]
        t = rel @ bn["pole_d"]
        r = np.linalg.norm(rel - np.outer(t, bn["pole_d"]), axis=1)
        top_t = float(t.max())
        edge = (r < bn["pole_r"] + 0.07) | (t > top_t - 0.03)
        seeds[idx[edge]] = True
        ramp[idx] = 0.10
    # seam vertices above the knees are always pinned (welded to the body)
    seeds |= seam & cloth & ~cap & (z > sk.knee_z)
    d = geodesic(nv, E, P, seeds & cloth)
    w = 1.0 - smoothstep(d / np.maximum(ramp, 1e-6))
    w[~np.isfinite(d)] = 0.0
    pin[cloth] = w[cloth]
    # waist band: everything of a lower garment above the hip joints follows the pelvis
    for k in ("robe", "skirt", "tabard", "back_panel"):
        m = kind == K[k]
        pin[m & (z > sk.hip_z)] = 1.0
    for bn in banners:
        if len(bn.get("pole_idx", [])):
            pin[bn["pole_idx"]] = 1.0
    # a hood keeps its shape: never freer than 0.6
    m = kind == K["hood"]
    pin[m] = np.maximum(pin[m], 0.6)
    pin[cap & cloth] = 0.0
    return pin


# ----------------------------------------------------------------------------------------------- garment groups

def garment_groups(seg, sk, presets=None):
    """Garment group per vertex (None = body): lower (robe / skirt / tabard / back panel / cape), hood, sleeve_l/r,
    banner_l/r - each becomes its own cloth object with its own preset (dominant region kind, or params.presets
    {group or kind: preset name or {settings}})."""
    kind, kinds, P = seg["kind"], seg["kinds"], seg["P"]
    names = np.array([None] * len(kind), dtype=object)
    side = np.where((P - sk.centre) @ sk.left > 0, "l", "r")
    for j, k in enumerate(kinds):
        m = kind == j
        if not m.any():
            continue
        if k == "sleeve":
            for i in np.nonzero(m)[0].tolist():
                names[i] = "sleeve_" + side[i]
        elif k == "banner":
            continue
        elif k == "hood":
            names[m] = "hood"
        else:
            names[m] = "lower"
    for bn in seg["banners"]:
        names[bn["idx"]] = "banner_" + bn["side"][0].lower()
    groups = {}
    presets = presets or {}
    for g in sorted({n for n in names.tolist() if n}):
        m = names == g
        ks = kind[m]
        dom = kinds[int(np.bincount(ks[ks >= 0], minlength=len(kinds)).argmax())]
        pre = presets.get(g, presets.get(dom, REGION_PRESET[dom]))
        if isinstance(pre, str):
            if pre not in PRESETS:
                raise ValueError("unknown cloth preset %r (have %s)" % (pre, sorted(PRESETS)))
            settings = dict(PRESETS[pre])
            name = pre
        else:
            base = PRESETS.get(pre.get("base", REGION_PRESET[dom]), PRESETS["wool_robe"])
            settings = dict(base, **{k: v for k, v in pre.items() if k != "base"})
            name = "custom"
        groups[g] = {"preset": name, "settings": settings, "kinds": sorted({kinds[i] for i in np.unique(ks)}),
                     "vertices": int(m.sum())}
        if g.startswith("banner"):
            bn = next(b for b in seg["banners"] if b["side"][0].lower() == g[-1])
            groups[g]["hand"] = sk.name(bn["side"] + "Hand")
    return names, groups


# ----------------------------------------------------------------------------------------------- split

def _poly_vertices(me):
    ls = np.empty(len(me.polygons), dtype=np.int64)
    lt = np.empty(len(me.polygons), dtype=np.int64)
    me.polygons.foreach_get("loop_start", ls)
    me.polygons.foreach_get("loop_total", lt)
    lv = np.empty(len(me.loops), dtype=np.int64)
    me.loops.foreach_get("vertex_index", lv)
    return ls, lt, lv


def _delete_faces(ob, face_mask):
    """Delete the masked faces of a mesh object (and vertices left without faces); shape-key layers are kept."""
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    bm.faces.ensure_lookup_table()
    doomed = [bm.faces[i] for i in np.nonzero(face_mask)[0].tolist()]
    if doomed:
        bmesh.ops.delete(bm, geom=doomed, context="FACES_ONLY")
    loose = [v for v in bm.verts if not v.link_faces]
    if loose:
        bmesh.ops.delete(bm, geom=loose, context="VERTS")
    bm.to_mesh(ob.data)
    bm.free()
    ob.data.update()


def split_garments(body, seg, names, groups, caps="delete"):
    """Separate the faces of every garment group into an object Body_cloth_<group> (a copy of the body with the
    other faces deleted: UVs, material, vertex groups and the Armature modifier stay); delete them from the body.
    A face belongs to a group when all its vertices do. Seam vertices exist in both objects. Integer attribute
    af_src_index = the body vertex the vertex came from; vertex groups af_cloth_pin / af_cloth_free (1 - pin)
    and attribute af_cap (rides along, not simulated). Returns {group: object}."""
    me = body.data
    nv = len(me.vertices)
    att = me.attributes.get("af_src_index") or me.attributes.new("af_src_index", "INT", "POINT")
    att.data.foreach_set("value", np.arange(nv, dtype=np.int32))
    ls, lt, lv = _poly_vertices(me)
    face_group = np.array([None] * len(ls), dtype=object)
    for fi in range(len(ls)):
        vs = lv[ls[fi]: ls[fi] + lt[fi]]
        g = names[vs[0]]
        if g is not None and all(names[v] == g for v in vs[1:]):
            face_group[fi] = g
    out = {}
    coll = body.users_collection[0] if body.users_collection else bpy.context.scene.collection
    for g in groups:
        fm = face_group == g
        if fm.sum() < 20:
            continue
        ob = body.copy()
        ob.data = body.data.copy()
        ob.name = "Body_cloth_" + g
        ob.data.name = ob.name
        coll.objects.link(ob)
        if ob.data.shape_keys is not None:
            ob.shape_key_clear()
        _delete_faces(ob, ~fm)
        src = np.empty(len(ob.data.vertices), dtype=np.int32)
        ob.data.attributes["af_src_index"].data.foreach_get("value", src)
        pin = seg["pin"][src]
        cap = seg["cap"][src]
        pg = ob.vertex_groups.get(PIN_GROUP) or ob.vertex_groups.new(name=PIN_GROUP)
        fg = ob.vertex_groups.get(FREE_GROUP) or ob.vertex_groups.new(name=FREE_GROUP)
        for i in range(len(src)):
            pg.add([i], float(pin[i]), "REPLACE")
            fg.add([i], float(1.0 - pin[i]), "REPLACE")
        ca = ob.data.attributes.new("af_cap", "INT", "POINT")
        ca.data.foreach_set("value", cap.astype(np.int32))
        if caps == "delete" and cap.any():
            # the generator closes a robe's hem with a disc / underside: under simulation the hem moves and that disc
            # would stand up like a lid, so the garment is left open (its inside renders like the inside of cloth)
            me_ = ob.data
            ls_, lt_, lv_ = _poly_vertices(me_)
            fm_ = np.array([cap[lv_[ls_[i]: ls_[i] + lt_[i]]].all() for i in range(len(ls_))], dtype=bool)
            if fm_.any():
                _delete_faces(ob, fm_)
        ob[CLOTH_ROLE] = "render"
        ob["af_cloth_group"] = g
        out[g] = ob
    whole = np.isin(face_group, list(out.keys()))
    _delete_faces(body, whole)
    return out


# ----------------------------------------------------------------------------------------------- proxy


def transfer_weights(dst, src, names=None):
    """Vertex-group weights of `src` interpolated onto `dst` (both meshes, world rest positions): nearest point on
    the triangulated source surface, barycentric blend of its triangle's vertex weights."""
    from mathutils.bvhtree import BVHTree
    from mathutils.geometry import barycentric_transform
    names = names or [vg.name for vg in src.vertex_groups]
    Ws = weight_matrix(src, names)
    bm = bmesh.new()
    bm.from_mesh(src.data)
    bm.transform(src.matrix_world)
    bmesh.ops.triangulate(bm, faces=list(bm.faces))
    bm.faces.ensure_lookup_table()
    bm.verts.ensure_lookup_table()
    tri = np.array([[v.index for v in f.verts] for f in bm.faces], dtype=np.int64)
    co = np.array([v.co[:] for v in bm.verts])
    bvh = BVHTree.FromBMesh(bm)
    bm.free()
    Md = dst.matrix_world
    nd = len(dst.data.vertices)
    Wd = np.zeros((nd, len(names)))
    for v in dst.data.vertices:
        p = Md @ v.co
        loc, _n, fi, _d = bvh.find_nearest(p)
        if fi is None:
            continue
        a, b, c = (Vector(co[k].tolist()) for k in tri[fi])
        # barycentric coordinates of loc in (a, b, c)
        v0, v1, v2 = b - a, c - a, loc - a
        d00, d01, d11 = v0.dot(v0), v0.dot(v1), v1.dot(v1)
        d20, d21 = v2.dot(v0), v2.dot(v1)
        den = d00 * d11 - d01 * d01
        if abs(den) < 1e-18:
            w = np.array([1.0, 0.0, 0.0])
        else:
            l1 = (d11 * d20 - d01 * d21) / den
            l2 = (d00 * d21 - d01 * d20) / den
            w = np.clip(np.array([1.0 - l1 - l2, l1, l2]), 0.0, 1.0)
            w /= max(w.sum(), 1e-9)
        Wd[v.index] = w @ Ws[tri[fi]]
    for vg in list(dst.vertex_groups):
        dst.vertex_groups.remove(vg)
    for j, nm in enumerate(names):
        col = Wd[:, j]
        nz = np.nonzero(col > 1e-5)[0]
        if not len(nz) and nm not in (PIN_GROUP, FREE_GROUP):
            continue
        vg = dst.vertex_groups.new(name=nm)
        for i in nz.tolist():
            vg.add([i], float(col[i]), "REPLACE")
    return Wd


def build_proxy(render_ob, edge_m=0.045, min_faces=300, max_faces=4000, hand=None, remesh=False):
    """Simulation proxy of a render cloth object: floor caps removed, welded, UVs dropped, remeshed into even quads
    of about edge_m by QuadriFlow (boundaries kept) and triangulated, the skin / pin weights interpolated from the
    render cloth (banners: 100 % on the holding hand). Falls back to collapse decimation when QuadriFlow fails.
    remesh False (default): the simulation mesh is the garment itself without its riding caps (integer attribute
    af_render_index maps every proxy vertex to its garment vertex: the garment takes the simulated positions
    exactly and only the caps are carried by bind_embedded). True: a QuadriFlow proxy (lighter, but a remeshed
    surface smooths flaps and layers, and the garment carried through it tears where they were).
    Returns the proxy object (same parent and transform as the render cloth)."""
    px = render_ob.copy()
    px.data = render_ob.data.copy()
    ria = px.data.attributes.new("af_render_index", "INT", "POINT")
    ria.data.foreach_set("value", np.arange(len(px.data.vertices), dtype=np.int32))
    px.name = render_ob.name.replace("Body_cloth_", "AF_cloth_proxy_")
    px.data.name = px.name
    for c in render_ob.users_collection:
        c.objects.link(px)
    px.modifiers.clear()
    me = px.data
    for uv in list(me.uv_layers):
        me.uv_layers.remove(uv)
    cap = np.zeros(len(me.vertices), dtype=np.int32)
    if "af_cap" in me.attributes:
        me.attributes["af_cap"].data.foreach_get("value", cap)
    for nm in ("af_cap", "af_src_index") + (("af_render_index",) if remesh else ()):
        if nm in me.attributes:
            me.attributes.remove(me.attributes[nm])
    for ca in list(me.color_attributes):
        me.color_attributes.remove(ca)
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.verts.ensure_lookup_table()
    gone = [bm.verts[i] for i in np.nonzero(cap)[0].tolist()]
    if gone:
        bmesh.ops.delete(bm, geom=gone, context="VERTS")
    bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=1e-4)
    # QuadriFlow wants a manifold surface with consistent normals: no wire edges, loose vertices or fans
    for _ in range(3):
        bad = [e for e in bm.edges if len(e.link_faces) > 2]
        if not bad:
            break
        bmesh.ops.delete(bm, geom=list({fc for e in bad for fc in e.link_faces}), context="FACES")
    wire = [e for e in bm.edges if not e.link_faces]
    if wire:
        bmesh.ops.delete(bm, geom=wire, context="EDGES")
    loose = [v for v in bm.verts if not v.link_faces]
    if loose:
        bmesh.ops.delete(bm, geom=loose, context="VERTS")
    bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
    area = sum(f.calc_area() for f in bm.faces)
    bm.to_mesh(me)
    bm.free()
    me.update()
    target = int(np.clip(area / (edge_m * edge_m), min_faces, max_faces))
    if not remesh:
        names = [vg.name for vg in render_ob.vertex_groups]
        nvp = len(px.data.vertices)
        if hand:
            for vg in list(px.vertex_groups):
                if vg.name not in (PIN_GROUP, FREE_GROUP):
                    px.vertex_groups.remove(vg)
            hg = px.vertex_groups.new(name=hand)
            hg.add(list(range(nvp)), 1.0, "REPLACE")
        px[CLOTH_ROLE] = "proxy"
        px["af_cloth_group"] = render_ob.get("af_cloth_group", "")
        px["af_proxy_method"] = "garment"
        px.hide_render = True
        px.display_type = "WIRE"
        return px
    method = "quadriflow"
    vl = bpy.context.view_layer
    prev = vl.objects.active
    for o in vl.objects:
        o.select_set(False)
    px.select_set(True)
    vl.objects.active = px
    ok = False
    nv0 = len(me.vertices)
    try:
        r = bpy.ops.object.quadriflow_remesh(target_faces=target, use_preserve_sharp=False,
                                             use_preserve_boundary=True, use_mesh_symmetry=False, seed=1)
        ok = "FINISHED" in r and 0.2 * target < len(px.data.polygons) < 3.0 * target
    except Exception:
        ok = False
    if not ok:
        method = "decimate"
        mod = px.modifiers.new("dec", "DECIMATE")
        mod.decimate_type = "COLLAPSE"
        mod.ratio = min(1.0, 2.0 * target / max(len(px.data.polygons), 1))
        mod.use_collapse_triangulate = True
        vl.update()
        dg = bpy.context.evaluated_depsgraph_get()
        new = bpy.data.meshes.new_from_object(px.evaluated_get(dg), depsgraph=dg)
        px.modifiers.remove(mod)
        old = px.data
        px.data = new
        bpy.data.meshes.remove(old)
    me = px.data
    me.name = px.name
    bm = bmesh.new()
    bm.from_mesh(me)
    bmesh.ops.triangulate(bm, faces=list(bm.faces), quad_method="BEAUTY")
    bmesh.ops.dissolve_degenerate(bm, dist=1e-4, edges=list(bm.edges))
    for _ in range(3):                      # Surface Deform needs a manifold target
        bad = [e for e in bm.edges if len(e.link_faces) > 2]
        if not bad:
            break
        bmesh.ops.delete(bm, geom=list({fc for e in bad for fc in e.link_faces}), context="FACES")
    loose = [v for v in bm.verts if not v.link_faces]
    if loose:
        bmesh.ops.delete(bm, geom=loose, context="VERTS")
    bm.to_mesh(me)
    bm.free()
    me.update()
    if prev is not None:
        vl.objects.active = prev
    names = [vg.name for vg in render_ob.vertex_groups]
    transfer_weights(px, render_ob, names)
    nvp = len(px.data.vertices)
    if hand:
        for vg in list(px.vertex_groups):
            if vg.name not in (PIN_GROUP, FREE_GROUP):
                px.vertex_groups.remove(vg)
        hg = px.vertex_groups.new(name=hand)
        hg.add(list(range(nvp)), 1.0, "REPLACE")
    px[CLOTH_ROLE] = "proxy"
    px["af_cloth_group"] = render_ob.get("af_cloth_group", "")
    px["af_proxy_method"] = method
    px.hide_render = True
    px.display_type = "WIRE"
    return px


# ----------------------------------------------------------------------------------------------- colliders

def capsule_mesh(name, length, ra, rb, seg=10, rings=3):
    """Closed tapered capsule along +Y from 0 to length, radius ra at 0 and rb at length."""
    verts, faces = [], []
    profile = []
    for i in range(rings, 0, -1):          # bottom hemisphere
        a = (math.pi / 2) * i / rings
        profile.append((-ra * math.sin(a), ra * math.cos(a)))
    profile.append((0.0, ra))
    profile.append((length, rb))
    for i in range(1, rings + 1):          # top hemisphere
        a = (math.pi / 2) * i / rings
        profile.append((length + rb * math.sin(a), rb * math.cos(a)))
    profile = [p for p in profile if p[1] > 1e-5]
    for y, r in profile:
        for k in range(seg):
            t = 2 * math.pi * k / seg
            verts.append((r * math.cos(t), y, r * math.sin(t)))
    nr = len(profile)
    for i in range(nr - 1):
        for k in range(seg):
            a = i * seg + k
            b = i * seg + (k + 1) % seg
            faces.append((a, a + seg, b + seg, b))
    bot = len(verts)
    verts.append((0.0, -ra, 0.0))
    top = len(verts)
    verts.append((0.0, length + rb, 0.0))
    for k in range(seg):
        faces.append((bot, k, (k + 1) % seg))
        faces.append((top, (nr - 1) * seg + (k + 1) % seg, (nr - 1) * seg + k))
    me = bpy.data.meshes.new(name)
    me.from_pydata(verts, [], faces)
    me.validate()
    me.update()
    return me


def _collider(ob, thickness=0.01, friction=1.0):
    ob.modifiers.new("Collision", "COLLISION")
    cs = ob.collision
    cs.thickness_outer = thickness
    cs.thickness_inner = 0.05
    cs.cloth_friction = friction
    cs.damping = 0.1
    ob.hide_render = True
    ob.display_type = "WIRE"
    ob[CLOTH_ROLE] = "collider"


def build_colliders(sk, rig, leg_k=None, inflate=0.006, poles=(), floor=True, skip=(), caps=None, friction=1.0):
    """Bone-parented capsules of the body (anthropometric radii, legs scaled by their measured bulk, + inflate),
    capsules along held poles (parented to the holding hand) and a floor plane. Returns {name: object}; every
    collider is in collection AF_CLOTH_COLLIDERS."""
    coll = bpy.data.collections.get("AF_CLOTH_COLLIDERS") or bpy.data.collections.new("AF_CLOTH_COLLIDERS")
    if coll.name not in bpy.context.scene.collection.children:
        bpy.context.scene.collection.children.link(coll)
    out = {}
    pos = rig.data.pose_position
    rig.data.pose_position = "REST"
    bpy.context.view_layer.update()
    W = rig.matrix_world
    for c, bname, a, b, ra, rb in (caps if caps is not None else sk.capsules()):
        if c in skip:
            continue
        side = "Left" if c.startswith("Left") else ("Right" if c.startswith("Right") else None)
        k = (leg_k or {}).get(side) if (caps is None and side and c.endswith(("UpLeg", "Leg"))) else None
        ra, rb = ra * (k or 1.0) + inflate, rb * (k or 1.0) + inflate
        bone = rig.data.bones[bname]
        M = W @ bone.matrix_local                       # bone head frame (rest), +Y along the bone
        local_b = M.inverted() @ Vector(b.tolist())     # the next joint in bone-local coordinates
        L = local_b.length
        me = capsule_mesh("AF_col_" + c, L, ra, rb)
        ob = bpy.data.objects.new("AF_col_" + c, me)
        coll.objects.link(ob)
        ob.parent = rig
        ob.parent_type = "BONE"
        ob.parent_bone = bname
        q = Vector((0.0, 1.0, 0.0)).rotation_difference(local_b.normalized()) if L > 1e-6 else Quaternion()
        bpy.context.view_layer.update()
        ob.matrix_world = M @ q.to_matrix().to_4x4()
        _collider(ob, friction=friction)      # low friction: legs push a robe, they do not drag it up
        out[ob.name] = ob
    for i, pole in enumerate(poles):
        hand = pole["hand"]
        c0, d = Vector(pole["a"]), Vector(pole["b"])
        L = (d - c0).length
        me = capsule_mesh("AF_col_pole_%d" % i, L, pole["r"] + inflate, pole["r"] + inflate, seg=8, rings=2)
        ob = bpy.data.objects.new("AF_col_pole_%d" % i, me)
        coll.objects.link(ob)
        ob.parent = rig
        ob.parent_type = "BONE"
        ob.parent_bone = hand
        q = Vector((0.0, 1.0, 0.0)).rotation_difference((d - c0).normalized())
        bpy.context.view_layer.update()
        ob.matrix_world = Matrix.Translation(c0) @ q.to_matrix().to_4x4()
        ob["af_pole_side"] = pole.get("side", "")
        _collider(ob, thickness=0.008)
        out[ob.name] = ob
    if floor:
        me = bpy.data.meshes.new("AF_col_floor")
        s = 5.0
        z = -0.003
        me.from_pydata([(-s, -s, z), (s, -s, z), (s, s, z), (-s, s, z)], [], [(0, 1, 2, 3)])
        ob = bpy.data.objects.new("AF_col_floor", me)
        coll.objects.link(ob)
        _collider(ob, thickness=0.004, friction=3.0)
        ob.collision.thickness_inner = 0.5          # cloth pulled under the floor is still pushed back up
        out[ob.name] = ob
    rig.data.pose_position = pos
    bpy.context.view_layer.update()
    return out


def collider_collection(name, objects):
    coll = bpy.data.collections.get(name) or bpy.data.collections.new(name)
    for o in objects:
        if o.name not in coll.objects:
            coll.objects.link(o)
    return coll


# ----------------------------------------------------------------------------------------------- cloth setup

def setup_cloth(px, rig, settings, coll, fps=30.0, internal=False):
    """Armature + Cloth modifiers on a proxy (pin group af_cloth_pin, colliders only from `coll`)."""
    am = px.modifiers.get("Armature") or px.modifiers.new("Armature", "ARMATURE")
    am.object = rig
    am.use_vertex_groups = True
    m = px.modifiers.new("AF_Cloth", "CLOTH")
    s = m.settings
    s.quality = int(settings["quality"])
    s.mass = float(settings["mass"])
    s.air_damping = float(settings["air_damping"])
    s.tension_stiffness = float(settings["tension"])
    s.compression_stiffness = float(settings["compression"])
    s.shear_stiffness = float(settings["shear"])
    s.bending_stiffness = float(settings["bending"])
    s.tension_damping = float(settings["tension_damping"])
    s.compression_damping = float(settings["compression_damping"])
    s.shear_damping = float(settings["shear_damping"])
    s.bending_damping = float(settings["bending_damping"])
    s.vertex_group_mass = PIN_GROUP
    s.pin_stiffness = float(settings["pin_stiffness"])
    s.time_scale = 30.0 / float(fps)     # Blender cloth steps ~1/30 s per frame whatever the scene fps
    if internal:
        s.use_internal_springs = True
        s.internal_spring_max_length = 0.06
        s.internal_spring_max_diversion = math.radians(30)
        s.internal_spring_normal_check = True
        s.internal_tension_stiffness = 10.0
        s.internal_compression_stiffness = 10.0
    c = m.collision_settings
    c.use_collision = True
    c.collision_quality = int(settings["collision_quality"])
    c.distance_min = float(settings["distance_min"])
    c.use_self_collision = False
    c.collection = coll
    return m


# ----------------------------------------------------------------------------------------------- timeline

def clip_curves(action):
    return [(fc.data_path, fc.array_index, fc) for fc in A.fcurves_of(action)]


def sim_timeline(n, loop, preroll=20, warm=None, blend=None, from_bind=0):
    """Frames of the simulation of one clip of n frames (0..n-1).

    one-shot: hold the first pose `preroll` frames, then the clip; loop (period P = n-1): hold, a warm-up of W
    frames of the cycle before frame 0 (W = clamp(P, 24, 48)), one cycle; the last B frames are cross-faded
    toward the warm-up frames one period earlier (B = clamp(P/3, 6, 15)) so the cache loops seamlessly.
    The simulation starts from the bind pose and eases into the first pose over from_bind frames before the hold:
    Blender cloth takes its spring rest lengths from the first simulated frame, and a skinned first pose may
    already be torn (robe weights split between hips and thighs) - starting in the bind pose keeps the garment's
    modelled proportions. "blend_in" = per sim frame the share of the clip pose (0 = bind pose).
    Returns {"times": clip time per sim frame (sim frames are 1..F), "offset": index of clip time 0, ...}."""
    ease = [float(smoothstep(np.array(k / float(max(from_bind, 1))))) for k in range(from_bind)]
    if loop and n > 2:
        P = n - 1
        W = int(min(max(P, 24), 48)) if warm is None else int(warm)
        B = int(min(max(P // 3, 6), 15, P - 1)) if blend is None else int(blend)
        t0 = float(-W % P)
        times = [t0] * (from_bind + preroll) + [float(t % P) for t in range(-W, P + 1)]
        blend_in = ease + [1.0] * (len(times) - from_bind)
        return {"times": times, "blend_in": blend_in, "offset": from_bind + preroll + W, "loop": True, "period": P,
                "warm": W, "blend": B, "preroll": preroll, "from_bind": from_bind}
    times = [0.0] * (from_bind + preroll) + [float(t) for t in range(n)]
    blend_in = ease + [1.0] * (len(times) - from_bind)
    return {"times": times, "blend_in": blend_in, "offset": from_bind + preroll, "loop": False, "period": None,
            "warm": 0, "blend": 0, "preroll": preroll, "from_bind": from_bind}


def timeline_action(rig, action, timeline, name):
    """A temporary layered action that plays `action` along the simulation timeline (sim frame f = 1..F)."""
    act, slot, bag = A.new_layered_action(name, rig)
    act.use_fake_user = False
    times = timeline["times"]
    F = len(times)
    frames = np.arange(1, F + 1, dtype=np.float64)
    lin = bpy.types.Keyframe.bl_rna.properties["interpolation"].enum_items["LINEAR"].value
    mix = np.array(timeline.get("blend_in") or [1.0] * F)
    for path, idx, fc in clip_curves(action):
        vals = np.array([fc.evaluate(t) for t in times])
        if path.startswith("pose.bones") and (mix < 1.0).any():
            prop = path.rsplit(".", 1)[-1]
            rest = {"rotation_quaternion": 1.0 if idx == 0 else 0.0, "scale": 1.0}.get(prop, 0.0)
            if prop in ("location", "rotation_quaternion", "scale", "rotation_euler"):
                vals = rest + mix * (vals - rest)
        nf = bag.fcurves.new(path, index=idx)
        nf.keyframe_points.add(F)
        co = np.empty(2 * F)
        co[0::2] = frames
        co[1::2] = vals
        nf.keyframe_points.foreach_set("co", co)
        nf.keyframe_points.foreach_set("interpolation", np.full(F, lin, dtype=np.int32))
        nf.update()
    act.use_frame_range = True
    act.frame_start, act.frame_end = 1, F
    return act


def read_points(ob, dg=None):
    """Evaluated vertex positions of a mesh object in its object space (N,3) float32."""
    dg = dg or bpy.context.evaluated_depsgraph_get()
    ev = ob.evaluated_get(dg)
    me = ev.to_mesh()
    try:
        co = np.empty(len(me.vertices) * 3, dtype=np.float32)
        me.vertices.foreach_get("co", co)
    finally:
        ev.to_mesh_clear()
    return co.reshape(-1, 3)


def simulate(rig, proxies, action, timeline, tag="clip"):
    """Run every proxy's cloth along the timeline (fresh cache) and return ({proxy name: (F, N, 3)}, seconds)."""
    import time as _time
    scene = bpy.context.scene
    F = len(timeline["times"])
    act = timeline_action(rig, action, timeline, "AF_SIM_" + tag)
    A.assign_action(rig, act)
    for px in proxies:
        m = px.modifiers["AF_Cloth"]
        m.show_viewport = True
        m.point_cache.frame_start = 1
        m.point_cache.frame_end = F + 1      # any change of the cache range frees it: a fresh simulation
        m.point_cache.frame_end = F
    scene.frame_start, scene.frame_end = 1, F
    out = {px.name: np.empty((F, len(px.data.vertices), 3), dtype=np.float32) for px in proxies}
    t0 = _time.time()
    for f in range(1, F + 1):
        scene.frame_set(f)
        dg = bpy.context.evaluated_depsgraph_get()
        for px in proxies:
            out[px.name][f - 1] = read_points(px, dg)
    secs = _time.time() - t0
    rig.animation_data.action = None
    bpy.data.actions.remove(act)
    return out, secs


def skinned(rig, proxies, action, frames):
    """Proxy positions under pure skinning (cloth off) at the given clip frames: {name: (len(frames), N, 3)}."""
    scene = bpy.context.scene
    A.assign_action(rig, action)
    mods = [px.modifiers["AF_Cloth"] for px in proxies]
    for m in mods:
        m.show_viewport = False
    out = {px.name: np.empty((len(frames), len(px.data.vertices), 3), dtype=np.float32) for px in proxies}
    for i, f in enumerate(frames):
        scene.frame_set(int(f))
        dg = bpy.context.evaluated_depsgraph_get()
        for px in proxies:
            out[px.name][i] = read_points(px, dg)
    for m in mods:
        m.show_viewport = True
    return out


def clip_window(S, timeline, n):
    """Clip frames 0..n-1 out of a simulation array S (F, N, 3); loops get the cross-faded seam."""
    o = timeline["offset"]
    if not timeline["loop"]:
        return S[o: o + n].copy()
    P, B = timeline["period"], timeline["blend"]
    out = S[o: o + P + 1].copy()                       # clip time 0..P
    for t in range(P - B, P + 1):
        w = float(smoothstep(np.array((t - (P - B)) / float(B))))
        out[t] = (1.0 - w) * S[o + t] + w * S[o + t - P]
    out[P] = out[0]
    return out


# ----------------------------------------------------------------------------------------------- caches

def _key_action(key, name):
    """Layered action with a KEY slot for a shape-key datablock."""
    act = bpy.data.actions.new(name)
    act.use_fake_user = True
    slot = act.slots.new(id_type="KEY", name=key.name)
    layer = act.layers.new("Layer")
    strip = layer.strips.new(type="KEYFRAME")
    bag = strip.channelbag(slot, ensure=True)
    return act, slot, bag


def assign_key_action(key, action):
    for kb in key.key_blocks:
        kb.value = 0.0
    if key.animation_data is None:
        key.animation_data_create()
    ad = key.animation_data
    ad.action = action
    if action is not None and len(action.slots):
        try:
            ad.action_slot = action.slots[0]
        except Exception:
            pass


# ----------------------------------------------------------------------------------------------- preview

def _cam_dir(view):
    dirs = {"three_quarter": (-0.62, -0.62, 0.48), "front": (0.0, -1.0, 0.2), "left": (-1.0, 0.0, 0.2),
            "back": (0.0, 1.0, 0.2), "right": (1.0, 0.0, 0.2), "back_quarter": (0.62, 0.62, 0.48)}
    return Vector(dirs.get(view, dirs["three_quarter"])).normalized()


def render_frames(path, objects, shots, size=300, view="three_quarter", color="TEXTURE", labels=None, cols=None,
                  title=None):
    """Contact sheet of shots [(setup callable, frame)] rendered with Workbench from one fixed orthographic camera
    framing every shot (the framing is measured on the evaluated meshes of all shots first)."""
    import os
    try:
        from steps import _export_lib as xl
    except ImportError:  # pragma: no cover
        import _export_lib as xl
    scene = bpy.context.scene
    pts = []
    for setup, f in shots:
        setup()
        scene.frame_set(int(f))
        dg = bpy.context.evaluated_depsgraph_get()
        for o in objects:
            co = read_points(o, dg).astype(np.float64)
            M = np.array(o.matrix_world)
            pts.append(co[:: max(1, len(co) // 2000)] @ M[:3, :3].T + M[:3, 3])
    P = np.concatenate(pts)
    d = _cam_dir(view)
    up = Vector((0, 0, 1))
    right = d.cross(up).normalized() * -1.0
    upv = right.cross(d).normalized() * -1.0
    R = np.array(right)
    U = np.array(upv)
    cx, cy = P @ R, P @ U
    centre = Vector(((cx.min() + cx.max()) / 2 * R + (cy.min() + cy.max()) / 2 * U + P.mean(0) @ np.array(d) * np.array(d)).tolist())
    extent = max(cx.max() - cx.min(), cy.max() - cy.min()) * 1.06
    cam_data = bpy.data.cameras.new("af_cloth_cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = extent
    cam_data.clip_end = 100
    cam = bpy.data.objects.new("af_cloth_cam", cam_data)
    scene.collection.objects.link(cam)
    cam.location = centre + d * 10.0
    cam.rotation_euler = (-d).to_track_quat("-Z", "Y").to_euler()
    prev = scene.camera
    scene.camera = cam
    r = scene.render
    af_engine = "BLENDER_WORKBENCH"
    r.engine = af_engine
    r.resolution_x = r.resolution_y = int(size)
    r.resolution_percentage = 100
    r.film_transparent = True
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGBA"
    sh = scene.display.shading
    sh.light = "STUDIO"
    sh.color_type = color
    sh.show_cavity = False
    hidden = []
    for o in scene.objects:
        if o.type == "MESH" and o not in objects and not o.hide_render:
            o.hide_render = True
            hidden.append(o)
    tiles = []
    tmp = os.path.join(os.path.dirname(os.path.abspath(path)), "_cloth_frames")
    os.makedirs(tmp, exist_ok=True)
    for i, (setup, f) in enumerate(shots):
        setup()
        scene.frame_set(int(f))
        one = os.path.join(tmp, "s%03d.png" % i)
        r.filepath = one
        bpy.ops.render.render(write_still=True)
        tiles.append(xl.load_png(one))
    for o in hidden:
        o.hide_render = False
    scene.camera = prev
    bpy.data.objects.remove(cam)
    bpy.data.cameras.remove(cam_data)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    sheet = xl.tile_preview(tiles, cols or len(tiles), size, size, labels=labels, title=title)
    xl.save_png(path, sheet)
    return path


# ----------------------------------------------------------------------------------------------- QA

def capsules_rest(sk, leg_k=None):
    """Body capsules without inflation (the body the cloth must not enter), legs scaled by their measured bulk."""
    out = []
    for c, bname, a, b, ra, rb in sk.capsules():
        side = "Left" if c.startswith("Left") else ("Right" if c.startswith("Right") else None)
        k = (leg_k or {}).get(side) if (side and c.endswith(("UpLeg", "Leg"))) else None
        out.append((c, bname, a, b, ra * (k or 1.0), rb * (k or 1.0)))
    return out


def posed_capsules(rig, caps):
    """The rest capsules carried by the current pose of their bones (world)."""
    W = np.array(rig.matrix_world)
    out = []
    for c, bname, a, b, ra, rb in caps:
        pb = rig.pose.bones.get(bname)
        if pb is None:
            continue
        D = np.array(rig.matrix_world @ pb.matrix @ pb.bone.matrix_local.inverted() @ rig.matrix_world.inverted())
        out.append((c, D[:3, :3] @ a + D[:3, 3], D[:3, :3] @ b + D[:3, 3], ra, rb))
    return out


def penetration(Pw, caps, mask=None):
    """Deepest entry (m, >= 0) of each point into the posed body capsules."""
    depth = np.zeros(len(Pw))
    for c, a, b, ra, rb in caps:
        sd, _ = capsule_sd(Pw, a, b, ra, rb)
        depth = np.maximum(depth, -sd)
    if mask is not None:
        depth = np.where(mask, depth, 0.0)
    return depth


def edge_ratio(P, edges, rest_len):
    L = np.linalg.norm(P[edges[:, 0]] - P[edges[:, 1]], axis=1)
    return L / np.maximum(rest_len, 1e-9)


def clip_qa(rig, caps, px, sim, skin, frames, pin, depth_tol=0.01, stretch_tol=1.3):
    """Per clip and proxy: share of free cloth points deeper than depth_tol inside the body (simulated and the
    skinned baseline, worst sampled frame), deepest entry, edge stretch (99th percentile and max of length / rest
    length, share of edges over stretch_tol), floor (lowest point), NaN. sim / skin: (len(frames), N, 3) object
    space of the proxy at those clip frames."""
    scene = bpy.context.scene
    M = np.array(px.matrix_world)
    me = px.data
    E = mesh_edges(me)
    rest = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", rest)
    rest = rest.reshape(-1, 3)
    rest_len = np.linalg.norm(rest[E[:, 0]] - rest[E[:, 1]], axis=1)
    free = pin < 0.5
    loose = pin < 0.2
    fe = loose[E[:, 0]] & loose[E[:, 1]]        # edges of the free cloth (pinned / ramp edges follow the skin)
    if not fe.any():
        fe = np.ones(len(E), dtype=bool)
    res = {"frames": len(frames), "free_points": int(free.sum()), "nan": int(np.isnan(sim).sum())}
    pen_sim, pen_skin, dmax_sim, dmax_skin = [], [], 0.0, 0.0
    st99, stmax, stover, lows = [], 0.0, 0.0, []
    sk99, skover = [], 0.0
    for i, f in enumerate(frames):
        scene.frame_set(int(f))
        pc = posed_capsules(rig, caps)
        Ps = sim[i].astype(np.float64) @ M[:3, :3].T + M[:3, 3]
        Pk = skin[i].astype(np.float64) @ M[:3, :3].T + M[:3, 3]
        ds = penetration(Ps, pc, free)
        dk = penetration(Pk, pc, free)
        nf = max(int(free.sum()), 1)
        pen_sim.append(float((ds > depth_tol).sum()) / nf)
        pen_skin.append(float((dk > depth_tol).sum()) / nf)
        dmax_sim = max(dmax_sim, float(ds.max()))
        dmax_skin = max(dmax_skin, float(dk.max()))
        r = edge_ratio(sim[i].astype(np.float64), E, rest_len)[fe]
        st99.append(float(np.percentile(r, 99)))
        stmax = max(stmax, float(r.max()))
        stover = max(stover, float((r > stretch_tol).mean()))
        rk = edge_ratio(skin[i].astype(np.float64), E, rest_len)[fe]
        sk99.append(float(np.percentile(rk, 99)))
        skover = max(skover, float((rk > stretch_tol).mean()))
        lows.append(float(Ps[free, 2].min()) if free.any() else float(Ps[:, 2].min()))
    res.update({"penetration_frac": round(max(pen_sim), 4), "penetration_frac_skinned": round(max(pen_skin), 4),
                "penetration_mean": round(float(np.mean(pen_sim)), 4),
                "penetration_mean_skinned": round(float(np.mean(pen_skin)), 4),
                "depth_max_cm": round(100 * dmax_sim, 1), "depth_max_skinned_cm": round(100 * dmax_skin, 1),
                "stretch_p99": round(max(st99), 3), "stretch_max": round(stmax, 3),
                "stretch_over_frac": round(stover, 4), "stretch_p99_skinned": round(max(sk99), 3),
                "stretch_over_frac_skinned": round(skover, 4), "floor_min_cm": round(100 * min(lows), 1)})
    return res


def lying_height(P, mask):
    """98th percentile height of the masked points (how high a lying garment stands)."""
    if not mask.any():
        return 0.0
    return float(np.percentile(P[mask, 2], 98))


# ----------------------------------------------------------------------------------------------- cloth bones (GLB)

def _proxy_rest(px):
    me = px.data
    co = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", co)
    M = np.array(px.matrix_world)
    return co.reshape(-1, 3) @ M[:3, :3].T + M[:3, 3]


def chain_layout(px, pin, spacing=0.2, joints=4, max_chains=10, min_len=0.1):
    """Secondary bone chains over a proxy garment (world rest positions). Roots are spread along the attachment
    line (pinned vertices next to free ones) by farthest-point sampling ~spacing apart; every vertex gets soft
    chain weights from its geodesic distance to each root (Gaussian in the difference to the nearest root); the
    joints of a chain sit at equal levels of geodesic distance from the attachment line over that chain's share.
    Returns {"chains": [{"root", "joints" (J+1, 3), "bands" [vertex index arrays], "length"}], "W" (N, K) chain
    shares, "dline" (N) distance from the attachment line, "joints": J}."""
    P = _proxy_rest(px)
    nv = len(P)
    E = mesh_edges(px.data)
    nb = adjacency(nv, E)
    pinned = pin >= 0.95
    free = ~pinned
    line = np.zeros(nv, dtype=bool)
    for v in np.nonzero(pinned)[0].tolist():
        if any(free[u] for u in nb[v]):
            line[v] = True
    if not line.any():
        top = P[:, 2] >= np.percentile(P[:, 2], 97)
        line = top
    dline = geodesic(nv, E, P, line)
    dline[~np.isfinite(dline)] = 0.0
    cand = np.nonzero(line)[0]
    roots = [int(cand[np.argmin(np.linalg.norm(P[cand] - P[cand].mean(0), axis=1))])]
    mind = np.linalg.norm(P[cand] - P[roots[0]], axis=1)
    while len(roots) < max_chains:
        k = int(np.argmax(mind))
        if mind[k] < spacing:
            break
        roots.append(int(cand[k]))
        mind = np.minimum(mind, np.linalg.norm(P[cand] - P[cand[k]], axis=1))
    G = []
    for r in roots:
        seed = np.zeros(nv, dtype=bool)
        seed[r] = True
        g = geodesic(nv, E, P, seed)
        g[~np.isfinite(g)] = 1e3
        G.append(g)
    G = np.stack(G, axis=1)                                     # (N, K)
    sigma = 0.5 * spacing
    gmin = G.min(1, keepdims=True)
    W = np.exp(-((G - gmin) / sigma) ** 2)
    W /= W.sum(1, keepdims=True)
    owner = np.argmax(W, axis=1)
    chains, keep = [], []
    for k, r in enumerate(roots):
        reg = np.nonzero((owner == k) & free)[0]
        if len(reg) < 20:
            continue
        L = float(dline[reg].max())
        if L < min_len:
            continue
        levels = [L * j / joints for j in range(joints + 1)]
        band_w = 0.5 * L / joints
        pts, bands = [], []
        for j, lv in enumerate(levels):
            if j == 0:
                b = np.array([r], dtype=np.int64)
            else:
                b = reg[np.abs(dline[reg] - lv) <= band_w]
                if len(b) == 0:
                    b = reg[np.argsort(np.abs(dline[reg] - lv))[:3]]
            bands.append(b)
            pts.append(P[b].mean(0))
        chains.append({"root": r, "joints": np.array(pts), "bands": bands, "length": L})
        keep.append(k)
    if not chains:
        return {"chains": [], "W": np.zeros((nv, 0)), "dline": dline, "joints": joints}
    W = W[:, keep]
    W /= np.maximum(W.sum(1, keepdims=True), 1e-9)
    return {"chains": chains, "W": W, "dline": dline, "joints": joints}


def chain_vertex_weights(layout, P, src_tree_pts, pin):
    """Per vertex of a garment (world rest P) the weights of the chain bones (N, K*J) from the nearest proxy point:
    chain share x position along the chain (linear between the two nearest bone middles)."""
    J = layout["joints"]
    K = len(layout["chains"])
    kd = KDTree(len(src_tree_pts))
    for i, p in enumerate(src_tree_pts):
        kd.insert(Vector(p.tolist()), i)
    kd.balance()
    near = np.array([kd.find(Vector(p.tolist()))[1] for p in P], dtype=np.int64)
    Wc = layout["W"][near]                       # (N, K)
    d = layout["dline"][near]
    out = np.zeros((len(P), K * J))
    for k, ch in enumerate(layout["chains"]):
        s = np.clip(d / max(ch["length"], 1e-6) * J - 0.5, 0.0, J - 1.0)
        b0 = np.floor(s).astype(np.int64)
        f = s - b0
        b1 = np.minimum(b0 + 1, J - 1)
        out[np.arange(len(P)), k * J + b0] += Wc[:, k] * (1.0 - f)
        out[np.arange(len(P)), k * J + b1] += Wc[:, k] * f
    return out


def add_chain_bones(rig, group, layout, attach):
    """Edit bones cloth_<group>_<k>_<j> along the chain joints (armature space), the first bone of a chain parented
    to `attach` (its own attachment bone per chain), the rest connected. Returns [[bone names per chain]]."""
    names = []
    Winv = rig.matrix_world.inverted()
    vl = bpy.context.view_layer
    prev_active = vl.objects.active
    for o in vl.objects:
        o.select_set(False)
    rig.select_set(True)
    vl.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")
    eb = rig.data.edit_bones
    for k, ch in enumerate(layout["chains"]):
        row = []
        parent = eb.get(attach[k])
        for j in range(len(ch["joints"]) - 1):
            nm = "cloth_%s_%d_%d" % (group, k, j)
            b = eb.new(nm)
            b.head = Winv @ Vector(ch["joints"][j].tolist())
            b.tail = Winv @ Vector(ch["joints"][j + 1].tolist())
            if (b.tail - b.head).length < 1e-4:
                b.tail = b.head + Vector((0.0, 0.0, -0.01))
            b.parent = parent
            b.use_connect = False          # heads follow the cloth (translation keys)
            b.use_deform = True
            parent = b
            row.append(nm)
        names.append(row)
    bpy.ops.object.mode_set(mode="OBJECT")
    if prev_active is not None:
        vl.objects.active = prev_active
    return names


def chain_attach_bones(px, layout, deform_names):
    """Attachment bone of each chain: the deform bone carrying most weight at its root band (pinned skin)."""
    out = []
    idx = {vg.index: vg.name for vg in px.vertex_groups if vg.name in deform_names}
    for ch in layout["chains"]:
        acc = {}
        v = px.data.vertices[int(ch["root"])]
        for g in v.groups:
            if g.group in idx:
                acc[idx[g.group]] = acc.get(idx[g.group], 0.0) + g.weight
        out.append(max(acc, key=acc.get) if acc else deform_names[0])
    return out


def joints_from_points(layout, pts_world):
    """Joint positions of every chain (K, J+1, 3) from garment points (proxy order) of one frame."""
    return np.stack([np.stack([pts_world[b].mean(0) for b in ch["bands"]]) for ch in layout["chains"]])


def _swing(a, b):
    a = Vector(a).normalized()
    b = Vector(b).normalized()
    return a.rotation_difference(b)


def chain_rotations(rig, names, joints_seq, frames_pose, follow_heads=True):
    """Pose of chain bones following joint positions: {bone: [(location, quaternion) per frame]}. names [[bone per
    chain]]; joints_seq (n, K, J+1, 3) world; frames_pose: callable(i) that puts the rig into frame i. Every bone
    turns its axis toward the next joint from where its parent left it (swing only: it keeps its rest roll); with
    follow_heads (bones not connected) its head is also translated onto its own joint, so the chain sits exactly
    on the cloth even where the cloth crumples or stretches (glTF translation channels)."""
    W = rig.matrix_world
    Winv = W.inverted()
    n = len(joints_seq)
    out = {nm: [] for row in names for nm in row}
    bones = rig.data.bones
    for i in range(n):
        frames_pose(i)
        for k, row in enumerate(names):
            M_parent = None
            for j, nm in enumerate(row):
                b = bones[nm]
                if j == 0:
                    par = b.parent
                    M_par = rig.pose.bones[par.name].matrix if par else Matrix.Identity(4)
                    R_rel = (par.matrix_local.inverted() @ b.matrix_local) if par else b.matrix_local.copy()
                else:
                    M_par = M_parent
                    R_rel = b.parent.matrix_local.inverted() @ b.matrix_local
                M0 = M_par @ R_rel                                  # bone with an identity pose
                loc = Vector((0.0, 0.0, 0.0))
                if follow_heads and not b.use_connect:
                    loc = M0.inverted() @ (Winv @ Vector(joints_seq[i][k][j].tolist()))
                Mh = M0 @ Matrix.Translation(loc)
                head = Mh.to_translation()
                target = Winv @ Vector(joints_seq[i][k][j + 1].tolist())
                y0 = (Mh.to_3x3() @ Vector((0.0, 1.0, 0.0)))
                want = target - head
                if want.length < 1e-6 or y0.length < 1e-9:
                    q = Quaternion()
                else:
                    d = _swing(y0, want)
                    R_new = d.to_matrix() @ Mh.to_3x3()
                    q = (Mh.to_3x3().inverted() @ R_new).to_quaternion()
                q.normalize()
                if out[nm] and out[nm][-1][1].dot(q) < 0:
                    q.negate()
                out[nm].append((loc.copy(), q))
                M_parent = Mh @ q.to_matrix().to_4x4()
    return out


def write_bone_keys(action, poses, n):
    """Location and rotation keys of chain bones on frames 0..n-1 into a layered clip action."""
    bag = A.channelbag(action)
    lin = bpy.types.Keyframe.bl_rna.properties["interpolation"].enum_items["LINEAR"].value
    frames = np.arange(n, dtype=np.float64)
    for nm, seq in poses.items():
        base = 'pose.bones["%s"].' % nm
        for fc in list(bag.fcurves):
            if fc.data_path.startswith(base):
                bag.fcurves.remove(fc)
        grp = bag.groups.get(nm) or bag.groups.new(nm)
        rot = np.array([[q.w, q.x, q.y, q.z] for _l, q in seq])
        loc = np.array([[l.x, l.y, l.z] for l, _q in seq])
        for path, arr in (("rotation_quaternion", rot), ("location", loc)):
            for c in range(arr.shape[1]):
                fc = bag.fcurves.new(base + path, index=c)
                fc.group = grp
                fc.keyframe_points.add(n)
                co = np.empty(2 * n)
                co[0::2] = frames
                co[1::2] = arr[:n, c]
                fc.keyframe_points.foreach_set("co", co)
                fc.keyframe_points.foreach_set("interpolation", np.full(n, lin, dtype=np.int32))
                fc.update()


def spring_joints(rig, layout, attach, caps, action, n, loop, stiffness=0.08, damping=0.12, substeps=2,
                  gravity=9.81, fps=30.0, cycles=2):
    """Joint positions (n, K, J+1, 3) of a spring / lag chain (Verlet) for clips without a simulation: the root
    joint rides its attachment bone, the others swing with gravity, damping and a soft pull toward the rigidly
    carried rest shape, keep their lengths, stay out of the body capsules and above the floor. Loops run `cycles`
    times and keep the last one with a cross-faded seam."""
    scene = bpy.context.scene
    K = len(layout["chains"])
    rest = np.stack([ch["joints"] for ch in layout["chains"]])           # (K, J+1, 3)
    seglen = np.linalg.norm(rest[:, 1:] - rest[:, :-1], axis=2)
    A.assign_action(rig, action)
    W = rig.matrix_world
    pbs = [rig.pose.bones[a] for a in attach]
    total = n * (cycles if loop else 1) + (0 if loop else 15)
    seq = []
    x = rest.copy()
    xp = rest.copy()
    dt = 1.0 / fps / substeps
    for step in range(total):
        f = (step % max(n - 1, 1)) if loop else max(0, step - 15)
        scene.frame_set(int(min(f, n - 1)))
        D = [np.array(W @ pb.matrix @ pb.bone.matrix_local.inverted() @ W.inverted()) for pb in pbs]
        target = np.stack([rest[k] @ D[k][:3, :3].T + D[k][:3, 3] for k in range(K)])
        pc = posed_capsules(rig, caps) if caps else []
        for _ in range(substeps):
            v = (x - xp) * (1.0 - damping)
            xp = x.copy()
            x = x + v
            x[:, :, 2] -= gravity * dt * dt
            x += stiffness * (target - x)
            x[:, 0] = target[:, 0]
            for _ in range(4):
                for j in range(1, x.shape[1]):
                    d = x[:, j] - x[:, j - 1]
                    L = np.maximum(np.linalg.norm(d, axis=1, keepdims=True), 1e-9)
                    x[:, j] = x[:, j - 1] + d / L * seglen[:, j - 1:j]
            flat = x.reshape(-1, 3)
            for c, a, b, ra, rb in pc:
                sd, t = capsule_sd(flat, a, b, ra, rb)
                inside = sd < 0
                if inside.any():
                    ab = b - a
                    axis_pt = a + t[:, None] * ab
                    nrm = flat - axis_pt
                    nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-9)
                    flat[inside] -= nrm[inside] * sd[inside, None]
            flat[:, 2] = np.maximum(flat[:, 2], 0.01)
            x = flat.reshape(x.shape)
            x[:, 0] = target[:, 0]
        seq.append(x.copy())
    seq = np.array(seq)
    if loop and n > 2:
        P = n - 1
        last = seq[-P - 1:]
        prev = seq[-2 * P - 1: -P]
        B = int(min(max(P // 3, 6), 15, P - 1))
        out = last.copy()
        for t in range(P - B, P + 1):
            w = float(smoothstep(np.array((t - (P - B)) / float(B))))
            out[t] = (1.0 - w) * last[t] + w * prev[t]
        out[P] = out[0]
        return out[:n]
    return seq[-n:]


def fit_capsules(caps, P_free, margin=0.012, min_scale=0.45):
    """Shrink capsules that the bind-pose garment enters: the anthropometric body is a guess, a skinny body under
    a robe (a skeleton lich) or a tight skirt would otherwise start inside the collision volumes and be pushed
    out every frame. Each capsule's radii are reduced by what the deepest free cloth point needs to stay `margin`
    outside (never below min_scale of the radius). caps: [(canon, bone, a, b, ra, rb)]; returns (caps, {canon:
    shrink m})."""
    out, shrunk = [], {}
    for c, bname, a, b, ra, rb in caps:
        if len(P_free):
            sd, _t = capsule_sd(P_free, a, b, ra, rb)
            need = float(max(0.0, (margin - sd).max()))
        else:
            need = 0.0
        if need > 0:
            ra2, rb2 = max(ra - need, min_scale * ra), max(rb - need, min_scale * rb)
            shrunk[c] = round(need, 4)
            ra, rb = ra2, rb2
        out.append((c, bname, a, b, ra, rb))
    return out, shrunk


def anchor_islands(px, pin, follow=0.6, min_pin=0.5):
    """Pieces of a proxy with no pinned vertex (inner robe layers, loose tatters the generator modelled as separate
    shells) would fall to the floor: they get a soft pin `follow` (goal springs toward the skin) on every vertex.
    Returns (pin, number of anchored islands, vertices)."""
    me = px.data
    nb = adjacency(len(me.vertices), mesh_edges(me))
    pin = pin.copy()
    n_isl, n_v = 0, 0
    for cidx in components(np.ones(len(pin), dtype=bool), nb):
        if pin[cidx].max() < min_pin:
            pin[cidx] = np.maximum(pin[cidx], follow)
            n_isl += 1
            n_v += len(cidx)
    if n_isl:
        pg = px.vertex_groups.get(PIN_GROUP) or px.vertex_groups.new(name=PIN_GROUP)
        for i in range(len(pin)):
            pg.add([i], float(pin[i]), "REPLACE")
    return pin, n_isl, n_v


def render_qa(rig, caps, ob, frames, after, before, depth_tol=0.01, stretch_tol=1.3):
    """Before / after on the visible garment (a render cloth object): `after` = positions with the cloth cache,
    `before` = the same mesh under the original skinning, both (len(frames), N, 3) object space at those clip
    frames. Free cloth = af_cloth_free >= 0.8 and not a riding cap. Per side: share of free points deeper than
    depth_tol inside the posed body capsules (worst frame) and the deepest entry; edge stretch of free edges (99th
    percentile, share over stretch_tol); torn share (free triangles whose area grew > 3x - the tear measure of
    motion_style's clip QA - and the same over the whole garment, pinned band included); lowest free point."""
    scene = bpy.context.scene
    me = ob.data
    nv = len(me.vertices)
    rest = np.empty(nv * 3)
    me.vertices.foreach_get("co", rest)
    rest = rest.reshape(-1, 3)
    fw = np.zeros(nv)
    fg = ob.vertex_groups.get(FREE_GROUP)
    if fg is not None:
        for v in me.vertices:
            for g in v.groups:
                if g.group == fg.index:
                    fw[v.index] = g.weight
    cap = np.zeros(nv, dtype=np.int32)
    if "af_cap" in me.attributes:
        me.attributes["af_cap"].data.foreach_get("value", cap)
    free = (fw >= 0.8) & (cap == 0)
    E = mesh_edges(me)
    fe = free[E[:, 0]] & free[E[:, 1]]
    T = mesh_tris(me)
    ft = free[T].all(1)
    L0 = np.linalg.norm(rest[E[:, 0]] - rest[E[:, 1]], axis=1)
    A0 = 0.5 * np.linalg.norm(np.cross(rest[T[:, 1]] - rest[T[:, 0]], rest[T[:, 2]] - rest[T[:, 0]]), axis=1)
    ok_t = ft & (A0 > 0.25 * float(np.median(A0)))
    ok_all = A0 > 0.25 * float(np.median(A0))          # the whole garment, pinned band included
    M = np.array(ob.matrix_world)
    out = {"free_points": int(free.sum()), "frames": len(frames)}
    if not free.any():
        return out
    acc = {s: {"pen": [], "depth": 0.0, "st99": [], "stover": 0.0, "torn": 0.0, "torn_all": 0.0, "low": []}
           for s in ("after", "before")}
    for i, f in enumerate(frames):
        scene.frame_set(int(f))
        pc = posed_capsules(rig, caps)
        for side, arr in (("after", after), ("before", before)):
            P = arr[i].astype(np.float64) @ M[:3, :3].T + M[:3, 3]
            d = penetration(P, pc, free)
            a = acc[side]
            a["pen"].append(float((d > depth_tol).sum()) / float(free.sum()))
            a["depth"] = max(a["depth"], float(d.max()))
            Pl = arr[i].astype(np.float64)
            r = (np.linalg.norm(Pl[E[:, 0]] - Pl[E[:, 1]], axis=1) / np.maximum(L0, 1e-9))[fe]
            if len(r):
                a["st99"].append(float(np.percentile(r, 99)))
                a["stover"] = max(a["stover"], float((r > stretch_tol).mean()))
            A1 = 0.5 * np.linalg.norm(np.cross(Pl[T[:, 1]] - Pl[T[:, 0]], Pl[T[:, 2]] - Pl[T[:, 0]]), axis=1)
            if ok_t.any():
                torn = ok_t & (A1 > 3.0 * A0)
                a["torn"] = max(a["torn"], float(A1[torn].sum() / max(A1[ok_t].sum(), 1e-12)))
            torn_a = ok_all & (A1 > 3.0 * A0)
            a["torn_all"] = max(a["torn_all"], float(A1[torn_a].sum() / max(A1[ok_all].sum(), 1e-12)))
            a["low"].append(float(P[free, 2].min()))
    for side, a in acc.items():
        out[side] = {"penetration_frac": round(max(a["pen"]), 4), "penetration_mean": round(float(np.mean(a["pen"])), 4),
                     "depth_max_cm": round(100 * a["depth"], 1),
                     "stretch_p99": round(max(a["st99"]), 3) if a["st99"] else None,
                     "stretch_over_frac": round(a["stover"], 4), "torn_frac": round(a["torn"], 4),
                     "torn_frac_garment": round(a["torn_all"], 4),
                     "floor_min_cm": round(100 * min(a["low"]), 1)}
    return out


def set_rigid_weights(ob, bone, deform_names):
    """Every vertex 100 % on one bone (a banner and its pole ride the holding hand)."""
    for vg in list(ob.vertex_groups):
        if vg.name in deform_names and vg.name != bone:
            ob.vertex_groups.remove(vg)
    hg = ob.vertex_groups.get(bone) or ob.vertex_groups.new(name=bone)
    hg.add(list(range(len(ob.data.vertices))), 1.0, "REPLACE")


# ----------------------------------------------------------------------------------------------- proxy -> render


def _object_rest(ob):
    me = ob.data
    co = np.empty(len(me.vertices) * 3)
    if me.shape_keys is not None and me.shape_keys.reference_key is not None:
        me.shape_keys.reference_key.data.foreach_get("co", co)
    else:
        me.vertices.foreach_get("co", co)
    return co.reshape(-1, 3)


def _barycentric(p, a, b, c):
    v0, v1, v2 = b - a, c - a, p - a
    d00, d01, d11 = v0 @ v0, v0 @ v1, v1 @ v1
    d20, d21 = v2 @ v0, v2 @ v1
    den = d00 * d11 - d01 * d01
    if abs(den) < 1e-18:
        return np.array([1.0, 0.0, 0.0])
    l1 = (d11 * d20 - d01 * d21) / den
    l2 = (d00 * d21 - d01 * d20) / den
    w = np.clip(np.array([1.0 - l1 - l2, l1, l2]), 0.0, 1.0)
    return w / max(w.sum(), 1e-9)


def free_weights(ob):
    fw = np.zeros(len(ob.data.vertices))
    fg = ob.vertex_groups.get(FREE_GROUP)
    if fg is not None:
        for v in ob.data.vertices:
            for g in v.groups:
                if g.group == fg.index:
                    fw[v.index] = g.weight
    return fw


def bake_render_cache(ob, clips):
    """Per-clip shape-key caches of the final garment on a render cloth object (absolute positions in its object
    space: the simulated cloth transferred from the proxy, blended into the original skinning by af_cloth_free so
    the seam stays welded). clips = [(armature action name, positions (n, N, 3))]. Every clip frame is a key block
    'c<clip>_f<frame>', every clip a shape-key action '<action>|cloth|<object>' keyed 0/1 (in-between times
    interpolate linearly). The object keeps its Armature modifier for clips without a cache; render_sprites
    switches (_export_lib.bind_cloth_cache): cache on = Armature off. Custom property af_cloth_cache maps armature
    action -> shape-key action."""
    if ob.data.shape_keys is None:
        ob.shape_key_add(name="Basis", from_mix=False)
    key = ob.data.shape_keys
    key.use_relative = True
    mapping = {}
    lin = bpy.types.Keyframe.bl_rna.properties["interpolation"].enum_items["LINEAR"].value
    for ci, (aname, arr) in enumerate(clips):
        n = len(arr)
        act, slot, bag = _key_action(key, "%s|cloth|%s" % (aname, ob.name))
        for f in range(n):
            kb = ob.shape_key_add(name="c%02d_f%03d" % (ci, f), from_mix=False)
            kb.data.foreach_set("co", np.asarray(arr[f], dtype=np.float32).ravel())
            kb.slider_min, kb.slider_max = 0.0, 1.0
            kb.value = 0.0
            fc = bag.fcurves.new('key_blocks["%s"].value' % kb.name)
            if n == 1:
                pts = [(0, 1.0)]
            elif f == 0:
                pts = [(0, 1.0), (1, 0.0)]
            elif f == n - 1:
                pts = [(f - 1, 0.0), (f, 1.0)]
            else:
                pts = [(f - 1, 0.0), (f, 1.0), (f + 1, 0.0)]
            fc.keyframe_points.add(len(pts))
            co = []
            for x, y in pts:
                co += [float(x), float(y)]
            fc.keyframe_points.foreach_set("co", co)
            fc.keyframe_points.foreach_set("interpolation", np.full(len(pts), lin, dtype=np.int32))
            fc.update()
        act.use_frame_range = True
        act.frame_start, act.frame_end = 0, max(1, n - 1)
        act["af_cloth_clip"] = aname
        mapping[aname] = act.name
    ob[CACHE_PROP] = json.dumps(mapping, ensure_ascii=False)
    assign_key_action(key, None)
    return mapping


def farthest_from_body(rig, caps, px, sim, frames):
    """Largest distance (m) of a simulated point from the posed body capsules over the sampled frames - an exploded
    cloth flies away; a long garment hanging or lying stays within ~1 m of some bone."""
    scene = bpy.context.scene
    M = np.array(px.matrix_world)
    far = 0.0
    for i, f in enumerate(frames):
        scene.frame_set(int(f))
        pc = posed_capsules(rig, caps)
        P = sim[i].astype(np.float64) @ M[:3, :3].T + M[:3, 3]
        d = np.full(len(P), np.inf)
        for c, a, b, ra, rb in pc:
            sd, _t = capsule_sd(P, a, b, ra, rb)
            d = np.minimum(d, sd)
        far = max(far, float(d.max()))
    return far


def bind_embedded(render_ob, px):
    """Embedded-deformation binding of the render cloth onto its proxy (bind pose): every render vertex is carried by
    the three corners of its nearest proxy triangle (barycentric weights - neighbours along the surface, never a
    point of another garment layer that merely lies close) with its offset from each; at a frame every proxy point
    carries a rotation fitted to its one-ring (Kabsch) and the render vertex is the weighted sum of (corner +
    rotation x offset). Exact in the rest pose and insensitive to crumpled proxy triangles (no normals involved)."""
    from mathutils.bvhtree import BVHTree
    V = _object_rest(px)
    R = _object_rest(render_ob)
    T = mesh_tris(px.data)
    E = mesh_edges(px.data)
    nb = adjacency(len(V), E)
    D = max(1, max(len(x) for x in nb))
    ring = np.array([x + [i] * (D - len(x)) for i, x in enumerate(nb)], dtype=np.int64)
    bvh = BVHTree.FromPolygons([tuple(v) for v in V.tolist()], [tuple(t) for t in T.tolist()], all_triangles=True)
    idx = np.zeros((len(R), 3), dtype=np.int64)
    w = np.zeros((len(R), 3))
    for i, p in enumerate(R.tolist()):
        loc, _n, f, _d = bvh.find_nearest(Vector(p))
        if f is None:
            f, loc = 0, Vector(V[T[0, 0]].tolist())
        idx[i] = T[f]
        w[i] = _barycentric(np.array(loc), *V[T[f]])
    off = R[:, None, :] - V[idx]
    return {"idx": idx, "w": w, "off": off, "ring": ring, "rest": V}


def proxy_rotations(bind, Vt):
    """Per proxy point the rotation of its one-ring from the bind pose to Vt (Kabsch, reflection-safe)."""
    V0 = bind["rest"]
    ring = bind["ring"]
    a = V0[ring] - V0[:, None, :]                 # (N, D, 3)
    b = Vt[ring] - Vt[:, None, :]
    Hm = np.einsum("ndi,ndj->nij", a, b)
    U, S, Wt = np.linalg.svd(Hm)
    Rm = np.einsum("nji,nkj->nik", Wt, U)         # R = W U^T  (maps a -> b)
    det = np.linalg.det(Rm)
    flip = det < 0
    if flip.any():
        Wt2 = Wt.copy()
        Wt2[flip, 2, :] *= -1.0
        Rm[flip] = np.einsum("nji,nkj->nik", Wt2[flip], U[flip])
    return Rm


def transfer_embedded(bind, Vt):
    Vt = Vt.astype(np.float64)
    Rm = proxy_rotations(bind, Vt)
    idx, w, off = bind["idx"], bind["w"], bind["off"]
    rot_off = np.einsum("nkij,nkj->nki", Rm[idx], off)
    return (w[:, :, None] * (Vt[idx] + rot_off)).sum(1)


def blend_merge_weights(ob, target, sources):
    """Leg weights of a garment moved onto the pelvis in proportion to its freedom (1 - af_cloth_pin): the seam keeps
    the body's weights exactly, the free part hangs from the pelvis like its simulation proxy."""
    pg = ob.vertex_groups.get(PIN_GROUP)
    if pg is None:
        return 0
    src = {ob.vertex_groups[s].index for s in sources if s in ob.vertex_groups}
    tg = ob.vertex_groups.get(target) or ob.vertex_groups.new(name=target)
    changed = 0
    for v in ob.data.vertices:
        pin = 1.0
        cur = 0.0
        for g in v.groups:
            if g.group == pg.index:
                pin = g.weight
            elif g.group == tg.index:
                cur = g.weight
        if pin >= 0.999:
            continue
        moved = 0.0
        for g in v.groups:
            if g.group in src and g.weight > 0:
                moved += g.weight * (1.0 - pin)
                g.weight = g.weight * pin
        if moved > 0:
            tg.add([v.index], min(1.0, cur + moved), "REPLACE")
            changed += 1
    return changed


def set_pins(px, pin):
    """Write the pin group (af_cloth_pin) of a proxy (read by the cloth modifier when its cache starts)."""
    pg = px.vertex_groups.get(PIN_GROUP) or px.vertex_groups.new(name=PIN_GROUP)
    for i, w in enumerate(np.asarray(pin, dtype=np.float64).tolist()):
        pg.add([i], float(w), "REPLACE")


def skin_matrices(rig, names):
    """Current skinning matrices (armature space, 3x4) of the named bones: pose matrix x inverse rest matrix."""
    out = np.zeros((len(names), 3, 4))
    for i, nm in enumerate(names):
        pb = rig.pose.bones[nm]
        out[i] = np.array(pb.matrix @ pb.bone.matrix_local.inverted())[:3, :4]
    return out


def fit_weights(rig, ob, vidx, samples, max_influences=4, n_cand=5, reg=0.02):
    """Linear-blend-skinning weights of the vertices `vidx` of `ob` fitted to target positions (least squares with
    a pull toward the current weights, sum 1, non-negative): the cloth-bone GLB then reproduces the simulated
    garment as closely as 4 bone influences allow. Candidates per vertex: its n_cand strongest current groups that
    are bones. samples = [(set_frame callable, targets (len(vidx), 3) in armature space)]. Returns the mean / 90th
    percentile error before and after over the samples (m)."""
    bones = set(rig.data.bones.keys())
    gname = {vg.index: vg.name for vg in ob.vertex_groups}
    me = ob.data
    V = len(vidx)
    cand_names = []
    prior = np.zeros((V, n_cand))
    for k, v_i in enumerate(vidx.tolist()):
        gs = sorted(((g.weight, gname[g.group]) for g in me.vertices[v_i].groups
                     if gname.get(g.group) in bones and g.weight > 1e-5), reverse=True)[:n_cand]
        names = [n for _w, n in gs]
        while len(names) < n_cand:
            names.append(names[0] if names else rig.data.bones[0].name)
        cand_names.append(names)
        ws = [w for w, _n in gs] + [0.0] * (n_cand - len(gs))
        prior[k] = np.array(ws) / max(sum(ws), 1e-9)
    union = sorted({n for row in cand_names for n in row})
    uidx = {n: i for i, n in enumerate(union)}
    cand = np.array([[uidx[n] for n in row] for row in cand_names], dtype=np.int64)
    Minv = np.array(rig.matrix_world.inverted() @ ob.matrix_world)
    co = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", co)
    p0 = co.reshape(-1, 3)[vidx] @ Minv[:3, :3].T + Minv[:3, 3]
    p0h = np.concatenate([p0, np.ones((V, 1))], axis=1)
    G = np.zeros((V, n_cand, n_cand))
    h = np.zeros((V, n_cand))
    Qs = []
    for setf, tgt in samples:
        setf()
        M = skin_matrices(rig, union)
        Q = np.einsum("vcij,vj->vci", M[cand], p0h)          # (V, C, 3): the vertex carried by each candidate
        G += np.einsum("vci,vdi->vcd", Q, Q)
        h += np.einsum("vci,vi->vc", Q, tgt)
        Qs.append((Q, tgt))
    lam = reg * np.trace(G, axis1=1, axis2=2)[:, None, None] / n_cand + 1e-9
    K = np.zeros((V, n_cand + 1, n_cand + 1))
    K[:, :n_cand, :n_cand] = 2 * (G + lam * np.eye(n_cand)[None])
    K[:, :n_cand, n_cand] = 1.0
    K[:, n_cand, :n_cand] = 1.0
    rhs = np.zeros((V, n_cand + 1))
    rhs[:, :n_cand] = 2 * (h + lam[:, :, 0] * prior)
    rhs[:, n_cand] = 1.0
    try:
        sol = np.linalg.solve(K, rhs[..., None])[..., 0][:, :n_cand]
    except np.linalg.LinAlgError:
        sol = prior.copy()
    # merge duplicate candidates, clip negatives, keep the strongest influences, renormalise
    W = np.zeros((V, len(union)))
    np.add.at(W, (np.repeat(np.arange(V), n_cand), cand.ravel()), np.clip(sol, 0.0, None).ravel())
    order = np.argsort(-W, axis=1)
    keep = np.zeros_like(W, dtype=bool)
    np.put_along_axis(keep, order[:, :max_influences], True, axis=1)
    W = np.where(keep, W, 0.0)
    s = W.sum(1, keepdims=True)
    bad = s[:, 0] < 1e-6
    W = W / np.maximum(s, 1e-9)
    Wp = np.zeros((V, len(union)))
    np.add.at(Wp, (np.repeat(np.arange(V), n_cand), cand.ravel()), prior.ravel())
    W[bad] = Wp[bad]
    # errors before / after
    err0, err1 = [], []
    for setf, tgt in samples:
        setf()
        M = skin_matrices(rig, union)
        Pall = np.einsum("bij,vj->vbi", M, p0h)                 # (V, B, 3)
        err0.append(np.linalg.norm((Wp[:, :, None] * Pall).sum(1) - tgt, axis=1))
        err1.append(np.linalg.norm((W[:, :, None] * Pall).sum(1) - tgt, axis=1))
    e0, e1 = np.concatenate(err0), np.concatenate(err1)
    # write
    for nm in union:
        if ob.vertex_groups.get(nm) is None:
            ob.vertex_groups.new(name=nm)
    for k, v_i in enumerate(vidx.tolist()):
        for g in list(me.vertices[v_i].groups):
            if gname.get(g.group) in bones:
                g.weight = 0.0
    for j, nm in enumerate(union):
        vg = ob.vertex_groups[nm]
        col = W[:, j]
        for k in np.nonzero(col > 1e-5)[0].tolist():
            vg.add([int(vidx[k])], float(col[k]), "REPLACE")
    return {"vertices": int(V), "samples": len(samples), "err_before_mean_cm": round(100 * float(e0.mean()), 2),
            "err_before_p90_cm": round(100 * float(np.percentile(e0, 90)), 2),
            "err_after_mean_cm": round(100 * float(e1.mean()), 2),
            "err_after_p90_cm": round(100 * float(np.percentile(e1, 90)), 2)}


SETTLE_PROP = "af_cloth_settle"


def _prop_curve(action, path, frames_values):
    bag = A.channelbag(action)
    for fc in list(bag.fcurves):
        if fc.data_path == path:
            bag.fcurves.remove(fc)
    fc = bag.fcurves.new(path, index=0)
    fc.keyframe_points.add(len(frames_values))
    co = []
    for f, v in frames_values:
        co += [float(f), float(v)]
    fc.keyframe_points.foreach_set("co", co)
    lin = bpy.types.Keyframe.bl_rna.properties["interpolation"].enum_items["LINEAR"].value
    fc.keyframe_points.foreach_set("interpolation", np.full(len(frames_values), lin, dtype=np.int32))
    fc.update()


def death_settle_morph(rig, ob, vidx, target, hips, act, ramp, all_actions, name=SETTLE_PROP):
    """Corrective morph of the GLB body for the end of a death: bones cannot flatten a robe cone onto the floor (the
    chains swing it like a fan). On the current frame (the last of the death) every garment vertex is moved onto the
    simulated garment by a rest-space offset through the inverse of its blended skinning rotation (exact for that
    frame). The shape key is driven by a custom property of the Hips that the death ramps 0 -> 1 over `ramp`
    (every other clip keys 0); the glTF exporter samples the driven key into morph-weight animation.
    target: (len(vidx), 3) armature space. Returns {vertices, ramp, residual the morph removes (cm)}."""
    bones = list(rig.data.bones.keys())
    gname = {vg.index: vg.name for vg in ob.vertex_groups}
    M = skin_matrices(rig, bones)
    bidx = {n: i for i, n in enumerate(bones)}
    me = ob.data
    dg = bpy.context.evaluated_depsgraph_get()
    Minv = np.array(rig.matrix_world.inverted() @ ob.matrix_world)
    cur = read_points(ob, dg).astype(np.float64)[vidx] @ Minv[:3, :3].T + Minv[:3, 3]
    resid = target - cur
    delta = np.zeros((len(vidx), 3))
    for k, v_i in enumerate(vidx.tolist()):
        R = np.zeros((3, 3))
        for g in me.vertices[v_i].groups:
            nm = gname.get(g.group)
            if nm in bidx and g.weight > 0:
                R += g.weight * M[bidx[nm]][:, :3]
        try:
            delta[k] = np.linalg.solve(R, resid[k])
        except np.linalg.LinAlgError:
            delta[k] = resid[k]
    # rest offsets are in the object space of the mesh
    Mo = np.array(ob.matrix_world.inverted() @ rig.matrix_world)[:3, :3]
    delta = delta @ Mo.T
    if me.shape_keys is None:
        ob.shape_key_add(name="Basis", from_mix=False)
    base = np.empty(len(me.vertices) * 3)
    me.shape_keys.reference_key.data.foreach_get("co", base)
    base = base.reshape(-1, 3)
    kb = me.shape_keys.key_blocks.get(name) or ob.shape_key_add(name=name, from_mix=False)
    new = base.copy()
    new[vidx] += delta
    kb.data.foreach_set("co", new.ravel())
    kb.slider_min, kb.slider_max = 0.0, 1.0
    kb.value = 0.0
    pb = rig.pose.bones[hips]
    pb[name] = 0.0                          # the property exists before the driver reads it
    fc = kb.driver_add("value")
    drv = fc.driver
    drv.type = "AVERAGE"
    for v in list(drv.variables):
        drv.variables.remove(v)
    var = drv.variables.new()
    var.name = "settle"
    var.type = "SINGLE_PROP"
    var.targets[0].id_type = "OBJECT"
    var.targets[0].id = rig
    var.targets[0].data_path = 'pose.bones["%s"]["%s"]' % (hips, name)
    drv.is_valid = True                     # the glTF exporter skips drivers flagged invalid by an earlier evaluation
    path = 'pose.bones["%s"]["%s"]' % (hips, name)
    f0, f1, last = ramp
    _prop_curve(act, path, [(0, 0.0), (f0, 0.0), (f1, 1.0), (last, 1.0)])
    for a in all_actions:
        if a is not act:
            _prop_curve(a, path, [(0, 0.0)])
    # (the driver is evaluated - and baked by the glTF exporter - once the file is re-read: cloth_sim exports the
    # GLB from the saved scene; measured there: the residual of the last death frame is 0)
    return {"vertices": int(len(vidx)), "ramp_frames": [int(f0), int(f1)],
            "residual_before_p90_cm": round(100 * float(np.percentile(np.linalg.norm(resid, axis=1), 90)), 1)}
