"""Shared helpers of the "items" module (item_kit, item_from_mesh, attach_prop).

Blender 5.0.1, bundled Python 3.11 + numpy. Only the items module edits this file.

Item frame (docs/FORGE-CONTRACT.md §4, §6): Z up, metres, the item stands on z = 0 with its main
axis (blade / shaft / handle) along +Z and the edge / face toward -Y. socket_grip_main uses the
shared grip frame: +Z out of the fist on the thumb side (along the blade or shaft from the grip),
-Y toward the cutting edge / face (the direction straight fingers point), X = Y x Z.
"""
import json
import math
import os
import random
import struct

import bmesh
import bpy
import mathutils
import numpy as np
from mathutils import Matrix, Vector

import af_common as af

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


def frame_matrix(x=None, y=None, z=None):
    """Rotation (3x3) whose columns are the given axes; missing axes complete a right-handed frame.
    Give exactly two axes (the second is orthogonalised against the first given)."""
    if z is not None and y is not None:
        z = Vector(z).normalized()
        y = (Vector(y) - z * Vector(y).dot(z)).normalized()
        x = y.cross(z)
    elif z is not None and x is not None:
        z = Vector(z).normalized()
        x = (Vector(x) - z * Vector(x).dot(z)).normalized()
        y = z.cross(x)
    elif x is not None and y is not None:
        x = Vector(x).normalized()
        y = (Vector(y) - x * Vector(y).dot(x)).normalized()
        z = x.cross(y)
    else:
        raise ValueError("frame_matrix needs two axes")
    return Matrix((x, y, z)).transposed()


def mat4(rot3=None, loc=(0, 0, 0)):
    m = (rot3 if rot3 is not None else Matrix.Identity(3)).to_4x4()
    m.translation = Vector(loc)
    return m


def offset_matrix(offset):
    """params.offset {"loc":[x,y,z] (m), "rot_deg":[x,y,z] (XYZ Euler)} -> 4x4 in socket space."""
    if not offset:
        return Matrix.Identity(4)
    loc = Vector(offset.get("loc", (0, 0, 0)))
    rot = [math.radians(a) for a in offset.get("rot_deg", (0, 0, 0))]
    return mat4(mathutils.Euler(rot, "XYZ").to_matrix(), loc)


def hex_to_linear(c):
    """'#rrggbb' or [r,g,b] (sRGB 0..1) -> linear RGBA tuple."""
    if isinstance(c, str):
        c = c.lstrip("#")
        rgb = [int(c[i:i + 2], 16) / 255.0 for i in (0, 2, 4)]
    else:
        rgb = [float(x) for x in c[:3]]

    def lin(v):
        return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4
    return (lin(rgb[0]), lin(rgb[1]), lin(rgb[2]), 1.0)


# --------------------------------------------------------------------------- palettes / materials

# role -> (sRGB colour, metallic, roughness, emission strength)
PALETTES = {
    "medieval": {
        "metal": ("#b8bec6", 1.0, 0.30, 0), "metal_dark": ("#6d737b", 1.0, 0.42, 0),
        "trim": ("#a4814e", 1.0, 0.38, 0), "grip": ("#5b3a23", 0.0, 0.72, 0),
        "wood": ("#7b5332", 0.0, 0.68, 0), "wood2": ("#694427", 0.0, 0.72, 0),
        "accent": ("#8a2121", 0.0, 0.62, 0), "accent2": ("#d9c27a", 0.0, 0.55, 0),
        "gem": ("#c0303a", 0.0, 0.12, 0), "bone": ("#ddd1b4", 0.0, 0.62, 0),
        "dark": ("#141212", 0.0, 0.9, 0), "string": ("#d9cfb5", 0.0, 0.8, 0),
        "glass": ("#bfe3e8", 0.0, 0.06, 0), "liquid": ("#c4202a", 0.0, 0.08, 0.6),
        "paper": ("#e6dcc0", 0.0, 0.85, 0), "flame": ("#ff8a1e", 0.0, 0.5, 6.0),
        "emit": ("#ffb347", 0.0, 0.4, 3.0),
    },
    "dark": {
        "metal": ("#6c7079", 1.0, 0.40, 0), "metal_dark": ("#44464d", 1.0, 0.5, 0),
        "trim": ("#4d4944", 1.0, 0.48, 0), "grip": ("#2c201b", 0.0, 0.78, 0),
        "wood": ("#3e2d24", 0.0, 0.74, 0), "wood2": ("#33251e", 0.0, 0.78, 0),
        "accent": ("#5c1414", 0.0, 0.66, 0), "accent2": ("#8f8a76", 0.0, 0.6, 0),
        "gem": ("#79ff4d", 0.0, 0.15, 4.0), "bone": ("#cfc3a1", 0.0, 0.66, 0),
        "dark": ("#090909", 0.0, 0.9, 0), "string": ("#7e7466", 0.0, 0.85, 0),
        "glass": ("#9fb5a8", 0.0, 0.08, 0), "liquid": ("#4dd62a", 0.0, 0.1, 1.2),
        "paper": ("#bdb08f", 0.0, 0.88, 0), "flame": ("#7dff4a", 0.0, 0.5, 6.0),
        "emit": ("#79ff4d", 0.0, 0.3, 4.0),
    },
    "elven": {
        "metal": ("#dde3e6", 1.0, 0.20, 0), "metal_dark": ("#9fa9ad", 1.0, 0.3, 0),
        "trim": ("#d2b264", 1.0, 0.28, 0), "grip": ("#2e5e4b", 0.0, 0.66, 0),
        "wood": ("#c9b48a", 0.0, 0.58, 0), "wood2": ("#b49e74", 0.0, 0.6, 0),
        "accent": ("#3f8f74", 0.0, 0.55, 0), "accent2": ("#e9e0c4", 0.0, 0.5, 0),
        "gem": ("#86e6ff", 0.0, 0.08, 2.5), "bone": ("#efe8d6", 0.0, 0.55, 0),
        "dark": ("#1d2a26", 0.0, 0.9, 0), "string": ("#efe9d6", 0.0, 0.7, 0),
        "glass": ("#d8f4f2", 0.0, 0.04, 0), "liquid": ("#36c2ff", 0.0, 0.06, 1.0),
        "paper": ("#f0e8d0", 0.0, 0.82, 0), "flame": ("#9fe8ff", 0.0, 0.5, 5.0),
        "emit": ("#86e6ff", 0.0, 0.3, 2.5),
    },
    "scifi": {
        "metal": ("#e3e6ea", 0.0, 0.42, 0), "metal_dark": ("#3c424b", 1.0, 0.34, 0),
        "trim": ("#4a5058", 1.0, 0.32, 0), "grip": ("#23262b", 0.0, 0.86, 0),
        "wood": ("#5b626b", 0.6, 0.4, 0), "wood2": ("#4c535b", 0.6, 0.45, 0),
        "accent": ("#ff8a1f", 0.0, 0.5, 0), "accent2": ("#c9ced6", 0.0, 0.5, 0),
        "gem": ("#33e0ff", 0.0, 0.1, 5.0), "bone": ("#d0d4da", 0.0, 0.5, 0),
        "dark": ("#0e1013", 0.0, 0.7, 0), "string": ("#9aa3ad", 0.6, 0.4, 0),
        "glass": ("#a8e8ff", 0.0, 0.04, 0), "liquid": ("#33e0ff", 0.0, 0.08, 1.5),
        "paper": ("#d8dde3", 0.0, 0.7, 0), "flame": ("#33e0ff", 0.0, 0.4, 6.0),
        "emit": ("#33e0ff", 0.0, 0.3, 5.0),
    },
}
STYLES = tuple(PALETTES)


class Materials:
    """Creates glTF-exportable Principled BSDF materials per role, cached by name."""

    def __init__(self, style="medieval", overrides=None, prefix="mat"):
        if style not in PALETTES:
            raise ValueError("unknown style %r (use %s)" % (style, ", ".join(STYLES)))
        self.style = style
        self.pal = {k: list(v) for k, v in PALETTES[style].items()}
        for role, val in (overrides or {}).items():
            base = self.pal.get(role, ["#808080", 0.0, 0.5, 0])
            if isinstance(val, str) or (isinstance(val, (list, tuple)) and len(val) == 3
                                        and all(isinstance(x, (int, float)) for x in val)):
                base[0] = val
            elif isinstance(val, dict):
                base = [val.get("color", base[0]), val.get("metallic", base[1]),
                        val.get("roughness", base[2]), val.get("emission", base[3])]
            self.pal[role] = base
        self.prefix = prefix
        self.cache = {}

    def __call__(self, role):
        if role in self.cache:
            return self.cache[role]
        col, metal, rough, emit = self.pal.get(role, self.pal["metal"])
        name = "%s_%s_%s" % (self.prefix, self.style, role)
        m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
        m.use_nodes = True
        nt = m.node_tree
        bsdf = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None)
        if bsdf is None:
            bsdf = nt.nodes.new("ShaderNodeBsdfPrincipled")
        rgba = hex_to_linear(col)
        bsdf.inputs["Base Color"].default_value = rgba
        bsdf.inputs["Metallic"].default_value = float(metal)
        bsdf.inputs["Roughness"].default_value = float(rough)
        if emit:
            bsdf.inputs["Emission Color"].default_value = rgba
            bsdf.inputs["Emission Strength"].default_value = float(emit)
        # viewport values drive only the Workbench preview: full metallic renders near-black there,
        # so the preview uses a softened metallic; the exported Principled values stay exact
        m.diffuse_color = rgba
        m.metallic = min(float(metal), 0.35)
        m.roughness = max(float(rough), 0.3)
        m["af_role"] = role
        self.cache[role] = m
        return m


