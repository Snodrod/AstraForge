"""_ship_lib — shared kit of the AstraForge "ships" module (ship_gen, turret_gen, ship_from_mesh).

Contents
  * contract data: budgets per class, roles, AF_* material library, faction palettes.
  * Kit: one bmesh per build with a material index per face; primitives (box, cylinder, lathe, loft,
    airfoil loft) are created directly in it, bevelled / inset per part, mirrored across X = 0.
  * finalize: split by material into M_<part>_<material> objects, sharp edges by angle, UVs.
  * convex COL_SIMPLE (<= 64 verts), LOD decimation, socket empties with explicit frames.
  * glb_summary: a read-only GLB parser that counts triangles, surfaces, materials, nodes and sockets.
  * manifest helpers + a tiny JSON-schema (draft-07 subset) validator.
  * Workbench preview grid (perspective 3/4 views + ortho views) and a numpy PNG writer.

Axes (contract §4): Blender nose +Y, up +Z, starboard +X, mirror X = 0;
glTF (export_yup) nose -Z, up +Y. Root at the geometric centre of the hull.
Socket frames: turret/utility/docking/sensor/fx — Blender local +Z = outward normal, +Y = ship
forward (glTF local +Y out, -Z forward). Muzzle — identity frame of the pitch pivot: glTF local -Z = fire
direction. Thruster — selectable (THRUSTER_FRAMES, params.thruster_frame):
  thrust    (default) glTF local -Z = thrust direction (ship forward), exhaust along local +Z, +Y up
  exhaust   glTF local -Z = exhaust direction (the way the nozzle opening points, like a muzzle), +Y up
"""
import hashlib
import json
import math
import os
import random
import re
import struct
import shutil
import tempfile
import zlib

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector

import af_common as af

GEN_SHIP = "af_ship_gen_v1"
GEN_TURRET = "af_turret_gen_v1"
GEN_FROM_MESH = "af_ship_from_mesh_v1"

# ----------------------------------------------------------------------------- budgets
# Budget per class of the generic generators (ship_gen / ship_from_mesh / kitbash: ship_s/m/l, turret_gen: turret).
BUDGETS = {
    # nodes = node cap of the GLB; strict_nodes = a stricter recommended node count (warning only).
    # lod1/lod2 = absolute LOD caps (also at most 35 % / 12 % of LOD0).
    "ship_s": dict(tris=20000, lod1=6000, lod2=1500, surfaces=9, materials=9, mb=3.0, nodes=32,
                   t_surfaces=4, t_materials=4, strict_nodes=24),
    "ship_m": dict(tris=40000, lod1=12000, lod2=3000, surfaces=9, materials=9, mb=6.0, nodes=32,
                   t_surfaces=4, t_materials=4, strict_nodes=24),
    "ship_l": dict(tris=60000, lod1=18000, lod2=5000, surfaces=9, materials=9, mb=12.0, nodes=32,
                   t_surfaces=4, t_materials=4, strict_nodes=24),
    "turret": dict(tris=9500, lod1=2500, lod2=700, surfaces=12, materials=8, mb=8.0, nodes=16,
                   t_surfaces=6, t_materials=2, strict_nodes=12),
}
CLASS_LENGTH = {"ship_s": (5.0, 30.0), "ship_m": (30.0, 100.0), "ship_l": (100.0, 300.0)}

ROLES = {
    "fighter": dict(cls="ship_s", length=14.0, turrets=2, engines=(1, 2)),
    "interceptor": dict(cls="ship_s", length=18.0, turrets=2, engines=(2, 3)),
    "gunship": dict(cls="ship_m", length=42.0, turrets=4, engines=(2, 3)),
    "corvette": dict(cls="ship_m", length=70.0, turrets=4, engines=(3, 4)),
    "freighter": dict(cls="ship_m", length=60.0, turrets=2, engines=(2, 4)),
    "miner": dict(cls="ship_m", length=48.0, turrets=2, engines=(2, 2)),
    "frigate": dict(cls="ship_l", length=160.0, turrets=8, engines=(3, 5)),
    "carrier": dict(cls="ship_l", length=240.0, turrets=6, engines=(4, 6)),
    "transport": dict(cls="ship_l", length=200.0, turrets=4, engines=(3, 4)),
}
FACTIONS = ("industrial", "military", "sleek", "alien")

MATS = ["armor", "graphite", "trim", "panel", "glass", "status", "drive", "marking"]
MI = {k: i for i, k in enumerate(MATS)}
MAT_NAME = {"armor": "AF_Armor", "graphite": "AF_Graphite", "trim": "AF_SteelTrim",
            "panel": "AF_ServicePanel", "glass": "AF_Glass", "status": "AF_EmissiveStatus",
            "drive": "AF_EmissiveDrive", "marking": "AF_Marking"}
SHORT = {"armor": "armor", "graphite": "graphite", "trim": "steeltrim", "panel": "servicepanel",
         "glass": "glass", "status": "emissivestatus", "drive": "emissivedrive", "marking": "marking"}

# Material library values (rgb linear, metallic, roughness, emission, strength)
MAT_LIB = {
    "graphite": dict(rgb=(0.09, 0.105, 0.12), metal=0.58, rough=0.42),
    "armor": dict(rgb=(0.72, 0.71, 0.66), metal=0.26, rough=0.37),
    "trim": dict(rgb=(0.27, 0.30, 0.33), metal=0.65, rough=0.35),
    "panel": dict(rgb=(0.028, 0.037, 0.045), metal=0.45, rough=0.39),
    "glass": dict(rgb=(0.02, 0.065, 0.08), metal=0.22, rough=0.2),
    "status": dict(rgb=(0.02, 0.61, 0.86), metal=0.1, rough=0.25, emit=(0.02, 0.7, 1.0), strength=2.4),
    "drive": dict(rgb=(0.04, 0.57, 1.0), metal=0.0, rough=0.25, emit=(0.02, 0.59, 1.0), strength=4.0),
    "marking": dict(rgb=(0.75, 0.32, 0.06), metal=0.1, rough=0.4),
}

# Faction liveries: overrides of MAT_LIB colours (linear RGB). Names of materials never change, only values,
# so a fleet livery can swap one set for the whole fleet.
FACTION_PALETTES = {
    "military": [
        dict(armor=(0.30, 0.33, 0.35), graphite=(0.045, 0.05, 0.055), marking=(0.55, 0.06, 0.03),
             status=(1.0, 0.16, 0.06), drive=(0.35, 0.62, 1.0)),
        dict(armor=(0.27, 0.3, 0.21), graphite=(0.045, 0.05, 0.04), marking=(0.66, 0.46, 0.1),
             status=(1.0, 0.3, 0.05), drive=(1.0, 0.45, 0.12)),
        dict(armor=(0.13, 0.17, 0.25), graphite=(0.035, 0.04, 0.05), marking=(0.75, 0.75, 0.72),
             status=(1.0, 0.16, 0.06), drive=(0.4, 0.7, 1.0)),
    ],
    "industrial": [
        dict(armor=(0.62, 0.38, 0.06), graphite=(0.06, 0.06, 0.06), marking=(0.02, 0.02, 0.02),
             status=(1.0, 0.55, 0.08), drive=(1.0, 0.42, 0.1), trim=(0.32, 0.31, 0.29)),
        dict(armor=(0.58, 0.56, 0.5), graphite=(0.08, 0.075, 0.07), marking=(0.78, 0.48, 0.02),
             status=(1.0, 0.55, 0.08), drive=(1.0, 0.5, 0.15)),
        dict(armor=(0.42, 0.12, 0.06), graphite=(0.07, 0.065, 0.06), marking=(0.8, 0.7, 0.55),
             status=(1.0, 0.65, 0.1), drive=(1.0, 0.42, 0.1)),
    ],
    "sleek": [
        dict(armor=(0.8, 0.82, 0.85), graphite=(0.06, 0.08, 0.11), marking=(0.03, 0.2, 0.75),
             status=(0.05, 0.75, 1.0), drive=(0.05, 0.6, 1.0)),
        dict(armor=(0.72, 0.71, 0.66), graphite=(0.09, 0.105, 0.12), marking=(0.75, 0.32, 0.06)),
        dict(armor=(0.05, 0.06, 0.08), graphite=(0.5, 0.52, 0.55), marking=(0.85, 0.08, 0.05),
             status=(1.0, 0.1, 0.1), drive=(1.0, 0.25, 0.2)),
    ],
    "alien": [
        dict(armor=(0.16, 0.08, 0.22), graphite=(0.03, 0.02, 0.045), marking=(0.65, 0.05, 0.4),
             status=(0.3, 1.0, 0.25), drive=(0.55, 0.15, 1.0), trim=(0.25, 0.2, 0.3)),
        dict(armor=(0.5, 0.44, 0.32), graphite=(0.06, 0.04, 0.03), marking=(0.25, 0.6, 0.1),
             status=(0.4, 1.0, 0.3), drive=(0.3, 1.0, 0.45), trim=(0.32, 0.24, 0.16)),
        dict(armor=(0.05, 0.2, 0.18), graphite=(0.02, 0.04, 0.04), marking=(0.6, 0.85, 0.2),
             status=(0.9, 0.3, 1.0), drive=(0.85, 0.3, 1.0), trim=(0.2, 0.3, 0.28)),
    ],
}


def srgb_to_linear(c):
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def parse_color(v):
    """'#rrggbb' (sRGB, like a colour picker) or [r, g, b] linear floats 0..1."""
    if isinstance(v, str):
        s = v.strip().lstrip("#")
        if len(s) != 6:
            raise ValueError("bad colour %r" % v)
        return tuple(srgb_to_linear(int(s[i:i + 2], 16) / 255.0) for i in (0, 2, 4))
    t = tuple(float(x) for x in v)[:3]
    if len(t) != 3:
        raise ValueError("bad colour %r" % (v,))
    return t


def palette_for(faction, variant=0, livery=None):
    """Library values with the faction variant and the user livery applied."""
    pal = {k: dict(v) for k, v in MAT_LIB.items()}
    variants = FACTION_PALETTES.get(faction) or [{}]
    for k, rgb in variants[variant % len(variants)].items():
        pal[k]["rgb"] = rgb
        if k in ("status", "drive"):
            pal[k]["emit"] = rgb
    for k, v in (livery or {}).items():
        key = {"accent": "graphite", "hull": "armor", "lights": "status", "engine": "drive"}.get(k, k)
        if key in pal:
            pal[key]["rgb"] = parse_color(v)
            if key in ("status", "drive"):
                pal[key]["emit"] = pal[key]["rgb"]
    return pal


def make_materials(palette, suffix=""):
    """Create (or reuse) the AF_* materials of a palette. Returns {key: bpy.types.Material}."""
    out = {}
    for key in MATS:
        p = palette[key]
        name = MAT_NAME[key] + suffix
        m = bpy.data.materials.get(name) or bpy.data.materials.new(name)
        nt = m.node_tree
        bsdf = next((n for n in nt.nodes if n.type == "BSDF_PRINCIPLED"), None) if nt else None
        if bsdf is not None:
            bsdf.inputs["Base Color"].default_value = (*p["rgb"], 1.0)
            bsdf.inputs["Metallic"].default_value = p["metal"]
            bsdf.inputs["Roughness"].default_value = p["rough"]
            if p.get("emit"):
                bsdf.inputs["Emission Color"].default_value = (*p["emit"], 1.0)
                bsdf.inputs["Emission Strength"].default_value = p.get("strength", 1.0)
            if key == "glass":
                bsdf.inputs["Alpha"].default_value = 0.6
        if key == "glass":
            for attr, val in (("surface_render_method", "BLENDED"), ("blend_method", "BLEND")):
                try:
                    setattr(m, attr, val)
                except (AttributeError, TypeError):
                    pass
        # viewport (Workbench preview) colour: emissive parts drawn bright
        rgb = p["rgb"]
        if p.get("emit"):
            rgb = tuple(min(1.0, c * 1.25 + 0.05) for c in p["emit"])
        m.diffuse_color = (*rgb, 1.0)
        m.metallic = min(p["metal"], 0.6)
        m.roughness = p["rough"]
        out[key] = m
    return out


# ----------------------------------------------------------------------------- geometry kit
def track(axis):
    """Rotation (3x3) mapping local +Z onto axis (local +Y kept as close to world +Y/+Z as possible)."""
    v = Vector(axis).normalized()
    up = "Y" if abs(v.y) < 0.99 else "X"
    return v.to_track_quat("Z", up).to_matrix()


def frame(z_axis, y_hint=(0, 1, 0)):
    """3x3 rotation whose local Z = z_axis and local Y = y_hint projected (socket frame)."""
    z = Vector(z_axis).normalized()
    y = Vector(y_hint)
    y = y - z * y.dot(z)
    if y.length < 1e-6:
        for alt in ((0, 0, 1), (1, 0, 0)):
            y = Vector(alt) - z * Vector(alt).dot(z)
            if y.length > 1e-6:
                break
    y.normalize()
    x = y.cross(z)
    return Matrix((x, y, z)).transposed()


def smooth_interp(ctrl, t):
    """Monotone (PCHIP-like) interpolation of control rows [(t, a, b, ...)] at t."""
    if t <= ctrl[0][0]:
        return list(ctrl[0][1:])
    if t >= ctrl[-1][0]:
        return list(ctrl[-1][1:])
    for i in range(len(ctrl) - 1):
        t0, t1 = ctrl[i][0], ctrl[i + 1][0]
        if t0 <= t <= t1:
            u = (t - t0) / max(t1 - t0, 1e-9)
            s = u * u * (3 - 2 * u)
            return [a + (b - a) * s for a, b in zip(ctrl[i][1:], ctrl[i + 1][1:])]
    return list(ctrl[-1][1:])


