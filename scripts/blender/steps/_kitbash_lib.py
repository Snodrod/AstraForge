"""_kitbash_lib — procedural detail parts for the kitbash step (scripts/blender/steps/kitbash.py).

Every builder writes into an _ship_lib.Kit (one bmesh, AF material index per face) in a local part frame set
through kit.pre (a 4x4 world matrix), so one call works for LOD 0 / 1 / 2 (Kit.lod drops small details and
segments). Frames: frame_matrix(origin, y_axis, z_hint) -> columns X, Y, Z with Y = the part's main axis.

  gatling   Y = fire direction (out of the hull), Z = up; origin on the bay wall (seat)
  nozzle    Y = exhaust direction (aft), Z = up; origin at the centre of the exit plane, bell towards -Y
  antenna   Z = up (surface normal), origin on the hull surface
  nav_light Z = outward surface normal, origin on the hull surface
  vent      Z = outward surface normal, Y = length direction of the grille, origin on the surface
  piping    world space: a smooth tube through points (Catmull-Rom), parallel copies, flanges

Region tests for removing AI geometry (cylinder / sphere / box) live here too.
"""
import math

import bmesh
import numpy as np
from mathutils import Matrix, Vector

from steps import _ship_lib as sl


# ----------------------------------------------------------------------------- frames
def frame_matrix(origin, y_axis, z_hint=(0, 0, 1)):
    y = Vector(y_axis).normalized()
    z = Vector(z_hint)
    z = z - y * z.dot(y)
    if z.length < 1e-6:
        for alt in ((0, 0, 1), (1, 0, 0), (0, 1, 0)):
            z = Vector(alt) - y * Vector(alt).dot(y)
            if z.length > 1e-6:
                break
    z.normalize()
    x = y.cross(z)
    M = Matrix((x, y, z)).transposed().to_4x4()
    M.translation = Vector(origin)
    return M


def frame_z(origin, z_axis, y_hint=(0, 1, 0)):
    """Frame whose local Z = z_axis (surface normal) and local Y ~ y_hint."""
    z = Vector(z_axis).normalized()
    y = Vector(y_hint)
    y = y - z * y.dot(z)
    if y.length < 1e-6:
        for alt in ((0, 1, 0), (1, 0, 0), (0, 0, 1)):
            y = Vector(alt) - z * Vector(alt).dot(z)
            if y.length > 1e-6:
                break
    y.normalize()
    x = y.cross(z)
    M = Matrix((x, y, z)).transposed().to_4x4()
    M.translation = Vector(origin)
    return M


class Placed:
    """Context manager: kit.pre = M while building one part."""

    def __init__(self, kit, M):
        self.kit, self.M = kit, M

    def __enter__(self):
        self.old = self.kit.pre
        self.kit.pre = self.M
        return self.kit

    def __exit__(self, *a):
        self.kit.pre = self.old


# ----------------------------------------------------------------------------- regions
def in_region(P, reg):
    """Bool mask of points P (N x 3) inside a region {cylinder: {p0, p1, radius}} | {sphere: {center, radius}}
    | {box: {min, max}}."""
    P = np.asarray(P, dtype=np.float64)
    if "cylinder" in reg:
        c = reg["cylinder"]
        p0, p1 = np.array(c["p0"], float), np.array(c["p1"], float)
        u = p1 - p0
        ln = float(np.linalg.norm(u))
        u = u / max(ln, 1e-12)
        d = P - p0
        t = d @ u
        rad = np.linalg.norm(d - np.outer(t, u), axis=1)
        return (t >= 0) & (t <= ln) & (rad <= float(c["radius"]))
    if "sphere" in reg:
        s = reg["sphere"]
        return np.linalg.norm(P - np.array(s["center"], float), axis=1) <= float(s["radius"])
    if "box" in reg:
        b = reg["box"]
        return np.all((P >= np.array(b["min"], float)) & (P <= np.array(b["max"], float)), axis=1)
    raise ValueError("region needs cylinder, sphere or box: %r" % (reg,))


