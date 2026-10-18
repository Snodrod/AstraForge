"""ship_gen — procedural spaceship per the AstraForge ship contract.

Request params (all optional except class):
  class           fighter | interceptor (ship_s); gunship | corvette | freighter | miner (ship_m);
                  frigate | carrier | transport (ship_l)
  seed            int (default 1). The seed walks through the role's three layout families (FAMILIES:
                  consecutive seeds never share one; the starting family depends on role + faction) and
                  draws proportions, part options, engine count, palette variant and greeble placement from
                  an RNG seeded with crc32(seed, role).
  layout          auto (default) | family name | index — force one layout family (see FAMILIES)
  length_m        hull length incl. nozzles (default per role, see _ship_lib.ROLES). Outside the role's
                  class range the budget class follows the length (with a warning).
  faction         industrial | military | sleek | alien (silhouette language + palette), default military
  turret_count    int >= 0 (default per role; limited to the hardpoints the layout offers, with a warning)
  engine_count    1..6 (default from the seed within the role range; clamped with a warning)
  wing_style      auto | none | delta | swept | forward | straight | x | stub | crescent (fighter /
                  interceptor); gunship: auto | none | stub; other roles ignore it (warning)
  greeble_density 0..1 (default 0.6)
  livery          {"armor"|"graphite"|"trim"|"panel"|"glass"|"status"|"drive"|"marking": "#rrggbb" or [r,g,b]}
  palette_variant 0..2 (default from the seed)
  thruster_frame  thrust (default) | exhaust — thruster socket frame
                  (see _ship_lib docstring); written into socket extras and the manifest axis text
  asset_id        default ship_<role>_<faction>_<seed:03d>
  tier            int (default 1)
  lods            bool (default true): ship_lod1.glb, ship_lod2.glb
  preview         bool (default true); preview_turrets bool (default true, preview-only turret copies)
  preview_size    px per tile (default 300)
  uv              smart | box (default smart)

Outputs (out_dir): ship.glb, ship.blend (after join), ship.json (manifest), ship.definition.json
(resolved params + chosen layout), ship_lod1.glb, ship_lod2.glb, preview.png.
Axes: nose +Y Blender (-Z glTF), root at the hull's geometric centre, mirror X = 0.
"""
import math
import os
import random
import zlib

import bpy
from mathutils import Matrix, Vector

import af_common as af
from steps import _ship_lib as sl

PI = math.pi

FAMILIES = {
    "fighter": ("arrow", "twin_boom", "dart"),
    "interceptor": ("arrow", "twin_boom", "dart"),
    "gunship": ("brick", "hammer", "catamaran"),
    "corvette": ("destroyer", "spear", "arrowhead"),
    "freighter": ("truss", "flatbed", "bulk"),
    "miner": ("pods", "crane", "scoop"),
    "frigate": ("wedge", "hammer", "fork"),
    "carrier": ("flattop", "hangar_pods", "stepdeck"),
    "transport": ("pods", "drums", "barge"),
}
WING_STYLES = ("auto", "none", "delta", "swept", "forward", "straight", "x", "stub", "crescent")


# ============================================================================= design core
class Design:
    def __init__(self, P, kit):
        self.P = P
        self.k = kit
        self.role = P["class"]
        self.style = P["faction"]
        self.L = float(P["nominal_length"])
        self.g = float(P["greeble_density"])
        self.rng = random.Random(int(P["seed"]) * 1000003 + zlib.crc32(self.role.encode()))
        self.prof = sl.PROFILES[self.style]
        self.sockets = []
        self.hard = []
        self.engines = []
        self.navs = {}
        self.notes = []
        self.layout = {}
        self.sharp_angle = 34.0 if self.style in ("industrial", "military") else 44.0

    # ---------------------------------------------------------------- bookkeeping
    def sock(self, name, kind, pos, z=(0, 0, 1), y=(0, 1, 0), priority=5, **extras):
        if any(s["name"] == name for s in self.sockets):
            raise ValueError("duplicate socket %s" % name)
        self.sockets.append(dict(name=name, kind=kind, pos=Vector(pos), z=Vector(z), y=Vector(y),
                                 priority=priority, extras=extras))

    def chance(self, p):
        """Random draw that always consumes the RNG (keeps LOD rebuilds identical)."""
        return self.rng.random() < p

    def U(self, a, b):
        return self.rng.uniform(a, b)

    def family(self, names):
        """Primary layout family. params.layout (name or index) wins; otherwise the seed walks through the
        families so consecutive seeds never share one (start offset from role + faction)."""
        n = len(names)
        lay = self.P.get("layout")
        idx = None
        if lay not in (None, "", "auto"):
            if isinstance(lay, str) and lay.lower() in names:
                idx = names.index(lay.lower())
            else:
                try:
                    idx = int(lay) % n
                except (TypeError, ValueError):
                    self.notes.append("layout %r unknown for %s (expected %s); auto used"
                                      % (lay, self.role, ", ".join(names)))
        if idx is None:
            off = zlib.crc32(("%s|%s" % (self.role, self.style)).encode()) % n
            idx = (int(self.P["seed"]) - 1 + off) % n
        self.layout["family"] = names[idx]
        return names[idx]

    def opt(self, key, choices):
        """Seeded discrete option, recorded in the layout summary."""
        v = self.rng.choice(choices)
        self.layout[key] = v
        return v

    # ---------------------------------------------------------------- hull
    def hull(self, ctrl, W, H, y0, y1, prof=None, n=None, colors=True, panels=True, mat="armor", xo=0.0,
             cap1="fan"):
        """Lofted hull through stations from ctrl rows (t, hw, hh, zc). xo > 0 builds a mirrored pair of
        hulls centred at x = +-xo (twin hull); the returned HullShape is in the hull's own frame."""
        prof = prof or self.prof
        if n is None:
            n = int((y1 - y0) / (max(W, H) * 0.62))
            n = max(10, min(34, n))
        st = []
        for k in range(n):
            u = k / (n - 1.0)
            t = 0.55 * u + 0.45 * (1 - math.cos(PI * u)) / 2.0
            hw, hh, zc = sl.smooth_interp(ctrl, t)
            st.append((y0 + (y1 - y0) * t, hw * W, hh * H, zc * H))
        hs = sl.HullShape(st, prof)
        fm = self.hull_colors(hs, y0, y1) if colors else None
        ins = self.hull_panels(y0, y1, W, H) if panels else None
        if xo:
            pre0 = self.k.pre
            self.k.pre = pre0 @ Matrix.Translation((xo, 0.0, 0.0))
            try:
                self.k.mirrored(self.k.loft, hs.rings(), mat, cap0="fan", cap1=cap1, face_mat=fm, inset=ins)
            finally:
                self.k.pre = pre0
        else:
            self.k.loft(hs.rings(), mat, cap0="fan", cap1=cap1, face_mat=fm, inset=ins)
        return hs

    def hull_colors(self, hs, y0, y1):
        st = self.style
        L = y1 - y0
        stripe_t = 0.72 + 0.08 * self.rng.random()

        def fm(i, j, c, n):
            t = (c.y - y0) / L
            if st == "military":
                if n.z < -0.55:
                    return "graphite"
                if stripe_t < t < stripe_t + 0.045 and abs(n.x) > 0.45:
                    return "marking"
                if t < 0.045:
                    return "graphite"
            elif st == "industrial":
                if stripe_t + 0.05 < t < stripe_t + 0.1 and n.z > -0.3:
                    return "marking" if j % 2 else "graphite"
                if t < 0.05 or n.z < -0.75:
                    return "graphite"
            elif st == "sleek":
                if n.z < -0.4:
                    return "graphite"
                if j in (4, 15) and 0.12 < t < 0.9:
                    return "marking"
            elif st == "alien":
                if n.z < -0.6:
                    return "graphite"
            return None
        return fm

    def hull_panels(self, y0, y1, W, H):
        th = max(0.012, 0.03 * min(W, H))
        st = self.style
        L = y1 - y0

        def sel(i, j, c, n):
            t = (c.y - y0) / L
            if t > 0.9 or t < 0.03:
                return None
            h = (i * 7 + j * 13) % 11
            if st == "military":
                if n.z > 0.8 and h < 2:
                    return "panel"
                return "plate" if (abs(n.x) > 0.6 and h % 3 == 0) else "raise"
            if st == "industrial":
                return "panel" if h in (0, 5) else "raise"
            if st == "sleek":
                return "raise" if i % 2 == 0 else None
            if st == "alien":
                # glowing seams follow whole station loops over the back (one inset region per row)
                if 0.16 < t < 0.84 and n.z > 0.05 and i % 3 == 1:
                    return "glowband"
                return None
            return None
        return {"thickness": th, "depth": th * 0.45, "select": sel}

    # ---------------------------------------------------------------- canopy / superstructure
    def canopy(self, hs, y0, y1, cw, ch, x=0.0, embed=0.25, mat="glass", frame=True):
        k = self.k
        m = 9
        npts = 11
        rings, tops = [], []
        for q in range(1, m):
            u = q / float(m)
            s = math.sin(PI * u ** 0.75) ** 0.6
            y = y0 + (y1 - y0) * u
            zb = hs.top(y, x) - embed * ch
            ring = []
            for a_i in range(npts):
                a = PI * a_i / (npts - 1)
                ring.append((x + cw * s * math.cos(a), y, zb + ch * s * math.sin(a)))
            rings.append(ring)
            tops.append((x, y, zb + ch * s))
        zb0 = hs.top(y0, x) - embed * ch
        zb1 = hs.top(y1, x) - embed * ch
        k.loft(rings, mat, cap0="apex", cap1="apex", apex0=(x, y0, zb0), apex1=(x, y1, zb1))
        if frame:
            # centre spine + two hoops (graphite) — reads as a framed canopy
            w = max(0.02, cw * 0.07)
            sp = [[(px - w, py, pz - w * 0.3), (px + w, py, pz - w * 0.3), (px + w, py, pz + w * 0.8),
                   (px - w, py, pz + w * 0.8)] for px, py, pz in tops[1:-1]]
            if len(sp) >= 2:
                k.loft(sp, "graphite", detail=cw * 0.2)
            for q in (3, 5):
                u = q / float(m)
                s = math.sin(PI * u ** 0.75) ** 0.6
                y = y0 + (y1 - y0) * u
                zb = hs.top(y, x) - embed * ch
                hoop = []
                for a_i in range(npts):
                    a = PI * a_i / (npts - 1)
                    hoop.append(Vector((x + cw * s * 1.02 * math.cos(a), y, zb + ch * s * 1.02 * math.sin(a))))
                self._hoop(hoop, Vector((x, y, zb)), w, cw * 0.05)
        return tops

    def _hoop(self, pts, centre, w, th):
        """Thin frame bar following a polyline (U across a canopy); outward = away from centre."""
        k = self.k
        if k.skip(th * 4):
            return
        rings = []
        for p in pts:
            n = Vector((p.x - centre.x, 0.0, p.z - centre.z))
            if n.length < 1e-6:
                continue
            n.normalize()
            rings.append([p + Vector((0, -w, 0)) - n * th * 0.3, p + Vector((0, w, 0)) - n * th * 0.3,
                          p + Vector((0, w, 0)) + n * th, p + Vector((0, -w, 0)) + n * th])
        if len(rings) >= 2:
            k.loft(rings, "graphite")

    def block(self, hs, y0, y1, w, h, x=0.0, front=0.38, prof="industrial", mat="armor", window=True,
              embed=0.15, rear_low=0.12, panels=False):
        """Superstructure on the hull top: loft with a sloped glacis and an optional window band."""
        L = y1 - y0
        P = sl.PROFILES[prof] if isinstance(prof, str) else prof
        ztop = max(hs.top(y0, x), hs.top(y1, x))
        zb = ztop - embed * h
        rows = [(y0, w * 0.9, h * 0.5 * (1 - rear_low)), (y0 + 0.12 * L, w, h * 0.5),
                (y1 - front * L, w, h * 0.5), (y1 - front * L * 0.45, w * 0.94, h * 0.5 * 0.8),
                (y1, w * 0.8, h * 0.5 * 0.5)]
        rings = [[(x + px * hw, y, zb + hh + pz * hh) for px, pz in P] for y, hw, hh in rows]

        def fm(i, j, c, n):
            if window and i == 2 and n.y > 0.2 and n.z > -0.3:
                return "glass"
            return None
        ins = None
        if panels:
            ins = {"thickness": max(0.01, 0.025 * min(w, h)), "depth": 0.01 * h,
                   "select": lambda i, j, c, n: "raise" if i == 1 and abs(n.x) > 0.7 else None}
        self.k.loft(rings, mat, cap0="fan", cap1="fan", face_mat=fm, inset=ins, bevel=0.0)
        return zb + h  # roof height

    def slab(self, c, size, mat="armor", end_taper=0.9, panels=True, face_mat=None, top_taper=1.0):
        """Flat box built as a loft along Y (box profile) so it gets panel insets. c centre, size (sx, sy, sz).
        top_taper < 1 narrows the upper face (sloped sides)."""
        cx, cy, cz = c
        sx, sy, sz = size
        P = sl.PROFILES["box"]
        ys = [(-0.5, end_taper), (-0.44, 1.0), (0.44, 1.0), (0.5, end_taper)]
        rings = [[(cx + px * sx / 2 * f * (top_taper if pz > 0 else 1.0), cy + yy * sy, cz + pz * sz / 2 * f)
                  for px, pz in P] for yy, f in ys]
        th = max(0.01, 0.07 * min(sz, sx))
        ins = None
        if panels:
            ins = {"thickness": th, "depth": th * 0.4,
                   "select": lambda i, j, cc, n: "raise" if (i == 1 and abs(n.z) > 0.8) else None}
        self.k.loft(rings, mat, face_mat=face_mat, inset=ins)
        return cz + sz / 2

    def beam(self, p0, p1, w, h, mat="graphite", up=(0, 0, 1), detail=None):
        """Box beam from p0 to p1 (local Z along the beam, local Y toward `up`)."""
        p0, p1 = Vector(p0), Vector(p1)
        dv = p1 - p0
        if dv.length < 1e-6:
            return
        R = sl.frame(dv, up)
        self.k.box((p0 + p1) / 2, (w, h, dv.length), mat, rot=R, bevel=min(w, h) * 0.1, detail=detail)

    def hammer(self, y_c, depth, span, h, zc, mat="armor", glass=True, tip_taper=0.55):
        """Hammerhead block across the bow: loft along X through wedge profiles (y, z) with a sloped glacis
        and a window band on the centre front. Returns (top_z(xu), front_y) helpers for sockets."""
        k = self.k
        prof = [(-0.5, -0.5), (0.5, -0.3), (0.5, -0.05), (0.4, 0.12), (0.15, 0.5), (-0.5, 0.5)]
        xs = [-1.0, -0.88, -0.5, -0.18, 0.18, 0.5, 0.88, 1.0]

        def f_at(xu):
            return 1.0 - (1.0 - tip_taper) * max(0.0, (abs(xu) - 0.5) / 0.5)
        rings = [[(xu * span, y_c + py * depth * f_at(xu), zc + pz * h * f_at(xu)) for py, pz in prof]
                 for xu in xs]

        def fm(i, j, c, n):
            if glass and j == 2 and abs(c.x) < span * 0.3:
                return "glass"
            if j == 0:
                return "graphite"
            if j == 2 and abs(c.x) > span * 0.6:
                return "marking"
            return None
        th = max(0.01, 0.04 * h)
        k.loft(rings, mat, cap0="fan", cap1="fan", face_mat=fm,
               inset={"thickness": th, "depth": th * 0.4,
                      "select": lambda i, j, c, n: "raise" if (j == 4 and 0 < i < len(xs) - 2) else None})
        return (lambda xu: zc + 0.5 * h * f_at(xu)), f_at

    def prongs(self, y_fork, y_tip, gap, pw, ph, zc=0.0, mat="armor", droop=0.0):
        """Forked prow: two mirrored tapered prongs from y_fork to y_tip whose inner faces stay gap/2 off
        the centre line. Returns the +X prong centre at fraction u (0 fork .. 1 tip) and its top z."""
        k = self.k
        prof = self.prof
        n = 8

        def sec(u):
            f = 1.0 - 0.8 * u ** 1.5
            hw = pw * (0.42 + 0.58 * f)
            hh = ph * f
            return gap / 2 + hw, hw, hh, zc - droop * u * u
        rings = []
        for q in range(n):
            u = q / (n - 1.0)
            xc, hw, hh, z = sec(u)
            rings.append([(xc + px * hw, y_fork + (y_tip - y_fork) * u, z + pz * hh) for px, pz in prof])
        st = self.style
        k.mirrored(k.loft, rings, mat, cap0="fan", cap1="fan",
                   face_mat=lambda i, j, c, nn: ("graphite" if nn.z < -0.55 else
                                                 ("marking" if (i == n - 3 and abs(nn.x) > 0.5 and st != "alien")
                                                  else None)),
                   inset=self.hull_panels(y_fork, y_tip, pw * 2, ph * 2))

        def at(u):
            xc, hw, hh, z = sec(u)
            return Vector((xc, y_fork + (y_tip - y_fork) * u, z + hh))
        return at

    def nacelle_pair(self, x, z, r, y0, y1, mat=None, prof=None, pylon_root=None, nozzle_r=None, place="main",
                     intake=True):
        """Mirrored engine nacelles lofted along Y (intake at y1, nozzle at y0) with an optional swept pylon
        from pylon_root (x, z) on the hull to the nacelle. Registers one thruster per side."""
        k = self.k
        st = self.style
        mat = mat or ("graphite" if st == "military" else "armor")
        Pn = sl.PROFILES[prof or ("sleek" if st in ("sleek", "alien") else "industrial")]
        Ln = y1 - y0
        rows = [(y1, 0.6), (y1 - 0.08 * Ln, 0.93), (y1 - 0.2 * Ln, 1.0), (y1 - 0.27 * Ln, 1.0), (y0 + 0.12 * Ln, 1.0),
                (y0, 0.94)]
        rings = [[(x + px * r * 1.2 * f, y, z + pz * r * 1.15 * f) for px, pz in Pn] for y, f in rows]
        stripe = "marking" if st != "alien" else "status"
        k.mirrored(k.loft, rings, mat, face_mat=lambda i, j, c, n: stripe if (i == 2 and n.z > 0.5) else None,
                   inset={"thickness": r * 0.06, "depth": r * 0.03,
                          "select": lambda i, j, c, n: "raise" if i == 2 and abs(n.x) > 0.6 else None})
        if intake:
            k.mirrored(k.cyl, (x, y1 + 0.01 * r, z), r * 0.5, r * 0.08, (0, 1, 0), "panel", segs=16,
                       detail=r * 0.5)
        if pylon_root is not None:
            xi, zi = pylon_root
            xb = x - r * 0.9
            dx, dz = xb - xi, z - zi
            ln = math.hypot(dx, dz)
            if ln > r * 0.2:
                rot = Matrix.Rotation(-math.atan2(dz, dx), 3, "Y")
                k.mirrored(k.box, ((xi + xb) / 2, y0 + 0.5 * Ln, (zi + z) / 2), (ln, 0.42 * Ln, r * 0.34),
                           "graphite", rot=rot, taper=(1.0, 0.72), shift_top=(0.0, -0.06 * Ln), bevel=r * 0.04)
        return self.nozzle(x, z, nozzle_r or r * 0.88, y0, mirror=True, housing=False, place=place)

    # ---------------------------------------------------------------- engines
    def nozzle(self, x, z, r, y_aft, length=None, housing=True, style=None, name=None, place="main",
               mirror=False, size_override=None):
        """Nozzle bell (lathe, glow interior in AF_EmissiveDrive) with its exit plane at y_aft - length.
        Registers a thruster socket (frame per params.thruster_frame)."""
        k = self.k
        st = style or self.style
        length = length or r * (1.15 if st != "sleek" else 0.8)
        re = r * {"industrial": 1.18, "military": 1.22, "sleek": 1.08, "alien": 0.92}[st]
        M = Matrix.Translation(Vector((x, y_aft, z))) @ sl.track((0, -1, 0)).to_4x4()
        prof = [(0.0, -0.08 * length), (r * 1.12, -0.08 * length), (r * 1.12, 0.12 * length),
                (r * 1.0, 0.2 * length), (re, length), (re * 0.9, length), (r * 0.82, 0.3 * length),
                (0.0, 0.3 * length)]
        mats = ["graphite", "trim", "trim", "graphite", "trim", "drive", "drive", "graphite"]
        build = (lambda: k.lathe(prof, M, mats, segs=28))
        if mirror:
            k.mirrored(build)
        else:
            build()
        if housing:
            hl = r * 1.6
            cyl = (lambda: k.cyl((x, y_aft + hl / 2 - 0.05 * r, z), r * 1.24, hl, axis=(0, 1, 0), mat="graphite",
                                 segs=24, bevel=r * 0.05))
            k.mirrored(cyl) if mirror else cyl()
            if st in ("industrial", "military"):
                ring = (lambda: k.cyl((x, y_aft + 0.12 * r, z), r * 1.32, r * 0.18, axis=(0, 1, 0), mat="trim",
                                      segs=24, detail=r * 0.3))
                k.mirrored(ring) if mirror else ring()
        diam = 2 * re
        sc = size_override or sl.size_class_for(diam)
        for sx in ((1, -1) if mirror else (1,)):
            self.engines.append(dict(x=x * sx, z=z, r=r, y_exit=y_aft - length, size=sc, place=place, d=diam))
        return y_aft - length

    def tail_cone(self, y_aft, rx, rz, z=0.0, length=None):
        """Closed tapered tail (no engine) — avoids a blunt open-looking cut where no nozzle sits."""
        length = length or max(rx, rz) * 1.2
        M = Matrix.Translation(Vector((0.0, y_aft, z))) @ sl.track((0, -1, 0)).to_4x4() @ \
            Matrix.Diagonal((1.0, rz / max(rx, 1e-6), 1.0, 1.0))
        prof = [(0.0, -0.02 * length), (rx * 0.98, -0.02 * length), (rx * 0.8, 0.45 * length),
                (rx * 0.35, 0.9 * length), (0.0, length)]
        self.k.lathe(prof, M, ["graphite", "trim", "graphite", "graphite"], segs=20)

    def name_thrusters(self):
        groups = {}
        for e in self.engines:
            groups.setdefault(e["place"], []).append(e)

        def names(group, base):
            n = len(group)
            g = sorted(group, key=lambda e: (round(-e["z"], 3), e["x"]))
            if n == 1:
                return [(g[0], base)]
            if n == 2 and abs(g[0]["z"] - g[1]["z"]) < 1e-3:
                g = sorted(g, key=lambda e: e["x"])
                return [(g[0], base + "_port"), (g[1], base + "_starboard")]
            if n == 3 and max(abs(e["z"] - g[0]["z"]) for e in g) < 1e-3:
                g = sorted(g, key=lambda e: e["x"])
                return [(g[0], base + "_port"), (g[1], base + "_center"), (g[2], base + "_starboard")]
            return [(e, "%s_%02d" % (base, i + 1)) for i, e in enumerate(g)]
        fr = self.P.get("thruster_frame", "thrust")
        fz, fy = sl.THRUSTER_FRAMES[fr]
        for place in sorted(groups, key=lambda p: (p != "main", p)):
            for e, nm in names(groups[place], "socket_thruster_" + place):
                # contract 6: mount_d_m = nozzle exit diameter (m), FX presets scale by it
                self.sock(nm, "thruster", (e["x"], e["y_exit"], e["z"]), z=fz, y=fy,
                          priority=1 if place == "main" else 3, size_class=e["size"], fx="drive", frame=fr,
                          mount_d_m=round(float(e["d"]), 3))

    def engine_layout(self, n, cx, cz, half_w, half_h, r_max):
        """Positions (x, z, r) of n nozzles inside a (half_w, half_h) aft face."""
        n = max(1, int(n))
        if n == 1:
            return [(cx, cz, min(r_max, 0.78 * min(half_w, half_h)))]
        if n == 2:
            r = min(r_max, half_w * 0.46, half_h * 0.8)
            return [(cx - half_w * 0.5, cz, r), (cx + half_w * 0.5, cz, r)]
        if n == 3:
            if half_w > half_h * 1.6:
                r = min(r_max, half_w * 0.3, half_h * 0.8)
                return [(cx - half_w * 0.64, cz, r), (cx, cz, r), (cx + half_w * 0.64, cz, r)]
            r = min(r_max, half_w * 0.42, half_h * 0.45)
            return [(cx - half_w * 0.48, cz - half_h * 0.36, r), (cx + half_w * 0.48, cz - half_h * 0.36, r),
                    (cx, cz + half_h * 0.42, r)]
        if n == 4:
            if half_w > half_h * 2.2:
                r = min(r_max, half_w * 0.22, half_h * 0.8)
                return [(cx + half_w * f, cz, r) for f in (-0.72, -0.24, 0.24, 0.72)]
            r = min(r_max, half_w * 0.42, half_h * 0.42)
            return [(cx + sx * half_w * 0.48, cz + sz * half_h * 0.46, r) for sz in (1, -1) for sx in (-1, 1)]
        if n == 5:
            r = min(r_max, half_w * 0.3, half_h * 0.4)
            pts = [(cx + sx * half_w * 0.62, cz + sz * half_h * 0.45, r) for sz in (1, -1) for sx in (-1, 1)]
            return pts + [(cx, cz, r * 1.15)]
        r = min(r_max, half_w * 0.28, half_h * 0.42)
        return [(cx + f * half_w, cz + sz * half_h * 0.46, r) for sz in (1, -1) for f in (-0.66, 0.0, 0.66)]

    def tail_engines(self, n, y_aft, half_w, half_h, r_max, cz=0.0):
        """n nozzles on a hull's aft face, or a closed tail cone when n == 0."""
        if n <= 0:
            self.tail_cone(y_aft, half_w * 0.9, half_h * 0.9, z=cz)
            return
        for x, z, r in self.engine_layout(n, 0.0, cz, half_w, half_h, r_max):
            self.nozzle(x, z, r, y_aft)

    # ---------------------------------------------------------------- wings / fins
    def wing_pair(self, x_root, y_le, z, span, c_root, c_tip, sweep, dihedral, t_root, t_tip, mat="armor",
                  stripe=True, curve=0.0, sections=None, plates=True, detail=None, stripe_mat="marking"):
        k = self.k
        d = math.radians(dihedral)
        span_vec = Vector((math.cos(d), 0.0, math.sin(d))) * span
        thick_dir = Vector((-math.sin(d), 0.0, math.cos(d)))
        S = sections or [0.0, 0.3, 0.62, 0.79, 0.86, 1.0]
        secs = []
        for s in S:
            chord = c_root + (c_tip - c_root) * s
            le = sweep * s + curve * math.sin(PI * s)
            secs.append((s, le, chord, t_root + (t_tip - t_root) * s, 0.0))
        style = self.style

        def fm(i, j, c, n):
            if stripe and 0.78 <= S[i] < 0.85:
                return stripe_mat
            if style in ("military", "sleek") and n.z < -0.6:
                return "graphite"
            return None
        th = max(0.01, 0.12 * t_root)
        ins = {"thickness": th, "depth": th * 0.4,
               "select": (lambda i, j, c, n: "raise" if (abs(n.z) > 0.75 and 0.0 < S[i] < 0.7) else None)} if plates else None
        k.mirrored(k.airfoil, (x_root, y_le, z), span_vec, (0, -1, 0), thick_dir, secs, mat, face_mat=fm,
                   inset=ins, bevel=0.0, detail=detail)

        def point(s, u, top=True):
            """Point on the +X wing: s span fraction, u chord fraction (0 LE .. 1 TE)."""
            chord = c_root + (c_tip - c_root) * s
            le = sweep * s + curve * math.sin(PI * s)
            th_ = (t_root + (t_tip - t_root) * s) * 0.45 * (1 if top else -1)
            return Vector((x_root, y_le, z)) + span_vec * s + Vector((0, -1, 0)) * (le + chord * u) + thick_dir * th_
        return point, thick_dir

    def fin(self, x, y_le, z, height, c_root, c_tip, sweep, cant, t_root, mirror=False, mat="armor", stripe=True):
        k = self.k
        c = math.radians(cant)
        span_vec = Vector((math.sin(c), 0.0, math.cos(c))) * height
        thick_dir = Vector((math.cos(c), 0.0, -math.sin(c)))
        S = [0.0, 0.5, 0.82, 0.9, 1.0]
        secs = [(s, sweep * s, c_root + (c_tip - c_root) * s, t_root * (1 - 0.6 * s), 0.0) for s in S]

        def fm(i, j, cc, n):
            return "marking" if (stripe and 0.81 <= S[i] < 0.89) else None
        if mirror:
            k.mirrored(k.airfoil, (x, y_le, z), span_vec, (0, -1, 0), thick_dir, secs, mat, face_mat=fm)
        else:
            k.airfoil((x, y_le, z), span_vec, (0, -1, 0), thick_dir, secs, mat, face_mat=fm)
        tip = Vector((x, y_le, z)) + span_vec + Vector((0, -1, 0)) * (sweep + c_tip * 0.5)
        return tip

    # ---------------------------------------------------------------- hardpoints / turrets
    def mount_d(self, size):
        """Mount ring diameter (m) within the size class (S <= 1.5, M <= 3, L <= 6)."""
        base = sl.TURRET_RING[size]
        if self.role in ("fighter", "interceptor"):
            return base * 0.75
        if self.P["budget_class"] == "ship_l":
            return base * 1.25
        return base

    def hardpoint(self, tag, pos, normal, size, priority, mirror_tag=None):
        self.hard.append(dict(tag=tag, pos=Vector(pos), n=Vector(normal).normalized(), size=size,
                              priority=priority, mirror_tag=mirror_tag))

    def hardpoint_pair(self, tag, pos, normal, size, priority):
        """Starboard (+X) / port (-X) pair: tag 'forward' -> starboard_forward + port_forward."""
        p = Vector((abs(pos[0]), pos[1], pos[2]))
        nrm = Vector(normal)
        self.hardpoint("starboard_" + tag, p, nrm, size, priority, mirror_tag="port_" + tag)
        self.hardpoint("port_" + tag, (-p.x, p.y, p.z), (-nrm.x, nrm.y, nrm.z), size, priority,
                       mirror_tag="starboard_" + tag)

    def place_turrets(self, count):
        """Pick `count` hardpoints by priority (mirror pairs stay together), build rings, add sockets."""
        k = self.k
        chosen, left = [], int(count)
        by_tag = {h["tag"]: h for h in self.hard}
        for h in sorted(self.hard, key=lambda h: (h["priority"], h["tag"])):
            if left <= 0:
                break
            if h in chosen:
                continue
            pair = by_tag.get(h["mirror_tag"]) if h["mirror_tag"] else None
            if pair is not None and pair not in chosen:
                if left >= 2:
                    chosen += [h, pair]
                    left -= 2
                continue
            chosen.append(h)
            left -= 1
        if left == 1:
            # one slot left but only pairs remain: swap the lowest-priority single for a pair
            singles = [h for h in chosen if not h["mirror_tag"]]
            pairs = [h for h in sorted(self.hard, key=lambda h: (h["priority"], h["tag"]))
                     if h["mirror_tag"] and h not in chosen and by_tag.get(h["mirror_tag"]) not in chosen
                     and by_tag.get(h["mirror_tag"]) is not None]
            if singles and pairs:
                chosen.remove(max(singles, key=lambda h: (h["priority"], h["tag"])))
                chosen += [pairs[0], by_tag[pairs[0]["mirror_tag"]]]
                left = 0
        for h in chosen:
            D = self.mount_d(h["size"])
            n = h["n"]
            hgt = 0.08 * D
            k.cyl(h["pos"] + n * (hgt / 2 - 0.03 * D), D * 0.56, hgt, axis=n, mat="graphite", segs=28,
                  bevel=0.012 * D)
            k.cyl(h["pos"] + n * (hgt + 0.005 * D), D * 0.47, 0.03 * D, axis=n, mat="trim", segs=28)
            yh = Vector((0, 1, 0)) if abs(n.y) < 0.9 else Vector((0, 0, 1))
            self.sock("socket_turret_" + h["tag"], "turret", h["pos"] + n * (hgt + 0.02 * D), z=n, y=yh,
                      priority=0, size_class=h["size"], mount_d_m=round(D, 3))
        return len(chosen)

    # ---------------------------------------------------------------- greebles
    def rcs(self, pos, normal, s):
        """Reaction-control block: small housing with three tiny nozzles."""
        k = self.k
        n = Vector(normal).normalized()
        R = sl.frame(n)
        k.box(Vector(pos) + n * s * 0.25, (s * 1.2, s * 1.6, s * 0.5), "graphite", rot=R, bevel=s * 0.08, detail=s)
        for dy in (-0.45, 0.0, 0.45):
            p = Vector(pos) + n * s * 0.55 + R @ Vector((0, dy * s, 0))
            k.cyl(p, s * 0.16, s * 0.25, axis=n, mat="trim", segs=8, detail=s * 0.5, r2=s * 0.2)

    def vents(self, c, along, out, length, width, n=6):
        """Louvre row on a flat surface (c centre, along = row direction, out = surface normal)."""
        k = self.k
        along, out = Vector(along).normalized(), Vector(out).normalized()
        R = sl.frame(out, along)
        k.box(Vector(c) + out * width * 0.02, (width, length, width * 0.06), "panel", rot=R, detail=width * 0.6)
        for i in range(n):
            p = Vector(c) + along * (length * ((i + 0.5) / n - 0.5)) + out * width * 0.06
            k.box(p, (width * 0.9, length / n * 0.35, width * 0.08), "trim", rot=R, detail=width * 0.4)

    def antenna(self, base, h, r, lamp=True):
        k = self.k
        base = Vector(base)
        k.cyl(base + Vector((0, 0, r * 1.5)), r * 2.6, r * 3.0, mat="graphite", segs=10, detail=h * 0.3)
        k.cyl(base + Vector((0, 0, h / 2)), r, h, mat="trim", segs=8, r2=r * 0.5, detail=h * 0.25)
        top = base + Vector((0, 0, h))
        if lamp:
            k.box(top, (r * 2.4, r * 2.4, r * 2.4), "status", detail=h * 0.25)
        return top

    def dome(self, c, r, mat="trim"):
        self.k.sphere(c, r, mat, segs=16, hemi=True, detail=r)

    def radiator(self, c, along, out, length, depth, n, t):
        """Stack of thin radiator plates standing out of a surface."""
        k = self.k
        along, out = Vector(along).normalized(), Vector(out).normalized()
        side = along.cross(out)
        R = sl.frame(out, along)
        for i in range(n):
            p = Vector(c) + side * ((i - (n - 1) / 2.0) * depth * 0.28) + out * depth * 0.5
            k.box(p, (t, length, depth), "graphite", rot=R, bevel=t * 0.3, detail=depth * 0.5)
        k.box(Vector(c) + out * depth * 0.05, (depth * 0.28 * n, length * 1.04, depth * 0.1), "trim", rot=R,
              detail=depth * 0.5)

    def pipes(self, y0, y1, x, z, r, n=2, dz=None):
        k = self.k
        dz = dz if dz is not None else r * 2.6
        for i in range(n):
            k.mirrored(k.cyl, (x, (y0 + y1) / 2, z + i * dz), r, abs(y1 - y0), (0, 1, 0), "trim", segs=10,
                       detail=r * 2)

    def nav_lights(self, side_pt, top, s):
        """Nav light pair at the flank point side_pt (either sign of x is accepted) + dorsal beacon.
        Starboard (green) = +X with outward +X, port (red) = -X with outward -X (Blender; glTF x is the same)."""
        k = self.k
        p = Vector((abs(side_pt[0]), side_pt[1], side_pt[2]))
        k.mirrored(k.box, p, (s, s * 1.6, s * 0.8), "status", detail=s * 0.5)
        k.box(Vector(top), (s * 0.9, s * 0.9, s * 0.9), "status", detail=s * 0.5)
        self.sock("socket_fx_nav_starboard", "fx", p, z=(1, 0, 0), priority=3, fx="nav_light",
                  side="starboard", color="green")
        self.sock("socket_fx_nav_port", "fx", (-p.x, p.y, p.z), z=(-1, 0, 0), priority=3, fx="nav_light",
                  side="port", color="red")
        self.sock("socket_fx_beacon_dorsal", "fx", top, z=(0, 0, 1), priority=4, fx="beacon")

    def hatch(self, c, out, w, h, along=(0, 1, 0)):
        """Side hatch: steel frame with a raised armour door (no dark recess — that reads as a hole)."""
        k = self.k
        out = Vector(out).normalized()
        R = sl.frame(out, along)
        k.box(Vector(c), (w, h, w * 0.08), "trim", rot=R, bevel=w * 0.03, detail=w * 0.5)
        k.box(Vector(c) + out * w * 0.04, (w * 0.82, h * 0.84, w * 0.06), "armor", rot=R, bevel=w * 0.02,
              detail=w * 0.5)
        k.box(Vector(c) + out * w * 0.075, (w * 0.12, h * 0.5, w * 0.02), "panel", rot=R, detail=w * 0.5)

    def container(self, c, size, mat, ribs=3):
        k = self.k
        sx, sy, sz = size
        k.box(c, size, mat, bevel=min(size) * 0.04)
        for i in range(ribs):
            y = c[1] + sy * ((i + 0.5) / ribs - 0.5)
            k.box((c[0], y, c[2]), (sx * 1.04, sy * 0.05, sz * 1.04), "trim", detail=min(size) * 0.5)

    def capsule(self, c, r, length, axis, mat, band="marking"):
        k = self.k
        a = Vector(axis).normalized()
        M = Matrix.Translation(Vector(c)) @ sl.track(a).to_4x4()
        h = length / 2.0
        prof = [(0.0, -h - r * 0.55)]
        for q in range(1, 4):
            ang = -PI / 2 + q * (PI / 2) / 4
            prof.append((r * math.cos(ang), -h + r * 0.55 * math.sin(ang) * 1.0))
        prof += [(r, -h), (r, -h * 0.25), (r * 1.03, -h * 0.2), (r * 1.03, h * 0.05), (r, h * 0.1), (r, h)]
        for q in range(1, 4):
            ang = q * (PI / 2) / 4
            prof.append((r * math.cos(ang), h + r * 0.55 * math.sin(ang)))
        prof.append((0.0, h + r * 0.55))
        mats = [mat] * len(prof)
        i_band = 5
        mats[i_band] = band
        mats[i_band + 1] = band
        mats[i_band + 2] = band
        k.lathe(prof, M, mats, segs=20)

    def truss(self, y0, y1, x, z, w, n_bays):
        """Square spine beam with ribs (graphite / trim)."""
        k = self.k
        k.box((x, (y0 + y1) / 2, z), (w, y1 - y0, w), "graphite", bevel=w * 0.06)
        for i in range(n_bays + 1):
            y = y0 + (y1 - y0) * i / float(n_bays)
            k.box((x, y, z), (w * 1.3, w * 0.18, w * 1.3), "trim", bevel=w * 0.03, detail=w * 0.5)

    def tower(self, hs, y0, y1, W, H, tiers=2, x=0.0):
        """Stepped command tower on a hull: base block + upper block + bridge with windows. Returns
        (bridge roof z, y of the bridge centre)."""
        k = self.k
        ln = y1 - y0
        z0 = self.block(hs, y0, y1, W, H, x=x, front=0.4, prof=self.prof, window=False, panels=True)

        class _Roof:
            def __init__(self, z):
                self.z = z

            def top(self, y, x_=0.0):
                return self.z
        z = z0
        ya, yb = y0, y1
        if tiers >= 2:
            ya, yb = y0 + 0.2 * ln, y1 - 0.12 * ln
            z = self.block(_Roof(z0 - 0.01 * H), ya, yb, W * 0.7, H * 0.85, x=x, front=0.45, prof=self.prof,
                           window=False)
        bya, byb = ya + 0.3 * (yb - ya), yb
        br = self.block(_Roof(z - 0.02 * H), bya, byb, W * 0.95, H * 0.42, x=x, front=0.5, prof=self.prof,
                        window=True)
        k.mirrored(k.box, (x + W * 0.66, yb - 0.25 * (yb - ya), z0 - H * 0.35), (W * 0.06, ln * 0.3, H * 0.06),
                   "trim", detail=0.05 * H)
        return br, (bya + byb) / 2