def principled_ok(mat):
    """True when the material has a Principled BSDF wired into the active Material Output surface
    (the shape the glTF exporter turns into a PBR metallic-roughness material)."""
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return False
    for n in mat.node_tree.nodes:
        if n.type == "OUTPUT_MATERIAL" and (n.is_active_output or len(
                [x for x in mat.node_tree.nodes if x.type == "OUTPUT_MATERIAL"]) == 1):
            surf = n.inputs.get("Surface")
            if surf and surf.is_linked and surf.links[0].from_node.type == "BSDF_PRINCIPLED":
                return True
    return False


def _principled_signature(mat):
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return None
    nodes = mat.node_tree.nodes
    if any(n.type == "TEX_IMAGE" for n in nodes):
        return None
    bsdf = [n for n in nodes if n.type == "BSDF_PRINCIPLED"]
    if len(bsdf) != 1:
        return None
    b = bsdf[0]
    sig = []
    for key in ("Base Color", "Metallic", "Roughness", "Emission Color", "Emission Strength", "Alpha"):
        inp = b.inputs.get(key)
        if inp is None:
            continue
        if inp.is_linked:
            return None
        v = inp.default_value
        sig.extend(round(float(x), 4) for x in (v[:] if hasattr(v, "__len__") else [v]))
    return tuple(sig)


def dedupe_materials():
    """Imported kit items reuse material names (mat_medieval_trim, ...): Blender then keeps
    'mat_medieval_trim.001'. Remap such copies onto the original when both are untextured
    Principled materials with the same values. Returns the merged names."""
    import re
    merged = []
    for m in list(bpy.data.materials):
        mt = re.match(r"^(.*)\.\d{3}$", m.name)
        if not mt:
            continue
        base = bpy.data.materials.get(mt.group(1))
        if base is None or base == m:
            continue
        s0, s1 = _principled_signature(base), _principled_signature(m)
        if s0 is None or s0 != s1:
            continue
        m.user_remap(base)
        merged.append(m.name)
        bpy.data.materials.remove(m)
    return merged


# --------------------------------------------------------------------------- geometry generators
# All generators return (verts, faces) as python lists; parts are closed where possible and their
# winding is normalised later with bmesh.recalc_face_normals.

def loft(sections, closed=True, cap0=True, cap1=True, tip0=None, tip1=None):
    """Rings of equal length -> quads. tip0/tip1: optional apex point instead of an n-gon cap."""
    n = len(sections[0])
    verts = [tuple(p) for s in sections for p in s]
    faces = []
    rows = len(sections)
    m = n if closed else n - 1
    for r in range(rows - 1):
        for i in range(m):
            a, b = r * n + i, r * n + (i + 1) % n
            faces.append((a, b, b + n, a + n))
    if closed:
        if tip0 is not None:
            c = len(verts)
            verts.append(tuple(tip0))
            for i in range(n):
                faces.append((c, (i + 1) % n, i))
        elif cap0:
            faces.append(tuple(reversed(range(n))))
        if tip1 is not None:
            c = len(verts)
            verts.append(tuple(tip1))
            base = (rows - 1) * n
            for i in range(n):
                faces.append((base + i, base + (i + 1) % n, c))
        elif cap1:
            faces.append(tuple(range((rows - 1) * n, rows * n)))
    return verts, faces


def lathe(profile, seg=16, phase=0.0, sx=1.0, sy=1.0, twist=0.0, rfunc=None):
    """Revolve a (r, z) profile (bottom -> top) around +Z. r <= 1e-6 at an end becomes a pole.
    rfunc(r, angle, z) -> r lets callers add flutes / spiral wraps."""
    prof = list(profile)
    pole0 = prof[0][0] <= 1e-6
    pole1 = prof[-1][0] <= 1e-6
    body = prof[1 if pole0 else 0: len(prof) - 1 if pole1 else len(prof)]
    rings = []
    for j, (r, z) in enumerate(body):
        ring = []
        for i in range(seg):
            a = phase + 2 * math.pi * i / seg + twist * z
            rr = rfunc(r, a, z) if rfunc else r
            ring.append((rr * math.cos(a) * sx, rr * math.sin(a) * sy, z))
        rings.append(ring)
    return loft(rings, True, not pole0, not pole1,
                tip0=(0, 0, prof[0][1]) if pole0 else None,
                tip1=(0, 0, prof[-1][1]) if pole1 else None)


def rounded_rect(w, h, r, n_corner=2):
    """CCW 2D outline of a rectangle w x h (centred) with corner radius r."""
    r = min(r, w / 2 * 0.999, h / 2 * 0.999)
    pts = []
    for cx, cy, a0 in ((w / 2 - r, h / 2 - r, 0), (-w / 2 + r, h / 2 - r, 90),
                       (-w / 2 + r, -h / 2 + r, 180), (w / 2 - r, -h / 2 + r, 270)):
        for k in range(n_corner + 1):
            a = math.radians(a0 + 90.0 * k / n_corner)
            pts.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


def ellipse(rx, ry, n=12, phase=0.0):
    return [(rx * math.cos(phase + 2 * math.pi * i / n), ry * math.sin(phase + 2 * math.pi * i / n))
            for i in range(n)]


def catmull(pts, n_seg=6, closed=False):
    """Catmull-Rom smoothing of a 2D/3D polyline (ported from an earlier in-house rig script)."""
    P = [Vector(p) for p in pts]
    m = len(P)
    out = []
    rng = range(m) if closed else range(m - 1)
    for i in rng:
        p0 = P[(i - 1) % m] if closed or i > 0 else P[0]
        p1, p2 = P[i], P[(i + 1) % m]
        p3 = P[(i + 2) % m] if closed or i + 2 < m else P[-1]
        for k in range(n_seg):
            t = k / n_seg
            t2, t3 = t * t, t * t * t
            out.append(0.5 * ((2 * p1) + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2
                              + (-p0 + 3 * p1 - 3 * p2 + p3) * t3))
    if not closed:
        out.append(P[-1])
    return [tuple(p) for p in out]


def transport_frames(path, closed=False, up_hint=None):
    """Parallel-transport frames (tangent, normal, binormal) along a 3D polyline."""
    P = [Vector(p) for p in path]
    n = len(P)
    T = []
    for i in range(n):
        if closed:
            t = P[(i + 1) % n] - P[i - 1]
        elif i == 0:
            t = P[1] - P[0]
        elif i == n - 1:
            t = P[-1] - P[-2]
        else:
            t = P[i + 1] - P[i - 1]
        T.append(t.normalized() if t.length > 1e-9 else Vector((0, 0, 1)))
    hint = Vector(up_hint) if up_hint is not None else None
    if hint is None or abs(hint.normalized().dot(T[0])) > 0.95:
        hint = Vector((0, 0, 1)) if abs(T[0].z) < 0.9 else Vector((1, 0, 0))
    N = [(hint - T[0] * hint.dot(T[0])).normalized()]
    for i in range(1, n):
        q = T[i - 1].rotation_difference(T[i])
        v = q @ N[-1]
        N.append((v - T[i] * v.dot(T[i])).normalized())
    B = [T[i].cross(N[i]) for i in range(n)]
    return T, N, B


def sweep(path, radius, n=8, closed_path=False, caps=True, section=None, up_hint=None,
          tip0=False, tip1=False, roll=0.0):
    """Tube along a 3D path. radius: float, list (per point) or callable(u). section: optional 2D
    outline (unit size) used instead of a circle; it is scaled by radius (or (rx, ry) tuples)."""
    T, N, B = transport_frames(path, closed_path, up_hint)
    m = len(path)
    sec = section or ellipse(1.0, 1.0, n, phase=roll)
    rings = []
    for i, p in enumerate(path):
        u = i / max(1, m - 1)
        r = radius(u) if callable(radius) else (radius[i] if isinstance(radius, (list, tuple))
                                                and len(radius) == m else radius)
        rx, ry = (r if isinstance(r, (list, tuple)) else (r, r))
        c = Vector(p)
        rings.append([tuple(c + N[i] * (q[0] * rx) + B[i] * (q[1] * ry)) for q in sec])
    if closed_path:
        rings.append(rings[0])
        v, f = loft(rings, True, False, False)
        return v, f
    t0 = path[0] if tip0 else None
    t1 = path[-1] if tip1 else None
    if tip0:
        rings = rings[1:]
    if tip1:
        rings = rings[:-1]
    return loft(rings, True, caps, caps, tip0=t0, tip1=t1)


def tessellate(outline):
    """Triangulate a simple 2D polygon -> list of index triples."""
    loop = [Vector((p[0], p[1], 0.0)) for p in outline]
    return [tuple(t) for t in mathutils.geometry.tessellate_polygon([loop])]