def remove_faces(ob, regions, cap=False):
    """Delete the faces of a mesh object whose centre (world) lies in any region; cap=True fills the new
    holes (bmesh holes_fill on the edges opened by the cut). Returns the number of faces removed."""
    me = ob.data
    if not len(me.polygons) or not regions:
        return 0
    M = np.array(ob.matrix_world)
    C = np.empty(len(me.polygons) * 3)
    me.polygons.foreach_get("center", C)
    C = C.reshape(-1, 3) @ M[:3, :3].T + M[:3, 3]
    kill = np.zeros(len(C), bool)
    for reg in regions:
        kill |= in_region(C, reg)
    n = int(kill.sum())
    if not n:
        return 0
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.faces.ensure_lookup_table()
    dead = [bm.faces[i] for i in np.where(kill)[0]]
    edges = {e for f in dead for e in f.edges}
    bmesh.ops.delete(bm, geom=dead, context="FACES")
    if cap:
        opened = [e for e in edges if e.is_valid and e.is_boundary]
        if opened:
            try:
                bmesh.ops.holes_fill(bm, edges=opened, sides=0)
            except (RuntimeError, ValueError):
                pass
    loose = [v for v in bm.verts if not v.link_faces]
    if loose:
        bmesh.ops.delete(bm, geom=loose, context="VERTS")
    bm.to_mesh(me)
    bm.free()
    me.update()
    return n


# ----------------------------------------------------------------------------- parts
def gatling(kit, M, barrel_len=5.5, cluster_d=None, housing=None, barrels=6):
    """Rotary gatling with receiver housing. Local frame: origin on the wall, +Y out (fire), +Z up.
    Returns dict(muzzle=local point, length=total length)."""
    c = float(cluster_d or barrel_len * 0.2)
    hl, hw, hh = housing or (barrel_len * 0.36, c * 1.75, c * 1.55)
    with Placed(kit, M):
        # wall mount plate + pivot ring
        kit.box((0, 0.09, 0), (hw * 1.3, 0.18, hh * 1.3), "graphite", bevel=0.05)
        kit.cyl((0, 0.2, 0), hh * 0.52, 0.12, axis=(0, 1, 0), mat="trim", segs=20, detail=0.3)
        y0 = 0.26
        # receiver: slightly tapered box, dark, with cooling fins and a feed chute
        kit.box((0, y0 + hl / 2, 0), (hw, hl, hh), "graphite", bevel=min(hw, hh) * 0.08, taper=(0.86, 0.9))
        if kit.lod <= 1:
            kit.box((0, y0 + hl * 0.42, hh * 0.5 + hh * 0.16), (hw * 0.55, hl * 0.62, hh * 0.32), "trim",
                    bevel=0.03, detail=0.35)
            for k in range(4):
                yy = y0 + hl * (0.22 + 0.17 * k)
                for sx in (-1, 1):
                    kit.box((sx * hw * 0.5, yy, 0), (0.06, hl * 0.07, hh * 0.78), "trim", detail=0.25)
            kit.cyl((hw * 0.58, y0 + hl * 0.45, -hh * 0.28), c * 0.09, hl * 0.9, axis=(0, 1, 0), mat="trim",
                    segs=8, detail=0.2)
            kit.box((-hw * 0.505, y0 + hl * 0.78, hh * 0.18), (0.05, 0.16, 0.12), "status", detail=0.1)
        # barrel cluster
        muzzle = sl.rotary_barrels(kit, (0, y0 + hl, 0), barrel_len, c, n=barrels)
    return {"muzzle": muzzle, "length": y0 + hl + barrel_len}


def ring(kit, y, r_out, r_in, depth, mat="trim", segs=32):
    """Open ring (a short tube with a hole) around the current frame's Y axis at local y (use inside Placed)."""
    Ml = Matrix.Translation((0.0, y, 0.0)) @ Matrix.Rotation(math.radians(90), 4, "X")
    d = depth / 2.0
    kit.lathe([(r_in, -d), (r_out, -d), (r_out, d), (r_in, d)], Ml, [mat], segs=segs)


def bell_radius(a, L, rt, re, k=1.7):
    """Outer bell radius at axial position a in [-L, 0] (exit at 0): throat rt -> exit re."""
    t = min(1.0, max(0.0, (a + L) / L))
    return rt + (re - rt) * t ** k