# ============================================================================= roles
def wing_style_for(d, choices=None):
    ws = d.P["wing_style"]
    if ws != "auto":
        d.rng.random()
        return ws
    choices = choices or {"military": ["swept", "delta", "x", "swept"], "industrial": ["straight", "stub", "straight"],
                          "sleek": ["delta", "swept", "forward"], "alien": ["crescent", "forward", "crescent"]}[d.style]
    return d.rng.choice(choices)


# ----------------------------------------------------------------------------- fighters
def design_fighter(d, interceptor=False):
    L, k, st = d.L, d.k, d.style
    U = d.U
    fam = d.family(FAMILIES["interceptor" if interceptor else "fighter"])
    prof = sl.PROFILES[{"military": "chined", "industrial": "box", "sleek": "sleek", "alien": "alien"}[st]]

    def G(t):  # global stations: 0 = tail, 1 = nose tip
        return -0.46 * L + 0.96 * L * t
    if fam == "twin_boom":
        W, H = L * U(0.068, 0.08), L * U(0.058, 0.068)
        nose = U(0.3, 0.38)
        y0, y1 = G(0.34), G(1.0)
    elif fam == "dart":
        W, H = L * U(0.05, 0.06), L * U(0.04, 0.046)
        nose = U(0.44, 0.52)
        y0, y1 = G(0.0), G(1.0)
    elif interceptor:
        W, H, nose = L * U(0.05, 0.06), L * U(0.042, 0.05), U(0.4, 0.48)
        y0, y1 = G(0.0), G(1.0)
    else:
        W, H, nose = L * U(0.075, 0.09), L * U(0.05, 0.062), U(0.28, 0.36)
        y0, y1 = G(0.0), G(1.0)
    droop = U(0.08, 0.22)
    tail_hw = 0.7 if fam == "twin_boom" else 0.8
    ctrl = [(0.0, tail_hw, 0.82, 0.02), (0.12, 1.0, 1.0, 0.0), (0.42, 1.0, 1.0, 0.0),
            (1 - nose, 0.86, 0.9, -0.02), (1 - nose * 0.5, 0.52, 0.6, -droop * 0.5),
            (0.97, 0.16, 0.24, -droop), (1.0, 0.04, 0.08, -droop * 1.05)]
    hs = d.hull(ctrl, W, H, y0, y1, prof=prof)
    yc = lambda t: y0 + (y1 - y0) * t  # noqa: E731
    # cockpit canopy
    c_t0 = 1 - nose - (0.1 if (interceptor or fam == "dart") else 0.05)
    c_t1 = c_t0 + {"arrow": 0.22 if interceptor else 0.27, "twin_boom": 0.32, "dart": 0.2}[fam]
    cw = W * U(0.48, 0.6)
    ch = H * U(0.7, 0.85)
    d.canopy(hs, yc(c_t0), yc(c_t1), cw, ch)
    # dorsal spine behind the canopy
    sy0, sy1 = yc(0.08), yc(c_t0 + 0.02)
    sw, sh = W * 0.24, H * 0.32
    rings = []
    for q in range(6):
        u = q / 5.0
        y = sy0 + (sy1 - sy0) * u
        zt = hs.top(y) - sh * 0.3
        f = 0.55 + 0.45 * math.sin(PI * min(1.0, u * 1.25))
        rings.append([(sw * f * px, y, zt + sh * f * (pz * 0.5 + 0.5)) for px, pz in sl.PROFILES["chined"]])
    k.loft(rings, "graphite")
    # side intakes (chined boxes); the boom fighter breathes through its booms
    if fam != "twin_boom" and (st in ("military", "industrial") or d.chance(0.35)):
        iy0, iy1 = yc(0.2), yc(0.5 if not interceptor else 0.44)
        iz = -0.15 * H
        ix = hs.side((iy0 + iy1) / 2, iz)
        iw, ih = W * 0.42, H * 0.8
        k.mirrored(k.box, (ix + iw * 0.22, (iy0 + iy1) / 2, iz), (iw, iy1 - iy0, ih), "armor",
                   taper=(0.7, 0.9), shift_top=(-iw * 0.12, -0.01 * L), bevel=iw * 0.05,
                   face_mats=["graphite", "armor", "armor", "armor", "armor", "armor"])
        k.mirrored(k.box, (ix + iw * 0.24, iy1 + 0.002 * L, iz - ih * 0.02), (iw * 0.7, 0.012 * L, ih * 0.7), "panel")
    else:
        d.rng.random()
    # wings
    ws = wing_style_for(d)
    wz = -0.3 * H
    wx = W * 0.6
    curve = 0.0
    if fam == "dart":
        span = L * U(0.19, 0.24) * (0.9 if interceptor else 1.0)
        cr, ct = L * U(0.36, 0.44), L * U(0.03, 0.045)
        sweep = cr * U(0.72, 0.88)
        dih = U(-3, 4)
        wy = G(U(0.4, 0.47))
        if ws not in ("delta", "crescent", "none"):
            if d.P["wing_style"] != "auto":
                d.notes.append("dart layout keeps its aft delta; wing_style %s ignored" % ws)
            ws = "delta"
    elif fam == "twin_boom":
        span = L * U(0.3, 0.37)
        cr = L * U(0.19, 0.24)
        ct, sweep = cr * U(0.55, 0.72), cr * U(0.08, 0.3)
        dih = U(0, 5)
        wy = G(U(0.56, 0.62))
        if ws not in ("straight", "crescent"):
            if d.P["wing_style"] != "auto":
                d.notes.append("twin_boom layout needs a straight wing to carry the booms; wing_style %s ignored" % ws)
            ws = "straight"
    else:
        if interceptor:
            span, cr, ct, sweep, le_t = L * U(0.2, 0.26), L * U(0.24, 0.3), L * U(0.04, 0.06), L * U(0.16, 0.22), U(0.34, 0.4)
        else:
            span, cr, ct, sweep, le_t = L * U(0.27, 0.36), L * U(0.34, 0.44), L * U(0.07, 0.12), L * U(0.12, 0.22), U(0.56, 0.64)
        dih = U(-4, 6)
        if ws == "delta":
            ct, sweep, le_t = L * 0.04, cr * U(0.8, 0.92), le_t + 0.04
        elif ws == "forward":
            sweep = -sweep * 0.5
            le_t -= 0.1
        elif ws == "straight":
            sweep, ct = cr * 0.12, cr * U(0.6, 0.72)
            span *= 0.85
        elif ws == "stub":
            span *= 0.55
            ct = cr * 0.7
        elif ws == "crescent":
            sweep, curve = sweep * 1.25, -L * 0.07
            ct = L * 0.03
        elif ws == "x":
            dih = U(13, 18)
            span *= 0.85
        wy = yc(le_t)
    d.layout["wing"] = ws
    tr, tt = H * 0.36, H * 0.14
    if st == "alien":
        curve = -L * U(0.06, 0.1) if ws != "forward" else L * U(0.04, 0.07)
        dih = U(-14, -6)
        ct = min(ct, L * 0.035)
    top = None
    if ws != "none":
        wp, wdir = d.wing_pair(wx, wy, wz, span, cr, ct, sweep, dih, tr, tt, curve=curve,
                               stripe_mat="status" if st == "alien" else "marking")
        if ws == "x":
            d.wing_pair(wx, wy, wz + H * 0.5, span, cr, ct, sweep, -dih, tr, tt, stripe=False)
        tip = wp(1.0, 0.5)
        nav_pt = tip + Vector((0.004 * L, 0, 0))
        if fam == "twin_boom":
            sb = U(0.36, 0.46)
            xb = wx + span * sb * math.cos(math.radians(dih))
            zb = wz + span * sb * math.sin(math.radians(dih))
            rb = H * U(0.26, 0.32)
            by0, by1 = G(0.015), wy + L * U(0.06, 0.1)
            rows = [(0.0, 0.85), (0.06, 1.0), (0.48, 1.0), (0.54, 1.0), (0.82, 1.0), (0.93, 0.7), (1.0, 0.22)]
            Pb = sl.PROFILES["sleek"]
            rings = [[(xb + px * rb * f, by0 + (by1 - by0) * t, zb + pz * rb * f) for px, pz in Pb] for t, f in rows]
            k.mirrored(k.loft, rings, "armor",
                       face_mat=lambda i, j, c, n: ("graphite" if n.z < -0.5 else
                                                    ("marking" if (i == 2 and n.z > 0.5 and st != "alien") else None)))
            # horizontal stabiliser between the booms + twin fins on the booms
            cs, ts = L * U(0.08, 0.1), H * 0.12
            sz_ = zb + rb * 0.45
            k.airfoil((-xb, G(0.12), sz_), (2 * xb, 0.0, 0.0), (0, -1, 0), (0, 0, 1),
                      [(0.0, 0.0, cs, ts, 0.0), (0.5, 0.0, cs, ts, 0.0), (1.0, 0.0, cs, ts, 0.0)], "armor",
                      face_mat=lambda i, j, c, n: "graphite" if n.z < -0.6 else None)
            d.fin(xb, G(0.16), zb + rb * 0.6, H * U(1.2, 1.6), L * 0.12, L * 0.05, L * 0.07, U(-12, -4), H * 0.16,
                  mirror=True)
            top = Vector((0.0, G(0.1), sz_ + ts * 0.6))
            d.layout["booms"] = round(sb, 2)
        # under-wing pylons = weapon hardpoints (socket points down, -Z)
        stations = ((0.55, "wing", 1), (0.28, "wing_root", 3))
        if fam == "twin_boom":
            stations = ((sb + (1 - sb) * 0.55, "wing", 1), (sb * 0.45, "wing_root", 3))
        elif fam == "dart":
            stations = ((0.6, "wing", 1), (0.3, "wing_root", 3))
        for s_, tag, pr in stations:
            p = wp(s_, 0.4, top=False)
            ph = H * 0.28
            k.mirrored(k.box, p + Vector((0, 0, -ph / 2)), (H * 0.12, cr * 0.32, ph), "graphite", taper=(1.0, 1.3),
                       bevel=H * 0.02)
            d.hardpoint_pair(tag, p + Vector((0, 0, -ph)), (0, 0, -1), "S", pr)
    else:
        nav_pt = Vector((W * 1.0, yc(0.3), 0.0))
        d.rng.random()
        # wingless: weapon pylons under the fuselage flanks
        for t_, tag, pr in ((0.5, "ventral", 1), (0.32, "ventral_aft", 3)):
            yy = yc(t_)
            xx = hs.side(yy, -0.3 * H) * 0.7
            p = Vector((xx, yy, hs.bottom(yy, xx)))
            ph = H * 0.22
            k.mirrored(k.box, p + Vector((0, 0, -ph / 2)), (H * 0.12, L * 0.08, ph), "graphite", taper=(1.0, 1.3),
                       bevel=H * 0.02)
            d.hardpoint_pair(tag, p + Vector((0, 0, -ph)), (0, 0, -1), "S", pr)
    # canards (arrow interceptors, every dart)
    if (interceptor and fam == "arrow") or fam == "dart":
        cy = yc(0.74)
        d.wing_pair(hs.side(cy, -0.1 * H) * 0.8, cy, -0.1 * H, L * 0.07, L * 0.08, L * 0.03, L * 0.05, 0.0,
                    H * 0.16, H * 0.07, stripe=False, plates=False)
    # nose guns
    if st != "alien":
        gy = yc(1 - nose * 0.62)
        gx = hs.side(gy, -0.35 * H) * 0.92
        k.mirrored(k.cyl, (gx, gy, -0.35 * H), H * 0.05, nose * (y1 - y0) * 0.3, (0, 1, 0), "trim", segs=8)
    if st == "alien":
        # dorsal spines: a row of swept-back blades along the spine
        for q in range(4):
            yy = yc(0.18 + q * 0.09)
            hh_ = H * (0.75 - q * 0.12)
            k.box((0, yy, hs.top(yy) + hh_ * 0.4), (max(H * 0.08, 0.012 * L), L * 0.05, hh_), "graphite",
                  taper=(0.4, 0.3), shift_top=(0.0, -L * 0.035))
    # tail fins
    if fam == "arrow":
        fins = {"military": "twin", "industrial": "single", "sleek": "single", "alien": "twin_swept"}[st]
        if interceptor:
            fins = "single"
        if d.chance(0.3):
            fins = "twin" if fins == "single" else "single"
        fy = yc(0.2)
        fz = hs.top(fy) - 0.1 * H
        if fins == "single":
            top = d.fin(0.0, fy, fz, H * U(1.3, 1.8), L * 0.18, L * 0.06, L * 0.13, 0.0, H * 0.22)
        else:
            cant = U(14, 26) if fins == "twin" else U(35, 50)
            d.fin(W * 0.5, fy, fz - 0.15 * H, H * U(1.1, 1.5), L * 0.16, L * 0.05, L * 0.12, cant, H * 0.2, mirror=True)
            top = Vector((0.0, yc(0.3), hs.top(yc(0.3)) + 0.02 * H))  # beacon on the spine, not between the fins
        d.layout["fins"] = fins
    elif fam == "dart":
        fy = G(0.06)
        top = d.fin(0.0, fy, hs.top(fy) - 0.1 * H, H * U(1.6, 2.1), L * 0.2, L * 0.06, L * 0.15, 0.0, H * 0.2)
        d.fin(0.0, G(0.08), hs.bottom(G(0.08)) + 0.1 * H, -H * U(0.8, 1.1), L * 0.14, L * 0.05, L * 0.09, 0.0,
              H * 0.16)
    if top is None:
        top = Vector((0.0, yc(0.3), hs.top(yc(0.3)) + 0.02 * H))
    # engines
    n_eng = d.P["engine_count"]
    if fam == "twin_boom":
        boom = 2 if n_eng >= 2 else 0
        if boom and ws != "none":
            d.nozzle(xb, zb, rb * 0.85, by0 + 0.01 * L, mirror=True, housing=False)
        else:
            boom = 0
        d.tail_engines(n_eng - boom, y0 + 0.02 * L, tail_hw * W * 0.95, 0.82 * H * 0.92, H * 0.7)
    else:
        tail_hw_, tail_hh = ctrl[0][1] * W, ctrl[0][2] * H
        for x, z, r in d.engine_layout(n_eng, 0.0, 0.0, tail_hw_ * 0.95, tail_hh * 0.92, H * 0.85):
            d.nozzle(x, z, r, y0 + 0.02 * L)
    # greebles
    if d.g > 0.2:
        for sx in (1, -1):
            d.rcs((sx * hs.side(yc(0.82), 0.0), yc(0.82), 0.0), (sx, 0, 0), H * 0.24)
    if d.g > 0.4:
        d.vents((0, yc(0.28), hs.top(yc(0.28)) + 0.002 * L), (0, 1, 0), (0, 0, 1), L * 0.1, W * 0.3, n=6)
    ant = d.antenna((W * 0.16, yc(0.38), hs.top(yc(0.38), W * 0.16) - 0.01 * L), H * 0.6, H * 0.035)
    d.sock("socket_utility_antenna", "utility", ant, priority=6)
    d.nav_lights(nav_pt, top, H * 0.12)
    d.sock("socket_docking_ventral", "docking", (0, yc(0.45), hs.bottom(yc(0.45))), z=(0, 0, -1), priority=5)
    return hs