def _point_in_poly(P, poly):
    """Even-odd test of points P (N x 2) against a closed polygon (M x 2)."""
    x, y = P[:, 0], P[:, 1]
    inside = np.zeros(len(P), dtype=bool)
    m = len(poly)
    for i in range(m):
        x0, y0 = poly[i]
        x1, y1 = poly[(i + 1) % m]
        cross = ((y0 > y) != (y1 > y))
        with np.errstate(divide="ignore", invalid="ignore"):
            xi = x0 + (y - y0) * (x1 - x0) / (y1 - y0)
        inside ^= cross & (x < xi)
    return inside


def _dist_to_poly(P, poly):
    d = np.full(len(P), 1e9)
    m = len(poly)
    for i in range(m):
        a = np.array(poly[i])
        b = np.array(poly[(i + 1) % m])
        ab = b - a
        t = np.clip(((P - a) @ ab) / max(1e-12, ab @ ab), 0.0, 1.0)
        d = np.minimum(d, np.linalg.norm(P - (a + np.outer(t, ab)), axis=1))
    return d


def fill_polygon(outline, step):
    """Triangulate a simple 2D polygon with interior sample points (constrained Delaunay,
    mathutils.geometry.delaunay_2d_cdt). Returns (points, tris); points[:len(outline)] is the outline
    in its input order. Falls back to plain tessellation when the CDT changes the outline."""
    n = len(outline)
    P = np.array(outline, dtype=np.float64)
    lo, hi = P.min(0), P.max(0)
    pts = []
    row = 0
    y = lo[1] + step * 0.5
    while y < hi[1]:
        x = lo[0] + step * (0.5 + 0.5 * (row % 2))
        while x < hi[0]:
            pts.append((x, y))
            x += step
        y += step * 0.866
        row += 1
    interior = np.zeros((0, 2))
    if pts:
        Q = np.array(pts)
        keep = _point_in_poly(Q, P) & (_dist_to_poly(Q, P) > 0.4 * step)
        interior = Q[keep]
    if not len(interior):
        return list(outline), tessellate(outline)
    area = 0.5 * float(np.sum(P[:, 0] * np.roll(P[:, 1], -1) - np.roll(P[:, 0], -1) * P[:, 1]))
    loop = list(range(n)) if area > 0 else list(reversed(range(n)))
    coords = [Vector((float(p[0]), float(p[1]))) for p in P] + [Vector((float(p[0]), float(p[1]))) for p in interior]
    edges = [(loop[i], loop[(i + 1) % n]) for i in range(n)]
    try:
        vco, _, vfaces, orig_v, _, _ = mathutils.geometry.delaunay_2d_cdt(coords, edges, [loop], 1, 1e-9, True)
    except Exception:
        return list(outline), tessellate(outline)
    points = [tuple(p) for p in P] + [tuple(p) for p in interior]
    out2in = {}
    for k, ov in enumerate(orig_v):
        if len(ov):
            out2in[k] = min(ov)          # merged input vertices collapse onto the lowest index
        else:                            # Steiner vertex where the outline crosses itself (catmull overshoot)
            out2in[k] = len(points)
            points.append((float(vco[k][0]), float(vco[k][1])))
    tris = [tuple(out2in[i] for i in f) for f in vfaces if len(f) == 3]
    if not tris:
        return list(outline), tessellate(outline)
    return points, tris


def plate(outline, thick, axes="YZ", centre=0.0, bulge=None, fill=None):
    """Extrude a 2D outline (u, v) into a slab. axes: the plane of the outline in item space
    ('YZ': u->Y, v->Z, thickness along X; 'XZ': u->X, v->Z, thickness along Y; 'XY': thickness Z).
    thick: float or callable(u, v) -> thickness. bulge(u, v) -> extra offset of both faces along
    the thickness axis (curved shields). fill: interior sample spacing (m) — a tapered or bulged cap
    then gets interior vertices (constrained Delaunay) and shades as a smooth surface instead of long
    fan triangles between outline points (those read as radial crease streaks when smooth-shaded)."""
    n = len(outline)
    if fill:
        pts2, tris = fill_polygon(outline, float(fill))
    else:
        pts2, tris = list(outline), tessellate(outline)
    m = len(pts2)
    verts, faces = [], []
    for side in (+1, -1):
        for (u, v) in pts2:
            t = thick(u, v) if callable(thick) else thick
            w = centre + side * t / 2 + (bulge(u, v) if bulge else 0.0)
            verts.append(_axes(axes, u, v, w))
    for (a, b, c) in tris:
        faces.append((a, b, c))
        faces.append((m + c, m + b, m + a))
    if fill:
        # side walls along the cap's own boundary (edges used by one triangle): stays watertight even
        # when the triangulation added a vertex where a smoothed outline crosses itself
        count = {}
        for (a, b, c) in tris:
            for e in ((a, b), (b, c), (c, a)):
                k = (min(e), max(e))
                count[k] = count.get(k, 0) + 1
        for (a, b, c) in tris:
            for (i, j) in ((a, b), (b, c), (c, a)):
                if count[(min(i, j), max(i, j))] == 1:
                    faces.append((j, i, m + i, m + j))
        return verts, faces
    for i in range(n):
        j = (i + 1) % n
        faces.append((j, i, m + i, m + j))
    return verts, faces


def _axes(axes, u, v, w):
    if axes == "YZ":
        return (w, u, v)
    if axes == "XZ":
        return (u, w, v)
    if axes == "XY":
        return (u, v, w)
    raise ValueError(axes)


def box(sx, sy, sz, centre=(0, 0, 0)):
    cx, cy, cz = centre
    hx, hy, hz = sx / 2, sy / 2, sz / 2
    v = [(cx - hx, cy - hy, cz - hz), (cx + hx, cy - hy, cz - hz), (cx + hx, cy + hy, cz - hz),
         (cx - hx, cy + hy, cz - hz), (cx - hx, cy - hy, cz + hz), (cx + hx, cy - hy, cz + hz),
         (cx + hx, cy + hy, cz + hz), (cx - hx, cy + hy, cz + hz)]
    f = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
    return v, f


def crystal(radius, height, sides=6, mid=0.62, top=0.22, bottom=0.18, jitter=0.0, rng=None):
    """Faceted crystal along +Z from z=0: bottom apex, two rings, top apex."""
    rng = rng or random.Random(0)
    rings = []
    for z, rs in ((height * bottom, 1.0), (height * mid, 0.86)):
        ring = []
        for i in range(sides):
            a = 2 * math.pi * i / sides + (0.0 if z < height * 0.5 else math.pi / sides * 0.35)
            rr = radius * rs * (1.0 + jitter * (rng.random() - 0.5))
            ring.append((rr * math.cos(a), rr * math.sin(a), z))
        rings.append(ring)
    return loft(rings, True, False, False, tip0=(0, 0, 0), tip1=(0, 0, height))


def uv_sphere(r, seg=12, rings=8, sz=None, centre=(0, 0, 0)):
    prof = [(r * math.sin(math.pi * k / rings), -r * math.cos(math.pi * k / rings) * (sz or 1.0))
            for k in range(rings + 1)]
    prof[0] = (0.0, prof[0][1])
    prof[-1] = (0.0, prof[-1][1])
    v, f = lathe(prof, seg)
    return translate(v, centre), f


def ico_sphere(r, subdiv=1, centre=(0, 0, 0)):
    bm = bmesh.new()
    bmesh.ops.create_icosphere(bm, subdivisions=subdiv, radius=r)
    v = [tuple(Vector(x.co) + Vector(centre)) for x in bm.verts]
    f = [tuple(x.index for x in face.verts) for face in bm.faces]
    bm.free()
    return v, f


def transform(verts, m):
    m = m if isinstance(m, Matrix) else Matrix(m)
    if len(m) == 3:
        m = m.to_4x4()
    return [tuple(m @ Vector(p)) for p in verts]


def translate(verts, d):
    dx, dy, dz = d
    return [(p[0] + dx, p[1] + dy, p[2] + dz) for p in verts]


def rot(axis, deg):
    return Matrix.Rotation(math.radians(deg), 4, axis)


def merge(*parts):
    """Merge (verts, faces) pairs into one."""
    V, F = [], []
    for v, f in parts:
        o = len(V)
        V.extend(v)
        F.extend(tuple(i + o for i in face) for face in f)
    return V, F


# --------------------------------------------------------------------------- parts -> objects

def link(ob):
    bpy.context.scene.collection.objects.link(ob)
    return ob


def apply_modifiers(ob):
    if not ob.modifiers:
        return ob
    dg = bpy.context.evaluated_depsgraph_get()
    ev = ob.evaluated_get(dg)
    new = bpy.data.meshes.new_from_object(ev)
    old = ob.data
    ob.modifiers.clear()
    ob.data = new
    if old.users == 0:
        bpy.data.meshes.remove(old)
    return ob