class Kit:
    """Accumulates the whole asset in one bmesh. Faces carry material_index = MI[key].

    lod 0: bevels, panel insets, full segment counts; lod 1: no bevels/insets, fewer segments,
    small details skipped; lod 2: silhouette parts only. `size_ref` is the ship length used for the
    detail thresholds."""

    def __init__(self, lod=0, size_ref=10.0):
        self.bm = bmesh.new()
        self.lod = lod
        self.size_ref = size_ref
        self.pre = Matrix.Identity(4)
        self.parts = 0
        self.skipped = 0

    # -- bookkeeping
    def skip(self, size):
        if size is None:
            return False
        lim = {0: 0.0, 1: 0.012, 2: 0.045}.get(self.lod, 0.045) * self.size_ref
        if size < lim:
            self.skipped += 1
            return True
        return False

    def segs(self, n, minimum=6):
        f = {0: 1.0, 1: 0.6, 2: 0.4}.get(self.lod, 0.4)
        return max(minimum, int(round(n * f)))

    def _poly(self, verts, faces, M, mat, face_mats=None):
        bm = self.bm
        T = self.pre @ M
        vs = [bm.verts.new(T @ Vector(v)) for v in verts]
        out = []
        for k, f in enumerate(faces):
            try:
                face = bm.faces.new([vs[i] for i in f])
            except ValueError:
                continue
            face.material_index = MI[face_mats[k] if face_mats else mat]
            out.append(face)
        if T.to_3x3().determinant() < 0:
            bmesh.ops.reverse_faces(bm, faces=out)
        self.parts += 1
        return vs, out

    def _finish(self, faces, bevel=0.0, segs=1, angle=30.0, recalc=True):
        if recalc and faces:
            bmesh.ops.recalc_face_normals(self.bm, faces=faces)
        if bevel > 0 and self.lod == 0 and faces:
            edges = set()
            lim = math.radians(angle)
            for f in faces:
                if not f.is_valid:
                    continue
                for e in f.edges:
                    if len(e.link_faces) == 2:
                        try:
                            if e.calc_face_angle() > lim:
                                edges.add(e)
                        except ValueError:
                            pass
            if edges:
                bmesh.ops.bevel(self.bm, geom=list(edges), offset=bevel, offset_type="OFFSET",
                                segments=segs, profile=0.5, affect="EDGES", clamp_overlap=True,
                                material=-1, harden_normals=False)
        return faces

    # -- primitives
    def box(self, c, size, mat, rot=None, bevel=0.0, taper=(1.0, 1.0), detail=None, shift_top=(0.0, 0.0),
            face_mats=None):
        """Box centred at c; taper scales the top face (x, y); shift_top moves it (x, y)."""
        if self.skip(detail if detail is not None else None):
            return []
        hx, hy, hz = size[0] / 2.0, size[1] / 2.0, size[2] / 2.0
        tx, ty = taper
        sx, sy = shift_top
        vs = [(-hx, -hy, -hz), (hx, -hy, -hz), (hx, hy, -hz), (-hx, hy, -hz),
              (-hx * tx + sx, -hy * ty + sy, hz), (hx * tx + sx, -hy * ty + sy, hz),
              (hx * tx + sx, hy * ty + sy, hz), (-hx * tx + sx, hy * ty + sy, hz)]
        fs = [(0, 3, 2, 1), (4, 5, 6, 7), (0, 1, 5, 4), (1, 2, 6, 5), (2, 3, 7, 6), (3, 0, 4, 7)]
        R = (rot.to_3x3() if rot is not None else Matrix.Identity(3)).to_4x4()
        M = Matrix.Translation(Vector(c)) @ R
        _, faces = self._poly(vs, fs, M, mat, face_mats)
        return self._finish(faces, bevel=min(bevel, min(size) * 0.3))

    def cyl(self, c, r1, depth, axis=(0, 0, 1), mat="trim", r2=None, segs=16, bevel=0.0, detail=None,
            cap_mat=None):
        if self.skip(detail):
            return []
        n = self.segs(segs)
        r2 = r1 if r2 is None else r2
        vs = []
        for z, r in ((-depth / 2.0, r1), (depth / 2.0, r2)):
            for i in range(n):
                a = i * math.tau / n
                vs.append((r * math.cos(a), r * math.sin(a), z))
        fs = [tuple(range(n - 1, -1, -1)), tuple(range(n, 2 * n))]
        fm = [cap_mat or mat, cap_mat or mat]
        for i in range(n):
            fs.append((i, (i + 1) % n, n + (i + 1) % n, n + i))
            fm.append(mat)
        M = Matrix.Translation(Vector(c)) @ track(axis).to_4x4()
        _, faces = self._poly(vs, fs, M, mat, fm)
        return self._finish(faces, bevel=min(bevel, depth * 0.3, min(r1, r2) * 0.3))

    def lathe(self, prof, M, mats, segs=24, detail=None, bevel=0.0):
        """Revolve the closed (r, a) polygon `prof` around local Z (a along Z). mats[k] = material of the
        edge prof[k] -> prof[k+1]. Points with r ~ 0 become poles."""
        if self.skip(detail):
            return []
        n = self.segs(segs, 8)
        verts, idx = [], []
        for r, a in prof:
            if r < 1e-6:
                idx.append([len(verts)])
                verts.append((0.0, 0.0, a))
            else:
                ring = []
                for i in range(n):
                    t = i * math.tau / n
                    ring.append(len(verts))
                    verts.append((r * math.cos(t), r * math.sin(t), a))
                idx.append(ring)
        fs, fm = [], []
        m = len(prof)
        for k in range(m):
            A, B = idx[k], idx[(k + 1) % m]
            if len(A) == 1 and len(B) == 1:
                continue
            for i in range(n):
                if len(A) == 1:
                    fs.append((A[0], B[(i + 1) % n], B[i]))
                elif len(B) == 1:
                    fs.append((A[i], A[(i + 1) % n], B[0]))
                else:
                    fs.append((A[i], A[(i + 1) % n], B[(i + 1) % n], B[i]))
                fm.append(mats[k % len(mats)])
        _, faces = self._poly(verts, fs, M, mats[0], fm)
        return self._finish(faces, bevel=bevel)

    def sphere(self, c, r, mat, segs=12, scale=(1, 1, 1), hemi=False, axis=(0, 0, 1), detail=None):
        n = max(3, segs // 2)
        prof = [(0.0, -r if not hemi else 0.0)]
        a0 = -math.pi / 2 if not hemi else 0.0
        for k in range(1, n):
            a = a0 + (math.pi / 2 - a0) * k / n
            prof.append((r * math.cos(a), r * math.sin(a)))
        prof.append((0.0, r))
        if hemi:
            prof = [(0.0, 0.0), (r, 0.0)] + prof[1:]
        M = Matrix.Translation(Vector(c)) @ track(axis).to_4x4() @ Matrix.Diagonal((*scale, 1.0))
        # lathe expects a closed polygon; prof goes axis -> rim -> axis, closing edge is on the axis
        return self.lathe(prof, M, [mat], segs=segs, detail=detail)

    def loft(self, rings, mat, cap0="fan", cap1="fan", apex0=None, apex1=None, face_mat=None, bevel=0.0,
             detail=None, inset=None, recalc=True):
        """Loft through rings (lists of points, same count). cap: 'fan' (flat-ish, centre vertex), 'apex'
        (fan to apex point), None (open). face_mat(i_segment, j_edge, centre, normal) -> key or None.
        inset: dict(thickness, depth, select(i, j, centre, normal) -> None|'raise'|'panel'|'sink')."""
        if self.skip(detail):
            return []
        bm = self.bm
        T = self.pre
        n = len(rings[0])
        vr = [[bm.verts.new(T @ Vector(p)) for p in ring] for ring in rings]
        faces, meta = [], []
        mi = MI[mat]
        for i in range(len(rings) - 1):
            for j in range(n):
                q = (vr[i][j], vr[i][(j + 1) % n], vr[i + 1][(j + 1) % n], vr[i + 1][j])
                try:
                    f = bm.faces.new(q)
                except ValueError:
                    continue
                f.material_index = mi
                faces.append(f)
                meta.append((i, j))
        caps = []
        for cap, ring, apex, rev in ((cap0, vr[0], apex0, True), (cap1, vr[-1], apex1, False)):
            if not cap:
                continue
            if cap == "apex" and apex is not None:
                cv = bm.verts.new(T @ Vector(apex))
            else:
                cv = bm.verts.new(sum((v.co for v in ring), Vector()) / len(ring))
            for j in range(n):
                tri = (cv, ring[j], ring[(j + 1) % n]) if rev else (cv, ring[(j + 1) % n], ring[j])
                try:
                    f = bm.faces.new(tri)
                except ValueError:
                    continue
                f.material_index = mi
                caps.append(f)
        allf = faces + caps
        if T.to_3x3().determinant() < 0:
            bmesh.ops.reverse_faces(bm, faces=allf)
        if recalc:
            bmesh.ops.recalc_face_normals(bm, faces=allf)
        if face_mat:
            for f, (i, j) in zip(faces, meta):
                k = face_mat(i, j, f.calc_center_median(), f.normal)
                if k:
                    f.material_index = MI[k]
        self.parts += 1
        if inset and self.lod == 0:
            self._inset(faces, meta, inset)
        if bevel > 0:
            self._finish(allf, bevel=bevel, recalc=False)
        return allf

    def _inset(self, faces, meta, spec):
        groups = {"raise": [], "panel": [], "sink": [], "plate": [], "glow": [], "cover": []}
        bands = {}
        th = spec["thickness"]
        for f, (i, j) in zip(faces, meta):
            if not f.is_valid:
                continue
            kind = spec["select"](i, j, f.calc_center_median(), f.normal)
            if not kind:
                continue
            if kind == "glowband":
                # whole station rows: one inset_region per row -> a seam that follows the panel loop
                bands.setdefault(i, []).append(f)
                continue
            short = min(e.calc_length() for e in f.edges)
            if short < th * 3.2:
                continue
            if kind == "panel" and short < th * 9:
                kind = "raise"
            elif kind == "panel" and short > th * 14:
                kind = "cover"  # big hatches: raised machined cover plate (a dark recess reads as a hole)
            groups[kind].append(f)
        depth = spec.get("depth", th * 0.35)
        for i in sorted(bands):
            row = [f for f in bands[i] if f.is_valid]
            if not row:
                continue
            res = bmesh.ops.inset_region(self.bm, faces=row, thickness=th * 0.45, depth=depth * 0.25,
                                         use_even_offset=True)
            for f in res.get("faces", []):
                f.material_index = MI["status"]
        for kind, fs in groups.items():
            if not fs:
                continue
            if kind == "cover":
                bmesh.ops.inset_individual(self.bm, faces=fs, thickness=th, depth=depth, use_even_offset=True)
                bmesh.ops.inset_individual(self.bm, faces=fs, thickness=th * 1.6, depth=depth * 0.9,
                                           use_even_offset=True)
                for f in fs:
                    f.material_index = MI["trim"]
                continue
            d = {"raise": depth, "plate": depth * 1.8, "panel": depth, "sink": -depth * 2.5, "glow": depth * 0.6}[kind]
            res = bmesh.ops.inset_individual(self.bm, faces=fs, thickness=th * (0.6 if kind == "glow" else 1.0),
                                             depth=d, use_even_offset=True)
            if kind == "glow":
                # the thin ring around each plate glows (alien circuitry seams)
                for f in res.get("faces", []):
                    f.material_index = MI["status"]
            if kind == "panel":
                # second, wider inset: a recessed service hatch framed by the plate
                bmesh.ops.inset_individual(self.bm, faces=fs, thickness=th * 2.2, depth=-depth * 1.6,
                                           use_even_offset=True)
                for f in fs:
                    f.material_index = MI["panel"]

    def airfoil(self, root, span_vec, chord_dir, thick_dir, sections, mat, face_mat=None, inset=None,
                bevel=0.0, detail=None, tip_cap=True):
        """Wing/fin loft. sections: [(s 0..1, le_offset_along_chord_dir, chord, thickness, lift)] with
        `lift` an extra offset along thick_dir. root: leading-edge point at s=0."""
        prof = [(0.0, 0.0), (0.16, 0.5), (0.62, 0.42), (1.0, 0.07), (1.0, -0.07), (0.62, -0.42), (0.16, -0.5)]
        root, span_vec = Vector(root), Vector(span_vec)
        cd, td = Vector(chord_dir).normalized(), Vector(thick_dir).normalized()
        rings = []
        for s, le, chord, thick, lift in sections:
            base = root + span_vec * s + cd * le + td * lift
            rings.append([base + cd * (u * chord) + td * (v * thick) for u, v in prof])
        return self.loft(rings, mat, cap0="fan", cap1="fan" if tip_cap else None, face_mat=face_mat,
                         inset=inset, bevel=bevel, detail=detail)

    def mirrored(self, fn, *a, **k):
        """Build fn(...) on the +X side and its mirror on -X."""
        out = fn(*a, **k) or []
        self.pre = Matrix.Diagonal((-1.0, 1.0, 1.0, 1.0)) @ self.pre
        try:
            out2 = fn(*a, **k) or []
        finally:
            self.pre = Matrix.Diagonal((-1.0, 1.0, 1.0, 1.0)) @ self.pre
        return list(out) + list(out2)

    def transform_all(self, M):
        bmesh.ops.transform(self.bm, matrix=M, verts=list(self.bm.verts))

    def bounds(self):
        if not self.bm.verts:
            return Vector((0, 0, 0)), Vector((0, 0, 0))
        co = np.array([v.co[:] for v in self.bm.verts])
        return Vector(co.min(0)), Vector(co.max(0))

    def tri_count(self):
        return sum(len(f.verts) - 2 for f in self.bm.faces)


# ----------------------------------------------------------------------------- hull profiles
def _superellipse(n_pts, ex, lobes=0.0, lobe_k=3):
    pts = []
    for i in range(n_pts):
        a = math.pi / 2 - i * math.tau / n_pts  # start at top, go clockwise seen from +Y
        c, s = math.cos(a), math.sin(a)
        r = 1.0 + lobes * math.cos(lobe_k * (a - math.pi / 2))
        x = math.copysign(abs(c) ** (2.0 / ex), c) * r
        z = math.copysign(abs(s) ** (2.0 / ex), s) * r
        pts.append((x, z))
    return pts


def _mirror_half(half):
    """Half profile from top centre (0, ztop) clockwise to bottom centre (0, zbot) -> full polygon."""
    mir = [(-x, z) for x, z in reversed(half[1:-1])]
    return list(half) + mir


PROFILES = {
    # unit (x, z) polygons, x in [-1, 1], z in [-1, 1]; start at top centre and go clockwise seen from +Y.
    # military / industrial carry a waist ledge (upper deck narrower than the lower hull) so hulls read as
    # layered structures instead of tubes.
    "industrial": _mirror_half([(0.0, 1.0), (0.6, 1.0), (0.76, 0.9), (0.8, 0.56), (1.0, 0.5), (1.0, -0.58),
                                (0.86, -0.9), (0.6, -1.0), (0.0, -1.0)]),
    "military": _mirror_half([(0.0, 1.0), (0.48, 1.0), (0.64, 0.8), (0.68, 0.46), (0.97, 0.32), (1.0, 0.06),
                              (0.86, -0.44), (0.52, -0.86), (0.26, -1.0), (0.0, -1.0)]),
    "chined": _mirror_half([(0.0, 1.0), (0.42, 0.92), (0.78, 0.46), (1.0, 0.04), (0.8, -0.38), (0.42, -0.84),
                            (0.0, -1.0)]),
    "box": _mirror_half([(0.0, 1.0), (0.82, 1.0), (1.0, 0.82), (1.0, -0.82), (0.82, -1.0), (0.0, -1.0)]),
    "sleek": _superellipse(20, 2.6),
    "alien": _superellipse(20, 2.1, lobes=0.08, lobe_k=4),
}


def _poly_cut(prof, along, value, pick):
    """Intersections of the polygon with x=value (along='x' -> returns z) or z=value (returns x)."""
    hits = []
    n = len(prof)
    for k in range(n):
        (x0, z0), (x1, z1) = prof[k], prof[(k + 1) % n]
        if along == "x":
            a0, a1, b0, b1 = x0, x1, z0, z1
        else:
            a0, a1, b0, b1 = z0, z1, x0, x1
        if (a0 - value) * (a1 - value) <= 0 and abs(a1 - a0) > 1e-9:
            u = (value - a0) / (a1 - a0)
            hits.append(b0 + (b1 - b0) * u)
        elif abs(a1 - a0) <= 1e-9 and abs(a0 - value) < 1e-9:
            hits.extend([b0, b1])
    if not hits:
        return 0.0
    return max(hits) if pick == "max" else min(hits)


class HullShape:
    """Lofted hull: stations [(y, hw, hh, zc)] sorted by y (tail -> nose) and a unit profile."""

    def __init__(self, stations, profile):
        self.st = sorted(stations, key=lambda s: s[0])
        self.prof = profile

    def at(self, y):
        st = self.st
        if y <= st[0][0]:
            return st[0][1:]
        if y >= st[-1][0]:
            return st[-1][1:]
        for i in range(len(st) - 1):
            if st[i][0] <= y <= st[i + 1][0]:
                u = (y - st[i][0]) / max(st[i + 1][0] - st[i][0], 1e-9)
                return tuple(a + (b - a) * u for a, b in zip(st[i][1:], st[i + 1][1:]))
        return st[-1][1:]

    def top(self, y, x=0.0):
        hw, hh, zc = self.at(y)
        return zc + hh * _poly_cut(self.prof, "x", max(-0.999, min(0.999, x / max(hw, 1e-6))), "max")

    def bottom(self, y, x=0.0):
        hw, hh, zc = self.at(y)
        return zc + hh * _poly_cut(self.prof, "x", max(-0.999, min(0.999, x / max(hw, 1e-6))), "min")

    def side(self, y, z):
        hw, hh, zc = self.at(y)
        return hw * _poly_cut(self.prof, "z", max(-0.999, min(0.999, (z - zc) / max(hh, 1e-6))), "max")

    def rings(self):
        return [[(x * hw, y, zc + z * hh) for x, z in self.prof] for y, hw, hh, zc in self.st]

    @property
    def y_min(self):
        return self.st[0][0]

    @property
    def y_max(self):
        return self.st[-1][0]


def stations_from_ctrl(ctrl, y0, y1, W, H, n, nose_bias=1.6):
    """ctrl rows (t, hw, hh, zc) with t 0 = tail, 1 = nose; returns n stations, denser near the nose."""
    out = []
    for k in range(n):
        u = k / (n - 1)
        t = 1.0 - (1.0 - u) ** 1.0 if nose_bias <= 1 else u
        if nose_bias > 1:
            # remap so that the last third (nose) gets more stations
            t = u + (nose_bias - 1.0) * 0.12 * math.sin(math.pi * u) * (u - 0.5) * 2.0
            t = max(0.0, min(1.0, t))
        hw, hh, zc = smooth_interp(ctrl, t)
        out.append((y0 + (y1 - y0) * t, hw * W, hh * H, zc * H))
    return out


# ----------------------------------------------------------------------------- finalize / export helpers
def mark_sharp(bm, angle_deg=34.0):
    lim = math.radians(angle_deg)
    for f in bm.faces:
        f.smooth = True
    for e in bm.edges:
        if len(e.link_faces) == 2:
            try:
                e.smooth = e.calc_face_angle() <= lim
            except ValueError:
                e.smooth = True
        else:
            e.smooth = False


def split_by_material(kit, mats_bpy, prefix, parent=None, collection=None, angle=34.0, suffix=""):
    """Kit -> one object per used material: M_<prefix>_<material><suffix>."""
    bm = kit.bm
    bm.normal_update()
    mark_sharp(bm, angle)
    used = sorted({f.material_index for f in bm.faces})
    coll = collection or bpy.context.scene.collection
    objs = []
    for idx in used:
        key = MATS[idx]
        b2 = bm.copy()
        kill = [f for f in b2.faces if f.material_index != idx]
        bmesh.ops.delete(b2, geom=kill, context="FACES_ONLY")
        loose_e = [e for e in b2.edges if not e.link_faces]
        if loose_e:
            bmesh.ops.delete(b2, geom=loose_e, context="EDGES")
        loose_v = [v for v in b2.verts if not v.link_edges]
        if loose_v:
            bmesh.ops.delete(b2, geom=loose_v, context="VERTS")
        for f in b2.faces:
            f.material_index = 0
        name = "M_%s_%s%s" % (prefix, SHORT[key], suffix)
        me = bpy.data.meshes.new(name)
        b2.to_mesh(me)
        b2.free()
        me.materials.append(mats_bpy[key])
        ob = bpy.data.objects.new(name, me)
        coll.objects.link(ob)
        if parent is not None:
            ob.parent = parent
        objs.append(ob)
    return objs


def uv_unwrap(objs, method="smart"):
    """TEXCOORD_0 for every mesh. smart = Smart UV Project atlas (non-overlapping, for later baking);
    box = dominant-axis projection (fast, overlapping)."""
    for ob in objs:
        me = ob.data
        if not me.uv_layers:
            me.uv_layers.new(name="UVMap")
        if method == "box" or len(me.polygons) == 0:
            uv = me.uv_layers[0]
            s = 1.0 / max(max(ob.dimensions), 1e-3)
            for p in me.polygons:
                n = p.normal
                ax = max(range(3), key=lambda i: abs(n[i]))
                a, b = [i for i in range(3) if i != ax]
                for li in p.loop_indices:
                    co = me.vertices[me.loops[li].vertex_index].co
                    uv.data[li].uv = (co[a] * s + 0.5, co[b] * s + 0.5)
            continue
        af.select_only([ob])
        try:
            bpy.ops.object.mode_set(mode="EDIT")
            bpy.ops.mesh.select_all(action="SELECT")
            bpy.ops.uv.smart_project(angle_limit=math.radians(66), island_margin=0.004,
                                     correct_aspect=True, scale_to_bounds=False)
        finally:
            bpy.ops.object.mode_set(mode="OBJECT")


def mesh_tris(objs):
    t = 0
    for o in objs:
        if o.type == "MESH":
            t += sum(len(p.vertices) - 2 for p in o.data.polygons)
    return t


def realize(ob):
    """Apply modifiers into the mesh data (no bpy.ops)."""
    if not ob.modifiers:
        return
    dg = bpy.context.evaluated_depsgraph_get()
    me = bpy.data.meshes.new_from_object(ob.evaluated_get(dg), preserve_all_data_layers=True, depsgraph=dg)
    old = ob.data
    ob.modifiers.clear()
    ob.data = me
    if old.users == 0:
        bpy.data.meshes.remove(old)
    me.validate(clean_customdata=False)
    me.name = ob.name


def _collapse_pass(objs, ratio, keep_min):
    for o in objs:
        if len(o.data.polygons) <= keep_min:
            continue
        mod = o.modifiers.new("lod_decimate", "DECIMATE")
        mod.decimate_type = "COLLAPSE"
        mod.ratio = ratio
        mod.use_collapse_triangulate = True
        realize(o)


def decimate_to(objs, target, keep_min=12, log=None):
    """Collapse-decimate a set of meshes to about `target` triangles in total (iterative).
    `log` (list) receives one dict per pass. Blender's collapse never collapses edges shared by 3+
    faces, so an AI mesh with inner sheets glued to the hull can stall above the target - see
    voxel_proxy_lod for the far-LOD fallback."""
    for _ in range(4):
        total = mesh_tris(objs)
        if total <= target or total == 0:
            break
        ratio = max(0.01, min(1.0, target / float(total) * 0.98))
        _collapse_pass(objs, ratio, keep_min)
        if log is not None:
            log.append({"stage": "collapse", "ratio": round(ratio, 4), "tris_in": total, "tris_out": mesh_tris(objs)})
    return mesh_tris(objs)


def mesh_triangles_gltf(objs):
    """World-space triangles of Blender mesh objects in glTF axes (x, z, -y), as glb_triangles returns."""
    Vs, Ts, off = [], [], 0
    for o in objs:
        me = o.data
        me.calc_loop_triangles()
        V = np.empty(len(me.vertices) * 3)
        me.vertices.foreach_get("co", V)
        V = V.reshape(-1, 3)
        M = np.array(o.matrix_world)
        V = V @ M[:3, :3].T + M[:3, 3]
        T = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
        me.loop_triangles.foreach_get("vertices", T)
        Vs.append(np.stack([V[:, 0], V[:, 2], -V[:, 1]], 1))
        Ts.append(T.reshape(-1, 3) + off)
        off += len(V)
    return np.concatenate(Vs), np.concatenate(Ts)


def mesh_silhouette_iou(ref_objs, objs, res=192):
    """Gate 10 on Blender objects (before export): top / side silhouette IoU of objs against ref_objs."""
    a, b = mesh_triangles_gltf(ref_objs), mesh_triangles_gltf(objs)
    return {"top": round(silhouette_iou([a, b], "top", res)[0], 3),
            "side": round(silhouette_iou([a, b], "side", res)[0], 3)}


def voxel_proxy_lod(src, target, divs=(150, 220, 300), min_iou=0.85, log=None):
    """Far-LOD fallback when collapse stalls above the budget (smoke 10.10: a Meshy fighter stopped
    at 2699 tris against a LOD2 target of 1425 - about 400 edges with 3+ faces that Blender's collapse never
    touches; splitting them reached 1496 tris but tore the side silhouette to IoU 0.73). A closed voxel
    remesh of `src` (voxel = longest side / div) is collapse-decimated to `target`; each face takes the
    material of the nearest `src` face and the UVs of the nearest `src` face corners (textured hulls), so
    split_by_material and the textures work as on the collapsed LODs. The first voxel size whose proxy is
    within `target` and keeps both silhouettes >= min_iou against `src` wins; otherwise the best one is
    returned. Returns (object linked to the scene, info) or (None, info)."""
    from mathutils.bvhtree import BVHTree
    L = max(src.dimensions)
    if L <= 0 or not len(src.data.polygons):
        return None, {"reason": "empty source"}
    src.data.calc_loop_triangles()
    bvh = BVHTree.FromPolygons([v.co[:] for v in src.data.vertices], [p.vertices[:] for p in src.data.polygons])
    src_mi = np.empty(len(src.data.polygons), dtype=np.int32)
    src.data.polygons.foreach_get("material_index", src_mi)
    best, best_info = None, None
    for div in divs:
        ob = src.copy()
        ob.data = src.data.copy()
        bpy.context.scene.collection.objects.link(ob)
        mod = ob.modifiers.new("lod_voxel", "REMESH")
        mod.mode = "VOXEL"
        mod.voxel_size = L / float(div)
        mod.use_smooth_shade = False
        realize(ob)
        voxel_tris = mesh_tris([ob])
        got = decimate_to([ob], target)
        me = ob.data
        while len(me.materials) < len(src.data.materials):
            me.materials.append(src.data.materials[len(me.materials)])
        mi = np.zeros(len(me.polygons), dtype=np.int32)
        for p in me.polygons:
            hit = bvh.find_nearest(p.center)
            if hit[2] is not None:
                mi[p.index] = src_mi[hit[2]]
        me.polygons.foreach_set("material_index", mi)
        if src.data.uv_layers:
            for uv in list(me.uv_layers):
                me.uv_layers.remove(uv)
            me.uv_layers.new(name=src.data.uv_layers.active.name if src.data.uv_layers.active else "UVMap")
            dt = ob.modifiers.new("lod_uv", "DATA_TRANSFER")
            dt.object = src
            dt.use_loop_data = True
            dt.data_types_loops = {"UV"}
            dt.loop_mapping = "POLYINTERP_NEAREST"
            realize(ob)
        me = ob.data
        me.update()
        sil = mesh_silhouette_iou([src], [ob])
        info = {"voxel_div": div, "voxel_m": round(L / float(div), 4), "voxel_tris": voxel_tris, "tris": got,
                "silhouette_vs_src": sil}
        if log is not None:
            log.append(dict(info, stage="voxel_proxy"))
        good = got <= target and min(sil.values()) >= min_iou
        better = best is None or (got <= target, min(sil.values())) > (best_info["tris"] <= target,
                                                                      min(best_info["silhouette_vs_src"].values()))
        if better:
            if best is not None:
                bme = best.data
                bpy.data.objects.remove(best, do_unlink=True)
                bpy.data.meshes.remove(bme)
            best, best_info = ob, info
        else:
            me = ob.data
            bpy.data.objects.remove(ob, do_unlink=True)
            bpy.data.meshes.remove(me)
        if good:
            break
    return best, best_info


def _hull_bm(points):
    bm = bmesh.new()
    vs = [bm.verts.new(tuple(p)) for p in points]
    res = bmesh.ops.convex_hull(bm, input=vs, use_existing_faces=False)
    kill = [g for g in res.get("geom_interior", []) + res.get("geom_unused", []) if isinstance(g, bmesh.types.BMVert)]
    if kill:
        bmesh.ops.delete(bm, geom=list(set(kill)), context="VERTS")
    bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
    return bm


def convex_collision(objs, name="COL_SIMPLE-convcolonly", max_verts=64, parent=None):
    """Convex COL_SIMPLE with <= max_verts vertices: greedy farthest-point refinement of the true convex hull
    (start from the 6 axis extremes, repeatedly add the hull vertex farthest outside the current hull).
    Returns (object, info) with coverage = volume / volume of the full convex hull."""
    pts = []
    for o in objs:
        if o.type != "MESH" or not len(o.data.vertices):
            continue
        mw = o.matrix_world
        co = np.empty(len(o.data.vertices) * 3)
        o.data.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        pts.append(co @ np.array(mw.to_3x3()).T + np.array(mw.translation))
    P = np.concatenate(pts) if pts else np.array([[-1.0, -1, -1], [1.0, -1, -1], [0, 1.0, -1], [0, 0, 1.0]])
    full = _hull_bm(P)
    V = np.array([v.co[:] for v in full.verts])
    vref = full.calc_volume(signed=False)
    full.free()
    sel = sorted(set(int(i) for i in np.concatenate([V.argmax(0), V.argmin(0)])))
    while True:
        bm = _hull_bm(V[sel])
        if len(bm.verts) >= max_verts or len(sel) >= len(V):
            break
        N = np.array([f.normal[:] for f in bm.faces])
        O = np.array([f.verts[0].co[:] for f in bm.faces])
        dist = ((V[:, None, :] - O[None, :, :]) * N[None, :, :]).sum(2).max(1)
        dist[sel] = -1.0
        best = int(dist.argmax())
        if dist[best] <= 1e-5:
            break
        bm.free()
        sel.append(best)
    while len(bm.verts) > max_verts:
        bm.free()
        sel = sel[:-1]
        bm = _hull_bm(V[sel])
    vol = bm.calc_volume(signed=False)
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    nverts = len(bm.verts)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    ob.display_type = "WIRE"
    ob.hide_render = True
    if parent is not None:
        ob.parent = parent
    return ob, {"vertices": nverts, "volume_m3": round(vol, 3), "hull_volume_m3": round(vref, 3),
                "coverage": round(vol / vref, 4) if vref > 0 else None}


def add_socket(name, kind, pos, z_axis=(0, 0, 1), y_hint=(0, 1, 0), parent=None, size=0.3, **extras):
    """Contract §6 socket empty with an explicit frame (see module docstring)."""
    R = frame(z_axis, y_hint)
    emp = af.add_socket(name, parent=parent, location=tuple(pos), rotation=R.to_euler(), kind=kind,
                        size=size, **extras)
    return emp


SOCKET_RE = re.compile(r"^socket_(turret|launcher|thruster|utility|cargo|docking|sensor|mount|muzzle|fx)(_[a-z0-9]+)*$")
SIZE_KINDS = ("turret", "launcher", "thruster", "cargo")


def size_class_for(diameter):
    return "S" if diameter <= 1.5 else ("M" if diameter <= 3.0 else "L")


# Blender (z_axis, y_hint) of a thruster socket in ship space for each candidate frame (see docstring)
THRUSTER_FRAMES = {
    "thrust": ((0, 0, 1), (0, 1, 0)),
    "exhaust": ((0, 0, 1), (0, -1, 0)),
}
THRUSTER_AXIS_TEXT = {
    "thrust": "frame=thrust: glTF -Z = thrust direction (ship forward), exhaust along +Z, +Y = up",
    "exhaust": "frame=exhaust: glTF -Z = exhaust direction (aft, nozzle opening), +Y = up",
}


def thruster_frame_ok(sock, frame_name):
    """Check one glb_summary socket row against the selected thruster frame (ship nose = glTF -Z)."""
    nz, y = sock["neg_z_gltf"], sock["y_gltf"]
    if frame_name == "exhaust":
        return nz[2] > 0.99 and y[1] > 0.99
    return nz[2] < -0.99 and y[1] > 0.99


def thruster_diameter_check(thr_sockets):
    """Contract §6: every socket_thruster_* carries extras.mount_d_m (nozzle exit diameter, m > 0); FX
    presets scale by it. thr_sockets: glb_summary socket rows of kind thruster."""
    bad = [s["game_socket"] for s in thr_sockets
           if not (isinstance(s["extras"].get("mount_d_m"), (int, float)) and s["extras"]["mount_d_m"] > 0)]
    vals = {s["game_socket"]: s["extras"].get("mount_d_m") for s in thr_sockets}
    return af.check("thruster_mount_d_m", not bad, vals if not bad else bad, "mount_d_m > 0 m on every thruster",
                    note="nozzle exit diameter in metres (contract §6), read by godot_package fx_auto_scale")


def socket_axis_text(kind, thruster_frame="thrust"):
    if kind == "thruster":
        return THRUSTER_AXIS_TEXT.get(thruster_frame, THRUSTER_AXIS_TEXT["thrust"])
    if kind == "muzzle":
        return "glTF -Z = fire direction (barrel axis, turret forward), +Y = up; Blender +Y fwd, +Z up"
    if kind == "pivot":
        return ""
    return "glTF +Y = outward normal, -Z = ship forward; Blender +Z out, +Y fwd"


# ----------------------------------------------------------------------------- GLB summary
# read_glb, accessor_values, node_matrix, glb_summary — counting only, plus a geometry hash
_COMPONENT = {5120: ("b", 1), 5121: ("B", 1), 5122: ("h", 2), 5123: ("H", 2), 5125: ("I", 4), 5126: ("f", 4)}
_WIDTH = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}
_GODOT_SUFFIX = re.compile(r"-(col|convcol|colonly|convcolonly|occ|occonly|noimp|rigid|vehicle|wheel|navmesh|loop|cycle)$")