def design_interceptor(d):
    return design_fighter(d, interceptor=True)


def armor_belt(d, hs, y0, y1, z_rel, depth, height, mat="armor", face=None):
    """Raised armour belt along both flanks of a hull (loft of thin plates hugging the side)."""
    k = d.k
    n = 6
    rings = []
    for q in range(n):
        u = q / (n - 1.0)
        y = y0 + (y1 - y0) * u
        hw, hh, zc = hs.at(y)
        z = zc + z_rel * hh
        x = hs.side(y, z)
        f = 0.35 + 0.65 * math.sin(PI * min(1.0, max(0.0, u * 1.15)))
        h = height * hh * (0.6 + 0.4 * f)
        dep = depth * f
        rings.append([(x - dep * 0.6, y, z - h / 2), (x + dep, y, z - h / 2 + h * 0.08),
                      (x + dep, y, z + h / 2 - h * 0.12), (x - dep * 0.6, y, z + h / 2)])
    fm = face or (lambda i, j, c, nn: None)
    k.mirrored(k.loft, rings, mat, face_mat=fm,
               inset={"thickness": depth * 0.25, "depth": depth * 0.12,
                      "select": lambda i, j, c, nn: "raise" if abs(nn.x) > 0.8 else None})


def launcher_pods(d, pod_c, pw, pl, ph):
    """Mirrored missile pods (six cells each) + launcher sockets."""
    k = d.k
    pod_c = Vector((abs(pod_c[0]), pod_c[1], pod_c[2]))
    k.mirrored(k.box, pod_c, (pw, pl, ph), "graphite", bevel=pw * 0.09, taper=(0.9, 0.95))
    for cc in range(3):
        for rz in (-1, 1):
            k.mirrored(k.cyl, pod_c + Vector(((cc - 1) * pw * 0.28, pl * 0.5, rz * ph * 0.25)), pw * 0.11, pw * 0.12,
                       (0, 1, 0), "panel", segs=10, detail=pw * 0.25)
    d.sock("socket_launcher_starboard", "launcher", pod_c, z=(0, 0, -1), priority=7, size_class="S")
    d.sock("socket_launcher_port", "launcher", (-pod_c.x, pod_c.y, pod_c.z), z=(0, 0, -1), priority=7,
           size_class="S")


# ----------------------------------------------------------------------------- gunship
def design_gunship(d):
    fam = d.family(FAMILIES["gunship"])
    return {"brick": gunship_brick, "hammer": gunship_hammer, "catamaran": gunship_catamaran}[fam](d)


def gunship_brick(d):
    L, k, st = d.L, d.k, d.style
    U = d.U
    W, H = L * U(0.15, 0.18), L * U(0.08, 0.095)
    ctrl = [(0.0, 0.9, 0.88, 0.0), (0.07, 1.0, 1.0, 0.0), (0.6, 1.0, 1.0, 0.0), (0.84, 0.8, 0.8, -0.06),
            (1.0, U(0.42, 0.55), 0.38, -0.22)]
    y0, y1 = -0.44 * L, 0.5 * L
    hs = d.hull(ctrl, W, H, y0, y1)
    yc = lambda t: y0 + (y1 - y0) * t  # noqa: E731
    # bridge forward-top
    by0, by1 = yc(U(0.58, 0.64)), yc(0.84)
    roof = d.block(hs, by0, by1, W * 0.42, H * 0.7, front=0.42, prof=d.prof)
    armor_belt(d, hs, yc(0.12), yc(0.74), -0.25, W * 0.06, 0.7)
    # flank batteries on the waist ledge
    for f, tag, pr in ((0.66, "forward", 0), (0.3, "aft", 1)):
        y = yc(f)
        x = hs.at(y)[0] * 0.84
        d.hardpoint_pair(tag, (x, y, hs.top(y, x)), (0, 0, 1), "M", pr)
    d.hardpoint("dorsal_aft", (0, yc(0.3), hs.top(yc(0.3))), (0, 0, 1), "M", 2)
    d.hardpoint("ventral_forward", (0, yc(0.6), hs.bottom(yc(0.6))), (0, 0, -1), "M", 3)
    d.hardpoint("dorsal_mid", (0, yc(0.48), hs.top(yc(0.48))), (0, 0, 1), "M", 4)
    # stub wings with launcher pods
    wy = yc(0.34)
    wx = hs.side(wy, -0.35 * H) * 0.9
    if d.P["wing_style"] != "none":
        wp, _wdir = d.wing_pair(wx, wy, -0.35 * H, W * U(0.45, 0.65), L * 0.17, L * 0.11, L * 0.04, U(-10, -2),
                                H * 0.24, H * 0.16, stripe=st != "alien")
        launcher_pods(d, wp(0.85, 0.5, top=False) + Vector((0, 0, -H * 0.15)), W * 0.16, L * 0.14, H * 0.24)
    else:
        d.U(0, 1), d.U(0, 1)
    # engines: two nacelles at the aft corners + optional centre nozzles
    n_eng = d.P["engine_count"]
    nac_r = H * 0.42
    nac_x = W * 0.86
    nac_z = -0.05 * H
    ny0, ny1 = y0 + 0.01 * L, yc(0.3)
    rows = [(ny1, 0.55), (ny1 - 0.18 * (ny1 - ny0), 1.0), (ny0 + 0.12 * (ny1 - ny0), 1.0), (ny0, 0.95)]
    Pn = sl.PROFILES["sleek" if st in ("sleek", "alien") else "industrial"]
    rings = [[(nac_x + px * nac_r * 1.2 * f, y, nac_z + pz * nac_r * 1.15 * f) for px, pz in Pn] for y, f in rows]
    k.mirrored(k.loft, rings, "graphite" if st == "military" else "armor",
               face_mat=lambda i, j, c, n: "marking" if (i == 1 and n.z > 0.5) else None,
               inset={"thickness": nac_r * 0.06, "depth": nac_r * 0.03,
                      "select": lambda i, j, c, n: "raise" if i == 1 and abs(n.x) > 0.6 else None})
    d.nozzle(nac_x, nac_z, nac_r * 0.88, ny0, mirror=True, housing=False)
    if n_eng >= 3:
        d.nozzle(0.0, 0.1 * H, H * 0.3, y0 + 0.01 * L)
    if n_eng >= 4:
        d.nozzle(0.0, -0.55 * H, H * 0.2, y0 + 0.01 * L)
    if n_eng >= 5:
        for x, z, r in d.engine_layout(n_eng - 4, 0.0, 0.62 * H, W * 0.4, H * 0.2, H * 0.16):
            d.nozzle(x, z, r, y0 + 0.01 * L)
    # greebles
    if d.g > 0.25:
        d.radiator((0, yc(0.14), hs.top(yc(0.14))), (0, 1, 0), (0, 0, 1), L * 0.1, H * 0.35, 4, W * 0.02)
    if d.g > 0.45:
        for f in (0.44, 0.52):
            d.vents((W * 0.3, yc(f), hs.top(yc(f), W * 0.3) + 0.002 * L), (0, 1, 0), (0, 0, 1), L * 0.06,
                    W * 0.16, n=5)
            d.vents((-W * 0.3, yc(f), hs.top(yc(f), -W * 0.3) + 0.002 * L), (0, 1, 0), (0, 0, 1), L * 0.06,
                    W * 0.16, n=5)
    for sx in (1, -1):
        d.rcs((sx * hs.side(yc(0.9), -0.1 * H), yc(0.9), -0.1 * H), (sx, 0, 0), H * 0.18)
    ant = d.antenna((W * 0.16, (by0 + by1) / 2 - 0.02 * L, roof - 0.004 * L), H * 0.55, H * 0.025)
    d.dome((-W * 0.14, by0 + 0.03 * L, roof - 0.004 * L), H * 0.12)
    d.sock("socket_sensor_dorsal", "sensor", (-W * 0.14, by0 + 0.03 * L, roof + H * 0.1), priority=6)
    d.sock("socket_utility_antenna", "utility", ant, priority=6)
    d.nav_lights((nac_x + nac_r * 1.2, yc(0.16), nac_z), ant + Vector((0, 0, 0.01 * L)), H * 0.08)
    d.sock("socket_docking_ventral", "docking", (0, yc(0.4), hs.bottom(yc(0.4))), z=(0, 0, -1), priority=5)
    if st == "alien":
        alien_claws(d, hs, yc(0.78), yc(0.1))
    return hs


def gunship_hammer(d):
    L, k, st = d.L, d.k, d.style
    U = d.U
    W, H = L * U(0.1, 0.12), L * U(0.085, 0.1)
    ctrl = [(0.0, 0.92, 0.95, 0.0), (0.08, 1.0, 1.0, 0.0), (0.72, 1.0, 0.96, 0.0), (0.9, 0.82, 0.8, -0.04),
            (1.0, 0.62, 0.55, -0.1)]
    y0, y1 = -0.44 * L, 0.4 * L
    hs = d.hull(ctrl, W, H, y0, y1)
    yc = lambda t: y0 + (y1 - y0) * t  # noqa: E731
    # hammerhead across the bow: bridge window band in the middle, weapon pods under the tips
    hy = yc(U(0.8, 0.88))
    span, depth, hh = L * U(0.22, 0.27), L * U(0.1, 0.13), H * U(0.75, 0.9)
    zc = hs.at(hy)[2] + 0.05 * H
    topz, f_at = d.hammer(hy, depth, span, hh, zc)
    launcher_pods(d, (span * 0.82, hy - depth * 0.05, zc - hh * 0.5 * f_at(0.82) - H * 0.12), W * 0.22,
                  depth * 1.1, H * 0.24)
    # dorsal deckhouse with radiators
    roof = d.block(hs, yc(0.3), yc(0.64), W * 0.5, H * 0.55, front=0.35, prof=d.prof, window=False, panels=True)
    if d.g > 0.25:
        d.radiator((0, yc(0.2), hs.top(yc(0.2))), (0, 1, 0), (0, 0, 1), L * 0.08, H * 0.3, 3, W * 0.03)
    # ventral fins aft
    d.fin(W * 0.6, yc(0.16), hs.bottom(yc(0.16), W * 0.6) + 0.05 * H, -H * U(0.6, 0.85), L * 0.12, L * 0.05,
          L * 0.07, U(20, 32), H * 0.14, mirror=True)
    # engines on the aft face
    d.tail_engines(d.P["engine_count"], y0 + 0.01 * L, W * 0.92, H * 0.88, H * 0.55)
    # hardpoints
    d.hardpoint_pair("forward", (span * 0.7, hy - depth * 0.25, topz(0.7)), (0, 0, 1), "M", 0)
    d.hardpoint("dorsal_aft", (0, yc(0.1), hs.top(yc(0.1))), (0, 0, 1), "M", 1)
    d.hardpoint("dorsal_mid", (0, yc(0.47), roof), (0, 0, 1), "M", 1)
    d.hardpoint("ventral_forward", (0, yc(0.62), hs.bottom(yc(0.62))), (0, 0, -1), "M", 2)
    d.hardpoint_pair("aft", (hs.at(yc(0.24))[0] * 0.8, yc(0.24), hs.top(yc(0.24), hs.at(yc(0.24))[0] * 0.8)),
                     (0, 0, 1), "S", 3)
    for sx in (1, -1):
        d.rcs((sx * hs.side(yc(0.72), 0.0), yc(0.72), 0.0), (sx, 0, 0), H * 0.16)
    if d.g > 0.45:
        d.vents((0, yc(0.7), hs.top(yc(0.7)) + 0.002 * L), (0, 1, 0), (0, 0, 1), L * 0.06, W * 0.3, n=5)
    ant = d.antenna((W * 0.18, yc(0.56), roof - 0.004 * L), H * 0.55, H * 0.025)
    d.dome((-W * 0.18, yc(0.4), roof - 0.004 * L), H * 0.12)
    d.sock("socket_sensor_dorsal", "sensor", (-W * 0.18, yc(0.4), roof + H * 0.1), priority=6)
    d.sock("socket_utility_antenna", "utility", ant, priority=6)
    d.nav_lights((span + 0.003 * L, hy, zc), ant + Vector((0, 0, 0.01 * L)), H * 0.08)
    d.sock("socket_docking_ventral", "docking", (0, yc(0.4), hs.bottom(yc(0.4))), z=(0, 0, -1), priority=5)
    if st == "alien":
        alien_claws(d, hs, yc(0.7), yc(0.1))
    return hs


def gunship_catamaran(d):
    L, k, st = d.L, d.k, d.style
    U = d.U
    xo = L * U(0.1, 0.125)
    W, H = L * U(0.048, 0.058), L * U(0.07, 0.084)
    ctrl = [(0.0, 0.9, 0.9, 0.0), (0.08, 1.0, 1.0, 0.0), (0.62, 1.0, 1.0, 0.0), (0.86, 0.72, 0.78, -0.06),
            (1.0, 0.22, 0.34, -0.18)]
    y0, y1 = -0.44 * L, L * U(0.28, 0.36)
    hs = d.hull(ctrl, W, H, y0, y1, xo=xo)
    yc = lambda t: y0 + (y1 - y0) * t  # noqa: E731
    # centre command pod between the hulls
    cW, cH = xo * U(0.5, 0.6), H * U(0.62, 0.74)
    cy0, cy1 = -0.06 * L, 0.5 * L
    cctrl = [(0.0, 0.75, 0.7, 0.15), (0.18, 1.0, 1.0, 0.1), (0.58, 1.0, 1.0, 0.05), (0.86, 0.66, 0.68, -0.05),
             (1.0, 0.18, 0.24, -0.14)]
    hc = d.hull(cctrl, cW, cH, cy0, cy1, prof=sl.PROFILES["chined"] if st == "military" else d.prof)
    yp = lambda t: cy0 + (cy1 - cy0) * t  # noqa: E731
    d.canopy(hc, yp(0.52), yp(0.8), cW * 0.6, cH * 0.75)
    d.tail_cone(cy0 + 0.004 * L, cW * 0.7, cH * 0.66, z=0.15 * cH)
    # connecting wing-deck between the hulls
    ya, yb = yc(U(0.14, 0.2)), yc(U(0.56, 0.62))
    dz = H * 0.05
    deck_top = d.slab((0, (ya + yb) / 2, dz), (2 * xo, yb - ya, H * 0.34), "armor")
    # vertical fins on the hulls
    fy = yc(0.08)
    top = d.fin(xo, fy, hs.top(fy) - 0.05 * H, H * U(0.75, 1.0), L * 0.11, L * 0.05, L * 0.07, U(-8, 6), H * 0.14,
                mirror=True)
    # engines: one or two per hull, the rest on a housing on the deck
    n_eng = d.P["engine_count"]
    per = 1 if n_eng < 4 else 2
    if per == 1:
        d.nozzle(xo, 0.0, min(W, H) * 0.72, y0 + 0.01 * L, mirror=True, housing=False)
    else:
        for zz in (0.42, -0.42):
            d.nozzle(xo, zz * H, min(W * 0.72, H * 0.4), y0 + 0.01 * L, mirror=True, housing=False)
    rest = n_eng - 2 * per
    if rest > 0:
        for x, z, r in d.engine_layout(rest, 0.0, deck_top + H * 0.2, xo * 0.5, H * 0.2, H * 0.22):
            d.nozzle(x, z, r, ya + 0.01 * L)
    # weapons
    launcher_pods(d, (xo, yc(0.74), hs.bottom(yc(0.74)) - H * 0.1), W * 0.7, L * 0.1, H * 0.22)
    d.hardpoint_pair("forward", (xo, yc(0.66), hs.top(yc(0.66))), (0, 0, 1), "M", 0)
    d.hardpoint_pair("aft", (xo, yc(0.3), hs.top(yc(0.3))), (0, 0, 1), "M", 1)
    d.hardpoint("ventral_mid", (0, (ya + yb) / 2, dz - H * 0.17), (0, 0, -1), "M", 2)
    d.hardpoint("dorsal_mid", (0, yp(0.24), hc.top(yp(0.24))), (0, 0, 1), "S", 3)
    for sx in (1, -1):
        d.rcs((sx * (xo + hs.side(yc(0.84), 0.0)), yc(0.84), 0.0), (sx, 0, 0), H * 0.16)
    ant = d.antenna((cW * 0.3, yp(0.35), hc.top(yp(0.35), cW * 0.3) - 0.004 * L), H * 0.55, H * 0.025)
    d.sock("socket_utility_antenna", "utility", ant, priority=6)
    d.nav_lights((xo + hs.side(yc(0.45), 0.0) + 0.003 * L, yc(0.45), 0.0), ant + Vector((0, 0, 0.01 * L)), H * 0.08)
    d.sock("socket_docking_ventral", "docking", (0, yp(0.6), hc.bottom(yp(0.6))), z=(0, 0, -1), priority=5)
    d.layout["hull_offset_m"] = round(xo, 2)
    return hs