def part(name, geo, mat, smooth=True, sharp_deg=40.0, bevel=None, recalc=True, face_mats=None,
         mats=None, subsurf=0):
    """Create a mesh object from (verts, faces). bevel: (width, segments, angle_deg) applied with a
    Bevel modifier (angle limited). face_mats: per-face index into `mats` (list of materials)."""
    verts, faces = geo
    me = bpy.data.meshes.new(name)
    me.from_pydata([tuple(map(float, p)) for p in verts], [], [tuple(f) for f in faces])
    me.validate(clean_customdata=False)
    me.update()
    if recalc:
        bm = bmesh.new()
        bm.from_mesh(me)
        bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-6)
        bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
        bm.to_mesh(me)
        bm.free()
    if mats:
        for m in mats:
            me.materials.append(m)
        if face_mats:
            for poly, mi in zip(me.polygons, face_mats):
                poly.material_index = int(mi)
    elif mat is not None:
        me.materials.append(mat)
    ob = link(bpy.data.objects.new(name, me))
    if smooth:
        me.shade_smooth()
    else:
        me.shade_flat()
    if subsurf:
        mod = ob.modifiers.new("subsurf", "SUBSURF")
        mod.levels = mod.render_levels = int(subsurf)
        apply_modifiers(ob)
    if bevel:
        w, segs, ang = (list(bevel) + [1, 35.0])[:3] if isinstance(bevel, (list, tuple)) else (bevel, 1, 35.0)
        mod = ob.modifiers.new("bevel", "BEVEL")
        mod.width = float(w)
        mod.segments = int(segs)
        mod.limit_method = "ANGLE"
        mod.angle_limit = math.radians(float(ang))
        mod.use_clamp_overlap = True
        mod.miter_outer = "MITER_ARC"
        apply_modifiers(ob)
    if smooth and sharp_deg:
        ob.data.set_sharp_from_angle(angle=math.radians(sharp_deg))
    return ob


def finalize_item(objs, name, uv_margin=0.01):
    """Join parts into one mesh object `name`, unwrap UVs (smart project) and return it."""
    objs = [o for o in objs if o is not None]
    ob = af.join(objs, name=name)
    ob.data.name = name
    # unwrap
    af.select_only([ob])
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.smart_project(angle_limit=math.radians(60), island_margin=uv_margin,
                             area_weight=0.0, correct_aspect=True, scale_to_bounds=False)
    bpy.ops.object.mode_set(mode="OBJECT")
    if ob.data.uv_layers:
        ob.data.uv_layers[0].name = "UVMap"
    return ob


def place_on_ground(ob, origin="bbox"):
    """Contract §4: lowest point on z = 0; X/Y centre at 0 ('bbox') or keep the main axis ('axis').
    Returns the applied translation."""
    ob.data.update()
    bpy.context.view_layer.update()  # bound_box is stale after mesh.transform() until the depsgraph runs
    lo, hi = af.world_bbox([ob])
    d = Vector((-(lo.x + hi.x) / 2 if origin == "bbox" else 0.0,
                -(lo.y + hi.y) / 2 if origin == "bbox" else 0.0, -lo.z))
    ob.data.transform(Matrix.Translation(d))
    ob.data.update()
    bpy.context.view_layer.update()
    return d


def add_item_socket(item, name, loc, rot3=None, kind=None, size=0.04, **extras):
    rot3 = rot3 if rot3 is not None else Matrix.Identity(3)
    emp = af.add_socket(name, parent=item, location=tuple(loc), rotation=(0, 0, 0), kind=kind,
                        size=size, **extras)
    emp.rotation_mode = "QUATERNION"
    emp.rotation_quaternion = rot3.to_quaternion()
    return emp


def socket_record(emp):
    bpy.context.view_layer.update()
    mw = emp.matrix_world
    r = mw.to_3x3().normalized()
    return {"name": emp.name, "kind": emp.get("kind"), "location": [round(x, 5) for x in mw.translation],
            "x_axis": [round(x, 4) for x in r.col[0]], "y_axis": [round(x, 4) for x in r.col[1]],
            "z_axis": [round(x, 4) for x in r.col[2]],
            "quaternion_wxyz": [round(x, 5) for x in r.to_quaternion()],
            "parent": emp.parent.name if emp.parent else None,
            "parent_bone": emp.parent_bone if emp.parent_type == "BONE" else None}


def find_sockets(objs=None):
    src = objs if objs is not None else bpy.context.scene.objects
    out = {}
    for o in src:
        nm = o.get("game_socket") or (o.name if o.name.startswith("socket_") else None)
        if nm:
            out[str(nm).split(".")[0]] = o
    return out


def unique_socket_name(base, taken=None):
    """Contract §6 socket names carry no dots: base, base_2, base_3 ... (not used by any object or
    game_socket in the scene)."""
    used = set(taken or ())
    for o in bpy.data.objects:
        used.add(o.name)
        if o.get("game_socket"):
            used.add(str(o["game_socket"]))
    if base not in used:
        return base
    k = 2
    while "%s_%d" % (base, k) in used:
        k += 1
    return "%s_%d" % (base, k)


def rename_socket(emp, new):
    """Rename a socket empty (object name and extras.game_socket) to a dot-free unique name."""
    if emp.name == new:
        emp["game_socket"] = new
        return new
    emp.name = unique_socket_name(new)
    emp["game_socket"] = emp.name
    return emp.name


def matrix_rows(M):
    return [[round(float(x), 6) for x in row] for row in M]


def flat16(M):
    return [round(float(M[i][j]), 6) for i in range(4) for j in range(4)]


# --------------------------------------------------------------------------- mesh statistics

def mesh_arrays(ob, world=True, evaluated=True):
    """(verts Nx3, tris Mx3, tri_material M) of an object (evaluated, world space)."""
    dg = bpy.context.evaluated_depsgraph_get()
    src = ob.evaluated_get(dg) if evaluated else ob
    me = src.to_mesh()
    try:
        me.calc_loop_triangles()
        nv = len(me.vertices)
        co = np.empty(nv * 3, dtype=np.float64)
        me.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        nt = len(me.loop_triangles)
        tri = np.empty(nt * 3, dtype=np.int64)
        me.loop_triangles.foreach_get("vertices", tri)
        tri = tri.reshape(-1, 3)
        tmat = np.empty(nt, dtype=np.int64)
        me.loop_triangles.foreach_get("material_index", tmat)
    finally:
        src.to_mesh_clear()
    if world:
        M = np.array(ob.matrix_world)
        co = co @ M[:3, :3].T + M[:3, 3]
    return co, tri, tmat


def non_manifold_edges(ob):
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    n = sum(1 for e in bm.edges if not e.is_manifold)
    bm.free()
    return n


def pca_axes(points, weights=None):
    """Principal axes (columns, descending variance), centroid, extents along the axes."""
    P = np.asarray(points, dtype=np.float64)
    w = np.ones(len(P)) if weights is None else np.asarray(weights, dtype=np.float64)
    c = (P * w[:, None]).sum(0) / w.sum()
    Q = P - c
    C = (Q * w[:, None]).T @ Q / w.sum()
    vals, vecs = np.linalg.eigh(C)
    order = np.argsort(vals)[::-1]
    vecs = vecs[:, order]
    if np.linalg.det(vecs) < 0:
        vecs[:, 2] *= -1
    proj = Q @ vecs
    return vecs, c, proj.min(0), proj.max(0)


def decimate_to(ob, budget, min_ratio=0.002):
    """Collapse-decimate a mesh object until its triangle count is <= budget. Returns (before, after)."""
    before = af.tri_count([ob])
    if before <= budget:
        return before, before
    ratio = budget / float(before)
    for _ in range(6):
        mod = ob.modifiers.new("decimate", "DECIMATE")
        mod.decimate_type = "COLLAPSE"
        mod.ratio = max(min_ratio, ratio * 0.985)
        mod.use_collapse_triangulate = True
        apply_modifiers(ob)
        now = af.tri_count([ob])
        if now <= budget:
            return before, now
        ratio = budget / float(now)
    return before, af.tri_count([ob])


def downscale_images(objs, max_px):
    done = []
    for ob in af.meshes(objs):
        for slot in ob.material_slots:
            m = slot.material
            if not m or not m.use_nodes:
                continue
            for n in m.node_tree.nodes:
                if n.type == "TEX_IMAGE" and n.image and max(n.image.size) > max_px:
                    img = n.image
                    w, h = img.size
                    s = max_px / float(max(w, h))
                    img.scale(max(1, int(w * s)), max(1, int(h * s)))
                    img.pack()
                    done.append([img.name, [w, h], list(img.size)])
    return done


# --------------------------------------------------------------------------- GLB inspection

def glb_json(path):
    """Read the JSON chunk of a .glb (no import) -> dict."""
    with open(path, "rb") as fh:
        head = fh.read(12)
        magic, ver, length = struct.unpack("<4sII", head)
        if magic != b"glTF":
            raise ValueError("not a GLB: %s" % path)
        clen, ctype = struct.unpack("<II", fh.read(8))
        return json.loads(fh.read(clen).decode("utf-8"))


