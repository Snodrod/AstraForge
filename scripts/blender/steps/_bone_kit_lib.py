"""Shared helpers of the bone_kit step (skeletal parts for undead characters).

Blender 5.0.1, bundled Python 3.11 + numpy. Only the bone_kit module edits this file. It keeps its own
small geometry / image helpers instead of importing steps/_items_lib.py, so edits of the items module
cannot break it.

Geometry is built as "pieces": closed (verts, faces) meshes with a material key and an optional bone
(vertex group) name. build_mesh() joins pieces into one object with material slots, rigid vertex groups
(one group, weight 1.0, per piece), smooth/flat shading and a smart-project UV map.
"""
import math
import os
import random

import bmesh
import bpy
import mathutils
import numpy as np
from mathutils import Matrix, Vector

import af_common as af

IVORY = "#CDBE9C"
GOLD = "#C9A04A"
CRYSTAL = "#227A42"
CRYSTAL_GLOW = "#38C060"


# --------------------------------------------------------------------------- small maths

def smoothstep(a, b, x):
    if a == b:
        return 1.0 if x >= b else 0.0
    t = max(0.0, min(1.0, (x - a) / (b - a)))
    return t * t * (3 - 2 * t)


def lerp(a, b, t):
    return a + (b - a) * t


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def rot_x(deg):
    return Matrix.Rotation(math.radians(deg), 3, "X")


def rot_y(deg):
    return Matrix.Rotation(math.radians(deg), 3, "Y")


def rot_z(deg):
    return Matrix.Rotation(math.radians(deg), 3, "Z")


def frame_from(z=None, y=None, x=None):
    """3x3 rotation (columns x, y, z). Give two axes; the second is orthogonalised to the first."""
    if z is not None and y is not None:
        z = Vector(z).normalized()
        y = (Vector(y) - z * Vector(y).dot(z)).normalized()
        x = y.cross(z)
    elif y is not None and x is not None:
        y = Vector(y).normalized()
        x = (Vector(x) - y * Vector(x).dot(y)).normalized()
        z = x.cross(y)
    elif z is not None and x is not None:
        z = Vector(z).normalized()
        x = (Vector(x) - z * Vector(x).dot(z)).normalized()
        y = z.cross(x)
    else:
        raise ValueError("frame_from needs two axes")
    return Matrix((x, y, z)).transposed()