def read_glb(path):
    raw = open(path, "rb").read()
    magic, version, total = struct.unpack_from("<4sII", raw, 0)
    if magic != b"glTF" or version != 2 or total != len(raw):
        raise ValueError("not a GLB 2.0 file: %s" % path)
    off, doc, binary = 12, None, b""
    while off + 8 <= len(raw):
        size, kind = struct.unpack_from("<I4s", raw, off)
        chunk = raw[off + 8: off + 8 + size]
        off += 8 + size
        if kind == b"JSON":
            doc = json.loads(chunk.decode("utf-8"))
        elif kind == b"BIN\x00":
            binary = chunk
    return doc, binary, len(raw)


def _accessor_values(doc, binary, index):
    acc = doc["accessors"][index]
    if "bufferView" not in acc:
        return np.zeros((0, 3))
    view = doc["bufferViews"][acc["bufferView"]]
    fmt, width = _COMPONENT[acc["componentType"]]
    n = _WIDTH[acc["type"]]
    start = view.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = view.get("byteStride", width * n)
    count = acc["count"]
    if stride == width * n:
        flat = struct.unpack_from("<%d%s" % (count * n, fmt), binary, start)
        return np.array(flat, dtype=np.float64).reshape(count, n)
    return np.array([struct.unpack_from("<%d%s" % (n, fmt), binary, start + i * stride) for i in range(count)])