def glb_summary(path):
    g = glb_json(path)
    nodes = g.get("nodes", [])
    sockets = {}
    for n in nodes:
        ex = n.get("extras") or {}
        if "game_socket" in ex:
            sockets[ex["game_socket"]] = {"node": n.get("name"), "rotation": n.get("rotation"),
                                          "translation": n.get("translation")}
    return {"nodes": len(nodes), "meshes": len(g.get("meshes", [])),
            "materials": [m.get("name") for m in g.get("materials", [])],
            "animations": [a.get("name") for a in g.get("animations", [])],
            "skins": len(g.get("skins", [])), "sockets": sockets,
            "images": len(g.get("images", [])), "bytes": os.path.getsize(path)}


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
    "#": "01010 01010 11111 01010 11111 01010 01010", "+": "00000 00100 00100 11111 00100 00100 00000",
}


def stamp_text(img, text, x, y_top, scale=2, color=(1, 1, 1, 1)):
    """Draw text into a top-down RGBA float image array (H, W, 4)."""
    H, W = img.shape[:2]
    cx = x
    for ch in str(text).upper():
        g = FONT5x7.get(ch, FONT5x7[" "]).split()
        for r, row in enumerate(g):
            for c, bit in enumerate(row):
                if bit == "1":
                    y0 = y_top + r * scale
                    x0 = cx + c * scale
                    if 0 <= y0 < H - scale and 0 <= x0 < W - scale:
                        img[y0:y0 + scale, x0:x0 + scale] = color
        cx += 6 * scale
    return img


def load_png(path):
    im = bpy.data.images.load(path, check_existing=False)
    px = np.array(im.pixels[:], dtype=np.float32).reshape(im.size[1], im.size[0], 4)[::-1]
    bpy.data.images.remove(im)
    return px  # top-down


def save_png(arr, path):
    """arr: top-down RGBA float (H, W, 4)."""
    h, w = arr.shape[:2]
    im = bpy.data.images.new("af_items_out", width=w, height=h, alpha=True)
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


def resize_nearest(arr, w, h):
    H, W = arr.shape[:2]
    ys = (np.arange(h) * H / h).astype(int)
    xs = (np.arange(w) * W / w).astype(int)
    return arr[ys][:, xs]


def contact_sheet(tiles, labels, path, cols=5, tile=256, bg=(0.16, 0.17, 0.19), label_h=34,
                  title=None, scales=None):
    """tiles: list of top-down RGBA arrays; each is fitted into `tile` px and labelled. scales:
    optional per-tile factor <= 1 (common-scale sheets: tile extent / largest extent)."""
    rows = int(math.ceil(len(tiles) / float(cols)))
    top = 28 if title else 0
    H = top + rows * (tile + label_h)
    W = cols * tile
    sheet = np.zeros((H, W, 4), dtype=np.float32)
    sheet[..., :3] = bg
    sheet[..., 3] = 1
    if title:
        stamp_text(sheet, title, 8, 6, 2, (0.95, 0.85, 0.55, 1))
    for i, (t, lab) in enumerate(zip(tiles, labels)):
        r, c = divmod(i, cols)
        th, tw = t.shape[:2]
        s = min(tile / float(tw), tile / float(th)) * (float(scales[i]) if scales else 1.0)
        t2 = resize_nearest(over_bg(t, bg), max(1, int(tw * s)), max(1, int(th * s)))
        y0 = top + r * (tile + label_h) + (tile - t2.shape[0]) // 2
        x0 = c * tile + (tile - t2.shape[1]) // 2
        sheet[y0:y0 + t2.shape[0], x0:x0 + t2.shape[1]] = t2
        lines = lab if isinstance(lab, (list, tuple)) else [lab]
        for k, line in enumerate(lines[:2]):
            stamp_text(sheet, line[:20], c * tile + 6, top + r * (tile + label_h) + tile + 2 + k * 16, 2,
                       (0.92, 0.92, 0.92, 1) if k == 0 else (0.6, 0.75, 0.9, 1))
    return save_png(sheet, path)


def stack_rows(paths, out, bg=(0.16, 0.17, 0.19), labels=None, scale=1.0):
    rows = [over_bg(load_png(p), bg) for p in paths]
    W = max(r.shape[1] for r in rows)
    pad = 20 if labels else 0
    canvas = []
    for i, r in enumerate(rows):
        if r.shape[1] < W:
            fill = np.zeros((r.shape[0], W - r.shape[1], 4), np.float32)
            fill[..., :3] = bg
            fill[..., 3] = 1
            r = np.concatenate([r, fill], axis=1)
        if labels:
            band = np.zeros((pad, W, 4), np.float32)
            band[..., :3] = bg
            band[..., 3] = 1
            stamp_text(band, labels[i], 6, 3, 2, (0.95, 0.85, 0.55, 1))
            canvas.append(band)
        canvas.append(r)
    arr = np.concatenate(canvas, axis=0)
    if scale != 1.0:
        arr = resize_nearest(arr, int(arr.shape[1] * scale), int(arr.shape[0] * scale))
    return save_png(arr, out)


def render_region(path, lo, hi, views=("front",), size=384, margin=1.05):
    """Render whatever is in the scene inside the box lo..hi (a hidden framing mesh)."""
    v, f = box(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2],
               ((lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, (lo[2] + hi[2]) / 2))
    me = bpy.data.meshes.new("af_frame_box")
    me.from_pydata(v, [], f)
    ob = link(bpy.data.objects.new("af_frame_box", me))
    ob.hide_render = True
    try:
        af.render_views(path, [ob], views=tuple(views), size=size, margin=margin)
    finally:
        bpy.data.objects.remove(ob)
        bpy.data.meshes.remove(me)
    return path


def render_tile(path, objects, view="three_quarter", size=384, rot_z_deg=0.0, margin=1.08):
    """Render one view of objects, optionally rotating them about Z (render only). Returns
    (path, extent): extent is the bbox diagonal af.render_views framed (metres per image width
    = extent * margin), so tiles can be put on a common scale."""
    roots = [o for o in objects if o.parent is None or o.parent not in objects]
    saved = [(o, o.matrix_world.copy()) for o in roots]
    if rot_z_deg:
        R = Matrix.Rotation(math.radians(rot_z_deg), 4, "Z")
        for o, mw in saved:
            o.matrix_world = R @ mw
    bpy.context.view_layer.update()
    try:
        lo, hi = af.world_bbox(objects)
        extent = max((hi - lo).length, 1e-3)
        af.render_views(path, objects, views=(view,), size=size, margin=margin)
    finally:
        for o, mw in saved:
            o.matrix_world = mw
        bpy.context.view_layer.update()
    return path, float(extent)


# --------------------------------------------------------------------------- characters

HAND_BONES = {"r": ("mixamorig:RightHand", "mixamorig:RightForeArm", "mixamorig:RightArm", "Right"),
              "l": ("mixamorig:LeftHand", "mixamorig:LeftForeArm", "mixamorig:LeftArm", "Left")}


def find_bone(arm, name):
    """Bone lookup tolerant to 'mixamorig:' vs 'mixamorig1:' and prefix-less rigs."""
    if name in arm.data.bones:
        return arm.data.bones[name]
    short = name.split(":")[-1]
    for b in arm.data.bones:
        if b.name.split(":")[-1] == short:
            return b
    return None


def skinned_meshes(arm, objs=None):
    src = objs if objs is not None else bpy.context.scene.objects
    out = []
    for o in src:
        if o.type != "MESH":
            continue
        if any(m.type == "ARMATURE" and m.object == arm for m in o.modifiers) or \
                (o.parent == arm and o.parent_type in ("ARMATURE", "OBJECT") and o.vertex_groups):
            out.append(o)
    return out


def group_weights(ob, names):
    """Per-vertex max weight over vertex groups whose names are in `names` (numpy array)."""
    idx = {g.index for g in ob.vertex_groups if g.name in names}
    w = np.zeros(len(ob.data.vertices), dtype=np.float32)
    if not idx:
        return w
    for v in ob.data.vertices:
        for ge in v.groups:
            if ge.group in idx and ge.weight > w[v.index]:
                w[v.index] = ge.weight
    return w


def side_group_names(ob, side_word, parts):
    """Vertex group names of a side whose short name starts with side+part (e.g. 'RightHand*')."""
    out = set()
    for g in ob.vertex_groups:
        short = g.name.split(":")[-1]
        for p in parts:
            if short.startswith(side_word + p):
                out.add(g.name)
    return out


def char_forward(arm):
    """Character forward in world (contract: -Y), rotated by the armature object."""
    f = arm.matrix_world.to_3x3() @ Vector((0, -1, 0))
    f.z = 0
    return f.normalized() if f.length > 1e-6 else Vector((0, -1, 0))


GRIP_DIR_NAMES = ("up", "down", "forward", "back", "out", "in")


def grip_dir_vector(spec, side):
    """A grip direction for hand_socket_frame: [x, y, z] or a name in the character frame (faces -Y,
    left = +X): up, down, forward (-Y), back (+Y), out (away from the body: +X for the left hand, -X
    for the right) and in. Returns a unit Vector."""
    if isinstance(spec, str):
        name = spec.lower()
        sx = 1.0 if side in ("l", "Left") else -1.0
        table = {"up": (0, 0, 1), "down": (0, 0, -1), "forward": (0, -1, 0), "back": (0, 1, 0),
                 "out": (sx, 0, 0), "in": (-sx, 0, 0)}
        if name not in table:
            raise ValueError("grip direction must be [x, y, z] or one of %s" % ", ".join(GRIP_DIR_NAMES))
        return Vector(table[name])
    v = Vector([float(x) for x in spec])
    if v.length < 1e-9:
        raise ValueError("grip direction must not be zero")
    return v.normalized()