# ----------------------------------------------------------------------------- corvette
def design_corvette(d):
    fam = d.family(FAMILIES["corvette"])
    return {"destroyer": corvette_destroyer, "spear": corvette_spear, "arrowhead": corvette_arrowhead}[fam](d)


def corvette_destroyer(d):
    L, k, st = d.L, d.k, d.style
    U = d.U
    W, H = L * U(0.095, 0.115), L * U(0.055, 0.068)
    prow = U(0.3, 0.4)
    ctrl = [(0.0, 1.0, 0.95, 0.0), (0.1, 1.06, 1.0, 0.0), (0.16, 0.92, 0.9, 0.0), (0.27, 1.0, 1.0, 0.0),
            (1 - prow, 1.0, 1.0, 0.0), (1 - prow * 0.55, 0.64, 0.82, -0.05), (0.96, 0.18, 0.5, -0.12),
            (1.0, 0.05, 0.4, -0.16)]
    y0, y1 = -0.45 * L, 0.5 * L
    hs = d.hull(ctrl, W, H, y0, y1)
    yc = lambda t: y0 + (y1 - y0) * t  # noqa: E731
    # superstructure: base block + bridge
    t0 = U(0.3, 0.38)
    roof0 = d.block(hs, yc(t0), yc(t0 + 0.24), W * 0.44, H * 0.8, front=0.45, prof=d.prof, window=False,
                    panels=True)

    class _Top:
        def top(self, y, x=0.0):
            return roof0 - 0.02 * H
    b1y0, b1y1 = yc(t0 + 0.08), yc(t0 + 0.235)
    roof1 = d.block(_Top(), b1y0, b1y1, W * 0.34, H * 0.55, front=0.5, prof=d.prof, window=True)
    # bridge wings (side windows)
    k.mirrored(k.box, (W * 0.38, b1y1 - 0.03 * L, roof1 - H * 0.16), (W * 0.18, 0.04 * L, H * 0.14), "armor",
               bevel=H * 0.02)
    k.mirrored(k.box, (W * 0.44, b1y1 - 0.011 * L, roof1 - H * 0.16), (W * 0.08, 0.006 * L, H * 0.08), "glass")
    armor_belt(d, hs, yc(0.18), yc(0.7), -0.3, W * 0.05, 0.6)
    # turrets
    d.hardpoint("dorsal_forward", (0, yc(0.68), hs.top(yc(0.68))), (0, 0, 1), "M", 0)
    d.hardpoint("dorsal_aft", (0, yc(t0 - 0.07), hs.top(yc(t0 - 0.07))), (0, 0, 1), "M", 1)
    d.hardpoint("ventral_forward", (0, yc(0.6), hs.bottom(yc(0.6))), (0, 0, -1), "S", 2)
    d.hardpoint("ventral_aft", (0, yc(0.3), hs.bottom(yc(0.3))), (0, 0, -1), "S", 2)
    d.hardpoint("dorsal_bow", (0, yc(0.79), hs.top(yc(0.79))), (0, 0, 1), "S", 3)
    yz = yc(0.52)
    xz = hs.at(yz)[0] * 0.84
    d.hardpoint_pair("mid", (xz, yz, hs.top(yz, xz)), (0, 0, 1), "S", 4)
    # engine block
    d.tail_engines(d.P["engine_count"], y0 + 0.01 * L, W * 0.92, H * 0.88, H * 0.6)
    # fins
    fy = yc(0.12)
    if st == "sleek" or d.chance(0.5):
        top = d.fin(0.0, fy, hs.top(fy) - 0.05 * H, H * U(1.0, 1.4), L * 0.12, L * 0.05, L * 0.08, 0.0, H * 0.16)
        d.fin(0.0, fy, hs.bottom(fy) + 0.05 * H, -H * U(0.6, 0.8), L * 0.1, L * 0.05, L * 0.06, 0.0, H * 0.14)
        top = top + Vector((0, 0, 0.004 * L))
    else:
        xf = W * 0.55
        d.fin(xf, fy, hs.top(fy, xf) - 0.05 * H, H * U(0.8, 1.1), L * 0.11, L * 0.05, L * 0.07,
              U(22, 34), H * 0.14, mirror=True)
        top = None
    # radiators / hatches / vents
    if d.g > 0.2 and st in ("military", "industrial"):
        ry = yc(0.22)
        rz = 0.15 * H
        rx = hs.side(ry, rz)
        k.mirrored(k.box, (rx + W * 0.2, ry, rz), (W * 0.42, L * 0.08, H * 0.05), "graphite", bevel=H * 0.01)
        for q in range(5):
            k.mirrored(k.box, (rx + W * 0.2, ry - L * 0.032 + q * L * 0.016, rz + H * 0.035),
                       (W * 0.38, L * 0.005, H * 0.03), "trim", detail=H * 0.05)
    if d.g > 0.35:
        for f in (0.44, 0.56):
            yy = yc(f)
            zz = -0.6 * H
            xx = hs.side(yy, zz)
            d.hatch((xx + 0.002 * L, yy, zz), (1, 0, 0), H * 0.25, L * 0.03)
            d.hatch((-xx - 0.002 * L, yy, zz), (-1, 0, 0), H * 0.25, L * 0.03)
    if d.g > 0.5:
        d.vents((0, yc(0.2), hs.top(yc(0.2)) + 0.001 * L), (0, 1, 0), (0, 0, 1), L * 0.07, W * 0.4, n=7)
    for f in (0.86, 0.06):
        yy = yc(f)
        for sx in (1, -1):
            d.rcs((sx * hs.side(yy, 0.0), yy, 0.0), (sx, 0, 0), H * 0.16)
    ant = d.antenna((W * 0.1, (b1y0 + b1y1) / 2 - 0.02 * L, roof1 - 0.003 * L), H * 0.8, H * 0.025)
    d.dome((-W * 0.12, b1y0 + 0.02 * L, roof1 - 0.003 * L), H * 0.12)
    d.sock("socket_sensor_dorsal", "sensor", (-W * 0.12, b1y0 + 0.02 * L, roof1 + H * 0.1), priority=6)
    d.sock("socket_utility_antenna", "utility", ant, priority=6)
    np_ = Vector((hs.side(yc(0.55), 0.1 * H) + 0.003 * L, yc(0.55), 0.1 * H))
    d.nav_lights(np_, top if top is not None else ant + Vector((0, 0, 0.004 * L)), H * 0.07)
    d.sock("socket_docking_ventral", "docking", (0, yc(0.45), hs.bottom(yc(0.45))), z=(0, 0, -1), priority=5)
    if st == "alien":
        alien_claws(d, hs, yc(0.72), yc(0.12))
    return hs


def corvette_spear(d):
    L, k, st = d.L, d.k, d.style
    U = d.U
    W, H = L * U(0.068, 0.08), L * U(0.058, 0.068)
    prow = U(0.34, 0.42)
    ctrl = [(0.0, 0.9, 0.88, 0.0), (0.08, 1.0, 1.0, 0.0), (1 - prow, 1.0, 1.0, 0.0),
            (1 - prow * 0.5, 0.68, 0.82, -0.04), (0.96, 0.16, 0.42, -0.1), (1.0, 0.05, 0.3, -0.12)]
    y0, y1 = -0.4 * L, 0.5 * L
    hs = d.hull(ctrl, W, H, y0, y1)
    yc = lambda t: y0 + (y1 - y0) * t  # noqa: E731
    # long low deckhouse + bridge well forward
    deck = d.block(hs, yc(0.16), yc(0.56), W * 0.56, H * 0.42, front=0.25, prof=d.prof, window=False, panels=True)
    bt0 = U(0.56, 0.6)
    br = d.block(hs, yc(bt0), yc(bt0 + 0.13), W * 0.62, H * 0.78, front=0.5, prof=d.prof, window=True)
    # outboard engine nacelles on swept pylons
    nr = H * U(0.42, 0.5)
    nx = W + nr * U(2.3, 2.9)
    nz = -0.12 * H
    ny0, ny1 = y0, yc(U(0.42, 0.5))
    d.nacelle_pair(nx, nz, nr, ny0, ny1, pylon_root=(W * 0.82, nz * 0.5))
    d.tail_engines(d.P["engine_count"] - 2, y0 + 0.01 * L, W * 0.8, H * 0.78, H * 0.5)
    # keel fin
    d.fin(0.0, yc(0.14), hs.bottom(yc(0.14)) + 0.05 * H, -H * U(0.9, 1.25), L * 0.14, L * 0.06, L * 0.08, 0.0,
          H * 0.14)
    # hardpoints
    d.hardpoint("dorsal_forward", (0, yc(0.8), hs.top(yc(0.8))), (0, 0, 1), "M", 0)
    d.hardpoint("dorsal_aft", (0, yc(0.36), deck), (0, 0, 1), "M", 1)
    d.hardpoint("ventral_forward", (0, yc(0.62), hs.bottom(yc(0.62))), (0, 0, -1), "S", 2)
    d.hardpoint_pair("aft", (nx, ny0 + 0.55 * (ny1 - ny0), nz + nr * 1.15), (0, 0, 1), "S", 2)
    d.hardpoint("ventral_aft", (0, yc(0.38), hs.bottom(yc(0.38))), (0, 0, -1), "S", 4)
    # greebles
    if d.g > 0.35:
        d.vents((0, yc(0.24), deck + 0.001 * L), (0, 1, 0), (0, 0, 1), L * 0.06, W * 0.4, n=6)
    if d.g > 0.3:
        for f in (0.44, 0.52):
            yy = yc(f)
            zz = -0.4 * H
            xx = hs.side(yy, zz)
            d.hatch((xx + 0.002 * L, yy, zz), (1, 0, 0), H * 0.25, L * 0.03)
            d.hatch((-xx - 0.002 * L, yy, zz), (-1, 0, 0), H * 0.25, L * 0.03)
    for sx in (1, -1):
        d.rcs((sx * hs.side(yc(0.86), 0.0), yc(0.86), 0.0), (sx, 0, 0), H * 0.16)
    ant = d.antenna((W * 0.12, yc(bt0 + 0.05), br - 0.003 * L), H * 0.8, H * 0.025)
    d.dome((-W * 0.16, yc(bt0 + 0.03), br - 0.003 * L), H * 0.12)
    d.sock("socket_sensor_dorsal", "sensor", (-W * 0.16, yc(bt0 + 0.03), br + H * 0.1), priority=6)
    d.sock("socket_utility_antenna", "utility", ant, priority=6)
    d.nav_lights((nx + nr * 1.2, ny0 + 0.4 * (ny1 - ny0), nz), ant + Vector((0, 0, 0.004 * L)), H * 0.07)
    d.sock("socket_docking_ventral", "docking", (0, yc(0.5), hs.bottom(yc(0.5))), z=(0, 0, -1), priority=5)
    if st == "alien":
        alien_claws(d, hs, yc(0.74), yc(0.1))
    return hs


def corvette_arrowhead(d):
    L, k, st = d.L, d.k, d.style
    U = d.U
    W, H = L * U(0.15, 0.18), L * U(0.048, 0.058)
    ctrl = [(0.0, 0.92, 0.9, 0.0), (0.08, 1.0, 1.0, 0.0), (0.45, 0.76, 0.92, 0.0), (0.78, 0.4, 0.72, -0.04),
            (0.95, 0.12, 0.45, -0.08), (1.0, 0.04, 0.32, -0.1)]
    y0, y1 = -0.45 * L, 0.5 * L
    hs = d.hull(ctrl, W, H, y0, y1)
    yc = lambda t: y0 + (y1 - y0) * t  # noqa: E731
    # command tower aft
    t0 = U(0.08, 0.14)
    br, bry = d.tower(hs, yc(t0), yc(t0 + U(0.2, 0.26)), W * 0.3, H * U(0.9, 1.15), tiers=2)
    # canted fins at the aft corners
    xf = W * 0.78
    d.fin(xf, yc(0.05), hs.top(yc(0.05), xf) - 0.05 * H, H * U(1.0, 1.4), L * 0.12, L * 0.05, L * 0.08, U(26, 40),
          H * 0.15, mirror=True)
    # chine strakes along the leading flanks
    armor_belt(d, hs, yc(0.36), yc(0.82), -0.1, W * 0.04, 0.45)
    d.tail_engines(d.P["engine_count"], y0 + 0.01 * L, W * 0.88, H * 0.8, H * 0.62)
    # hardpoints
    d.hardpoint("dorsal_forward", (0, yc(0.62), hs.top(yc(0.62))), (0, 0, 1), "M", 0)
    d.hardpoint("dorsal_mid", (0, yc(0.45), hs.top(yc(0.45))), (0, 0, 1), "M", 1)
    yz = yc(0.32)
    xz = hs.at(yz)[0] * 0.62
    d.hardpoint_pair("mid", (xz, yz, hs.top(yz, xz)), (0, 0, 1), "S", 2)
    d.hardpoint("ventral_forward", (0, yc(0.56), hs.bottom(yc(0.56))), (0, 0, -1), "S", 3)
    d.hardpoint("ventral_aft", (0, yc(0.25), hs.bottom(yc(0.25))), (0, 0, -1), "S", 4)
    # greebles
    if d.g > 0.3:
        d.vents((0, yc(0.72), hs.top(yc(0.72)) + 0.001 * L), (0, 1, 0), (0, 0, 1), L * 0.06, W * 0.2, n=6)
    if d.g > 0.25:
        for sx in (1, -1):
            d.radiator((sx * W * 0.45, yc(0.14), hs.top(yc(0.14), sx * W * 0.45)), (0, 1, 0), (0, 0, 1), L * 0.08,
                       H * 0.3, 3, W * 0.015)
    for f in (0.86, 0.1):
        yy = yc(f)
        for sx in (1, -1):
            d.rcs((sx * hs.side(yy, 0.0), yy, 0.0), (sx, 0, 0), H * 0.16)
    ant = d.antenna((W * 0.08, bry, br - 0.003 * L), H * 0.9, H * 0.025)
    d.dome((-W * 0.08, bry - 0.02 * L, br - 0.003 * L), H * 0.12)
    d.sock("socket_sensor_dorsal", "sensor", (-W * 0.08, bry - 0.02 * L, br + H * 0.1), priority=6)
    d.sock("socket_utility_antenna", "utility", ant, priority=6)
    d.nav_lights((hs.side(yc(0.1), 0.0) + 0.003 * L, yc(0.1), 0.0), ant + Vector((0, 0, 0.004 * L)), H * 0.07)
    d.sock("socket_docking_ventral", "docking", (0, yc(0.42), hs.bottom(yc(0.42))), z=(0, 0, -1), priority=5)
    if st == "alien":
        alien_claws(d, hs, yc(0.66), yc(0.12))
    return hs


# ----------------------------------------------------------------------------- freighter
def design_freighter(d):
    fam = d.family(FAMILIES["freighter"])
    return {"truss": freighter_truss, "flatbed": freighter_flatbed, "bulk": freighter_bulk}[fam](d)


def freighter_cab(d):
    L, st = d.L, d.style
    U = d.U
    cw, chh = L * U(0.07, 0.085), L * U(0.06, 0.075)
    cy0, cy1 = 0.28 * L, 0.5 * L
    if d.chance(0.5):
        ctrl = [(0.0, 0.85, 0.85, 0.0), (0.2, 1.0, 1.0, 0.0), (0.6, 1.0, 1.0, 0.0), (0.85, 0.8, 0.75, -0.05),
                (1.0, 0.45, 0.3, -0.2)]
        hs = d.hull(ctrl, cw, chh, cy0, cy1)
        d.layout["cab"] = "bubble"
    else:
        ctrl = [(0.0, 0.85, 0.85, 0.0), (0.25, 1.0, 1.0, 0.0), (0.5, 1.0, 0.95, 0.0), (0.82, 0.6, 0.7, -0.08),
                (1.0, 0.15, 0.4, -0.2)]
        hs = d.hull(ctrl, cw * 1.1, chh * 0.9, cy0, cy1,
                    prof=sl.PROFILES["military" if st != "alien" else "alien"])
        d.layout["cab"] = "wedge"
    d.block(hs, cy0 + 0.06 * L, cy1 - 0.035 * L, cw * 0.62, chh * 0.55, front=0.45, prof=d.prof)
    return hs, cw, chh, cy0, cy1


def freighter_engine(d, wide=1.0):
    L = d.L
    U = d.U
    eh_w, eh_h = L * U(0.075, 0.09) * wide, L * U(0.06, 0.07)
    ey0, ey1 = -0.46 * L, -0.31 * L
    ectrl = [(0.0, 0.95, 0.95, 0.0), (0.15, 1.0, 1.0, 0.0), (0.8, 1.0, 1.0, 0.0), (1.0, 0.75, 0.7, 0.0)]
    hs_e = d.hull(ectrl, eh_w, eh_h, ey0, ey1, panels=True)
    d.tail_engines(d.P["engine_count"], ey0 + 0.005 * L, eh_w * 0.9, eh_h * 0.88, eh_h * 0.7)
    if d.g > 0.25:
        for sx in (1, -1):
            d.k.cyl((sx * (eh_w + L * 0.018), (ey0 + ey1) / 2, -eh_h * 0.3), L * 0.018, (ey1 - ey0) * 0.8,
                    (0, 1, 0), "trim", segs=14, bevel=L * 0.002)
    d.radiator((0, (ey0 + ey1) / 2, hs_e.top((ey0 + ey1) / 2)), (0, 1, 0), (0, 0, 1), (ey1 - ey0) * 0.6,
               eh_h * 0.5, 5, eh_w * 0.03)
    return hs_e, eh_w, eh_h, ey0, ey1


def freighter_tail(d, hs, hs_e, cw, chh, cy0, eh_w, ey0, ey1):
    L = d.L
    d.hardpoint("dorsal_aft", (0, ey1 - 0.02 * L, hs_e.top(ey1 - 0.02 * L)), (0, 0, 1), "S", 0)
    d.hardpoint("dorsal_forward", (0, cy0 + 0.04 * L, hs.top(cy0 + 0.04 * L)), (0, 0, 1), "S", 0)
    d.hardpoint("ventral_aft", (0, ey1 - 0.03 * L, hs_e.bottom(ey1 - 0.03 * L)), (0, 0, -1), "S", 1)
    d.hardpoint("ventral_forward", (0, cy0 + 0.05 * L, hs.bottom(cy0 + 0.05 * L)), (0, 0, -1), "S", 2)
    ant = d.antenna((cw * 0.3, cy0 + 0.04 * L, hs.top(cy0 + 0.04 * L, cw * 0.3) - 0.003 * L), chh * 0.9, chh * 0.04)
    d.sock("socket_utility_antenna", "utility", ant, priority=6)
    nav = Vector((hs_e.side((ey0 + ey1) / 2, 0.0) + 0.003 * L, (ey0 + ey1) / 2, 0.0))
    d.nav_lights(nav, ant + Vector((0, 0, 0.004 * L)), chh * 0.1)
    d.sock("socket_docking_ventral", "docking", (0, cy0 + 0.12 * L, hs.bottom(cy0 + 0.12 * L)), z=(0, 0, -1),
           priority=5)


def freighter_truss(d):
    L, k, st = d.L, d.k, d.style
    U = d.U
    hs, cw, chh, cy0, cy1 = freighter_cab(d)
    sy0, sy1 = -0.32 * L, cy0 + 0.02 * L
    sw = L * U(0.03, 0.04)
    d.truss(sy0, sy1, 0.0, 0.0, sw, 8)
    clen = L * U(0.075, 0.09)
    gap = L * 0.01
    n_rows = int((sy1 - sy0 - 0.03 * L) / (clen + gap))
    csx, csz = L * U(0.045, 0.055), L * U(0.04, 0.05)
    mats = ["armor", "marking", "graphite", "trim", "armor", "panel"]
    y = sy0 + 0.02 * L + clen / 2
    slots = []
    for r_ in range(n_rows):
        for pos, (px, pz) in (("starboard", (sw / 2 + csx / 2 + 0.003 * L, 0.0)),
                              ("dorsal", (0.0, sw / 2 + csz / 2 + 0.003 * L)),
                              ("ventral", (0.0, -sw / 2 - csz / 2 - 0.003 * L))):
            m = d.rng.choice(mats)
            keep = d.chance(0.86)
            if not keep:
                slots.append((pos, r_, y, px, pz))
                continue
            sz_ = (csx, clen, csz) if pos == "starboard" else (csx * 1.6, clen, csz)
            if st in ("alien", "sleek"):
                rr = min(sz_[0], sz_[2]) * 0.5
                if pos == "starboard":
                    k.mirrored(d.capsule, (px, y, pz), rr, clen * 0.8, (0, 1, 0), m if m != "panel" else "armor")
                else:
                    d.capsule((px, y, pz), rr, clen * 0.8, (0, 1, 0), m if m != "panel" else "armor")
            elif pos == "starboard":
                k.mirrored(d.container, (px, y, pz), sz_, m)
            else:
                d.container((px, y, pz), sz_, m)
        y += clen + gap
    cands = slots[:2] if len(slots) >= 2 else [("dorsal", 0, sy0 + 0.05 * L, 0.0, sw / 2 + 0.003 * L),
                                                ("ventral", 0, sy0 + 0.05 * L, 0.0, -sw / 2 - 0.003 * L)][:2 - len(slots)] + slots
    for q, (pos, r_, yy, px, pz) in enumerate(cands[:2]):
        nz = (0, 0, 1) if pos == "dorsal" else ((0, 0, -1) if pos == "ventral" else (1, 0, 0))
        d.sock("socket_cargo_%s_%02d" % (pos, q + 1), "cargo", (px, yy, pz), z=nz, priority=6, size_class="M")
    hs_e, eh_w, eh_h, ey0, ey1 = freighter_engine(d)
    freighter_tail(d, hs, hs_e, cw, chh, cy0, eh_w, ey0, ey1)
    return hs


def freighter_flatbed(d):
    L, k, st = d.L, d.k, d.style
    U = d.U
    hs, cw, chh, cy0, cy1 = freighter_cab(d)
    sy0, sy1 = -0.32 * L, cy0 + 0.02 * L
    sw = L * U(0.022, 0.028)
    ncol = d.opt("columns", (2, 3))
    csx, csz = L * U(0.05, 0.06), L * U(0.032, 0.04)
    xs = sw * 0.5 + csx * (0.5 if ncol == 2 else 1.0)
    k.mirrored(d.truss, sy0, sy1, xs if ncol == 2 else csx * 0.6, 0.0, sw, 8)
    clen = L * U(0.085, 0.1)
    gap = L * 0.008
    n_rows = int((sy1 - sy0 - 0.03 * L) / (clen + gap))
    mats = ["armor", "marking", "graphite", "trim", "armor"]
    cols = [csx / 2 + 0.002 * L] if ncol == 2 else [0.0, csx + 0.004 * L]
    y = sy0 + 0.02 * L + clen / 2
    slots = []
    for r_ in range(n_rows):
        for layer, pz in (("dorsal", sw / 2 + csz / 2 + 0.002 * L), ("ventral", -sw / 2 - csz / 2 - 0.002 * L)):
            for cx in cols:
                m = d.rng.choice(mats)
                if not d.chance(0.88):
                    slots.append((layer, r_, y, cx, pz))
                    continue
                if st in ("alien", "sleek"):
                    rr = min(csx, csz) * 0.5
                    fn = (lambda c_=(cx, y, pz), m_=m: d.capsule(c_, rr, clen * 0.82, (0, 1, 0), m_))
                else:
                    fn = (lambda c_=(cx, y, pz), m_=m: d.container(c_, (csx, clen, csz), m_))
                if cx > 1e-6:
                    k.mirrored(fn)
                else:
                    fn()
        y += clen + gap
    cands = (slots + [("dorsal", 0, sy0 + 0.05 * L, 0.0, sw / 2 + 0.002 * L),
                      ("ventral", 0, sy0 + 0.05 * L, 0.0, -sw / 2 - 0.002 * L)])[:2]
    for q, (pos, r_, yy, px, pz) in enumerate(cands):
        nz = (0, 0, 1) if pos == "dorsal" else (0, 0, -1)
        d.sock("socket_cargo_%s_%02d" % (pos, q + 1), "cargo", (px, yy, pz), z=nz, priority=6, size_class="M")
    hs_e, eh_w, eh_h, ey0, ey1 = freighter_engine(d, wide=1.3)
    freighter_tail(d, hs, hs_e, cw, chh, cy0, eh_w, ey0, ey1)
    return hs


