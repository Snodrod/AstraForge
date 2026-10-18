"""Creature module library (AstraForge Forge, module "creatures").

Automatic skeletons for non-human creatures fitted from geometry, robust skin weights and
procedural, IK-planted animation. Used by steps/rig_creature.py and steps/anim_creature.py.

Frame (contract §4): metres, Z up, the creature faces -Y, its LEFT side is +X, ground z = 0.
"Forward" coordinate in this file is f = -y.

Ideas ported (rewritten, generalised) from earlier in-house creature rig scripts: IK bake, hoof slide
QA, chain weights, island-aware smoothing for fragmented meshes, bone-heat probe, wing fitting, region
masks with soft borders, 4 influences.
"""
import heapq
import json
import math

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Quaternion, Vector

import af_common as af

UP = Vector((0.0, 0.0, 1.0))
FWD = Vector((0.0, -1.0, 0.0))
LEFT = Vector((1.0, 0.0, 0.0))

TEMPLATES = ("quadruped", "winged_quadruped", "bird", "hexapod", "octopod", "serpent", "custom")
LEG_POSITIONS = {
    "quadruped": ["front", "back"],
    "winged_quadruped": ["front", "back"],
    "bird": ["back"],
    "hexapod": ["front", "mid", "back"],
    "octopod": ["front", "mid", "mid2", "back"],
    "serpent": [],
    "custom": [],
}
LEG_KIND = {"quadruped": "mammal", "winged_quadruped": "mammal", "bird": "bird",
            "hexapod": "insect", "octopod": "insect"}
WINGED = ("winged_quadruped", "bird")


# =========================================================================== small math

def smoothstep(e0, e1, x):
    e0 = np.asarray(e0, float)
    e1 = np.asarray(e1, float)
    den = np.where(np.abs(e1 - e0) > 1e-12, e1 - e0, 1e-9)
    t = np.clip((np.asarray(x, float) - e0) / den, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def v3(p):
    return Vector((float(p[0]), float(p[1]), float(p[2])))


def r3(p, nd=4):
    return [round(float(x), nd) for x in p]


def seg_dist(P, a, b):
    a = np.asarray(a, float)
    ab = np.asarray(b, float) - a
    t = np.clip(((P - a) @ ab) / max(float(ab @ ab), 1e-12), 0.0, 1.0)
    return np.linalg.norm(P - (a + t[:, None] * ab), axis=1), t


def polyline_len(pts):
    pts = np.asarray(pts, float)
    if len(pts) < 2:
        return 0.0
    return float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum())


def resample(pts, fracs):
    """Points at arc-length fractions along a polyline."""
    pts = np.asarray(pts, float)
    if len(pts) == 1:
        return np.repeat(pts, len(fracs), axis=0)
    seg = np.linalg.norm(np.diff(pts, axis=0), axis=1)
    S = np.concatenate([[0.0], np.cumsum(seg)])
    out = []
    for fr in fracs:
        s = float(np.clip(fr, 0.0, 1.0)) * S[-1]
        i = int(np.clip(np.searchsorted(S, s) - 1, 0, len(seg) - 1))
        t = (s - S[i]) / max(seg[i], 1e-12)
        out.append(pts[i] + (pts[i + 1] - pts[i]) * t)
    return np.array(out)


def smooth_path(pts, it=2):
    pts = np.asarray(pts, float).copy()
    if len(pts) < 3:
        return pts
    for _ in range(it):
        q = pts.copy()
        q[1:-1] = 0.25 * pts[:-2] + 0.5 * pts[1:-1] + 0.25 * pts[2:]
        pts = q
    return pts


def at_height(poly, zq):
    """First point along a polyline (walked from its start) whose z reaches zq (interpolated)."""
    poly = np.asarray(poly, float)
    for i in range(len(poly) - 1):
        a, b = poly[i], poly[i + 1]
        if (a[2] - zq) * (b[2] - zq) <= 0 and abs(b[2] - a[2]) > 1e-9:
            t = (zq - a[2]) / (b[2] - a[2])
            return a + (b - a) * t
    return poly[int(np.argmin(np.abs(poly[:, 2] - zq)))].copy()


# =========================================================================== mesh helpers

def prepare_mesh(objs, name="creature_mesh"):
    """Join every mesh into one object with applied transforms, weld seams.

    Accepts glTF hierarchies (parent empties with scale/quaternion rotation), rigged GLB/FBX and this
    module's own rigged.glb: armature modifiers, vertex groups, shape keys, armatures, empties (sockets)
    and other non-mesh objects are removed; every mesh keeps its world placement (rest shape)."""
    bpy.context.view_layer.update()
    meshes = af.meshes(objs)
    if not meshes:
        raise ValueError("input has no mesh")
    for o in meshes:
        for m in list(o.modifiers):
            if m.type == "ARMATURE":
                o.modifiers.remove(m)
        mw = o.matrix_world.copy()
        o.parent = None
        o.matrix_world = mw
        if o.data.shape_keys:
            o.shape_key_clear()
        if o.data.users > 1:              # instanced glTF meshes: transform_apply refuses multi-user data
            o.data = o.data.copy()
    for o in list(objs):
        if o.type != "MESH" and o.name in bpy.data.objects:
            bpy.data.objects.remove(o, do_unlink=True)
    # removing objects leaves stale (None) entries in view_layer.objects until the next update,
    # which made af.join -> select_only crash on any input with an empty or an armature
    bpy.context.view_layer.update()
    ob = af.join(meshes, name=name)
    af.apply_transforms([ob])
    ob.rotation_mode = "XYZ"          # glTF import leaves QUATERNION mode: rotation_euler would be ignored
    ob.vertex_groups.clear()
    size = max(ob.dimensions) or 1.0
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    n0 = len(bm.verts)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5 * size)
    bm.to_mesh(ob.data)
    bm.free()
    ob.data.update()
    return ob, n0 - len(ob.data.vertices)


def mesh_np(ob, world=True):
    me = ob.data
    V = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", V)
    V = V.reshape(-1, 3)
    if world:
        M = np.array(ob.matrix_world)
        V = V @ M[:3, :3].T + M[:3, 3]
    me.calc_loop_triangles()
    T = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("vertices", T)
    E = np.empty(len(me.edges) * 2, dtype=np.int64)
    me.edges.foreach_get("vertices", E)
    return V, T.reshape(-1, 3), E.reshape(-1, 2)


def islands(n, E):
    """Connected-component label per vertex (vectorised min-label propagation)."""
    lab = np.arange(n)
    if len(E) == 0:
        return lab
    while True:
        m = np.minimum(lab[E[:, 0]], lab[E[:, 1]])
        new = lab.copy()
        np.minimum.at(new, E[:, 0], m)
        np.minimum.at(new, E[:, 1], m)
        new = new[new]
        if np.array_equal(new, lab):
            return lab
        lab = new