def hand_socket_frame(arm, body, side, thumb_hint=None, along=0.42, palm_gap=None, grip_dir=None,
                      exclude=None):
    """Rest-pose frame of socket_mount_hand_<side> (world space) from the hand bone and the mesh.

    The one hand frame of the forge: attach_prop (ensure_hand_socket), rig_humanoid and
    import_mixamo_rig (_humanoid_lib.add_human_sockets) all build socket_mount_hand_* here.
    Shared grip frame: +Z out of the fist on the thumb side, -Y along the knuckles (hand bone
    direction projected perpendicular to +Z), X = Y x Z. The centre sits on the palm side of the
    hand axis, `along` of the hand length from the wrist.
    Thumb side: character forward projected perpendicular to the hand axis (Mixamo A/T poses have
    the thumbs forward) unless thumb_hint (world vector) is given; a prop baked into the hand mesh
    (hand-weighted vertices far from the hand axis) gives +Z along its long axis.
    grip_dir: world direction of a prop the caller knows is held in this hand (rig_humanoid's grip
    rule): +Z is exactly this direction (like a baked prop, without guessing it from the vertices).
    exclude: boolean mask over the body vertices that are not hand flesh (props rigid to the hand);
    they do not count for the hand length, the palm thickness or the baked-prop guess.
    Returns (Matrix 4x4 world, info dict)."""
    hand_name, fore_name, _, word = HAND_BONES[side]
    hb = find_bone(arm, hand_name)
    if hb is None:
        raise ValueError("hand bone %s not found" % hand_name)
    mw = arm.matrix_world
    wrist = mw @ hb.head_local
    axis = ((mw.to_3x3() @ (hb.tail_local - hb.head_local))).normalized()
    fb = find_bone(arm, fore_name)
    if fb is not None:
        fa = (mw.to_3x3() @ (fb.tail_local - fb.head_local)).normalized()
        # Mixamo leaf hand bones keep the forearm direction; average for robustness
        axis = (axis * 0.7 + fa * 0.3).normalized()
    hint = Vector(thumb_hint).normalized() if thumb_hint else char_forward(arm)
    thumb = hint - axis * hint.dot(axis)
    if thumb.length < 1e-4:
        thumb = Vector((0, 0, 1)) - axis * axis.z
    thumb.normalize()
    # mesh: vertices of the hand
    info = {"bone": hb.name, "thumb_source": "hint" if thumb_hint else "character_forward"}
    L = hb.length
    q = d = None
    if body is not None:
        names = side_group_names(body, word, ("Hand",))
        w = group_weights(body, names)
        sel = np.where(w > 0.5)[0]
        if exclude is not None and len(sel):
            ex = np.asarray(exclude, dtype=bool)
            info["excluded_vertices"] = int(ex[sel].sum())
            sel = sel[~ex[sel]]
        if len(sel) >= 12:
            co = np.array([tuple(body.matrix_world @ body.data.vertices[i].co) for i in sel])
            q = co - np.array(wrist)
            d = q @ np.array(axis)
            L = float(np.percentile(d, 98)) if np.percentile(d, 98) > 0.02 else hb.length
            info["hand_vertices"] = int(len(sel))
            info["hand_length_mesh"] = round(L, 4)
            # a prop baked into the hand mesh (e.g. a generated character holding a sword) shows up as
            # hand-weighted vertices far from the hand axis: its long axis is the real fist-hole axis
            perp = q - np.outer(q @ np.array(axis), np.array(axis))
            rad_ = np.linalg.norm(perp, axis=1)
            far = q[rad_ > 1.2 * max(L, 0.05)]
            if thumb_hint is None and grip_dir is None and len(far) >= 6:
                u_, s_, vt = np.linalg.svd(far - far.mean(0), full_matrices=False)
                dirp = Vector(vt[0]).normalized()
                proj = (far - np.array(axis) * L * along) @ np.array(dirp)  # from the fist centre
                if abs(np.percentile(proj, 98)) < abs(np.percentile(proj, 2)):
                    dirp = -dirp  # the longer part (blade) exits on the thumb side
                if abs(dirp.dot(axis)) < 0.9:
                    thumb = dirp
                    info["baked_prop"] = {"vertices": int(len(far)), "thumb_from_prop": list(dirp),
                                          "extent_m": round(float(rad_.max()), 3)}
                    info["thumb_source"] = "baked_prop_axis"
    if grip_dir is not None:
        thumb = Vector(grip_dir).normalized()
        info["thumb_source"] = "grip_dir"
    zax = thumb
    yv = axis - zax * axis.dot(zax)
    if yv.length < 0.3:
        # a held prop (nearly) along the hand axis: knuckles from the character front instead
        f = char_forward(arm)
        yv = f - zax * f.dot(zax)
        if yv.length < 1e-4:
            yv = Vector((0, 0, 1)) - zax * zax.z
        info["knuckles_from"] = "character_forward"
    yax = -yv.normalized()
    xax = yax.cross(zax)
    # palm side: X of the frame; left hand palm = -X, right hand palm = +X (see module doc)
    palm_dir = xax if side == "r" else -xax
    if q is not None:
        band = (d > L * (along - 0.15)) & (d < L * (along + 0.15))
        if band.sum() >= 4:
            ext = q[band] @ np.array(palm_dir)
            pos = ext[ext > 0]
            # robust palm thickness: thumbs and curled claws give outliers along the palm normal
            info["palm_extent"] = round(float(np.percentile(pos, 60)) if len(pos) >= 3 else 0.012, 4)
            info["palm_extent_max"] = round(float(ext.max()), 4)
            info["back_extent"] = round(float(-ext.min()), 4)
    if palm_gap is None:
        sc = L / 0.19 if L > 0.05 else 1.0  # hand length of a 1.8 m human ~ 0.19 m
        pe = info.get("palm_extent", 0.013 * sc)
        pe = min(max(pe, 0.008 * sc), 0.03 * sc)
        # grip axis = palm surface + one grip radius (~1.5 cm)
        palm_gap = pe + 0.015 * sc
    centre = wrist + axis * (L * along) + palm_dir * palm_gap
    info.update({"wrist": list(wrist), "hand_axis": list(axis), "palm_dir": list(palm_dir),
                 "along": along, "palm_gap": round(palm_gap, 4)})
    return mat4(Matrix((xax, yax, zax)).transposed(), centre), info


def ensure_hand_socket(arm, body, side, **kw):
    name = "socket_mount_hand_%s" % side
    existing = find_sockets().get(name)
    if existing is not None:
        return existing, {"created": False}
    M, info = hand_socket_frame(arm, body, side, **kw)
    hand = find_bone(arm, HAND_BONES[side][0])
    emp = af.add_socket(name, parent=arm, bone=hand.name, kind="mount", size=0.05)
    emp.rotation_mode = "QUATERNION"
    bpy.context.view_layer.update()
    emp.matrix_world = M
    bpy.context.view_layer.update()
    info["created"] = True
    return emp, info


def parent_keep(ob, parent, bone=None):
    mw = ob.matrix_world.copy()
    ob.parent = parent
    if bone:
        ob.parent_type = "BONE"
        ob.parent_bone = bone
    else:
        ob.parent_type = "OBJECT"
    bpy.context.view_layer.update()
    ob.matrix_world = mw
    bpy.context.view_layer.update()
    return ob


# --------------------------------------------------------------------------- clips

def fcurves_of(action):
    """Curves of a Blender 4.4+/5.0 slotted action (ported from an earlier in-house rig script)."""
    out = []
    for layer in action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                out.extend(bag.fcurves)
    return out


def assign_action(obj, action):
    """Assign action with its slot (Blender 5.0)."""
    if obj.animation_data is None:
        obj.animation_data_create()
    obj.animation_data.action = action
    if getattr(obj.animation_data, "action_slot", None) is None and len(action.slots):
        obj.animation_data.action_slot = action.slots[0]


LOOP_PREFIXES = ("idle", "walk", "run")


def clip_spec(entry):
    """inputs.clips entry: a path or {"file", "name", "loop"} -> (path, action name).
    Contract §7: looping clips carry the '-loop' suffix; loop defaults to true for clip names that
    start with idle / walk / run (idle, idle_2, walk-fast, run ...). Names already ending in '-loop'
    are kept."""
    import re
    if isinstance(entry, dict):
        path = entry.get("file") or entry.get("path")
        name = entry.get("name")
        loop = entry.get("loop")
    else:
        path, name, loop = entry, None, None
    if not path:
        raise ValueError("clip entry without a file: %r" % (entry,))
    name = str(name or os.path.splitext(os.path.basename(path))[0])
    base = name[:-5] if name.endswith("-loop") else name
    if loop is None:
        loop = bool(re.match(r"^(%s)($|[^a-z])" % "|".join(LOOP_PREFIXES), base.lower())) or name.endswith("-loop")
    name = (base + "-loop") if loop else base
    return path, name