def _node_matrix(node):
    if "matrix" in node:
        return np.array(node["matrix"], dtype=np.float64).reshape(4, 4).T
    t = node.get("translation", [0, 0, 0])
    x, y, z, w = node.get("rotation", [0, 0, 0, 1])
    s = node.get("scale", [1, 1, 1])
    rot = np.array([[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
                    [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
                    [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]])
    m = np.eye(4)
    m[:3, :3] = rot * np.array(s)[None, :]
    m[:3, 3] = t
    return m


def glb_summary(path):
    doc, binary, nbytes = read_glb(path)
    nodes = doc.get("nodes", [])
    meshes = doc.get("meshes", [])
    materials = doc.get("materials", [])
    accessors = doc.get("accessors", [])
    mesh_tris, mesh_prims = [], []
    pos_acc = set()
    no_uv = no_n = no_mat = 0
    col_meshes = {n["mesh"] for n in nodes if "mesh" in n and _GODOT_SUFFIX.search(n.get("name", ""))}
    for mi_, mesh in enumerate(meshes):
        tris = 0
        for prim in mesh.get("primitives", []):
            attrs = prim.get("attributes", {})
            if "POSITION" in attrs:
                pos_acc.add(attrs["POSITION"])
            if mi_ not in col_meshes:
                no_uv += "TEXCOORD_0" not in attrs
                no_n += "NORMAL" not in attrs
                no_mat += "material" not in prim
            count = accessors[prim["indices"]]["count"] if "indices" in prim else accessors[attrs["POSITION"]]["count"]
            mode = prim.get("mode", 4)
            tris += count // 3 if mode == 4 else max(0, count - 2)
        mesh_tris.append(tris)
        mesh_prims.append(len(mesh.get("primitives", [])))
    parent_of = {c: i for i, n in enumerate(nodes) for c in n.get("children", [])}
    world = {}
    stack = [(r, np.eye(4)) for r in range(len(nodes)) if r not in parent_of]
    while stack:
        i, pm = stack.pop()
        m = pm @ _node_matrix(nodes[i])
        world[i] = m
        for c in nodes[i].get("children", []):
            stack.append((c, m))
    tris = surfaces = 0
    per_mesh_tris, per_mesh_prims = [], []      # per render-mesh instance (heaviest mesh)
    used = set()
    sockets, bad, cols = [], [], []
    lo, hi = np.full(3, np.inf), np.full(3, -np.inf)
    for i, n in enumerate(nodes):
        name = n.get("name", "")
        if name and ("." in name or " " in name or not name.isascii()):
            bad.append(name)
        suf = _GODOT_SUFFIX.search(name)
        is_col = bool(suf) and suf.group(1) in ("col", "convcol", "colonly", "convcolonly")
        if is_col:
            cols.append(name)
        ex = n.get("extras") or {}
        if isinstance(ex, dict) and "game_socket" in ex:
            p = world[i] @ np.array([0, 0, 0, 1.0])
            axes = world[i][:3, :3]
            sockets.append({"node": name, "game_socket": ex["game_socket"],
                            "extras": {k: v for k, v in ex.items() if k != "game_socket"},
                            "position_gltf": [round(float(c), 4) for c in p[:3]],
                            "neg_z_gltf": [round(float(c), 3) for c in -axes[:, 2]],
                            "y_gltf": [round(float(c), 3) for c in axes[:, 1]]})
        if "mesh" in n and not is_col:
            mi_ = n["mesh"]
            tris += mesh_tris[mi_]
            surfaces += mesh_prims[mi_]
            per_mesh_tris.append(mesh_tris[mi_])
            per_mesh_prims.append(mesh_prims[mi_])
            for prim in meshes[mi_].get("primitives", []):
                if "material" in prim:
                    used.add(prim["material"])
                pa = prim.get("attributes", {}).get("POSITION")
                acc = accessors[pa]
                if "min" in acc and "max" in acc:
                    bmin, bmax = np.array(acc["min"]), np.array(acc["max"])
                    corners = np.array([[a, b, c, 1.0] for a in (bmin[0], bmax[0]) for b in (bmin[1], bmax[1])
                                        for c in (bmin[2], bmax[2])])
                    wc = (world[i] @ corners.T).T[:, :3]
                    lo = np.minimum(lo, wc.min(0))
                    hi = np.maximum(hi, wc.max(0))
    h = hashlib.sha256()
    finite = True
    for idx in sorted(pos_acc):
        vals = _accessor_values(doc, binary, idx)
        if not np.all(np.isfinite(vals)):
            finite = False
        rounded = sorted(tuple(round(float(c), 4) for c in v) for v in vals)
        for v in rounded:
            h.update(struct.pack("<3d", *v))
    size = (hi - lo) if np.all(np.isfinite(lo)) else np.zeros(3)
    center = (hi + lo) / 2 if np.all(np.isfinite(lo)) else np.zeros(3)
    return {
        "file": path, "bytes": nbytes, "mb": round(nbytes / 1048576.0, 3), "nodes": len(nodes),
        "meshes": len(meshes), "surfaces_lod0": surfaces, "triangles": tris,
        "worst_mesh_triangles": max(per_mesh_tris, default=0), "worst_mesh_surfaces": max(per_mesh_prims, default=0),
        "materials_used": len(used), "material_names_used": [materials[i].get("name", "") for i in sorted(used)],
        "alpha_modes": {m.get("name", ""): m.get("alphaMode", "OPAQUE") for m in materials},
        "images": len(doc.get("images", [])), "sockets": sockets, "bad_names": bad, "collision_nodes": cols,
        "prims_without_uv": no_uv, "prims_without_normal": no_n, "prims_without_material": no_mat,
        "animations": [{"name": a.get("name", ""), "channels": len(a.get("channels", []))}
                       for a in doc.get("animations", [])],
        "finite": finite, "size_gltf": [round(float(c), 4) for c in size],
        "center_offset_gltf": [round(float(c), 4) for c in center],
        "geometry_hash": h.hexdigest()[:32], "node_names": [n.get("name", "") for n in nodes],
        "extensions_used": doc.get("extensionsUsed", []),
    }


def budget_checks(summ, cls, lod_tris=None):
    """Contract checks (af_common.check items) of a GLB summary against the class budget."""
    b = BUDGETS[cls]
    tris, surf, per = summ["triangles"], summ["surfaces_lod0"], "whole file"
    out = [
        af.check("tris_lod0", tris <= b["tris"], tris, b["tris"], note=per),
        af.check("surfaces_lod0", surf <= b["surfaces"], surf, b["surfaces"], note=per),
        af.check("surfaces_target_151_2", surf <= b["t_surfaces"], surf,
                 b["t_surfaces"], severity="warning", note="target tier after material merge"),
        af.check("materials", summ["materials_used"] <= b["materials"], summ["materials_used"], b["materials"]),
        af.check("glb_mb", summ["mb"] <= b["mb"], summ["mb"], b["mb"]),
        af.check("nodes", summ["nodes"] <= b["nodes"], summ["nodes"], b["nodes"],
                 note="class node cap"),
    ]
    if b["strict_nodes"] is not None:
        out.append(af.check("nodes_strict_row", summ["nodes"] <= b["strict_nodes"], summ["nodes"], b["strict_nodes"],
                            severity="warning", note="recommended node count (hull <= 24, turret <= 12)"))
    out += [
        af.check("bad_names", not summ["bad_names"], len(summ["bad_names"]), 0),
        af.check("finite_positions", summ["finite"], summ["finite"], True),
        af.check("prims_without_uv", summ["prims_without_uv"] == 0, summ["prims_without_uv"], 0),
        af.check("prims_without_normal", summ["prims_without_normal"] == 0, summ["prims_without_normal"], 0),
        af.check("prims_without_material", summ["prims_without_material"] == 0, summ["prims_without_material"], 0),
    ]
    if lod_tris:
        l0 = max(1, tris)
        for k, lim_cls, frac in (("lod1", b["lod1"], 0.35), ("lod2", b["lod2"], 0.12)):
            if k in lod_tris:
                lim = int(frac * l0) if lim_cls is None else min(lim_cls, int(frac * l0))
                out.append(af.check("tris_" + k, lod_tris[k] <= lim, lod_tris[k], lim))
    bad_sock = [s["game_socket"] for s in summ["sockets"]
                if not (SOCKET_RE.match(s["game_socket"]) or s["game_socket"].startswith("pivot_"))]
    out.append(af.check("socket_names", not bad_sock, bad_sock, "socket_<kind>_<place>"))
    no_size = [s["game_socket"] for s in summ["sockets"]
               if s["extras"].get("kind") in SIZE_KINDS and s["extras"].get("size_class") not in ("S", "M", "L")]
    out.append(af.check("socket_size_class", not no_size, no_size, "S/M/L"))
    mism = [s["node"] for s in summ["sockets"] if s["node"] != s["game_socket"]]
    out.append(af.check("socket_node_equals_game_socket", not mism, mism, [], severity="warning"))
    return out


# ----------------------------------------------------------------------------- manifest
def validate_schema(value, schema, path="$"):
    """Tiny draft-07 subset validator: type, required, properties, additionalProperties(false), enum,
    const, pattern, minimum, maximum, exclusiveMinimum, minItems, maxItems, items."""
    errs = []
    types = schema.get("type")
    if types:
        tl = types if isinstance(types, list) else [types]
        okt = False
        for t in tl:
            if (t == "object" and isinstance(value, dict)) or (t == "array" and isinstance(value, list)) or \
               (t == "string" and isinstance(value, str)) or (t == "boolean" and isinstance(value, bool)) or \
               (t == "null" and value is None) or \
               (t == "integer" and isinstance(value, int) and not isinstance(value, bool)) or \
               (t == "number" and isinstance(value, (int, float)) and not isinstance(value, bool)):
                okt = True
        if not okt:
            return ["%s: type %s expected, got %s" % (path, types, type(value).__name__)]
    if "const" in schema and value != schema["const"]:
        errs.append("%s: const %r" % (path, schema["const"]))
    if "enum" in schema and value not in schema["enum"]:
        errs.append("%s: %r not in enum" % (path, value))
    if isinstance(value, str) and "pattern" in schema and not re.search(schema["pattern"], value):
        errs.append("%s: %r does not match %s" % (path, value, schema["pattern"]))
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append("%s: < minimum" % path)
        if "maximum" in schema and value > schema["maximum"]:
            errs.append("%s: > maximum" % path)
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            errs.append("%s: <= exclusiveMinimum" % path)
    if isinstance(value, dict):
        for k in schema.get("required", []):
            if k not in value:
                errs.append("%s: missing %s" % (path, k))
        props = schema.get("properties", {})
        for k, v in value.items():
            if k in props:
                errs.extend(validate_schema(v, props[k], "%s.%s" % (path, k)))
            elif schema.get("additionalProperties") is False:
                errs.append("%s: additional property %s" % (path, k))
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errs.append("%s: fewer than %d items" % (path, schema["minItems"]))
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append("%s: more than %d items" % (path, schema["maxItems"]))
        if isinstance(schema.get("items"), dict):
            for i, v in enumerate(value):
                errs.extend(validate_schema(v, schema["items"], "%s[%d]" % (path, i)))
    return errs


def socket_manifest_rows(empties):
    rows = []
    for e in empties:
        kind = e.get("kind")
        row = {"name": e.get("game_socket", e.name), "kind": kind,
               "position_m": [round(float(c), 4) for c in e.matrix_world.translation]}
        if e.get("size_class"):
            row["size_class"] = e["size_class"]
        if e.get("axis"):
            row["axis"] = str(e["axis"])
        elif kind:
            txt = socket_axis_text(kind, e.get("frame", "thrust"))
            if txt:
                row["axis"] = txt
        if e.get("range_degrees"):
            row["range_degrees"] = [float(x) for x in e["range_degrees"]]
        rows.append(row)
    return rows


def today():
    import datetime
    return datetime.date.today().isoformat()


def write_json(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(af._jsonable(data), fh, ensure_ascii=False, indent=2)
    return path


# ----------------------------------------------------------------------------- previews
PREVIEW_VIEWS = {
    # name: (direction from target to camera, perspective?, up)
    "three_quarter": ((0.95, 1.05, 0.62), True, "Z"),
    "three_quarter_long": ((1.0, 0.62, 0.5), True, "Z"),
    "rear_quarter_long": ((-1.0, -0.62, 0.42), True, "Z"),
    "rear_quarter": ((-0.95, -1.05, 0.5), True, "Z"),
    "side": ((-1.0, 0.0, 0.0), False, "Z"),
    "front": ((0.0, 1.0, 0.0), False, "Z"),
    "back": ((0.0, -1.0, 0.0), False, "Z"),
    "top": ((0.0, 0.0, 1.0), False, "Y"),
    "bottom": ((0.0, 0.0, -1.0), False, "Y"),
}


def _bbox_of(objs):
    pts = []
    for o in objs:
        if o.type == "MESH" and not o.hide_render:
            pts.extend(o.matrix_world @ Vector(c) for c in o.bound_box)
    if not pts:
        return Vector((-1, -1, -1)), Vector((1, 1, 1))
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return lo, hi


def _sample_points(objs, limit=6000):
    pts = []
    for o in objs:
        if o.type != "MESH" or not len(o.data.vertices):
            continue
        co = np.empty(len(o.data.vertices) * 3)
        o.data.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        mw = o.matrix_world
        pts.append(co @ np.array(mw.to_3x3()).T + np.array(mw.translation))
    if not pts:
        return np.array([[-1.0, -1, -1], [1.0, 1, 1]])
    P = np.concatenate(pts)
    if len(P) > limit:
        P = P[np.linspace(0, len(P) - 1, limit).astype(int)]
    return P


def render_views_custom(objs, views, sizes, margin=1.04, exposure=0.25, color_type="MATERIAL", frame_objs=None):
    """Render each named view (PREVIEW_VIEWS) at its own square size; returns list of float RGBA arrays.
    frame_objs: objects that define the camera framing (default: objs) — same framing for LOD comparisons."""
    scene = bpy.context.scene
    bpy.context.view_layer.update()
    vis = set(objs)
    hidden_before = {o.name: o.hide_render for o in scene.objects if o.type == "MESH"}
    for o in scene.objects:
        if o.type == "MESH":
            o.hide_render = o not in vis
    P = _sample_points(frame_objs or objs)
    center = Vector(((P.min(0) + P.max(0)) / 2).tolist())
    radius = max(float(np.linalg.norm(P.max(0) - P.min(0))) / 2, 1e-3)
    cam_data = bpy.data.cameras.new("shp_preview_cam")
    cam = bpy.data.objects.new("shp_preview_cam", cam_data)
    scene.collection.objects.link(cam)
    prev_cam = scene.camera
    scene.camera = cam
    r = scene.render
    r.engine = af.engine_id("WORKBENCH")
    r.resolution_percentage = 100
    r.film_transparent = True
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGBA"
    try:
        scene.view_settings.view_transform = "Standard"
        scene.view_settings.exposure = exposure
    except TypeError:
        pass
    sh = scene.display.shading
    sh.light = "STUDIO"
    sh.color_type = color_type
    sh.show_cavity = True
    try:
        sh.cavity_type = "BOTH"
        sh.cavity_ridge_factor = 1.0
        sh.cavity_valley_factor = 1.3
        sh.curvature_ridge_factor = 0.7
        sh.curvature_valley_factor = 0.9
    except (AttributeError, TypeError):
        pass
    sh.show_specular_highlight = True
    sh.show_shadows = False
    sh.show_object_outline = False
    tmp = tempfile.mkdtemp(prefix="af_ship_prev_")
    try:
        return _render_loop(scene, cam, cam_data, r, P, center, radius, views, sizes, margin, tmp)
    finally:
        scene.camera = prev_cam
        bpy.data.objects.remove(cam)
        bpy.data.cameras.remove(cam_data)
        shutil.rmtree(tmp, ignore_errors=True)
        for name, h in hidden_before.items():
            o = bpy.data.objects.get(name)
            if o is not None:
                o.hide_render = h


def _render_loop(scene, cam, cam_data, r, P, center, radius, views, sizes, margin, tmp):
    tiles = []
    Pc = P - np.array(center[:])
    for name, size in zip(views, sizes):
        r.resolution_x = r.resolution_y = int(size)
        d, persp, up = PREVIEW_VIEWS[name]
        d = Vector(d).normalized()
        up_hint = Vector((0, 1, 0)) if up == "Y" else Vector((0, 0, 1))
        right = up_hint.cross(d)
        if right.length < 1e-6:
            right = Vector((1, 0, 0))
        right.normalize()
        upv = d.cross(right).normalized()
        R = np.array(right[:])
        Uv = np.array(upv[:])
        Dv = np.array(d[:])
        pr, pu, pd = Pc @ R, Pc @ Uv, Pc @ Dv
        if persp:
            cam_data.type = "PERSP"
            cam_data.lens = 50
            cam_data.sensor_width = 36
            th = 18.0 / cam_data.lens
            # re-centre on the projected extents, then fit the distance to every sampled point
            off = right * float((pr.max() + pr.min()) / 2) + upv * float((pu.max() + pu.min()) / 2)
            tgt = center + off
            pr2, pu2 = pr - float((pr.max() + pr.min()) / 2), pu - float((pu.max() + pu.min()) / 2)
            dist = float(np.max(np.maximum(pd + np.abs(pr2) / th, pd + np.abs(pu2) / th))) * margin
            cam.matrix_world = Matrix((right, upv, d)).transposed().to_4x4()
            cam.location = tgt + d * dist
            cam_data.clip_start = max(dist * 0.01, 0.01)
            cam_data.clip_end = dist * 6
        else:
            cam_data.type = "ORTHO"
            cr, cu = float((pr.max() + pr.min()) / 2), float((pu.max() + pu.min()) / 2)
            ext = max(float(pr.max() - pr.min()), float(pu.max() - pu.min()))
            cam_data.ortho_scale = ext * margin
            cam.matrix_world = Matrix((right, upv, d)).transposed().to_4x4()
            cam.location = center + right * cr + upv * cu + d * radius * 4
            cam_data.clip_start = radius * 0.01
            cam_data.clip_end = radius * 12
        one = os.path.join(tmp, "%s_%d.png" % (name, size))
        r.filepath = one
        bpy.ops.render.render(write_still=True)
        tiles.append(load_png_rgba(one))
    return tiles


def _over(t, bg):
    a = t[..., 3:4]
    return t[..., :3] * a + np.array(bg, dtype=np.float32) * (1 - a)


def render_grid(path, objs, views=("three_quarter", "rear_quarter", "side", "front", "top", "back"),
                tile=360, cols=3, bg=(0.085, 0.095, 0.115), margin=1.04, layout="hero", color_type="MATERIAL"):
    """Workbench preview sheet composited on a dark background.
    layout 'hero': first view at 2x size on the left, the next four views 2x2 on the right.
    layout 'grid': all views at `tile` in rows of `cols`."""
    sep = (0.2, 0.22, 0.26)
    if layout == "hero":
        views = list(views)[:5]
        sizes = [tile * 2] + [tile] * (len(views) - 1)
        tiles = render_views_custom(objs, views, sizes, margin, color_type=color_type)
        sheet = np.zeros((tile * 2, tile * 4, 3), dtype=np.float32)
        sheet[:] = bg
        sheet[:, :tile * 2] = _over(tiles[0], bg)
        for k, t in enumerate(tiles[1:5]):
            rr, cc = divmod(k, 2)
            y0, x0 = rr * tile, tile * 2 + cc * tile
            sheet[y0:y0 + tile, x0:x0 + tile] = _over(t, bg)
            sheet[y0:y0 + tile, x0:x0 + 1] = sep
            sheet[y0:y0 + 1, x0:x0 + tile] = sep
    else:
        tiles = render_views_custom(objs, views, [tile] * len(views), margin, color_type=color_type)
        rows = int(math.ceil(len(tiles) / float(cols)))
        sheet = np.zeros((rows * tile, cols * tile, 3), dtype=np.float32)
        sheet[:] = bg
        for k, t in enumerate(tiles):
            rr, cc = divmod(k, cols)
            sheet[rr * tile:(rr + 1) * tile, cc * tile:(cc + 1) * tile] = _over(t, bg)
            sheet[rr * tile:(rr + 1) * tile, cc * tile:cc * tile + 1] = sep
            sheet[rr * tile:rr * tile + 1, cc * tile:(cc + 1) * tile] = sep
    write_png(path, (np.clip(sheet, 0, 1) * 255 + 0.5).astype(np.uint8))
    return os.path.abspath(path)


def load_png_rgba(path):
    """PNG -> float32 HxWx4 (top row first) via Blender's image loader."""
    img = bpy.data.images.load(path, check_existing=False)
    w, h = img.size
    px = np.empty(w * h * 4, dtype=np.float32)
    img.pixels.foreach_get(px)
    bpy.data.images.remove(img)
    return px.reshape(h, w, 4)[::-1].copy()


def write_png(path, img):
    """Minimal PNG encoder (8-bit RGB/RGBA, adaptive None/Sub/Up/Paeth row filters, zlib level 9)."""
    img = np.ascontiguousarray(img)
    if img.dtype != np.uint8:
        img = (np.clip(img, 0, 1) * 255 + 0.5).astype(np.uint8)
    h, w, c = img.shape
    x = img.reshape(h, w * c).astype(np.int16)
    left = np.zeros_like(x)
    left[:, c:] = x[:, :-c]
    up = np.zeros_like(x)
    up[1:] = x[:-1]
    ul = np.zeros_like(x)
    ul[1:, c:] = x[:-1, :-c]
    p = left + up - ul
    pa, pb, pc = np.abs(p - left), np.abs(p - up), np.abs(p - ul)
    pred = np.where((pa <= pb) & (pa <= pc), left, np.where(pb <= pc, up, ul))
    cands = [x, x - left, x - up, x - pred]
    scores = np.stack([np.abs(((f + 128) % 256) - 128).sum(1) for f in cands])
    best = scores.argmin(0)
    out = np.empty((h, w * c + 1), dtype=np.uint8)
    out[:, 0] = np.array([0, 1, 2, 4], dtype=np.uint8)[best]  # PNG filter ids: none, sub, up, paeth
    for k in range(4):
        rows = best == k
        if rows.any():
            out[rows, 1:] = (cands[k][rows] % 256).astype(np.uint8)
    raw = zlib.compress(out.tobytes(), 9)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6 if c == 4 else 2, 0, 0, 0)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", raw) + chunk(b"IEND", b""))
    return path


def downscale(img, factor):
    """Area-average downscale of an HxWxC float/uint8 image by an integer factor."""
    f = int(factor)
    if f <= 1:
        return img
    h, w = img.shape[0] // f * f, img.shape[1] // f * f
    x = img[:h, :w].astype(np.float32).reshape(h // f, f, w // f, f, -1).mean((1, 3))
    return x


def resize_nearest(img, new_w):
    h, w = img.shape[:2]
    new_h = max(1, int(round(h * new_w / float(w))))
    ys = (np.arange(new_h) * h / new_h).astype(int)
    xs = (np.arange(new_w) * w / new_w).astype(int)
    return img[ys][:, xs]


def resize_area(img, new_w):
    """Downscale to new_w using integer area averaging then nearest for the remainder."""
    w = img.shape[1]
    f = max(1, w // new_w)
    x = downscale(img, f) if f > 1 else img.astype(np.float32)
    if x.shape[1] != new_w:
        x = resize_nearest(x, new_w)
    return x


def contact_sheet(path, images, cols, cell_w, labels_bg=(0.085, 0.095, 0.115), max_bytes=None):
    """Compose existing PNG files (or arrays) into a grid; each scaled to cell_w wide."""
    cells = []
    for im in images:
        a = load_png_rgba(im) if isinstance(im, str) else im
        if a.shape[2] == 4:
            al = a[..., 3:4]
            a = a[..., :3] * al + np.array(labels_bg, dtype=np.float32) * (1 - al)
        if a.dtype == np.uint8:
            a = a.astype(np.float32) / 255.0
        cells.append(resize_area(a, cell_w))
    ch = max(c.shape[0] for c in cells)
    rows = int(math.ceil(len(cells) / float(cols)))
    sheet = np.zeros((rows * ch, cols * cell_w, 3), dtype=np.float32)
    sheet[:] = labels_bg
    for k, c in enumerate(cells):
        rr, cc = divmod(k, cols)
        sheet[rr * ch: rr * ch + c.shape[0], cc * cell_w: cc * cell_w + c.shape[1]] = c[..., :3]
    write_png(path, (np.clip(sheet, 0, 1) * 255 + 0.5).astype(np.uint8))
    return path


# ----------------------------------------------------------------------------- turret geometry (shared)
TURRET_RING = {"S": 1.2, "M": 2.4, "L": 4.8}


def rotary_barrels(kit, base, length, cluster_d, n=6, mat="trim", ring_mat="graphite", drum=True):
    """Rotary (gatling) barrel cluster along +Y of the kit frame starting at `base` (x, y, z): rear drum,
    n barrels on a circle around a spindle, two clamp rings and a muzzle ring. LOD 2 keeps one cylinder.
    Returns the muzzle point (centre of the cluster end)."""
    bx, by, bz = base
    c = float(cluster_d)
    if kit.lod >= 2:
        kit.cyl((bx, by + length * 0.5, bz), c * 0.5, length, axis=(0, 1, 0), mat=mat, segs=10)
        return (bx, by + length, bz)
    if drum:
        kit.cyl((bx, by + length * 0.1, bz), c * 0.56, length * 0.2, axis=(0, 1, 0), mat=ring_mat, segs=20,
                bevel=c * 0.04)
        kit.cyl((bx, by + length * 0.215, bz), c * 0.5, length * 0.03, axis=(0, 1, 0), mat=mat, segs=20,
                detail=c * 0.2)
    rr = c * 0.32
    br = c * 0.095
    for k in range(int(n)):
        a = (k + 0.5) * math.tau / n
        x, z = bx + rr * math.cos(a), bz + rr * math.sin(a)
        kit.cyl((x, by + length * 0.6, z), br, length * 0.8, axis=(0, 1, 0), mat=mat, segs=8)
        kit.cyl((x, by + length * 0.985, z), br * 1.25, length * 0.03, axis=(0, 1, 0), mat=mat, segs=8,
                detail=br * 2.5)
    kit.cyl((bx, by + length * 0.6, bz), c * 0.12, length * 0.8, axis=(0, 1, 0), mat=ring_mat, segs=8,
            detail=c * 0.25)
    for t in (0.45, 0.86):
        kit.cyl((bx, by + length * t, bz), c * 0.5, length * 0.04, axis=(0, 1, 0), mat=ring_mat, segs=18,
                bevel=c * 0.02, detail=c * 0.3)
    return (bx, by + length, bz)


def turret_geometry(size="M", barrels=2, style="military", seed=1, lod=0, ring=None, rotary=False):
    """Build a turret as four Kits keyed by rig level: 'root' (fixed base), 'yaw' (turntable + housing),
    'pitch' (cradle / mantlet), 'barrel' (one AF_SteelTrim mesh, animated for recoil).
    rotary=True: every gun is a 6-barrel rotary cluster (rotary_barrels) instead of a plain barrel.
    Local frame of the mount: +Z up (outward), +Y forward. Returns (kits, rig) where rig holds pivot
    positions and muzzle points in mount space."""
    rng = random.Random(seed * 7919 + {"S": 1, "M": 2, "L": 3}.get(size, 2))
    D = float(ring or TURRET_RING.get(size, 2.4))
    kits = {k: Kit(lod=lod, size_ref=D * 3) for k in ("root", "yaw", "pitch", "barrel")}
    kr, ky, kp, kb = kits["root"], kits["yaw"], kits["pitch"], kits["barrel"]
    # --- fixed base (mount plate + bolt ring)
    plate_h = 0.1 * D
    kr.cyl((0, 0, plate_h / 2), 0.5 * D, plate_h, mat="graphite", segs=32, bevel=0.012 * D)
    kr.cyl((0, 0, plate_h + 0.02 * D), 0.44 * D, 0.04 * D, mat="trim", segs=32)
    nb = 12
    for i in range(nb):
        a = i * math.tau / nb
        kr.cyl((0.47 * D * math.cos(a), 0.47 * D * math.sin(a), plate_h + 0.008 * D), 0.022 * D, 0.03 * D,
               mat="trim", segs=6, detail=0.022 * D)
    # --- yaw: turntable + housing
    yaw_z = plate_h + 0.04 * D
    ky.cyl((0, 0, yaw_z + 0.04 * D), 0.41 * D, 0.08 * D, mat="graphite", segs=32, bevel=0.01 * D)
    hz0 = yaw_z + 0.08 * D
    hh = rng.uniform(0.3, 0.36) * D * (0.9 if style == "sleek" else 1.0)
    hw = rng.uniform(0.32, 0.38) * D
    hl = rng.uniform(0.38, 0.46) * D
    prof = PROFILES["sleek" if style in ("sleek", "alien") else ("industrial" if style == "industrial" else "military")]
    # housing loft along Y: rear full, front sloped (glacis)
    st = [(-hl, hw * 0.92, hh * 0.46, hz0 + hh * 0.46), (-hl * 0.55, hw, hh * 0.5, hz0 + hh * 0.5),
          (hl * 0.35, hw, hh * 0.5, hz0 + hh * 0.5), (hl, hw * 0.78, hh * 0.3, hz0 + hh * 0.3)]
    rings = [[(x * w, y, zc + z * h) for x, z in prof] for y, w, h, zc in st]
    ky.loft(rings, "armor", cap0="fan", cap1="fan", bevel=0.01 * D,
            inset={"thickness": 0.012 * D, "depth": 0.006 * D,
                   "select": lambda i, j, c, n: ("raise" if abs(n.z) > 0.6 and i == 1 else None)})
    # side cheeks holding the trunnions
    tz = hz0 + hh * 0.62
    cheek_w = 0.07 * D
    gun_w = hw * 0.62
    for sx in (-1, 1):
        ky.box((sx * (gun_w + cheek_w / 2 + 0.01 * D), 0.08 * D, tz - 0.02 * D), (cheek_w, 0.34 * D, 0.32 * D),
               "graphite", bevel=0.01 * D, taper=(1.0, 0.7))
        ky.cyl((sx * (gun_w + cheek_w + 0.012 * D), 0.08 * D, tz), 0.07 * D, 0.03 * D, axis=(1, 0, 0),
               mat="graphite", segs=16, detail=0.05 * D)
    # rear sensor / ammo box
    if style == "industrial":
        ky.box((0, -hl - 0.06 * D, hz0 + hh * 0.35), (hw * 1.1, 0.14 * D, hh * 0.55), "graphite", bevel=0.01 * D)
        for sx in (-1, 1):
            ky.cyl((sx * hw * 0.55, -hl * 0.2, hz0 + hh * 1.02), 0.03 * D, hl * 1.2, axis=(0, 1, 0),
                   mat="graphite", segs=10, detail=0.03 * D)
    else:
        ky.box((rng.choice((-1, 1)) * hw * 0.45, -hl * 0.4, hz0 + hh * 1.0 + 0.04 * D), (0.12 * D, 0.16 * D, 0.08 * D),
               "graphite", bevel=0.008 * D, detail=0.08 * D)
    # --- pitch: mantlet block around the trunnion axis
    py = 0.08 * D
    mant_l = 0.38 * D
    mant_h = 0.26 * D
    if style in ("sleek", "alien"):
        prof_m = PROFILES["sleek"]
    else:
        prof_m = PROFILES["military"]
    st = [(py - mant_l * 0.5, gun_w * 0.92, mant_h * 0.5, tz), (py + mant_l * 0.25, gun_w, mant_h * 0.52, tz),
          (py + mant_l * 0.6, gun_w * 0.8, mant_h * 0.42, tz)]
    rings = [[(x * w, y, zc + z * h) for x, z in prof_m] for y, w, h, zc in st]
    kp.loft(rings, "armor", cap0="fan", cap1="fan", bevel=0.008 * D)
    # status strip on the mantlet sides (emissive, faction colour)
    for sx in (-1, 1):
        kp.box((sx * gun_w * 0.985, py, tz + mant_h * 0.12), (0.012 * D, mant_l * 0.55, 0.03 * D), "status",
               detail=0.03 * D)
    # barrels
    nb = max(1, min(4, int(barrels)))
    br = {"S": 0.05, "M": 0.048, "L": 0.046}.get(size, 0.048) * D
    blen = {"S": 1.25, "M": 1.35, "L": 1.45}.get(size, 1.35) * D * (1.15 if style == "sleek" else 1.0)
    if nb == 1:
        offs = [(0.0, 0.0)]
        br *= 1.5
    elif nb == 2:
        offs = [(-gun_w * 0.45, 0.0), (gun_w * 0.45, 0.0)]
    elif nb == 3:
        offs = [(-gun_w * 0.55, 0.0), (0.0, 0.0), (gun_w * 0.55, 0.0)]
    else:
        offs = [(-gun_w * 0.38, mant_h * 0.2), (gun_w * 0.38, mant_h * 0.2),
                (-gun_w * 0.38, -mant_h * 0.2), (gun_w * 0.38, -mant_h * 0.2)]
    by0 = py + mant_l * 0.45
    muzzles = []
    for bx, bz in offs:
        z = tz + bz
        if rotary:
            cd = min(br * 5.0, (gun_w * 0.9 if nb > 1 else gun_w * 1.6))
            rotary_barrels(kb, (bx, by0, z), blen, cd)
            muzzles.append((bx, by0 + blen + 0.02 * D, z))
            continue
        kb.cyl((bx, by0 + blen * 0.18, z), br * 1.35, blen * 0.36, axis=(0, 1, 0), mat="trim", segs=16,
               bevel=br * 0.15)
        kb.cyl((bx, by0 + blen * 0.5, z), br, blen, axis=(0, 1, 0), mat="trim", segs=14)
        if style in ("military", "industrial"):
            kb.cyl((bx, by0 + blen * 0.96, z), br * 1.45, blen * 0.1, axis=(0, 1, 0), mat="trim", segs=14,
                   bevel=br * 0.15, detail=br)
        else:
            for k in range(3):
                kb.cyl((bx, by0 + blen * (0.45 + 0.15 * k), z), br * 1.28, blen * 0.035, axis=(0, 1, 0),
                       mat="trim", segs=14, detail=br)
        muzzles.append((bx, by0 + blen + 0.02 * D, z))
    rig = {"ring_d": D, "yaw_z": yaw_z, "pitch": (0.0, py, tz), "muzzles": muzzles, "barrel_r": br,
           "barrel_len": blen, "height": tz + mant_h}
    return kits, rig


def hero_crop(preview_path, bg=(0.085, 0.095, 0.115), pad=0.04):
    """Big 3/4 view of a hero-layout preview (left square), cropped to its content."""
    a = load_png_rgba(preview_path)[..., :3]
    h = a.shape[0]
    a = a[:, :h] if a.shape[1] >= 2 * h else a
    diff = np.abs(a - np.array(bg, dtype=np.float32)).sum(2) > 0.04
    ys, xs = np.where(diff)
    if len(ys) == 0:
        return a
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    py, px = int((y1 - y0) * pad) + 2, int((x1 - x0) * pad) + 2
    return a[max(0, y0 - py):min(a.shape[0], y1 + py), max(0, x0 - px):min(a.shape[1], x1 + px)]


def fit_cell(img, cw, ch, bg=(0.085, 0.095, 0.115)):
    """Scale img (float HxWx3) to fit inside cw x ch keeping aspect; centre on bg."""
    h, w = img.shape[:2]
    f = min(cw / float(w), ch / float(h))
    nw, nh = max(1, int(w * f)), max(1, int(h * f))
    if f < 1.0:
        k = max(1, int(1.0 / f))
        small = downscale(img, k) if k > 1 else img
        ys = (np.arange(nh) * small.shape[0] / float(nh)).astype(int)
        xs = (np.arange(nw) * small.shape[1] / float(nw)).astype(int)
        r = small[ys][:, xs]
    else:
        ys = (np.arange(nh) * h / float(nh)).astype(int)
        xs = (np.arange(nw) * w / float(nw)).astype(int)
        r = img[ys][:, xs]
    cell = np.zeros((ch, cw, 3), dtype=np.float32)
    cell[:] = bg
    oy, ox = (ch - nh) // 2, (cw - nw) // 2
    cell[oy:oy + nh, ox:ox + nw] = r[..., :3]
    return cell


def fleet_sheet(path, previews, cols=6, cell_w=212, cell_h=150, bg=(0.085, 0.095, 0.115), gap=2):
    """Contact sheet of the hero views of many ship previews (one cell per ship)."""
    rows = int(math.ceil(len(previews) / float(cols)))
    W = cols * cell_w + (cols - 1) * gap
    H = rows * cell_h + (rows - 1) * gap
    sheet = np.zeros((H, W, 3), dtype=np.float32)
    sheet[:] = (0.2, 0.22, 0.26)
    for k, p in enumerate(previews):
        r, c = divmod(k, cols)
        y, x = r * (cell_h + gap), c * (cell_w + gap)
        sheet[y:y + cell_h, x:x + cell_w] = fit_cell(hero_crop(p, bg), cell_w, cell_h, bg)
    for k in range(len(previews), rows * cols):
        r, c = divmod(k, cols)
        y, x = r * (cell_h + gap), c * (cell_w + gap)
        sheet[y:y + cell_h, x:x + cell_w] = bg
    write_png(path, (np.clip(sheet, 0, 1) * 255 + 0.5).astype(np.uint8))
    return path


def purge_orphans():
    """Drop unused materials / images / meshes before saving the .blend."""
    try:
        bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
    except TypeError:
        bpy.data.orphans_purge()


def save_blend_clean(path):
    """orphans purge + af_common.save_blend + remove Blender's numbered backup (<name>.blend1)."""
    purge_orphans()
    out = af.save_blend(path)
    for ext in ("1", "2"):
        bak = out + ext
        if os.path.isfile(bak):
            try:
                os.remove(bak)
            except OSError:
                pass
    return out


# ----------------------------------------------------------------------------- silhouettes (QA)
def glb_triangles(path, skip_collision=True):
    """World-space triangles of a GLB in glTF axes (x starboard, y up, z aft). Returns (V, T)."""
    doc, binary, _ = read_glb(path)
    nodes = doc.get("nodes", [])
    parent_of = {c: i for i, n in enumerate(nodes) for c in n.get("children", [])}
    world, stack = {}, [(r, np.eye(4)) for r in range(len(nodes)) if r not in parent_of]
    while stack:
        i, pm = stack.pop()
        m = pm @ _node_matrix(nodes[i])
        world[i] = m
        for c in nodes[i].get("children", []):
            stack.append((c, m))
    Vs, Ts, off = [], [], 0
    for i, n in enumerate(nodes):
        if "mesh" not in n:
            continue
        suf = _GODOT_SUFFIX.search(n.get("name", ""))
        if skip_collision and suf and suf.group(1) in ("col", "convcol", "colonly", "convcolonly"):
            continue
        for prim in doc["meshes"][n["mesh"]].get("primitives", []):
            if prim.get("mode", 4) != 4:
                continue
            P = _accessor_values(doc, binary, prim["attributes"]["POSITION"])
            if "indices" in prim:
                idx = _accessor_values(doc, binary, prim["indices"]).astype(np.int64).reshape(-1, 3)
            else:
                idx = np.arange(len(P)).reshape(-1, 3)
            W = P @ world[i][:3, :3].T + world[i][:3, 3]
            Vs.append(W)
            Ts.append(idx + off)
            off += len(W)
    if not Vs:
        return np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int64)
    return np.concatenate(Vs), np.concatenate(Ts)


def silhouette_mask(V, T, axes, lo, hi, res=256, samples_per_px=2.5, seed=0):
    """Binary orthographic silhouette of triangles (V, T) projected on the two given axes, inside the
    window lo..hi (2-vectors, same scale on both axes). Area sampling + 1-px closing; deterministic."""
    lo, hi = np.asarray(lo, float), np.asarray(hi, float)
    ext = float(max(hi - lo))
    s = (res - 1) / max(ext, 1e-9)
    w = int(np.ceil((hi[0] - lo[0]) * s)) + 1
    h = int(np.ceil((hi[1] - lo[1]) * s)) + 1
    mask = np.zeros((h, w), dtype=bool)
    if len(T) == 0:
        return mask
    P = (V[:, list(axes)] - lo) * s
    a, b, c = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
    area = np.abs((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])) / 2
    n = np.minimum(np.ceil(area * samples_per_px).astype(np.int64), 200000) + 1
    rng = np.random.default_rng(seed)
    rep_ = np.repeat(np.arange(len(T)), n)
    u, v = rng.random(len(rep_)), rng.random(len(rep_))
    flip = u + v > 1
    u[flip], v[flip] = 1 - u[flip], 1 - v[flip]
    pts = a[rep_] + (b[rep_] - a[rep_]) * u[:, None] + (c[rep_] - a[rep_]) * v[:, None]
    pts = np.concatenate([pts, a, b, c, (a + b) / 2, (b + c) / 2, (a + c) / 2])
    xi = np.clip(np.round(pts[:, 0]).astype(int), 0, w - 1)
    yi = np.clip(np.round(pts[:, 1]).astype(int), 0, h - 1)
    mask[yi, xi] = True
    # closing (dilate, then erode) fills pin-holes left by the sampling
    d = mask.copy()
    d[1:] |= mask[:-1]
    d[:-1] |= mask[1:]
    d[:, 1:] |= mask[:, :-1]
    d[:, :-1] |= mask[:, 1:]
    e = d.copy()
    e[1:] &= d[:-1]
    e[:-1] &= d[1:]
    e[:, 1:] &= d[:, :-1]
    e[:, :-1] &= d[:, 1:]
    return e


VIEW_AXES = {"top": (0, 2), "side": (2, 1), "front": (0, 1)}  # glTF axes per orthographic view


def silhouette_iou(meshes, view="top", res=256):
    """Silhouettes of two or more (V, T) meshes in one common window. Returns (iou, masks) where iou is
    the mean IoU of meshes[1:] against meshes[0]."""
    ax = list(VIEW_AXES[view])
    lo = np.min([V[:, ax].min(0) for V, _ in meshes], axis=0)
    hi = np.max([V[:, ax].max(0) for V, _ in meshes], axis=0)
    c, half = (lo + hi) / 2, (hi - lo) / 2 * 1.02
    masks = [silhouette_mask(V, T, ax, c - half, c + half, res) for V, T in meshes]
    ious = [mask_iou(masks[0], m) for m in masks[1:]]
    return (float(np.mean(ious)) if ious else 1.0), masks


def mask_iou(a, b):
    return float(np.logical_and(a, b).sum()) / float(max(np.logical_or(a, b).sum(), 1))


def lod_silhouettes(glb0, lod_paths, res=192):
    """Gate 10 helper: IoU of the top and side silhouettes of each LOD GLB against LOD0 (same window)."""
    m0 = glb_triangles(glb0)
    out = {}
    for key, path in sorted(lod_paths.items()):
        mk = glb_triangles(path)
        top, _ = silhouette_iou([m0, mk], "top", res)
        side, _ = silhouette_iou([m0, mk], "side", res)
        out[key] = {"top": round(top, 3), "side": round(side, 3)}
    return out


def lod_strip(path, sets, views=("three_quarter_long", "top"), tile=300, bg=(0.085, 0.095, 0.115),
              color_type="MATERIAL"):
    """Same-camera LOD comparison: one row per view, one column per LOD (framing from LOD0)."""
    sep = np.array((0.2, 0.22, 0.26), dtype=np.float32)
    rows = []
    for view in views:
        cells = []
        for objs in sets:
            t = render_views_custom(objs, [view], [tile], frame_objs=sets[0], color_type=color_type)[0]
            c = _over(t, bg)
            c[:, :1] = sep
            c[:1, :] = sep
            cells.append(c)
        rows.append(np.concatenate(cells, axis=1))
    sheet = np.concatenate(rows, axis=0)
    write_png(path, (np.clip(sheet, 0, 1) * 255 + 0.5).astype(np.uint8))
    return os.path.abspath(path)


# ----------------------------------------------------------------------------- image helpers (numpy only)
# Blender's Python has numpy but no scipy / PIL: small separable filters, run-length connected components,
# a perspective camera model shared with the concept projection (ship_from_mesh params.paint) and a
# triangle rasteriser by area sampling.
def load_image(path, max_side=None):
    """Any image Blender can read -> float32 HxWx4 (RGBA 0..1, top row first; colour values as stored,
    i.e. sRGB-encoded). Images without alpha get alpha 1."""
    img = bpy.data.images.load(os.path.abspath(path), check_existing=False)
    try:
        img.colorspace_settings.name = "Non-Color"  # raw stored values
    except TypeError:
        pass
    w, h = img.size
    if max_side and max(w, h) > max_side:
        f = max_side / float(max(w, h))
        img.scale(max(1, int(w * f)), max(1, int(h * f)))
        w, h = img.size
    px = np.empty(w * h * 4, dtype=np.float32)
    img.pixels.foreach_get(px)
    channels = img.channels
    bpy.data.images.remove(img)
    out = px.reshape(h, w, 4)[::-1].copy()
    if channels < 4:
        out[..., 3] = 1.0
    return out


def _shift(a, d, axis, fill):
    """a shifted by d cells along axis (positive = towards higher indices); vacated cells = fill."""
    out = np.empty_like(a)
    n = a.shape[axis]
    d = int(d)
    if abs(d) >= n:
        out[...] = fill
        return out
    src = [slice(None)] * a.ndim
    dst = [slice(None)] * a.ndim
    gap = [slice(None)] * a.ndim
    if d >= 0:
        src[axis], dst[axis], gap[axis] = slice(0, n - d), slice(d, n), slice(0, d)
    else:
        src[axis], dst[axis], gap[axis] = slice(-d, n), slice(0, n + d), slice(n + d, n)
    out[tuple(dst)] = a[tuple(src)]
    out[tuple(gap)] = fill
    return out


def max_filter(a, r):
    """Square (2r+1) max filter (bool or float); outside = -inf / False."""
    r = int(r)
    if r <= 0:
        return a.copy()
    fill = False if a.dtype == bool else -np.inf
    out = a
    for ax in (0, 1):
        acc = out.copy()
        for d in range(1, r + 1):
            acc = np.maximum(acc, _shift(out, d, ax, fill))
            acc = np.maximum(acc, _shift(out, -d, ax, fill))
        out = acc
    return out


def min_filter(a, r):
    r = int(r)
    if r <= 0:
        return a.copy()
    fill = True if a.dtype == bool else np.inf
    out = a
    for ax in (0, 1):
        acc = out.copy()
        for d in range(1, r + 1):
            acc = np.minimum(acc, _shift(out, d, ax, fill))
            acc = np.minimum(acc, _shift(out, -d, ax, fill))
        out = acc
    return out


def binary_open(m, r):
    return max_filter(min_filter(m, r), r)


def binary_close(m, r):
    return min_filter(max_filter(m, r), r)


def box_mean(a, r):
    """Square (2r+1) mean filter with edge replication (cumulative sums)."""
    r = int(r)
    if r <= 0:
        return a.astype(np.float32)
    p = np.pad(a.astype(np.float64), r, mode="edge")
    c = np.pad(p.cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    k = 2 * r + 1
    s = c[k:, k:] - c[:-k, k:] - c[k:, :-k] + c[:-k, :-k]
    return (s / (k * k)).astype(np.float32)


def median3(a):
    """3x3 median (edge replicated)."""
    p = np.pad(a, 1, mode="edge")
    h, w = a.shape
    st = np.stack([p[dy:dy + h, dx:dx + w] for dy in range(3) for dx in range(3)])
    return np.partition(st, 4, axis=0)[4]


def label_components(mask):
    """8-connected components of a 2D bool mask (run-length union-find). Returns (labels int32, sizes):
    labels 1..n (0 = background) ordered by size, largest first; sizes[i] = pixels of label i + 1."""
    h, w = mask.shape
    parent = [0]

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    runs_by_row = []
    prev = []
    for y in range(h):
        row = mask[y]
        if not row.any():
            runs_by_row.append([])
            prev = []
            continue
        d = np.diff(np.concatenate(([0], row.astype(np.int8), [0])))
        starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
        cur = []
        j = 0
        for s, e in zip(starts.tolist(), ends.tolist()):
            lab = 0
            while j < len(prev) and prev[j][1] < s:
                j += 1
            k = j
            while k < len(prev) and prev[k][0] <= e:
                pl = find(prev[k][2])
                if lab == 0:
                    lab = pl
                elif pl != lab:
                    a_, b_ = (lab, pl) if lab < pl else (pl, lab)
                    parent[b_] = a_
                    lab = a_
                k += 1
            if lab == 0:
                lab = len(parent)
                parent.append(lab)
            cur.append((s, e, lab))
        runs_by_row.append(cur)
        prev = cur
    labels = np.zeros((h, w), dtype=np.int32)
    sizes = {}
    for y, runs in enumerate(runs_by_row):
        for s, e, lab in runs:
            r = find(lab)
            labels[y, s:e] = r
            sizes[r] = sizes.get(r, 0) + (e - s)
    order = sorted(sizes, key=lambda r: -sizes[r])
    remap = np.zeros(len(parent) + 1, dtype=np.int32)
    for i, r in enumerate(order):
        remap[r] = i + 1
    return remap[labels], np.array([sizes[r] for r in order], dtype=np.int64)


def largest_component(mask, keep_frac=0.02):
    """Mask without specks: components of at least keep_frac x the largest one."""
    lab, sizes = label_components(mask)
    if not len(sizes):
        return mask.copy()
    keep = np.where(sizes >= keep_frac * sizes[0])[0] + 1
    return np.isin(lab, keep)


def resize_mask(m, w):
    """Area-average resize of a bool mask to width w (aspect kept) -> bool (> 0.5)."""
    h0, w0 = m.shape
    h = max(1, int(round(h0 * w / float(w0))))
    ys = np.round(np.arange(h + 1) * h0 / float(h)).astype(int)
    xs = np.round(np.arange(w + 1) * w0 / float(w)).astype(int)
    c = np.pad(m.astype(np.float64).cumsum(0).cumsum(1), ((1, 0), (1, 0)))
    s = c[ys[1:]][:, xs[1:]] - c[ys[:-1]][:, xs[1:]] - c[ys[1:]][:, xs[:-1]] + c[ys[:-1]][:, xs[:-1]]
    area = np.outer(np.diff(ys), np.diff(xs)).astype(np.float64)
    return (s / np.maximum(area, 1)) > 0.5


# ----------------------------------------------------------------------------- perspective camera (concept fit)
CAM_PARAM_NAMES = ["az_deg", "el_deg", "dist_m", "tx", "ty", "tz", "roll_deg", "lens_mm"]


def cam_from_params(p, res, sensor=36.0):
    """p = [az_deg, el_deg, dist, tx, ty, tz, roll_deg, lens_mm] (camera on a sphere around the target,
    Blender frame) -> camera dict {location, rotation_matrix (world-from-camera, columns = cam X, Y, Z),
    lens_mm, sensor_width_mm (horizontal fit), resolution [W, H], params}."""
    az, el, dist, tx, ty, tz, roll, lens = [float(v) for v in p]
    t = np.array([tx, ty, tz])
    a, e = math.radians(az), math.radians(el)
    C = t + dist * np.array([math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e)])
    f = t - C
    f /= max(np.linalg.norm(f), 1e-12)
    r = np.cross(f, [0, 0, 1.0])
    if np.linalg.norm(r) < 1e-9:
        r = np.array([1.0, 0, 0])
    r /= np.linalg.norm(r)
    u = np.cross(r, f)
    th = math.radians(roll)
    r2 = r * math.cos(th) + u * math.sin(th)
    u2 = u * math.cos(th) - r * math.sin(th)
    R = np.stack([r2, u2, -f], 1)
    return {"location": [round(float(x), 6) for x in C], "rotation_matrix": R.round(8).tolist(), "lens_mm": lens,
            "sensor_width_mm": sensor, "resolution": [int(res[0]), int(res[1])],
            "params": dict(zip(CAM_PARAM_NAMES, [round(float(x), 5) for x in p]))}


def cam_normalize(cam):
    """Accept the v1 json form ({params: {lens_mm}}) and ours; returns a complete camera dict."""
    c = dict(cam)
    if "lens_mm" not in c:
        c["lens_mm"] = float((c.get("params") or {}).get("lens_mm", 50.0))
    c.setdefault("sensor_width_mm", 36.0)
    if "location" not in c or "rotation_matrix" not in c or "resolution" not in c:
        raise ValueError("camera needs location, rotation_matrix, resolution (and lens_mm)")
    return c


def cam_project(cam, P, res=None):
    """Project world points -> (px, py, depth) in pixels of `res` (default the camera's resolution)."""
    W, H = res or cam["resolution"]
    R = np.array(cam["rotation_matrix"], dtype=np.float64)
    r, u, f = R[:, 0], R[:, 1], -R[:, 2]
    D = np.asarray(P, dtype=np.float64) - np.array(cam["location"], dtype=np.float64)
    zc = D @ f
    fpx = float(cam["lens_mm"]) / float(cam.get("sensor_width_mm", 36.0)) * W
    zs = np.where(np.abs(zc) < 1e-9, 1e-9, zc)
    return W / 2.0 + fpx * (D @ r) / zs, H / 2.0 - fpx * (D @ u) / zs, zc


def tri_raster(px, py, T, w, h, samples_per_px=2.0, max_samples=3000000, seed=0):
    """Silhouette of triangles from projected vertex pixels: area sampling + vertices + 1-px closing."""
    mask = np.zeros((h, w), dtype=bool)
    if len(T) == 0:
        return mask
    P = np.stack([px, py], 1)
    a, b, c = P[T[:, 0]], P[T[:, 1]], P[T[:, 2]]
    area = np.abs((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])) / 2
    n = np.ceil(area * samples_per_px).astype(np.int64)
    tot = int(n.sum())
    if tot > max_samples:
        n = np.ceil(n * (max_samples / float(tot))).astype(np.int64)
    rep_ = np.repeat(np.arange(len(T)), n)
    rng = np.random.default_rng(seed)
    uu, vv = rng.random(len(rep_)), rng.random(len(rep_))
    flip = uu + vv > 1
    uu[flip], vv[flip] = 1 - uu[flip], 1 - vv[flip]
    pts = np.concatenate([a[rep_] + (b[rep_] - a[rep_]) * uu[:, None] + (c[rep_] - a[rep_]) * vv[:, None],
                          P, (a + b) / 2, (b + c) / 2, (a + c) / 2])
    ok = (pts[:, 0] >= -0.5) & (pts[:, 0] < w - 0.5) & (pts[:, 1] >= -0.5) & (pts[:, 1] < h - 0.5)
    pts = pts[ok]
    mask[np.round(pts[:, 1]).astype(int), np.round(pts[:, 0]).astype(int)] = True
    return binary_close(mask, 1)


def nelder_mead(f, x0, steps, iters=400, ftol=1e-5):
    """Plain Nelder-Mead (no scipy in Blender). Returns (x, fx, evaluations)."""
    n = len(x0)
    xs = [np.array(x0, dtype=np.float64)]
    for i in range(n):
        x = xs[0].copy()
        x[i] += steps[i]
        xs.append(x)
    fs = [f(x) for x in xs]
    ev = n + 1
    for _ in range(iters):
        order = np.argsort(fs)
        xs = [xs[i] for i in order]
        fs = [fs[i] for i in order]
        if abs(fs[-1] - fs[0]) < ftol:
            break
        cen = np.mean(xs[:-1], axis=0)
        xr = cen + (cen - xs[-1])
        fr = f(xr)
        ev += 1
        if fr < fs[0]:
            xe = cen + 2.0 * (cen - xs[-1])
            fe = f(xe)
            ev += 1
            xs[-1], fs[-1] = (xe, fe) if fe < fr else (xr, fr)
        elif fr < fs[-2]:
            xs[-1], fs[-1] = xr, fr
        else:
            xc = cen + 0.5 * (xs[-1] - cen)
            fc = f(xc)
            ev += 1
            if fc < fs[-1]:
                xs[-1], fs[-1] = xc, fc
            else:
                for i in range(1, n + 1):
                    xs[i] = xs[0] + 0.5 * (xs[i] - xs[0])
                    fs[i] = f(xs[i])
                    ev += 1
    i = int(np.argmin(fs))
    return xs[i], fs[i], ev


def fit_camera(V, T, mask, log=None, coarse_w=160, refine_w=(320, 640), top=4, seed=0):
    """Perspective camera (cam_from_params form) whose silhouette of the triangles (V, T) best matches the
    target mask (bool HxW at the image resolution): coarse search over view directions (15 deg x 7 elevations,
    lens 50 mm) with a 2D similarity alignment (silhouette area + centroid), then Nelder-Mead on
    [az, el, dist, target xyz, roll, lens] at two working resolutions for the best distinct starts.
    Returns (camera dict at the mask resolution, IoU at the last working resolution, info)."""
    H0, W0 = mask.shape
    V = np.asarray(V, dtype=np.float64)
    T = np.asarray(T, dtype=np.int64)
    lo, hi = V.min(0), V.max(0)
    centre = (lo + hi) / 2
    L = float(np.linalg.norm(hi - lo))
    info = {"coarse": [], "refine": []}

    def work(w):
        m = resize_mask(mask, w)
        return m, (w, m.shape[0])

    mc, resc = work(coarse_w)
    tgt_area = float(mc.sum())
    ys, xs = np.nonzero(mc)
    if not len(xs):
        raise ValueError("paint image mask is empty")
    tgt_c = np.array([xs.mean(), ys.mean()])
    half = np.array([resc[0] / 2.0, resc[1] / 2.0])
    D = 4.0 * L
    fpx = 50.0 / 36.0 * resc[0]
    cands = []
    for az in range(0, 360, 15):
        for el in (-40, -20, 0, 15, 30, 45, 60):
            cam = cam_from_params([az, el, D, centre[0], centre[1], centre[2], 0.0, 50.0], resc)
            px, py, _ = cam_project(cam, V)
            # pass 1 at an arbitrary scale -> silhouette area and centroid; pass 2 similarity-aligned
            k0 = 0.6 * resc[0] / max(px.max() - px.min(), 1e-9)
            pm = np.array([px.mean(), py.mean()])
            m0 = tri_raster((px - pm[0]) * k0 + half[0], (py - pm[1]) * k0 + half[1], T, resc[0], resc[1], seed=seed)
            a0 = float(m0.sum())
            if a0 < 4:
                continue
            yy, xx = np.nonzero(m0)
            c_src = (np.array([xx.mean(), yy.mean()]) - half) / k0 + pm  # silhouette centroid, projection units
            k = k0 * math.sqrt(tgt_area / a0)
            m1 = tri_raster((px - c_src[0]) * k + tgt_c[0], (py - c_src[1]) * k + tgt_c[1], T, resc[0], resc[1],
                            seed=seed)
            iou = mask_iou(m1, mc)
            # equivalent camera: same direction, lens 50 mm, distance D / k, target shifted in the image plane
            dist_new = D / k
            R = np.array(cam["rotation_matrix"])
            r_, u_ = R[:, 0], R[:, 1]
            dpx = (tgt_c - half) - (c_src - half) * k
            tnew = centre - r_ * dpx[0] * dist_new / fpx + u_ * dpx[1] * dist_new / fpx
            cands.append((iou, [float(az), float(el), dist_new, tnew[0], tnew[1], tnew[2], 0.0, 50.0]))
    cands.sort(key=lambda c: -c[0])
    info["coarse"] = [{"iou": round(c[0], 4), "az": c[1][0], "el": c[1][1]} for c in cands[:8]]
    if log is not None:
        log.append({"stage": "camera_coarse", "best": info["coarse"][:4]})
    starts = []
    for iou, p in cands:
        if all(abs(((p[0] - q[0] + 180) % 360) - 180) > 20 or abs(p[1] - q[1]) > 20 for _, q in starts):
            starts.append((iou, p))
        if len(starts) >= top:
            break
    best = None
    for iou0, p in starts:
        p = np.array(p, dtype=np.float64)
        steps = np.array([6.0, 5.0, 0.12 * p[2], 0.04 * L, 0.04 * L, 0.04 * L, 4.0, 8.0])
        fx = 1.0
        for w in refine_w:
            mw, resw = work(w)

            def obj(q, mw=mw, resw=resw):
                if not (8.0 < q[7] < 250.0) or q[2] <= 0.05 * L or not (-89.0 < q[1] < 89.0):
                    return 1.0
                cam = cam_from_params(q, resw)
                px, py, zc = cam_project(cam, V)
                if (zc <= 0.02 * L).any():
                    return 1.0
                return 1.0 - mask_iou(tri_raster(px, py, T, resw[0], resw[1], seed=seed), mw)
            p, fx, ev = nelder_mead(obj, p, steps, iters=500 if w == refine_w[0] else 350)
            steps = steps * 0.35
            info["refine"].append({"start_iou": round(iou0, 4), "az0": starts[0][1][0], "w": w,
                                   "iou": round(1 - fx, 4), "evals": ev})
        if best is None or fx < best[0]:
            best = (fx, p)
    if log is not None:
        log.append({"stage": "camera_refine", "runs": info["refine"]})
    cam = cam_from_params(best[1], (W0, H0))
    return cam, round(1.0 - best[0], 4), info


def render_text_mask(lines, font_path, px_per_m, size_m, bold_px=0, align="LEFT", line_gap=1.18, aa="16",
                     scales=None, pad_px=4, slashed_zero=False):
    """Rasterise text with Blender's font engine (Workbench, orthographic, flat white on transparent) into an
    alpha mask of size_m x px_per_m pixels; the text block is fitted inside the rectangle (aspect kept, left /
    centre aligned). lines: strings stacked top -> bottom; scales: relative size per line. bold_px > 0 thickens
    the strokes by a grey dilation of the mask (the FONT outline offset self-intersects and leaves holes).
    slashed_zero: a short slash inside every '0' (measured from prefix widths, like a stencil zero).
    Returns float32 HxW alpha (top row first)."""
    scene = bpy.data.scenes.new("af_text_tmp")
    vl = scene.view_layers[0]
    font = bpy.data.fonts.load(os.path.abspath(font_path), check_existing=True)
    tmp_objs = []

    def text_obj(txt, size, loc, name="af_txt"):
        cu = bpy.data.curves.new(name, type="FONT")
        cu.body = txt
        cu.font = font
        cu.size = float(size)
        cu.align_x = "CENTER" if align == "CENTER" else "LEFT"
        cu.align_y = "TOP"
        ob = bpy.data.objects.new(name, cu)
        scene.collection.objects.link(ob)
        ob.location = loc
        tmp_objs.append(ob)
        return ob

    def bounds(objs):
        vl.update()
        dg = vl.depsgraph
        pts = []
        for ob in objs:
            me = bpy.data.meshes.new_from_object(ob.evaluated_get(dg), depsgraph=dg)
            M = ob.matrix_world
            pts += [tuple((M @ v.co)[:2]) for v in me.vertices]
            bpy.data.meshes.remove(me)
        if not pts:
            return None
        P = np.array(pts)
        return P.min(0), P.max(0)

    scales = scales or [1.0] * len(lines)
    objs, y = [], 0.0
    for i, (txt, sc) in enumerate(zip(lines, scales)):
        objs.append(text_obj(txt, sc, (0.0, y, 0.0), "af_txt_%d" % i))
        y -= float(sc) * line_gap
    slashes = []
    if slashed_zero and align != "CENTER":
        for i, (txt, sc) in enumerate(zip(lines, scales)):
            y0 = objs[i].location.y
            for k, ch in enumerate(txt):
                if ch != "0":
                    continue
                pre = text_obj(txt[:k], sc, (0.0, y0 - 50.0, 0.0), "af_meas_a") if k else None
                cur = text_obj(txt[:k + 1], sc, (0.0, y0 - 50.0, 0.0), "af_meas_b")
                zero = text_obj("0", sc, (0.0, y0 - 50.0, 0.0), "af_meas_c")
                b_pre = bounds([pre]) if pre is not None else None
                b_cur, b_zero = bounds([cur]), bounds([zero])
                for o_ in (pre, cur, zero):
                    if o_ is not None:
                        cu_ = o_.data
                        tmp_objs.remove(o_)
                        bpy.data.objects.remove(o_)
                        bpy.data.curves.remove(cu_)
                if b_cur is None or b_zero is None:
                    continue
                x1 = b_cur[1][0]
                zw = b_zero[1][0] - b_zero[0][0]
                x0 = x1 - zw
                zy0, zy1 = b_zero[0][1] + 50.0, b_zero[1][1] + 50.0
                slashes.append((x0, x1, zy0, zy1))
    for k, (x0, x1, zy0, zy1) in enumerate(slashes):
        w, h = x1 - x0, zy1 - zy0
        a = Vector((x0 + w * 0.2, zy0 + h * 0.18, 0.0))
        b = Vector((x1 - w * 0.2, zy1 - h * 0.18, 0.0))
        d = (b - a).normalized()
        nrm = Vector((-d.y, d.x, 0.0)) * (w * 0.085)
        me = bpy.data.meshes.new("af_slash_%d" % k)
        me.from_pydata([a - nrm, b - nrm, b + nrm, a + nrm], [], [(0, 1, 2, 3)])
        ob = bpy.data.objects.new("af_slash_%d" % k, me)
        scene.collection.objects.link(ob)
        tmp_objs.append(ob)
    Wpx, Hpx = max(8, int(round(size_m[0] * px_per_m))), max(8, int(round(size_m[1] * px_per_m)))
    bb = bounds(objs)
    try:
        if bb is None:
            return np.zeros((Hpx, Wpx), np.float32)
        lo, hi = bb
        tw, th = hi - lo
        pad = pad_px + bold_px
        s = min((Wpx - 2 * pad) / max(tw, 1e-6), (Hpx - 2 * pad) / max(th, 1e-6))  # px per text unit
        cam_d = bpy.data.cameras.new("af_txt_cam")
        cam_d.type = "ORTHO"
        cam_d.sensor_fit = "AUTO"
        cam_d.ortho_scale = max(Wpx, Hpx) / s
        cam = bpy.data.objects.new("af_txt_cam", cam_d)
        scene.collection.objects.link(cam)
        tmp_objs.append(cam)
        cx = (lo[0] + hi[0]) / 2 if align == "CENTER" else lo[0] - pad / s + (Wpx / s) / 2
        cam.location = (cx, (lo[1] + hi[1]) / 2, 10.0)
        scene.camera = cam
        r = scene.render
        r.engine = af.engine_id("WORKBENCH")
        r.resolution_x, r.resolution_y = Wpx, Hpx
        r.resolution_percentage = 100
        r.film_transparent = True
        r.image_settings.file_format = "PNG"
        r.image_settings.color_mode = "RGBA"
        try:
            scene.display.render_aa = aa
        except TypeError:
            pass
        sh = scene.display.shading
        sh.light = "FLAT"
        sh.color_type = "SINGLE"
        sh.single_color = (1.0, 1.0, 1.0)
        try:
            scene.view_settings.view_transform = "Standard"
        except TypeError:
            pass
        tmp = tempfile.mkdtemp(prefix="af_txt_")
        try:
            r.filepath = os.path.join(tmp, "t.png")
            bpy.ops.render.render(write_still=True, scene=scene.name)
            a = load_png_rgba(r.filepath)[..., 3]
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        if bold_px > 0:
            a = max_filter(a.astype(np.float32), int(round(bold_px)))
        return np.clip(a, 0, 1).astype(np.float32)
    finally:
        for ob in tmp_objs:
            data = ob.data
            bpy.data.objects.remove(ob)
            if isinstance(data, bpy.types.Curve):
                bpy.data.curves.remove(data)
            elif isinstance(data, bpy.types.Mesh):
                bpy.data.meshes.remove(data)
            elif isinstance(data, bpy.types.Camera):
                bpy.data.cameras.remove(data)
        bpy.data.scenes.remove(scene)