def freighter_bulk(d):
    L, k, st = d.L, d.k, d.style
    U = d.U
    W, H = L * U(0.085, 0.1), L * U(0.078, 0.092)
    ctrl = [(0.0, 0.88, 0.9, 0.0), (0.05, 1.0, 1.0, 0.0), (0.82, 1.0, 1.0, 0.0), (0.93, 0.86, 0.84, -0.04),
            (1.0, 0.58, 0.55, -0.1)]
    y0, y1 = -0.42 * L, 0.5 * L
    prof = sl.PROFILES["box"] if st in ("industrial", "military") else d.prof
    hs = d.hull(ctrl, W, H, y0, y1, prof=prof)
    yc = lambda t: y0 + (y1 - y0) * t  # noqa: E731
    # hatch covers along the cargo deck
    n_cov = d.opt("covers", (5, 6, 7))
    ca, cb = yc(0.3), yc(0.88)
    seg = (cb - ca) / n_cov
    cover_tops = []
    for i in range(n_cov):
        yy = ca + seg * (i + 0.5)
        zt = hs.top(yy)
        m = "marking" if i in (0, n_cov - 1) else "graphite"
        k.box((0, yy, zt + H * 0.04), (W * 1.5, seg * 0.86, H * 0.12), m, bevel=H * 0.02, taper=(0.9, 0.96))
        for q in (-1, 1):
            k.box((0, yy + q * seg * 0.22, zt + H * 0.105), (W * 1.36, seg * 0.05, H * 0.03), "trim", detail=H * 0.05)
        cover_tops.append((yy, zt + H * 0.1))
    # bridge tower aft (tanker style)
    tb0 = U(0.08, 0.12)
    br, bry = d.tower(hs, yc(tb0), yc(tb0 + 0.14), W * 0.62, H * U(0.9, 1.2), tiers=2)
    d.tail_engines(d.P["engine_count"], y0 + 0.01 * L, W * 0.88, H * 0.86, H * 0.6)
    # side doors
    if d.g > 0.3:
        for f in (0.4, 0.55, 0.7):
            yy = yc(f)
            xx = hs.side(yy, -0.2 * H)
            d.hatch((xx + 0.002 * L, yy, -0.2 * H), (1, 0, 0), H * 0.5, L * 0.05)
            d.hatch((-xx - 0.002 * L, yy, -0.2 * H), (-1, 0, 0), H * 0.5, L * 0.05)
    for q, i in enumerate((1, n_cov - 2)):
        yy, zt = cover_tops[i]
        d.sock("socket_cargo_dorsal_%02d" % (q + 1), "cargo", (0, yy, zt), z=(0, 0, 1), priority=6, size_class="M")
    d.hardpoint("dorsal_forward", (0, yc(0.93), hs.top(yc(0.93))), (0, 0, 1), "S", 0)
    d.hardpoint("dorsal_aft", (0, yc(0.04), hs.top(yc(0.04))), (0, 0, 1), "S", 0)
    d.hardpoint("ventral_forward", (0, yc(0.8), hs.bottom(yc(0.8))), (0, 0, -1), "S", 1)
    d.hardpoint("ventral_aft", (0, yc(0.2), hs.bottom(yc(0.2))), (0, 0, -1), "S", 2)
    for sx in (1, -1):
        d.rcs((sx * hs.side(yc(0.92), 0.0), yc(0.92), 0.0), (sx, 0, 0), H * 0.14)
    ant = d.antenna((W * 0.2, bry, br - 0.003 * L), H * 0.9, H * 0.03)
    d.sock("socket_utility_antenna", "utility", ant, priority=6)
    d.nav_lights((hs.side(yc(0.5), 0.3 * H) + 0.003 * L, yc(0.5), 0.3 * H), ant + Vector((0, 0, 0.004 * L)), H * 0.08)
    d.sock("socket_docking_ventral", "docking", (0, yc(0.5), hs.bottom(yc(0.5))), z=(0, 0, -1), priority=5)
    return hs


# ----------------------------------------------------------------------------- miner
def design_miner(d):
    fam = d.family(FAMILIES["miner"])
    return {"pods": miner_pods, "crane": miner_crane, "scoop": miner_scoop}[fam](d)


def drill_bit(d, x, y, z, dr, dl_, segs=10):
    """Stepped drill bit pointing +Y from (x, y, z)."""
    L = d.L
    M = Matrix.Translation(Vector((x, y, z))) @ sl.track((0, 1, 0)).to_4x4()
    prof = [(0.0, -0.02 * L), (dr * 1.05, -0.02 * L), (dr * 1.05, 0.02 * L), (dr * 0.95, 0.025 * L),
            (dr * 0.95, 0.3 * dl_), (dr * 0.72, 0.35 * dl_), (dr * 0.72, 0.6 * dl_), (dr * 0.45, 0.65 * dl_),
            (dr * 0.4, 0.85 * dl_), (0.0, dl_)]
    mats = ["graphite", "trim", "trim", "trim", "trim", "trim", "trim", "trim", "trim", "graphite"]
    d.k.lathe(prof, M, mats, segs=segs)


def miner_pods(d):
    L, k, st = d.L, d.k, d.style
    U = d.U
    W, H = L * U(0.12, 0.145), L * U(0.1, 0.12)
    ctrl = [(0.0, 0.9, 0.9, 0.0), (0.1, 1.0, 1.0, 0.0), (0.75, 1.0, 1.0, 0.0), (0.92, 0.88, 0.8, -0.08),
            (1.0, 0.7, 0.55, -0.15)]
    y0, y1 = -0.3 * L, 0.28 * L
    hs = d.hull(ctrl, W, H, y0, y1)
    yc = lambda t: y0 + (y1 - y0) * t  # noqa: E731
    # cab: block with a big window, offset to port for an industrial look
    cx = -W * 0.35 if d.chance(0.5) else 0.0
    roof = d.block(hs, yc(0.62), yc(0.98), W * 0.42, H * 0.55, x=cx, front=0.5, prof=d.prof)
    # ore hopper(s) behind the cab
    n_hop = d.rng.choice((1, 2))
    hz = hs.top(yc(0.35))
    hw_ = W * 0.8
    hh_ = H * 0.5
    spans = [(yc(0.12), yc(0.58))] if n_hop == 1 else [(yc(0.1), yc(0.32)), (yc(0.36), yc(0.58))]
    for hy0, hy1 in spans:
        k.box((0, (hy0 + hy1) / 2, hz + hh_ / 2 - 0.05 * H), (hw_ * 2, hy1 - hy0, hh_), "graphite", bevel=0.01 * L,
              taper=(1.08, 1.04))
        k.box((0, (hy0 + hy1) / 2, hz + hh_ - 0.05 * H + 0.002 * L), (hw_ * 1.9, (hy1 - hy0) * 0.94, 0.01 * L),
              "panel")
        n_ribs = 5 if n_hop == 1 else 3
        for i in range(n_ribs):
            y = hy0 + (hy1 - hy0) * (i + 0.5) / n_ribs
            k.box((0, y, hz + hh_ / 2 - 0.04 * H), (hw_ * 2.18, 0.012 * L, hh_ * 1.02), "trim", detail=0.01 * L)
        for i in range(int((4 + 6 * d.g) / n_hop)):
            px, py = d.U(-hw_ * 0.75, hw_ * 0.75), d.U(hy0 + 0.02 * L, hy1 - 0.02 * L)
            rr = d.U(0.012, 0.022) * L
            k.sphere((px, py, hz + hh_ - 0.05 * H), rr, "marking" if d.chance(0.3) else "trim", segs=6,
                     scale=(1.0, 1.2, 0.7), detail=rr)
    # mining tools: the seed picks twin arms, one spinal drill or a quad drill head
    mode = d.opt("drills", ("twin", "twin", "spinal", "quad"))
    ay0, ay1 = yc(0.55), 0.38 * L
    dl = 0.12 * L

    def drill(x, z, dr, dl_, tag, mirror=False):
        if mirror:
            k.mirrored(drill_bit, d, x, ay1, z, dr, dl_)
            d.sock("socket_utility_drill_starboard" + tag, "utility", (x, ay1 + dl_, z), z=(0, 1, 0), y=(0, 0, 1),
                   priority=4)
            d.sock("socket_utility_drill_port" + tag, "utility", (-x, ay1 + dl_, z), z=(0, 1, 0), y=(0, 0, 1),
                   priority=4)
        else:
            drill_bit(d, x, ay1, z, dr, dl_, segs=12)
            d.sock("socket_utility_drill" + tag, "utility", (x, ay1 + dl_, z), z=(0, 1, 0), y=(0, 0, 1), priority=4)

    if mode in ("twin", "quad"):
        ax = W * 0.78
        az = -0.25 * H
        k.mirrored(k.box, (ax, (ay0 + ay1) / 2, az), (W * 0.22, ay1 - ay0, H * (0.26 if mode == "twin" else 0.5)),
                   "armor", bevel=0.004 * L, taper=(0.85, 1.0))
        k.mirrored(k.box, (ax, (ay0 + ay1) / 2, az + H * (0.14 if mode == "twin" else 0.26)),
                   (W * 0.06, (ay1 - ay0) * 0.9, H * 0.04), "marking", detail=0.01 * L)
        if mode == "twin":
            drill(ax, az, H * 0.28, dl, "", mirror=True)
        else:
            drill(ax, az + H * 0.13, H * 0.17, dl * 0.8, "_upper", mirror=True)
            drill(ax, az - H * 0.13, H * 0.17, dl * 0.8, "_lower", mirror=True)
        k.mirrored(k.box, (ax, ay1 - 0.01 * L, az + H * 0.3), (W * 0.1, 0.01 * L, H * 0.05), "status",
                   detail=0.01 * L)
    else:
        bz = -0.15 * H
        k.box((0, (yc(0.85) + ay1) / 2, bz), (W * 0.42, ay1 - yc(0.85), H * 0.5), "armor", bevel=0.004 * L,
              taper=(0.8, 1.0))
        drill(0.0, bz, H * 0.45, dl * 1.25, "_main")
        for sx in (1, -1):
            rot = Matrix.Rotation(sx * math.radians(18), 3, "Z")
            k.box((sx * W * 0.5, (ay0 + ay1) / 2 + 0.02 * L, -0.45 * H), (W * 0.1, (ay1 - ay0) * 0.9, H * 0.12),
                  "graphite", rot=rot, bevel=0.003 * L)
        k.box((0, ay1 - 0.01 * L, bz + H * 0.28), (W * 0.2, 0.01 * L, H * 0.05), "status", detail=0.01 * L)
    # engine pods
    py0, py1 = -0.47 * L, -0.2 * L
    px_ = W * 1.12
    pr = H * 0.32
    rows = [(py1, 0.6), (py1 - 0.15 * (py1 - py0), 1.0), (py0 + 0.1 * (py1 - py0), 1.0), (py0, 0.92)]
    Pn = sl.PROFILES["industrial"]
    rings = [[(px_ + qx * pr * 1.2 * f, y, -0.1 * H + qz * pr * 1.15 * f) for qx, qz in Pn] for y, f in rows]
    k.mirrored(k.loft, rings, "armor", face_mat=lambda i, j, c, n: ("marking" if (i == 1 and j % 2) else
                                                                     ("graphite" if n.z < -0.5 else None)))
    k.mirrored(k.box, ((W * 0.9 + px_ - pr) / 2, (py0 + py1) / 2 + 0.03 * L, -0.1 * H), (px_ - pr - W * 0.75,
               0.1 * L, H * 0.16), "graphite", bevel=0.003 * L)
    d.nozzle(px_, -0.1 * H, pr * 0.9, py0 + 0.003 * L, mirror=True, housing=False)
    rest = d.P["engine_count"] - 2
    if rest > 0:
        for x, z, r in d.engine_layout(rest, 0.0, 0.0, W * 0.6, H * 0.6, H * 0.32):
            d.nozzle(x, z, r, y0 + 0.01 * L)
    d.hardpoint("dorsal_aft", (0, yc(0.06), hs.top(yc(0.06))), (0, 0, 1), "S", 0)
    d.hardpoint("ventral_forward", (0, yc(0.7), hs.bottom(yc(0.7))), (0, 0, -1), "S", 0)
    d.hardpoint("ventral_aft", (0, yc(0.25), hs.bottom(yc(0.25))), (0, 0, -1), "S", 1)
    if d.g > 0.3:
        d.pipes(yc(0.1), yc(0.7), hs.side(yc(0.4), 0.35 * H) + 0.006 * L, 0.35 * H, 0.008 * L, n=2)
    ant = d.antenna((cx + W * 0.15, yc(0.7), roof - 0.003 * L), H * 0.6, H * 0.03)
    d.sock("socket_utility_antenna", "utility", ant, priority=6)
    d.nav_lights((px_ + pr * 1.2, (py0 + py1) / 2, -0.1 * H), ant + Vector((0, 0, 0.004 * L)), H * 0.07)
    d.sock("socket_docking_ventral", "docking", (0, yc(0.45), hs.bottom(yc(0.45))), z=(0, 0, -1), priority=5)
    return hs


def miner_crane(d):
    L, k, st = d.L, d.k, d.style
    U = d.U
    W, H = L * U(0.11, 0.13), L * U(0.09, 0.105)
    ctrl = [(0.0, 0.92, 0.92, 0.0), (0.08, 1.0, 1.0, 0.0), (0.78, 1.0, 1.0, 0.0), (0.93, 0.85, 0.8, -0.06),
            (1.0, 0.66, 0.52, -0.14)]
    y0, y1 = -0.46 * L, U(0.14, 0.2) * L
    hs = d.hull(ctrl, W, H, y0, y1)
    yc = lambda t: y0 + (y1 - y0) * t  # noqa: E731
    roof = d.block(hs, yc(0.72), yc(0.99), W * 0.5, H * 0.55, front=0.5, prof=d.prof)
    # side ore bins along the flanks
    bw, bh = W * U(0.38, 0.48), H * U(0.7, 0.9)
    ba, bb = yc(0.14), yc(0.6)
    bx = W * 0.92 + bw / 2
    k.mirrored(k.box, (bx, (ba + bb) / 2, -0.12 * H), (bw, bb - ba, bh), "graphite", bevel=0.004 * L, taper=(1.0, 0.96))
    for i in range(4):
        yy = ba + (bb - ba) * (i + 0.5) / 4
        k.mirrored(k.box, (bx, yy, -0.12 * H), (bw * 1.06, 0.012 * L, bh * 1.04), "trim", detail=0.01 * L)
    k.mirrored(k.box, (bx, (ba + bb) / 2, -0.12 * H + bh / 2 + 0.002 * L), (bw * 0.9, (bb - ba) * 0.95, 0.008 * L),
               "panel")
    for i in range(int(3 + 4 * d.g)):
        px, py = d.U(bx - bw * 0.3, bx + bw * 0.3), d.U(ba + 0.02 * L, bb - 0.02 * L)
        rr = d.U(0.01, 0.018) * L
        k.mirrored(k.sphere, (px, py, -0.12 * H + bh / 2), rr, "marking" if d.chance(0.3) else "trim", segs=6,
                   scale=(1.0, 1.2, 0.7), detail=rr)
    # crane arms with drills: shoulder on the dorsal shoulder, elbow up and forward, wrist down ahead of the bow
    S = Vector((W * 0.55, yc(0.66), hs.top(yc(0.66), W * 0.55)))
    E = Vector((W * 0.7, y1 + L * U(0.03, 0.07), S.z + H * U(0.5, 0.8)))
    Wr = Vector((W * 0.62, y1 + L * U(0.15, 0.2), -0.05 * H))
    k.mirrored(d.beam, S, E, W * 0.24, W * 0.28, "armor")
    k.mirrored(d.beam, E, Wr, W * 0.2, W * 0.22, "graphite")
    k.mirrored(d.beam, E.lerp(Wr, 0.62), Wr, W * 0.26, W * 0.26, "armor")  # drill motor housing
    k.mirrored(k.cyl, S, W * 0.19, W * 0.3, (1, 0, 0), "trim", segs=16)
    k.mirrored(k.cyl, E, W * 0.16, W * 0.28, (1, 0, 0), "trim", segs=16)
    k.mirrored(d.beam, S + Vector((0, -0.02 * L, -0.03 * L)), E.lerp(Wr, 0.35) + Vector((0, 0, -0.01 * L)),
               W * 0.06, W * 0.06, "trim", detail=0.01 * L)
    dl = L * U(0.08, 0.11)
    k.mirrored(drill_bit, d, Wr.x, Wr.y, Wr.z, H * 0.24, dl)
    d.sock("socket_utility_drill_starboard", "utility", (Wr.x, Wr.y + dl, Wr.z), z=(0, 1, 0), y=(0, 0, 1), priority=4)
    d.sock("socket_utility_drill_port", "utility", (-Wr.x, Wr.y + dl, Wr.z), z=(0, 1, 0), y=(0, 0, 1), priority=4)
    d.tail_engines(d.P["engine_count"], y0 + 0.01 * L, W * 0.9, H * 0.86, H * 0.5)
    if d.g > 0.25:
        d.radiator((0, yc(0.3), hs.top(yc(0.3))), (0, 1, 0), (0, 0, 1), L * 0.12, H * 0.4, 4, W * 0.025)
    d.hardpoint("dorsal_aft", (0, yc(0.08), hs.top(yc(0.08))), (0, 0, 1), "S", 0)
    d.hardpoint("ventral_forward", (0, yc(0.72), hs.bottom(yc(0.72))), (0, 0, -1), "S", 0)
    d.hardpoint("ventral_aft", (0, yc(0.3), hs.bottom(yc(0.3))), (0, 0, -1), "S", 1)
    ant = d.antenna((W * 0.15, yc(0.86), roof - 0.003 * L), H * 0.6, H * 0.03)
    d.sock("socket_utility_antenna", "utility", ant, priority=6)
    d.nav_lights((bx + bw / 2 + 0.003 * L, (ba + bb) / 2, -0.12 * H), ant + Vector((0, 0, 0.004 * L)), H * 0.07)
    d.sock("socket_docking_ventral", "docking", (0, yc(0.5), hs.bottom(yc(0.5))), z=(0, 0, -1), priority=5)
    return hs


def _rect(hw, hh, zc, y, c=0.18):
    pts = [(-hw * (1 - c), hh), (hw * (1 - c), hh), (hw, hh * (1 - c)), (hw, -hh * (1 - c)), (hw * (1 - c), -hh),
           (-hw * (1 - c), -hh), (-hw, -hh * (1 - c)), (-hw, hh * (1 - c))]
    return [(x, y, zc + z) for x, z in pts]


def miner_scoop(d):
    L, k, st = d.L, d.k, d.style
    U = d.U
    W, H = L * U(0.115, 0.135), L * U(0.095, 0.11)
    ctrl = [(0.0, 0.9, 0.9, 0.0), (0.1, 1.0, 1.0, 0.0), (0.85, 1.0, 1.0, 0.0), (1.0, 0.94, 0.92, 0.0)]
    y0, y1 = -0.34 * L, 0.16 * L
    hs = d.hull(ctrl, W, H, y0, y1)
    yc = lambda t: y0 + (y1 - y0) * t  # noqa: E731
    # forward ore scoop: a thick-walled funnel opening forward (one closed loft: outside, lip, inside)
    sl_ = L * U(0.2, 0.26)
    mw, mh = W * U(1.25, 1.45), H * U(1.05, 1.22)
    th = W * 0.09
    ya, yb = y1 - 0.02 * L, y1 + sl_
    ym = ya + 0.35 * sl_
    rings = [_rect(W * 0.96, H * 0.92, 0.0, ya), _rect(W * 1.06, H * 0.98, 0.0, ym), _rect(mw, mh, 0.0, yb),
             _rect(mw - th, mh - th, 0.0, yb - 0.004 * L), _rect(W * 1.06 - th, H * 0.98 - th, 0.0, ym),
             _rect(W * 0.96 - th, H * 0.92 - th, 0.0, ya + 0.03 * L)]
    lip = "marking" if st in ("industrial", "military") else "trim"
    k.loft(rings, "graphite", cap0="fan", cap1="fan",
           face_mat=lambda i, j, c, n: "armor" if i < 2 else (lip if i == 2 else None))
    # stiffening ribs around the funnel (bands slightly proud of the outer wall)
    for u in (0.3, 0.62, 0.9):
        yy = ya + (yb - ya) * u
        fw = W * 0.96 + (mw - W * 0.96) * u
        fh = H * 0.92 + (mh - H * 0.92) * u
        band = [_rect(fw * 0.99, fh * 0.99, 0.0, yy - 0.006 * L), _rect(fw * 1.035, fh * 1.035, 0.0, yy - 0.004 * L),
                _rect(fw * 1.035, fh * 1.035, 0.0, yy + 0.004 * L), _rect(fw * 0.99, fh * 0.99, 0.0, yy + 0.006 * L)]
        k.loft(band, "trim", cap0=None, cap1=None, detail=0.01 * L)
    for i in range(7):
        x = (i - 3) / 3.0 * (mw - th) * 0.85
        for zs in (1, -1):
            k.box((x, yb + 0.006 * L, zs * (mh - th * 0.5)), (th * 0.5, 0.016 * L, th * 0.6), "trim", detail=th * 0.4,
                  taper=(0.5, 0.5))
    drill_bit(d, 0.0, ya + 0.03 * L, 0.0, H * 0.38, sl_ * 0.92, segs=12)
    d.sock("socket_utility_drill", "utility", (0.0, ya + 0.03 * L + sl_ * 0.92, 0.0), z=(0, 1, 0), y=(0, 0, 1),
           priority=4)
    d.sock("socket_cargo_scoop", "cargo", (0.0, yb, 0.0), z=(0, 1, 0), y=(0, 0, 1), priority=6, size_class="L")
    # cab offset to port, processing stack behind it
    cx = -W * 0.45
    roof = d.block(hs, yc(0.5), yc(0.92), W * 0.36, H * 0.6, x=cx, front=0.5, prof=d.prof)
    k.box((W * 0.3, yc(0.4), hs.top(yc(0.4)) + H * 0.3), (W * 0.5, (y1 - y0) * 0.3, H * 0.6), "graphite",
          bevel=0.004 * L, taper=(0.85, 0.9))
    k.cyl((W * 0.3, yc(0.4), hs.top(yc(0.4)) + H * 0.75), W * 0.1, H * 0.4, mat="trim", segs=12)
    # ventral engine pods on pylons
    nr = H * U(0.28, 0.34)
    nx, nz = W * 0.62, -H * 1.1
    d.nacelle_pair(nx, nz, nr, -0.46 * L, yc(U(0.5, 0.65)), pylon_root=(W * 0.45, -H * 0.8))
    d.tail_engines(d.P["engine_count"] - 2, y0 + 0.01 * L, W * 0.88, H * 0.86, H * 0.45)
    d.hardpoint("dorsal_aft", (0, yc(0.1), hs.top(yc(0.1))), (0, 0, 1), "S", 0)
    d.hardpoint("starboard_forward", (W * 0.45, yc(0.8), hs.top(yc(0.8), W * 0.45)), (0, 0, 1), "S", 0)
    d.hardpoint("ventral_mid", (0, yc(0.5), hs.bottom(yc(0.5))), (0, 0, -1), "S", 1)
    ant = d.antenna((cx, yc(0.62), roof - 0.003 * L), H * 0.6, H * 0.03)
    d.sock("socket_utility_antenna", "utility", ant, priority=6)
    d.nav_lights((mw + 0.003 * L, yb - 0.02 * L, 0.0), ant + Vector((0, 0, 0.004 * L)), H * 0.07)
    d.sock("socket_docking_ventral", "docking", (0, yc(0.25), hs.bottom(yc(0.25))), z=(0, 0, -1), priority=5)
    return hs


# ----------------------------------------------------------------------------- frigate
def design_frigate(d):
    fam = d.family(FAMILIES["frigate"])
    return {"wedge": frigate_wedge, "hammer": frigate_hammer, "fork": frigate_fork}[fam](d)