def import_clip(path, name=None):
    """Import the longest action of an FBX/GLB clip file; delete imported objects. An older action
    with the same name (a clip re-imported onto a step's own output) is replaced."""
    before_obj = set(bpy.data.objects)
    before_act = set(bpy.data.actions)
    ext = os.path.splitext(path)[1].lower()
    if ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path, automatic_bone_orientation=False)
    elif ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    else:
        raise ValueError("unsupported clip format: %s" % path)
    new_objs = [o for o in bpy.data.objects if o not in before_obj]
    new_acts = [a for a in bpy.data.actions if a not in before_act]
    meshes_removed = 0
    for o in new_objs:
        if o.type == "MESH":
            meshes_removed += 1
        bpy.data.objects.remove(o, do_unlink=True)
    if not new_acts:
        raise ValueError("no animation in %s" % path)
    act = max(new_acts, key=lambda a: a.frame_range[1] - a.frame_range[0])
    for a in new_acts:
        if a is not act:
            bpy.data.actions.remove(a)
    name = name or os.path.splitext(os.path.basename(path))[0]
    old = bpy.data.actions.get(name)
    if old is not None and old is not act:
        for ob in bpy.data.objects:
            ad = ob.animation_data
            if ad is not None and ad.action == old:
                ad.action = None
        bpy.data.actions.remove(old)
    act.name = name
    act.use_fake_user = True
    return act


def sample_frames(action, count):
    a0, a1 = action.frame_range
    if count <= 1:
        return [int(round(a0))]
    return sorted({int(round(a0 + (a1 - a0) * k / (count - 1))) for k in range(count)})


# --------------------------------------------------------------------------- shields

SHIELD_MOUNT = {"r": "socket_mount_shield_r", "l": "socket_mount_shield_l"}


def shield_proxy(scale=1.0):
    """Canonical shield in grip space (contract §6 grip frame for shields: +Z up, -Y face, grip on
    the back-centre handle). Elliptic board 0.54 x 0.80 m (centre 5 cm below the grip), 16 mm thick,
    its back 32 mm in front of the grip, plus the kit's bow handle around the grip point. attach_prop
    fits socket_mount_shield_<side> against this proxy once per character and hand, so every shield
    (kit or item_from_mesh) reuses the same mount. Returns (co N x 3, tri M x 3) numpy arrays."""
    outline = [(x, z - 0.05) for (x, z) in ellipse(0.27, 0.40, 24)]
    board = plate(outline, 0.016, axes="XZ", centre=-0.040)
    so = 0.032
    hpath = [(0.0, -so - 0.002, -0.065), (0.0, -so * 0.2, -0.05), (0.0, 0.0, -0.02), (0.0, 0.0, 0.02),
             (0.0, -so * 0.2, 0.05), (0.0, -so - 0.002, 0.065)]
    handle = sweep(catmull(hpath, 2), 0.011, 8, up_hint=(1, 0, 0))
    V, F = merge(board, handle)
    tris = []
    for f in F:
        for k in range(1, len(f) - 1):
            tris.append((f[0], f[k], f[k + 1]))
    return np.array(V, dtype=np.float64) * float(scale), np.array(tris, dtype=np.int64)


# --------------------------------------------------------------------------- mount fit (shared)
# Used by attach_prop (rest + clip frames) and by rig_humanoid / import_mixamo_rig through
# _humanoid_lib.add_human_sockets (rest pose only): one implementation of the canonical shield fit.

CANON_UP = Vector((0, 0, 1))     # contract §6 grip frame of a shield: +Z up ...
CANON_FACE = Vector((0, -1, 0))  # ... and -Y = face


def normalized4(M):
    R = M.to_3x3().normalized().to_4x4()
    R.translation = M.translation
    return R


def to_world(M, co):
    A = np.array(M)
    return co @ A[:3, :3].T + A[:3, 3]


class Body:
    """Static data of the skinned meshes: vertex masks for hands / forearms / upper arms."""

    def __init__(self, bodies):
        self.bodies = bodies
        self.masks = {}
        for side, word in (("r", "Right"), ("l", "Left")):
            hand, fore, arm_ = [], [], []
            for b in bodies:
                hand.append(group_weights(b, side_group_names(b, word, ("Hand",))))
                fore.append(group_weights(b, side_group_names(b, word, ("ForeArm",))))
                arm_.append(group_weights(b, side_group_names(b, word, ("Arm",))))
            self.masks[side] = {"hand": np.concatenate(hand), "forearm": np.concatenate(fore),
                                "arm": np.concatenate(arm_)}

    def arrays(self):
        cos, tris, off = [], [], 0
        for b in self.bodies:
            co, tri, _ = mesh_arrays(b)
            cos.append(co)
            tris.append(tri + off)
            off += len(co)
        return np.concatenate(cos), np.concatenate(tris)


def rest_height(bodies):
    """Character height as attach_prop measures it: z extent of the skinned meshes (current pose;
    call with the armature in REST)."""
    zs = np.concatenate([mesh_arrays(b)[0][:, 2] for b in bodies])
    return float(zs.max() - zs.min())


def pose_record(arm, body, hand_socks, clip="rest", frame=None):
    """Body triangles + hand socket frames of the current pose (attach_prop's per-pose QA input)."""
    from mathutils.bvhtree import BVHTree
    spine = find_bone(arm, "mixamorig:Spine2") or find_bone(arm, "mixamorig:Spine1")
    co, tri = body.arrays()
    H = {s: normalized4(e.matrix_world.copy()) for s, e in hand_socks.items()}
    pb = arm.pose.bones.get(spine.name) if spine else None
    chest = (arm.matrix_world @ pb.head) if pb else arm.matrix_world.translation
    return {"clip": clip, "frame": frame, "H": H, "chest": chest.copy(), "co": co, "tri": tri,
            "bvh": BVHTree.FromPolygons(co.tolist(), tri.tolist(), all_triangles=True)}


def evaluate_pose(att, pose, O, scale_char, body_masks, want_rays=True):
    """QA numbers of one attachment in one pose. O: socket-space 4x4 (offset @ fit)."""
    from mathutils.bvhtree import BVHTree
    side = att["hand"]
    H = pose["H"][side]
    M = H @ O
    co_w = to_world(M, att["co_g"])
    ibvh = BVHTree.FromPolygons(co_w.tolist(), att["tri_g"].tolist(), all_triangles=True)
    bbvh = pose["bvh"]
    masks = body_masks[side]
    tri = pose["tri"]
    pairs = ibvh.overlap(bbvh)
    body_tris = {b for (_, b) in pairs}
    hand_t, fore_t, other_t = 0, 0, 0
    for t in body_tris:
        vs = tri[t]
        if masks["hand"][vs].max() > 0.5:
            hand_t += 1
        elif masks["forearm"][vs].max() > 0.4 or masks["arm"][vs].max() > 0.4:
            fore_t += 1
        else:
            other_t += 1
    res = {"clip": pose["clip"], "frame": pose["frame"], "overlap_hand_tris": hand_t,
           "overlap_arm_tris": fore_t, "overlap_body_tris": other_t}
    # blade / shaft inside the arm or body (sample the item's main axis beyond the fist)
    grip = M.translation
    ax = (M.to_3x3() @ Vector((0, 0, 1))).normalized()
    zmax, zmin = float(att["co_g"][:, 2].max()), float(att["co_g"][:, 2].min())
    inside_arm, inside_body, n_s = 0, 0, 0
    clear = 0.07 * scale_char
    spans = () if att["is_shield"] else ((clear, zmax), (-clear, zmin))  # shields: BVH overlap only
    for (z0, z1) in spans:
        if abs(z1) <= abs(z0):
            continue
        n = max(2, int(abs(z1 - z0) / 0.02))
        for k in range(n + 1):
            z = z0 + (z1 - z0) * k / n
            p = grip + ax * z
            hit = bbvh.find_nearest(p, 0.3)
            if hit[0] is None:
                continue
            n_s += 1
            if (p - hit[0]).dot(hit[1]) < 0:
                vs = tri[hit[2]]
                if masks["forearm"][vs].max() > 0.4 or masks["arm"][vs].max() > 0.4:
                    inside_arm += 1
                elif masks["hand"][vs].max() <= 0.5:
                    inside_body += 1
    res.update({"axis_samples": n_s, "axis_inside_arm": inside_arm, "axis_inside_body": inside_body})
    if want_rays:
        hmask = masks["hand"][tri].max(axis=1) > 0.5
        if hmask.any():
            hb = BVHTree.FromPolygons(pose["co"].tolist(), tri[hmask].tolist(), all_triangles=True)
            dmin = 1e9
            hits, rays = 0, 0
            xa = (M.to_3x3() @ Vector((1, 0, 0))).normalized()
            ya = (M.to_3x3() @ Vector((0, 1, 0))).normalized()
            for dz in (-0.03, 0.0, 0.03):
                p = grip + ax * dz * scale_char
                h = hb.find_nearest(p, 0.5)
                if h[0] is not None:
                    dmin = min(dmin, h[3])
                for k in range(12):
                    a = 2 * math.pi * k / 12
                    dv = xa * math.cos(a) + ya * math.sin(a)
                    rays += 1
                    if hb.ray_cast(p, dv, 0.06 * scale_char)[0] is not None:
                        hits += 1
            res["grip_to_hand_m"] = round(dmin, 4)
            res["grip_enclosure"] = round(hits / float(max(1, rays)), 3)
    if att["is_shield"]:
        up = (M.to_3x3() @ att["up_g"]).normalized()
        face = (M.to_3x3() @ att["face_g"]).normalized()
        out = grip - pose["chest"]
        out.z = 0
        out = out.normalized() if out.length > 1e-6 else Vector((0, -1, 0))
        res["shield_up_dot"] = round(up.z, 3)
        res["shield_face_out_dot"] = round(face.dot(out), 3)
    return res