def hex_lin(c):
    """'#rrggbb' (sRGB) -> linear RGBA tuple."""
    c = c.lstrip("#")
    rgb = [int(c[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]

    def lin(v):
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return (lin(rgb[0]), lin(rgb[1]), lin(rgb[2]), 1.0)


def angdiff(a, b):
    return (a - b + math.pi) % (2 * math.pi) - math.pi


# --------------------------------------------------------------------------- pieces

class Piece:
    __slots__ = ("name", "verts", "faces", "mat", "bone", "smooth", "kind")

    def __init__(self, name, verts, faces, mat="bone", bone=None, smooth=True, kind="bone"):
        self.name = name
        self.verts = [tuple(float(c) for c in v) for v in verts]
        self.faces = [tuple(f) for f in faces]
        self.mat = mat
        self.bone = bone
        self.smooth = smooth
        self.kind = kind

    def transformed(self, M):
        M = M if len(M) == 4 else M.to_4x4()
        self.verts = [tuple(M @ Vector(v)) for v in self.verts]
        return self

    def tris(self):
        return sum(len(f) - 2 for f in self.faces)


def loft(rings, tip0=None, tip1=None, cap0=False, cap1=False):
    """Closed rings of equal length -> quads; optional pole points (fans) or n-gon caps at the ends."""
    n = len(rings[0])
    verts = [tuple(p) for r in rings for p in r]
    faces = []
    for r in range(len(rings) - 1):
        for i in range(n):
            a, b = r * n + i, r * n + (i + 1) % n
            faces.append((a, b, b + n, a + n))
    if tip0 is not None:
        c = len(verts)
        verts.append(tuple(tip0))
        for i in range(n):
            faces.append((c, (i + 1) % n, i))
    elif cap0:
        faces.append(tuple(reversed(range(n))))
    last = (len(rings) - 1) * n
    if tip1 is not None:
        c = len(verts)
        verts.append(tuple(tip1))
        for i in range(n):
            faces.append((last + i, last + (i + 1) % n, c))
    elif cap1:
        faces.append(tuple(range(last, last + n)))
    return verts, faces


def section(n, w, d, lobe=0.0, flat=0.0, tri=0.0, sq=0.0, ridge=0.0, ridge_at=-math.pi / 2,
            phase=0.0, tri_phase=0.0):
    """2D cross-section (a, b) with a = width axis, b = depth axis (positive b = dorsal / front).
    lobe: cos(2t) bilobe across a (condyles, notch on the b axis when > 0.2); flat: flattening of the
    b < 0 side (palmar flat of phalanges); tri: cos(3t) triangular term (tibia crest); sq: cos(4t)
    squaring; ridge: a gaussian bump at angle ridge_at (linea aspera, tuberosity)."""
    pts = []
    for k in range(n):
        t = phase + 2 * math.pi * k / n
        c, s = math.cos(t), math.sin(t)
        f = 1.0 + lobe * math.cos(2 * t) + tri * math.cos(3 * (t - tri_phase)) + sq * math.cos(4 * t)
        if ridge:
            f += ridge * math.exp(-(angdiff(t, ridge_at) / 0.42) ** 2)
        a = w * c * f
        b = d * s * f
        if s < 0:
            b *= (1.0 - flat)
        pts.append((a, b))
    return pts


def station_rings(stations, origin, A, Y, B, n, phase=0.0):
    """Rings along the axis Y from stations: dicts with y (m along Y), w, d (half width along A /
    half depth along B, m), optional lobe, flat, tri, sq, ridge, ridge_at, ca / cb (centre offsets)."""
    O = Vector(origin)
    rings = []
    for st in stations:
        sec = section(n, st["w"], st["d"], st.get("lobe", 0.0), st.get("flat", 0.0), st.get("tri", 0.0),
                      st.get("sq", 0.0), st.get("ridge", 0.0), st.get("ridge_at", -math.pi / 2), phase,
                      st.get("tri_phase", 0.0))
        c = O + Y * st["y"] + A * st.get("ca", 0.0) + B * st.get("cb", 0.0)
        rings.append([tuple(c + A * a + B * b) for (a, b) in sec])
    return rings


def bone_from_stations(stations, origin, A, Y, B, n, pole0=None, pole1=None, phase=0.0):
    """stations sorted by y. pole0 / pole1: (y, ca, cb) of the end points (rounded ends) or None
    (flat n-gon cap)."""
    rings = station_rings(stations, origin, A, Y, B, n, phase)
    O = Vector(origin)
    t0 = (O + Y * pole0[0] + A * pole0[1] + B * pole0[2]) if pole0 is not None else None
    t1 = (O + Y * pole1[0] + A * pole1[1] + B * pole1[2]) if pole1 is not None else None
    return loft(rings, t0, t1, cap0=t0 is None, cap1=t1 is None)


def transport_frames(path, up_hint=None):
    P = [Vector(p) for p in path]
    n = len(P)
    T = []
    for i in range(n):
        t = (P[1] - P[0]) if i == 0 else (P[-1] - P[-2]) if i == n - 1 else (P[i + 1] - P[i - 1])
        T.append(t.normalized() if t.length > 1e-12 else Vector((0, 0, 1)))
    hint = Vector(up_hint) if up_hint is not None else Vector((0, 0, 1))
    if abs(hint.normalized().dot(T[0])) > 0.95:
        hint = Vector((1, 0, 0)) if abs(T[0].x) < 0.9 else Vector((0, 1, 0))
    N = [(hint - T[0] * hint.dot(T[0])).normalized()]
    for i in range(1, n):
        v = T[i - 1].rotation_difference(T[i]) @ N[-1]
        N.append((v - T[i] * v.dot(T[i])).normalized())
    Bn = [T[i].cross(N[i]) for i in range(n)]
    return T, N, Bn


def sweep_sections(path, secfun, n, up_hint=None, tip0=None, tip1=None):
    """Tube along a 3D path; secfun(i, u) -> dict(w, d, lobe, flat, ...) gives the section of ring i
    (a = path normal, b = binormal). tip0 / tip1: pole points (rounded or pointed ends)."""
    T, N, Bn = transport_frames(path, up_hint)
    m = len(path)
    rings = []
    for i, p in enumerate(path):
        u = i / max(1, m - 1)
        st = secfun(i, u)
        sec = section(n, st["w"], st["d"], st.get("lobe", 0.0), st.get("flat", 0.0), st.get("tri", 0.0),
                      st.get("sq", 0.0), st.get("ridge", 0.0), st.get("ridge_at", -math.pi / 2),
                      st.get("phase", 0.0))
        c = Vector(p)
        rings.append([tuple(c + N[i] * a + Bn[i] * b) for (a, b) in sec])
    return loft(rings, tip0, tip1, cap0=tip0 is None, cap1=tip1 is None)


def ellipsoid(centre, radii, R=None, seg=8, rings=5, jitter=0.0, rng=None):
    """Closed low-poly ellipsoid (lathe with poles) rotated by R (3x3)."""
    rng = rng or random.Random(0)
    R = R if R is not None else Matrix.Identity(3)
    rx, ry, rz = radii
    rws = []
    for j in range(1, rings):
        ph = math.pi * j / rings
        ring = []
        for i in range(seg):
            t = 2 * math.pi * i / seg + (0.5 * math.pi / seg if j % 2 else 0.0)
            k = 1.0 + jitter * (rng.random() - 0.5)
            p = Vector((rx * math.sin(ph) * math.cos(t) * k, ry * math.sin(ph) * math.sin(t) * k,
                        -rz * math.cos(ph)))
            ring.append(tuple(Vector(centre) + R @ p))
        rws.append(ring)
    t0 = Vector(centre) + R @ Vector((0, 0, -rz))
    t1 = Vector(centre) + R @ Vector((0, 0, rz))
    return loft(rws, t0, t1)


def band(centre, axis, r_in, r_out, width, seg=8, bead=0.25, up_hint=None):
    """Closed ring (gold band) around `axis`: 4-point profile (inner, outer edge, bead, outer edge)."""
    ax = Vector(axis).normalized()
    hint = Vector(up_hint) if up_hint is not None else (Vector((0, 0, 1)) if abs(ax.z) < 0.9 else Vector((1, 0, 0)))
    u = (hint - ax * hint.dot(ax)).normalized()
    v = ax.cross(u)
    prof = [(r_in, -width / 2), (r_out, -width / 2)] + \
        ([(r_out + (r_out - r_in) * bead, 0.0)] if bead else []) + [(r_out, width / 2), (r_in, width / 2)]
    # profile is a closed loop (inner side joins back): rings around the axis for each profile point
    rings = []
    C = Vector(centre)
    for (r, h) in prof:
        rings.append([tuple(C + ax * h + (u * math.cos(2 * math.pi * i / seg) + v * math.sin(2 * math.pi * i / seg)) * r)
                      for i in range(seg)])
    verts = [p for r in rings for p in r]
    faces = []
    m = len(rings)
    for k in range(m):
        k2 = (k + 1) % m
        for i in range(seg):
            a, b = k * seg + i, k * seg + (i + 1) % seg
            c, d = k2 * seg + (i + 1) % seg, k2 * seg + i
            faces.append((a, b, c, d))
    return verts, faces


def planar_loop(path, normal, r_tube, seg=6):
    """Closed tube along a closed planar path (points not repeating the first) lying in the plane
    with the given normal: the binormal is the plane normal, so the frames never drift."""
    nrm = Vector(normal).normalized()
    m = len(path)
    P = [Vector(p) for p in path]
    rings = []
    for i in range(m):
        t = (P[(i + 1) % m] - P[i - 1]).normalized()
        nn = nrm.cross(t).normalized()
        rings.append([tuple(P[i] + (nn * math.cos(2 * math.pi * k / seg) + nrm * math.sin(2 * math.pi * k / seg)) * r_tube)
                      for k in range(seg)])
    verts = [p for r in rings for p in r]
    faces = []
    for i in range(m):
        i2 = (i + 1) % m
        for k in range(seg):
            faces.append((i * seg + k, i * seg + (k + 1) % seg, i2 * seg + (k + 1) % seg, i2 * seg + k))
    return verts, faces


# --------------------------------------------------------------------------- materials

def principled(name, color, metallic=0.0, roughness=0.5, emission=0.0, emission_color=None, vertex_color=None):
    """glTF-exportable Principled BSDF material. vertex_color: name of a colour attribute linked
    straight into Base Color (exports as COLOR_0 with baseColorFactor 1)."""
    m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    bsdf = next((nd for nd in nt.nodes if nd.type == "BSDF_PRINCIPLED"), None)
    if bsdf is None:
        bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
    out = next((nd for nd in nt.nodes if nd.type == "OUTPUT_MATERIAL"), None)
    if out is None:
        out = nt.nodes.new("ShaderNodeOutputMaterial")
    if not out.inputs["Surface"].is_linked:
        nt.links.new(bsdf.outputs["BSDF"], out.inputs["Surface"])
    rgba = hex_lin(color) if isinstance(color, str) else tuple(color)
    bsdf.inputs["Base Color"].default_value = rgba
    bsdf.inputs["Metallic"].default_value = float(metallic)
    bsdf.inputs["Roughness"].default_value = float(roughness)
    if emission:
        ec = hex_lin(emission_color) if isinstance(emission_color, str) else (emission_color or rgba)
        bsdf.inputs["Emission Color"].default_value = ec
        bsdf.inputs["Emission Strength"].default_value = float(emission)
    if vertex_color:
        vc = next((nd for nd in nt.nodes if nd.type == "VERTEX_COLOR"), None) or nt.nodes.new("ShaderNodeVertexColor")
        vc.layer_name = vertex_color
        nt.links.new(vc.outputs["Color"], bsdf.inputs["Base Color"])
    m.diffuse_color = rgba
    m.metallic = min(float(metallic), 0.4)  # Workbench preview only (full metal renders near black)
    m.roughness = max(float(roughness), 0.25)
    return m


class Materials:
    """Material per key: bone / gold / crystal (+ dark, used by nothing exported)."""

    def __init__(self, style="clean", overrides=None):
        self.style = style
        ov = overrides or {}
        self.spec = {
            "bone": dict(name="Bone" if style == "clean" else "Bone_aged", color=ov.get("bone", IVORY),
                         metallic=0.0, roughness=0.7),
            "gold": dict(name="Gold", color=ov.get("gold", GOLD), metallic=1.0, roughness=0.32),
            "crystal": dict(name="Crystal", color=ov.get("crystal", CRYSTAL), metallic=0.0, roughness=0.18,
                            emission=float(ov.get("crystal_emission", 0.4)),
                            emission_color=ov.get("crystal_glow", CRYSTAL_GLOW)),
        }
        self.cache = {}

    def __call__(self, key):
        if key in self.cache:
            return self.cache[key]
        sp = dict(self.spec[key])
        name = sp.pop("name")
        if key == "bone" and self.style == "aged":
            sp["vertex_color"] = "Color"
        m = principled(name, **sp)
        m["af_role"] = key
        self.cache[key] = m
        return m


def principled_ok(mat):
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return False
    for nd in mat.node_tree.nodes:
        if nd.type == "OUTPUT_MATERIAL":
            s = nd.inputs.get("Surface")
            if s and s.is_linked and s.links[0].from_node.type == "BSDF_PRINCIPLED":
                return True
    return False


def material_record(m):
    b = next(nd for nd in m.node_tree.nodes if nd.type == "BSDF_PRINCIPLED")
    return {"name": m.name, "role": m.get("af_role"), "principled_to_output": principled_ok(m),
            "base_color_linear": [round(x, 4) for x in b.inputs["Base Color"].default_value[:3]],
            "base_color_from_vertex_color": b.inputs["Base Color"].is_linked,
            "metallic": round(b.inputs["Metallic"].default_value, 3),
            "roughness": round(b.inputs["Roughness"].default_value, 3),
            "emission_strength": round(b.inputs["Emission Strength"].default_value, 3),
            "emission_color_linear": [round(x, 4) for x in b.inputs["Emission Color"].default_value[:3]]}


# --------------------------------------------------------------------------- pieces -> object

def build_mesh(name, pieces, mats, sharp_deg=55.0, uv=True):
    """Join pieces into one mesh object. Returns (object, piece_records). Each record keeps the
    vertex index range of its piece, its bone and its material."""
    V, F, fmat, fsmooth = [], [], [], []
    keys = []
    records = []
    for pc in pieces:
        o = len(V)
        V.extend(pc.verts)
        if pc.mat not in keys:
            keys.append(pc.mat)
        mi = keys.index(pc.mat)
        for f in pc.faces:
            F.append(tuple(i + o for i in f))
            fmat.append(mi)
            fsmooth.append(pc.smooth)
        records.append({"name": pc.name, "bone": pc.bone, "material": pc.mat, "kind": pc.kind,
                        "v0": o, "v1": len(V), "tris": pc.tris()})
    me = bpy.data.meshes.new(name)
    me.from_pydata(V, [], F)
    me.validate(clean_customdata=False)
    me.update()
    if len(me.vertices) != len(V):
        raise RuntimeError("mesh validation changed the vertex count (%d -> %d)" % (len(V), len(me.vertices)))
    bm = bmesh.new()
    bm.from_mesh(me)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(me)
    bm.free()
    for k in keys:
        me.materials.append(mats(k))
    me.polygons.foreach_set("material_index", fmat)
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    if sharp_deg:
        me.set_sharp_from_angle(angle=math.radians(sharp_deg))  # also resets every face to smooth
    me.polygons.foreach_set("use_smooth", fsmooth)              # flat pieces (crystal facets) stay flat
    # rigid vertex groups
    groups = {}
    for rec in records:
        if rec["bone"]:
            g = groups.get(rec["bone"]) or ob.vertex_groups.new(name=rec["bone"])
            groups[rec["bone"]] = g
            g.add(list(range(rec["v0"], rec["v1"])), 1.0, "REPLACE")
    if uv:
        unwrap(ob)
    return ob, records


def unwrap(ob, margin=0.006):
    af.select_only([ob])
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.smart_project(angle_limit=math.radians(66), island_margin=margin, area_weight=0.0,
                             correct_aspect=True, scale_to_bounds=False)
    bpy.ops.object.mode_set(mode="OBJECT")
    if ob.data.uv_layers:
        ob.data.uv_layers[0].name = "UVMap"


def mesh_arrays(ob, world=True):
    me = ob.data
    nv = len(me.vertices)
    co = np.empty(nv * 3)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    me.calc_loop_triangles()
    tri = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("vertices", tri)
    tri = tri.reshape(-1, 3)
    if world:
        M = np.array(ob.matrix_world)
        co = co @ M[:3, :3].T + M[:3, 3]
    return co, tri


def piece_overlaps(ob, records, skip=None, limit=40):
    """Pairs of pieces whose triangles intersect (BVH overlap). skip(a, b) -> True ignores a pair."""
    from mathutils.bvhtree import BVHTree
    co, tri = mesh_arrays(ob, world=False)
    trees, boxes = [], []
    for rec in records:
        m = (tri[:, 0] >= rec["v0"]) & (tri[:, 0] < rec["v1"])
        t = tri[m]
        trees.append(BVHTree.FromPolygons(co.tolist(), t.tolist(), all_triangles=True) if len(t) else None)
        pts = co[rec["v0"]:rec["v1"]]
        boxes.append((pts.min(0), pts.max(0)))
    out = []
    for i in range(len(records)):
        for j in range(i + 1, len(records)):
            if trees[i] is None or trees[j] is None:
                continue
            (a0, a1), (b0, b1) = boxes[i], boxes[j]
            if np.any(a1 < b0) or np.any(b1 < a0):
                continue
            if skip and skip(records[i], records[j]):
                continue
            hits = trees[i].overlap(trees[j])
            if hits:
                out.append([records[i]["name"], records[j]["name"], len(hits)])
                if len(out) >= limit:
                    return out
    return out


def non_manifold_edges(ob):
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    n = sum(1 for e in bm.edges if not e.is_manifold)
    bm.free()
    return n


# --------------------------------------------------------------------------- vertex colour (aged)

def _hash_noise(P, scale, seed):
    """Smooth value noise in 3D (trilinear, hashed lattice), P: N x 3 -> N in [0, 1]."""
    Q = P * scale + seed * 17.13
    i0 = np.floor(Q).astype(np.int64)
    f = Q - i0
    f = f * f * (3 - 2 * f)

    def h(ix, iy, iz):
        n = (ix * 73856093) ^ (iy * 19349663) ^ (iz * 83492791) ^ (seed * 2654435761 & 0xffffffff)
        n = (n ^ (n >> 13)) * 1274126177
        return ((n ^ (n >> 16)) & 0xffff) / 65535.0
    out = 0.0
    for dx in (0, 1):
        for dy in (0, 1):
            for dz in (0, 1):
                w = (f[:, 0] if dx else 1 - f[:, 0]) * (f[:, 1] if dy else 1 - f[:, 1]) * (f[:, 2] if dz else 1 - f[:, 2])
                out = out + w * h(i0[:, 0] + dx, i0[:, 1] + dy, i0[:, 2] + dz)
    return out


def vertex_ao(ob, dist, rays=20, seed=0):
    """Per-vertex ambient occlusion in [0, 1] (1 = open) from hemisphere rays (BVH of the object)."""
    from mathutils.bvhtree import BVHTree
    me = ob.data
    co, tri = mesh_arrays(ob, world=False)
    nrm = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("normal", nrm)
    nrm = nrm.reshape(-1, 3)
    bvh = BVHTree.FromPolygons(co.tolist(), tri.tolist(), all_triangles=True)
    rng = np.random.default_rng(seed)
    dirs = rng.normal(size=(rays, 3))
    dirs /= np.linalg.norm(dirs, axis=1, keepdims=True)
    ao = np.ones(len(co))
    eps = dist * 0.01
    for i in range(len(co)):
        n = nrm[i]
        p = co[i] + n * eps
        hit = 0
        tot = 0.0
        for d in dirs:
            c = float(d @ n)
            if c < 0:
                d = -d
                c = -c
            if c < 0.08:
                continue
            tot += c
            loc, _, _, _ = bvh.ray_cast(Vector(p), Vector(d), dist)
            if loc is not None:
                hit += c
        ao[i] = 1.0 - hit / max(tot, 1e-6)
    return ao


def paint_vertex_colors(ob, records, mats, style, seed=0, ao_dist=0.02):
    """Colour attribute 'Color' (linear, point domain). Aged bone: ivory x (AO + crevice darkening) x
    brown stain noise; gold / crystal pieces get their flat colours (used by the preview only — their
    materials do not read the attribute, so glTF COLOR_0 is written for the bone primitive only)."""
    me = ob.data
    co, _ = mesh_arrays(ob, world=False)
    ao = vertex_ao(ob, ao_dist, seed=seed) if style == "aged" else np.ones(len(co))
    col = np.ones((len(co), 4))
    bc = mats.spec["bone"]["color"] if hasattr(mats, "spec") else IVORY
    ivory = np.array((hex_lin(bc) if isinstance(bc, str) else tuple(bc))[:3])
    stain = np.array(hex_lin("#6E5A3A")[:3])
    for rec in records:
        sl = slice(rec["v0"], rec["v1"])
        if rec["material"] == "bone":
            if style == "aged":
                P = co[sl]
                n1 = _hash_noise(P, 1.0 / max(ao_dist * 2.5, 1e-4), seed)
                n2 = _hash_noise(P * np.array([1.0, 1.0, 0.25]), 1.0 / max(ao_dist * 1.2, 1e-4), seed + 7)
                st = np.clip((n1 * 0.6 + n2 * 0.4 - 0.42) * 2.4, 0, 1)[:, None] * 0.75
                occ = np.clip(ao[sl], 0, 1) ** 2.4
                base = ivory * 0.90 * (0.18 + 0.82 * occ)[:, None]
                c = base * (1 - st) + (stain * (0.55 + 0.45 * occ)[:, None]) * st
            else:
                c = np.tile(ivory, (rec["v1"] - rec["v0"], 1))
        else:
            # glTF multiplies COLOR_0 into every primitive's base colour, so gold / crystal stay white
            c = np.ones((rec["v1"] - rec["v0"], 3))
        col[sl, :3] = c
    attr = me.color_attributes.get("Color") or me.color_attributes.new("Color", "FLOAT_COLOR", "POINT")
    attr.data.foreach_set("color", col.ravel().astype(np.float32))
    me.color_attributes.active_color = attr
    try:
        me.color_attributes.render_color_index = me.color_attributes.find("Color")
    except Exception:
        pass
    return attr


def preview_tint(ob, records, mats, on=True):
    """Workbench VERTEX previews: temporarily paint non-bone pieces with their material colour
    (on=True) or back to white (on=False, the exported state)."""
    attr = ob.data.color_attributes.get("Color")
    if attr is None:
        return
    col = np.empty(len(attr.data) * 4, dtype=np.float32)
    attr.data.foreach_get("color", col)
    col = col.reshape(-1, 4)
    for rec in records:
        if rec["material"] != "bone":
            col[rec["v0"]:rec["v1"], :3] = np.array(mats(rec["material"]).diffuse_color[:3]) if on else 1.0
    attr.data.foreach_set("color", col.ravel())


# --------------------------------------------------------------------------- sockets / records

def socket_record(emp):
    bpy.context.view_layer.update()
    mw = emp.matrix_world
    r = mw.to_3x3().normalized()
    return {"name": emp.name, "kind": emp.get("kind"), "location": [round(x, 5) for x in mw.translation],
            "x_axis": [round(x, 5) for x in r.col[0]], "y_axis": [round(x, 5) for x in r.col[1]],
            "z_axis": [round(x, 5) for x in r.col[2]],
            "quaternion_wxyz": [round(x, 5) for x in r.to_quaternion()],
            "parent": emp.parent.name if emp.parent else None,
            "parent_bone": emp.parent_bone if emp.parent_type == "BONE" else None,
            "frame": emp.get("frame")}


def frame_errors(rec):
    """(orthonormality error, handedness x.(y x z))."""
    X, Y, Z = Vector(rec["x_axis"]), Vector(rec["y_axis"]), Vector(rec["z_axis"])
    ortho = max(abs(X.dot(Y)), abs(Y.dot(Z)), abs(Z.dot(X)), abs(X.length - 1), abs(Y.length - 1), abs(Z.length - 1))
    return ortho, X.dot(Y.cross(Z))


def add_socket(name, loc, rot3, parent=None, bone=None, kind="grip", frame=None, size=0.03, **extras):
    """Contract §6 socket empty; world placement given by loc / rot3 (world), parent kept."""
    emp = af.add_socket(name, parent=None, kind=kind, size=size, **extras)
    if frame:
        emp["frame"] = frame
    emp.rotation_mode = "QUATERNION"
    M = Matrix(rot3).to_4x4()
    M.translation = Vector(loc)
    emp.matrix_world = M
    bpy.context.view_layer.update()
    if parent is not None:
        mw = emp.matrix_world.copy()
        emp.parent = parent
        if bone:
            emp.parent_type = "BONE"
            emp.parent_bone = bone
        bpy.context.view_layer.update()
        emp.matrix_world = mw
        bpy.context.view_layer.update()
    return emp


# --------------------------------------------------------------------------- images

FONT5x7 = {
    "A": "01110 10001 10001 11111 10001 10001 10001", "B": "11110 10001 10001 11110 10001 10001 11110",
    "C": "01110 10001 10000 10000 10000 10001 01110", "D": "11100 10010 10001 10001 10001 10010 11100",
    "E": "11111 10000 10000 11110 10000 10000 11111", "F": "11111 10000 10000 11110 10000 10000 10000",
    "G": "01110 10001 10000 10111 10001 10001 01111", "H": "10001 10001 10001 11111 10001 10001 10001",
    "I": "01110 00100 00100 00100 00100 00100 01110", "J": "00111 00010 00010 00010 00010 10010 01100",
    "K": "10001 10010 10100 11000 10100 10010 10001", "L": "10000 10000 10000 10000 10000 10000 11111",
    "M": "10001 11011 10101 10101 10001 10001 10001", "N": "10001 10001 11001 10101 10011 10001 10001",
    "O": "01110 10001 10001 10001 10001 10001 01110", "P": "11110 10001 10001 11110 10000 10000 10000",
    "Q": "01110 10001 10001 10001 10101 10010 01101", "R": "11110 10001 10001 11110 10100 10010 10001",
    "S": "01111 10000 10000 01110 00001 00001 11110", "T": "11111 00100 00100 00100 00100 00100 00100",
    "U": "10001 10001 10001 10001 10001 10001 01110", "V": "10001 10001 10001 10001 10001 01010 00100",
    "W": "10001 10001 10001 10101 10101 10101 01010", "X": "10001 10001 01010 00100 01010 10001 10001",
    "Y": "10001 10001 10001 01010 00100 00100 00100", "Z": "11111 00001 00010 00100 01000 10000 11111",
    "0": "01110 10001 10011 10101 11001 10001 01110", "1": "00100 01100 00100 00100 00100 00100 01110",
    "2": "01110 10001 00001 00010 00100 01000 11111", "3": "11111 00010 00100 00010 00001 10001 01110",
    "4": "00010 00110 01010 10010 11111 00010 00010", "5": "11111 10000 11110 00001 00001 10001 01110",
    "6": "00110 01000 10000 11110 10001 10001 01110", "7": "11111 00001 00010 00100 01000 01000 01000",
    "8": "01110 10001 10001 01110 10001 10001 01110", "9": "01110 10001 10001 01111 00001 00010 01100",
    "_": "00000 00000 00000 00000 00000 00000 11111", "-": "00000 00000 00000 11111 00000 00000 00000",
    ".": "00000 00000 00000 00000 00000 01100 01100", ":": "00000 01100 01100 00000 01100 01100 00000",
    "/": "00001 00010 00010 00100 01000 01000 10000", " ": "00000 00000 00000 00000 00000 00000 00000",
    "(": "00010 00100 01000 01000 01000 00100 00010", ")": "01000 00100 00010 00010 00010 00100 01000",
    "=": "00000 00000 11111 00000 11111 00000 00000", ",": "00000 00000 00000 00000 01100 00100 01000",
    "+": "00000 00100 00100 11111 00100 00100 00000", "%": "11001 11010 00010 00100 01000 01011 10011",
}


def stamp_text(img, text, x, y_top, scale=2, color=(1, 1, 1, 1)):
    """Draw text into a top-down RGBA float array (H, W, 4)."""
    H, W = img.shape[:2]
    cx = x
    for ch in str(text).upper():
        g = FONT5x7.get(ch, FONT5x7[" "]).split()
        for r, row in enumerate(g):
            for c, bit in enumerate(row):
                if bit == "1":
                    y0, x0 = y_top + r * scale, cx + c * scale
                    if 0 <= y0 < H - scale and 0 <= x0 < W - scale:
                        img[y0:y0 + scale, x0:x0 + scale] = color
        cx += 6 * scale
    return img


def load_png(path):
    im = bpy.data.images.load(path, check_existing=False)
    px = np.array(im.pixels[:], dtype=np.float32).reshape(im.size[1], im.size[0], 4)[::-1].copy()
    bpy.data.images.remove(im)
    return px  # top-down


def save_png(arr, path):
    h, w = arr.shape[:2]
    im = bpy.data.images.new("af_bone_kit_out", width=w, height=h, alpha=True)
    im.pixels = np.ascontiguousarray(arr[::-1]).ravel()
    im.filepath_raw = os.path.abspath(path)
    im.file_format = "PNG"
    im.save()
    bpy.data.images.remove(im)
    return os.path.abspath(path)


def over_bg(tile, bg=(0.16, 0.17, 0.19)):
    a = tile[..., 3:4]
    out = tile.copy()
    out[..., :3] = tile[..., :3] * a + np.array(bg, dtype=np.float32) * (1 - a)
    out[..., 3] = 1.0
    return out


def resize(arr, w, h):
    """Area-average downscale / nearest upscale (numpy)."""
    H, W = arr.shape[:2]
    if w <= W and h <= H:
        ys = np.linspace(0, H, h + 1).astype(int)
        xs = np.linspace(0, W, w + 1).astype(int)
        csum = np.cumsum(np.cumsum(np.pad(arr, ((1, 0), (1, 0), (0, 0))), 0), 1)
        tot = (csum[ys[1:]][:, xs[1:]] - csum[ys[:-1]][:, xs[1:]] - csum[ys[1:]][:, xs[:-1]] + csum[ys[:-1]][:, xs[:-1]])
        area = ((ys[1:] - ys[:-1])[:, None] * (xs[1:] - xs[:-1])[None, :])[..., None]
        return (tot / np.maximum(area, 1)).astype(np.float32)
    yi = (np.arange(h) * H / h).astype(int)
    xi = (np.arange(w) * W / w).astype(int)
    return arr[yi][:, xi]


def fit_tile(arr, w, h, bg=(0.16, 0.17, 0.19)):
    """Fit an RGBA top-down image into w x h (keep aspect) over bg."""
    H, W = arr.shape[:2]
    s = min(w / float(W), h / float(H))
    t = resize(over_bg(arr, bg), max(1, int(W * s)), max(1, int(H * s)))
    out = np.zeros((h, w, 4), np.float32)
    out[..., :3] = bg
    out[..., 3] = 1
    y0, x0 = (h - t.shape[0]) // 2, (w - t.shape[1]) // 2
    out[y0:y0 + t.shape[0], x0:x0 + t.shape[1]] = t
    return out


def contact_sheet(tiles, labels, path, cols=4, tile=256, bg=(0.16, 0.17, 0.19), title=None):
    rows = int(math.ceil(len(tiles) / float(cols)))
    top = 28 if title else 0
    lh = 36
    sheet = np.zeros((top + rows * (tile + lh), cols * tile, 4), np.float32)
    sheet[..., :3] = bg
    sheet[..., 3] = 1
    if title:
        stamp_text(sheet, title, 8, 6, 2, (0.95, 0.85, 0.55, 1))
    for i, (t, lab) in enumerate(zip(tiles, labels)):
        r, c = divmod(i, cols)
        y0 = top + r * (tile + lh)
        sheet[y0:y0 + tile, c * tile:(c + 1) * tile] = fit_tile(t, tile, tile, bg)
        for k, line in enumerate((lab if isinstance(lab, (list, tuple)) else [lab])[:2]):
            stamp_text(sheet, str(line)[:21], c * tile + 6, y0 + tile + 2 + k * 16, 2,
                       (0.92, 0.92, 0.92, 1) if k == 0 else (0.6, 0.75, 0.9, 1))
    return save_png(sheet, path)


# --------------------------------------------------------------------------- EEVEE close-ups

def eevee_shot(path, objects, view_dir, up=(0, 0, 1), size=(512, 512), margin=1.12, target=None,
               ortho_scale=None, bg=(0.62, 0.62, 0.64), samples=32):
    """Orthographic EEVEE render of `objects` (studio key + fill + rim, grey world) -> RGBA PNG.
    Used for comparison close-ups; part previews use af.render_views (Workbench)."""
    scene = bpy.context.scene
    bpy.context.view_layer.update()
    lo, hi = af.world_bbox(objects)
    centre = Vector(target) if target is not None else (lo + hi) / 2
    d = Vector(view_dir).normalized()
    cam_data = bpy.data.cameras.new("bk_cam")
    cam_data.type = "ORTHO"
    upv = Vector(up)
    # extent of the boxes projected on the image plane
    R = frame_from(z=-d, y=upv)
    pts = [Vector((x, y, z)) for x in (lo.x, hi.x) for y in (lo.y, hi.y) for z in (lo.z, hi.z)]
    loc = [R.transposed() @ (p - centre) for p in pts]
    ext_x = max(abs(p.x) for p in loc) * 2
    ext_y = max(abs(p.y) for p in loc) * 2
    # Blender's ortho_scale spans the larger image side (sensor fit AUTO)
    W_, H_ = float(size[0]), float(size[1])
    fit = max(ext_x, ext_y * W_ / H_) if W_ >= H_ else max(ext_y, ext_x * H_ / W_)
    cam_data.ortho_scale = ortho_scale or fit * margin
    span = max((hi - lo).length, 0.05)
    cam_data.clip_start = span * 0.01
    cam_data.clip_end = span * 40
    cam = bpy.data.objects.new("bk_cam", cam_data)
    scene.collection.objects.link(cam)
    cam.location = centre - d * span * 4
    cam.rotation_euler = R.to_euler()
    prev = scene.camera
    scene.camera = cam
    lights = []
    for nm, dirv, energy, col in (("key", (-0.55, -0.75, 0.9), 4.2, (1.0, 0.96, 0.9)),
                                  ("fill", (0.9, -0.5, 0.15), 0.8, (0.85, 0.9, 1.0)),
                                  ("rim", (0.2, 0.9, 0.5), 1.8, (1.0, 1.0, 1.0))):
        ld = bpy.data.lights.new("bk_" + nm, "SUN")
        ld.energy = energy
        ld.color = col
        ld.angle = math.radians(8)
        lo_ = bpy.data.objects.new("bk_" + nm, ld)
        scene.collection.objects.link(lo_)
        lo_.rotation_euler = (Vector((0, 0, 1)).rotation_difference(Vector(dirv).normalized())).to_euler()
        lights.append((lo_, ld))
    world = bpy.data.worlds.new("bk_world")
    world.use_nodes = True
    bgn = world.node_tree.nodes.get("Background")
    bgn.inputs["Color"].default_value = (bg[0], bg[1], bg[2], 1)
    bgn.inputs["Strength"].default_value = 0.35
    prev_world = scene.world
    scene.world = world
    r = scene.render
    af.set_engine(scene, "EEVEE")
    try:
        scene.eevee.taa_render_samples = samples
    except Exception:
        pass
    try:
        scene.view_settings.view_transform = "AgX"
        scene.view_settings.look = "AgX - Medium High Contrast"
    except Exception:
        try:
            scene.view_settings.view_transform = "Standard"
        except Exception:
            pass
    r.resolution_x, r.resolution_y = int(size[0]), int(size[1])
    r.resolution_percentage = 100
    r.film_transparent = True
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGBA"
    r.filepath = os.path.abspath(path)
    hidden = []
    keep = set(objects)
    for o in scene.objects:
        if o.type == "MESH" and o not in keep and not o.hide_render:
            o.hide_render = True
            hidden.append(o)
    try:
        bpy.ops.render.render(write_still=True)
    finally:
        for o in hidden:
            o.hide_render = False
        scene.camera = prev
        scene.world = prev_world
        bpy.data.worlds.remove(world)
        for lo_, ld in lights:
            bpy.data.objects.remove(lo_)
            bpy.data.lights.remove(ld)
        bpy.data.objects.remove(cam)
        bpy.data.cameras.remove(cam_data)
    return os.path.abspath(path)