def sample_surface(V, T, spacing):
    """Points on every triangle with spacing <= `spacing` (plus all vertices)."""
    A, B, C = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
    emax = np.max(np.stack([np.linalg.norm(B - A, axis=1), np.linalg.norm(C - B, axis=1),
                            np.linalg.norm(A - C, axis=1)], 1), 1)
    nsub = np.clip(np.ceil(emax / max(spacing, 1e-9)), 1, 96).astype(int)
    out = [V]
    for k in np.unique(nsub):
        sel = np.nonzero(nsub == k)[0]
        uv = np.array([(i / k, j / k) for i in range(k + 1) for j in range(k + 1 - i)])
        u, w = uv[:, 0], uv[:, 1]
        chunk = max(1, 400000 // len(uv))
        for c0 in range(0, len(sel), chunk):
            s = sel[c0:c0 + chunk]
            P = (A[s, None, :] * (1 - u - w)[None, :, None] + B[s, None, :] * u[None, :, None]
                 + C[s, None, :] * w[None, :, None])
            out.append(P.reshape(-1, 3))
    return np.concatenate(out, 0)


# =========================================================================== voxels

def _sh(a, ax, k):
    """Shift a boolean/num array by k cells along axis ax with zero fill."""
    out = np.zeros_like(a)
    src = [slice(None)] * a.ndim
    dst = [slice(None)] * a.ndim
    if k > 0:
        src[ax], dst[ax] = slice(0, -k), slice(k, None)
    else:
        src[ax], dst[ax] = slice(-k, None), slice(0, k)
    out[tuple(dst)] = a[tuple(src)]
    return out


def box_dilate(a, r=1):
    for ax in range(a.ndim):
        for _ in range(r):
            a = a | _sh(a, ax, 1) | _sh(a, ax, -1)
    return a


def box_erode(a, r=1):
    for ax in range(a.ndim):
        for _ in range(r):
            a = a & _sh(a, ax, 1) & _sh(a, ax, -1)
    return a


def cross_dilate(a):
    b = a.copy()
    for ax in range(a.ndim):
        b |= _sh(a, ax, 1)
        b |= _sh(a, ax, -1)
    return b


def cross_erode(a):
    b = a.copy()
    for ax in range(a.ndim):
        b &= _sh(a, ax, 1)
        b &= _sh(a, ax, -1)
    return b


def distance_transform(solid):
    """Approximate inner distance in cells (alternating cross/box erosion = octagonal metric)."""
    dt = np.zeros(solid.shape, np.float32)
    cur = solid.copy()
    k = 0
    while cur.any() and k < 400:
        dt[cur] += 1.0
        cur = cross_erode(cur) if k % 2 == 0 else box_erode(cur)
        k += 1
    return dt


def exterior(closed):
    free = ~closed
    ext = np.zeros_like(free)
    for ax in range(3):
        ext |= ~np.logical_or.accumulate(closed, axis=ax)
        ext |= ~np.flip(np.logical_or.accumulate(np.flip(closed, ax), axis=ax), ax)
    ext &= free
    for _ in range(2000):
        new = cross_dilate(ext) & free
        if np.count_nonzero(new) == np.count_nonzero(ext):
            break
        ext = new
    return ext


def label3(mask):
    """Connected components (26-neighbourhood) of a boolean grid; returns (labels, count)."""
    shape = mask.shape
    flat = mask.ravel()
    idx = np.flatnonzero(flat)
    lab = np.full(flat.size, -1, np.int64)
    if len(idx) == 0:
        return lab.reshape(shape), 0
    pos = np.full(flat.size, -1, np.int64)
    pos[idx] = np.arange(len(idx))
    strides = np.array([shape[1] * shape[2], shape[2], 1])
    pairs = []
    ijk = np.stack(np.unravel_index(idx, shape), 1)
    for d in [(1, 0, 0), (0, 1, 0), (0, 0, 1), (1, 1, 0), (1, -1, 0), (1, 0, 1), (1, 0, -1), (0, 1, 1),
              (0, 1, -1), (1, 1, 1), (1, 1, -1), (1, -1, 1), (1, -1, -1)]:
        q = ijk + np.array(d)
        ok = np.all((q >= 0) & (q < np.array(shape)), 1)
        j = (q[ok] * strides).sum(1)
        good = flat[j]
        pairs.append(np.stack([pos[idx[ok][good]], pos[j[good]]], 1))
    P = np.concatenate(pairs, 0) if pairs else np.zeros((0, 2), np.int64)
    comp = islands(len(idx), P)
    _, inv = np.unique(comp, return_inverse=True)
    lab[idx] = inv
    return lab.reshape(shape), int(inv.max()) + 1


class Grid:
    """Solid voxelisation of a (possibly open, fragmented) triangle soup + inner distance."""

    def __init__(self, V, T, cell, pad=3):
        self.cell = float(cell)
        self.lo = V.min(0) - pad * cell
        self.shape = tuple(int(x) for x in np.ceil((V.max(0) - self.lo) / cell).astype(int) + pad + 1)
        P = sample_surface(V, T, cell * 0.5)
        ijk = np.clip(np.floor((P - self.lo) / cell).astype(int), 0, np.array(self.shape) - 1)
        shell = np.zeros(self.shape, bool)
        shell[ijk[:, 0], ijk[:, 1], ijk[:, 2]] = True
        closed = box_dilate(shell, 1)
        ext = exterior(closed)
        self.solid = box_erode(~ext, 1) | shell
        self.shell = shell
        self.dt = distance_transform(self.solid)
        sx, sy, sz = self.shape
        self.strides = np.array([sy * sz, sz, 1])
        offs, lens = [], []
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    if dx or dy or dz:
                        offs.append(dx * sy * sz + dy * sz + dz)
                        lens.append(math.sqrt(dx * dx + dy * dy + dz * dz))
        self.offs, self.lens = offs, lens

    def centers(self, flat_ids):
        ijk = np.stack(np.unravel_index(np.asarray(flat_ids), self.shape), -1)
        return self.lo + (ijk + 0.5) * self.cell

    def ijk(self, P):
        return np.clip(np.floor((np.asarray(P, float) - self.lo) / self.cell).astype(int), 0,
                       np.array(self.shape) - 1)

    def flat(self, P):
        q = self.ijk(P)
        return (q * self.strides).sum(-1)

    def dt_at(self, P, reach=1):
        """Inner distance (metres) near points P (max over a small neighbourhood)."""
        q = self.ijk(np.atleast_2d(P))
        out = []
        for a in q:
            sl = tuple(slice(max(0, c - reach), c + reach + 1) for c in a)
            out.append(float(self.dt[sl].max()) * self.cell)
        return np.array(out)

    def nearest_solid(self, P, mask=None, radius=6):
        """Flat id of the solid voxel nearest to P (searching a small box)."""
        m = self.solid if mask is None else mask
        a = self.ijk(P)
        for r in range(1, radius + 1):
            sl = tuple(slice(max(0, c - r), c + r + 1) for c in a)
            sub = np.argwhere(m[sl])
            if len(sub):
                base = np.array([s.start for s in sl])
                cand = sub + base
                c = self.lo + (cand + 0.5) * self.cell
                k = int(np.argmin(np.linalg.norm(c - np.asarray(P), axis=1)))
                return int((cand[k] * self.strides).sum())
        ids = np.flatnonzero(m.ravel())
        c = self.centers(ids)
        return int(ids[int(np.argmin(np.linalg.norm(c - np.asarray(P), axis=1)))])

    def dijkstra(self, sources, mask=None, medial=4.0):
        """Shortest paths inside `mask` with a cost that prefers the medial axis (thick voxels)."""
        m = (self.solid if mask is None else mask).ravel().tolist()
        dtf = self.dt.ravel()
        W = (1.0 + medial / np.maximum(dtf, 1.0) ** 2).tolist()
        n = len(m)
        dist = [math.inf] * n
        par = [-1] * n
        heap = []
        for s in sources:
            s = int(s)
            dist[s] = 0.0
            heap.append((0.0, s))
        heapq.heapify(heap)
        offs, lens = self.offs, self.lens
        pop, push = heapq.heappop, heapq.heappush
        while heap:
            d, i = pop(heap)
            if d > dist[i]:
                continue
            wi = W[i]
            for o, ln in zip(offs, lens):
                j = i + o
                if not m[j]:
                    continue
                nd = d + ln * 0.5 * (wi + W[j])
                if nd < dist[j]:
                    dist[j] = nd
                    par[j] = i
                    push(heap, (nd, j))
        return np.array(dist), np.array(par, np.int64)

    @staticmethod
    def trace(par, start, limit=100000):
        out = [int(start)]
        i = int(start)
        while par[i] >= 0 and len(out) < limit:
            i = int(par[i])
            out.append(i)
        return out


# =========================================================================== ground contacts

def _label2(occ):
    """8-connected components of a 2D boolean grid (BFS)."""
    lab = -np.ones(occ.shape, int)
    n = 0
    H, W = occ.shape
    for i, j in np.argwhere(occ):
        if lab[i, j] >= 0:
            continue
        stack = [(i, j)]
        lab[i, j] = n
        while stack:
            a, b = stack.pop()
            for da in (-1, 0, 1):
                for db in (-1, 0, 1):
                    x, y = a + da, b + db
                    if 0 <= x < H and 0 <= y < W and occ[x, y] and lab[x, y] < 0:
                        lab[x, y] = n
                        stack.append((x, y))
        n += 1
    return lab, n


def ground_clusters(V, cell, body_len, zband=None):
    """Clusters of near-ground vertices (feet, toes, tail tips, serpent bellies)."""
    z0 = float(V[:, 2].min())
    zb = zband if zband is not None else max(1.5 * cell, 0.02 * (V[:, 2].max() - z0))
    low = np.nonzero(V[:, 2] < z0 + zb)[0]
    if len(low) == 0:
        return []
    P = V[low, :2]
    c2 = max(cell, 0.008 * body_len)
    base = P.min(0) - c2
    ij = np.floor((P - base) / c2).astype(int)
    occ = np.zeros(ij.max(0) + 2, bool)
    occ[ij[:, 0], ij[:, 1]] = True
    lab, n = _label2(occ)
    vl = lab[ij[:, 0], ij[:, 1]]
    cl = []
    for k in range(n):
        ids = low[vl == k]
        if len(ids) < 3:
            continue
        Q = V[ids]
        cl.append({"ids": ids, "xy": Q[:, :2].mean(0), "lo": Q.min(0), "hi": Q.max(0)})
    # merge toes of one foot: same side, gap below a fraction of the body length
    dm = 0.045 * body_len
    merged = True
    while merged:
        merged = False
        for a in range(len(cl)):
            for b in range(a + 1, len(cl)):
                A, B = cl[a], cl[b]
                gap = np.maximum(0, np.maximum(A["lo"][:2] - B["hi"][:2], B["lo"][:2] - A["hi"][:2]))
                same_side = (A["xy"][0] * B["xy"][0] > 0) or min(abs(A["xy"][0]), abs(B["xy"][0])) < 0.01 * body_len
                if np.linalg.norm(gap) < dm and same_side:
                    ids = np.concatenate([A["ids"], B["ids"]])
                    Q = V[ids]
                    cl[a] = {"ids": ids, "xy": Q[:, :2].mean(0), "lo": Q.min(0), "hi": Q.max(0)}
                    del cl[b]
                    merged = True
                    break
            if merged:
                break
    for c in cl:
        c["n"] = len(c["ids"])
        c["ext"] = c["hi"] - c["lo"]
        # straddles the midline -> belly / tail / chin contact, not a foot
        c["midline"] = bool(c["lo"][0] < 0 < c["hi"][0] and abs(c["xy"][0]) < 0.3 * max(c["ext"][0], 1e-6))
    return cl


def low_slab_labels(V, H, g):
    """Connected components of the solid voxels below z0 + max(6 % H, 2.5 cells) (feet, toes, pads)."""
    z0 = float(V[:, 2].min())
    zb = z0 + max(0.06 * H, 2.5 * g.cell)
    kz = int(np.ceil((zb - g.lo[2]) / g.cell)) + 1
    slab = np.zeros_like(g.solid)
    slab[:, :, :kz] = g.solid[:, :, :kz]
    lab, _ = label3(slab)
    return lab.ravel(), zb


def merge_foot_contacts(cl, V, H, span, g):
    """One foot often touches the ground as several clusters: a bird's forward toes and hallux, split
    hooves, claws. ground_clusters() only merges them when the gap is tiny (0.045 x span), which works
    for spread-wing birds (wide span) but not for folded-wing ones. Here clusters are merged when they
    share a component of the low solid slab (they join through the foot/metatarsal joint above the
    ground), sit on the same side, are close along the body and the merged footprint is foot-sized.
    Returns (clusters, merges)."""
    if len(cl) < 2:
        return cl, 0
    flat_lab, _ = low_slab_labels(V, H, g)
    comps = []
    for c in cl:
        s = set(flat_lab[g.flat(V[c["ids"]])].tolist()) - {-1}
        comps.append(s)
    merges = 0
    changed = True
    while changed:
        changed = False
        for a in range(len(cl)):
            for b in range(a + 1, len(cl)):
                A_, B_ = cl[a], cl[b]
                if A_["midline"] or B_["midline"] or A_["xy"][0] * B_["xy"][0] <= 0:
                    continue
                if not (comps[a] & comps[b]):
                    continue
                if abs(A_["xy"][1] - B_["xy"][1]) > 0.3 * span:
                    continue
                ids = np.concatenate([A_["ids"], B_["ids"]])
                Q = V[ids]
                ext = Q.max(0) - Q.min(0)
                if ext[1] > 0.3 * span or ext[0] > 0.2 * span:
                    continue
                c = {"ids": ids, "xy": Q[:, :2].mean(0), "lo": Q.min(0), "hi": Q.max(0), "n": len(ids),
                     "ext": ext, "midline": False}
                cl[a] = c
                comps[a] = comps[a] | comps[b]
                del cl[b]
                del comps[b]
                merges += 1
                changed = True
                break
            if changed:
                break
    return cl, merges


def pair_feet(cl, body_len):
    """Pair left (+X) and right (-X) contacts with similar forward position."""
    left = [c for c in cl if not c["midline"] and c["xy"][0] > 0]
    right = [c for c in cl if not c["midline"] and c["xy"][0] < 0]
    pairs, used = [], set()
    cand = []
    for i, a in enumerate(left):
        for j, b in enumerate(right):
            cost = abs(a["xy"][1] - b["xy"][1]) + 0.5 * abs(a["xy"][0] + b["xy"][0])
            cand.append((cost, i, j))
    cand.sort()
    li, ri = set(), set()
    for cost, i, j in cand:
        if i in li or j in ri or cost > 0.22 * body_len:
            continue
        li.add(i)
        ri.add(j)
        pairs.append((left[i], right[j]))
    unp = [c for i, c in enumerate(left) if i not in li] + [c for j, c in enumerate(right) if j not in ri]
    # sort pairs front (-Y) to back (+Y)
    pairs.sort(key=lambda p: 0.5 * (p[0]["xy"][1] + p[1]["xy"][1]))
    used = {id(c) for p in pairs for c in p}
    rest = [c for c in cl if id(c) not in used]
    return pairs, unp, rest


def grow_footprints(V, pairs, H, span, cell, g=None):
    """Grow every paired contact (often only claw/hoof tips) into the foot's footprint.

    The footprint is the connected low part of the solid voxels around the contact (toes that
    rest behind the talons, heel pads), limited to the same side and a generous radius."""
    feet = [c for p in pairs for c in p]
    if not feet:
        return
    z0 = float(V[:, 2].min())
    zb = z0 + max(0.06 * H, 2.5 * cell)
    cand = np.nonzero(V[:, 2] < zb)[0]
    C = np.array([c["xy"] for c in feet])
    d = np.linalg.norm(V[cand, None, :2] - C[None], axis=-1)
    near = np.argmin(d, 1)
    vlab = None
    if g is not None:
        kz = int(np.ceil((zb - g.lo[2]) / g.cell)) + 1
        slab = np.zeros_like(g.solid)
        slab[:, :, :kz] = g.solid[:, :, :kz]
        lab, _ = label3(slab)
        vlab = lab.ravel()[g.flat(V[cand])]
    for k, c in enumerate(feet):
        same = np.sign(V[cand, 0]) == np.sign(c["xy"][0])
        sel = cand[(near == k) & (d[np.arange(len(cand)), near] < 0.09 * span) & same]
        if vlab is not None:
            seeds = set(vlab[np.isin(cand, c["ids"])].tolist()) - {-1}
            if seeds:
                conn = np.isin(vlab, list(seeds)) & same & (d[:, k] < 0.25 * span)
                sel = np.union1d(sel, cand[conn])
        if len(sel) < len(c["ids"]):
            sel = c["ids"]
        Q = V[sel]
        c["fp_ids"] = sel
        c["fp_xy"] = Q[:, :2].mean(0)
        c["fp_lo"], c["fp_hi"] = Q.min(0), Q.max(0)


# =========================================================================== facing

FACING_NAMES = {"-Y": (0, -1), "+Y": (0, 1), "-X": (-1, 0), "+X": (1, 0),
                "Y": (0, 1), "X": (1, 0)}


def _facing_param(value, key):
    """None (= auto) or a unit 2D head direction from a facing/head_dir parameter.

    Labels: 'auto' / '' / None -> auto; '-Y' '+Y' '-X' '+X' (case-insensitive). Vectors: [x, y(, z)]."""
    if value is None or value is False:
        return None
    if isinstance(value, str):
        v = value.strip().upper().replace(" ", "")
        if v in ("", "AUTO"):
            return None
        if v in FACING_NAMES:
            return np.array(FACING_NAMES[v], float)
        raise ValueError("params.%s=%r: use 'auto', '-Y', '+Y', '-X', '+X' or [x, y]" % (key, value))
    if isinstance(value, (list, tuple)) and len(value) >= 2 and all(
            isinstance(x, (int, float)) and not isinstance(x, bool) for x in value[:2]):
        d = np.array(value[:2], float)
        n = float(np.linalg.norm(d))
        if n < 1e-9:
            raise ValueError("params.%s is a zero vector" % key)
        return d / n
    raise ValueError("params.%s=%r: use 'auto', '-Y', '+Y', '-X', '+X' or [x, y]" % (key, value))


def detect_facing(ob, params):
    """Return (head direction in the input as a unit 2D vector, info). params.head_dir/facing win.

    Both parameters accept the label form the server sends ('auto' = detect) or an [x, y] vector."""
    for key in ("head_dir", "facing"):
        d = _facing_param(params.get(key), key)
        if d is not None:
            return d, {"method": "params." + key, "confidence": 1.0}
    V, T, _ = mesh_np(ob)
    size = np.ptp(V, 0)
    cell = float(max(size)) / 56.0
    g = Grid(V, T, cell)
    ids = np.flatnonzero(g.solid.ravel())
    P = g.centers(ids)
    dt = g.dt.ravel()[ids]
    # body axis from THICK voxels only: thin wings/tails/legs have long lever arms and would win the PCA
    w = dt.astype(float) ** 2 * (dt >= max(2.0, 0.35 * float(dt.max())))
    if (w > 0).sum() < 20:
        w = dt.astype(float) ** 2
    c = (P[:, :2] * w[:, None]).sum(0) / w.sum()
    Q = P[:, :2] - c
    C = (Q * w[:, None]).T @ Q / w.sum()
    ev, evec = np.linalg.eigh(C)
    axis = evec[:, int(np.argmax(ev))]
    ang = math.degrees(math.atan2(axis[1], axis[0]))
    snap = round(ang / 90.0) * 90.0
    snapped = abs(ang - snap) < 20.0 and params.get("snap_facing", True)
    if snapped:
        axis = np.array([math.cos(math.radians(snap)), math.sin(math.radians(snap))])
    t = (P[:, :2] - c) @ axis
    span = max(float(np.ptp(t)), 1e-9)
    zmin = float(P[:, 2].min())
    H = float(np.ptp(P[:, 2]))
    thick = dt >= 2
    cues = {}
    upper = thick & (P[:, 2] > zmin + 0.55 * H)
    cues["upper_mass"] = float((t[upper].mean() - t[thick].mean()) / span) if upper.sum() > 8 else 0.0
    # height of the thick mass at each end: heads are carried high, tails/rumps lower
    tmid = 0.5 * (t.max() + t.min())
    ends = {}
    for sgn in (1, -1):
        e = thick & ((sgn * (t - tmid)) > 0.25 * span)
        if e.sum() < 4:
            e = (sgn * (t - tmid)) > 0.25 * span
        ends[sgn] = float(np.percentile(P[e, 2], 95)) if e.any() else zmin
    cues["end_height"] = float((ends[1] - ends[-1]) / max(H, 1e-9))
    # feet are paired left/right in the body frame (lateral = perpendicular to the body axis)
    lat = np.array([-axis[1], axis[0]])
    Va = np.stack([(V[:, :2] - c) @ lat, (V[:, :2] - c) @ axis, V[:, 2]], 1)
    cl = ground_clusters(Va, cell, float(max(size[:2])))
    pairs, _, _ = pair_feet(cl, float(max(size[:2])))
    nfeet = 2 * len(pairs)
    if nfeet >= 6:
        ft = np.array([foot["xy"][1] for pr in pairs for foot in pr])
        cues["legs_forward"] = float((ft.mean() - t[thick].mean()) / span)
        score = 1.0 * cues["legs_forward"] + 0.2 * cues["upper_mass"]
    else:
        score = 0.5 * cues["upper_mass"] + 0.5 * cues["end_height"]
    head = axis if score >= 0 else -axis
    conf = float(min(1.0, abs(score) / 0.08))
    info = {"method": "auto", "body_axis_deg": round(ang, 1), "snapped": bool(snapped), "cues": cues,
            "score": round(score, 4), "feet_pairs": len(pairs), "confidence": round(conf, 3)}
    return head, info


def head_dir_label(d):
    d = np.asarray(d, float)
    if abs(d[0]) > abs(d[1]):
        return "+X" if d[0] > 0 else "-X"
    return "+Y" if d[1] > 0 else "-Y"


def yaw_to_minus_y(head):
    """Rotation about Z (degrees) turning the 2D head direction into -Y."""
    a = math.atan2(head[1], head[0])
    return math.degrees(-math.pi / 2 - a)


# =========================================================================== analysis

def ball_dilate(a, r):
    for k in range(int(r)):
        a = cross_dilate(a) if k % 2 == 0 else box_dilate(a, 1)
    return a


class Analysis(dict):
    """Everything measured on the mesh; plain dict so it serialises into rig.json."""


def analyse(ob, params):
    V, T, E = mesh_np(ob)
    lo, hi = V.min(0), V.max(0)
    size = hi - lo
    H, Lb, Wd = float(size[2]), float(size[1]), float(size[0])
    res = int(params.get("voxel_res", 112))
    cell = float(max(size)) / res
    g = Grid(V, T, cell)
    solid, dt = g.solid, g.dt
    A = Analysis(cell=cell, H=H, L=Lb, W=Wd, grid_shape=list(g.shape), solid_voxels=int(solid.sum()))
    A["_g"], A["_V"], A["_E"], A["_T"] = g, V, E, T
    span = max(Lb, Wd)
    # --- ground contacts
    cl = ground_clusters(V, cell, span)
    cl, A["contact_merges"] = merge_foot_contacts(cl, V, H, span, g)
    pairs, unpaired, rest = pair_feet(cl, span)
    A["contacts"] = [{"xy": r3(c["xy"]), "n": c["n"], "midline": c["midline"]} for c in cl]
    A["_pairs"], A["_unpaired"] = pairs, unpaired
    grow_footprints(V, pairs, H, span, cell, g)
    # --- torso = morphological opening (thick parts). The radius sits just above the limb thickness
    # measured right above the feet, so thinner body segments (spider cephalothorax, insect thorax,
    # necks) stay "body" while legs/wings/tails become thin parts.
    dmax = float(dt.max())
    limb = []
    for pr in pairs:
        for c in pr:
            i, j, _ = g.ijk([c["fp_xy"][0], c["fp_xy"][1], 0.0])
            k0 = int((0.06 * H - g.lo[2]) / cell)
            k1 = int((0.3 * H - g.lo[2]) / cell)
            col = dt[max(0, i - 3):i + 4, max(0, j - 3):j + 4, max(0, k0):max(k0 + 1, k1)]
            per = col.max((0, 1)) if col.size else np.zeros(0)
            per = per[per > 0]
            if len(per):
                limb.append(float(np.median(per)))
    if limb:
        lm = float(np.median(limb))
        r = int(np.clip(max(math.ceil(1.6 * lm), lm + 2), 2, max(2, round(0.6 * dmax))))
    else:
        r = max(2, int(round(0.5 * dmax)))
    A["limb_radius_cells"] = round(float(np.median(limb)), 2) if limb else None
    core = dt >= r
    lab, n = label3(core)
    if n > 1:
        # keep every substantial thick blob (thorax AND abdomen of insects, cephalothorax of spiders)
        counts = np.bincount(lab[lab >= 0].ravel())
        keep = np.nonzero(counts >= 0.12 * counts.max())[0]
        core = np.isin(lab, keep)
    torso = ball_dilate(core, r) & solid
    A["opening_radius_cells"] = r
    # a second, coarser body mask (radius = half the max thickness) tells where thick tails leave the body
    rb = max(r, int(round(0.5 * dmax)))
    if rb > r:
        cb = dt >= rb
        lb_, nb_ = label3(cb)
        if nb_ > 1:
            cnt = np.bincount(lb_[lb_ >= 0].ravel())
            cb = np.isin(lb_, np.nonzero(cnt >= 0.12 * cnt.max())[0])
        A["_torso_big"] = ball_dilate(cb, rb) & solid
    else:
        A["_torso_big"] = torso
    A["dt_max_m"] = round(dmax * cell, 4)
    A["_torso"] = torso
    tid = np.flatnonzero(torso.ravel())
    TP = g.centers(tid)
    A["torso_halfwidth"] = float(np.percentile(np.abs(TP[:, 0]), 95)) if len(TP) else 0.1 * Wd
    A["torso_z"] = [float(TP[:, 2].min()), float(TP[:, 2].max())] if len(TP) else [0.3 * H, H]
    A["torso_f"] = [float(-TP[:, 1].max()), float(-TP[:, 1].min())] if len(TP) else [-0.5 * Lb, 0.5 * Lb]
    # --- medial geodesics from the torso
    dist, par = g.dijkstra(tid)
    A["_dist"], A["_par"] = dist, par
    thin = solid & ~torso
    tl, tn = label3(thin)
    A["_thin_lab"] = tl
    # --- legs: trace every paired foot to the torso
    legs = []
    leg_vox = np.zeros(solid.size, bool)
    for pi, (cL, cR) in enumerate(pairs):
        for side, c in (("l", cL), ("r", cR)):
            Q = V[c["ids"]]
            p0 = np.array([c["xy"][0], c["xy"][1], float(Q[:, 2].min()) + 0.5 * cell])
            s = g.nearest_solid(p0)
            path = Grid.trace(par, s)
            pts = smooth_path(np.vstack([p0, g.centers(path)]), 2)
            leg_vox[path] = True
            # second trace from the middle of the footprint (mammal/bird legs rise from there)
            p1 = np.array([c["fp_xy"][0], c["fp_xy"][1], float(Q[:, 2].min()) + 0.5 * cell])
            s1 = g.nearest_solid(p1)
            path1 = Grid.trace(par, s1)
            pts1 = smooth_path(np.vstack([p1, g.centers(path1)]), 2)
            legs.append({"pair": pi, "side": side, "foot": c, "path": pts, "attach": pts[-1].copy(),
                         "path_fp": pts1, "attach_fp": pts1[-1].copy(),
                         "path_len": polyline_len(pts), "start_vox": s})
    A["_legs"] = legs
    # leg region = tubes around the medial leg paths (whole thin components would also swallow
    # tails or pelvis bones that happen to be connected to a leg, e.g. on skeletal meshes)
    legmask = np.zeros_like(solid)
    leg_comps = set()
    for lg in legs:
        for vid in Grid.trace(par, lg["start_vox"]):
            lb = tl.ravel()[vid]
            if lb >= 0:
                leg_comps.add(int(lb))
        pts = lg["path"]
        rr = g.dt_at(pts)
        for p_, r_ in zip(pts, rr):
            rc = int(math.ceil(1.8 * r_ / cell)) + 1
            c = g.ijk(p_)
            sl = tuple(slice(max(0, c[k] - rc), c[k] + rc + 1) for k in range(3))
            sub = np.stack(np.meshgrid(*[np.arange(s_.start, min(s_.stop, g.shape[k]))
                                         for k, s_ in enumerate(sl)], indexing="ij"), -1)
            ball = np.linalg.norm(sub - c, axis=-1) <= rc
            legmask[sl] |= ball
    legmask &= thin
    A["_legmask"] = legmask
    # --- wings: thin, high, wide components not touching the ground
    hw = max(A["torso_halfwidth"], 2 * cell)
    tz = 0.5 * (A["torso_z"][0] + A["torso_z"][1])
    wings = {}
    wingmask = np.zeros_like(solid)
    if tn:
        flat_tl = tl.ravel()
        ids_all = np.flatnonzero(flat_tl >= 0)
        labs = flat_tl[ids_all]
        order = np.argsort(labs, kind="stable")
        labs_s, ids_s = labs[order], ids_all[order]
        bounds = np.searchsorted(labs_s, np.arange(tn + 1))
        for k in range(tn):
            if k in leg_comps:
                continue
            ids = ids_s[bounds[k]:bounds[k + 1]]
            if len(ids) < max(12, 0.004 * A["solid_voxels"]):
                continue
            P = g.centers(ids)
            if (P[:, 2].min() < 0.1 * H or np.abs(P[:, 0]).max() < 1.35 * hw
                    or P[:, 2].mean() < tz - 0.15 * H):
                continue
            wingmask.ravel()[ids] = True
    for side, sg in (("l", 1.0), ("r", -1.0)):
        ids = np.flatnonzero(wingmask.ravel())
        if not len(ids):
            continue
        P = g.centers(ids)
        sel = ids[(sg * P[:, 0]) > 0.5 * hw]
        if len(sel) < 12:
            continue
        dsel = np.where(np.isfinite(dist[sel]), dist[sel], -1.0)
        tip = int(sel[int(np.argmax(dsel))])
        path = Grid.trace(par, tip)
        pts = smooth_path(g.centers(path), 2)
        wings[side] = {"tip": pts[0].copy(), "path": pts[::-1].copy(), "attach": pts[-1].copy(),
                       "voxels": int(len(sel)), "span": float(np.abs(g.centers(sel)[:, 0]).max())}
    A["_wings"] = wings
    A["_wingmask"] = wingmask
    # --- axial path rear -> front through non-leg, non-wing voxels
    cand = solid & ~legmask & ~wingmask
    cids = np.flatnonzero(cand.ravel())
    if len(cids) < 10:
        cand = solid.copy()
        cids = np.flatnonzero(cand.ravel())
    CP = g.centers(cids)
    near_mid = np.abs(CP[:, 0]) < 0.6 * hw + cell
    if near_mid.sum() < 5:
        near_mid[:] = True
    sub = cids[near_mid]
    SP = CP[near_mid]
    front = int(sub[int(np.argmin(SP[:, 1]))])
    rear = int(sub[int(np.argmax(SP[:, 1]))])
    d2, p2 = g.dijkstra([rear], mask=cand)
    if not np.isfinite(d2[front]):
        d2, p2 = g.dijkstra([rear])
    ax_ids = Grid.trace(p2, front)[::-1]          # rear -> front
    axis = smooth_path(g.centers(ax_ids), 3)
    A["_axis"] = axis
    A["front_tip"] = r3(g.centers([front])[0])
    A["rear_tip"] = r3(g.centers([rear])[0])
    return A


def classify(A):
    """Template from geometry: contact pairs, wings, elongation."""
    npairs = len(A["_pairs"])
    winged = len(A["_wings"]) == 2
    elong = A["L"] / max(A["H"], 1e-6)
    if npairs == 0:
        t = "serpent"
    elif npairs == 1:
        t = "bird"
    elif npairs == 2:
        t = "winged_quadruped" if winged else "quadruped"
    elif npairs == 3:
        t = "hexapod"
    else:
        t = "octopod"
    ev = {"contact_pairs": npairs, "unpaired_contacts": len(A["_unpaired"]), "wings": winged,
          "elongation_l_over_h": round(elong, 3)}
    conf = 0.9
    if A["_unpaired"]:
        conf -= 0.2
    if npairs > 4:
        conf = 0.3
    if t == "serpent" and elong < 2.5:
        conf = 0.4
    return t, ev, round(conf, 2)


# --------------------------------------------------------------------------- axis helpers

def axis_at_f(axis, fq):
    """Point on the axis polyline (rear->front) at forward coordinate fq (= -y)."""
    f = -axis[:, 1]
    for i in range(len(axis) - 1):
        a, b = f[i], f[i + 1]
        if (a - fq) * (b - fq) <= 0 and abs(b - a) > 1e-9:
            t = (fq - a) / (b - a)
            return axis[i] + (axis[i + 1] - axis[i]) * t, i + t
    i = int(np.argmin(np.abs(f - fq)))
    return axis[i].copy(), float(i)


def sub_polyline(axis, i0, i1):
    """Polyline between fractional indices i0 < i1."""
    def pt(i):
        k = int(np.clip(math.floor(i), 0, len(axis) - 2))
        return axis[k] + (axis[k + 1] - axis[k]) * (i - k)
    ks = [k for k in range(int(math.ceil(i0)), int(math.floor(i1)) + 1) if i0 < k < i1]
    return np.array([pt(i0)] + [axis[k] for k in ks] + [pt(i1)])


def lift_spine(A, axis, frac):
    """Move axis points inside the torso toward the back (spine sits above the medial line)."""
    g, torso = A["_g"], A["_torso"]
    out = axis.copy()
    for k, p in enumerate(axis):
        i, j, _ = g.ijk(p)
        col = torso[max(0, i - 1):i + 2, j, :].any(0)
        zs = np.nonzero(col)[0]
        if len(zs) < 2:
            continue
        zb = g.lo[2] + (zs.min() + 0.5) * g.cell
        zt = g.lo[2] + (zs.max() + 0.5) * g.cell
        if zb - g.cell <= p[2] <= zt + g.cell:
            out[k, 2] = zb + frac * (zt - zb)
    return smooth_path(out, 1)


def turning_point(poly, lo=0.3, hi=0.88, min_deg=22.0):
    """Arc fraction of the sharpest bend of a polyline (None if it is nearly straight)."""
    pts = resample(poly, np.linspace(0, 1, 25))
    best, arg = 0.0, None
    for k in range(2, 23):
        a = pts[k] - pts[k - 2]
        b = pts[k + 2] - pts[k]
        if np.linalg.norm(a) < 1e-9 or np.linalg.norm(b) < 1e-9:
            continue
        ang = math.degrees(math.acos(np.clip(a @ b / np.linalg.norm(a) / np.linalg.norm(b), -1, 1)))
        fr = k / 24.0
        if lo <= fr <= hi and ang > best:
            best, arg = ang, fr
    return (arg if best >= min_deg else None), best


def constriction(A, poly, lo=0.15, hi=0.85):
    """Arc fraction of the thinnest place along a polyline (None if no clear waist)."""
    pts = resample(poly, np.linspace(0, 1, 21))
    r = A["_g"].dt_at(pts)
    k0, k1 = int(lo * 20), int(hi * 20)
    seg = r[k0:k1 + 1]
    if not len(seg):
        return None
    k = int(np.argmin(seg)) + k0
    if r[k] < 0.75 * np.median(r):
        return k / 20.0
    return None


# --------------------------------------------------------------------------- leg fitting

def torso_column(A, y, x=0.0, halfwin=2):
    g, torso = A["_g"], A["_torso"]
    i, j, _ = g.ijk([x, y, 0.0])
    col = torso[:, max(0, j - halfwin):j + halfwin + 1, :]
    zs = np.nonzero(col.any((0, 1)))[0]
    if not len(zs):
        return A["torso_z"][0], A["torso_z"][1]
    return g.lo[2] + (zs.min() + 0.5) * g.cell, g.lo[2] + (zs.max() + 0.5) * g.cell


def cone_path(A, foot, z_floor):
    """Medial path from a footprint up to the body, restricted to an upward cone above the foot.

    Mammal and bird legs stand under the body; without this the medial path may detour through a
    tail, mane or cloth that touches the leg after voxel closing."""
    g = A["_g"]
    cell = g.cell
    c = np.asarray(foot["fp_xy"], float)
    sx, sy, sz = g.shape
    zc = g.lo[2] + (np.arange(sz) + 0.5) * cell
    xc = g.lo[0] + (np.arange(sx) + 0.5) * cell
    yc = g.lo[1] + (np.arange(sy) + 0.5) * cell
    rad = np.maximum(0.06 * A["L"], 0.5 * (zc - z_floor) + 2 * cell)
    dxy = np.sqrt((xc[:, None] - c[0]) ** 2 + (yc[None, :] - c[1]) ** 2)
    cone = dxy[:, :, None] <= rad[None, None, :]
    mask = cone & g.solid
    src = np.flatnonzero((mask & A["_torso"]).ravel())
    if not len(src):
        return None
    d, par = g.dijkstra(src, mask=mask)
    p0 = np.array([c[0], c[1], z_floor + 0.5 * cell])
    s0 = g.nearest_solid(p0, mask=mask)
    if not np.isfinite(d[s0]):
        return None
    path = Grid.trace(par, s0)
    return smooth_path(np.vstack([p0, g.centers(path)]), 2)


def fit_leg(A, leg, kind, pos, ratios):
    """Joints [root, j1, j2, paw_head, toe] for one traced leg, plus a confidence."""
    foot = leg["foot"]
    if kind == "insect":
        path, att = leg["path"], leg["attach"]          # contact tip -> attachment
    else:
        path, att = leg["path_fp"], leg["attach_fp"]    # footprint centre -> attachment
        rise = att[2] - path[0][2]
        drift = np.linalg.norm(att[:2] - path[0][:2])
        if drift > 0.6 * max(rise, 1e-6):               # suspicious detour: re-trace inside a cone
            cp = cone_path(A, foot, float(A["_V"][foot["ids"]][:, 2].min()))
            if cp is not None:
                path, att = cp, cp[-1].copy()
                leg["retraced"] = True
    V = A["_V"]
    Q = V[foot["ids"]]
    cell = A["cell"]
    H = A["H"]
    zb, zt = torso_column(A, att[1])
    conf = 1.0
    if kind == "insect":
        tip = np.array([foot["xy"][0], foot["xy"][1], float(Q[:, 2].min()) + 0.15 * cell])
        R = att.copy()
        R[0] -= np.sign(att[0]) * cell
        poly = np.vstack([tip, path[1:], R])          # tip -> root
        k = int(np.argmax(poly[:, 2]))
        span_z = poly[k, 2] - max(poly[0, 2], poly[-1, 2])
        if span_z > 0.08 * polyline_len(poly) and 0 < k < len(poly) - 1:
            knee = poly[k]
            down = poly[:k + 1][::-1]                  # knee -> tip
        else:
            conf = 0.6
            knee = resample(poly[::-1], [0.42])[0]
            down = np.vstack([knee, tip])
        ank, ph = resample(down, [0.55, 0.86])
        return [R, knee, ank, ph, tip], conf
    if kind == "bird":
        zp = max(0.03 * H, 1.5 * cell)
        R = np.array([att[0] * 0.8, att[1], zb + 0.35 * (zt - zb)])
        poly = np.vstack([path, R])
        k1 = at_height(poly, zb)
        k1 = k1 + np.array([0, -0.06 * zb, 0])         # knee forward
        R[1] = k1[1] + 0.45 * (R[2] - k1[2])           # hip behind the knee
        ph = at_height(poly, zp)
        k2 = at_height(poly, ratios.get("mid", 0.45) * zb)
        seg = np.array([p_ for p_ in poly if zp + 0.1 * (k1[2] - zp) < p_[2] < k1[2] - 0.1 * (k1[2] - zp)])
        if len(seg) >= 3:                                # the ankle is the backward bend of the leg
            line = k1 - ph
            ln = np.linalg.norm(line)
            if ln > 1e-6:
                t_ = ((seg - ph) @ line) / (ln * ln)
                dev = (seg - (ph + t_[:, None] * line))[:, 1]
                i_ = int(np.argmax(dev))
                if dev[i_] > 0.03 * zb:
                    k2 = seg[i_]
        fy = float(foot["fp_lo"][1])
        toe = np.array([ph[0], fy + 0.1 * (foot["fp_hi"][1] - fy), 0.4 * zp])
        j = [R, k1, k2, ph, toe]
        _ensure_bend(j, +1.0, 0.04)                     # ankle sticks out backward
        return j, conf
    # mammal
    front = pos == "front"
    poly = np.vstack([path, att])
    zr = zb + 0.45 * (zt - zb)
    R = np.array([att[0] * 0.72, att[1], zr])
    poly_r = np.vstack([poly, R])
    k1 = at_height(poly_r, zb)
    dz = zr - k1[2]
    R[1] = k1[1] - 0.35 * dz if front else k1[1] + 0.3 * dz
    if att[2] < 0.5 * zb or att[2] > zb + 0.6 * (zt - zb):
        conf = 0.7
    rr = ratios.get(pos, {})
    zp = max(rr.get("paw", 0.16) * zb, 0.04 * H)
    ph = at_height(poly, zp)
    zm = rr.get("mid", 0.5 if front else 0.55) * zb
    k2 = at_height(poly, zm)
    # bend detection between paw head and knee: hock (back) / carpus (front)
    seg = np.array([p for p in poly if zp + 0.1 * (k1[2] - zp) < p[2] < k1[2] - 0.15 * (k1[2] - zp)])
    if len(seg) >= 3:
        line = k1 - ph
        ln = np.linalg.norm(line)
        if ln > 1e-6:
            t = ((seg - ph) @ line) / (ln * ln)
            off = seg - (ph + t[:, None] * line)
            dev = off[:, 1] * (1.0 if not front else -1.0)      # +y = backward
            i = int(np.argmax(dev))
            if dev[i] > 0.03 * zb:
                k2 = seg[i]
    fy = float(foot["fp_lo"][1])
    toe = np.array([ph[0], fy + 0.12 * (foot["fp_hi"][1] - fy), min(0.3 * zp, 0.02 * H)])
    j = [R, k1, k2, ph, toe]
    _ensure_bend(j, -1.0 if front else +1.0, 0.02 if front else 0.035)
    return j, conf


def _ensure_bend(j, sign_y, frac):
    """Make the middle joint j[2] stick out along sign_y*Y from the line j[1]->j[3] (IK slack)."""
    a, m, b = j[1], j[2], j[3]
    line = b - a
    L = np.linalg.norm(line)
    if L < 1e-6:
        return
    t = float(np.clip(((m - a) @ line) / (L * L), 0.05, 0.95))
    foot = a + t * line
    off = (m - foot)[1] * sign_y
    need = frac * L
    if off < need:
        j[2] = m + np.array([0.0, sign_y * (need - off), 0.0])


def symmetrize_pair(jl, jr, tol):
    """Average mirrored joint sets when the mesh is near-symmetric."""
    ok = all(np.linalg.norm(np.asarray(a) * [-1, 1, 1] - np.asarray(b)) < tol for a, b in zip(jl, jr))
    if not ok:
        return jl, jr, False
    out_l, out_r = [], []
    for a, b in zip(jl, jr):
        m = 0.5 * (np.asarray(a) + np.asarray(b) * [-1, 1, 1])
        m[0] = abs(m[0])
        out_l.append(m)
        out_r.append(m * [-1, 1, 1])
    return out_l, out_r, True


# --------------------------------------------------------------------------- jaw

def find_mouth(A, head_base, tip):
    """Open-mouth detection on the mesh itself: vertical rays through the front half of the head.

    A closed head gives 2 surface hits per ray, an open mouth 4 (upper jaw top/bottom, lower jaw
    top/bottom). Rays work at any resolution; the voxel grid would close gaps under ~2 cells."""
    from mathutils.bvhtree import BVHTree
    hb, tp = np.asarray(head_base, float), np.asarray(tip, float)
    L = float(np.linalg.norm(tp - hb))
    if L < 1e-4:
        return None
    if "_bvh" not in A:
        A["_bvh"] = BVHTree.FromPolygons([tuple(v) for v in A["_V"]], [tuple(f) for f in A["_T"].tolist()])
    bvh = A["_bvh"]
    top = float(A["_V"][:, 2].max()) + 0.05 * A["H"] + 0.01
    gaps = []
    for t in np.linspace(0.4, 1.02, 16):
        p = hb + (tp - hb) * t
        hits = []
        o = Vector((float(p[0]), float(p[1]), top))
        d = Vector((0, 0, -1))
        for _ in range(8):
            loc, nrm, idx, dist = bvh.ray_cast(o, d)
            if loc is None:
                break
            hits.append(loc.z)
            o = loc + d * 1e-4
        hits = [h for h in hits if abs(h - p[2]) < 0.9 * L]
        best = None
        g = A["_g"]
        if len(hits) >= 4:
            for k in range(1, len(hits) - 2, 2):        # (upper bottom, lower top) pairs
                gap = hits[k] - hits[k + 1]
                if gap > max(0.004 * A["L"], 0.06 * L) and (best is None or gap > best[3]):
                    zm = 0.5 * (hits[k] + hits[k + 1])
                    # overlapping closed parts (kitbashed head + muzzle) also give 4 hits; air in a mouth
                    # is not solid. Gaps up to ~3 cells may be closed by the voxeliser, so only wider
                    # gaps are tested.
                    if gap > 3.0 * g.cell and g.solid.ravel()[g.flat([p[0], p[1], zm])]:
                        continue
                    best = (float(t), zm, 0.5 * (hits[k + 1] + hits[k + 2]), gap)
        gaps.append(best)
    run = []
    for gp in reversed(gaps):          # contiguous gap run that reaches the front of the head
        if gp is None:
            if run:
                break
            continue
        run.append(gp)
    if len(run) < 3:
        return None
    hinge_t, hinge_z = run[-1][0], run[-1][1]
    tip_t, _, low_z, _ = run[0]
    if (tip_t - hinge_t) < 0.25:                     # a mouth spans a good part of the head
        return None
    hinge = hb + (tp - hb) * hinge_t
    hinge[2] = hinge_z
    jt = hb + (tp - hb) * min(1.0, tip_t)
    jt[2] = low_z
    return {"hinge": hinge, "tip": jt, "gap_len": float((tip_t - hinge_t) * L),
            "gap_m": round(float(max(g_[3] for g_ in run)), 4)}


def folded_wing(A, side, withers):
    """([shoulder, elbow, wrist, tip], tip) of a wing folded against the body, or None.

    Folded wings of AI meshes are fused to the flank, so they are not thin parts the analysis can
    separate. The chain is FITTED, not detected: shoulder beside the withers a little below the back
    line, tip at the rearmost upper-lateral mass that is not tail (folded primaries end over the rump),
    elbow/wrist at 30/62 % inside the outer layer of the flank. It is a control chain for small motions,
    not wing anatomy (a real folded wing zig-zags: humerus back, forearm forward, hand back)."""
    g = A["_g"]
    cell = g.cell
    sg = 1.0 if side == "l" else -1.0
    ids = np.flatnonzero(g.solid.ravel())
    P = g.centers(ids)
    tz0, tz1 = A["torso_z"]
    th = max(tz1 - tz0, 4 * cell)
    hw = max(A["torso_halfwidth"], 2 * cell)
    Wp = np.asarray(withers, float)

    def outer(y, z):
        m = (np.abs(P[:, 1] - y) <= 1.5 * cell) & (np.abs(P[:, 2] - z) <= 1.5 * cell) & (sg * P[:, 0] > 0)
        return float((sg * P[m, 0]).max()) if m.any() else hw

    zs = min(float(Wp[2]), tz1) - 0.18 * th
    xs = outer(Wp[1], zs)
    S = np.array([sg * 0.55 * xs, Wp[1], zs])
    rear_y = -A["torso_f"][0]                       # torso_f = forward coords; rear end of the torso
    up = ((sg * P[:, 0] > 0.3 * hw) & (P[:, 2] > tz0 + 0.45 * th) & (P[:, 1] > Wp[1])
          & (P[:, 1] < rear_y + 0.1 * A["L"]))
    if up.sum() < 10:
        return None
    Q = P[up]
    tip = Q[int(np.argmax(Q[:, 1]))].copy()
    if tip[1] - S[1] < 0.12 * A["L"]:
        return None
    jj = [S]
    for fr in (0.3, 0.62, 0.97):
        p = S + (tip - S) * fr
        xe = outer(p[1], p[2])
        p[0] = sg * max(0.55 * xe, xe - 2.0 * cell)
        jj.append(p)
    return jj, tip


# =========================================================================== layout (bones)

def _pos_names(template, npairs):
    pos = list(LEG_POSITIONS.get(template, []))
    if npairs == len(pos):
        return pos
    if npairs <= 0:
        return []
    if npairs == 1:
        return ["back"] if template == "bird" else ["front"]
    names = ["front"] + ["mid" if i == 1 else "mid%d" % i for i in range(1, npairs - 1)] + ["back"]
    return names


SPINE_COUNT = {"quadruped": 3, "winged_quadruped": 3, "bird": 2, "hexapod": 2, "octopod": 1}
LIFT = {"quadruped": 0.62, "winged_quadruped": 0.62, "bird": 0.55, "hexapod": 0.5, "octopod": 0.5,
        "serpent": 0.5}


class Layout:
    def __init__(self):
        self.bones = {}          # name -> dict
        self.order = []
        self.chains = []         # dicts: id, kind, side, bones, pts
        self.legs = []           # anim info per leg
        self.wings = []
        self.conf = {}
        self.marks = {}
        self.warnings = []

    def add(self, name, head, tail, parent=None, connect=False, deform=True, kind="main", side=None):
        h = np.asarray(head, float)
        t = np.asarray(tail, float)
        if np.linalg.norm(t - h) < 1e-4:
            t = h + np.array([0.0, -1e-3, 0.0])          # Blender drops zero-length bones
        self.bones[name] = {"head": h, "tail": t, "parent": parent, "connect": bool(connect),
                            "deform": bool(deform), "kind": kind, "side": side}
        self.order.append(name)

    def chain(self, cid, kind, names, side=None, **extra):
        pts = [self.bones[names[0]]["head"]] + [self.bones[n]["tail"] for n in names]
        c = {"id": cid, "kind": kind, "side": side, "bones": list(names), "pts": np.array(pts)}
        c.update(extra)
        self.chains.append(c)
        return c


def _nearest_main(lay, p, names):
    best, arg = 1e9, names[0]
    for n in names:
        b = lay.bones[n]
        d = float(seg_dist(np.atleast_2d(p), b["head"], b["tail"])[0][0])
        if d < best:
            best, arg = d, n
    return arg


def build_layout(A, template, params):
    lay = Layout()
    L, H, cell = A["L"], A["H"], A["cell"]
    axis0 = A["_axis"]
    lay.add("root", (0, 0, 0), (0, -0.12 * max(L, H), 0), None, deform=False, kind="root")
    if template == "custom":
        _layout_custom(lay, A, params)
        return lay
    if template == "serpent":
        _layout_serpent(lay, A, params)
        return lay
    kind = LEG_KIND[template]
    ratios = params.get("leg_ratios", {}) or {}
    # ----- legs
    pairs = A["_pairs"]
    names = _pos_names(template, len(pairs))
    expected = len(LEG_POSITIONS[template])
    if len(pairs) != expected:
        lay.warnings.append("template %s expects %d leg pairs, geometry has %d" % (template, expected, len(pairs)))
    fitted = {}
    for lg in A["_legs"]:
        pos = names[lg["pair"]] if lg["pair"] < len(names) else "mid%d" % lg["pair"]
        j, c = fit_leg(A, lg, kind, pos if kind == "mammal" else pos, ratios)
        fitted[(pos, lg["side"])] = (j, c, lg)
    sym_tol = 0.06 * max(L, A["W"])
    for pos in names:
        if (pos, "l") in fitted and (pos, "r") in fitted and params.get("symmetrize", True):
            jl, cl_, lgl = fitted[(pos, "l")]
            jr, cr, lgr = fitted[(pos, "r")]
            jl, jr, ok = symmetrize_pair(jl, jr, sym_tol)
            fitted[(pos, "l")] = (jl, cl_, lgl)
            fitted[(pos, "r")] = (jr, cr, lgr)
    # ----- spine landmarks
    frac = LIFT[template]
    axis = lift_spine(A, axis0, frac)
    roots = {pos: np.mean([fitted[(pos, s)][0][0] for s in ("l", "r") if (pos, s) in fitted], axis=0)
             for pos in names if any((pos, s) in fitted for s in ("l", "r"))}
    tf0, tf1 = A["torso_f"]
    f_back = -roots[names[-1]][1] if names else tf0 + 0.25 * (tf1 - tf0)
    if template == "bird":
        if len(A["_wings"]) == 2:
            f_front = -np.mean([w["attach"][1] for w in A["_wings"].values()])
        else:
            f_front = tf0 + 0.8 * (tf1 - tf0)
    else:
        f_front = -roots[names[0]][1] if len(names) > 1 else tf0 + 0.8 * (tf1 - tf0)
    if f_front - f_back < 0.08 * L:
        if template not in ("bird", "octopod"):           # normal for birds/spiders, odd otherwise
            lay.warnings.append("leg roots too close along the body; spine span from torso extent")
        f_back, f_front = tf0 + 0.25 * (tf1 - tf0), tf0 + 0.8 * (tf1 - tf0)
    # pelvis / withers = axis points nearest to the hind / front limb roots (works for rising necks
    # and chests that sit under the head, where a forward-coordinate lookup would land in the neck)
    def nearest_on_axis(pt):
        d = np.linalg.norm(axis - np.asarray(pt), axis=1)
        k = int(np.argmin(d))
        return axis[k].copy(), float(k)
    back_ref = roots[names[-1]] if names else axis_at_f(axis, f_back)[0]
    if template == "bird" and len(A["_wings"]) == 2:
        front_ref = np.mean([w["attach"] for w in A["_wings"].values()], axis=0)
    elif len(names) > 1:
        front_ref = roots[names[0]]
    else:
        front_ref = axis_at_f(axis, f_front)[0]
    # pelvis by forward coordinate (a 3D-nearest lookup can snap onto a tail curving down past the hips),
    # withers by 3D distance (chests that sit under a rising neck)
    P0, i0 = axis_at_f(axis, -float(back_ref[1]))
    W, iw = nearest_on_axis(front_ref)
    if iw - i0 < 2 or polyline_len(sub_polyline(axis, i0, max(iw, i0 + 1e-3))) < 0.25 * (tf1 - tf0):
        # legs under the wing roots (birds) or under the chest: span the spine over the torso instead
        f_back, f_front = tf0 + 0.22 * (tf1 - tf0), tf0 + 0.8 * (tf1 - tf0)
        P0, i0 = axis_at_f(axis, f_back)
        W, iw = axis_at_f(axis, f_front)
    if template in ("quadruped", "winged_quadruped"):
        # withers sit a little ahead of the shoulder joints
        mid_poly = sub_polyline(axis, i0, len(axis) - 1)
        ml = polyline_len(mid_poly)
        off = polyline_len(sub_polyline(axis, i0, iw)) + 0.03 * L
        W = resample(mid_poly, [min(off / max(ml, 1e-9), 0.7)])[0]
        iw = float(np.argmin(np.linalg.norm(axis - W, axis=1)))
    ns = SPINE_COUNT[template]
    fr = [0.0, 0.2] + list(np.linspace(0.2, 1.0, ns + 1)[1:])
    spine_poly = np.vstack([sub_polyline(axis, i0, iw), W])
    sp = list(resample(spine_poly, fr))
    lay.add("pelvis", sp[0], sp[1], "root", kind="main")
    prev = "pelvis"
    main = ["pelvis"]
    for k in range(ns):
        nm = "spine_%02d" % (k + 1)
        lay.add(nm, sp[k + 1], sp[k + 2], prev, connect=True, kind="main")
        main.append(nm)
        prev = nm
    spine_names = list(main)
    # ----- neck + head
    front_poly = sub_polyline(axis, iw, len(axis) - 1)
    front_poly = np.vstack([W, front_poly[1:]]) if len(front_poly) > 1 else np.vstack([W, axis[-1]])
    flen = polyline_len(front_poly)
    hb_fr = None
    if template in ("quadruped", "winged_quadruped", "bird"):
        hb_fr, ang = turning_point(front_poly, 0.35, 0.78)
        if hb_fr is not None and flen * (1.0 - hb_fr) < 0.1 * L:
            hb_fr = None                                   # a bend at the muzzle is not the poll
        lay.conf["head"] = 0.9 if hb_fr is not None else 0.6
        if hb_fr is None:
            hb_fr = 0.6 if template != "bird" else 0.62
        n_neck = 3 if (template == "bird" or flen * hb_fr > 0.3 * L) else 2
    elif template == "hexapod":
        hb_fr = constriction(A, front_poly, 0.15, 0.8)
        lay.conf["head"] = 0.85 if hb_fr is not None else 0.6
        hb_fr = hb_fr if hb_fr is not None else 0.45
        n_neck = 1
    else:
        hb_fr, n_neck = 0.0, 0
        lay.conf["head"] = 0.7
    min_head = {"quadruped": 0.09, "winged_quadruped": 0.09, "bird": 0.05}.get(template, 0.0) * L
    if n_neck and flen * (0.965 - hb_fr) < min_head:
        hb_fr = float(np.clip(0.965 - min_head / max(flen, 1e-9), 0.25, hb_fr))
    hp = resample(front_poly, [hb_fr, 0.965])
    hbase, htip = hp[0], hp[1]
    if n_neck:
        nk = resample(front_poly, list(np.linspace(0, hb_fr, n_neck + 1)))
        for k in range(n_neck):
            nm = "neck_%02d" % (k + 1)
            lay.add(nm, nk[k], nk[k + 1], prev, connect=True, kind="main")
            main.append(nm)
            prev = nm
        lay.add("head", hbase, htip, prev, connect=True, kind="main")
    else:
        lay.add("head", W, htip, prev, connect=True, kind="main")
    main.append("head")
    lay.chain("main", "main", main)
    # ----- jaw
    jaw_p = params.get("jaw", "auto")
    mouth = None
    if jaw_p:
        mouth = find_mouth(A, lay.bones["head"]["head"], htip)
        if mouth is None and jaw_p is True:
            hb_, ht_ = lay.bones["head"]["head"], lay.bones["head"]["tail"]
            hl = np.linalg.norm(ht_ - hb_)
            mouth = {"hinge": hb_ + (ht_ - hb_) * 0.35 - np.array([0, 0, 0.18 * hl]),
                     "tip": ht_ - np.array([0, 0, 0.22 * hl]), "gap_len": 0.0, "forced": True}
    if mouth is not None:
        lay.add("jaw", mouth["hinge"], mouth["tip"], "head", kind="jaw")
        lay.marks["mouth"] = mouth
    lay.conf["jaw"] = "found" if (mouth and not mouth.get("forced")) else ("forced" if mouth else "none")
    # ----- tail
    tail_poly = sub_polyline(axis, 0, i0)[::-1]           # pelvis -> rear tip
    tlen = polyline_len(tail_poly)
    tp = params.get("tail", "auto")
    torso = A["_torso_big"]
    g = A["_g"]
    if tp is not False and tp != 0 and tlen >= 0.12 * L:
        # the tail starts where the axis leaves the thick torso (otherwise the rump would swing)
        inside = torso.ravel()[g.flat(tail_poly)]
        start = 0.0
        if kind == "mammal" and not inside.all():
            k = int(np.argmin(inside)) if not inside[0] else int(np.argmax(~inside))
            k = max(0, k - 1)
            start = polyline_len(tail_poly[:k + 1]) / max(tlen, 1e-9)
        rest_len = tlen * (1 - start)
        if isinstance(tp, int) and not isinstance(tp, bool):
            nt = int(tp)
        elif kind == "mammal":
            nt = int(np.clip(round(rest_len / (0.1 * L)), 2, 6))
        elif kind == "bird":
            nt = 2
        else:
            nt = int(np.clip(round(rest_len / (0.12 * L)), 1, 3))
        tj = resample(tail_poly, list(np.linspace(start, 0.985, nt + 1)))
        prev_t = "pelvis"
        tnames = []
        for k in range(nt):
            nm = "tail_%02d" % (k + 1)
            lay.add(nm, tj[k], tj[k + 1], prev_t, connect=k > 0, kind="tail")
            tnames.append(nm)
            prev_t = nm
        lay.chain("tail", "tail", tnames)
        lay.conf["tail"] = 0.85 if start > 0 or kind != "mammal" else 0.65
    else:
        lay.conf["tail"] = "none"
    # ----- legs (bones)
    for pos in names:
        for side in ("l", "r"):
            if (pos, side) not in fitted:
                continue
            j, c, lg = fitted[(pos, side)]
            parent = _nearest_main(lay, j[0], spine_names)
            nm = ["leg_%s_%s_%d" % (pos, side, k) for k in (1, 2, 3)] + ["paw_%s_%s" % (pos, side)]
            lay.add(nm[0], j[0], j[1], parent, kind="leg", side=side)
            lay.add(nm[1], j[1], j[2], nm[0], connect=True, kind="leg", side=side)
            lay.add(nm[2], j[2], j[3], nm[1], connect=True, kind="leg", side=side)
            lay.add(nm[3], j[3], j[4], nm[2], connect=True, kind="leg", side=side)
            ch = lay.chain("leg_%s_%s" % (pos, side), "leg", nm, side=side, leg_kind=kind, pos=pos)
            lay.legs.append({"id": "%s_%s" % (pos, side), "pos": pos, "side": side, "kind": kind,
                             "bones": nm, "parent": parent, "contact": r3(j[4]), "paw_head": r3(j[3]),
                             "root": r3(j[0]), "confidence": c})
            lay.marks.setdefault("_footprints", {})["%s_%s" % (pos, side)] = lg["foot"].get("fp_ids")
            lay.conf["leg_%s_%s" % (pos, side)] = c
    # ----- wings
    if template in WINGED:
        found = {s: A["_wings"][s] for s in ("l", "r") if s in A["_wings"]}
        for side in ("l", "r"):
            w = found.get(side)
            folded = False
            if w is not None:
                att = w["attach"].copy()
                Rw = np.array([att[0] * 0.85, att[1], att[2] - cell])
                pts = np.vstack([Rw, w["path"]])
                jj = resample(pts, [0.0, 0.3, 0.62, 0.97])
                tip = w["tip"]
                lay.conf["wing_" + side] = 0.8
            elif found:                                    # one wing found: mirror it
                o = found["r" if side == "l" else "l"]
                att = o["attach"] * [-1, 1, 1]
                Rw = np.array([att[0] * 0.85, att[1], att[2] - cell])
                pts = np.vstack([Rw, o["path"] * [-1, 1, 1]])
                jj = resample(pts, [0.0, 0.3, 0.62, 0.97])
                tip = o["tip"] * [-1, 1, 1]
                lay.warnings.append("wing %s not found in geometry: mirrored from the other wing" % side)
                lay.conf["wing_" + side] = 0.5
            else:
                fw = folded_wing(A, side, W)
                if fw is None:
                    lay.warnings.append("wing %s not found in geometry" % side)
                    lay.conf["wing_" + side] = 0.0
                    continue
                jj, tip = fw
                folded = True
                lay.conf["wing_" + side] = 0.45
            parent = _nearest_main(lay, jj[0], spine_names)
            nm = ["wing_%s_%d" % (side, k) for k in (1, 2, 3)]
            lay.add(nm[0], jj[0], jj[1], parent, kind="wing", side=side)
            lay.add(nm[1], jj[1], jj[2], nm[0], connect=True, kind="wing", side=side)
            lay.add(nm[2], jj[2], jj[3], nm[1], connect=True, kind="wing", side=side)
            lay.chain("wing_" + side, "wing", nm, side=side, folded=folded)
            lay.wings.append({"side": side, "bones": nm, "parent": parent, "tip": r3(tip), "folded": folded})
        if any(w_.get("folded") for w_ in lay.wings):
            lay.warnings.append("wings are not separate thin parts of the mesh (folded against the body): "
                                "wing bones are fitted along the upper flank, not detected; fly-loop and "
                                "glide-loop are skipped and wing motion in other clips is damped")
    lay.marks.update({"pelvis": r3(P0), "withers": r3(W), "head_base": r3(lay.bones["head"]["head"]),
                      "nose": r3(htip), "spine_bones": spine_names})
    lay.conf["spine"] = 0.85
    return lay


def _layout_serpent(lay, A, params):
    axis = lift_spine(A, A["_axis"], LIFT["serpent"])
    n_sp = int(params.get("spine_segments", 6))
    n_tl = int(params.get("tail_segments", 6))
    total = polyline_len(axis)
    # fractions measured from the FRONT tip (axis runs rear -> front)
    def at(ff):
        return resample(axis, [1.0 - ff])[0]
    head_b, n2, n1, pel_f = 0.065, 0.11, 0.16, 0.55
    sp_f = list(np.linspace(pel_f - 0.04, n1, n_sp + 1))        # spine_01 head .. spine_N tail
    lay.add("pelvis", at(pel_f), at(sp_f[0]), "root", kind="main")
    prev = "pelvis"
    main = ["pelvis"]
    for k in range(n_sp):
        nm = "spine_%02d" % (k + 1)
        lay.add(nm, at(sp_f[k]), at(sp_f[k + 1]), prev, connect=True, kind="main")
        main.append(nm)
        prev = nm
    lay.add("neck_01", at(n1), at(n2), prev, connect=True, kind="main")
    lay.add("neck_02", at(n2), at(head_b), "neck_01", connect=True, kind="main")
    lay.add("head", at(head_b), at(0.004), "neck_02", connect=True, kind="main")
    main += ["neck_01", "neck_02", "head"]
    lay.chain("main", "main", main)
    jaw_p = params.get("jaw", "auto")
    mouth = find_mouth(A, lay.bones["head"]["head"], lay.bones["head"]["tail"]) if jaw_p else None
    if mouth is not None:
        lay.add("jaw", mouth["hinge"], mouth["tip"], "head", kind="jaw")
        lay.marks["mouth"] = mouth
    lay.conf["jaw"] = "found" if mouth else "none"
    tl_f = list(np.linspace(pel_f, 0.995, n_tl + 1))
    prev = "pelvis"
    tn = []
    for k in range(n_tl):
        nm = "tail_%02d" % (k + 1)
        lay.add(nm, at(tl_f[k]), at(tl_f[k + 1]), prev, connect=k > 0, kind="tail")
        tn.append(nm)
        prev = nm
    lay.chain("tail", "tail", tn)
    lay.marks.update({"pelvis": r3(at(pel_f)), "head_base": r3(at(head_b)), "nose": r3(at(0.004)),
                      "spine_bones": ["pelvis"] + ["spine_%02d" % (k + 1) for k in range(n_sp)],
                      "length_m": round(total, 4)})
    lay.conf.update({"spine": 0.8, "head": 0.7, "tail": 0.8})


def _layout_custom(lay, A, params):
    chains = params.get("custom_chains") or []
    if not chains:
        raise ValueError("template 'custom' needs params.custom_chains = [{name, parent, points}]")
    for ch in chains:
        pts = np.asarray(ch["points"], float)
        if len(pts) < 2:
            raise ValueError("custom chain %s needs at least 2 points" % ch.get("name"))
        names = ch.get("bones") or ["%s_%02d" % (ch["name"], k + 1) for k in range(len(pts) - 1)]
        parent = ch.get("parent", "root")
        prev = parent
        for k, nm in enumerate(names):
            lay.add(nm, pts[k], pts[k + 1], prev, connect=k > 0, kind=ch.get("kind", "custom"),
                    side=ch.get("side"))
            prev = nm
        lay.chain(ch["name"], ch.get("kind", "custom"), names, side=ch.get("side"))
    lay.conf["custom"] = 1.0


# =========================================================================== armature

def build_armature(lay, name="creature_rig"):
    data = bpy.data.armatures.new(name)
    data.display_type = "OCTAHEDRAL"
    arm = bpy.data.objects.new(name, data)
    bpy.context.scene.collection.objects.link(arm)
    arm.show_in_front = True
    af.select_only([arm])
    bpy.ops.object.mode_set(mode="EDIT")
    eb = data.edit_bones
    for n in lay.order:
        b = lay.bones[n]
        e = eb.new(n)
        e.head = v3(b["head"])
        e.tail = v3(b["tail"])
        d = (e.tail - e.head).normalized()
        e.align_roll(FWD if abs(d.z) > 0.7 else UP)
        e.use_deform = b["deform"]
    for n in lay.order:
        b = lay.bones[n]
        if b["parent"]:
            e = eb[n]
            e.parent = eb[b["parent"]]
            e.use_connect = bool(b["connect"]) and (e.head - e.parent.tail).length < 1e-5
    bpy.ops.object.mode_set(mode="OBJECT")
    for pb in arm.pose.bones:
        pb.rotation_mode = "QUATERNION"
    return arm


# =========================================================================== weights

def chain_arc_weights(P, joints, blend_frac=0.3):
    """Distance to a joint polyline, arc parameter and per-bone weights with smooth joints."""
    J = [np.asarray(j, float) for j in joints]
    n = len(J) - 1
    lens = np.array([max(np.linalg.norm(J[i + 1] - J[i]), 1e-6) for i in range(n)])
    S = np.concatenate([[0.0], np.cumsum(lens)])
    dmin = np.full(len(P), np.inf)
    s = np.zeros(len(P))
    for i in range(n):
        d, t = seg_dist(P, J[i], J[i + 1])
        better = d < dmin
        dmin[better] = d[better]
        s[better] = S[i] + t[better] * lens[i]
    Wc = np.zeros((len(P), n))
    steps = [smoothstep(S[k] - blend_frac * min(lens[k - 1], lens[k]),
                        S[k] + blend_frac * min(lens[k - 1], lens[k]), s) for k in range(1, n)]
    for i in range(n):
        lo = steps[i - 1] if i > 0 else np.ones(len(P))
        hi = steps[i] if i < n - 1 else np.zeros(len(P))
        Wc[:, i] = lo - hi
    return dmin, s, S, Wc


def fallback_weights(ob, lay, A, deform):
    """Distance-to-chain weights with region masks, local thickness, smoothing, 4 influences."""
    V, _, E = mesh_np(ob)
    g = A["_g"]
    n = len(V)
    col = {b: i for i, b in enumerate(deform)}
    torso_v = A["_torso"].ravel()[g.flat(V)]
    span = max(A["L"], A["W"])
    wside = max(g.cell, 0.01 * span)
    f = -V[:, 1]
    aff, per = [], []
    for c in lay.chains:
        bones = [b for b in c["bones"] if b in col]
        if not bones:
            continue
        pts = c["pts"]
        d, s, S, Wc = chain_arc_weights(V, pts)
        r = g.dt_at(pts)
        if c["kind"] in ("leg", "wing"):
            r[0] = r[1] if len(r) > 1 else r[0]
        r = np.maximum(r, 1.5 * g.cell)
        rv = np.interp(s, S, r)
        dn = d / rv
        mask = np.ones(n)
        sg = {"l": 1.0, "r": -1.0}.get(c.get("side"), 0.0)
        if c["kind"] == "main":
            # the body chain must not grab wing membranes/feathers lateral of the shoulders
            for wc in lay.chains:
                if wc["kind"] != "wing":
                    continue
                wsg = 1.0 if wc.get("side") == "l" else -1.0
                rx = abs(float(wc["pts"][0][0]))
                out = smoothstep(rx + g.cell, rx + 3 * g.cell, wsg * V[:, 0]) * (~torso_v)
                mask *= 1.0 - 0.95 * out
        if sg:
            mask *= smoothstep(-wside, wside, sg * V[:, 0])
        if c["kind"] == "leg":
            R, K1 = pts[0], pts[1]
            if c.get("leg_kind") == "insect":
                inner = 0.15 + 0.85 * smoothstep(0.4 * abs(R[0]), abs(R[0]) + g.cell, sg * V[:, 0])
            else:
                dz = max(R[2] - K1[2], g.cell)
                inner = 0.15 + 0.85 * (smoothstep(K1[2] + 0.7 * dz, K1[2] + 0.1 * dz, V[:, 2])
                                       * smoothstep(0.25 * abs(R[0]), 0.75 * abs(R[0]) + 1e-6, sg * V[:, 0]))
            mask *= np.where(torso_v, inner, 1.0)
        elif c["kind"] == "wing":
            R0 = pts[0]
            lateral = smoothstep(abs(R0[0]), abs(R0[0]) + 2 * g.cell, sg * V[:, 0])
            mask *= np.where(torso_v, 0.08 + 0.92 * lateral, 1.0)
        elif c["kind"] == "tail":
            f0 = -pts[0][1]
            mask *= smoothstep(f0 + 2 * g.cell, f0 - g.cell, f)
            mask *= np.where(torso_v, 0.35, 1.0)
        aff.append(mask / (dn + 0.08) ** 4)
        Wfull = np.zeros((n, len(deform)))
        for k, b in enumerate(c["bones"]):
            if b in col:
                Wfull[:, col[b]] += Wc[:, k]
        per.append(Wfull)
    Aff = np.stack(aff, 1)
    Rg = Aff / np.maximum(Aff.sum(1, keepdims=True), 1e-30)
    W = np.zeros((n, len(deform)))
    for k, Wfull in enumerate(per):
        W += Rg[:, k:k + 1] * Wfull
    # jaw: lower part of the head in front of the hinge
    if "jaw" in col and "mouth" in lay.marks:
        m = lay.marks["mouth"]
        h, t = np.asarray(m["hinge"]), np.asarray(m["tip"])
        hd = col["head"]
        fh, ft = -h[1], -t[1]
        tt = np.clip((f - fh) / max(ft - fh, 1e-6), 0, 1)
        zline = h[2] + (t[2] - h[2]) * tt + 0.15 * (h[2] - t[2]) * 0
        below = smoothstep(zline + g.cell, zline - g.cell, V[:, 2]) * smoothstep(fh - 2 * g.cell, fh + g.cell, f)
        move = W[:, hd] * below
        W[:, hd] -= move
        W[:, col["jaw"]] += move
    lab = islands(n, E)
    W = _smooth_weights(W, E, V, g.cell, islands_n=len(np.unique(lab)), labels=lab)
    return limit_normalize(W, 4)


def bridge_edges(V, labels, radius, per_vertex=6):
    """Vertex pairs of DIFFERENT islands closer than `radius` (kitbashed / primitive parts that overlap).

    Edge smoothing alone runs inside each island, so two overlapping parts (a wing plank inside the
    body, two wing planks overlapping at the elbow) end up with different weights at the same place
    and split apart when the joint bends. These pairs let the smoothing pass weights across."""
    from mathutils import kdtree
    kd = kdtree.KDTree(len(V))
    for i, p in enumerate(V):
        kd.insert(p.tolist(), i)
    kd.balance()
    pairs = []
    for i, p in enumerate(V):
        li = labels[i]
        hits = [(d, j) for _, j, d in kd.find_range(p.tolist(), radius) if labels[j] != li]
        if len(hits) > per_vertex:                 # dense fragmented meshes: keep the nearest few
            hits = sorted(hits)[:per_vertex]
        pairs.extend((i, j) for _, j in hits if j > i)
    return np.array(pairs, np.int64).reshape(-1, 2)


def _smooth_weights(W, E, V, cell, islands_n=1, iters=4, labels=None):
    n = len(W)
    bridges = 0
    if islands_n > 1 and labels is not None and len(E):
        med = float(np.median(np.linalg.norm(V[E[:, 0]] - V[E[:, 1]], axis=1)))
        Eb = bridge_edges(V, labels, max(2.5 * cell, med))
        bridges = len(Eb)
        if bridges:
            E = np.concatenate([E, Eb])
    if len(E):
        Ee = np.concatenate([E, E[:, ::-1]])
        deg = np.bincount(Ee[:, 0], minlength=n).astype(float)
        for _ in range(iters):
            acc = np.zeros_like(W)
            np.add.at(acc, Ee[:, 0], W[Ee[:, 1]])
            nb = acc / np.maximum(deg, 1)[:, None]
            W = np.where(deg[:, None] > 0, 0.5 * W + 0.5 * nb, W)
    if islands_n > 1:
        # fragmented meshes: also smooth through space so detached pieces follow their neighbours
        from mathutils import kdtree
        kd = kdtree.KDTree(n)
        for i, p in enumerate(V):
            kd.insert(p.tolist(), i)
        kd.balance()
        rad = 1.2 * cell
        out = np.empty_like(W)
        for i, p in enumerate(V):
            hits = kd.find_range(p.tolist(), rad)
            idx = np.fromiter((h[1] for h in hits), int, len(hits))
            dd = np.fromiter((h[2] for h in hits), float, len(hits))
            w = np.exp(-3.0 * (dd / rad) ** 2)
            out[i] = (W[idx] * w[:, None]).sum(0) / w.sum()
        W = out
    return W


def limit_normalize(W, top=4, floor=0.02):
    W = np.maximum(W, 0.0).copy()
    if W.shape[1] > top:
        idx = np.argsort(-W, axis=1)[:, top:]
        np.put_along_axis(W, idx, 0.0, axis=1)
    W[W < floor] = 0.0
    s = W.sum(1, keepdims=True)
    empty = s[:, 0] <= 0
    W = np.where(s > 0, W / np.maximum(s, 1e-12), 0.0)
    return W


def read_weights(ob, names):
    idx = {vg.index: names.index(vg.name) for vg in ob.vertex_groups if vg.name in names}
    W = np.zeros((len(ob.data.vertices), len(names)))
    for v in ob.data.vertices:
        for gr in v.groups:
            j = idx.get(gr.group)
            if j is not None:
                W[v.index, j] = gr.weight
    return W


def write_weights(ob, names, W):
    ob.vertex_groups.clear()
    groups = [ob.vertex_groups.new(name=nm) for nm in names]
    for j, gr in enumerate(groups):
        nz = np.nonzero(W[:, j] > 0)[0]
        for i in nz:
            gr.add([int(i)], float(W[i, j]), "REPLACE")


def attach_mesh(ob, arm):
    ob.parent = arm
    ob.matrix_parent_inverse = Matrix.Identity(4)
    ob.matrix_basis = Matrix.Identity(4)
    mod = next((m for m in ob.modifiers if m.type == "ARMATURE"), None) or ob.modifiers.new("Armature", "ARMATURE")
    mod.object = arm
    mod.use_vertex_groups = True
    mod.use_bone_envelopes = False


def side_purity(W, V, deform, lay, span):
    """Limb bones fade out toward the midline; their weight there goes to the limb's parent body bone.

    Bone heat on fused AI meshes often binds the sternum, belly or back to BOTH legs, which tears the
    mesh when the legs move in opposition. Returns the number of vertices that carried >0.25 weight
    across the line."""
    col = {b: i for i, b in enumerate(deform)}
    w = 0.02 * span
    bad = 0
    for c in lay.chains:
        sd = c.get("side")
        if sd not in ("l", "r") or c["kind"] not in ("leg", "wing"):
            continue
        sg = 1.0 if sd == "l" else -1.0
        rx = abs(float(c["pts"][0][0]))
        keep = smoothstep(0.25 * rx - w, 0.6 * rx, sg * V[:, 0]) if rx > w else smoothstep(-w, w, sg * V[:, 0])
        first = lay.bones[c["bones"][0]]
        parent = first["parent"] if first["parent"] in col else None
        for b in c["bones"]:
            if b not in col:
                continue
            j = col[b]
            bad += int(((sg * V[:, 0]) < -w).sum() and (W[(sg * V[:, 0]) < -w, j] > 0.25).sum())
            moved = W[:, j] * (1.0 - keep)
            W[:, j] -= moved
            if parent is not None:
                W[:, col[parent]] += moved
    for j, b in enumerate(deform):          # any other sided bone: plain cut across the midline
        sd = lay.bones[b]["side"]
        if sd in ("l", "r") and lay.bones[b]["kind"] not in ("leg", "wing"):
            sg = 1.0 if sd == "l" else -1.0
            W[(sg * V[:, 0]) < -w, j] = 0.0
    return bad


def leg_tubes(W, V, deform, lay, A):
    """Loose geometry next to a leg (cloth, caparison, hair) must not follow the lower leg bones.

    A vertex farther than ~1.6 local leg radii from the leg axis gives its leg_2/leg_3/paw weight
    to leg_1 (upper leg), so hanging cloth swings with the shoulder/hip instead of tearing."""
    col = {b: i for i, b in enumerate(deform)}
    g = A["_g"]
    moved = 0
    for lg in lay.legs:
        n1, n2, n3, n4 = lg["bones"]
        pts = np.array([lay.bones[n1]["tail"], lay.bones[n2]["tail"], lay.bones[n3]["tail"],
                        lay.bones[n4]["tail"]])
        r = np.maximum(g.dt_at(pts), 1.5 * g.cell)
        d, sarc, S, _ = chain_arc_weights(V, pts)
        rv = np.interp(sarc, S, r)
        out = smoothstep(1.6 * rv + g.cell, 2.6 * rv + 2 * g.cell, d)
        sg = 1.0 if lg["side"] == "l" else -1.0
        out *= (sg * V[:, 0]) > -0.01
        if lg["kind"] != "insect":                  # the foot itself (toes, hallux, heel) is not cloth
            K3, TT = lay.bones[n4]["head"], lay.bones[n4]["tail"]
            out *= smoothstep(K3[2] + 0.5 * (K3[2] - TT[2]) + g.cell,
                              K3[2] + 0.5 * (K3[2] - TT[2]) + 3 * g.cell, V[:, 2])
        low = [col[n] for n in (n2, n3, n4) if n in col]
        amt = W[:, low].sum(1) * out
        sel = np.nonzero(amt > 1e-4)[0]
        if not len(sel):
            continue
        for j in low:
            W[sel, j] *= (1 - out[sel])
        W[sel, col[n1]] += amt[sel]
        moved += int((amt > 0.05).sum())
    return moved


def rigid_feet(W, V, deform, lay, A):
    """Everything below a mammal/bird paw joint follows the paw bone only (planted feet stay planted)."""
    col = {b: i for i, b in enumerate(deform)}
    fps = lay.marks.get("_footprints", {})
    total = 0
    for lg in lay.legs:
        if lg["kind"] == "insect":
            continue
        paw = lg["bones"][3]
        b = lay.bones[paw]
        K3, TT = b["head"], b["tail"]
        ids = fps.get(lg["id"])
        if ids is None or not len(ids):
            continue
        Q = V[ids]
        c = Q[:, :2].mean(0)
        rad = 0.5 * float(np.max(np.ptp(Q[:, :2], 0))) + 1.5 * A["cell"]
        dlt = max(0.25 * (K3[2] - TT[2]), A["cell"])
        top = K3[2] + 0.75 * (K3[2] - TT[2]) + A["cell"]
        near = np.linalg.norm(V[:, :2] - c, axis=1) < rad
        near &= np.sign(V[:, 0]) == np.sign(c[0])
        # plus anything low that already belongs to this leg's lower bones (rear toes, dew claws)
        lower = sum(W[:, col[n]] for n in lg["bones"][1:] if n in col)
        near = near | (lower > 0.5)
        bw = smoothstep(top, top - 3.5 * dlt, V[:, 2]) * near
        sel = np.nonzero(bw > 0)[0]
        one = np.zeros(W.shape[1])
        one[col[paw]] = 1.0
        W[sel] = (1 - bw[sel, None]) * W[sel] + bw[sel, None] * one
        total += len(sel)
    return total


HEAT_SPAN_M = 4.5      # bone heat runs on a copy whose largest dimension is at least this long ...
HEAT_EDGE_M = 0.05     # ... and whose median edge is at least this long (absolute solver tolerances)


def heat_weights(ob, arm, deform, span_target=None, edge_target=None):
    """Blender bone heat (ARMATURE_AUTO) on a uniformly scaled copy of mesh + armature.

    Blender's heat solver works with absolute tolerances and fails silently (every bone, no weights)
    when the triangles are small in absolute terms: measured on a horse model, a median edge of
    0.016 m fails (native 1 m Tripo size, or 224k verts at 4.5 m) while 0.025 m and longer works.
    Weights do not depend on the scale, so the solve runs on copies scaled up to span >= 4.5 m and
    median edge >= 0.05 m; the originals stay untouched. Returns (weights [verts x deform], scale)."""
    size = float(max(ob.dimensions)) or 1.0
    E = np.empty(len(ob.data.edges) * 2, dtype=np.int64)
    ob.data.edges.foreach_get("vertices", E)
    E = E.reshape(-1, 2)
    Vl = np.empty(len(ob.data.vertices) * 3)
    ob.data.vertices.foreach_get("co", Vl)
    Vl = Vl.reshape(-1, 3)
    med = float(np.median(np.linalg.norm(Vl[E[:, 0]] - Vl[E[:, 1]], axis=1))) if len(E) else size / 100
    s = max(float(span_target or HEAT_SPAN_M) / size, float(edge_target or HEAT_EDGE_M) / max(med, 1e-9))
    s = min(s, 200.0 / size)
    S = Matrix.Scale(s, 4)
    me = ob.data.copy()
    me.transform(S @ ob.matrix_world)
    mo = bpy.data.objects.new("_af_heat_mesh", me)
    ad = arm.data.copy()
    ad.transform(S @ arm.matrix_world)
    ao = bpy.data.objects.new("_af_heat_arm", ad)
    for o in (mo, ao):
        bpy.context.scene.collection.objects.link(o)
    bpy.context.view_layer.update()
    try:
        af.select_only([mo, ao])
        bpy.context.view_layer.objects.active = ao
        bpy.ops.object.parent_set(type="ARMATURE_AUTO")
        W = read_weights(mo, deform)
    finally:
        for o in (mo, ao):
            bpy.data.objects.remove(o, do_unlink=True)
        bpy.data.meshes.remove(me)
        bpy.data.armatures.remove(ad)
        bpy.context.view_layer.update()
    return W, s


def skin(ob, arm, lay, A, mode="auto"):
    """Bone heat first (unless mode='distance'); fall back to chain weights when it fails.

    info['heat']['rejected'] names the reason whenever auto mode drops the heat result."""
    deform = [n for n in lay.order if lay.bones[n]["deform"]]
    V, _, E = mesh_np(ob)
    span = max(A["L"], A["W"])
    info = {"requested": mode, "verts": len(V), "deform_bones": len(deform),
            "islands": int(len(np.unique(islands(len(V), E))))}
    W = None
    if mode in ("auto", "heat"):
        try:
            Wh, hs = heat_weights(ob, arm, deform)
            tot = Wh.sum(1)
            unweighted = int((tot < 1e-3).sum())
            dead = [b for j, b in enumerate(deform) if Wh[:, j].max() < 0.1]
            cross = side_purity(Wh.copy(), V, deform, lay, span)
            info["heat"] = {"unweighted": unweighted, "dead_bones": dead, "cross_side_verts": cross,
                            "solve_scale": round(hs, 4)}
            why = []
            if unweighted > 0.02 * len(V):
                why.append("%d of %d vertices got no weight" % (unweighted, len(V)))
            if dead:
                why.append("%d bones got no vertices (%s)" % (len(dead), ", ".join(dead[:6])
                                                             + (", ..." if len(dead) > 6 else "")))
            if cross > 0.01 * len(V):
                why.append("%d vertices bound to a limb of the other side" % cross)
            if why:
                info["heat"]["rejected"] = "; ".join(why)
            if not why or mode == "heat":
                W = Wh
                info["mode"] = "bone_heat"
                if unweighted:
                    Wf = fallback_weights(ob, lay, A, deform)
                    empty = tot < 1e-3
                    W[empty] = Wf[empty]
                    info["mode"] = "bone_heat+distance_fill"
        except RuntimeError as exc:
            info["heat"] = {"error": str(exc), "rejected": "bone heat raised: %s" % exc}
    if W is None:
        W = fallback_weights(ob, lay, A, deform)
        info["mode"] = "distance_fallback"
    info["cross_side_removed"] = side_purity(W, V, deform, lay, span)
    info["loose_near_legs_verts"] = leg_tubes(W, V, deform, lay, A)
    info["rigid_foot_verts"] = rigid_feet(W, V, deform, lay, A)
    W = limit_normalize(W, 4)
    write_weights(ob, deform, W)
    attach_mesh(ob, arm)
    nz = (W > 0).sum(1)
    info["max_influences"] = int(nz.max()) if len(nz) else 0
    info["unweighted_after"] = int((W.sum(1) < 0.99).sum())
    info["bones_without_verts"] = [b for j, b in enumerate(deform) if W[:, j].max() < 0.05]
    return info


# =========================================================================== sockets

def place_sockets(lay, arm, ob, A, template, W=None):
    V, _, _ = mesh_np(ob)
    span = max(A["L"], A["W"], A["H"])
    sc = "S" if span < 1.0 else ("M" if span < 2.6 else "L")
    deform = [n for n in lay.order if lay.bones[n]["deform"]]
    if W is None:
        W = read_weights(ob, deform)
    col = {b: i for i, b in enumerate(deform)}
    out = []

    def put(name, bone, p, kind):
        s = af.add_socket(name, parent=arm, bone=bone, kind=kind, size=0.04 * span, size_class=sc)
        bpy.context.view_layer.update()
        s.matrix_world = Matrix.Translation(v3(p))
        out.append({"name": name, "bone": bone, "location": r3(p), "kind": kind})
        return s

    hb = lay.bones["head"]
    h0, h1 = hb["head"], hb["tail"]
    hl = float(np.linalg.norm(h1 - h0))
    headw = W[:, col["head"]] + (W[:, col["jaw"]] if "jaw" in col else 0.0)
    hv = V[headw > 0.5]
    if len(hv) < 4:
        d, _ = seg_dist(V, h0, h1)
        hv = V[d < 0.5 * hl + 1e-6]
    if len(hv):
        top = hv[int(np.argmax(hv[:, 2]))]
        mid = 0.5 * (h0 + h1)
        put("socket_fx_head", "head", (mid[0], top[1], top[2]), "fx")
        near = hv[np.abs(hv[:, 0] - mid[0]) < max(0.3 * hl, 2 * A["cell"])]
        near = near if len(near) else hv
        nose = near[int(np.argmin(near[:, 1]))].copy()
        if "mouth" in lay.marks:
            jt = np.asarray(lay.marks["mouth"]["tip"])
            nose = 0.5 * (nose + jt)                   # between the upper jaw tip and the lower jaw tip
        nose[1] += 0.03 * hl
        # with a jaw the socket rides on it, so breath/bite FX follow the opening mouth
        put("socket_fx_mouth", "jaw" if "jaw" in col else "head", nose, "fx")
    if template != "serpent":
        sp = lay.marks.get("spine_bones", [])
        bone = sp[len(sp) // 2] if sp else "pelvis"
        b = lay.bones[bone]
        y0 = float(0.5 * (b["head"][1] + b["tail"][1]))
        # top profile of the back near the midline; the seat is its lowest point (saddle dip)
        mid = np.abs(V[:, 0]) < 0.035 * span
        best = None
        for yb in np.linspace(y0 - 0.06 * span, y0 + 0.06 * span, 13):
            m = mid & (np.abs(V[:, 1] - yb) < 0.006 * span + 0.5 * A["cell"])
            if m.sum() < 2:
                continue
            zt = float(V[m, 2].max())
            if best is None or zt < best[1]:
                best = (float(yb), zt)
        if best is None:
            best = (y0, float(b["head"][2]))
        put("socket_mount_back", bone, (0.0, best[0], best[1]), "mount")
    tails = [n for n in lay.order if n.startswith("tail_")]
    if tails:
        put("socket_fx_tail", tails[-1], lay.bones[tails[-1]]["tail"], "fx")
    return out


# =========================================================================== previews

SIDE_COL = {"l": (0.95, 0.3, 0.2, 1.0), "r": (0.2, 0.5, 1.0, 1.0), None: (1.0, 0.82, 0.15, 1.0)}


def _bone_mesh(name, h, t, width, col):
    h, t = Vector(h), Vector(t)
    d = t - h
    L = d.length
    if L < 1e-6:
        return None
    y = d / L
    x = y.orthogonal().normalized()
    z = y.cross(x)
    c = h + d * 0.12
    w = width
    verts = [h, c + x * w, c + z * w, c - x * w, c - z * w, t]
    faces = [(0, 1, 2), (0, 2, 3), (0, 3, 4), (0, 4, 1), (5, 2, 1), (5, 3, 2), (5, 4, 3), (5, 1, 4)]
    me = bpy.data.meshes.new(name)
    me.from_pydata([tuple(v) for v in verts], [], faces)
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    mat = bpy.data.materials.new(name)
    mat.diffuse_color = col
    me.materials.append(mat)
    return ob


def _load_px(path):
    img = bpy.data.images.load(path, check_existing=False)
    px = np.array(img.pixels[:], dtype=np.float32).reshape(img.size[1], img.size[0], 4)
    bpy.data.images.remove(img)
    return px


def save_png(path, px):
    out = bpy.data.images.new("af_png", width=px.shape[1], height=px.shape[0], alpha=True)
    out.pixels = np.ascontiguousarray(px, dtype=np.float32).ravel()
    out.filepath_raw = path
    out.file_format = "PNG"
    out.save()
    bpy.data.images.remove(out)
    return path


def over(top, bottom, fade=1.0):
    a_t = top[..., 3:4]
    a_b = bottom[..., 3:4] * fade
    a = a_t + a_b * (1 - a_t)
    rgb = (top[..., :3] * a_t + bottom[..., :3] * a_b * (1 - a_t)) / np.maximum(a, 1e-6)
    return np.concatenate([rgb, a], -1)


def render_rig_preview(path, ob, arm, socket_objs, views=("front", "left", "top", "three_quarter"), size=384):
    import os
    span = max(ob.dimensions)
    temp = []
    for b in arm.data.bones:
        if not b.use_deform:
            continue
        side = "l" if b.name.endswith(("_l",)) or "_l_" in b.name else ("r" if b.name.endswith("_r") or "_r_" in b.name else None)
        w = float(np.clip(0.09 * b.length, 0.004 * span, 0.03 * span))
        o = _bone_mesh("afb_" + b.name, arm.matrix_world @ b.head_local, arm.matrix_world @ b.tail_local, w,
                       SIDE_COL[side])
        if o:
            temp.append(o)
    for s in socket_objs:
        bpy.ops.mesh.primitive_uv_sphere_add(radius=0.018 * span, location=s.matrix_world.translation,
                                             segments=12, ring_count=6)
        sp = bpy.context.active_object
        m = bpy.data.materials.new("afs")
        m.diffuse_color = (0.1, 0.9, 0.3, 1.0)
        sp.data.materials.append(m)
        temp.append(sp)
    base = os.path.splitext(path)[0]
    for o in temp:
        o.hide_render = True
    p_mesh = af.render_views(base + "_m.png", [ob], views=views, size=size)
    for o in temp:
        o.hide_render = False
    ob.hide_render = True
    p_bone = af.render_views(base + "_b.png", [ob], views=views, size=size)
    ob.hide_render = False
    px = over(_load_px(p_bone), _load_px(p_mesh), fade=0.5)
    save_png(path, px)
    for o in temp:
        me = o.data
        bpy.data.objects.remove(o, do_unlink=True)
        if me and me.users == 0:
            bpy.data.meshes.remove(me)
    for p in (p_mesh, p_bone):
        try:
            os.remove(p)
        except OSError:
            pass
    _drop_views_dir(path)
    return path


def _drop_views_dir(path):
    """af_common.render_views leaves its per-view tiles in <dir>/_views; remove that folder."""
    import os
    import shutil
    shutil.rmtree(os.path.join(os.path.dirname(os.path.abspath(path)), "_views"), ignore_errors=True)


# =========================================================================== rig metadata

def rig_meta(lay, A, template, extra):
    bones = []
    for n in lay.order:
        b = lay.bones[n]
        bones.append({"name": n, "parent": b["parent"], "head": r3(b["head"]), "tail": r3(b["tail"]),
                      "deform": b["deform"], "kind": b["kind"], "side": b["side"]})
    chains = [{"id": c["id"], "kind": c["kind"], "side": c.get("side"), "bones": c["bones"],
               "leg_kind": c.get("leg_kind"), "pos": c.get("pos")} for c in lay.chains]
    meta = {"template": template, "bones": bones, "chains": chains, "legs": lay.legs,
            "wings": lay.wings,
            "marks": _jsonable({k: v for k, v in lay.marks.items() if not k.startswith("_")}),
            "size": {"length_m": round(A["L"], 4), "height_m": round(A["H"], 4), "width_m": round(A["W"], 4)},
            "confidence": lay.conf}
    meta.update(extra)
    return meta


def _jsonable(v):
    if isinstance(v, dict):
        return {k: _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, np.ndarray):
        return [round(float(x), 5) for x in v.ravel()] if v.ndim == 1 else v.tolist()
    if isinstance(v, (np.floating, float)):
        return round(float(v), 5)
    if isinstance(v, np.integer):
        return int(v)
    return v


def overall_confidence(conf):
    nums = [float(v) for v in conf.values() if isinstance(v, (int, float))]
    if not nums:
        return 0.0
    return round(0.5 * min(nums) + 0.5 * float(np.mean(nums)), 3)


# =========================================================================== animation: rig model

AX = {"X": Vector((1, 0, 0)), "Y": Vector((0, 1, 0)), "Z": Vector((0, 0, 1))}


def qrot(rots):
    """Compose [(axis, degrees), ...] (armature axes) into one quaternion, first item applied first."""
    q = Quaternion()
    for axis, deg in rots:
        if deg:
            a = AX[axis] if isinstance(axis, str) else Vector(axis)
            q = Quaternion(a, math.radians(deg)) @ q
    return q


def bump(s):
    s = min(max(s, 0.0), 1.0)
    return 16.0 * s * s * (1 - s) * (1 - s)


def ease(x):
    x = min(max(x, 0.0), 1.0)
    return x * x * (3 - 2 * x)


def keys(t, kv):
    """Piecewise eased interpolation through [(t, value), ...] (values: float)."""
    if t <= kv[0][0]:
        return kv[0][1]
    for (t0, v0), (t1, v1) in zip(kv[:-1], kv[1:]):
        if t <= t1:
            return v0 + (v1 - v0) * ease((t - t0) / max(t1 - t0, 1e-9))
    return kv[-1][1]


def wing_dir(elev, sweep, sg):
    e, s = math.radians(elev), math.radians(sweep)
    return Vector((sg * math.cos(e) * math.cos(s), math.cos(e) * math.sin(s), math.sin(e))).normalized()


def two_bone(A, T, a, b, pole):
    d_vec = T - A
    d = d_vec.length
    dmax = (a + b) * 0.9995
    dmin = abs(a - b) * 1.0005 + 1e-6
    dc = min(max(d, dmin), dmax)
    u = d_vec / d if d > 1e-9 else Vector((0, 0, -1))
    v = pole - u * pole.dot(u)
    if v.length < 1e-6:
        v = u.orthogonal()
    v.normalize()
    ca = (a * a + dc * dc - b * b) / (2 * a * dc)
    ca = min(max(ca, -1.0), 1.0)
    sa = math.sqrt(max(0.0, 1 - ca * ca))
    J = A + (u * ca + v * sa) * a
    Te = A + u * dc
    return J, Te, (T - Te).length, d / (a + b)


def aim(M0, direction):
    d0 = Vector(M0.col[1][:3]).normalized()
    q = d0.rotation_difference(direction.normalized())
    R = q.to_matrix() @ M0.to_3x3()
    Md = R.to_4x4()
    Md.translation = M0.translation
    return Md


def rot_about(M, q, p):
    return Matrix.Translation(p) @ q.to_matrix().to_4x4() @ Matrix.Translation(-p) @ M


class Rig:
    """Rest data + an exact FK/IK pose solver mirroring Blender's bone evaluation."""

    def __init__(self, arm, ob):
        self.arm, self.ob = arm, ob
        self.meta = json.loads(arm["af_rig"])
        self.template = self.meta["template"]
        bones = arm.data.bones
        order = []

        def visit(b):
            order.append(b.name)
            for c in b.children:
                visit(c)
        for b in bones:
            if b.parent is None:
                visit(b)
        self.order = order
        self.rest = {b.name: b.matrix_local.copy() for b in bones}
        self.rest_inv = {n: m.inverted() for n, m in self.rest.items()}
        self.rq = {n: m.to_quaternion() for n, m in self.rest.items()}
        self.parent = {b.name: (b.parent.name if b.parent else None) for b in bones}
        self.length = {b.name: b.length for b in bones}
        self.lrest = {n: (self.rest_inv[p] @ self.rest[n] if p else self.rest[n].copy())
                      for n, p in self.parent.items()}
        self.head = {b.name: Vector(b.head_local) for b in bones}
        self.tail = {b.name: Vector(b.tail_local) for b in bones}
        chains = {c["id"]: c for c in self.meta["chains"]}
        self.main = chains.get("main", {}).get("bones", [])
        self.spine = [n for n in self.main if n == "pelvis" or n.startswith("spine_")]
        self.neck = [n for n in self.main if n.startswith("neck_")]
        self.tailb = [n for n in self.order if n.startswith("tail_")]
        self.has_jaw = "jaw" in self.rest
        size = self.meta["size"]
        self.Lb, self.H = size["length_m"], size["height_m"]
        self.hw = self.meta.get("analysis", {}).get("torso_halfwidth_m", 0.15 * size["width_m"])
        self.legs = []
        for lg in self.meta["legs"]:
            n1, n2, n3, n4 = lg["bones"]
            R, K1, K2, K3, TT = self.head[n1], self.tail[n1], self.tail[n2], self.tail[n3], self.tail[n4]
            kind = lg["kind"]
            if kind == "insect":
                line = (K2 - R).normalized()
                pole = (K1 - R) - line * (K1 - R).dot(line)
                default = Vector((0, 0, 1))
            else:
                line = (K3 - K1).normalized()
                pole = (K2 - K1) - line * (K2 - K1).dot(line)
                default = Vector((0, -1, 0)) if (lg["pos"] == "front" and kind == "mammal") else Vector((0, 1, 0))
            pole = pole.normalized() if pole.length > 1e-5 else default
            if kind == "insect":
                height = R.z
                unit = (Vector((TT.x, TT.y, 0)) - Vector((R.x, R.y, 0))).length
            else:
                height = R.z - K3.z
                unit = height
            self.legs.append(dict(lg, R=R, K1=K1, K2=K2, K3=K3, TT=TT, pole=pole, height=height, unit=unit,
                                  a1=(K1 - R).length, a2=(K2 - K1).length, a3=(K3 - K2).length))
        self.leg_by_first = {lg["bones"][0]: lg for lg in self.legs}
        self.clear_r = self._clearances()
        self._foot_samples()
        self.wings = self.meta.get("wings", [])
        self.wing_by_first = {w["bones"][0]: w for w in self.wings}
        kinds = {lg["kind"] for lg in self.legs}
        self.leg_kind = kinds.pop() if len(kinds) == 1 else ("mammal" if self.legs else None)
        self.Lg = float(np.mean([lg["unit"] for lg in self.legs])) if self.legs else 0.25 * self.Lb
        if self.spine:
            self.pivot = 0.5 * (self.head[self.spine[0]] + self.tail[self.spine[-1]])
        else:
            self.pivot = Vector((0, 0, 0.5 * self.H))
        self.body_len = (self.tail[self.spine[-1]] - self.head[self.spine[0]]).length if self.spine else self.Lb
        self.Lz = float(np.mean([lg["height"] for lg in self.legs])) if self.legs else 0.1 * self.H
        # serpent: arc position of every main/tail bone tail measured from the head tip
        if self.template == "serpent":
            pts = [self.tail["head"]] + [self.head[n] for n in reversed(self.main)] + \
                  [self.tail[n] for n in self.tailb]
            names = ["head_tip"] + list(reversed(self.main)) + self.tailb
            s, acc = {}, 0.0
            prev = pts[0]
            for nm, p in zip(names, pts):
                acc += (p - prev).length
                prev = p
                s[nm] = acc
            self.arc = s
            self.arc_total = acc

    def _foot_samples(self):
        """Lowest vertices of the paw and lower leg bones, stored in their bone's rest frame.

        Used as swing clearance: a lifted or striking foot (and toes bound to the shin, e.g. an
        eagle's hallux) must not dip below its rest height."""
        ob = self.ob
        names = [vg.name for vg in ob.vertex_groups]
        V = W = None
        for lg in self.legs:
            lg["samples"] = []
            for bone, count in ((lg["bones"][3], 24), (lg["bones"][2], 8), (lg["bones"][1], 4)):
                if bone not in names:
                    continue
                if V is None:
                    V, _, _ = mesh_np(ob)
                    W = read_weights(ob, names)
                sel = np.nonzero(W[:, names.index(bone)] > 0.35)[0]
                if not len(sel):
                    continue
                low = sel[np.argsort(V[sel, 2])[:count]]
                inv = self.rest_inv[bone]
                lg["samples"] += [(bone, inv @ Vector(V[i]), float(V[i][2])) for i in low]

    def _clearances(self):
        """How far the mesh hangs below the tail point of each tail/head bone at rest."""
        ob = self.ob
        names = [vg.name for vg in ob.vertex_groups]
        cand = [n for n in self.tailb + self.neck + ["head", "jaw"] if n in names]
        if not cand:
            return {}
        V, _, _ = mesh_np(ob)
        W = read_weights(ob, names)
        out = {}
        for n in cand:
            w = W[:, names.index(n)]
            sel = w > 0.3
            if not sel.any():
                continue
            tp = np.array(self.tail[n])
            near = sel & (np.linalg.norm(V - tp, axis=1) < 0.6 * self.length[n] + 0.02 * self.H)
            if not near.any():
                near = sel
            out[n] = max(0.0, float(tp[2] - V[near, 2].min())) + 0.01 * self.H
        return out

    def _clear(self, n, M0, B, M):
        """Rotate bone n about its head (world) so its tail point stays above the ground clearance."""
        need = self.clear_r.get(n)
        if need is None:
            return
        for _ in range(3):
            Mn = M0 @ B[n]
            h = Mn.translation
            t = Mn @ Vector((0, self.length[n], 0))
            dz = need - t.z
            if dz <= 1e-4:
                return
            d = t - h
            axis = d.cross(UP)
            if axis.length < 1e-6:
                axis = Vector((1, 0, 0))
            axis.normalize()
            rate = axis.cross(d).z
            if abs(rate) < 1e-6:
                return
            ang = max(-0.6, min(0.6, dz / rate))
            Md = rot_about(Mn, Quaternion(axis, ang), h)
            B[n] = M0.inverted() @ Md

    # ------------------------------------------------------------------ pose
    def local_rot(self, n, q):
        r = self.rq[n]
        return (r.inverted() @ q @ r).to_matrix().to_4x4()

    def solve(self, spec):
        """spec -> (basis matrices, posed armature-space matrices, stats)."""
        B, M = {}, {}
        st = {"reach": 0.0, "ratio": 0.0}
        rot = spec.get("rot", {})
        loc = spec.get("loc", {})
        for n in self.order:
            if n in B:
                continue
            p = self.parent[n]
            Mp = M[p] if p else Matrix.Identity(4)
            M0 = Mp @ self.lrest[n]
            if n in self.leg_by_first:
                self._solve_leg(self.leg_by_first[n], spec, Mp, B, M, st)
                continue
            if n in self.wing_by_first and spec.get("wings", {}).get(self.wing_by_first[n]["side"]):
                self._solve_wing(self.wing_by_first[n], spec["wings"][self.wing_by_first[n]["side"]], Mp, B, M)
                continue
            if p is None:
                D = spec.get("root", Matrix.Identity(4))
                B[n] = self.rest_inv[n] @ D @ self.rest[n]
            else:
                Bq = self.local_rot(n, qrot(rot.get(n, [])))
                if n in loc:
                    Bq = Matrix.Translation(self.rq[n].inverted() @ Vector(loc[n])) @ Bq
                if n == "pelvis" and "body" in spec:
                    bd = spec["body"]
                    C = self.pivot
                    D = (Matrix.Translation(C + Vector(bd.get("off", (0, 0, 0))))
                         @ qrot(bd.get("rot", [])).to_matrix().to_4x4() @ Matrix.Translation(-C))
                    Bq = M0.inverted() @ D @ M0 @ Bq
                B[n] = Bq
            if "aim" in spec and n in spec["aim"]:
                Md = aim(M0 @ B[n], spec["aim"][n] - (M0 @ B[n]).translation)
                B[n] = M0.inverted() @ Md
            if spec.get("clear_ground", True) and n in self.clear_r:
                self._clear(n, M0, B, M)
            M[n] = M0 @ B[n]
        return B, M, st

    def _fk_chain(self, names, rots, Mp, B, M):
        for n in names:
            p = self.parent[n]
            M0 = (M[p] if p in M else Mp) @ self.lrest[n]
            B[n] = self.local_rot(n, qrot(rots.get(n, [])))
            M[n] = M0 @ B[n]

    def _solve_leg(self, lg, spec, Mp, B, M, st):
        n1, n2, n3, n4 = lg["bones"]
        fs = spec.get("feet", {}).get(lg["id"])
        rot = spec.get("rot", {})
        if fs is None or fs.get("mode") == "fk":
            r = dict(rot)
            if fs:
                r.update(fs.get("fk", {}))
            self._fk_chain([n1, n2, n3, n4], r, Mp, B, M)
            return
        par = self.parent[n1]
        Dp = Mp @ self.rest_inv[par]
        D3 = Dp.to_3x3()
        M0 = Mp @ self.lrest[n1]
        R = M0.translation
        off = Vector(fs.get("off", (0, 0, 0)))
        flex = fs.get("flex", 0.0)
        for attempt in range(2):
            if lg["kind"] == "insect":
                T = lg["TT"] + off
                At = T - (lg["TT"] - lg["K2"])
                pole = D3 @ lg["pole"]
                J, Ae, err, ratio = two_bone(R, At, lg["a1"], lg["a2"], pole)
                Md1 = aim(M0, J - R)
                M[n1], B[n1] = Md1, M0.inverted() @ Md1
                M02 = Md1 @ self.lrest[n2]
                Md2 = aim(M02, Ae - M02.translation)
                M[n2], B[n2] = Md2, M02.inverted() @ Md2
                for n in (n3, n4):
                    M0n = M[self.parent[n]] @ self.lrest[n]
                    Mdn = self.rest[n].copy()
                    Mdn.translation = M0n.translation
                    M[n], B[n] = Mdn, M0n.inverted() @ Mdn
                toe = M[n4] @ Vector((0, self.length[n4], 0))
            else:
                T = lg["K3"] + off
                K3p = Dp @ lg["K3"]
                q_full = (K3p - R).rotation_difference(T - R)
                k1 = 0.7 if lg["kind"] == "mammal" else 0.4
                q1 = Quaternion().slerp(q_full, k1)
                Md1 = rot_about(M0, q1, R)
                M[n1], B[n1] = Md1, M0.inverted() @ Md1
                K1 = Md1 @ Vector((0, self.length[n1], 0))
                pole = q1 @ (D3 @ lg["pole"])
                J, Te, err, ratio = two_bone(K1, T, lg["a2"], lg["a3"], pole)
                M02 = Md1 @ self.lrest[n2]
                Md2 = aim(M02, J - M02.translation)
                M[n2], B[n2] = Md2, M02.inverted() @ Md2
                M03 = Md2 @ self.lrest[n3]
                Md3 = aim(M03, Te - M03.translation)
                M[n3], B[n3] = Md3, M03.inverted() @ Md3
                M04 = Md3 @ self.lrest[n4]
                q = qrot([("X", flex)]) @ self.rq[n4]
                Md4 = q.to_matrix().to_4x4()
                Md4.translation = M04.translation
                M[n4], B[n4] = Md4, M04.inverted() @ Md4
                toe = M[n4] @ Vector((0, self.length[n4], 0))
            # keep the toe and the lowest paw vertices above their rest height while the foot swings
            low = lg["TT"].z - toe.z
            for bone, loc_p, z_rest in lg.get("samples", []):
                low = max(low, z_rest - (M[bone] @ loc_p).z)
            if attempt == 0 and off.z > 1e-6 and low > 1e-4:
                off = off + Vector((0, 0, low))
                continue
            break
        if fs.get("blend", 0.0) < 0.01:
            st["reach"] = max(st["reach"], err)
            st["ratio"] = max(st["ratio"], ratio)

    def _solve_wing(self, w, ws, Mp, B, M):
        names = w["bones"]
        par = self.parent[names[0]]
        D3 = (Mp @ self.rest_inv[par]).to_3x3()
        if "rot" in ws:
            self._fk_chain(names, ws["rot"], Mp, B, M)
            return
        for n, d in zip(names, ws["dirs"]):
            p = self.parent[n]
            M0 = (M[p] if p in M else Mp) @ self.lrest[n]
            Md = aim(M0, D3 @ d)
            M[n], B[n] = Md, M0.inverted() @ Md

    def rest_wing_dirs(self, side):
        for w in self.wings:
            if w["side"] == side:
                return [(self.tail[n] - self.head[n]).normalized() for n in w["bones"]]
        return None


# =========================================================================== animation: clips

LOCO = {
    "quadruped": {
        "walk": dict(duty=0.65, ph={"back_l": 0.0, "front_l": 0.25, "back_r": 0.5, "front_r": 0.75},
                     stride=0.8, lift=0.12, frames=32, bob=0.012, bobs=2, flexf=55, flexb=35, nod=2.5),
        "trot": dict(duty=0.42, ph={"back_l": 0.0, "front_r": 0.0, "back_r": 0.5, "front_l": 0.5},
                     stride=1.2, lift=0.16, frames=20, bob=0.018, bobs=2, flexf=75, flexb=45, nod=3.0),
        "gallop": dict(duty=0.36, ph={"back_l": 0.0, "back_r": 0.1, "front_l": 0.45, "front_r": 0.55},
                       stride=1.35, lift=0.18, frames=18, bob=0.02, bobs=1, flexf=85, flexb=50, nod=4.0,
                       spine_flex=2.5),
    },
    "hexapod": {
        "walk": dict(duty=0.55, ph={"front_l": 0.0, "mid_r": 0.0, "back_l": 0.0, "front_r": 0.5, "mid_l": 0.5,
                                    "back_r": 0.5}, stride=0.55, lift=0.35, frames=24, bob=0.01, bobs=2, nod=1.5),
        "run": dict(duty=0.5, ph={"front_l": 0.0, "mid_r": 0.0, "back_l": 0.0, "front_r": 0.5, "mid_l": 0.5,
                                  "back_r": 0.5}, stride=0.8, lift=0.4, frames=14, bob=0.015, bobs=2, nod=2.0),
    },
    "octopod": {
        "walk": dict(duty=0.75, ph={"back_l": 0.0, "mid2_l": 0.25, "mid_l": 0.5, "front_l": 0.75,
                                    "back_r": 0.5, "mid2_r": 0.75, "mid_r": 0.0, "front_r": 0.25},
                     stride=0.45, lift=0.32, frames=32, bob=0.006, bobs=4, nod=1.0),
        "run": dict(duty=0.5, ph={"front_l": 0.0, "mid_r": 0.0, "mid2_l": 0.0, "back_r": 0.0,
                                  "front_r": 0.5, "mid_l": 0.5, "mid2_r": 0.5, "back_l": 0.5},
                    stride=0.7, lift=0.38, frames=16, bob=0.012, bobs=2, nod=1.5),
    },
    "bird": {
        "walk": dict(duty=0.6, ph={"back_l": 0.0, "back_r": 0.5}, stride=0.9, lift=0.16, frames=24, bob=0.02,
                     bobs=2, flexb=40, nod=7.0),
        "run": dict(duty=0.4, ph={"back_l": 0.0, "back_r": 0.5}, stride=1.4, lift=0.2, frames=16, bob=0.03,
                    bobs=2, flexb=50, nod=5.0),
        "hop": dict(duty=0.45, ph={"back_l": 0.0, "back_r": 0.0}, stride=0.8, lift=0.12, frames=20, bob=0.0,
                    bobs=1, flexb=45, nod=4.0, hop=0.25),
    },
}
LOCO["winged_quadruped"] = LOCO["quadruped"]


class Clip:
    def __init__(self, name, loop, frames, fn, stance=None, planted=True, ground=None, info=None):
        self.name, self.loop, self.frames, self.fn = name, loop, int(frames), fn
        self.stance = stance or (lambda lg, t: planted)
        self.planted = planted
        self.ground = ground
        self.info = info or {}
        self.in_place_shift = 0.0     # metres per cycle to add back when measuring ground-locked feet
        self.root_shift = 0.0         # metres per cycle the root bone travels (root-motion loops)


def _pv(P, key, clip, default):
    v = P.get(key)
    if isinstance(v, dict):
        return v.get(clip, default)
    if v is not None and clip == "walk-loop":
        return v
    return default


def foot_offset(t, phase, duty, sweep, lift, skew=1.0):
    """(offset in armature axes, swing fraction or None) of a foot over one gait cycle."""
    x = (t - phase) % 1.0
    if x < duty:
        s = x / duty
        return Vector((0.0, -sweep * (0.5 - s), 0.0)), None
    s = (x - duty) / (1.0 - duty)
    m = sweep * (1.0 - duty) / duty                  # stance speed in swing units: touch down/lift off
    p0, p1 = 0.5 * sweep, -0.5 * sweep               # with the ground speed (no slip at contact)
    h00, h10 = 2 * s ** 3 - 3 * s ** 2 + 1, s ** 3 - 2 * s ** 2 + s
    h01, h11 = -2 * s ** 3 + 3 * s ** 2, s ** 3 - s ** 2
    y = h00 * p0 + h10 * m + h01 * p1 + h11 * m
    z = lift * bump(s ** skew)
    return Vector((0.0, y, z)), s


class ClipBuilder:
    def __init__(self, rig, P):
        self.r = rig
        self.P = P
        self.fps = int(P.get("fps", 30))
        self.in_place = bool(P.get("in_place", True))
        self.side = {"l": 1.0, "r": -1.0}
        self.tempo = float(P.get("tempo", 1.0) or 1.0)          # cycle speed multiplier (UI "speed x")
        self.folded = any(w.get("folded") for w in rig.wings)  # wings fitted along the flank
        self.wing_amp = 0.3 if self.folded else 1.0
        self.skipped = {}                                        # clip -> reason it was not built
        self.notes = {}                                          # clip -> extra warning text

    def frames(self, n):
        """Cycle length after the tempo multiplier (>= 8 frames)."""
        return int(max(8, round(n / max(self.tempo, 1e-3))))

    def cycle(self, clip, default):
        """params.cycle_frames (number = walk-loop, or {clip: frames}) wins; else default / tempo."""
        v = _pv(self.P, "cycle_frames", clip, None)
        return int(v) if v else self.frames(default)

    def spec(self):
        return {"rot": {}, "loc": {}, "feet": {}, "wings": {}}

    def rot(self, s, bone, axis, deg):
        if bone in self.r.rest and deg:
            s["rot"].setdefault(bone, []).append((axis, deg))

    def planted(self, s, extra=None):
        for lg in self.r.legs:
            s["feet"][lg["id"]] = {"off": Vector((0, 0, 0)), "flex": 0.0}

    def tail_wave(self, s, t, az, ax, cycles=1, lag=0.7):
        for k, n in enumerate(self.r.tailb):
            self.rot(s, n, "Z", (az + 0.6 * az * k) * math.sin(2 * math.pi * cycles * t - lag * k))
            self.rot(s, n, "X", (ax + 0.4 * ax * k) * math.sin(4 * math.pi * cycles * t - lag * k))

    def neck_rot(self, s, axis, deg_total, lag=0.0, t=0.0):
        nk = self.r.neck
        if not nk:
            self.rot(s, "head", axis, deg_total)
            return
        for k, n in enumerate(nk):
            self.rot(s, n, axis, deg_total / len(nk))

    def wings_rot(self, s, deg_up, deg_back=0.0):
        """Raise (+) / lower wings about the body axis, sweep back (+)."""
        for w in self.r.wings:
            sg = self.side[w["side"]]
            self.rot(s, w["bones"][0], "Y", -sg * deg_up * self.wing_amp)
            self.rot(s, w["bones"][0], "Z", -sg * deg_back * self.wing_amp)

    # ------------------------------------------------------------------ clips
    def build(self, name):
        T = self.r.template
        if T == "custom" and name != "idle-loop":
            return None                        # user chains: no ground contacts known, idle sway only
        if name == "idle-loop":
            return self.idle()
        if name in ("walk-loop", "run-loop"):
            if T == "serpent":
                return self.serpent_loco(name, run=name == "run-loop")
            if T == "custom":
                return None
            if name == "walk-loop":
                gait = "walk"
            elif T in ("quadruped", "winged_quadruped"):
                gait = self.P.get("run_gait", "trot")
            else:
                gait = "run"
            if T == "bird" and name == "walk-loop" and self.P.get("bird_gait") == "hop":
                gait = "hop"
            return self.loco(name, gait)
        if name in ("fly-loop", "glide-loop"):
            if T not in WINGED or not self.r.wings:
                return None
            if self.folded:
                self.skipped[name] = ("wings are folded against the body in the rest mesh (bones fitted along "
                                      "the flank): spreading them would tear the flank; rig a spread-wing mesh")
                return None
            return self.fly(name, glide=name == "glide-loop")
        if name == "attack":
            return self.attack()
        if name == "hit":
            return self.hit()
        if name == "death":
            return self.death()
        raise ValueError("unknown clip %r" % name)

    def idle(self):
        r = self.r
        N = self.cycle("idle-loop", 72)
        Lz = r.Lz if r.legs else 0.3 * r.H

        def fn(t, crouch):
            s = self.spec()
            w = 2 * math.pi * t
            br = 0.5 - 0.5 * math.cos(2 * w)
            s["body"] = {"off": Vector((0.004 * Lz * math.sin(w), 0.006 * Lz * math.sin(w + 1.3),
                                        -crouch - 0.006 * Lz * br)), "rot": [("Y", 0.6 * math.sin(w))]}
            for k, n in enumerate(r.spine[1:]):
                if r.template != "serpent":
                    self.rot(s, n, "X", -0.5 * br)
                self.rot(s, n, "Z", (1.6 if r.template == "serpent" else 0.5) * math.sin(w - 0.5 * k))
            lying = r.template == "serpent"            # a body resting on the ground may only lift
            for k, n in enumerate(r.neck):
                nod = 2.0 * math.sin(w + 0.6 - 0.4 * k)
                self.rot(s, n, "X", (-2.0 - abs(nod)) if lying else nod)
                self.rot(s, n, "Z", 3.0 * math.sin(w - 0.35 * k))
            hx = 2.5 * math.sin(w - 0.4) + 1.2 * math.sin(2 * w)
            self.rot(s, "head", "X", -abs(hx) if lying else hx)
            self.rot(s, "head", "Z", 3.5 * math.sin(w - 0.9))
            if not lying:
                self.rot(s, "jaw", "X", 3.0 * br)
            self.tail_wave(s, t, 4.0, 0.0 if lying else 2.0)
            self.wings_rot(s, 2.0 * br)
            self.planted(s)
            return s
        return Clip("idle-loop", True, N, fn, info={"cycle_frames": N})

    def loco(self, name, gait):
        r = self.r
        g = LOCO[r.template][gait]
        N = self.cycle(name, g["frames"])
        stride = _pv(self.P, "stride_m", name, None)
        speed = _pv(self.P, "speed", name, None)
        if stride is None:
            stride = g["stride"] * r.Lg
            if speed:
                N = int(np.clip(round(stride / float(speed) * self.fps), 8, 120))
        elif speed:
            N = int(np.clip(round(float(stride) / float(speed) * self.fps), 8, 120))
        stride = float(stride)
        duty = g["duty"]
        sweep = duty * stride
        lift = g["lift"] * (r.Lz if r.leg_kind == "insect" else r.Lg)
        unit_z = r.Lz
        names = [lg["id"] for lg in r.legs]

        def phase_of(lid, i):
            return g["ph"].get(lid, (i % 2) * 0.5)

        def fn(t, crouch):
            s = self.spec()
            for i, lg in enumerate(r.legs):
                off, sw = foot_offset(t, phase_of(lg["id"], i), duty, sweep, lift,
                                      skew=0.8 if lg["pos"] == "front" else 1.0)
                flex = 0.0
                if sw is not None and lg["kind"] != "insect":
                    flex = (g.get("flexf", 40) if lg["pos"] == "front" else g.get("flexb", 35)) * bump(sw)
                if not self.in_place:
                    off = off + Vector((0, -stride * t, 0))
                s["feet"][lg["id"]] = {"off": off, "flex": flex}
            w = 2 * math.pi * t
            bob = g["bob"] * unit_z * (0.5 + 0.5 * math.cos(g["bobs"] * w))
            z = -crouch - bob
            if g.get("hop"):
                sw = ((t % 1.0) - duty) / (1 - duty) if (t % 1.0) >= duty else None
                z += g["hop"] * unit_z * (bump(sw) if sw is not None else 0.0)
            brot = [("Y", (1.5 if gait == "walk" else 1.0) * math.sin(w))]
            if g.get("spine_flex"):
                brot.append(("X", 0.5 * g["spine_flex"] * math.sin(w - 0.6)))
            s["body"] = {"off": Vector((0.0, 0.0, z)), "rot": brot}
            if not self.in_place:
                s["root"] = Matrix.Translation((0, -stride * t, 0))
            for k, n in enumerate(r.spine[1:]):
                self.rot(s, n, "Z", 1.2 * math.sin(w - 0.4 * k))
                if g.get("spine_flex"):
                    self.rot(s, n, "X", g["spine_flex"] * math.sin(w - 0.5 * k))
            nod = g.get("nod", 2.0)
            for k, n in enumerate(r.neck):
                self.rot(s, n, "X", nod / max(1, len(r.neck)) * math.sin(g["bobs"] * w - 0.8 - 0.3 * k))
            self.rot(s, "head", "X", -0.5 * nod * math.sin(g["bobs"] * w - 1.2))
            self.tail_wave(s, t, 3.0, 2.0)
            if r.template == "winged_quadruped":
                self.wings_rot(s, 2.5 * math.sin(2 * w))
            return s

        def stance(lg, t):
            i = names.index(lg["id"])
            return ((t - phase_of(lg["id"], i)) % 1.0) < duty
        c = Clip(name, True, N, fn, stance=stance,
                 info={"gait": gait, "cycle_frames": N, "stride_m": round(stride, 4), "duty": duty,
                       "speed_mps": round(stride / (N / self.fps), 4), "in_place": self.in_place})
        c.in_place_shift = stride if self.in_place else 0.0
        c.root_shift = 0.0 if self.in_place else stride
        return c

    def serpent_loco(self, name, run=False):
        r = self.r
        N = self.cycle(name, 24 if run else 40)
        A = (0.085 if run else 0.065) * r.Lb
        lam = (0.45 if run else 0.55) * r.Lb
        bones = r.main + r.tailb
        arc_tail = {}
        for i, n in enumerate(r.main):
            arc_tail[n] = r.arc[r.main[i + 1]] if i + 1 < len(r.main) else 0.0
        for n in r.tailb:
            arc_tail[n] = r.arc[n]
        s_pelvis = r.arc["pelvis"]

        def wave(sa, t):
            env = 0.2 + 0.8 * float(smoothstep(0.0, 0.3 * r.arc_total, sa))
            return A * env * math.sin(2 * math.pi * (sa / lam - t))

        # The wave travels back along the body one wavelength per cycle. Moving the root forward by the
        # same wavelength per cycle keeps the wave still in the world (slip-free lateral undulation), so
        # in_place:false translates the root by lam per cycle and aims at targets that move with it.
        shift = 0.0 if self.in_place else lam

        def fn(t, crouch):
            s = self.spec()
            s["aim"] = {}
            fwd = Vector((0.0, -shift * t, 0.0))
            if shift:
                s["root"] = Matrix.Translation(fwd)
            s["loc"]["pelvis"] = Vector((wave(s_pelvis, t), 0.0, 0.0))
            for n in bones:
                tgt = r.tail[n] + Vector((wave(arc_tail[n], t), 0.0, 0.0)) + fwd
                s["aim"][n] = tgt
            self.rot(s, "jaw", "X", 0.0)
            return s
        speed = lam / (N / self.fps)
        c = Clip(name, True, N, fn, planted=False, ground=("clear", 0.0),
                 info={"gait": "lateral_undulation", "cycle_frames": N, "wave_amplitude_m": round(A, 4),
                       "wavelength_m": round(lam, 4), "stride_m": round(lam, 4), "speed_mps": round(speed, 4),
                       "in_place": self.in_place})
        c.root_shift = shift
        return c

    def tuck(self, s):
        for lg in self.r.legs:
            n1, n2, n3, n4 = lg["bones"]
            if lg["kind"] == "insect":
                sg = self.side[lg["side"]]
                fk = {n1: [("Y", -sg * 25.0)], n2: [("Y", sg * 35.0)]}
            elif lg["pos"] == "front":
                fk = {n1: [("X", -25.0)], n2: [("X", -70.0)], n3: [("X", 110.0)], n4: [("X", 40.0)]}
            else:
                fk = {n1: [("X", 35.0)], n2: [("X", 25.0)], n3: [("X", -20.0)], n4: [("X", 30.0)]}
            s["feet"][lg["id"]] = {"mode": "fk", "fk": fk}

    def fly(self, name, glide=False):
        r = self.r
        N = self.cycle(name, 60 if glide else 24)
        hover = float(self.P.get("hover_m", 0.3 * r.H))

        def fn(t, crouch):
            s = self.spec()
            w = 2 * math.pi * t
            if glide:
                lift = 0.015 * r.H * math.sin(w)
                s["body"] = {"off": Vector((0, 0, hover + lift)), "rot": [("X", -3.0), ("Y", 4.0 * math.sin(w))]}
                e1 = 6 + 2.5 * math.sin(w)
                el = [e1, e1 - 2, e1 - 5]
                sw = [6, 10, 16]
            else:
                s["body"] = {"off": Vector((0, 0, hover - 0.035 * r.H * math.sin(w))),
                             "rot": [("X", -6.0 + 2.0 * math.cos(w))]}
                el = [12 + 42 * math.sin(w), 8 + 40 * math.sin(w - 0.6), 4 + 36 * math.sin(w - 1.2)]
                sw = [4, 10 + 22 * (0.5 + 0.5 * math.cos(w - 0.6)), 16 + 30 * (0.5 + 0.5 * math.cos(w - 1.2))]
            for wg in r.wings:
                sg = self.side[wg["side"]]
                s["wings"][wg["side"]] = {"dirs": [wing_dir(el[k], sw[k], sg) for k in range(3)]}
            self.tuck(s)
            for k, n in enumerate(r.tailb):
                self.rot(s, n, "X", -4.0 + 5.0 * math.sin(w - 0.8 * k))
                # integer frequency only: sin(w * 0.5) was not periodic over the loop (pop at the seam)
                self.rot(s, n, "Z", 2.0 * math.sin(w - 0.6 * k) if glide else 0.0)
            self.neck_rot(s, "X", -6.0)
            self.rot(s, "head", "X", 5.0 + (0 if glide else 2.0 * math.sin(w)))
            return s
        return Clip(name, True, N, fn, planted=False, ground=("clear", 0.03 * r.H),
                    info={"cycle_frames": N, "hover_m": round(hover, 4)})

    def attack_kind(self):
        a = self.P.get("attack", "auto")
        if a != "auto":
            return a
        return {"winged_quadruped": "claw", "octopod": "lunge", "serpent": "strike"}.get(self.r.template, "bite")

    def attack(self):
        r = self.r
        N = self.cycle("attack", 30)
        kind = self.attack_kind()
        Lb = r.Lb
        Lz = r.Lz if r.legs else 0.3 * r.H
        claw_leg = next((lg for lg in r.legs if lg["pos"] == "front" and lg["side"] == "l"), None)
        lunge_legs = [lg for lg in r.legs if lg["pos"] == "front"]

        def fn(t, crouch):
            s = self.spec()
            fwd = keys(t, [(0, 0), (0.35, -0.035), (0.5, 0.05), (0.62, 0.045), (1, 0)]) * r.body_len
            dz = keys(t, [(0, 0), (0.35, 0.0), (0.5, -0.03), (1, 0)]) * Lz
            pitch = keys(t, [(0, 0), (0.35, -3), (0.5, 2.5), (0.62, 2), (1, 0)])
            neck = keys(t, [(0, 0), (0.35, -18), (0.5, 22), (0.62, 18), (1, 0)])
            head = keys(t, [(0, 0), (0.35, -10), (0.5, 12), (0.62, 10), (1, 0)])
            jaw = keys(t, [(0, 0), (0.38, 26), (0.5, 30), (0.56, 0), (1, 0)])
            if kind in ("claw", "lunge"):
                pitch = keys(t, [(0, 0), (0.4, -7), (0.56, 2), (1, 0)])
                neck = keys(t, [(0, 0), (0.4, -10), (0.56, 12), (1, 0)])
            if kind == "strike":                          # coil up, then strike forward (never down)
                neck = keys(t, [(0, 0), (0.35, -34), (0.5, -10), (0.62, -12), (1, 0)])
                head = keys(t, [(0, 0), (0.35, 14), (0.5, 6), (0.62, 6), (1, 0)])
                pitch, dz = 0.0, 0.0
                for k, n in enumerate(r.spine[-3:]):
                    self.rot(s, n, "X", keys(t, [(0, 0), (0.35, -8), (0.5, -2), (1, 0)]))
            s["body"] = {"off": Vector((0, -fwd, dz - crouch)), "rot": [("X", pitch)]}
            self.neck_rot(s, "X", neck)
            self.rot(s, "head", "X", head)
            self.rot(s, "jaw", "X", jaw)
            for k, n in enumerate(r.tailb):
                self.rot(s, n, "X", keys(t, [(0, 0), (0.35, 12), (0.5, -8), (1, 0)]))
            self.wings_rot(s, keys(t, [(0, 0), (0.4, 25), (0.55, -10), (1, 0)]))
            self.planted(s)
            if kind == "claw" and claw_leg is not None:
                u = r.Lg
                y = keys(t, [(0, 0), (0.3, -0.12), (0.45, -0.22), (0.58, -0.2), (0.78, -0.07), (1, 0)]) * u
                z = keys(t, [(0, 0), (0.3, 0.35), (0.45, 0.5), (0.58, 0.025), (0.78, 0.06), (1, 0)]) * u
                fl = keys(t, [(0, 0), (0.3, 40), (0.45, 20), (0.58, 0), (1, 0)])
                s["feet"][claw_leg["id"]] = {"off": Vector((0, y, z)), "flex": fl}
            if kind == "lunge":
                for lg in lunge_legs:
                    u = lg["unit"]
                    # the horizontal return ends exactly when the foot lands (no drag on the ground)
                    y = keys(t, [(0, 0), (0.4, -0.25), (0.56, -0.3), (0.75, -0.1), (0.88, 0)]) * u
                    z = keys(t, [(0, 0), (0.4, 0.9), (0.56, 0.0), (0.72, 0.15), (0.88, 0.0)]) * r.Lz
                    s["feet"][lg["id"]] = {"off": Vector((0, y, z)), "flex": 0.0}
            return s

        lifted = set()
        if kind == "claw" and claw_leg:
            lifted.add(claw_leg["id"])
        if kind == "lunge":
            lifted |= {lg["id"] for lg in lunge_legs}
        eps = 1e-4 * max(r.H, 0.01)

        def stance(lg, t):
            # a lifted leg is in stance only while its foot target is on the ground; the old fixed
            # window (t < 0.1 or t > 0.92) counted the start of the strike itself as "sliding"
            if lg["id"] in lifted:
                return fn(t, 0.0)["feet"][lg["id"]]["off"].z <= eps
            return True
        return Clip("attack", False, N, fn, stance=stance, planted=bool(r.legs),
                    info={"kind": kind, "frames": N, "hit_frame": int(round(0.5 * N))})

    def hit(self):
        r = self.r
        N = self.cycle("hit", 16)
        Lb = r.Lb
        Lz = r.Lz if r.legs else 0.3 * r.H

        def fn(t, crouch):
            s = self.spec()
            k = keys(t, [(0, 0), (0.18, 1.0), (0.42, 0.55), (1, 0)])
            lying = r.template == "serpent"
            s["body"] = {"off": Vector((0.012 * r.body_len * k, 0.035 * r.body_len * k,
                                        0.0 if lying else (-crouch - 0.02 * Lz * k))),
                         "rot": [] if lying else [("X", -3 * k), ("Y", 4 * k)]}
            self.neck_rot(s, "X", -12 * k)
            self.neck_rot(s, "Z", 8 * k)
            self.rot(s, "head", "X", -10 * k)
            self.rot(s, "jaw", "X", 15 * k)
            for i, n in enumerate(r.tailb):
                self.rot(s, n, "X", 10 * k)
            self.wings_rot(s, 15 * k, -5 * k)
            self.planted(s)
            return s
        return Clip("hit", False, N, fn, planted=bool(r.legs), info={"frames": N})

    def death(self):
        r = self.r
        N = self.cycle("death", 48)
        side = self.P.get("death_side", "r")
        sgf = -1.0 if side == "r" else 1.0
        roll_max = 120.0 if r.template == "serpent" else 86.0
        pivot = Vector((sgf * r.hw, float(np.mean([r.head[n].y for n in r.spine])) if r.spine else 0.0, 0.0))
        Lz = r.Lz if r.legs else 0.3 * r.H
        fold = {}
        for w in r.wings:
            sg = self.side[w["side"]]
            # closed wing lying back along the body (no zig-zag: reads cleanly on any wing mesh)
            fold[w["side"]] = [Vector((sg * 0.5, 0.75, 0.43)).normalized(), Vector((sg * 0.25, 0.95, 0.15)).normalized(),
                               Vector((sg * 0.15, 0.98, 0.04)).normalized()]

        def fn(t, crouch):
            s = self.spec()
            s["clear_ground"] = t < 0.3
            roll = keys(t, [(0, 0), (0.25, 3), (0.45, 25), (0.68, roll_max), (0.76, roll_max - 5),
                            (0.86, roll_max - 1), (1, roll_max - 1)])
            s["root"] = rot_about(Matrix.Identity(4), Quaternion(Vector((0, 1, 0)), math.radians(sgf * roll)), pivot)
            sink = keys(t, [(0, 0), (0.25, 0.08), (0.5, 0.12), (1, 0.12)]) * Lz
            s["body"] = {"off": Vector((0, 0, -sink))}
            wl = keys(t, [(0, 0), (0.25, 0), (0.55, 1), (1, 1)])
            for lg in r.legs:
                n1, n2, n3, n4 = lg["bones"]
                if lg["kind"] == "insect":
                    sg = self.side[lg["side"]]
                    fk = {n1: [("Y", -sg * 25.0)], n2: [("Y", sg * 55.0)], n3: [("Y", sg * 25.0)]}
                elif lg["pos"] == "front":
                    fk = {n1: [("X", -12.0)], n2: [("X", -20.0)], n3: [("X", 25.0)], n4: [("X", 30.0)]}
                else:
                    fk = {n1: [("X", 15.0)], n2: [("X", 20.0)], n3: [("X", -25.0)], n4: [("X", 25.0)]}
                s["feet"][lg["id"]] = {"off": Vector((0, 0, 0)), "flex": 0.0, "blend": wl, "fk": fk}
            self.neck_rot(s, "X", keys(t, [(0, 0), (0.25, 14), (0.6, 6), (1, 4)]))
            self.neck_rot(s, "Z", keys(t, [(0, 0), (0.5, 0), (0.8, sgf * 22), (1, sgf * 24)]))
            self.rot(s, "head", "X", keys(t, [(0, 0), (0.3, 10), (0.8, 4), (1, 4)]))
            self.rot(s, "jaw", "X", keys(t, [(0, 0), (0.7, 0), (1, 14)]))
            for k, n in enumerate(r.tailb):
                self.rot(s, n, "X", keys(t, [(0, 0), (0.4, 8), (1, 6)]))
                self.rot(s, n, "Z", keys(t, [(0, 0), (0.7, sgf * 6), (1, sgf * 8)]))
            for k, n in enumerate(r.spine[1:]):
                self.rot(s, n, "Z", keys(t, [(0, 0), (0.7, sgf * 3), (1, sgf * 3)]))
            for w in r.wings:
                if w.get("folded"):
                    continue                           # already lying along the body
                rest = r.rest_wing_dirs(w["side"])
                wf = keys(t, [(0, 0), (0.2, 0), (0.6, 1), (1, 1)])
                s["wings"][w["side"]] = {"dirs": [rest[k].lerp(fold[w["side"]][k], wf).normalized() for k in range(3)]}
            return s
        return Clip("death", False, N, fn, planted=False, ground=("touch_end", 0.72),
                    info={"frames": N, "side": side, "roll_deg": roll_max - 1})


# =========================================================================== bake, QA, render

def solve_spec(rig, spec):
    """Rig.solve plus IK/FK blending for legs with a 'blend' weight (used by death)."""
    feet = spec.get("feet", {})
    blends = {k: v for k, v in feet.items() if v.get("blend", 0.0) > 0.0 and v.get("mode") != "fk"}
    if not blends:
        return rig.solve(spec)
    B_ik, M_ik, st = rig.solve(spec)
    spec_fk = dict(spec)
    spec_fk["feet"] = {k: (dict(v, mode="fk") if k in blends else v) for k, v in feet.items()}
    B_fk, _, _ = rig.solve(spec_fk)
    B = dict(B_ik)
    for lg in rig.legs:
        fs = blends.get(lg["id"])
        if not fs:
            continue
        w = fs["blend"]
        for n in lg["bones"]:
            q = B_ik[n].to_quaternion().slerp(B_fk[n].to_quaternion(), w)
            B[n] = q.to_matrix().to_4x4()
    M = {}
    for n in rig.order:
        p = rig.parent[n]
        M[n] = (M[p] if p else Matrix.Identity(4)) @ rig.lrest[n] @ B[n]
    return B, M, st


def auto_crouch(rig, clip, samples=24, limit=0.985):
    if not clip.planted or not rig.legs or rig.leg_kind == "insect":
        return 0.0, 0.0
    def worst(c):
        r_ = 0.0
        for k in range(samples):
            _, _, st = solve_spec(rig, clip.fn(k / samples, c))
            r_ = max(r_, st["ratio"])
        return r_
    r0 = worst(0.0)
    if r0 <= limit:
        return 0.0, r0
    lo_c, hi_c = 0.0, 0.35 * rig.Lz
    if worst(hi_c) > limit:
        return hi_c, worst(hi_c)
    for _ in range(12):
        mid = 0.5 * (lo_c + hi_c)
        if worst(mid) > limit:
            lo_c = mid
        else:
            hi_c = mid
    return hi_c, worst(hi_c)


def fcurve_set(act, arm, path, index, group, frames, values):
    fc = act.fcurve_ensure_for_datablock(arm, path, index=index, group_name=group)
    fc.keyframe_points.clear()
    fc.keyframe_points.add(len(frames))
    fc.keyframe_points.foreach_set("co", [c for f, v in zip(frames, values) for c in (float(f), float(v))])
    for kp in fc.keyframe_points:
        kp.interpolation = "LINEAR"
    fc.update()
    return fc


def fcurves_of(action):
    out = []
    for layer in action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                out.extend(bag.fcurves)
    return out


def assign_action(arm, act):
    ad = arm.animation_data or arm.animation_data_create()
    ad.action = act
    if act is not None and getattr(ad, "action_slot", None) is None and len(act.slots):
        ad.action_slot = act.slots[0]


def bake_clip(rig, clip):
    arm = rig.arm
    crouch, ratio = auto_crouch(rig, clip)
    N = clip.frames
    bases, reach = [], 0.0
    for f in range(N + 1):
        # the last key is fn(1.0), evaluated on its own (never copied from t = 0): a clip function that
        # is not periodic shows up as loop_seam_error / seam acceleration in qa_clip instead of hiding
        t = f / N
        B, M, st = solve_spec(rig, clip.fn(t, crouch))
        bases.append(B)
        reach = max(reach, st["reach"])
    act = bpy.data.actions.get(clip.name)
    if act is not None:
        bpy.data.actions.remove(act)
    act = bpy.data.actions.new(clip.name)
    act.use_fake_user = True
    assign_action(arm, act)
    frames = list(range(N + 1))
    for n in rig.order:
        qs, prev = [], None
        for B in bases:
            q = B[n].to_quaternion()
            if prev is not None and q.dot(prev) < 0:
                q.negate()
            qs.append(q)
            prev = q
        dp = 'pose.bones["%s"].rotation_quaternion' % n
        for i in range(4):
            fcurve_set(act, arm, dp, i, n, frames, [q[i] for q in qs])
        locs = [B[n].translation for B in bases]
        if n in ("root", "pelvis") or max(v.length for v in locs) > 1e-5:
            for i in range(3):
                fcurve_set(act, arm, 'pose.bones["%s"].location' % n, i, n, frames, [v[i] for v in locs])
    act.use_frame_range = True
    act.frame_start, act.frame_end = 0, N
    act.use_cyclic = clip.loop
    info = dict(clip.info, crouch_cm=round(crouch * 100, 2), ik_reach_ratio=round(ratio, 4),
                ik_reach_error_cm=round(reach * 100, 3))
    return act, info


def stash_actions(arm, acts):
    ad = arm.animation_data or arm.animation_data_create()
    ad.action = None
    for tr in list(ad.nla_tracks):
        ad.nla_tracks.remove(tr)
    for act in acts:
        tr = ad.nla_tracks.new()
        tr.name = act.name
        tr.strips.new(act.name, int(act.frame_start), act)
        tr.mute = True


def eval_verts(ob):
    dg = bpy.context.evaluated_depsgraph_get()
    e = ob.evaluated_get(dg)
    me = e.to_mesh()
    co = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", co)
    e.to_mesh_clear()
    M = np.array(ob.matrix_world)
    return co.reshape(-1, 3) @ M[:3, :3].T + M[:3, 3]


def eval_clip(rig, act, frames):
    assign_action(rig.arm, act)
    scene = bpy.context.scene
    out = []
    for f in frames:
        scene.frame_set(f)
        out.append(eval_verts(rig.ob))
    return np.array(out)


def ground_fix(rig, act, clip):
    """Lift/lower the root per frame from the deformed mesh (fly clearance, death resting on the ground)."""
    mode, val = clip.ground
    N = clip.frames
    P = eval_clip(rig, act, range(N + 1))
    minz = P[:, :, 2].min(1)
    dz = np.zeros(N + 1)
    for f in range(N + 1):
        if mode == "clear":
            dz[f] = max(0.0, val - minz[f])
        else:
            if f >= val * N:
                dz[f] = -minz[f]
            else:
                dz[f] = max(0.0, -minz[f])
    if mode == "clear":
        dz[:] = dz.max()          # constant hover keeps the loop seamless
    rq = rig.rq["root"]
    local = [rq.inverted() @ Vector((0, 0, d)) for d in dz]
    path = 'pose.bones["root"].location'
    for fc in fcurves_of(act):
        if fc.data_path == path:
            for kp in fc.keyframe_points:
                f = int(round(kp.co[0]))
                kp.co[1] += local[f][fc.array_index]
            fc.update()
    return float(dz.max()), float(dz.min())


def contact_sets(rig, V0):
    """Rest vertices touching the ground around each foot."""
    if not rig.legs:
        return {}
    z0 = V0[:, 2].min()
    zc = z0 + min(0.025 * rig.H, 0.04) + 0.003
    low = V0[:, 2] < zc
    tips = np.array([[lg["TT"].x, lg["TT"].y] for lg in rig.legs])
    if len(tips) > 1:
        dd = np.linalg.norm(tips[:, None] - tips[None], axis=-1)
        dd[dd == 0] = 1e9
        rad = 0.5 * dd.min()
    else:
        rad = 0.2 * rig.Lb
    out = {}
    for i, lg in enumerate(rig.legs):
        d = np.linalg.norm(V0[:, :2] - tips[i], axis=1)
        ids = np.nonzero(low & (d < rad))[0]
        if len(ids) < 3:
            ids = np.argsort(d + 10 * (V0[:, 2] - z0))[:8]
        out[lg["id"]] = ids
    return out


def qa_clip(rig, act, clip, V0, E, contacts):
    N = clip.frames
    frames = list(range(N + 1))
    P = eval_clip(rig, act, frames)
    res = {}
    res["ground_penetration_cm"] = round(max(0.0, -float(P[:, :, 2].min())) * 100, 3)
    if clip.loop:
        back = np.array([0.0, clip.root_shift, 0.0])      # root motion: compare after removing one stride
        # P[N] is fn(1.0) baked on its own, so this is a real measurement (it was 0 by construction)
        res["loop_seam_error_cm"] = round(float(np.linalg.norm(P[N] + back - P[0], axis=1).max()) * 100, 4)
        v0 = P[1] - P[0]
        v1 = P[N] - P[N - 1]
        steps = [float(np.linalg.norm(P[i + 1] - P[i], axis=1).max()) for i in range(N)]
        res["loop_velocity_jump_cm"] = round(float(np.linalg.norm(v0 - v1, axis=1).max()) * 100, 4)
        res["max_step_cm"] = round(max(steps) * 100, 3)
        # per-vertex second difference (acceleration per frame): the seam (frame 0 with frame N-1 as its
        # predecessor) must look like an ordinary frame, otherwise the loop pops when it wraps
        acc = [float(np.linalg.norm(P[f + 1] - 2 * P[f] + P[f - 1], axis=1).max()) for f in range(1, N)]
        seam = float(np.linalg.norm(P[1] - 2 * P[0] + (P[N - 1] + back), axis=1).max())
        res["seam_accel_cm"] = round(seam * 100, 4)
        res["interior_accel_max_cm"] = round(max(acc) * 100, 4) if acc else 0.0
    L0 = np.linalg.norm(V0[E[:, 0]] - V0[E[:, 1]], axis=1)
    ok = L0 > 1e-5
    smin, smax = 9.0, 0.0
    for f in frames[::max(1, N // 12)]:
        Lf = np.linalg.norm(P[f][E[:, 0]] - P[f][E[:, 1]], axis=1)
        k = Lf[ok] / L0[ok]
        smin, smax = min(smin, float(np.percentile(k, 0.1))), max(smax, float(np.percentile(k, 99.9)))
    res["edge_stretch_p999"] = [round(smin, 3), round(smax, 3)]
    slide = {}
    if contacts and clip.planted:
        shift = clip.in_place_shift
        for lg in rig.legs:
            ids = contacts.get(lg["id"])
            if ids is None or not len(ids):
                continue
            best = 0.0
            run = []
            for f in frames:
                t = f / N
                if clip.stance(lg, t if not (clip.loop and f == N) else 0.0):
                    c = P[f][ids].mean(0)
                    c[1] -= shift * t
                    run.append(c)
                else:
                    if len(run) > 1:
                        R = np.array(run)
                        best = max(best, float(np.linalg.norm(R[:, :2] - R[0, :2], axis=1).max()))
                    run = []
            if len(run) > 1:
                R = np.array(run)
                best = max(best, float(np.linalg.norm(R[:, :2] - R[0, :2], axis=1).max()))
            slide[lg["id"]] = round(best * 100, 3)
        res["foot_slide_cm"] = max(slide.values()) if slide else 0.0
        res["foot_slide_cm_per_leg"] = slide
    else:
        res["foot_slide_cm"] = None
    res["_bbox"] = (P.reshape(-1, 3).min(0), P.reshape(-1, 3).max(0))
    return res


def pose_frames(clip):
    N = clip.frames
    if clip.loop:
        return [int(round(N * k / 4)) for k in range(4)]
    if clip.name == "death":
        return [0, int(0.3 * N), int(0.55 * N), int(0.8 * N), N]
    return [0, int(0.35 * N), int(0.5 * N), int(0.7 * N), N]


def render_pose_sheet(path, rig, acts_clips, bbox, view="left", tile=256):
    """Rows = clips, columns = key frames; fixed camera framing for every tile."""
    import os
    lo, hi = np.array(bbox[0], float), np.array(bbox[1], float)
    lo[2] = min(lo[2], 0.0)
    flo, fhi = lo.copy(), hi.copy()
    collapse = {"left": 0, "right": 0, "front": 1, "back": 1, "top": 2}.get(view)
    if collapse is not None:                      # frame only the extents the camera actually sees
        c_ = 0.5 * (lo[collapse] + hi[collapse])
        flo[collapse] = fhi[collapse] = c_
    pad = 0.04 * float(np.max(fhi - flo))
    flo -= pad
    fhi += pad
    if collapse is not None:
        flo[collapse] = fhi[collapse] = 0.5 * (lo[collapse] + hi[collapse])
    me = bpy.data.meshes.new("af_frame")
    c = [(x, y, z) for x in (flo[0], fhi[0]) for y in (flo[1], fhi[1]) for z in (flo[2], fhi[2])]
    me.from_pydata(c, [], [(0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6), (0, 2, 6, 4), (1, 3, 7, 5)])
    frame_obj = bpy.data.objects.new("af_frame", me)
    bpy.context.scene.collection.objects.link(frame_obj)
    frame_obj.hide_render = True
    gm = bpy.data.meshes.new("af_ground")
    ext = 2.0 * max(hi[0] - lo[0], hi[1] - lo[1])
    th = 0.004 * max(rig.H, 0.1)
    cx, cy = 0.5 * (lo[0] + hi[0]), 0.5 * (lo[1] + hi[1])
    gv = [(cx + sx * ext, cy + sy * ext, sz) for sx in (-1, 1) for sy in (-1, 1) for sz in (-th, 0.0)]
    gm.from_pydata(gv, [], [(0, 2, 6, 4), (1, 3, 7, 5), (0, 1, 3, 2), (4, 5, 7, 6), (0, 1, 5, 4), (2, 3, 7, 6)])
    ground = bpy.data.objects.new("af_ground", gm)
    mat = bpy.data.materials.new("af_ground")
    mat.diffuse_color = (0.62, 0.63, 0.66, 1.0)
    gm.materials.append(mat)
    bpy.context.scene.collection.objects.link(ground)
    ground.hide_render = collapse is None or collapse == 2     # ground line only in side/front views
    tmp = os.path.join(os.path.dirname(path), "_poses_tmp.png")
    rows = []
    ncol = max(len(pose_frames(c)) for _, c in acts_clips)
    for act, clip in acts_clips:
        assign_action(rig.arm, act)
        tiles = []
        for f in pose_frames(clip):
            af.render_views(tmp, [frame_obj], views=(view,), size=tile, frame=f, margin=1.0)
            tiles.append(_load_px(tmp))
        while len(tiles) < ncol:
            tiles.append(np.zeros_like(tiles[0]))
        row = np.concatenate(tiles, axis=1)
        rows.append(row)
    sheet = np.concatenate(rows[::-1], axis=0)       # images are stored bottom-up
    bg = np.ones_like(sheet)
    bg[..., :3] = 0.97
    out = over(sheet, bg)
    h, w = out.shape[:2]
    for r_ in range(1, len(rows)):
        out[r_ * tile - 1:r_ * tile + 1, :, :3] = 0.75
    for c_ in range(1, ncol):
        out[:, c_ * tile - 1:c_ * tile + 1, :3] = 0.85
    save_png(path, out)
    for o in (frame_obj, ground):
        m = o.data
        bpy.data.objects.remove(o, do_unlink=True)
        bpy.data.meshes.remove(m)
    try:
        os.remove(tmp)
    except OSError:
        pass
    _drop_views_dir(tmp)
    assign_action(rig.arm, None)
    return path


def glb_animation_names(path):
    import struct
    with open(path, "rb") as fh:
        data = fh.read()
    ln = struct.unpack("<I", data[12:16])[0]
    js = json.loads(data[20:20 + ln])
    return [a.get("name") for a in js.get("animations", [])]