def nozzle(kit, M, D=5.4, length=None, throat_d=None, collar_len=None, ribs=28, bands=3, actuators=4,
           inner="drive", plate_d=None, outer_glow="status", inner_rings=2):
    """Ribbed rocket bell. Local frame: origin = exit plane centre, +Y = exhaust (aft), +Z up; the bell runs
    towards -Y. Dark ribbed outer shell (graphite) with steel bands and rim, glowing inner surface (drive) and a
    hot throat disc, steel collar with flanges, actuator pistons, a mount plate that closes the nacelle."""
    re = D / 2.0
    L = float(length or D * 0.8)
    rt = float(throat_d or D * 0.5) / 2.0
    cl = float(collar_len or D * 0.28)
    t = max(0.06, D * 0.025)
    # lathe profile (r, a) revolves around the lathe Z axis; RotX(+90) maps lathe +Z onto part -Y, so a = -y
    Ml = M @ Matrix.Rotation(math.radians(90), 4, "X")
    n = 9 if kit.lod < 2 else 4
    outer = [(bell_radius(-L + L * i / (n - 1), L, rt, re), L - L * i / (n - 1)) for i in range(n)]  # a = -y
    inner_pts = [(max(0.05, bell_radius(-L + L * i / (n - 1), L, rt, re) - t), L - L * i / (n - 1))
                 for i in range(n - 1, -1, -1)]
    prof = outer + [(re - t, 0.0)] + inner_pts[1:]
    # inner surface two-tone: the deep part glows hottest (drive), the band near the mouth dimmer (outer_glow;
    # a dark band reads deeper head-on but hides the glow in 3/4 views, where the concept shows it)
    n_out = max(1, int(round((n - 1) * 0.4)))
    mats = ["graphite"] * (n - 1) + ["trim"] + [outer_glow] * n_out + [inner] * (n - 1 - n_out) + [inner]
    kit.lathe(prof, Ml, mats, segs=32)
    with Placed(kit, M):
        # hot throat (deep inside) + collar
        kit.cyl((0, -L + t * 0.5, 0), max(0.05, rt - t), t, axis=(0, 1, 0), mat=inner, segs=24)
        kit.cyl((0, -L - cl / 2, 0), rt * 1.18, cl, axis=(0, 1, 0), mat="graphite", segs=28, bevel=0.04)
        for fy in (-L - 0.06, -L - cl + 0.08):
            kit.cyl((0, fy, 0), rt * 1.32, 0.14, axis=(0, 1, 0), mat="trim", segs=28, detail=0.4)
        # mount plate closing the nacelle behind the collar
        pd = float(plate_d or D * 1.08)
        kit.cyl((0, -L - cl - 0.12, 0), pd / 2, 0.24, axis=(0, 1, 0), mat="graphite", segs=32, bevel=0.05)
        # exit rim + bands: open rings (a closed cylinder would cap the mouth and hide the glow)
        if kit.lod <= 1:
            ring(kit, -0.07, re + 0.05, re - t * 0.6, 0.16, "trim", segs=32)
            for k in range(bands):
                a = -L * (0.25 + 0.5 * k / max(bands - 1, 1))
                r = bell_radius(a, L, rt, re)
                ring(kit, a, r + 0.06, r - t * 0.3, 0.13, "trim", segs=28)
        # internal stiffener rings: darker circles inside the glowing mouth give it depth (a flat glow disc
        # otherwise; the concept's bell interiors are ribbed)
        if kit.lod == 0:
            for k in range(int(inner_rings)):
                a = -L * (0.3 + 0.4 * k / max(int(inner_rings) - 1, 1))
                r = bell_radius(a, L, rt, re) - t
                ring(kit, a, r + 0.01, r - max(0.06, D * 0.018), 0.1, "trim", segs=24)
        # longitudinal ribs (two chords per rib along the bell generatrix)
        nr = ribs if kit.lod == 0 else (ribs // 2 if kit.lod == 1 else 0)
        for k in range(nr):
            th = k * math.tau / nr
            cs, sn = math.cos(th), math.sin(th)
            for a0, a1 in ((-L * 0.97, -L * 0.5), (-L * 0.5, -L * 0.04)):
                r0, r1 = bell_radius(a0, L, rt, re) + 0.04, bell_radius(a1, L, rt, re) + 0.04
                p0 = Vector((r0 * cs, a0, r0 * sn))
                p1 = Vector((r1 * cs, a1, r1 * sn))
                mid = (p0 + p1) / 2
                ln = (p1 - p0).length
                yv = (p1 - p0).normalized()
                outv = Vector((cs, 0, sn))
                xv = yv.cross(outv).normalized()
                zv = xv.cross(yv)
                R = Matrix((xv, yv, zv)).transposed()
                kit.box(tuple(mid), (D * 0.022, ln, D * 0.03), "graphite", rot=R, detail=D * 0.03)
        # actuator pistons from the collar to the bell
        na = actuators if kit.lod <= 1 else 0
        for k in range(na):
            th = math.radians(45) + k * math.tau / max(na, 1)
            cs, sn = math.cos(th), math.sin(th)
            a0 = Vector((rt * 1.3 * cs, -L - cl * 0.55, rt * 1.3 * sn))
            am = -L * 0.45
            rm = bell_radius(am, L, rt, re) + 0.12
            a1 = Vector((rm * cs, am, rm * sn))
            v = a1 - a0
            mid = a0 + v * 0.3
            kit.cyl(tuple(mid), D * 0.045, v.length * 0.6, axis=tuple(v), mat="trim", segs=10, detail=0.3)
            kit.cyl(tuple(a0 + v * 0.72), D * 0.022, v.length * 0.56, axis=tuple(v), mat="graphite", segs=8,
                    detail=0.2)
    return {"length": L + cl, "exit_d": D}


def _catmull(points, n_per=8):
    P = [Vector(p) for p in points]
    if len(P) < 2:
        return P
    P = [P[0] + (P[0] - P[1])] + P + [P[-1] + (P[-1] - P[-2])]
    out = []
    for i in range(1, len(P) - 2):
        p0, p1, p2, p3 = P[i - 1], P[i], P[i + 1], P[i + 2]
        for k in range(n_per):
            t = k / float(n_per)
            t2, t3 = t * t, t * t * t
            out.append(0.5 * ((2 * p1) + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2
                              + (-p0 + 3 * p1 - 3 * p2 + p3) * t3))
    out.append(P[-2])
    return out


def tube(kit, points, radius, mat="trim", sides=8, n_per=5, flanges=True, flange_mat="graphite"):
    """Smooth tube through world points (Catmull-Rom), rings with parallel-transported frames, end caps and
    optional flanges."""
    pts = _catmull(points, n_per if kit.lod == 0 else max(2, n_per // 2))
    if len(pts) < 2:
        return
    sides = kit.segs(sides, 6)
    tangents = []
    for i in range(len(pts)):
        a = pts[max(0, i - 1)]
        b = pts[min(len(pts) - 1, i + 1)]
        tangents.append((b - a).normalized())
    up = Vector((0, 0, 1)) if abs(tangents[0].z) < 0.9 else Vector((1, 0, 0))
    nrm = (up - tangents[0] * up.dot(tangents[0])).normalized()
    rings = []
    for i, (p, tg) in enumerate(zip(pts, tangents)):
        nrm = (nrm - tg * nrm.dot(tg))
        if nrm.length < 1e-6:
            nrm = Vector((1, 0, 0)) - tg * tg.x
        nrm.normalize()
        bn = tg.cross(nrm)
        rings.append([p + (nrm * math.cos(a) + bn * math.sin(a)) * radius
                      for a in [k * math.tau / sides for k in range(sides)]])
    old = kit.pre
    kit.pre = Matrix.Identity(4)
    try:
        kit.loft([[tuple(v) for v in r] for r in rings], mat, cap0="fan", cap1="fan")
        if flanges and kit.lod <= 1:
            for p, tg in ((pts[0], tangents[0]), (pts[-1], tangents[-1])):
                kit.cyl(tuple(p - tg * radius * 0.2), radius * 1.45, radius * 0.6, axis=tuple(tg), mat=flange_mat,
                        segs=sides + 2, detail=radius)
    finally:
        kit.pre = old


def antenna(kit, M, height=3.0, style="whip", light=True, radius=None):
    """Antennas on a base: whip (tapered rod, amber tip), mast (thick post with cap and cross bars), rod
    (thin stub), dome (sensor hemisphere). Local Z = up (surface normal), origin on the surface."""
    H = float(height)
    with Placed(kit, M):
        if style == "dome":
            R = float(radius or H * 0.35)
            kit.cyl((0, 0, R * 0.12), R * 1.12, R * 0.24, axis=(0, 0, 1), mat="graphite", segs=24)
            kit.sphere((0, 0, R * 0.22), R, "graphite", segs=20, hemi=True)
            if light:
                kit.sphere((0, 0, R * 1.22), R * 0.12, "status", segs=8, detail=0.05)
            return {"tip": (0, 0, R * 1.3)}
        base = float(radius or max(0.12, H * 0.06))
        kit.box((0, 0, base * 0.9), (base * 3.2, base * 3.2, base * 1.8), "graphite", bevel=base * 0.3)
        if style == "mast":
            kit.cyl((0, 0, base * 1.8 + H * 0.5), base, H, axis=(0, 0, 1), mat="trim", segs=12, r2=base * 0.8)
            kit.cyl((0, 0, base * 1.8 + H + base * 0.4), base * 1.35, base * 0.8, axis=(0, 0, 1), mat="graphite",
                    segs=12)
            for zt in (0.45, 0.75):
                kit.box((0, 0, base * 1.8 + H * zt), (base * 5, base * 0.5, base * 0.5), "trim", detail=0.1)
            tip = base * 1.8 + H + base * 0.8
        elif style == "rod":
            kit.cyl((0, 0, base * 1.8 + H * 0.5), base * 0.5, H, axis=(0, 0, 1), mat="trim", segs=8)
            tip = base * 1.8 + H
        else:  # whip
            kit.cyl((0, 0, base * 1.8 + H * 0.12), base * 0.75, H * 0.24, axis=(0, 0, 1), mat="trim", segs=10)
            kit.cyl((0, 0, base * 1.8 + H * 0.24 + H * 0.38), base * 0.32, H * 0.76, axis=(0, 0, 1), mat="trim",
                    r2=base * 0.12, segs=8)
            tip = base * 1.8 + H
        if light:
            kit.sphere((0, 0, tip + base * 0.3), base * 0.55, "status", segs=10, detail=0.05)
            tip += base * 0.8
    return {"tip": (0, 0, tip)}


def nav_light(kit, M, size=0.5, lens="status"):
    """Small light housing with an emissive lens. Local Z = outward normal, origin on the surface."""
    s = float(size)
    with Placed(kit, M):
        kit.box((0, 0, s * 0.18), (s, s * 0.7, s * 0.36), "graphite", bevel=s * 0.06)
        kit.box((0, 0, s * 0.42), (s * 0.62, s * 0.4, s * 0.16), lens, bevel=s * 0.04)
    return {"tip": (0, 0, s * 0.5)}


def vent_grille(kit, M, size=(4.0, 1.4), slats=6, depth=0.12):
    """Raised frame with angled slats. Local Z = outward normal, Y = length (size[0]), X = height."""
    L_, Hh = float(size[0]), float(size[1])
    f = max(0.08, Hh * 0.08)
    with Placed(kit, M):
        kit.box((0, 0, depth * 0.5), (Hh, f, depth), "graphite", bevel=f * 0.2)
        for sy in (-1, 1):
            kit.box((0, sy * (L_ / 2 - f / 2), depth * 0.5), (Hh, f, depth), "graphite")
        for sx in (-1, 1):
            kit.box((sx * (Hh / 2 - f / 2), 0, depth * 0.5), (f, L_, depth), "graphite")
        n = int(slats) if kit.lod == 0 else max(2, int(slats) // 2)
        pitch = (L_ - 2 * f) / n
        R = Matrix.Rotation(math.radians(35), 3, "X")
        for k in range(n):
            y = -L_ / 2 + f + pitch * (k + 0.5)
            kit.box((0, y, depth * 0.45), (Hh - 2 * f, pitch * 0.32, depth * 0.7), "trim", rot=R, detail=0.15)
        kit.box((0, 0, depth * 0.08), (Hh - 2 * f, L_ - 2 * f, depth * 0.1), "panel")
    return {}


def block(kit, M, size=(1.0, 1.0, 1.0), mat="graphite", bevel=0.05, taper=(1.0, 1.0), panel=""):
    """Bevelled greeble block centred at the frame origin (size along the frame X, Y, Z); optional raised
    top panel in another material."""
    sx, sy, sz = (float(v) for v in size)
    with Placed(kit, M):
        kit.box((0, 0, 0), (sx, sy, sz), mat, bevel=bevel, taper=taper)
        if panel:
            kit.box((0, 0, sz / 2 + 0.03), (sx * taper[0] * 0.8, sy * taper[1] * 0.8, 0.06), panel,
                    detail=min(sx, sy) * 0.3)
    return {}