def _hangar_slot(d, hs, hy, W):
    L, k = d.L, d.k
    k.box((0, hy, hs.bottom(hy) - 0.001 * L), (W * 0.5, L * 0.07, 0.004 * L), "panel")
    k.box((0, hy, hs.bottom(hy) - 0.0025 * L), (W * 0.44, L * 0.004, 0.003 * L), "status", detail=0.004 * L)
    d.sock("socket_docking_hangar", "docking", (0, hy, hs.bottom(hy) - 0.003 * L), z=(0, 0, -1), priority=5)


def frigate_wedge(d):
    L, k, st = d.L, d.k, d.style
    U = d.U
    W, H = L * U(0.15, 0.19), L * U(0.042, 0.052)
    wedge = U(0.18, 0.28)
    ctrl = [(0.0, 0.96, 1.0, 0.0), (wedge, 1.0, 1.0, 0.0), (0.55, 0.76, 0.85, -0.04), (0.82, 0.42, 0.6, -0.1),
            (0.96, 0.12, 0.3, -0.16), (1.0, 0.04, 0.16, -0.18)]
    y0, y1 = -0.45 * L, 0.5 * L
    hs = d.hull(ctrl, W, H, y0, y1)
    yc = lambda t: y0 + (y1 - y0) * t  # noqa: E731
    # dorsal ridge forward of the tower
    ry0, ry1 = yc(0.32), yc(0.84)
    rings = []
    for q in range(8):
        u = q / 7.0
        y = ry0 + (ry1 - ry0) * u
        f = 1.0 - 0.75 * u
        zt = hs.top(y) - H * 0.1
        rings.append([(W * 0.22 * f * px, y, zt + H * 0.32 * f * (pz * 0.5 + 0.5)) for px, pz in sl.PROFILES["military"]])
    k.loft(rings, "armor", face_mat=lambda i, j, c, n: "graphite" if abs(n.x) > 0.8 else None,
           inset={"thickness": 0.004 * L, "depth": 0.0015 * L, "select": lambda i, j, c, n: "raise" if n.z > 0.7 else None})
    # stepped tower
    t0 = U(0.12, 0.18)
    tw0 = d.block(hs, yc(t0), yc(t0 + 0.2), W * 0.5, H * 0.75, front=0.4, prof=d.prof, window=False, panels=True)

    class _Roof:
        def __init__(self, z):
            self.z = z

        def top(self, y, x=0.0):
            return self.z

    tw1 = d.block(_Roof(tw0 - 0.01 * L), yc(t0 + 0.04), yc(t0 + 0.17), W * 0.34, H * 0.7, front=0.45,
                  prof=d.prof, window=False)
    br = d.block(_Roof(tw1 - 0.006 * L), yc(t0 + 0.08), yc(t0 + 0.17), W * 0.5, H * 0.3, front=0.5,
                 prof=d.prof, window=True)
    for f in (0.3, 0.6):
        k.mirrored(k.box, (W * 0.36, yc(t0 + 0.1), tw0 - H * f * 0.6), (W * 0.06, L * 0.06, H * 0.05), "trim",
                   detail=0.005 * L)
    ant = d.antenna((W * 0.08, yc(t0 + 0.11), br - 0.002 * L), H * 1.2, H * 0.03)
    d.antenna((-W * 0.12, yc(t0 + 0.13), br - 0.002 * L), H * 0.7, H * 0.022, lamp=False)
    d.dome((0.0, yc(t0 + 0.06), tw1 - 0.002 * L), H * 0.22)
    d.sock("socket_sensor_dorsal", "sensor", (0.0, yc(t0 + 0.06), tw1 + H * 0.2), priority=6)
    d.sock("socket_utility_antenna", "utility", ant, priority=7)
    # turrets: heavy dorsal line + flank batteries + ventral
    for t, tag, pr, sz in ((0.62, "dorsal_forward", 0, "L"), (0.46, "dorsal_mid", 0, "L"), (0.06, "dorsal_aft", 2, "M")):
        y = yc(t)
        z = hs.top(y) if t < 0.32 or t > 0.84 else hs.top(y) + H * 0.32 * (1.0 - 0.75 * (y - ry0) / (ry1 - ry0)) - H * 0.1
        d.hardpoint(tag, (0, y, z), (0, 0, 1), sz, pr)
    for t, tag, pr in ((0.42, "forward", 1), (0.26, "aft", 1), (0.56, "bow", 3)):
        y = yc(t)
        x = hs.side(y, H * 0.35) * 0.82
        d.hardpoint_pair(tag, (x, y, hs.top(y, x)), (0, 0, 1), "M", pr)
    d.hardpoint("ventral_forward", (0, yc(0.5), hs.bottom(yc(0.5))), (0, 0, -1), "M", 2)
    d.hardpoint("ventral_aft", (0, yc(0.2), hs.bottom(yc(0.2))), (0, 0, -1), "M", 4)
    # engines
    d.tail_engines(d.P["engine_count"], y0 + 0.004 * L, W * 0.88, H * 0.82, H * 0.62, cz=0.05 * H)
    for sx in (1, -1):
        d.nozzle(sx * W * 0.98, -0.55 * H, H * 0.18, y0 + 0.004 * L, place="aux")
    # keel and side sponsons
    ky0, ky1 = yc(0.08), yc(0.5)
    rows = [(ky0, 0.4), (ky0 + 0.15 * (ky1 - ky0), 1.0), (ky1 - 0.3 * (ky1 - ky0), 0.9), (ky1, 0.2)]
    rings = []
    for y, f in rows:
        zb = hs.bottom(y) + 0.1 * H
        rings.append([(W * 0.1 * px, y, zb - H * 0.55 * f * (0.5 - pz * 0.5)) for px, pz in sl.PROFILES["military"]])
    k.loft(rings, "graphite")
    sy0, sy1 = yc(0.05), yc(0.4)
    zz = -0.35 * H
    sx_ = hs.side((sy0 + sy1) / 2, zz)
    k.mirrored(k.box, (sx_ + W * 0.06, (sy0 + sy1) / 2, zz), (W * 0.16, sy1 - sy0, H * 0.42), "armor",
               taper=(0.8, 0.95), bevel=0.003 * L)
    if d.g > 0.2:
        for f in (0.12, 0.2, 0.28):
            d.hatch((sx_ + W * 0.145, yc(f), zz), (1, 0, 0), H * 0.2, 0.02 * L)
            d.hatch((-sx_ - W * 0.145, yc(f), zz), (-1, 0, 0), H * 0.2, 0.02 * L)
    _hangar_slot(d, hs, yc(0.36), W)
    if d.g > 0.4:
        for f in (0.68, 0.74):
            d.vents((W * 0.35, yc(f), hs.top(yc(f), W * 0.35) + 0.001 * L), (0, 1, 0), (0, 0, 1), L * 0.03,
                    W * 0.12, n=5)
            d.vents((-W * 0.35, yc(f), hs.top(yc(f), -W * 0.35) + 0.001 * L), (0, 1, 0), (0, 0, 1), L * 0.03,
                    W * 0.12, n=5)
    for f in (0.8, 0.04):
        yy = yc(f)
        d.rcs((hs.side(yy, 0.0), yy, 0.0), (1, 0, 0), H * 0.12)
        d.rcs((-hs.side(yy, 0.0), yy, 0.0), (-1, 0, 0), H * 0.12)
    np_ = Vector((hs.side(yc(0.1), 0.0) + 0.002 * L, yc(0.1), 0.0))
    d.nav_lights(np_, ant + Vector((0, 0, 0.003 * L)), H * 0.06)
    if st == "alien":
        alien_claws(d, hs, yc(0.6), yc(0.1))
    return hs


def frigate_hammer(d):
    L, k, st = d.L, d.k, d.style
    U = d.U
    W, H = L * U(0.1, 0.12), L * U(0.052, 0.062)
    ctrl = [(0.0, 0.94, 1.0, 0.0), (0.08, 1.0, 1.0, 0.0), (0.68, 0.94, 0.95, -0.02), (0.86, 0.74, 0.8, -0.05),
            (1.0, 0.48, 0.5, -0.1)]
    y0, y1 = -0.45 * L, 0.42 * L
    hs = d.hull(ctrl, W, H, y0, y1)
    yc = lambda t: y0 + (y1 - y0) * t  # noqa: E731
    hy = yc(U(0.84, 0.9))
    span, depth, hh = L * U(0.15, 0.19), L * U(0.07, 0.09), H * U(0.85, 1.0)
    zc = hs.at(hy)[2] + 0.05 * H
    topz, f_at = d.hammer(hy, depth, span, hh, zc)
    t0 = U(0.38, 0.46)
    br, bry = d.tower(hs, yc(t0), yc(t0 + 0.18), W * 0.42, H * 0.8, tiers=2)
    armor_belt(d, hs, yc(0.3), yc(0.8), -0.2, W * 0.05, 0.6)
    # engine nacelles on pylons + the rest in the hull
    nr = H * U(0.5, 0.6)
    nx = W + nr * U(1.3, 1.7)
    nz = -0.05 * H
    ny0, ny1 = y0 + 0.004 * L, yc(U(0.3, 0.36))
    d.nacelle_pair(nx, nz, nr, ny0, ny1, pylon_root=(W * 0.85, 0.0))
    d.tail_engines(d.P["engine_count"] - 2, y0 + 0.004 * L, W * 0.86, H * 0.82, H * 0.6)
    d.fin(0.0, yc(0.08), hs.bottom(yc(0.08)) + 0.05 * H, -H * U(0.9, 1.2), L * 0.1, L * 0.04, L * 0.06, 0.0,
          H * 0.12)
    # hardpoints
    d.hardpoint("dorsal_forward", (0, yc(0.74), hs.top(yc(0.74))), (0, 0, 1), "L", 0)
    d.hardpoint("dorsal_aft", (0, yc(0.24), hs.top(yc(0.24))), (0, 0, 1), "L", 0)
    d.hardpoint_pair("forward", (span * 0.72, hy - depth * 0.2, topz(0.72)), (0, 0, 1), "M", 1)
    yz = yc(0.64)
    xz = hs.at(yz)[0] * 0.72
    d.hardpoint_pair("mid", (xz, yz, hs.top(yz, xz)), (0, 0, 1), "M", 2)
    d.hardpoint_pair("aft", (nx, ny0 + 0.62 * (ny1 - ny0), nz + nr * 1.15), (0, 0, 1), "M", 2)
    d.hardpoint("ventral_forward", (0, yc(0.62), hs.bottom(yc(0.62))), (0, 0, -1), "M", 3)
    d.hardpoint("ventral_aft", (0, yc(0.28), hs.bottom(yc(0.28))), (0, 0, -1), "M", 4)
    _hangar_slot(d, hs, yc(0.45), W)
    if d.g > 0.3:
        for f in (0.36, 0.5, 0.64):
            yy = yc(f)
            zz = -0.5 * H
            xx = hs.side(yy, zz)
            d.hatch((xx + 0.002 * L, yy, zz), (1, 0, 0), H * 0.22, 0.02 * L)
            d.hatch((-xx - 0.002 * L, yy, zz), (-1, 0, 0), H * 0.22, 0.02 * L)
    if d.g > 0.4:
        d.vents((0, yc(0.62), hs.top(yc(0.62)) + 0.001 * L), (0, 1, 0), (0, 0, 1), L * 0.04, W * 0.3, n=6)
    for f in (0.8, 0.06):
        yy = yc(f)
        for sx in (1, -1):
            d.rcs((sx * hs.side(yy, 0.0), yy, 0.0), (sx, 0, 0), H * 0.12)
    ant = d.antenna((W * 0.1, bry, br - 0.002 * L), H * 1.2, H * 0.03)
    d.dome((-W * 0.12, bry - 0.02 * L, br - 0.002 * L), H * 0.2)
    d.sock("socket_sensor_dorsal", "sensor", (-W * 0.12, bry - 0.02 * L, br + H * 0.2), priority=6)
    d.sock("socket_utility_antenna", "utility", ant, priority=7)
    d.nav_lights((span + 0.002 * L, hy, zc), ant + Vector((0, 0, 0.003 * L)), H * 0.06)
    if st == "alien":
        alien_claws(d, hs, yc(0.7), yc(0.1))
    return hs


def frigate_fork(d):
    L, k, st = d.L, d.k, d.style
    U = d.U
    W, H = L * U(0.13, 0.16), L * U(0.046, 0.056)
    y0, yf, y1 = -0.45 * L, L * U(0.0, 0.1), 0.5 * L
    ctrl = [(0.0, 0.92, 1.0, 0.0), (0.12, 1.0, 1.0, 0.0), (0.72, 0.96, 0.96, -0.02), (1.0, 0.9, 0.9, -0.03)]
    hs = d.hull(ctrl, W, H, y0, yf)
    yc = lambda t: y0 + (yf - y0) * t  # noqa: E731
    # forked prow + spinal weapon between the prongs
    gap = W * U(0.36, 0.48)
    pw = (W * 0.9 - gap / 2) / 2
    ph = H * 0.82
    zf = hs.at(yf)[2]
    prong = d.prongs(yf - 0.02 * L, y1, gap, pw, ph, zc=zf, droop=H * U(0.0, 0.3))
    sr = gap * 0.2
    k.cyl((0, yf + 0.06 * L, zf), sr * 1.4, 0.12 * L, (0, 1, 0), "graphite", segs=20, bevel=sr * 0.1)
    k.cyl((0, yf + 0.15 * L, zf), sr, 0.06 * L, (0, 1, 0), "trim", segs=20)
    k.cyl((0, yf + 0.183 * L, zf), sr * 0.8, 0.008 * L, (0, 1, 0), "status", segs=20)
    d.sock("socket_utility_spinal", "utility", (0, yf + 0.19 * L, zf), z=(0, 1, 0), y=(0, 0, 1), priority=5)
    # command tower on the main hull
    t0 = U(0.12, 0.3)
    br, bry = d.tower(hs, yc(t0), yc(t0 + 0.26), W * 0.36, H * 0.85, tiers=d.opt("tower_tiers", (1, 2)))
    if d.opt("sponsons", (True, False)):
        sy0, sy1 = yc(0.1), yc(0.62)
        zz = -0.35 * H
        sx_ = hs.side((sy0 + sy1) / 2, zz)
        k.mirrored(k.box, (sx_ + W * 0.06, (sy0 + sy1) / 2, zz), (W * 0.16, sy1 - sy0, H * 0.42), "armor",
                   taper=(0.8, 0.95), bevel=0.003 * L)
    else:
        armor_belt(d, hs, yc(0.12), yc(0.9), -0.25, W * 0.05, 0.6)
    d.tail_engines(d.P["engine_count"], y0 + 0.004 * L, W * 0.88, H * 0.82, H * 0.62)
    # hardpoints
    d.hardpoint_pair("forward", prong(0.28), (0, 0, 1), "M", 0)
    d.hardpoint_pair("bow", prong(0.58), (0, 0, 1), "S", 2)
    d.hardpoint("dorsal_mid", (0, yc(0.85), hs.top(yc(0.85))), (0, 0, 1), "L", 0)
    d.hardpoint("dorsal_aft", (0, yc(0.05), hs.top(yc(0.05))), (0, 0, 1), "M", 1)
    yz = yc(0.66)
    xz = hs.at(yz)[0] * 0.72
    d.hardpoint_pair("mid", (xz, yz, hs.top(yz, xz)), (0, 0, 1), "M", 1)
    d.hardpoint("ventral_forward", (0, yc(0.8), hs.bottom(yc(0.8))), (0, 0, -1), "M", 3)
    d.hardpoint("ventral_aft", (0, yc(0.3), hs.bottom(yc(0.3))), (0, 0, -1), "M", 4)
    _hangar_slot(d, hs, yc(0.55), W)
    for f in (0.9, 0.06):
        yy = yc(f)
        for sx in (1, -1):
            d.rcs((sx * hs.side(yy, 0.0), yy, 0.0), (sx, 0, 0), H * 0.12)
    if d.g > 0.4:
        d.vents((0, yc(0.95), hs.top(yc(0.95)) + 0.001 * L), (0, 1, 0), (0, 0, 1), L * 0.03, W * 0.25, n=6)
    ant = d.antenna((W * 0.08, bry, br - 0.002 * L), H * 1.1, H * 0.03)
    d.dome((-W * 0.1, bry - 0.02 * L, br - 0.002 * L), H * 0.2)
    d.sock("socket_sensor_dorsal", "sensor", (-W * 0.1, bry - 0.02 * L, br + H * 0.2), priority=6)
    d.sock("socket_utility_antenna", "utility", ant, priority=7)
    side = prong(0.15)
    d.nav_lights((side.x + pw * 0.9, side.y, zf), ant + Vector((0, 0, 0.003 * L)), H * 0.06)
    d.layout["fork_gap_m"] = round(gap, 2)
    return hs


# ----------------------------------------------------------------------------- carrier
def design_carrier(d):
    fam = d.family(FAMILIES["carrier"])
    return {"flattop": carrier_flattop, "hangar_pods": carrier_pods, "stepdeck": carrier_stepdeck}[fam](d)


def runway(d, dy0, dy1, dz, dw, edge_x=None):
    """Runway markings on a deck top at dz: centre dashes, edge lines, edge lights."""
    L, k = d.L, d.k
    n_d = 14
    for i in range(n_d):
        y = dy0 + (dy1 - dy0) * (i + 0.5) / n_d
        k.box((0, y, dz + 0.0004 * L), (0.006 * L, (dy1 - dy0) / n_d * 0.5, 0.001 * L), "marking", detail=0.004 * L)
    for sx in (1, -1):
        ex = edge_x(sx) if edge_x else dw * 0.82
        k.box((sx * ex, (dy0 + dy1) / 2, dz + 0.0004 * L), (0.003 * L, (dy1 - dy0) * 0.96, 0.001 * L), "marking",
              detail=0.003 * L)
        for i in range(10):
            y = dy0 + (dy1 - dy0) * (i + 0.5) / 10
            k.box((sx * dw * 0.97, y, dz + 0.0006 * L), (0.004 * L, 0.004 * L, 0.0015 * L), "status", detail=0.004 * L)


def carrier_flattop(d):
    L, k, st = d.L, d.k, d.style
    U = d.U
    W, H = L * U(0.1, 0.125), L * U(0.03, 0.04)
    ctrl = [(0.0, 0.9, 0.92, 0.0), (0.08, 1.0, 1.0, 0.0), (0.8, 1.0, 1.0, 0.0), (0.93, 0.82, 0.8, -0.06),
            (1.0, 0.55, 0.45, -0.18)]
    y0, y1 = -0.45 * L, 0.48 * L
    hs = d.hull(ctrl, W, H, y0, y1)
    yc = lambda t: y0 + (y1 - y0) * t  # noqa: E731
    # flight deck
    dy0, dy1 = yc(0.1), yc(0.97)
    dz = hs.top((dy0 + dy1) / 2) + 0.002 * L
    dw = W * U(1.05, 1.18)
    dt = 0.006 * L
    k.box((0, (dy0 + dy1) / 2, dz + dt / 2), (dw * 2, dy1 - dy0, dt), "graphite", bevel=dt * 0.3,
          taper=(1.0, 0.995))
    # island: starboard, outboard of the runway edge line (edge line pulled in to 0.7 dw on that side)
    # island half-width iw (bridge 1.2 iw): outer face stays inside the deck edge (bbox stays mirror-centred),
    # inner face outboard of the starboard runway edge line at 0.7 dw
    iw = W * 0.1
    ix = dw - iw * 1.25
    runway(d, dy0, dy1, dz + dt, dw, edge_x=lambda sx: dw * (0.7 if sx > 0 else 0.82))
    # bow hangar mouth
    k.box((0, yc(0.985), dz - H * 0.45), (W * 0.9, 0.012 * L, H * 0.7), "panel")
    k.box((0, yc(0.985) + 0.004 * L, dz - H * 0.45 - H * 0.28), (W * 0.82, 0.004 * L, H * 0.06), "status")
    d.sock("socket_docking_hangar", "docking", (0, yc(0.99), dz - H * 0.45), z=(0, 1, 0), y=(0, 0, 1), priority=5)
    # angled deck variant (port side, forward)
    if d.chance(0.5):
        th_ = math.radians(U(6, 10))
        rot = Matrix.Rotation(th_, 3, "Z")
        ay = yc(0.62)
        a_, b_ = dw * 0.3, (dy1 - dy0) * 0.25
        ext = a_ * math.cos(th_) + b_ * math.sin(th_)
        ax_ = -dw + ext + 0.002 * L  # stays inside the deck footprint: bbox (and root) stay mirror-centred
        k.box((ax_, ay, dz + dt * 1.5), (a_ * 2, b_ * 2, dt), "graphite", rot=rot, bevel=dt * 0.3)
        k.box((ax_, ay, dz + dt * 2 + 0.0004 * L), (0.004 * L, b_ * 1.9, 0.001 * L), "marking",
              rot=rot, detail=0.003 * L)
        d.layout["angled_deck"] = True
    else:
        U(6, 10)
        d.layout["angled_deck"] = False
    # island: tiered superstructure with bridge, mast and radar
    ilen = L * U(0.1, 0.16)
    iy0 = yc(U(0.3, 0.42))
    iy1 = iy0 + ilen

    class _Deck:
        def top(self, y, x=0.0):
            return dz + dt
    roof = d.block(_Deck(), iy0, iy1, iw, H * U(1.2, 1.8), x=ix, front=0.3, prof=d.prof, window=False,
                   embed=0.02, panels=True)

    class _Roof:
        def top(self, y, x=0.0):
            return roof - 0.002 * L
    br = d.block(_Roof(), iy0 + ilen * 0.3, iy1 + 0.004 * L, iw * 1.2, H * 0.55, x=ix, front=0.4, prof=d.prof,
                 window=True, embed=0.05)
    ant = d.antenna((ix, iy0 + ilen * 0.45, br - 0.001 * L), H * 2.0, H * 0.05)
    k.cyl((ix, iy0 + ilen * 0.75, br + H * 0.5), iw * 1.05, H * 0.08, axis=(0, 0.3, 1), mat="trim", segs=20,
          detail=H * 0.3)
    k.cyl((ix, iy0 + ilen * 0.75, br + H * 0.2), H * 0.05, H * 0.5, mat="graphite", segs=8, detail=H * 0.2)
    d.sock("socket_sensor_radar", "sensor", (ix, iy0 + ilen * 0.75, br + H * 0.55), priority=6)
    d.sock("socket_utility_antenna", "utility", ant, priority=7)
    # turrets along the deck edges (under the overhang)
    for t, tag, pr in ((0.86, "forward", 0), (0.55, "mid", 1), (0.2, "aft", 0)):
        y = yc(t)
        x = dw * 0.9
        d.hardpoint_pair(tag, (x, y, hs.bottom(y, x * 0.8)), (0, 0, -1), "M", pr + 1)
    d.hardpoint("ventral_forward", (0, yc(0.7), hs.bottom(yc(0.7))), (0, 0, -1), "M", 3)
    # side galleries under the deck overhang
    gy0, gy1 = yc(0.15), yc(0.9)
    gz = dz - H * 0.35
    gx = hs.side((gy0 + gy1) / 2, gz)
    k.mirrored(k.box, ((gx + dw) / 2, (gy0 + gy1) / 2, gz), (dw - gx, gy1 - gy0, H * 0.5), "armor",
               taper=(1.0, 1.0), bevel=0.002 * L)
    if d.g > 0.3:
        for i in range(8):
            y = gy0 + (gy1 - gy0) * (i + 0.5) / 8
            d.hatch((dw + 0.0015 * L, y, gz), (1, 0, 0), H * 0.3, 0.03 * L)
            d.hatch((-dw - 0.0015 * L, y, gz), (-1, 0, 0), H * 0.3, 0.03 * L)
    d.tail_engines(d.P["engine_count"], y0 + 0.003 * L, W * 0.9, H * 0.85, H * 0.8)
    np_ = Vector((dw, yc(0.5), dz + dt / 2))
    d.nav_lights(np_ + Vector((0.002 * L, 0, 0)), ant + Vector((0, 0, 0.003 * L)), H * 0.08)
    return hs