def fibonacci_sphere(n):
    i = np.arange(n) + 0.5
    phi = np.arccos(1 - 2 * i / n)
    th = math.pi * (1 + 5 ** 0.5) * i
    return np.stack([np.cos(th) * np.sin(phi), np.sin(th) * np.sin(phi), np.cos(phi)], axis=1)


def fit_rotation(att, poses, mode, O_user, scale_char, body_masks, w_rest=0.35, n_dirs=320, n_roll=24,
                 clip_weight=None, front_bias=0.0):
    """Rotation R (socket space, about the grip) maximising upright + outward over the poses.
    clip_weight: optional f(clip name) -> weight of that clip's frames (0 drops them, e.g. a death on the floor).
    front_bias: "outward" leans toward the character's front (-Y) by this much (0: straight away from the chest -
    a hand hanging at the side then shows the board edge-on to the camera; 1: diagonal front-out)."""
    use = [p for p in poses if p["clip"] == "rest"] if mode == "rest" else poses
    if clip_weight is not None and mode != "rest":
        use = [p for p in use if p["clip"] == "rest" or clip_weight(p["clip"]) > 0]
    side = att["hand"]
    Hs = np.array([np.array((p["H"][side] @ O_user).to_3x3()) for p in use])          # K x 3 x 3
    outs = []
    for p in use:
        g = (p["H"][side] @ O_user).translation
        o = g - p["chest"]
        o.z = 0
        o = o.normalized() if o.length > 1e-6 else Vector((0, -1, 0))
        if front_bias:
            o = (o + Vector((0, -front_bias, 0))).normalized() if (o + Vector((0, -front_bias, 0))).length > 1e-6 else o
        outs.append(list(o))
    outs = np.array(outs)
    if clip_weight is None:
        wts = np.array([w_rest if p["clip"] == "rest" else (1 - w_rest) / max(1, len(use) - 1) for p in use])
    else:
        raw = np.array([0.0 if p["clip"] == "rest" else float(clip_weight(p["clip"])) for p in use])
        raw = raw / raw.sum() * (1 - w_rest) if raw.sum() > 0 else raw
        wts = np.where(np.array([p["clip"] == "rest" for p in use]), w_rest, raw)
    if len(use) == 1:
        wts = np.array([1.0])
    up_g, face_g = att["up_g"], att["face_g"]
    Bg = np.array(frame_matrix(z=up_g, y=-face_g))                                   # item basis in socket space
    dirs = fibonacci_sphere(n_dirs)
    cands = []
    for u in dirs:
        u = Vector(u)
        ref = Vector((1, 0, 0)) if abs(u.x) < 0.9 else Vector((0, 1, 0))
        e1 = (ref - u * ref.dot(u)).normalized()
        e2 = u.cross(e1)
        for k in range(n_roll):
            a = 2 * math.pi * k / n_roll
            f = e1 * math.cos(a) + e2 * math.sin(a)       # target face direction (socket space)
            Bt = np.array(frame_matrix(z=u, y=-f))
            cands.append(Bt @ Bg.T)
    R = np.array(cands)                                                                  # C x 3 x 3
    upw = np.einsum("kij,cjl,l->cki", Hs, R, np.array(up_g))                            # C x K x 3
    facew = np.einsum("kij,cjl,l->cki", Hs, R, np.array(face_g))
    c_up = 1 - upw[..., 2]
    c_face = 1 - np.einsum("cki,ki->ck", facew, outs)
    cost = (c_up + 0.7 * c_face) @ wts
    order = np.argsort(cost)[:8]
    best, best_score, report = None, 1e9, []
    for ci in order:
        Rm = Matrix(R[ci].tolist()).to_4x4()
        O = O_user @ Rm
        pen = 0
        for p in use[:: max(1, len(use) // 8)]:
            r = evaluate_pose(att, p, O, scale_char, body_masks, want_rays=False)
            pen += r["overlap_arm_tris"] + r["overlap_body_tris"] + 2 * r["axis_inside_arm"]
        score = float(cost[ci]) + 0.004 * pen
        report.append({"cost": round(float(cost[ci]), 4), "penetration": pen, "score": round(score, 4)})
        if score < best_score:
            best, best_score = Rm, score
    return best, {"mode": mode, "poses": len(use), "candidates": len(R), "top": report,
                  "rotation_euler_deg": [round(math.degrees(a), 2) for a in best.to_euler("XYZ")]}


def fit_shield_mount(side, poses, fit_mode, s_char, body_masks, standoff="auto", w_rest=0.35, clip_weight=None,
                     front_bias=0.0):
    """Socket-space 4x4 of socket_mount_shield_<side> (relative to socket_mount_hand_<side>): rotation
    that keeps a canonical shield upright and facing outward over the poses, then a standoff along the
    face until the rest-pose hand clears the canonical board + handle (open hands). w_rest / clip_weight:
    see fit_rotation (attach_prop's extracted shields weigh the looping clips and drop the death)."""
    co, tri = shield_proxy(s_char)
    proxy = {"hand": side, "co_g": co, "tri_g": tri, "is_shield": True, "up_g": CANON_UP, "face_g": CANON_FACE}
    R_fit, info = fit_rotation(proxy, poses, fit_mode, Matrix.Identity(4), s_char, body_masks, w_rest=w_rest,
                               clip_weight=clip_weight, front_bias=front_bias)
    dirv = (R_fit.to_3x3() @ CANON_FACE).normalized()
    if standoff == "auto":
        k = 0
        for k in range(0, 13):
            T = Matrix.Translation(dirv * (0.01 * k * s_char))
            r = evaluate_pose(proxy, poses[0], T @ R_fit, s_char, body_masks, want_rays=False)
            if r["overlap_hand_tris"] == 0:
                break
        off = 0.01 * k * s_char
    else:
        off = float(standoff)
    info["standoff_m"] = round(off, 4)
    info["proxy"] = "canonical shield: board 0.54 x 0.80 m, 16 mm, back 32 mm in front of the grip, bow handle"
    return Matrix.Translation(dirv * off) @ R_fit, info


def create_shield_mount(arm, side, H_rest, O_mount, hand_emp, fit_info=None):
    """socket_mount_shield_<side>: child of the hand bone at hand socket x O_mount (contract §6)."""
    name = SHIELD_MOUNT[side]
    hand = find_bone(arm, HAND_BONES[side][0])
    emp = af.add_socket(name, parent=arm, bone=hand.name, kind="mount", size=0.06, fitted_for="shield",
                        base_socket=hand_emp.name, socket_space_matrix=flat16(O_mount))
    emp.rotation_mode = "QUATERNION"
    bpy.context.view_layer.update()
    emp.matrix_world = H_rest @ O_mount
    bpy.context.view_layer.update()
    # the same frame as extras on the hand socket (kept for readers of the first round's interim field)
    hand_emp["shield_mount_matrix"] = flat16(O_mount)
    hand_emp["shield_mount_socket"] = emp.name
    return emp


def rest_shield_mounts(arm, bodies, hand_socks, standoff="auto"):
    """Canonical shield mounts fitted on the REST pose only (what attach_prop does with fit 'rest'),
    once per character: {side: (empty, O_mount 4x4, info)}. Hands whose socket_mount_shield_<side>
    already exists are skipped (the existing mount is kept untouched)."""
    saved = arm.data.pose_position
    arm.data.pose_position = "REST"
    bpy.context.view_layer.update()
    try:
        s_char = rest_height(bodies) / 1.8
        body = Body(bodies)
        pose = pose_record(arm, body, hand_socks)
        existing = find_sockets()
        out = {}
        for side in sorted(hand_socks):
            if SHIELD_MOUNT[side] in existing:
                continue
            O, info = fit_shield_mount(side, [pose], "rest", s_char, body.masks, standoff)
            info["scale_char"] = round(s_char, 4)
            emp = create_shield_mount(arm, side, pose["H"][side], O, hand_socks[side], info)
            out[side] = (emp, O, info)
        return out
    finally:
        arm.data.pose_position = saved
        bpy.context.view_layer.update()


# --------------------------------------------------------------------------- BVH QA

def bvh_from_arrays(co, tri, mask_tris=None):
    from mathutils.bvhtree import BVHTree
    if mask_tris is not None:
        tri = tri[mask_tris]
    if len(tri) == 0:
        return None, tri
    return BVHTree.FromPolygons(co.tolist(), tri.tolist(), all_triangles=True), tri


def point_inside(bvh, p, max_dist=0.25):
    hit = bvh.find_nearest(Vector(p), max_dist)
    if hit[0] is None:
        return False, None
    loc, nrm, idx, dist = hit
    return (Vector(p) - loc).dot(nrm) < 0.0, dist