def carrier_pods(d):
    L, k, st = d.L, d.k, d.style
    U = d.U
    W, H = L * U(0.055, 0.068), L * U(0.05, 0.06)
    ctrl = [(0.0, 0.9, 0.92, 0.0), (0.08, 1.0, 1.0, 0.0), (0.8, 1.0, 1.0, 0.0), (0.94, 0.72, 0.76, -0.06),
            (1.0, 0.36, 0.4, -0.14)]
    y0, y1 = -0.45 * L, 0.44 * L
    hs = d.hull(ctrl, W, H, y0, y1)
    yc = lambda t: y0 + (y1 - y0) * t  # noqa: E731
    # twin hangar pods on pylons, open bows
    pw, ph = L * U(0.038, 0.048), H * U(0.95, 1.15)
    xo = W + pw + L * U(0.035, 0.05)
    pa, pb = -0.38 * L, 0.5 * L
    pctrl = [(0.0, 0.9, 0.9, 0.0), (0.06, 1.0, 1.0, 0.0), (0.9, 1.0, 1.0, 0.0), (1.0, 0.96, 0.94, 0.0)]
    hp = d.hull(pctrl, pw, ph, pa, pb, prof=sl.PROFILES["box"] if st != "alien" else d.prof, xo=xo)
    yq = lambda t: pa + (pb - pa) * t  # noqa: E731
    k.mirrored(k.box, (xo, pb + 0.002 * L, 0.0), (pw * 1.5, 0.006 * L, ph * 1.3), "panel")
    k.mirrored(k.box, (xo, pb + 0.005 * L, -ph * 0.62), (pw * 1.4, 0.004 * L, ph * 0.08), "status")
    d.sock("socket_docking_hangar_starboard", "docking", (xo, pb + 0.006 * L, 0.0), z=(0, 1, 0), y=(0, 0, 1),
           priority=5)
    d.sock("socket_docking_hangar_port", "docking", (-xo, pb + 0.006 * L, 0.0), z=(0, 1, 0), y=(0, 0, 1),
           priority=5)
    for f in (0.15, 0.48, 0.8):
        y = yq(f)
        xa, xb = W * 0.8, xo - pw * 0.9
        k.mirrored(k.box, ((xa + xb) / 2, y, 0.0), (xb - xa, L * 0.04, H * 0.35), "graphite", bevel=0.002 * L,
                   taper=(1.0, 0.8))
    # command tower on the spine
    t0 = U(0.5, 0.62)
    br, bry = d.tower(hs, yc(t0), yc(t0 + 0.14), W * 0.72, H * U(1.1, 1.4), tiers=2)
    k.cyl((0, bry - 0.01 * L, br + H * 0.5), H * 0.5, H * 0.08, axis=(0, 0.3, 1), mat="trim", segs=20, detail=H * 0.3)
    k.cyl((0, bry - 0.01 * L, br + H * 0.2), H * 0.05, H * 0.5, mat="graphite", segs=8, detail=H * 0.2)
    d.sock("socket_sensor_radar", "sensor", (0, bry - 0.01 * L, br + H * 0.55), priority=6)
    ant = d.antenna((W * 0.3, bry + 0.01 * L, br - 0.002 * L), H * 1.4, H * 0.04)
    d.sock("socket_utility_antenna", "utility", ant, priority=7)
    # engines: spine + one per pod
    n_eng = d.P["engine_count"]
    d.nozzle(xo, 0.0, min(pw, ph) * 0.72, pa + 0.003 * L, mirror=True, housing=False)
    d.tail_engines(n_eng - 2, y0 + 0.003 * L, W * 0.9, H * 0.86, H * 0.6)
    # hardpoints
    d.hardpoint_pair("forward", (xo, yq(0.78), hp.top(yq(0.78))), (0, 0, 1), "M", 0)
    d.hardpoint_pair("aft", (xo, yq(0.3), hp.top(yq(0.3))), (0, 0, 1), "M", 1)
    d.hardpoint("dorsal_forward", (0, yc(0.86), hs.top(yc(0.86))), (0, 0, 1), "M", 2)
    d.hardpoint("dorsal_aft", (0, yc(0.25), hs.top(yc(0.25))), (0, 0, 1), "M", 2)
    d.hardpoint("ventral_forward", (0, yc(0.7), hs.bottom(yc(0.7))), (0, 0, -1), "M", 3)
    if d.g > 0.3:
        for i in range(6):
            y = yq(0.2 + 0.1 * i)
            d.hatch((xo + pw + 0.0015 * L, y, 0.0), (1, 0, 0), ph * 0.5, 0.03 * L)
            d.hatch((-xo - pw - 0.0015 * L, y, 0.0), (-1, 0, 0), ph * 0.5, 0.03 * L)
    d.nav_lights((xo + pw * 1.0 + 0.002 * L, yq(0.6), ph * 0.6), ant + Vector((0, 0, 0.003 * L)), H * 0.08)
    d.layout["pod_offset_m"] = round(xo, 2)
    return hs


def carrier_stepdeck(d):
    L, k, st = d.L, d.k, d.style
    U = d.U
    W, H = L * U(0.1, 0.12), L * U(0.042, 0.05)
    ctrl = [(0.0, 0.92, 0.92, 0.0), (0.08, 1.0, 1.0, 0.0), (0.5, 1.0, 1.0, 0.0), (0.78, 0.74, 0.92, -0.03),
            (0.94, 0.36, 0.7, -0.08), (1.0, 0.14, 0.5, -0.12)]
    y0, y1 = -0.45 * L, 0.5 * L
    hs = d.hull(ctrl, W, H, y0, y1)
    yc = lambda t: y0 + (y1 - y0) * t  # noqa: E731
    # aft flight deck (lower level), wider than the hull
    dy0, dy1 = yc(0.05), yc(U(0.46, 0.52))
    dz = hs.top((dy0 + dy1) / 2) + 0.002 * L
    dw = W * U(1.15, 1.3)
    dt = 0.006 * L
    k.box((0, (dy0 + dy1) / 2, dz + dt / 2), (dw * 2, dy1 - dy0, dt), "graphite", bevel=dt * 0.3)
    runway(d, dy0, dy1, dz + dt, dw)
    # forward hangar superstructure (upper level) following the tapering hull; hangar door opens aft
    fy0, fy1 = dy1 - 0.01 * L, yc(0.93)
    fh = H * U(0.9, 1.2)
    Pb = sl.PROFILES["box"] if st != "alien" else d.prof
    rings, zr = [], []
    us = [0.0, 0.14, 0.3, 0.46, 0.56, 0.61, 0.76, 0.89, 1.0]
    n = len(us)
    for u in us:
        y = fy0 + (fy1 - fy0) * u
        hw = hs.at(y)[0] * 0.9
        hh = fh * 0.5 * (1.0 if u < 0.65 else 1.0 - (u - 0.65) / 0.35 * 0.55)
        zb = hs.top(y) - 0.1 * fh
        rings.append([(px * hw, y, zb + hh + pz * hh) for px, pz in Pb])
        zr.append(zb + 2 * hh)
    th = max(0.01, 0.025 * fh)
    k.loft(rings, "armor", cap0="fan", cap1="fan",
           face_mat=lambda i, j, c, nn: "graphite" if nn.z < -0.5 else ("marking" if (i == 4 and nn.z > -0.3)
                                                                       else None),
           inset={"thickness": th, "depth": th * 0.4,
                  "select": lambda i, j, c, nn: (("raise" if (abs(nn.x) > 0.7 or j % 2 == 0) else "plate")
                                                 if (0 < i < n - 3 and i != 4 and nn.z > -0.5) else None)})
    roof = min(zr[:5])
    zdoor = zr[0] - fh * 0.5
    k.box((0, fy0 - 0.002 * L, zdoor), (hs.at(fy0)[0] * 1.2, 0.006 * L, fh * 0.62), "panel")
    k.box((0, fy0 - 0.005 * L, zdoor + fh * 0.36), (hs.at(fy0)[0] * 1.1, 0.004 * L, fh * 0.05), "status")
    d.sock("socket_docking_hangar", "docking", (0, fy0 - 0.006 * L, zdoor), z=(0, -1, 0), y=(0, 0, 1), priority=5)
    # island on the upper block, starboard; aircraft elevators and vents on the roof
    fu = lambda u: fy0 + (fy1 - fy0) * u  # noqa: E731
    for u in (0.2, 0.48):
        k.box((-hs.at(fu(u))[0] * 0.45, fu(u), roof + 0.001 * L), (W * 0.36, (fy1 - fy0) * 0.12, 0.003 * L), "panel",
              detail=0.004 * L)
        k.box((-hs.at(fu(u))[0] * 0.45, fu(u), roof + 0.0025 * L), (W * 0.33, 0.002 * L, 0.002 * L), "status",
              detail=0.004 * L)
    if d.g > 0.35:
        d.vents((hs.at(fu(0.68))[0] * 0.35, fu(0.68), roof + 0.001 * L), (0, 1, 0), (0, 0, 1), (fy1 - fy0) * 0.1,
                W * 0.2, n=6)
    ilen = (fy1 - fy0) * U(0.24, 0.32)
    iy0 = fu(0.1)
    ix = hs.at(iy0 + ilen)[0] * 0.55

    class _Roof:
        def __init__(self, z):
            self.z = z

        def top(self, y, x=0.0):
            return self.z
    iroof = d.block(_Roof(roof - 0.002 * L), iy0, iy0 + ilen, W * 0.12, H * U(1.1, 1.5), x=ix, front=0.3,
                    prof=d.prof, window=False, panels=True)
    br = d.block(_Roof(iroof - 0.002 * L), iy0 + ilen * 0.3, iy0 + ilen + 0.004 * L, W * 0.15, H * 0.5, x=ix,
                 front=0.4, prof=d.prof, window=True)
    ant = d.antenna((ix, iy0 + ilen * 0.45, br - 0.001 * L), H * 1.8, H * 0.05)
    k.cyl((ix, iy0 + ilen * 0.8, br + H * 0.45), W * 0.12, H * 0.08, axis=(0, 0.3, 1), mat="trim", segs=20,
          detail=H * 0.3)
    d.sock("socket_sensor_radar", "sensor", (ix, iy0 + ilen * 0.8, br + H * 0.5), priority=6)
    d.sock("socket_utility_antenna", "utility", ant, priority=7)
    # side galleries along the lower deck
    gz = dz - H * 0.35
    gx = hs.side((dy0 + dy1) / 2, gz)
    k.mirrored(k.box, ((gx + dw) / 2, (dy0 + dy1) / 2, gz), (dw - gx, dy1 - dy0, H * 0.5), "armor", bevel=0.002 * L)
    if d.g > 0.3:
        for i in range(5):
            y = dy0 + (dy1 - dy0) * (i + 0.5) / 5
            d.hatch((dw + 0.0015 * L, y, gz), (1, 0, 0), H * 0.3, 0.03 * L)
            d.hatch((-dw - 0.0015 * L, y, gz), (-1, 0, 0), H * 0.3, 0.03 * L)
    d.tail_engines(d.P["engine_count"], y0 + 0.003 * L, W * 0.9, H * 0.85, H * 0.8)
    # hardpoints
    yf = fu(0.62)
    d.hardpoint_pair("forward", (hs.at(yf)[0] * 0.5, yf, roof), (0, 0, 1), "M", 0)
    for t, tag, pr in ((0.4, "mid", 1), (0.12, "aft", 1)):
        y = yc(t)
        d.hardpoint_pair(tag, (dw * 0.9, y, hs.bottom(y, dw * 0.72)), (0, 0, -1), "M", pr)
    d.hardpoint("ventral_forward", (0, yc(0.75), hs.bottom(yc(0.75))), (0, 0, -1), "M", 3)
    d.hardpoint("dorsal_aft", (-hs.at(fu(0.25))[0] * 0.5, fu(0.25), roof), (0, 0, 1), "M", 4)
    d.nav_lights((dw + 0.002 * L, yc(0.3), dz + dt / 2), ant + Vector((0, 0, 0.003 * L)), H * 0.08)
    return hs


# ----------------------------------------------------------------------------- transport
def design_transport(d):
    fam = d.family(FAMILIES["transport"])
    return {"pods": transport_pods, "drums": transport_drums, "barge": transport_barge}[fam](d)


def transport_command(d):
    L, st = d.L, d.style
    U = d.U
    cw, chh = L * U(0.05, 0.065), L * U(0.045, 0.055)
    cy0, cy1 = 0.3 * L, 0.5 * L
    ctrl = [(0.0, 0.8, 0.8, 0.0), (0.25, 1.0, 1.0, 0.0), (0.6, 1.0, 1.0, 0.0), (0.88, 0.7, 0.66, -0.06),
            (1.0, 0.25, 0.22, -0.15)]
    hs = d.hull(ctrl, cw, chh, cy0, cy1, prof=sl.PROFILES["sleek" if st != "industrial" else "industrial"])
    d.block(hs, cy0 + 0.05 * L, cy1 - 0.04 * L, cw * 0.55, chh * 0.5, front=0.5, prof=d.prof)
    return hs, cw, chh, cy0, cy1


def transport_engine(d, ey1=None, radiators="side"):
    """Engine section with its radiator layout: side (wide flat panels), vertical (dorsal + ventral fins) or
    x (four diagonal panels). Returns (hull, ew, eh, ey0, ey1, nav point, top of the section)."""
    L, k, st = d.L, d.k, d.style
    U = d.U
    ey0, ey1 = -0.47 * L, (ey1 if ey1 is not None else -0.33 * L)
    ew, eh = L * U(0.055, 0.07), L * U(0.05, 0.06)
    ectrl = [(0.0, 0.95, 0.95, 0.0), (0.2, 1.0, 1.0, 0.0), (0.75, 1.0, 1.0, 0.0), (1.0, 0.7, 0.7, 0.0)]
    hs_e = d.hull(ectrl, ew, eh, ey0, ey1, prof=sl.PROFILES["industrial" if st == "industrial" else "military"])
    rad = L * U(0.12, 0.18)
    ry = (ey0 + ey1) / 2 + 0.01 * L
    rl = min(L * 0.08, (ey1 - ey0) * 0.62)

    def panel(dirv, base):
        """Radiator panel from `base` outward along dirv (unit, in the XZ plane); both faces detailed so a
        mirrored pair looks identical."""
        dirv = Vector(dirv).normalized()
        side = Vector((0, 1, 0)).cross(dirv)
        R = Matrix((side, Vector((0, 1, 0)), dirv)).transposed()
        c = Vector(base) + dirv * (rad / 2)
        k.box(c, (0.004 * L, rl, rad), "graphite", rot=R, bevel=0.001 * L)
        for sg in (1, -1):
            k.box(c + side * sg * 0.0025 * L, (0.002 * L, rl * 0.94, rad * 0.96), "panel", rot=R, detail=0.003 * L)
            for q in range(6):
                k.box(Vector(base) + dirv * (rad * (q + 0.5) / 6) + side * sg * 0.003 * L,
                      (0.003 * L, rl * 0.98, 0.0025 * L), "trim", rot=R, detail=0.003 * L)
        return Vector(base) + dirv * rad
    tips = []
    if radiators == "vertical":
        for sz in (1, -1):
            tips.append(panel((0, 0, sz), (0, ry, sz * eh * 0.95)))
        nav = Vector((ew + 0.002 * L, ry, 0.0))
    elif radiators == "x":
        for sx in (1, -1):
            for sz in (1, -1):
                tips.append(panel((sx, 0, sz), (sx * ew * 0.7, ry, sz * eh * 0.7)))
        nav = tips[0] + Vector((0.002 * L, 0, 0))
    else:
        for sx in (1, -1):
            tips.append(panel((sx, 0, 0), (sx * ew, ry, eh * 0.2)))
        nav = Vector((ew + rad + 0.002 * L, ry, eh * 0.2))
    d.layout["radiators"] = radiators
    d.tail_engines(d.P["engine_count"], ey0 + 0.003 * L, ew * 0.9, eh * 0.88, eh * 0.7)
    return hs_e, ew, eh, ey0, ey1, nav


def transport_finish(d, hs, cw, chh, cy0, hs_e, ey1, nav, dock_y):
    L = d.L
    d.hardpoint("dorsal_forward", (0, cy0 + 0.06 * L, hs.top(cy0 + 0.06 * L)), (0, 0, 1), "M", 0)
    d.hardpoint("ventral_forward", (0, cy0 + 0.07 * L, hs.bottom(cy0 + 0.07 * L)), (0, 0, -1), "S", 0)
    d.hardpoint("dorsal_aft", (0, ey1 - 0.03 * L, hs_e.top(ey1 - 0.03 * L)), (0, 0, 1), "M", 1)
    d.hardpoint("ventral_aft", (0, ey1 - 0.03 * L, hs_e.bottom(ey1 - 0.03 * L)), (0, 0, -1), "S", 1)
    ant = d.antenna((cw * 0.2, cy0 + 0.11 * L, hs.top(cy0 + 0.11 * L, cw * 0.2) + chh * 0.4), chh * 0.9, chh * 0.03)
    d.sock("socket_utility_antenna", "utility", ant, priority=6)
    d.sock("socket_docking_dorsal", "docking", dock_y, z=(0, 0, 1), priority=5)
    d.sock("socket_docking_ventral", "docking", (0, cy0 + 0.13 * L, hs.bottom(cy0 + 0.13 * L)), z=(0, 0, -1),
           priority=5)
    d.nav_lights(nav, ant + Vector((0, 0, 0.003 * L)), chh * 0.08)


def transport_pods(d):
    L, k, st = d.L, d.k, d.style
    U = d.U
    hs, cw, chh, cy0, cy1 = transport_command(d)
    sy0, sy1 = -0.33 * L, cy0 + 0.01 * L
    sw = L * U(0.022, 0.03)
    d.truss(sy0, sy1, 0.0, 0.0, sw, 10)
    n_seg = d.opt("segments", (2, 3, 3, 4))
    n_pods = d.opt("pods", (3, 4, 4, 6))
    pr = L * U(0.028, 0.036)
    seg_len = (sy1 - sy0 - 0.04 * L) / n_seg
    plen = seg_len * 0.72
    off = sw * 0.5 + pr * 1.05
    ring_seg = d.rng.randrange(n_seg) if d.chance(0.5) else -1
    d.layout["habitat_ring"] = ring_seg >= 0
    for s_ in range(n_seg):
        y = sy0 + 0.02 * L + seg_len * (s_ + 0.5)
        k.cyl((0, y, 0), off + pr * 0.3, seg_len * 0.08, axis=(0, 1, 0), mat="trim", segs=24, bevel=0.002 * L)
        if s_ == ring_seg:
            R_ = off + pr * 2.2
            t_, w_ = pr * 0.55, min(seg_len * 0.3, pr * 1.6)
            M = Matrix.Translation(Vector((0, y, 0))) @ sl.track((0, 1, 0)).to_4x4()
            k.lathe([(R_ - t_, -w_), (R_ + t_, -w_), (R_ + t_ * 1.1, 0.0), (R_ + t_, w_), (R_ - t_, w_)], M,
                    ["armor", "marking", "armor", "armor", "panel"], segs=48)
            for q in range(4):
                a = q * PI / 2 + PI / 4
                c = Vector((math.cos(a) * (R_ + sw) / 2, y, math.sin(a) * (R_ + sw) / 2))
                k.box(c, (pr * 0.35, pr * 0.35, R_ - sw / 2), "graphite",
                      rot=sl.frame(Vector((math.cos(a), 0, math.sin(a)))), bevel=pr * 0.04)
            continue
        for p in range(n_pods):
            a = PI / 2 + p * math.tau / n_pods + (PI / n_pods if n_pods % 2 == 0 else 0.0)
            c = (off * math.cos(a), y, off * math.sin(a))
            m = "armor" if (p + s_) % 3 else "marking"
            d.capsule(c, pr, plen, (0, 1, 0), m, band="marking" if m == "armor" else "armor")
    hs_e, ew, eh, ey0, ey1, nav = transport_engine(d, radiators="side")
    transport_finish(d, hs, cw, chh, cy0, hs_e, ey1, nav, (0, sy0 + 0.01 * L, sw / 2))
    return hs


def transport_drums(d):
    L, k, st = d.L, d.k, d.style
    U = d.U
    hs, cw, chh, cy0, cy1 = transport_command(d)
    sy0, sy1 = -0.33 * L, cy0 + 0.01 * L
    sw = L * U(0.022, 0.03)
    d.truss(sy0, sy1, 0.0, 0.0, sw, 10)
    n_drum = d.opt("drums", (2, 3))
    R = L * U(0.075, 0.095)
    seg = (sy1 - sy0 - 0.04 * L) / n_drum
    dl = seg * U(0.6, 0.72)
    band = "marking" if st != "alien" else "status"
    for i in range(n_drum):
        y = sy0 + 0.02 * L + seg * (i + 0.5)
        h = dl / 2
        prof = [(0.0, -h - R * 0.3), (R * 0.55, -h - R * 0.27), (R * 0.88, -h - R * 0.12), (R, -h), (R, -h * 0.4),
                (R * 1.025, -h * 0.36), (R * 1.025, -h * 0.2), (R, -h * 0.16), (R, h * 0.3), (R * 0.985, h * 0.34),
                (R * 0.985, h * 0.42), (R, h * 0.46), (R, h), (R * 0.88, h + R * 0.12), (R * 0.55, h + R * 0.27),
                (0.0, h + R * 0.3)]
        mats = ["armor", "armor", "graphite", "armor", band, band, band, "armor", "glass", "glass", "glass", "armor",
                "graphite", "armor", "armor", "armor"]
        M = Matrix.Translation(Vector((0, y, 0))) @ sl.track((0, 1, 0)).to_4x4()
        k.lathe(prof, M, mats, segs=36)
        # docking collar rings between drums
        if i < n_drum - 1:
            yb = y + seg / 2
            k.cyl((0, yb, 0), sw * 1.6, seg - dl - R * 0.5, axis=(0, 1, 0), mat="graphite", segs=16)
            # radiator wings between drums
            rw = R * U(1.3, 1.8) if i == 0 else R * 1.5
            k.mirrored(k.box, (sw * 0.8 + rw / 2, yb, 0.0), (rw, (seg - dl) * 0.5, 0.004 * L), "graphite",
                       bevel=0.001 * L)
            k.mirrored(k.box, (sw * 0.8 + rw / 2, yb, 0.0025 * L), (rw * 0.94, (seg - dl) * 0.44, 0.002 * L), "panel",
                       detail=0.003 * L)
    d.layout["drum_radius_m"] = round(R, 1)
    hs_e, ew, eh, ey0, ey1, nav = transport_engine(d, radiators="vertical")
    transport_finish(d, hs, cw, chh, cy0, hs_e, ey1, nav, (0, sy0 + 0.01 * L, sw / 2))
    return hs


def transport_barge(d):
    L, k, st = d.L, d.k, d.style
    U = d.U
    W, H = L * U(0.07, 0.085), L * U(0.065, 0.08)
    ctrl = [(0.0, 0.92, 0.92, 0.0), (0.05, 1.0, 1.0, 0.0), (0.86, 1.0, 1.0, 0.0), (0.95, 0.86, 0.84, -0.04),
            (1.0, 0.55, 0.5, -0.12)]
    y0, y1 = -0.32 * L, 0.5 * L
    prof = sl.PROFILES["box" if st == "industrial" else ("military" if st == "military" else st)]
    hs = d.hull(ctrl, W, H, y0, y1, prof=prof)
    yc = lambda t: y0 + (y1 - y0) * t  # noqa: E731
    # passenger window rows
    n_win = 20
    for zr in (0.3, -0.05):
        z = zr * H
        for i in range(n_win):
            y = yc(0.1 + 0.72 * (i + 0.5) / n_win)
            x = hs.side(y, z)
            k.mirrored(k.box, (x + 0.0006 * L, y, z), (0.002 * L, 0.72 * (y1 - y0) / n_win * 0.62, H * 0.07), "glass",
                       detail=0.004 * L)
    # optional habitat ring around the mid hull
    if d.opt("ring", (True, False)):
        R_ = W * U(1.8, 2.2)
        yr = yc(0.45)
        M = Matrix.Translation(Vector((0, yr, 0))) @ sl.track((0, 1, 0)).to_4x4()
        t_, w_ = R_ * 0.12, L * 0.02
        k.lathe([(R_ - t_, -w_), (R_ + t_, -w_), (R_ + t_ * 1.1, 0.0), (R_ + t_, w_), (R_ - t_, w_)], M,
                ["armor", "marking", "armor", "armor", "glass"], segs=48)
        for q in range(4):
            a = q * PI / 2 + PI / 4
            c = Vector((math.cos(a) * (R_ + W) / 2, yr, math.sin(a) * (R_ + W) / 2))
            k.box(c, (L * 0.01, L * 0.01, R_ - W * 0.8), "graphite", rot=sl.frame(Vector((math.cos(a), 0, math.sin(a)))),
                  bevel=L * 0.001)
    # bridge on top forward
    roof = d.block(hs, yc(0.8), yc(0.95), W * 0.6, H * 0.5, front=0.5, prof=d.prof)
    if d.g > 0.3:
        for f in (0.25, 0.65):
            d.radiator((0, yc(f), hs.top(yc(f))), (0, 1, 0), (0, 0, 1), L * 0.08, H * 0.35, 4, W * 0.03)
    hs_e, ew, eh, ey0, ey1, nav = transport_engine(d, ey1=y0 + 0.01 * L, radiators="x")
    d.hardpoint("dorsal_forward", (0, yc(0.72), hs.top(yc(0.72))), (0, 0, 1), "M", 0)
    d.hardpoint("ventral_forward", (0, yc(0.72), hs.bottom(yc(0.72))), (0, 0, -1), "S", 0)
    d.hardpoint("dorsal_aft", (0, yc(0.05), hs.top(yc(0.05))), (0, 0, 1), "M", 1)
    d.hardpoint("ventral_aft", (0, yc(0.05), hs.bottom(yc(0.05))), (0, 0, -1), "S", 1)
    ant = d.antenna((W * 0.2, yc(0.88), roof - 0.002 * L), H * 0.8, H * 0.03)
    d.sock("socket_utility_antenna", "utility", ant, priority=6)
    d.sock("socket_docking_dorsal", "docking", (0, yc(0.35), hs.top(yc(0.35))), z=(0, 0, 1), priority=5)
    d.sock("socket_docking_ventral", "docking", (0, yc(0.5), hs.bottom(yc(0.5))), z=(0, 0, -1), priority=5)
    d.nav_lights(nav, ant + Vector((0, 0, 0.003 * L)), H * 0.08)
    return hs


def alien_claws(d, hs, y_bow, y_stern):
    """Alien silhouette cue: forward-swept claws at the bow and swept-back spines at the stern. Cross
    sections never thinner than ~1.5 % of the length so they do not read as stray lines at game distance."""
    k = d.k
    tmin = 0.016 * d.L
    hw, hh, zc = hs.at(y_bow)
    for dz, ln in ((0.25, 0.2), (-0.35, 0.15)):
        z = zc + dz * hh
        x = hs.side(y_bow, z)
        dirv = Vector((0.35, 1.0, 0.05 * (1 if dz > 0 else -1))).normalized()
        L_ = d.L * ln
        c = Vector((x * 0.9, y_bow, z)) + dirv * (L_ * 0.5)
        k.mirrored(k.box, c, (max(hh * 0.5, tmin * 1.6), max(hh * 0.28, tmin), L_), "graphite",
                   rot=sl.frame(dirv, (0, 0, 1)), taper=(0.4, 0.45))
    hw, hh, zc = hs.at(y_stern)
    for dz in (0.6, -0.6):
        z = zc + dz * hh
        x = hs.side(y_stern, z)
        dirv = Vector((0.55, -1.0, 0.35 * (1 if dz > 0 else -1))).normalized()
        L_ = d.L * 0.11
        c = Vector((x * 0.9, y_stern, z)) + dirv * (L_ * 0.5)
        k.mirrored(k.box, c, (max(hh * 0.45, tmin * 1.6), max(hh * 0.22, tmin), L_), "armor",
                   rot=sl.frame(dirv, (0, 0, 1)), taper=(0.4, 0.45))


DESIGNS = {"fighter": design_fighter, "interceptor": design_interceptor, "gunship": design_gunship,
           "corvette": design_corvette, "freighter": design_freighter, "miner": design_miner,
           "frigate": design_frigate, "carrier": design_carrier, "transport": design_transport}


# ============================================================================= build
def resolve_params(req):
    """Resolved parameters + warnings for every clamped, replaced or ignored value."""
    p = dict(req.get("params") or {})
    warn = []
    role = str(p.get("class", p.get("role", "fighter"))).lower()
    if role not in sl.ROLES:
        raise ValueError("unknown class %r (expected one of %s)" % (role, ", ".join(sl.ROLES)))
    info = sl.ROLES[role]
    seed = int(p.get("seed", 1))
    faction = str(p.get("faction", p.get("style", "military"))).lower()
    if faction not in sl.FACTIONS:
        raise ValueError("unknown faction %r" % faction)
    rng = random.Random(zlib.crc32(("%d|%s" % (seed, role)).encode()))
    lo, hi = info["engines"]
    eng_default = rng.randint(lo, hi)
    pal_default = rng.randint(0, 2)
    length = float(p.get("length_m", info["length"]))
    if length <= 0:
        raise ValueError("length_m must be > 0")
    cls = info["cls"]
    a, b = sl.CLASS_LENGTH[cls]
    if not a <= length <= b:
        new = next((c for c, (a2, b2) in sl.CLASS_LENGTH.items() if a2 <= length <= b2),
                   "ship_s" if length < a else "ship_l")
        warn.append("length_m %.1f outside the %s range %.0f-%.0f m for %s: budget_class %s used"
                    % (length, cls, a, b, role, new))
        cls = new
    eng = int(p.get("engine_count", eng_default))
    if not 1 <= eng <= 6:
        warn.append("engine_count %d clamped to %d (1..6)" % (eng, max(1, min(6, eng))))
        eng = max(1, min(6, eng))
    tur = int(p.get("turret_count", info["turrets"]))
    if tur < 0:
        warn.append("turret_count %d clamped to 0" % tur)
        tur = 0
    ws = str(p.get("wing_style", "auto")).lower()
    if ws not in WING_STYLES:
        warn.append("wing_style %r unknown (%s); auto used" % (ws, ", ".join(WING_STYLES)))
        ws = "auto"
    if ws != "auto":
        if role == "gunship" and ws not in ("none", "stub"):
            warn.append("gunship has stub wings only (auto | none | stub); wing_style %s ignored" % ws)
            ws = "auto"
        elif role not in ("fighter", "interceptor", "gunship"):
            warn.append("wing_style applies to fighter / interceptor / gunship; ignored for %s" % role)
            ws = "auto"
    g = float(p.get("greeble_density", 0.6))
    if not 0.0 <= g <= 1.0:
        warn.append("greeble_density %.2f clamped to 0..1" % g)
        g = max(0.0, min(1.0, g))
    pal = int(p.get("palette_variant", pal_default))
    if not 0 <= pal <= 2:
        warn.append("palette_variant %d wrapped to %d (0..2)" % (pal, pal % 3))
        pal %= 3
    fr = str(p.get("thruster_frame", "thrust")).lower()
    if fr not in sl.THRUSTER_FRAMES:
        warn.append("thruster_frame %r unknown (%s); thrust used" % (fr, ", ".join(sl.THRUSTER_FRAMES)))
        fr = "thrust"
    livery = p.get("livery") or {}
    alias = {"accent": "graphite", "hull": "armor", "lights": "status", "engine": "drive"}
    for key in list(livery):
        if alias.get(key, key) not in sl.MATS:
            warn.append("livery key %r ignored (expected %s)" % (key, ", ".join(sl.MATS)))
    psz = int(p.get("preview_size", 300))
    if not 120 <= psz <= 800:
        warn.append("preview_size %d clamped to 120..800" % psz)
        psz = max(120, min(800, psz))
    uv = str(p.get("uv", "smart")).lower()
    if uv not in ("smart", "box"):
        warn.append("uv %r unknown; smart used" % uv)
        uv = "smart"
    lay = p.get("layout", "auto")
    if lay not in (None, "", "auto") and not (str(lay).lower() in FAMILIES[role] or str(lay).lstrip("-").isdigit()):
        warn.append("layout %r unknown for %s (expected %s or an index); auto used"
                    % (lay, role, ", ".join(FAMILIES[role])))
        lay = "auto"
    out = {
        "class": role, "budget_class": cls, "seed": seed, "faction": faction, "length_m": length,
        "nominal_length": info["length"], "turret_count": tur, "engine_count": eng, "wing_style": ws,
        "greeble_density": g, "livery": livery, "palette_variant": pal, "thruster_frame": fr,
        "layout": lay if lay not in (None, "") else "auto",
        "tier": int(p.get("tier", 1)), "lods": bool(p.get("lods", True)), "preview": bool(p.get("preview", True)),
        "preview_turrets": bool(p.get("preview_turrets", True)), "preview_size": psz, "uv": uv,
    }
    out["asset_id"] = str(p.get("asset_id") or "ship_%s_%s_%03d" % (role, faction, seed % 1000))
    return out, warn


def build(P, lod):
    kit = sl.Kit(lod=lod, size_ref=P["nominal_length"])
    d = Design(P, kit)
    DESIGNS[P["class"]](d)
    placed = d.place_turrets(P["turret_count"])
    d.name_thrusters()
    d.turrets_placed = placed
    return d


def socket_budget(d, n_meshes, cls, warnings):
    """Keep root + meshes + COL + sockets within the node budget (drops low-priority sockets)."""
    limit = sl.BUDGETS[cls]["nodes"] - 1 - n_meshes - 1
    socks = sorted(d.sockets, key=lambda s: (s["priority"], s["name"]))
    if len(socks) > limit:
        dropped = socks[limit:]
        warnings.append("node budget: dropped %d low-priority sockets: %s"
                        % (len(dropped), ", ".join(s["name"] for s in dropped)))
        socks = socks[:limit]
    return socks


def nav_checks(summ):
    """socket_fx_nav_port at -X facing -X (red), socket_fx_nav_starboard at +X facing +X (green), glTF."""
    s = {x["game_socket"]: x for x in summ["sockets"]}
    port, stbd = s.get("socket_fx_nav_port"), s.get("socket_fx_nav_starboard")
    if not port or not stbd:
        return af.check("nav_lights_sides", True, "not present", "port -X / starboard +X", severity="warning")
    ok = (port["position_gltf"][0] < 0 and port["y_gltf"][0] < -0.99 and stbd["position_gltf"][0] > 0
          and stbd["y_gltf"][0] > 0.99)
    return af.check("nav_lights_sides", ok, {"port_x": port["position_gltf"][0], "port_out": port["y_gltf"],
                                             "starboard_x": stbd["position_gltf"][0], "starboard_out": stbd["y_gltf"]},
                    "port -X out -X, starboard +X out +X")


def run(req):
    P, warnings = resolve_params(req)
    cls = P["budget_class"]
    bud = sl.BUDGETS[cls]
    af.reset_scene()
    af.log("ship_gen", role=P["class"], seed=P["seed"], faction=P["faction"])

    # ---- LOD0 (with an automatic detail fallback if the class budget is exceeded)
    d = None
    for attempt in range(3):
        d = build(P, 0)
        tris = d.k.tri_count()
        if tris <= bud["tris"] * 0.97:
            break
        warnings.append("LOD0 %d tris over budget, rebuilding with less detail (attempt %d)" % (tris, attempt + 1))
        P["greeble_density"] = max(0.0, P["greeble_density"] - 0.35)
        d.k.bm.free()
        d = None
    if d is None:
        d = build(P, 1)
        warnings.append("LOD0 built at detail level 1 to stay inside the budget")
    warnings.extend(d.notes)
    n_main = sum(1 for e in d.engines if e["place"] == "main")
    if n_main != P["engine_count"]:
        warnings.append("layout %s builds %d main engines (engine_count %d requested)"
                        % (d.layout.get("family"), n_main, P["engine_count"]))
    kit = d.k
    # rescale to the exact length, centre on the bbox (root = geometric centre of the hull)
    lo, hi = kit.bounds()
    s = P["length_m"] / max(hi.y - lo.y, 1e-6)
    c = (lo + hi) / 2
    c.x = 0.0
    T = Matrix.Scale(s, 4) @ Matrix.Translation(-c)
    kit.transform_all(T)

    pal = sl.palette_for(P["faction"], P["palette_variant"], P["livery"])
    mats = sl.make_materials(pal)
    scene = bpy.context.scene
    root = bpy.data.objects.new(P["asset_id"], None)
    root.empty_display_type = "PLAIN_AXES"
    root.empty_display_size = P["length_m"] * 0.1
    scene.collection.objects.link(root)
    objs = sl.split_by_material(kit, mats, "hull", parent=root, angle=d.sharp_angle)
    kit.bm.free()
    sl.uv_unwrap(objs, P["uv"])
    col, col_info = sl.convex_collision(objs, parent=root)
    socks = socket_budget(d, len(objs), cls, warnings)
    empties = []
    ssz = P["length_m"] * 0.02
    for sd in socks:
        pos = T @ sd["pos"]
        extras = {k_: v for k_, v in sd["extras"].items()}
        if sd["kind"] == "thruster" and "mount_d_m" in extras:
            extras["mount_d_m"] = round(float(extras["mount_d_m"]) * s, 3)  # nozzles follow the length rescale
        e = sl.add_socket(sd["name"], sd["kind"], pos, z_axis=sd["z"], y_hint=sd["y"], parent=root, size=ssz,
                          **extras)
        empties.append(e)
    bpy.context.view_layer.update()

    out_glb = af.out_path(req, "ship.glb")
    export_objs = [root] + objs + [col] + empties
    af.export_glb(out_glb, export_objs, animations=False, skins=False, export_tangents=False,
                  export_image_format="AUTO", export_draco_mesh_compression_enable=False)
    summ = sl.glb_summary(out_glb)

    # ---- LODs
    outputs = {"glb": out_glb}
    lod_tris, lod_files = {}, []
    if P["lods"]:
        lod_coll = bpy.data.collections.new("LODs - separate files")
        scene.collection.children.link(lod_coll)
        for lod in (1, 2):
            dl = build(P, lod)
            dl.k.transform_all(T)
            lroot = bpy.data.objects.new("%s_lod%d" % (P["asset_id"], lod), None)
            lod_coll.objects.link(lroot)
            lobjs = sl.split_by_material(dl.k, mats, "hull", parent=lroot, collection=lod_coll,
                                         angle=d.sharp_angle, suffix="_lod%d" % lod)
            dl.k.bm.free()
            frac = 0.35 if lod == 1 else 0.12
            target = int(min(bud["lod%d" % lod], frac * summ["triangles"]) * 0.95)
            got = sl.decimate_to(lobjs, target)
            sl.uv_unwrap(lobjs, "box")
            path = af.out_path(req, "ship_lod%d.glb" % lod)
            af.export_glb(path, [lroot] + lobjs, animations=False, skins=False, export_tangents=False,
                          export_image_format="AUTO", export_draco_mesh_compression_enable=False)
            lod_tris["lod%d" % lod] = sl.glb_summary(path)["triangles"]
            outputs["lod%d" % lod] = path
            lod_files.append(path)
            for o in lobjs:
                o.hide_render = True
                o.hide_set(True)
            af.log("lod built", lod=lod, tris=got)

    # ---- checks
    checks = sl.budget_checks(summ, cls, lod_tris if P["lods"] else None)
    checks.append(af.check("collision_vertices", col_info["vertices"] <= 64, col_info["vertices"], 64))
    cov = col_info["coverage"] or 0
    checks.append(af.check("collision_hull_coverage", cov >= 0.9, cov, 0.9, severity="warning",
                           note="COL_SIMPLE volume / convex hull volume of the render mesh (not gate 11)"))
    size_y = summ["size_gltf"][2]
    checks.append(af.check("length_m", abs(size_y - P["length_m"]) <= 0.03 * P["length_m"], round(size_y, 3),
                           P["length_m"]))
    cx = summ["center_offset_gltf"][0]
    checks.append(af.check("origin_x_centered", abs(cx) <= 0.01, cx, 0.01))
    thr = [s_ for s_ in summ["sockets"] if s_["extras"].get("kind") == "thruster"]
    # glTF: nose is -Z, so thrusters must sit at +Z (aft)
    aft_ok = all(s_["position_gltf"][2] > 0 for s_ in thr) and len(thr) >= 1
    checks.append(af.check("thrusters_aft", aft_ok, len(thr), ">=1 at +Z glTF (aft)"))
    fr = P["thruster_frame"]
    checks.append(af.check("thruster_frame", all(sl.thruster_frame_ok(s_, fr) for s_ in thr),
                           [(s_["neg_z_gltf"], s_["y_gltf"]) for s_ in thr][:2], sl.THRUSTER_AXIS_TEXT[fr]))
    checks.append(sl.thruster_diameter_check(thr))
    checks.append(nav_checks(summ))
    lod_sil = sl.lod_silhouettes(out_glb, {k_: outputs[k_] for k_ in ("lod1", "lod2") if k_ in outputs})
    for key, v in lod_sil.items():
        checks.append(af.check("silhouette_iou_%s" % key, min(v["top"], v["side"]) >= 0.85, v, 0.85,
                               severity="warning", note="gate 10: top/side silhouette IoU vs LOD0"))
    tur = [s_ for s_ in summ["sockets"] if s_["extras"].get("kind") == "turret"]
    checks.append(af.check("turret_sockets", len(tur) == d.turrets_placed, len(tur), P["turret_count"],
                           severity="warning" if len(tur) < P["turret_count"] else "error"))
    if d.turrets_placed < P["turret_count"]:
        warnings.append("turret_count %d requested, layout %s offers %d hardpoints"
                        % (P["turret_count"], d.layout.get("family"), d.turrets_placed))
    glass = summ["alpha_modes"].get("AF_Glass")
    if glass and glass != "BLEND":
        warnings.append("AF_Glass exported with alphaMode %s (BLEND expected)" % glass)

    # ---- preview
    if P["preview"]:
        col.hide_render = True
        prev = []
        if P["preview_turrets"]:
            prev = preview_turrets(empties, P, mats)
        dims = summ["size_gltf"]
        longish = dims[2] > 3.6 * max(dims[0], dims[1] * 1.6)
        views = (("three_quarter_long", "rear_quarter_long") if longish else ("three_quarter", "rear_quarter")) + \
            ("side", "top", "front")
        outputs["preview"] = sl.render_grid(af.out_path(req, "preview.png"), objs + prev, tile=P["preview_size"],
                                             views=views)
        for o in prev:
            me = o.data
            bpy.data.objects.remove(o)
            bpy.data.meshes.remove(me)
    blend = sl.save_blend_clean(af.out_path(req, "ship.blend"))
    outputs["blend"] = blend

    # ---- manifest + definition
    definition = dict(P)
    definition["generator_version"] = sl.GEN_SHIP
    definition["layout_resolved"] = d.layout
    defn_path = sl.write_json(af.out_path(req, "ship.definition.json"), definition)
    manifest = build_manifest(P, summ, lod_tris, col, col_info, empties, outputs, req)
    outputs["manifest"] = sl.write_json(af.out_path(req, "ship.json"), manifest)
    outputs["definition"] = defn_path

    stats = {
        "asset_id": P["asset_id"], "class": P["class"], "budget_class": cls, "faction": P["faction"],
        "seed": P["seed"], "layout": d.layout, "length_m": P["length_m"], "dimensions_m": manifest["dimensions_m"],
        "tris": summ["triangles"], "tris_lod1": lod_tris.get("lod1"), "tris_lod2": lod_tris.get("lod2"),
        "lod_silhouette_iou": lod_sil,
        "surfaces": summ["surfaces_lod0"], "materials": summ["material_names_used"], "nodes": summ["nodes"],
        "glb_mb": summ["mb"], "sockets": [s_["game_socket"] for s_ in summ["sockets"]],
        "turrets_placed": d.turrets_placed, "engines": len(d.engines),
        "engines_main": sum(1 for e in d.engines if e["place"] == "main"), "engine_count": P["engine_count"],
        "palette_variant": P["palette_variant"], "thruster_frame": fr, "collision": col_info,
        "geometry_hash": summ["geometry_hash"], "parts": d.k.parts, "budget": bud,
        "registry_entry": {"id": P["asset_id"], "comment": "%s %s %s (AstraForge %s seed %d)" % (
            P["faction"], d.layout.get("family"), P["class"], sl.GEN_SHIP, P["seed"]),
            "path": "res://assets/models/ships/%s.glb" % P["asset_id"], "forward_axis": "-Z", "up_axis": "+Y"},
    }
    return {"outputs": outputs, "stats": stats, "checks": checks, "warnings": warnings}


def build_manifest(P, summ, lod_tris, col, col_info, empties, outputs, req, source="procedural",
                   generator=None, type_="ship"):
    sx, sy, sz = summ["size_gltf"]  # glTF: x width, y height, z length
    tri = {"lod0": int(summ["triangles"])}
    for k_ in ("lod1", "lod2"):
        if k_ in lod_tris:
            tri[k_] = int(lod_tris[k_])
    files = {"game_glb": os.path.basename(outputs["glb"]), "source_blend": "ship.blend"}
    if lod_tris:
        files["lod_glb"] = [os.path.basename(outputs[k_]) for k_ in ("lod1", "lod2") if k_ in outputs]
    if outputs.get("preview"):
        files["preview"] = [os.path.basename(outputs["preview"])]
    notes = []
    notes.append("gltf_validator and godot_import not run by the forge step (-1 = not run)")
    notes.append("gate 11 (COL volume 90-130 % of hull volume) not evaluated")
    notes.append("thruster socket frame: %s" % sl.THRUSTER_AXIS_TEXT[P.get("thruster_frame", "thrust")])
    return {
        "schema_version": 1,
        "asset_id": P["asset_id"],
        "type": type_,
        "class": P["budget_class"],
        "faction": P.get("faction", "neutral"),
        "tier": int(P.get("tier", 1)),
        "source": source,
        "generator_version": generator or sl.GEN_SHIP,
        "seed": int(P.get("seed", 0)),
        "definition_file": "ship.definition.json",
        "dimensions_m": {"length": round(sz, 3), "width": round(sx, 3), "height": round(sy, 3)},
        "axes": {"blender_forward": "+Y", "blender_up": "+Z", "godot_forward": "-Z", "godot_up": "+Y"},
        "triangles": tri,
        "surfaces_lod0": int(summ["surfaces_lod0"]),
        "materials": sorted(set(summ["material_names_used"])),
        "textures": [],
        "collision": {"simple": {"node": col.name, "vertices": int(col_info["vertices"])}, "complex": None},
        "sockets": sl.socket_manifest_rows(empties),
        "animations": [],
        "files": files,
        "glb_bytes": int(summ["bytes"]),
        "geometry_hash": summ["geometry_hash"],
        "validation": {"date": sl.today(),
                       "gltf_validator": {"errors": -1, "warnings": -1, "version": "not_run"},
                       "godot_import": "SKIPPED", "notes": "; ".join(notes)},
        "status": "prototype",
        "created": sl.today(),
        "notes": "AstraForge forge step %s; request out_dir %s" % (req.get("step", "ship_gen"), req["out_dir"]),
    }


def preview_turrets(empties, P, mats):
    """Preview-only turret copies on the turret sockets (not exported; turrets are separate assets)."""
    out = []
    for e in empties:
        if e.get("kind") != "turret":
            continue
        size = e.get("size_class", "M")
        D = float(e.get("mount_d_m", sl.TURRET_RING[size]))
        kits, _rig = sl.turret_geometry(size, 2 if size != "L" else 3, P["faction"], P["seed"], lod=1, ring=D)
        M = e.matrix_world.copy()
        for key, kt in kits.items():
            kt.transform_all(M)
            for o in sl.split_by_material(kt, mats, "pv_turret_%s_%s" % (e.name, key)):
                out.append(o)
            kt.bm.free()
    return out
