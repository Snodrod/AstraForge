"""item_kit — procedural, parametric low-poly-with-bevels items (contract §4, §6).

Request:
  params.type       one of TYPES (sword, greatsword, dagger, axe, battle_axe, mace, warhammer, spear,
                    halberd, staff, wand, bow, crossbow, shield_round, shield_kite, shield_tower,
                    buckler, torch, potion, book, blaster_pistol, blaster_rifle)
  params.style      medieval | dark | elven | scifi | random   (default per type)
  params.seed       int, drives proportions and "auto" sub-styles (default 0)
  params.ornament   0..3 (default: seed picks 1 or 2)
  params.palette    {"metal": "#rrggbb", "trim": {...}, ...} role overrides (see _items_lib.PALETTES)
  params.<dims>     per-type lengths / widths (see DEFAULTS below), guard/pommel/head styles
  params.origin     bbox (contract §4: X/Y centre of the bounding box at 0) | axis (main axis at 0)
  params.forward    -Y (default: item frame, edge / face / muzzle on -Y) | +Y (props with the nose at +Y,
                    contract §4): the item is turned 180 deg about Z, sockets keep their frames on the geometry
  params.preview    true (default) — preview.png (3 views); params.preview_size (px, default 384 / 320 in a
                    batch). params.size is a dimension (crossbow / blaster scale), not the preview size
  params.batch      list of param dicts (each merged over the top-level params) or a count of neighbouring
                    seeds -> one sub-folder per variant (name sanitised to [A-Za-z0-9_-], always inside
                    out_dir) + contact_sheet.png; params.sheet_cols (5), sheet_tile (256 px), sheet_title,
                    sheet_scale (common when all variants share a type: lengths compare | fit)
Outputs (single): item.glb, item.blend, item.json, preview.png. Batch: glb / blend = lists (one per
variant, the pipeline takes the first), preview = contact_sheet.png, json = batch.json.
Item frame: Z up, the item stands on z = 0, main axis +Z, edge / face -Y. socket_grip_main uses the
shared grip frame (+Z thumb side / along the shaft, -Y toward the edge, X = Y x Z).
"""
import copy
import hashlib
import json
import math
import os
import random
import re

import bmesh
import bpy
from mathutils import Matrix, Vector

import af_common as af
from steps import _items_lib as L

ONE_HANDED_BUDGET = 2500
TWO_HANDED_BUDGET = 4000

TYPES = ("sword", "greatsword", "dagger", "axe", "battle_axe", "mace", "warhammer", "spear",
         "halberd", "staff", "wand", "bow", "crossbow", "shield_round", "shield_kite",
         "shield_tower", "buckler", "torch", "potion", "book", "blaster_pistol", "blaster_rifle")
TWO_HANDED = {"greatsword", "battle_axe", "warhammer", "spear", "halberd", "staff", "bow",
              "crossbow", "shield_round", "shield_kite", "shield_tower", "buckler", "blaster_rifle"}
DEFAULT_STYLE = {"blaster_pistol": "scifi", "blaster_rifle": "scifi"}
# best viewing rotation (deg about Z) for the 3/4 contact-sheet tile
HERO_ROT = {"shield_round": -25, "shield_kite": -25, "shield_tower": -25, "buckler": -25}


class Ctx:
    def __init__(self, typ, params):
        if typ not in TYPES:
            raise ValueError("unknown item type %r; known: %s" % (typ, ", ".join(TYPES)))
        self.type = typ
        self.p = dict(params)
        self.seed = int(self.p.get("seed", 0))
        self.rng = random.Random(self.seed * 7919 + TYPES.index(typ) * 104729 + 17)
        style = self.p.get("style") or DEFAULT_STYLE.get(typ, "medieval")
        if style == "random":
            style = self.rng.choice(("medieval", "dark", "elven"))
        self.style = style
        orn = self.p.get("ornament")
        self.orn = int(L.clamp(int(orn), 0, 3)) if orn is not None else self.rng.choice((1, 2))
        pal = self.p.get("palette") or {}
        prefix = "mat"
        if pal:
            prefix += "_" + hashlib.md5(json.dumps(pal, sort_keys=True).encode()).hexdigest()[:6]
        self.mats = L.Materials(self.style, pal, prefix=prefix)
        self.parts = []
        self.sockets = []
        self.resolved = {}

    def g(self, name, default, jitter=0.0):
        """Param with seeded jitter when the caller did not set it explicitly."""
        if name in self.p:
            v = self.p[name]
        elif isinstance(default, (int, float)) and jitter:
            v = default * (1.0 + jitter * (self.rng.random() * 2 - 1))
        else:
            v = default
        self.resolved[name] = v
        return v

    def choice(self, name, options, auto_pool=None):
        v = self.p.get(name, "auto")
        if v == "auto" or v is None:
            v = self.rng.choice(auto_pool or options)
        if v not in options:
            raise ValueError("%s=%r not in %s" % (name, v, options))
        self.resolved[name] = v
        return v

    def m(self, role):
        return self.mats(role)

    def add(self, name, geo, role, **kw):
        ob = L.part("%s_%s" % (self.type, name), geo, self.m(role) if role else None, **kw)
        self.parts.append(ob)
        return ob

    def sock(self, name, loc, rot3=None, kind="grip", **extras):
        self.sockets.append((name, Vector(loc), rot3 if rot3 is not None else Matrix.Identity(3),
                             kind, extras))


RX90 = Matrix.Rotation(math.radians(90), 3, "X")  # +Z -> -Y (fx along a muzzle pointing -Y)


# =============================================================================== shared parts

def handle_geo(z0, z1, r0, r1, seg=12, wrap="spiral", rings=None, pitch=0.014, depth=0.0011,
               sx=1.0, sy=1.0):
    """Grip / haft section along Z with an optional leather wrap pattern."""
    n = rings or max(4, int(abs(z1 - z0) / 0.008))
    prof = [(L.lerp(r0, r1, k / n), L.lerp(z0, z1, k / n)) for k in range(n + 1)]

    def rf(r, a, z):
        if wrap == "spiral":
            return r + depth * (0.5 + 0.5 * math.cos(a * 2 - 2 * math.pi * (z - z0) / pitch * 2))
        if wrap == "rings":
            return r + depth * (0.5 + 0.5 * math.cos(2 * math.pi * (z - z0) / pitch))
        return r
    return L.lathe(prof, seg, rfunc=rf if wrap else None, sx=sx, sy=sy)


def ring_geo(z, r, h, seg=12, bevel=0.3):
    b = h * bevel
    return L.lathe([(r - b, z - h / 2), (r, z - h / 2 + b), (r, z + h / 2 - b), (r - b, z + h / 2)], seg)


def dome_geo(r, h, seg=8, rings=3):
    prof = [(0.0, 0.0)]
    prof = [(r, 0.0)] + [(r * math.cos(math.pi / 2 * k / rings), h * math.sin(math.pi / 2 * k / rings))
                         for k in range(1, rings)] + [(0.0, h)]
    return L.lathe([(0.0, -0.0005)] + prof, seg)


def oriented(geo, z_axis, loc):
    """Rotate geo (built around +Z at origin) so +Z -> z_axis, then move to loc."""
    q = Vector((0, 0, 1)).rotation_difference(Vector(z_axis).normalized())
    M = Matrix.Translation(Vector(loc)) @ q.to_matrix().to_4x4()
    return L.transform(geo[0], M), geo[1]


def rivet(c, name, loc, normal, r=0.006, h=0.003):
    return c.add(name, oriented(dome_geo(r, h, 6, 2), normal, loc), "trim", sharp_deg=0)


def gem_geo(r, h, seg=8):
    """Cabochon / faceted gem along +Z from z=0."""
    return L.lathe([(0.0, 0.0), (r, 0.0), (r * 0.96, h * 0.35), (r * 0.62, h * 0.85), (0.0, h)], seg)


# =============================================================================== blades

def blade_section(w, t, fuller_w, fuller_d, style, serr=0.0, edge_b=0.30, edge_t=0.22):
    """Cross-section (x = thickness axis, y = width axis, edge at -Y), CCW in XY. edge_t: edge
    thickness as a fraction of t (0.22 sharp; ~0.6 on an unsharpened ricasso)."""
    hw = w / 2
    te = t * edge_t
    if style == "scifi":
        pts = [(0.0, -hw), (t * 0.5, -hw * 0.55), (t * 0.5, hw * 0.55), (0.0, hw + serr),
               (-t * 0.5, hw * 0.55), (-t * 0.5, -hw * 0.55)]
        return pts
    eb = hw * edge_b
    if fuller_w > 1e-4 and fuller_d is not None:  # same point count with or without depth
        fw = fuller_w / 2
        right = [(te, -hw + eb), (t / 2, -fw - 0.0015), (t / 2 - fuller_d * 0.75, -fw * 0.55),
                 (t / 2 - fuller_d, 0.0), (t / 2 - fuller_d * 0.75, fw * 0.55), (t / 2, fw + 0.0015),
                 (te, hw - eb + serr)]
    else:
        right = [(te, -hw + eb), (t / 2, -hw * 0.25), (t / 2, hw * 0.25), (te, hw - eb + serr)]
    left = [(-x, y) for (x, y) in reversed(right)]
    return [(0.0, -hw)] + right + [(0.0, hw + serr)] + left


def build_blade(c, z0, length, w0, t, n, kind):
    style = c.style
    pools = {"medieval": ("straight", "tapered", "broad", "leaf", "falchion"), "dark": ("falchion", "broad"),
             "elven": ("leaf",), "scifi": ("straight",)}
    profile = c.choice("blade_profile", ("straight", "tapered", "broad", "leaf", "falchion"), pools[style])
    tip_start = {"straight": 0.82, "tapered": 0.9, "broad": 0.88, "leaf": 0.72, "falchion": 0.84}[profile]
    if style == "scifi":
        tip_start = 0.9
    fuller_default = kind != "dagger" and style in ("medieval", "elven", "dark") and \
        (style != "medieval" or c.rng.random() < 0.75)
    has_fuller = c.g("fuller", fuller_default)
    fuller_w = w0 * (c.rng.uniform(0.24, 0.36) if style != "elven" else 0.5)
    fuller_d = t * (0.28 if style != "elven" else 0.16)
    # seeded fuller run-out (fraction of the blade) and an unsharpened ricasso above the guard
    fuller_end = c.g("fuller_length", c.rng.uniform(0.42, 0.82) if style != "elven" else 0.62)
    ric_default = style in ("medieval", "dark") and kind != "dagger" and c.rng.random() < 0.5
    ric_len = c.g("ricasso_length", c.rng.uniform(0.035, 0.065) if c.g("ricasso", ric_default) else 0.0)
    ric_u = ric_len / max(1e-6, length)
    ric_w = 0.014 / max(1e-6, length)  # shoulder where the ricasso widens into the sharpened blade
    us = [i / n for i in range(n + 1)]
    if ric_u > 0:
        extra = [ric_u, ric_u + ric_w * 0.5, ric_u + ric_w]
        us = sorted([u for u in us if all(abs(u - e) > 0.35 / n for e in extra)] + extra)
    secs = []
    for u in us:
        rk = (1.0 - L.smoothstep(ric_u, ric_u + ric_w, u)) if ric_u > 0 else 0.0
        if profile == "leaf":
            w = w0 * (0.86 + 0.32 * math.sin(math.pi * min(1.0, u / 0.9) * 0.9))
        elif profile == "falchion":
            w = w0 * (1.0 + 0.16 * u)
        elif profile == "tapered":
            w = w0 * (1.0 - 0.55 * u)
        elif profile == "broad":
            w = w0 * 1.15 * (1.0 - 0.1 * u)
        else:
            w = w0 * (1.0 - 0.28 * u)
        if u > tip_start:
            k = (u - tip_start) / (1 - tip_start)
            if style == "scifi":
                w *= 1.0 - 0.75 * k
            else:
                w *= math.sqrt(max(0.0, 1 - k * k))
        w *= 1.0 - 0.26 * rk
        w = max(w, 0.0015)
        tt = max(t * (1 - 0.45 * u), 0.0016)
        fd = (fuller_d * (1 - L.smoothstep(fuller_end - 0.14, fuller_end + 0.04, u)) * L.smoothstep(0.0, 0.04, u)) \
            if has_fuller else None
        serr = 0.0
        if style == "dark" and 0.08 < u < 0.55:
            ph = (u - 0.08) / 0.47 * 7
            serr = 0.006 * (ph - math.floor(ph))
        fwu = min(fuller_w * (1 - 0.3 * u), w * 0.4)
        sec = blade_section(w, tt, fwu, fd, style, serr, edge_t=0.22 + 0.4 * rk)
        if profile == "falchion" and u > tip_start:  # clipped point: the back edge drops toward the tip
            k = (u - tip_start) / (1 - tip_start)
            sec = [(x, y - w * 0.35 * k if y > 0 else y) for (x, y) in sec]
        z = z0 + length * u
        secs.append([(x, y, z) for (x, y) in sec])
    tip_y = 0.0 if style != "scifi" else -w0 * 0.25
    geo = L.loft(secs, True, True, False, tip1=(0.0, tip_y, z0 + length * 1.012))
    c.add("blade", geo, "metal", sharp_deg=28)
    if style == "scifi":  # emissive core strip on both flats
        for sx in (1, -1):
            pts = [(-w0 * 0.12, z0 + 0.03), (w0 * 0.12, z0 + 0.03), (w0 * 0.08, z0 + length * 0.86),
                   (-w0 * 0.02, z0 + length * 0.9)]
            v, f = L.plate(pts, 0.0012, axes="YZ", centre=sx * (t * 0.5 + 0.0002))
            c.add("core_%d" % (sx > 0), (v, f), "emit", smooth=False)
    if c.orn >= 3 and has_fuller:
        role = "emit" if style in ("dark", "elven") else "trim"
        for sx in (1, -1):
            for k in range(5):
                if 0.08 + 0.085 * k + 0.02 / length > fuller_end - 0.14:  # runes stay in the full-depth fuller
                    break
                zc = z0 + length * (0.08 + 0.085 * k)
                v, f = L.box(0.0016, fuller_w * 0.38, 0.026, (sx * (t / 2 - fuller_d + 0.0003), 0.0, zc))
                c.add("rune_%d_%d" % (sx > 0, k), (v, f), role, smooth=False)
    return has_fuller


def build_guard(c, gz, gw, style_name, scale=1.0):
    s = scale
    # seeded curvature: quillons swept toward the blade, or drooping toward the hand
    curve = c.g("guard_curve", c.rng.choice((1, 1, -1)) * c.rng.uniform(0.015, 0.04))
    if style_name in ("cross", "curved", "spiked"):
        M = 18
        secs = []
        for i in range(M + 1):
            u = -1 + 2 * i / M
            au = abs(u)
            y = u * gw / 2
            if style_name == "curved":
                z = gz + curve * s * au ** 2.2
            elif style_name == "spiked":
                z = gz + 0.045 * s * au ** 3
            else:
                z = gz + 0.004 * s * au ** 2
            hx = (0.0105 - 0.0035 * au) * s
            hz = (0.0090 - 0.0025 * au) * s
            if au < 0.18:  # quillon block
                bump = 1.0 + 0.45 * (1 - au / 0.18)
                hx *= bump
                hz *= 1.0 + 0.6 * (1 - au / 0.18)
            if style_name == "spiked":
                hx *= max(0.05, 1 - L.smoothstep(0.7, 1.0, au) * 0.95)
                hz *= max(0.05, 1 - L.smoothstep(0.7, 1.0, au) * 0.95)
            elif au > 0.86:
                fl = 1 + 0.5 * L.smoothstep(0.86, 1.0, au)
                hx *= fl
                hz *= fl
            sec = L.rounded_rect(hx * 2, hz * 2, min(hx, hz) * 0.45, 1)
            secs.append([(px, y, z + pz) for (px, pz) in sec])
        c.add("guard", L.loft(secs), "trim", sharp_deg=35)
        if c.orn >= 1 and style_name != "spiked":
            for sgn in (-1, 1):
                zz = gz + (curve * s if style_name == "curved" else 0.004 * s)
                v, f = L.uv_sphere(0.0085 * s, 8, 6, centre=(0, sgn * (gw / 2 + 0.004 * s), zz))
                c.add("finial_%d" % (sgn > 0), (v, f), "trim")
        if c.orn >= 3:
            for sx in (1, -1):
                g = oriented(gem_geo(0.0065 * s, 0.004 * s, 8), (sx, 0, 0), (sx * 0.0145 * s, 0, gz))
                c.add("guard_gem_%d" % (sx > 0), g, "gem", smooth=False)
    elif style_name == "swept":
        for sgn in (-1, 1):
            path = [(0, 0, gz), (0, sgn * gw * 0.18, gz + 0.002), (0, sgn * gw * 0.36, gz + 0.012 * s),
                    (0, sgn * gw * 0.47, gz + 0.034 * s), (0, sgn * gw * 0.5, gz + 0.06 * s)]
            path = L.catmull(path, 4)
            rad = lambda u: (0.0045 * s * (1 - 0.85 * u), 0.011 * s * (1 - 0.8 * u) + 0.0015)
            c.add("wing_%d" % (sgn > 0), L.sweep(path, rad, 8, up_hint=(1, 0, 0), tip1=True), "trim",
                  sharp_deg=50)
        v, f = L.lathe([(0.0, gz - 0.012 * s), (0.013 * s, gz - 0.008 * s), (0.015 * s, gz + 0.004 * s),
                        (0.0, gz + 0.012 * s)], 10, sy=1.5)
        c.add("guard_core", (v, f), "trim")
        if c.orn >= 2:
            for sx in (1, -1):
                g = oriented(gem_geo(0.006 * s, 0.004 * s, 8), (sx, 0, 0), (sx * 0.0125 * s, 0, gz))
                c.add("guard_gem_%d" % (sx > 0), g, "gem", smooth=False)
    elif style_name == "disc":
        v, f = L.lathe([(0.0, gz - 0.008 * s), (gw * 0.36, gz - 0.008 * s), (gw * 0.36, gz + 0.008 * s),
                        (0.0, gz + 0.008 * s)], 6, sy=1.0, sx=0.55, phase=math.pi / 2)
        c.add("guard", (v, f), "metal_dark", bevel=(0.0018, 1, 30), sharp_deg=30)
        if c.orn >= 1:
            c.add("guard_glow", ring_geo(gz + 0.009 * s, 0.017 * s, 0.003, 12), "emit")
    return gz + 0.01 * s


def build_pommel(c, ztop, r, style_name):
    """Pommel whose top is at ztop; returns its bottom z."""
    if style_name == "disc":
        t = r * 0.75
        prof = [(0.0, -t / 2), (r * 0.72, -t / 2), (r, -t * 0.2), (r, t * 0.2), (r * 0.72, t / 2), (0.0, t / 2)]
        v, f = L.lathe(prof, 16)
        v = L.transform(v, Matrix.Translation((0, 0, ztop - r)) @ L.rot("Y", 90))
        c.add("pommel", (v, f), "trim", sharp_deg=40)
        if c.orn >= 2:
            for sx in (1, -1):
                g = oriented(gem_geo(r * 0.38, r * 0.22, 8), (sx, 0, 0), (sx * t / 2, 0, ztop - r))
                c.add("pommel_gem_%d" % (sx > 0), g, "gem", smooth=False)
        c.add("peen", L.lathe([(0.0, ztop - 2 * r - 0.006), (0.006, ztop - 2 * r - 0.006),
                               (0.008, ztop - 2 * r + 0.004), (0.0, ztop - 2 * r + 0.004)], 8), "trim")
        return ztop - 2 * r - 0.006
    if style_name == "ball":
        v, f = L.uv_sphere(r, 12, 8, sz=1.05, centre=(0, 0, ztop - r))
        c.add("pommel", (v, f), "trim")
        return ztop - 2.1 * r
    if style_name == "scent":
        h = r * 2.4
        prof = [(0.0, 0.0), (r * 0.35, 0.0), (r * 0.95, h * 0.28), (r * 0.8, h * 0.62), (r * 0.42, h * 0.86),
                (r * 0.5, h), (0.0, h)]
        v, f = L.lathe(prof, 8)
        c.add("pommel", (L.translate(v, (0, 0, ztop - h)), f), "trim", sharp_deg=30)
        return ztop - h
    if style_name == "teardrop":
        h = r * 2.6
        prof = [(0.0, 0.0), (r * 0.45, h * 0.1), (r * 0.85, h * 0.45), (r * 0.6, h * 0.82), (r * 0.42, h), (0.0, h)]
        v, f = L.lathe(prof, 12)
        c.add("pommel", (L.translate(v, (0, 0, ztop - h)), f), "trim")
        if c.orn >= 2:
            v, f = L.uv_sphere(r * 0.38, 8, 6, centre=(0, 0, ztop - h - r * 0.25))
            c.add("pommel_gem", (v, f), "gem")
        return ztop - h - (r * 0.6 if c.orn >= 2 else 0)
    if style_name == "spike":
        h = r * 3.2
        prof = [(0.0, 0.0), (r * 0.5, h * 0.45), (r, h * 0.68), (r * 0.9, h * 0.85), (r * 0.55, h), (0.0, h)]
        v, f = L.lathe(prof, 6)
        c.add("pommel", (L.translate(v, (0, 0, ztop - h)), f), "trim", sharp_deg=25)
        return ztop - h
    if style_name == "ring":
        h = r * 1.4
        v, f = L.lathe([(0.0, 0.0), (r * 0.85, 0.0), (r, h * 0.25), (r, h * 0.75), (r * 0.8, h), (0.0, h)], 6)
        c.add("pommel", (L.translate(v, (0, 0, ztop - h)), f), "metal_dark", bevel=(0.0012, 1, 30), sharp_deg=30)
        c.add("pommel_glow", ring_geo(ztop - h * 0.5, r * 1.02, h * 0.22, 12), "emit")
        return ztop - h
    return ztop


def build_sword(c):
    kind = c.type
    D = {"sword": dict(blade_len=0.76, blade_w=0.046, blade_t=0.0068, grip_len=0.105, grip_r=0.0145,
                       guard_w=0.19, pommel_r=0.024, n=22),
         "greatsword": dict(blade_len=1.06, blade_w=0.056, blade_t=0.0078, grip_len=0.27, grip_r=0.0158,
                            guard_w=0.30, pommel_r=0.03, n=26),
         "dagger": dict(blade_len=0.21, blade_w=0.031, blade_t=0.0058, grip_len=0.092, grip_r=0.0128,
                        guard_w=0.085, pommel_r=0.017, n=14)}[kind]
    # seeded ranges wide enough that neighbouring seeds read as different swords on a contact sheet
    blade_len = c.g("blade_length", D["blade_len"], 0.20)
    blade_w = c.g("blade_width", D["blade_w"], 0.30)
    blade_t = c.g("blade_thickness", D["blade_t"])
    grip_len = c.g("grip_length", D["grip_len"], 0.20)
    grip_r = c.g("grip_radius", D["grip_r"])
    guard_w = c.g("guard_width", D["guard_w"], 0.35)
    pommel_r = c.g("pommel_radius", D["pommel_r"], 0.08)
    auto_guard = {"medieval": ("cross", "curved"), "dark": ("spiked",), "elven": ("swept",),
                  "scifi": ("disc",)}[c.style]
    auto_pommel = {"medieval": ("disc", "scent", "ball", "teardrop"), "dark": ("spike",), "elven": ("teardrop",),
                   "scifi": ("ring",)}[c.style]
    guard = c.choice("guard", ("cross", "curved", "spiked", "swept", "disc", "none"), auto_guard)
    pommel = c.choice("pommel", ("disc", "ball", "scent", "teardrop", "spike", "ring", "none"), auto_pommel)
    gs = guard_w / 0.19 if kind != "dagger" else 0.7
    # stack from the pommel top: build at z=0 then everything is moved to the ground by place_on_ground
    zg0 = 0.0
    zg1 = zg0 + grip_len
    wrap = "rings" if c.style == "scifi" else "spiral"
    c.add("grip", handle_geo(zg0, zg1, grip_r * 0.93, grip_r, 12, wrap, sy=1.12), "grip", sharp_deg=0)
    if c.orn >= 1:
        c.add("ferrule_lo", ring_geo(zg0 + 0.004, grip_r * 1.12, 0.009, 12), "trim")
        c.add("ferrule_hi", ring_geo(zg1 - 0.004, grip_r * 1.12, 0.009, 12), "trim")
    gz = zg1 + 0.009 * gs
    if guard != "none":
        build_guard(c, gz, guard_w, guard, scale=gs)
    zb0 = gz + 0.004
    if c.orn >= 2 and c.style == "medieval":
        c.add("ricasso_collar", ring_geo(zb0 + 0.006, blade_w * 0.33, 0.012, 8), "trim", sharp_deg=30)
    build_blade(c, zb0, blade_len, blade_w, blade_t, D["n"], kind)
    if pommel != "none":
        build_pommel(c, zg0 + 0.002, pommel_r, pommel)
    if kind == "greatsword":
        main = zg1 - 0.055
        c.sock("socket_grip_main", (0, 0, main))
        c.sock("socket_grip_off", (0, 0, max(zg0 + 0.05, main - 0.11)))
    else:
        c.sock("socket_grip_main", (0, 0, (zg0 + zg1) / 2))


# =============================================================================== hafted weapons

def build_haft(c, length, r, grips, wood=True, butt=True, seg=10, oval=1.22):
    """Haft along +Z from z=0, grip wraps at the given (z0, z1) ranges."""
    n = max(8, int(length / 0.06))
    prof = [(0.0, 0.0)]
    for k in range(n + 1):
        u = k / n
        z = length * u
        rr = r * (1.06 - 0.12 * u)
        if u < 0.06:  # swell at the butt
            rr *= 1.0 + 0.22 * (1 - u / 0.06)
        prof.append((rr, z))
    prof.append((0.0, length))
    c.add("haft", L.lathe(prof, seg, sy=oval), "wood" if wood else "metal_dark", sharp_deg=50)
    for i, (z0, z1) in enumerate(grips):
        c.add("wrap_%d" % i, handle_geo(z0, z1, r * 1.1, r * 1.08, seg + 2, "spiral", sy=oval), "grip",
              sharp_deg=0)
    if butt:
        c.add("butt", L.lathe([(0.0, -0.012), (r * 1.05, -0.012), (r * 1.3, 0.004), (r * 1.22, 0.022),
                               (0.0, 0.022)], seg, sy=oval), "trim")


def axe_outline(style, scale, double=False):
    s = scale
    if double:
        pts = [(-0.022, 0.06), (-0.05, 0.05), (-0.11, 0.1), (-0.16, 0.15), (-0.2, 0.17),
               (-0.215, 0.08), (-0.22, 0.0), (-0.215, -0.08), (-0.2, -0.17), (-0.16, -0.15),
               (-0.11, -0.1), (-0.05, -0.05), (-0.022, -0.06)]
    elif style == "elven":
        pts = [(-0.022, 0.035), (-0.06, 0.05), (-0.11, 0.1), (-0.15, 0.085), (-0.165, 0.02),
               (-0.155, -0.06), (-0.12, -0.11), (-0.08, -0.08), (-0.05, -0.04), (-0.022, -0.035)]
    elif style == "scifi":
        pts = [(-0.022, 0.036), (-0.07, 0.05), (-0.14, 0.07), (-0.15, -0.02), (-0.125, -0.1),
               (-0.06, -0.06), (-0.022, -0.036)]
    else:  # bearded
        pts = [(-0.022, 0.036), (-0.05, 0.042), (-0.09, 0.055), (-0.128, 0.068), (-0.142, 0.03),
               (-0.145, -0.02), (-0.135, -0.07), (-0.112, -0.112), (-0.075, -0.085),
               (-0.045, -0.05), (-0.022, -0.036)]
    if style != "scifi":
        pts = L.catmull(pts, 3)
    return [(u * s, v * s) for (u, v) in [(p[0], p[1]) for p in pts]]


def build_axe_head(c, zc, haft_r, double=False, scale=1.0):
    s = scale
    out = axe_outline(c.style, s, double)
    # eye side of the outline: from the lower end back up to the first point (a simple, non-crossing loop)
    eye = [(0.026 * s, -0.038 * s * (1.5 if double else 1)), (0.026 * s, 0.038 * s * (1.5 if double else 1))]
    edge_u = min(p[0] for p in out)
    thick = lambda u, v: L.lerp(0.034 * s, 0.003, L.smoothstep(-0.03 * s, edge_u, u))
    blades = [out + eye]
    if double:
        blades.append([(-u, v) for (u, v) in reversed(out)] + [(-p[0], p[1]) for p in reversed(eye)])
    body_role = "metal_dark" if c.style in ("medieval", "dark") else "metal"
    for i, poly in enumerate(blades):
        # tapered cheeks: interior vertices (fill) keep the smooth-shaded taper free of fan streaks
        v, f = L.plate(poly, lambda u, vv: thick(-abs(u) if double else u, vv), axes="YZ", fill=0.03 * s)
        v = L.translate(v, (0, 0, zc))
        c.add("head_%d" % i, (v, f), body_role, bevel=(0.0014, 1, 30), sharp_deg=30)
        # bright cutting edge strip
        edge = [p for p in poly if (abs(p[0]) if double else -p[0]) > abs(edge_u) * 0.72]
        if len(edge) >= 3:
            inner = []
            for (u, vv) in reversed(edge):
                k = 0.016 * s
                inner.append((u + (k if u < 0 else -k), vv * 0.93))
            strip = edge + inner
            v, f = L.plate(strip, lambda u, vv: thick(-abs(u), vv) + 0.0009, axes="YZ")  # narrow band: plain caps
            c.add("edge_%d" % i, (L.translate(v, (0, 0, zc)), f), "metal", sharp_deg=30)
    # eye collar
    h = 0.08 * s * (1.4 if double else 1)
    c.add("eye", L.lathe([(haft_r * 1.25, zc - h / 2), (haft_r * 1.45, zc - h / 2 + 0.006),
                          (haft_r * 1.45, zc + h / 2 - 0.006), (haft_r * 1.25, zc + h / 2)], 10, sy=1.3),
          body_role, sharp_deg=40)
    if c.orn >= 1:
        for sx in (1, -1):
            for dz in (-0.022, 0.022):
                rivet(c, "rivet_%d_%d" % (sx > 0, dz > 0), (sx * 0.0175 * s, 0.012 * s, zc + dz * s), (sx, 0, 0))
    if not double and c.style in ("dark", "medieval") and c.orn >= 2:
        # back spike (poll): forged lozenge section, thick at the eye and tapering to the point
        secs = []
        lozenge = [(1.0, 0.0), (0.45, 0.8), (-0.45, 0.8), (-1.0, 0.0), (-0.45, -0.8), (0.45, -0.8)]
        for k in range(7):
            u = k / 6
            hx = 0.0125 * s * (1 - 0.78 * u) + 0.0012
            hz = 0.021 * s * (1 - 0.8 * u) + 0.0012
            y = 0.024 * s + 0.072 * s * u
            zz = zc - 0.014 * s * u ** 2
            secs.append([(qx * hx, y, zz + qz * hz) for (qx, qz) in lozenge])
        c.add("poll_spike", L.loft(secs, tip1=(0, 0.024 * s + 0.088 * s, zc - 0.019 * s)), body_role,
              sharp_deg=35)
    return zc + h / 2


def build_axe(c):
    two = c.type == "battle_axe"
    length = c.g("haft_length", 1.15 if two else 0.68, 0.06)
    r = c.g("haft_radius", 0.0185 if two else 0.0155)
    head = c.g("head_size", 1.1 if two else 1.0, 0.08)
    grips = [(0.03, 0.2)] if not two else [(0.04, 0.2), (0.38, 0.56)]
    build_haft(c, length, r, grips, butt=c.orn >= 1)
    zc = length - 0.085 * head * (1.4 if two else 1)
    top = build_axe_head(c, zc, r, double=two, scale=head)
    if two or c.style == "dark":
        secs = []
        for k in range(4):
            u = k / 3
            w = r * 1.2 * (1 - u * 0.85)
            secs.append([(x * w, y * w * 1.2, top + 0.05 * u) for (x, y) in ((1, 0), (0, 1), (-1, 0), (0, -1))])
        c.add("top_spike", L.loft(secs, tip1=(0, 0, top + 0.09)), "metal", smooth=False)
    if c.orn >= 2:
        for sy in (1, -1):  # langets
            v, f = L.box(0.006, 0.003, 0.14, (0, sy * r * 1.18, zc - 0.12 * head))
            c.add("langet_%d" % (sy > 0), (v, f), "metal_dark", bevel=(0.001, 1, 30))
    if two:
        c.sock("socket_grip_main", (0, 0, 0.47))
        c.sock("socket_grip_off", (0, 0, 0.12))
    else:
        c.sock("socket_grip_main", (0, 0, 0.115))


def build_mace(c):
    length = c.g("haft_length", 0.6, 0.06)
    r = c.g("haft_radius", 0.0145)
    head_r = c.g("head_radius", 0.062, 0.08)
    build_haft(c, length, r, [(0.03, 0.17)], wood=c.style in ("medieval", "elven"), oval=1.0)
    zc = length - head_r * 0.9
    if c.style == "dark":  # morningstar
        v, f = L.ico_sphere(head_r * 0.62, 2, centre=(0, 0, zc))
        c.add("head", (v, f), "metal_dark", smooth=False)
        dirs, _ = L.ico_sphere(1.0, 1)
        for i, d in enumerate(dirs):
            d = Vector(d).normalized()
            if d.z < -0.8:
                continue
            g = oriented(L.lathe([(0.0, 0.0), (head_r * 0.16, 0.0), (0.0, head_r * 0.62)], 5), d,
                         Vector((0, 0, zc)) + d * head_r * 0.5)
            c.add("spike_%02d" % i, g, "metal", smooth=False)
    else:
        n_fl = c.g("flanges", 6 if c.orn < 2 else 8)
        c.add("core", L.lathe([(0.0, zc - head_r * 0.95), (r * 1.3, zc - head_r * 0.95),
                               (head_r * 0.45, zc - head_r * 0.25), (head_r * 0.45, zc + head_r * 0.35),
                               (r * 1.4, zc + head_r * 0.9), (0.0, zc + head_r * 0.95)], 12),
              "metal_dark" if c.style != "scifi" else "metal")
        for k in range(int(n_fl)):
            a = -math.pi / 2 + 2 * math.pi * k / n_fl
            if c.style == "elven":
                pts = [(0.02, -0.8), (0.55, -0.55), (1.0, 0.05), (0.75, 0.6), (0.02, 0.95)]
            elif c.style == "scifi":
                pts = [(0.02, -0.8), (0.8, -0.4), (0.8, 0.5), (0.02, 0.85)]
            else:
                pts = [(0.02, -0.85), (0.5, -0.62), (0.95, -0.05), (1.0, 0.25), (0.62, 0.62), (0.02, 0.9)]
            pts = [(u * head_r, v * head_r) for (u, v) in pts]
            v, f = L.plate(pts, lambda u, vv: L.lerp(0.009, 0.004, u / head_r), axes="XZ")
            M = Matrix.Translation((0, 0, zc)) @ Matrix.Rotation(a, 4, "Z")
            c.add("flange_%d" % k, (L.transform(v, M), f), "metal", bevel=(0.0012, 1, 30), sharp_deg=30)
        if c.style == "scifi":
            c.add("glow", ring_geo(zc, head_r * 0.47, 0.01, 12), "emit")
    if c.orn >= 1:
        c.add("finial", L.lathe([(0.0, length - 0.01), (r, length - 0.01), (0.0, length + 0.035)], 8),
              "metal", smooth=False)
        c.add("collar", ring_geo(zc - head_r * 1.05, r * 1.35, 0.016, 10), "trim")
    c.sock("socket_grip_main", (0, 0, 0.1))


def build_warhammer(c):
    length = c.g("haft_length", 1.05, 0.06)
    r = c.g("haft_radius", 0.0175)
    hs = c.g("head_size", 1.0, 0.08)
    build_haft(c, length, r, [(0.04, 0.2), (0.38, 0.56)], wood=c.style != "scifi")
    zc = length - 0.06 * hs
    c.add("block", L.box(0.052 * hs, 0.075 * hs, 0.085 * hs, (0, 0, zc)), "metal_dark",
          bevel=(0.003, 2, 30), sharp_deg=30)
    secs = []
    for k in range(4):
        u = k / 3
        w = 0.026 * hs * (1 + 0.25 * u)
        y = -0.037 * hs - 0.075 * hs * u
        secs.append([(x * w, y, zc + zz * w) for (x, zz) in ((1, 1), (-1, 1), (-1, -1), (1, -1))])
    c.add("face", L.loft(secs), "metal", bevel=(0.003, 2, 30), sharp_deg=30)
    if c.orn >= 2:  # waffle face
        for i in (-1, 1):
            for j in (-1, 1):
                g = oriented(L.lathe([(0.0, 0.0), (0.009 * hs, 0.0), (0.0, 0.008 * hs)], 4, phase=math.pi / 4),
                             (0, -1, 0), (i * 0.014 * hs, -0.112 * hs - 0.0, zc + j * 0.014 * hs))
                c.add("waffle_%d_%d" % (i > 0, j > 0), g, "metal", smooth=False)
    secs = []
    for k in range(6):
        u = k / 5
        w = 0.02 * hs * (1 - 0.8 * u) + 0.002
        y = 0.037 * hs + 0.15 * hs * u
        zz = zc - 0.05 * hs * u ** 2
        secs.append([(x * w, y, zz + q * w) for (x, q) in ((1, 1), (-1, 1), (-1, -1), (1, -1))])
    c.add("pick", L.loft(secs, tip1=(0, 0.037 * hs + 0.17 * hs, zc - 0.065 * hs)), "metal", smooth=False)
    secs = []
    for k in range(3):
        u = k / 2
        w = 0.016 * hs * (1 - 0.7 * u)
        secs.append([(x * w, y * w, zc + 0.042 * hs + 0.05 * hs * u) for (x, y) in ((1, 0), (0, 1), (-1, 0), (0, -1))])
    c.add("top_spike", L.loft(secs, tip1=(0, 0, zc + 0.042 * hs + 0.09 * hs)), "metal", smooth=False)
    if c.orn >= 1:
        for sx in (1, -1):
            v, f = L.box(0.003, 0.012, 0.22, (sx * r * 1.12, 0, zc - 0.15))
            c.add("langet_%d" % (sx > 0), (v, f), "metal_dark", bevel=(0.001, 1, 30))
            for k in range(3):
                rivet(c, "rivet_%d_%d" % (sx > 0, k), (sx * r * 1.22, 0, zc - 0.08 - 0.06 * k), (sx, 0, 0), 0.004, 0.002)
    c.sock("socket_grip_main", (0, 0, 0.47))
    c.sock("socket_grip_off", (0, 0, 0.12))


def spear_head(c, z0, length, w, t, n=12, role="metal"):
    secs = []
    for i in range(n + 1):
        u = i / n
        ww = w * (math.sin(math.pi * min(1.0, u / 0.85) * 0.85 + 0.25) ** 1.2) * (1 - u ** 3) + 0.002
        if u < 0.08:
            ww *= 0.35 + 0.65 * u / 0.08
        tt = t * (1 - 0.6 * u) + 0.0015
        hw = ww / 2
        sec = [(0.0, -hw), (tt * 0.25, -hw * 0.45), (tt / 2, 0.0), (tt * 0.25, hw * 0.45), (0.0, hw),
               (-tt * 0.25, hw * 0.45), (-tt / 2, 0.0), (-tt * 0.25, -hw * 0.45)]
        secs.append([(x, y, z0 + length * u) for (x, y) in sec])
    return c.add("spearhead", L.loft(secs, tip1=(0, 0, z0 + length * 1.02)), role, sharp_deg=30)


def build_spear(c):
    length = c.g("shaft_length", 2.0, 0.05)
    r = c.g("shaft_radius", 0.0152)
    head_len = c.g("head_length", 0.28, 0.1)
    head_w = c.g("head_width", 0.055, 0.12)
    main = length * 0.45
    off = length * 0.2
    shaft_top = length - head_len - 0.07
    c.add("shaft", L.lathe([(0.0, 0.0), (r * 0.95, 0.0)] + [(r * (1 - 0.14 * k / 8), shaft_top * k / 8) for k in range(1, 9)]
                           + [(0.0, shaft_top)], 8), "wood", sharp_deg=50)
    for i, zc in enumerate((main, off)):
        c.add("wrap_%d" % i, handle_geo(zc - 0.06, zc + 0.06, r * 1.12, r * 1.12, 10, "spiral"), "grip", sharp_deg=0)
    sz = shaft_top - 0.1
    c.add("socket", L.lathe([(0.0, sz), (r * 1.18, sz), (r * 1.2, sz + 0.012), (r * 0.95, shaft_top + 0.07),
                             (0.0, shaft_top + 0.075)], 8), "metal_dark", sharp_deg=35)
    for k in range(c.orn):
        c.add("socket_ring_%d" % k, ring_geo(sz + 0.02 + 0.03 * k, r * 1.25, 0.006, 8), "trim")
    spear_head(c, shaft_top + 0.06, head_len, head_w, 0.011)
    c.add("butt_spike", L.lathe([(0.0, -0.075), (r * 0.5, -0.06), (r * 1.08, 0.0), (r * 1.1, 0.03), (0.0, 0.03)], 8),
          "metal_dark", smooth=False)
    if c.orn >= 2:  # tassel
        c.add("tassel", L.lathe([(0.0, sz - 0.12), (r * 2.2, sz - 0.1), (r * 1.6, sz - 0.02), (r * 1.15, sz),
                                 (0.0, sz + 0.002)], 10, rfunc=lambda rr, a, z: rr * (1 + 0.15 * math.cos(a * 5))),
              "accent")
    if c.orn >= 3:  # wings (lugs)
        for sy in (1, -1):
            g = L.lathe([(0.0, 0.0), (0.006, 0.0), (0.0, 0.05)], 4)
            c.add("lug_%d" % (sy > 0), oriented(g, (0, sy, 0.25), (0, sy * r, sz + 0.03)), "metal_dark", smooth=False)
    c.sock("socket_grip_main", (0, 0, main))
    c.sock("socket_grip_off", (0, 0, off))
    c.sock("socket_fx_tip", (0, 0, shaft_top + 0.06 + head_len), kind="fx")


def build_halberd(c):
    length = c.g("shaft_length", 1.9, 0.05)
    r = c.g("shaft_radius", 0.0168)
    hs = c.g("head_size", 1.0, 0.08)
    top = length - 0.26 * hs
    c.add("shaft", L.lathe([(0.0, 0.0), (r, 0.0), (r * 0.94, top), (0.0, top)], 8), "wood", sharp_deg=30)
    main, off = length * 0.42, length * 0.12
    for i, zc in enumerate((main, off)):
        c.add("wrap_%d" % i, handle_geo(zc - 0.065, zc + 0.065, r * 1.12, r * 1.12, 10, "spiral"), "grip", sharp_deg=0)
    zc = top - 0.12 * hs
    c.add("collar", L.lathe([(0.0, zc - 0.13 * hs), (r * 1.25, zc - 0.13 * hs), (r * 1.25, top + 0.02),
                             (0.0, top + 0.02)], 8), "metal_dark", sharp_deg=30)
    spear_head(c, top + 0.0, 0.26 * hs, 0.034 * hs, 0.012)
    blade = [(-0.015, 0.075), (-0.06, 0.06), (-0.12, 0.085), (-0.16, 0.1), (-0.175, 0.02), (-0.17, -0.07),
             (-0.15, -0.13), (-0.1, -0.08), (-0.05, -0.065), (-0.015, -0.07)]
    blade = [(u * hs, v * hs) for (u, v) in L.catmull(blade, 3)]
    th = lambda u, v: L.lerp(0.012, 0.003, L.smoothstep(-0.02 * hs, -0.17 * hs, u))
    v, f = L.plate(blade, th, axes="YZ", fill=0.024 * hs)
    c.add("axe_blade", (L.translate(v, (0, 0, zc)), f), "metal", bevel=(0.0012, 1, 30), sharp_deg=30)
    hook = [(0.015, 0.035), (0.06, 0.03), (0.11, 0.0), (0.15, -0.055), (0.1, -0.01), (0.05, -0.025), (0.015, -0.035)]
    hook = [(u * hs, vv * hs) for (u, vv) in L.catmull(hook, 2)]
    v, f = L.plate(hook, lambda u, vv: L.lerp(0.012, 0.004, u / (0.15 * hs)), axes="YZ")
    c.add("hook", (L.translate(v, (0, 0, zc)), f), "metal", bevel=(0.0012, 1, 30), sharp_deg=30)
    if c.orn >= 1:
        for sx in (1, -1):
            v, f = L.box(0.003, 0.012, 0.32, (sx * r * 1.1, 0, zc - 0.28 * hs))
            c.add("langet_%d" % (sx > 0), (v, f), "metal_dark", bevel=(0.001, 1, 30))
        for sx in (1, -1):
            for dz in (-0.03, 0.03):
                rivet(c, "rivet_%d_%d" % (sx > 0, dz > 0), (sx * 0.0072, -0.03 * hs, zc + dz * hs), (sx, 0, 0), 0.005, 0.002)
    if c.orn >= 2:
        c.add("tassel", L.lathe([(0.0, zc - 0.25), (r * 2.4, zc - 0.22), (r * 1.6, zc - 0.15), (r * 1.2, zc - 0.13),
                                 (0.0, zc - 0.128)], 10, rfunc=lambda rr, a, z: rr * (1 + 0.15 * math.cos(a * 5))),
              "accent")
    c.sock("socket_grip_main", (0, 0, main))
    c.sock("socket_grip_off", (0, 0, off))
    c.sock("socket_fx_tip", (0, 0, top + 0.26 * hs), kind="fx")


# =============================================================================== magic

def shaft_path(c, length, wobble, n=24):
    rng = random.Random(c.seed * 31 + 5)
    ph = [rng.random() * 6.28 for _ in range(4)]
    pts = []
    for k in range(n + 1):
        u = k / n
        x = wobble * (math.sin(u * 7.0 + ph[0]) * 0.6 + math.sin(u * 13.0 + ph[1]) * 0.4) * math.sin(math.pi * u)
        y = wobble * (math.sin(u * 5.0 + ph[2]) * 0.6 + math.sin(u * 11.0 + ph[3]) * 0.4) * math.sin(math.pi * u)
        pts.append((x, y, length * u))
    return pts


def skull_parts(c, centre, size, eye_role):
    """Low-poly human skull facing -Y (unit s ~ half the cranium width): cranium, flat face with
    brow ridge, deep orbits, cheekbones, nasal opening, upper teeth and a separate mandible."""
    cx, cy, cz = centre
    s = size

    def W(p):
        return (cx + p[0] * s, cy + p[1] * s, cz + p[2] * s)
    # cranium: ellipsoid, flattened base, slightly wider at the back
    v, f = L.uv_sphere(1.0, 16, 10)
    cr = []
    for (x, y, z) in v:
        x2, y2, z2 = x * (0.95 + 0.06 * max(0.0, y)), y * 1.18 + 0.18, z * 1.0 + 0.12
        if z2 < -0.42:
            z2 = -0.42 + (z2 + 0.42) * 0.35
        cr.append(W((x2, y2, z2)))
    cran = c.add("skull", (cr, f), "bone", sharp_deg=0)
    # face: sections from the forehead to the maxilla; brow ridge, recessed orbits and cheekbones
    # are part of the section profile (no stuck-on tubes)
    levels = [(0.2, 0.84, -0.9, 0.0), (0.07, 0.88, -1.0, 0.0), (-0.07, 0.89, -0.97, 0.22),
              (-0.25, 0.88, -0.97, 0.17), (-0.4, 0.8, -1.0, 0.0), (-0.56, 0.62, -0.99, 0.0),
              (-0.82, 0.48, -0.88, 0.0)]
    secs = []
    for (z, hw, yf, rec) in levels:
        ring = []
        for i in range(17):
            th = math.pi * i / 16
            x = hw * math.cos(th)
            y = 0.3 + (yf - 0.3) * math.sin(th) ** 0.5
            if rec and math.sin(th) > 0.3:
                y += rec * math.exp(-((abs(x) - 0.38) / 0.16) ** 2)
            ring.append(W((x, y, z)))
        secs.append(ring)
    c.add("face", L.loft(secs), "bone", sharp_deg=60)
    # orbits (dark or glowing) sit inside the recesses
    for i, sx in enumerate((-1, 1)):
        v2, f2 = L.uv_sphere(1.0, 10, 6)
        v2 = [W((sx * 0.38 + x * 0.2, -0.76 + y * 0.12, -0.16 + z * 0.16)) for (x, y, z) in v2]
        c.add("orbit_%d" % i, (v2, f2), eye_role)
    # nasal opening
    v3, f3 = L.plate([(0.0, -0.3), (0.12, -0.48), (0.05, -0.56), (-0.05, -0.56), (-0.12, -0.48)], 0.04, axes="XZ")
    c.add("nose", ([W((x, y - 1.0, z)) for (x, y, z) in v3], f3), "dark", smooth=False)
    # upper teeth along the maxilla arc
    for k in range(8):
        th = math.pi / 2 + (k - 3.5) * 0.19
        x, y = 0.48 * math.cos(th), -0.87 * math.sin(th) ** 0.6
        v4, f4 = L.box(0.1, 0.075, 0.15, (0, 0, 0))
        v4 = L.transform(v4, Matrix.Rotation(th - math.pi / 2, 4, "Z"))
        c.add("tooth_u%d" % k, ([W((x + a, y + b, -0.87 + d)) for (a, b, d) in v4], f4), "bone", sharp_deg=30)
    # mandible (U) with lower teeth: broad ramus plates up to the cranium base, solid body and chin.
    # Explicit section frames: thin axis = outward horizontal, wide axis = perpendicular in-plane.
    jaw = [(-0.78, 0.05, -0.44), (-0.75, -0.12, -0.96), (-0.5, -0.62, -1.07), (0.0, -0.83, -1.1)]
    jaw = jaw + [(-x, y, z) for (x, y, z) in reversed(jaw[:-1])]
    jp = [Vector(p) for p in L.catmull(jaw, 4)]
    T, _, _ = L.transport_frames(jp)
    secs = []
    for i, p in enumerate(jp):
        u = i / (len(jp) - 1)
        e = abs(2 * u - 1)
        out = Vector((p.x, p.y + 0.35, 0.0))
        out = (out - T[i] * out.dot(T[i])).normalized()
        wide = T[i].cross(out).normalized()
        hw = 0.12 + 0.17 * L.smoothstep(0.62, 1.0, e)
        ht = 0.065
        ring = [W(tuple(p + wide * (qx * hw) + out * (qy * ht))) for (qx, qy) in L.rounded_rect(2, 2, 0.6, 1)]
        secs.append(ring)
    c.add("mandible", L.loft(secs), "bone", sharp_deg=50)
    for k in range(6):
        th = math.pi / 2 + (k - 2.5) * 0.21
        x, y = 0.42 * math.cos(th), -0.8 * math.sin(th) ** 0.6
        c.add("tooth_l%d" % k, L.box(0.09 * s, 0.07 * s, 0.11 * s, W((x, y, -0.99))), "bone", sharp_deg=30)
    return cran


def build_staff(c):
    length = c.g("shaft_length", 1.62, 0.05)
    r = c.g("shaft_radius", 0.0165)
    auto = {"medieval": ("crook", "orb"), "dark": ("skull",), "elven": ("crystal",), "scifi": ("orb",)}[c.style]
    head = c.choice("head", ("crystal", "skull", "orb", "crook"), auto)
    wob = {"medieval": 0.006, "dark": 0.014, "elven": 0.004, "scifi": 0.0}[c.style]
    path = shaft_path(c, length, wob)
    rng = random.Random(c.seed * 13 + 3)
    knots = []
    while len(knots) < (5 if c.style == "dark" else 2):  # keep knots away from the hand positions
        kc = rng.uniform(0.08, 0.92)
        if not 0.33 < kc < 0.68:
            knots.append((kc, rng.uniform(0.15, 0.35)))

    def rad(u):
        rr = r * (1.08 - 0.2 * u)
        for (kc, ka) in knots:
            rr *= 1 + ka * math.exp(-((u - kc) / 0.018) ** 2)
        return rr
    role = "wood" if c.style != "scifi" else "metal_dark"
    if c.style == "dark":
        role = "wood"
    c.add("shaft", L.sweep(path, rad, 9 if c.style != "scifi" else 8, tip0=False), role, sharp_deg=50)
    top = Vector(path[-1])
    main = length * 0.56
    off = length * 0.42

    def at(z):  # shaft centre (x, y) at height z (the shaft wobbles between straight ends)
        u = L.clamp(z / length, 0.0, 1.0) * (len(path) - 1)
        i = min(int(u), len(path) - 2)
        t = u - i
        return (L.lerp(path[i][0], path[i + 1][0], t), L.lerp(path[i][1], path[i + 1][1], t))

    for i, zc in enumerate((main,)):
        wx, wy = at(zc)
        g = handle_geo(zc - 0.075, zc + 0.075, rad(zc / length) * 1.07, rad(zc / length) * 1.05, 11, "spiral")
        c.add("wrap_%d" % i, (L.translate(g[0], (wx, wy, 0)), g[1]), "grip", sharp_deg=0)
    c.add("ferrule", L.lathe([(0.0, -0.01), (r * 0.9, -0.01), (r * 1.25, 0.04), (0.0, 0.045)], 8), "trim", smooth=False)
    if c.style == "elven":
        helix = []
        for t in [k * 0.5 for k in range(37)]:
            z = length - 0.42 + t * 0.022
            wx, wy = at(z)
            rr = rad(z / length) * 1.05
            helix.append((wx + math.cos(t) * rr, wy + math.sin(t) * rr, z))
        c.add("helix", L.sweep(helix, 0.0022, 5), "trim", sharp_deg=0)
    if c.style == "scifi":
        for k in range(3):
            c.add("glow_ring_%d" % k, ring_geo(length * (0.25 + 0.25 * k), r * 1.12, 0.012, 10), "emit")
    mx, my = at(main)
    ox, oy = at(off)
    tip = None
    if head == "crook":
        rr = 0.07 * c.g("head_size", 1.0, 0.1)
        pts = [(top.x, top.y, top.z - 0.02), (top.x, top.y, top.z + 0.1)]
        for k in range(1, 14):
            a = math.pi * 1.25 * k / 13
            pts.append((top.x, top.y - rr + rr * math.cos(a), top.z + 0.1 + rr * math.sin(a)))
        pts = L.catmull(pts, 2)
        c.add("crook", L.sweep(pts, lambda u: r * (1.0 - 0.45 * u), 9, tip1=False), "wood", sharp_deg=50)
        c.add("crook_ring", ring_geo(top.z + 0.02, r * 1.3, 0.018, 10), "trim")
        tip = Vector(pts[-1])
        if c.orn >= 2:
            g = L.crystal(0.013, 0.05, 6)
            g = (L.transform(g[0], Matrix.Translation((top.x, top.y - rr, top.z + 0.1 + 0.005)) @ L.rot("X", 180)), g[1])
            c.add("crook_gem", g, "gem", smooth=False)
            tip = Vector((top.x, top.y - rr, top.z + 0.06))
        head_top = top.z + 0.1 + rr
    else:
        cup_h = 0.07
        c.add("collar", L.lathe([(r * 0.9, top.z - 0.03), (r * 1.35, top.z - 0.02), (r * 1.25, top.z + 0.01),
                                 (r * 2.1, top.z + cup_h * 0.6), (r * 1.8, top.z + cup_h), (0.0, top.z + cup_h * 0.9)], 10),
              "trim", sharp_deg=40)
        base = Vector((top.x, top.y, top.z + cup_h * 0.8))
        if head == "crystal":
            hgt = 0.2 * c.g("head_size", 1.0, 0.1)
            g = L.crystal(0.03, hgt, 6, jitter=0.15, rng=random.Random(c.seed))
            c.add("crystal", (L.translate(g[0], base), g[1]), "gem", smooth=False)
            n_pr = 3
            for k in range(n_pr):
                a = 2 * math.pi * k / n_pr + 0.4
                d = Vector((math.cos(a), math.sin(a), 0))
                pts = [base + d * 0.02, base + d * 0.05 + Vector((0, 0, hgt * 0.25)),
                       base + d * 0.042 + Vector((0, 0, hgt * 0.55)), base + d * 0.022 + Vector((0, 0, hgt * 0.72))]
                c.add("prong_%d" % k, L.sweep(L.catmull([tuple(p) for p in pts], 3), lambda u: 0.0055 * (1 - 0.7 * u), 6,
                                              tip1=True), "trim", sharp_deg=0)
            if c.orn >= 2:
                for k in range(2):
                    g = L.crystal(0.012, 0.07, 5, rng=random.Random(c.seed + k))
                    M = Matrix.Translation(base + Vector((0.03 * (1 if k else -1), 0.01, 0.0))) @ L.rot("Y", 30 if k else -30)
                    c.add("crystal_small_%d" % k, (L.transform(g[0], M), g[1]), "gem", smooth=False)
            tip = base + Vector((0, 0, hgt * 0.55))
            head_top = base.z + hgt
        elif head == "orb":
            orr = 0.05 * c.g("head_size", 1.0, 0.1)
            oc = base + Vector((0, 0, orr * 1.05))
            c.add("orb", L.uv_sphere(orr, 16, 10, centre=tuple(oc)), "gem")
            for k in range(4):
                a = 2 * math.pi * k / 4 + math.pi / 4
                d = Vector((math.cos(a), math.sin(a), 0))
                pts = [base + d * 0.015]
                for j in range(1, 8):
                    t = math.pi * (j / 7) * 0.92 - math.pi / 2
                    pts.append(oc + d * (orr * 1.12) * math.cos(t) + Vector((0, 0, orr * 1.12 * math.sin(t))))
                c.add("cage_%d" % k, L.sweep([tuple(p) for p in pts], lambda u: 0.0048 * (1 - 0.5 * u), 6), "trim",
                      sharp_deg=0)
            if c.orn >= 2:
                c.add("cap", L.lathe([(0.0, oc.z + orr * 1.08), (0.012, oc.z + orr * 1.08), (0.0, oc.z + orr * 1.08 + 0.04)], 6,
                                     ), "trim", smooth=False)
            tip = oc
            head_top = oc.z + orr * 1.15
        else:  # skull
            sz = 0.082 * c.g("head_size", 1.0, 0.06)
            sc = base + Vector((0, 0, sz * 1.15))
            eye_role = "gem" if c.style in ("dark", "elven", "scifi") else "dark"
            skull_parts(c, tuple(sc), sz, eye_role)
            if c.orn >= 1:  # bone claws gripping the skull from below
                for k in range(4):
                    a = 2 * math.pi * k / 4 + math.pi / 4
                    d = Vector((math.cos(a), math.sin(a), 0))
                    pts = [base + d * 0.012, base + d * 0.045 + Vector((0, 0, 0.02)),
                           base + d * 0.06 + Vector((0, 0, sz * 0.9)), base + d * 0.042 + Vector((0, 0, sz * 1.5))]
                    c.add("claw_%d" % k, L.sweep(L.catmull([tuple(p) for p in pts], 3), lambda u: 0.0065 * (1 - 0.75 * u), 6,
                                                 tip1=True), "bone" if c.style == "dark" else "trim", sharp_deg=0)
            if c.orn >= 2:  # horns
                for sx in (1, -1):
                    pts = [sc + Vector((sx * sz * 0.55, 0.0, sz * 0.45)), sc + Vector((sx * sz * 1.1, sz * 0.2, sz * 0.9)),
                           sc + Vector((sx * sz * 1.25, sz * 0.5, sz * 1.5)), sc + Vector((sx * sz * 1.0, sz * 0.6, sz * 1.9))]
                    c.add("horn_%d" % (sx > 0), L.sweep(L.catmull([tuple(p) for p in pts], 3),
                                                        lambda u: sz * 0.16 * (1 - 0.9 * u) + 0.001, 7, tip1=True),
                          "bone", sharp_deg=0)
            tip = sc + Vector((0, 0, sz * 0.9))
            head_top = sc.z + sz
    c.sock("socket_grip_main", (mx, my, main))
    c.sock("socket_grip_off", (ox, oy, off))
    c.sock("socket_fx_tip", tuple(tip), kind="fx")
    c.resolved["head_top_z"] = round(head_top, 3)


def build_wand(c):
    length = c.g("length", 0.36, 0.06)
    r0 = c.g("radius", 0.0095)
    tw = {"elven": 18.0, "dark": 0.0, "medieval": 0.0, "scifi": 0.0}[c.style]
    prof = [(0.0, 0.0), (r0 * 0.9, 0.0), (r0 * 1.15, 0.012)]
    for k in range(1, 15):
        u = k / 14
        z = 0.012 + (length - 0.03) * u
        rr = L.lerp(r0, r0 * 0.42, u)
        if c.style == "dark":
            rr *= 1 + 0.2 * max(0, math.cos(u * 40)) ** 6
        prof.append((rr, z))
    prof.append((0.0, length - 0.015))
    rf = (lambda rr, a, z: rr * (1 + 0.14 * math.cos(3 * a + z * tw))) if tw else None
    role = {"medieval": "wood", "dark": "bone", "elven": "wood", "scifi": "metal_dark"}[c.style]
    c.add("body", L.lathe(prof, 10, rfunc=rf), role, sharp_deg=50)
    c.add("handle", handle_geo(0.012, 0.11, r0 * 1.1, r0 * 1.05, 10, "spiral" if c.style != "scifi" else "rings"),
          "grip" if c.style != "elven" else "trim", sharp_deg=0)
    for k in range(c.orn):
        c.add("ring_%d" % k, ring_geo(0.115 + 0.012 * k, r0 * 1.15, 0.005, 10), "trim")
    tz = length - 0.02
    if c.style in ("elven", "medieval"):
        g = L.crystal(0.0085, 0.04, 6)
        c.add("tip", (L.translate(g[0], (0, 0, tz)), g[1]), "gem", smooth=False)
        tip = (0, 0, tz + 0.02)
    else:
        c.add("tip", L.uv_sphere(0.011, 10, 8, centre=(0, 0, tz + 0.008)), "gem")
        tip = (0, 0, tz + 0.008)
    c.sock("socket_grip_main", (0, 0, 0.06))
    c.sock("socket_fx_tip", tip, kind="fx")


# =============================================================================== ranged

def build_bow(c):
    length = c.g("length", 1.42 if c.style in ("medieval", "dark") else 1.32, 0.05)
    brace = c.g("brace_height", 0.17)
    recurve = c.g("recurve", c.style in ("elven", "dark", "scifi"))
    zc = length / 2
    half = length / 2
    riser = 0.09
    n = 14
    limbs = []
    for sgn in (1, -1):
        pts = []
        for k in range(n + 1):
            s = k / n
            z = zc + sgn * (riser + s * (half - riser))
            y = brace * s ** 1.7
            if recurve:
                y -= 0.07 * L.smoothstep(0.72, 1.0, s) ** 1.4
            pts.append((0.0, y, z))
        limbs.append(pts)
    width = 0.034 if c.style != "scifi" else 0.04
    for i, pts in enumerate(limbs):
        rad = lambda u: (width / 2 * (1 - 0.62 * u), 0.011 * (1 - 0.45 * u))
        sec = None if c.style != "scifi" else L.rounded_rect(2, 2, 0.4, 1)
        c.add("limb_%d" % i, L.sweep(pts, rad, 8, up_hint=(1, 0, 0), section=sec), "wood" if c.style != "scifi" else "metal",
              sharp_deg=40)
        tip = Vector(pts[-1])
        c.add("nock_%d" % i, L.uv_sphere(0.008, 6, 4, sz=1.6, centre=tuple(tip)), "bone" if c.style != "scifi" else "trim")
    rpath = [(0.0, 0.0, zc - riser - 0.01 + (riser + 0.01) * 2 * k / 8) for k in range(9)]
    c.add("riser", L.sweep(rpath, (width / 2 * 1.02, 0.017), 10, up_hint=(1, 0, 0)), "wood" if c.style != "scifi" else "metal_dark",
          sharp_deg=40)
    c.add("grip_wrap", handle_geo(zc - 0.05, zc + 0.05, 0.0185, 0.0185, 10, "spiral", sx=1.0, sy=1.05), "grip", sharp_deg=0)
    shelf = c.add("shelf", L.box(0.012, 0.022, 0.006, (-0.016, -0.004, zc + 0.054)), "trim", bevel=(0.001, 1, 30))
    t0, t1 = Vector(limbs[0][-1]), Vector(limbs[1][-1])
    c.add("string", L.sweep([tuple(t1), tuple(t0)], 0.0013, 4), "string", sharp_deg=0)
    if c.orn >= 1:
        for k, s in enumerate((-1, 1)):
            c.add("band_%d" % k, ring_geo(zc + s * 0.062, 0.02, 0.007, 10), "trim")
    if c.style == "dark":
        for i, pts in enumerate(limbs):
            for j in (4, 8, 11):
                p = Vector(pts[j])
                g = L.lathe([(0.0, 0.0), (0.006, 0.0), (0.0, 0.035)], 4)
                c.add("spike_%d_%d" % (i, j), oriented(g, (0, -1, 0.3 * (1 if i == 0 else -1)), p + Vector((0, -0.006, 0))),
                      "bone", smooth=False)
    if c.style == "elven" and c.orn >= 2:
        for i, s in enumerate((1, -1)):
            leaf = [(0.0, 0.0), (0.012, 0.03), (0.0, 0.07), (-0.012, 0.03)]
            v, f = L.plate(L.catmull(leaf, 3, closed=True), 0.003, axes="YZ")
            v = L.transform(v, Matrix.Translation((0, -0.008, zc + s * (riser + 0.005))) @ L.rot("X", 0 if s > 0 else 180))
            c.add("leaf_%d" % i, (v, f), "trim")
    if c.style == "scifi":
        for i, pts in enumerate(limbs):
            tip = Vector(pts[-1])
            g = L.lathe([(0.0, -0.007), (0.018, -0.007), (0.018, 0.007), (0.0, 0.007)], 10)
            c.add("cam_%d" % i, oriented(g, (1, 0, 0), tip), "trim", sharp_deg=30)
        c.add("riser_glow", L.box(0.004, 0.006, 0.12, (0.0, -0.02, zc + 0.11)), "emit", smooth=False)
    c.sock("socket_grip_main", (0, 0, zc))
    c.sock("socket_fx_tip", (-0.016, -0.012, zc + 0.062), RX90, kind="fx", forward="+Z")


def build_crossbow(c):
    s = c.g("size", 1.0, 0.05)
    zt = 0.11 * s
    stock = [(0.4, 0.075), (0.4, -0.025), (0.33, -0.04), (0.2, -0.005), (0.12, -0.005), (0.05, -0.03),
             (-0.12, -0.02), (-0.33, -0.01), (-0.36, 0.012), (-0.36, 0.03), (0.0, 0.03), (0.2, 0.035), (0.3, 0.05)]
    stock = [(u * s, v * s + zt) for (u, v) in stock]
    v, f = L.plate(stock, 0.04 * s, axes="YZ")
    c.add("tiller", (v, f), "wood", bevel=(0.004, 2, 30), sharp_deg=30)
    c.add("rail", L.box(0.016 * s, 0.34 * s, 0.006 * s, (0, -0.19 * s, zt + 0.032 * s)), "metal_dark", bevel=(0.001, 1, 30))
    py = -0.31 * s
    pts = []
    for k in range(13):
        x = -0.31 * s + 0.62 * s * k / 12
        y = py + 0.055 * s * (abs(x) / (0.31 * s)) ** 1.6
        pts.append((x, y, zt + 0.02 * s))
    c.add("prod", L.sweep(pts, lambda u: (0.012 * s * (1 - 0.35 * abs(2 * u - 1)), 0.007 * s), 8, up_hint=(0, 0, 1)),
          "metal", sharp_deg=40)
    c.add("prod_block", L.box(0.07 * s, 0.05 * s, 0.045 * s, (0, py, zt + 0.018 * s)), "metal_dark", bevel=(0.002, 1, 30))
    nut = (0.0, -0.035 * s, zt + 0.04 * s)
    for sgn in (-1, 1):
        tip = pts[0] if sgn < 0 else pts[-1]
        c.add("string_%d" % (sgn > 0), L.sweep([tip, nut], 0.0016 * s, 4), "string", sharp_deg=0)
    c.add("nut", oriented(L.lathe([(0.0, -0.008), (0.012, -0.008), (0.012, 0.008), (0.0, 0.008)], 10), (1, 0, 0),
                          (0, -0.03 * s, zt + 0.03 * s)), "trim", sharp_deg=40)
    lever = [(0.0, -0.02 * s, zt - 0.01 * s), (0.0, 0.06 * s, zt - 0.04 * s), (0.0, 0.2 * s, zt - 0.055 * s)]
    c.add("trigger", L.sweep(L.catmull(lever, 3), lambda u: 0.004 * s * (1 - 0.3 * u), 5), "metal_dark", sharp_deg=0)
    loop = []
    for k in range(13):
        a = math.pi * k / 12
        loop.append((math.cos(a) * 0.045 * s, -0.37 * s - math.sin(a) * 0.05 * s, zt + 0.02 * s))
    c.add("stirrup", L.sweep(loop, 0.0045 * s, 6), "metal_dark", sharp_deg=0)
    if c.orn >= 1:  # loaded bolt
        bz = zt + 0.04 * s
        c.add("bolt", oriented(L.lathe([(0.0, 0.0), (0.004, 0.0), (0.004, 0.27 * s), (0.0, 0.27 * s)], 6), (0, -1, 0),
                               (0, -0.03 * s, bz)), "wood2", sharp_deg=40)
        c.add("bolt_head", oriented(L.lathe([(0.0, 0.0), (0.008, 0.0), (0.0, 0.03)], 4), (0, -1, 0), (0, -0.3 * s, bz)),
              "metal", smooth=False)
        for k in range(3):
            a = math.radians(90 + 120 * k)
            fl = [(0.0, 0.0), (0.0, 0.04 * s), (0.012, 0.03 * s), (0.012, 0.0)]
            v, f = L.plate(fl, 0.0012, axes="XY")
            v = L.transform(v, Matrix.Translation((0, -0.065 * s, bz)) @ Matrix.Rotation(a, 4, "Y") @ L.rot("X", 0))
            c.add("fletch_%d" % k, (v, f), "accent", smooth=False)
    if c.orn >= 2:
        for sx in (1, -1):
            for k in range(3):
                rivet(c, "rivet_%d_%d" % (sx > 0, k), (sx * 0.021 * s, (0.05 + 0.1 * k) * s, zt + 0.0 * s), (sx, 0, 0), 0.004, 0.002)
    main = Matrix(((0, -1, 0), (0, 0, -1), (1, 0, 0)))  # columns X=+Z, Y=-X, Z=-Y (stock grip, right hand)
    off = Matrix(((0, 1, 0), (0, 0, -1), (-1, 0, 0)))   # columns X=-Z, Y=+X, Z=-Y (cradle, left hand)
    c.sock("socket_grip_main", (0, 0.13 * s, zt + 0.014 * s), main)
    c.sock("socket_grip_off", (0, -0.17 * s, zt + 0.005 * s), off)
    c.sock("socket_fx_tip", (0, -0.37 * s, zt + 0.04 * s), RX90, kind="fx", forward="+Z")


# =============================================================================== shields

def kite_outline(W, H):
    side = [(1.0, 0.0), (1.02, 0.08), (1.0, 0.18), (0.94, 0.31), (0.84, 0.45), (0.69, 0.6),
            (0.51, 0.74), (0.32, 0.86), (0.14, 0.95), (0.0, 1.0)]
    r = L.catmull([(W / 2 * a, H * (1 - b), 0) for a, b in side], 3)
    right = [(p[0], p[1]) for p in r]
    left = [(-x, z) for (x, z) in reversed(right[:-1])]
    top = [(W / 2 * (1 - 2 * i / 10), H + 0.03 * H * math.sin(math.pi * i / 10)) for i in range(1, 10)]
    return right + left + list(reversed(top))


def shield_geo(outline, bow, t, rings=5):
    """Curved board: face toward -Y. outline: (x, z) loop. bow(x, z) >= 0 pushes toward -Y."""
    n = len(outline)
    cx = sum(p[0] for p in outline) / n
    cz = sum(p[1] for p in outline) / n
    verts, faces, fm = [], [], []
    bases = {}
    for side, sgn in (("front", -1), ("back", 1)):
        base = len(verts)
        bases[side] = base
        verts.append((cx, -bow(cx, cz) + sgn * t / 2, cz))
        for r in range(1, rings + 1):
            k = r / rings
            for (x, z) in outline:
                u, w = cx + (x - cx) * k, cz + (z - cz) * k
                verts.append((u, -bow(u, w) + sgn * t / 2, w))
        for i in range(n):
            j = (i + 1) % n
            faces.append((base, base + 1 + i, base + 1 + j))
            fm.append(0 if side == "front" else 1)
        for r in range(1, rings):
            o0, o1 = base + 1 + (r - 1) * n, base + 1 + r * n
            for i in range(n):
                j = (i + 1) % n
                faces.append((o0 + i, o1 + i, o1 + j, o0 + j))
                fm.append(0 if side == "front" else 1)
    fo = bases["front"] + 1 + (rings - 1) * n
    bo = bases["back"] + 1 + (rings - 1) * n
    for i in range(n):
        j = (i + 1) % n
        faces.append((fo + j, fo + i, bo + i, bo + j))
        fm.append(2)
    return (verts, faces), fm, (cx, cz)


def emblem_outline(kind, size):
    s = size
    if kind == "cross":
        a, b = 0.18, 1.0
        pts = [(a, b), (-a, b), (-a, a), (-b * 0.7, a), (-b * 0.7, -a), (-a, -a), (-a, -b * 1.1), (a, -b * 1.1),
               (a, -a), (b * 0.7, -a), (b * 0.7, a), (a, a)]
    elif kind == "chevron":
        pts = [(0.0, 0.35), (1.0, -0.45), (1.0, -0.05), (0.0, 0.75), (-1.0, -0.05), (-1.0, -0.45)]
    elif kind == "leaf":
        pts = L.catmull([(0.0, -1.0), (0.45, -0.35), (0.38, 0.4), (0.0, 1.0), (-0.38, 0.4), (-0.45, -0.35)], 3, closed=True)
        pts = [(p[0], p[1]) for p in pts]
    elif kind == "star":
        pts = []
        for i in range(16):
            r = 1.0 if i % 2 == 0 else 0.42
            a = math.pi / 2 + 2 * math.pi * i / 16
            pts.append((r * math.cos(a), r * math.sin(a)))
    else:  # diamond
        pts = [(0.0, 1.0), (0.6, 0.0), (0.0, -1.0), (-0.6, 0.0)]
    return [(x * s, z * s) for (x, z) in pts]


def build_shield(c):
    typ = c.type
    if typ == "shield_kite":
        W, H = c.g("width", 0.56, 0.05), c.g("height", 0.95, 0.05)
        outline = kite_outline(W, H)
        d = 0.07
        bow = lambda x, z: d * (1 - (x / (W / 2)) ** 2)
        grip = (0.0, H * 0.58)
        t = 0.016
    elif typ == "shield_tower":
        W, H = c.g("width", 0.62, 0.05), c.g("height", 1.25, 0.04)
        rr = L.rounded_rect(W, H, 0.05, 3)
        outline = [(x, z + H / 2) for (x, z) in rr]
        d = 0.13
        bow = lambda x, z: d * (1 - (x / (W / 2)) ** 2)
        grip = (0.0, H * 0.52)
        t = 0.018
    else:
        R = c.g("radius", 0.37 if typ == "shield_round" else 0.16, 0.05)
        W = H = 2 * R
        outline = [(R * math.cos(2 * math.pi * i / 32), R + R * math.sin(2 * math.pi * i / 32)) for i in range(32)]
        d = 0.045 if typ == "shield_round" else 0.06
        bow = lambda x, z: d * (1 - min(1.0, (x * x + (z - R) ** 2) / (R * R)))
        grip = (0.0, R)
        t = 0.014 if typ == "shield_round" else 0.004
    buckler = typ == "buckler"
    face_role = {"medieval": "accent", "dark": "metal_dark", "elven": "accent", "scifi": "metal"}[c.style]
    if buckler:
        face_role = "metal"
    back_role = "wood2" if c.style != "scifi" else "metal_dark"
    geo, fm, (cx, cz) = shield_geo(outline, bow, t, rings=5 if not buckler else 4)
    rim_role = "trim" if c.style in ("elven",) else "metal_dark"
    c.add("board", geo, None, mats=[c.m(face_role), c.m(back_role), c.m(rim_role)], face_mats=fm, sharp_deg=45)
    # rim
    rim = [(x, -bow(x, z), z) for (x, z) in outline]
    rim_r = t / 2 + 0.006 if not buckler else 0.006
    c.add("rim", L.sweep(rim, (rim_r, rim_r * 1.0), 6, closed_path=True, up_hint=(0, 1, 0)), rim_role, sharp_deg=0)
    gx, gz = grip
    # boss
    has_boss = typ in ("shield_round", "buckler") or c.orn >= 3 or c.style == "scifi"
    if has_boss:
        br = 0.085 if typ == "shield_round" else (0.07 if buckler else 0.06)
        bh = br * (0.75 if not buckler else 0.6)
        prof = [(br * 1.2, 0.0), (br * 1.2, 0.004), (br, 0.008), (br * 0.92, bh * 0.55), (br * 0.55, bh * 0.92), (0.0, bh)]
        if buckler and c.orn >= 2:
            prof = prof[:-1] + [(br * 0.2, bh), (0.0, bh + 0.05)]
        g = L.lathe([(0.0, 0.0)] + prof, 14)
        c.add("boss", oriented(g, (0, -1, 0), (gx, -bow(gx, gz) - t / 2 + 0.001, gz)), "metal", sharp_deg=50)
    # emblem
    if not buckler and c.orn >= 1:
        pool = ("cross", "star") if has_boss else ("cross", "chevron")  # a central boss would cut a chevron
        ek = {"medieval": c.rng.choice(pool), "dark": "star", "elven": "leaf" if not has_boss else "star",
              "scifi": "diamond" if not has_boss else "star"}[c.style]
        ek = c.p.get("emblem", ek)
        c.resolved["emblem"] = ek
        es = min(W, H) * 0.32
        ec = (cx, cz + (0.06 * H if typ == "shield_kite" else 0))
        pts = [(ec[0] + x, ec[1] + z) for (x, z) in emblem_outline(ek, es)]
        e_role = {"medieval": "accent2", "dark": "bone", "elven": "trim", "scifi": "emit"}[c.style]
        v, f = L.plate(pts, 0.003, axes="XZ", centre=-(t / 2 + 0.003), bulge=lambda x, z: -bow(x, z))
        eo = c.add("emblem", (v, f), e_role, sharp_deg=40)
        # the emblem's large flat triangles would cut behind a strongly curved board: densify and
        # re-project every vertex onto the board surface (front 4.5 mm, back 1.5 mm in front of it)
        bm = bmesh.new()
        bm.from_mesh(eo.data)
        bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=2, use_grid_fill=True)
        for vert in bm.verts:
            x, y, z = vert.co
            base = -bow(x, z) - t / 2
            vert.co.y = base - (0.0045 if (y - base) < -0.003 else 0.0015)
        bm.to_mesh(eo.data)
        bm.free()
        eo.data.update()
    # rivets around the rim
    if c.orn >= 1 or buckler:
        n = len(outline)
        step = max(1, n // (14 if not buckler else 10))
        for i in range(0, n, step):
            x, z = outline[i]
            k = 0.9 if not buckler else 0.86
            u, w = cx + (x - cx) * k, cz + (z - cz) * k
            rivet(c, "rivet_%02d" % i, (u, -bow(u, w) - t / 2, w), (0, -1, 0), 0.007 if not buckler else 0.005, 0.0035)
    if typ == "shield_tower" and c.orn >= 1:
        for k, zz in enumerate((H * 0.2, H * 0.8)):
            band = [(x, -bow(x, zz) - t / 2 - 0.002, zz) for x in [W / 2 * (-0.98 + 1.96 * i / 16) for i in range(17)]]
            c.add("band_%d" % k, L.sweep(band, (0.004, 0.022), 4, up_hint=(0, -1, 0), section=L.rounded_rect(2, 2, 0.3, 1)),
                  "metal_dark", sharp_deg=40)
    # handle on the back (vertical bar), straps
    yb = -bow(gx, gz) + t / 2
    so = 0.032
    hpath = [(gx, yb - 0.002, gz - 0.065), (gx, yb + so * 0.8, gz - 0.05), (gx, yb + so, gz - 0.02),
             (gx, yb + so, gz + 0.02), (gx, yb + so * 0.8, gz + 0.05), (gx, yb - 0.002, gz + 0.065)]
    c.add("handle", L.sweep(L.catmull(hpath, 2), 0.011, 8, up_hint=(1, 0, 0)), "grip", sharp_deg=0)
    if typ in ("shield_kite", "shield_tower"):
        # enarme (forearm strap): a 7 mm leather band standing clear of the board, riveted at both ends
        sz = gz - 0.2
        xs = [-0.1 + 0.2 * i / 10 for i in range(11)]
        loop = lambda x: 0.034 * math.exp(-(x / 0.045) ** 2)
        strap = [(x, -bow(x, sz) + t / 2 + 0.0055 + loop(x), sz) for x in xs]
        c.add("arm_strap", L.sweep(L.catmull(strap, 2), (0.0035, 0.018), 6, up_hint=(0, 1, 0),
                                   section=L.rounded_rect(2, 2, 0.35, 1)), "grip", sharp_deg=40)
        for x in (xs[0] + 0.006, xs[-1] - 0.006):
            rivet(c, "strap_rivet_%d" % (x > 0), (x, -bow(x, sz) + t / 2 + 0.009, sz), (0, 1, 0), 0.006, 0.003)
    c.sock("socket_grip_main", (gx, yb + so, gz))
    c.resolved["face_axis"] = "-Y"
    c.resolved["grip_style"] = "center"


# =============================================================================== props

def build_torch(c):
    length = c.g("length", 0.5, 0.05)
    r = c.g("radius", 0.016)
    hz = length * 0.74
    if c.style == "scifi":
        c.add("body", L.lathe([(0.0, 0.0), (r, 0.0), (r * 1.05, hz), (r * 1.6, hz + 0.03), (r * 1.6, hz + 0.07),
                               (0.0, hz + 0.07)], 10), "metal_dark", bevel=(0.0015, 1, 30), sharp_deg=35)
        c.add("lens", L.lathe([(0.0, hz + 0.07), (r * 1.4, hz + 0.07), (0.0, hz + 0.078)], 12), "emit", smooth=False)
        c.add("grip", handle_geo(0.02, hz * 0.6, r * 1.08, r * 1.08, 10, "rings"), "grip", sharp_deg=0)
        c.sock("socket_grip_main", (0, 0, hz * 0.32))
        c.sock("socket_fx_tip", (0, 0, hz + 0.078), kind="fx")
        return
    c.add("handle", L.lathe([(0.0, 0.0), (r * 1.2, 0.0), (r * 1.25, 0.015), (r, 0.03), (r * 1.15, hz), (0.0, hz)], 10),
          "wood", sharp_deg=50)
    c.add("wrap", handle_geo(0.04, 0.2, r * 1.12, r * 1.15, 11, "spiral"), "grip", sharp_deg=0)
    c.add("cup", L.lathe([(0.0, hz - 0.03), (r * 1.3, hz - 0.03), (r * 2.4, hz + 0.02), (r * 2.5, hz + 0.03),
                          (0.0, hz + 0.028)], 12), "metal_dark", sharp_deg=40)
    for k in range(4):
        a = 2 * math.pi * k / 4 + math.pi / 4
        p0 = (math.cos(a) * r * 2.4, math.sin(a) * r * 2.4, hz + 0.025)
        p1 = (math.cos(a) * r * 2.2, math.sin(a) * r * 2.2, hz + 0.075)
        p2 = (math.cos(a) * r * 1.2, math.sin(a) * r * 1.2, hz + 0.1)
        c.add("strap_%d" % k, L.sweep(L.catmull([p0, p1, p2], 3), (0.0025, 0.005), 4, section=L.rounded_rect(2, 2, 0.3, 1)),
              "metal_dark", sharp_deg=40)
    c.add("rag", L.lathe([(0.0, hz + 0.02), (r * 2.1, hz + 0.02), (r * 2.25, hz + 0.05), (r * 2.0, hz + 0.08),
                          (r * 1.2, hz + 0.1), (0.0, hz + 0.105)], 10, rfunc=lambda rr, a, z: rr * (1 + 0.08 * math.cos(a * 4 + z * 90))),
          "wood2", sharp_deg=60)
    if c.g("flame", True):
        fz = hz + 0.085
        prof = [(0.0, fz), (r * 2.0, fz + 0.01), (r * 2.2, fz + 0.04), (r * 1.6, fz + 0.09), (r * 0.7, fz + 0.14),
                (0.0, fz + 0.18)]
        c.add("flame", L.lathe(prof, 9, twist=8.0, rfunc=lambda rr, a, z: rr * (1 + 0.22 * math.cos(a * 3 + z * 40))),
              "flame", sharp_deg=0)
    for k in range(c.orn):
        c.add("ring_%d" % k, ring_geo(0.03 + 0.18 * k, r * 1.3, 0.008, 10), "trim")
    c.sock("socket_grip_main", (0, 0, 0.12))
    c.sock("socket_fx_tip", (0, 0, hz + 0.1), kind="fx")


def build_potion(c):
    shape = c.choice("shape", ("round", "tall", "square"), ("round", "tall", "square"))
    h = c.g("height", 0.16, 0.08)
    if shape == "round":
        R = h * 0.32
        prof = [(0.0, 0.0), (R * 0.55, 0.0)] + [(R * math.sin(math.pi * k / 8), R - R * math.cos(math.pi * k / 8))
                                                for k in range(2, 8)] + [(h * 0.07, R * 2.05), (h * 0.065, h * 0.88), (h * 0.085, h * 0.9),
                                                                          (h * 0.085, h * 0.93), (0.0, h * 0.93)]
        seg, liq = 14, R * 1.25
    elif shape == "tall":
        R = h * 0.2
        prof = [(0.0, 0.0), (R * 0.9, 0.0), (R, 0.01), (R, h * 0.55), (R * 0.85, h * 0.66), (h * 0.065, h * 0.74),
                (h * 0.06, h * 0.88), (h * 0.08, h * 0.9), (h * 0.08, h * 0.93), (0.0, h * 0.93)]
        seg, liq = 12, h * 0.48
    else:
        R = h * 0.24
        prof = [(0.0, 0.0), (R * 0.95, 0.0), (R, 0.01), (R, h * 0.5), (R * 0.6, h * 0.64), (h * 0.065, h * 0.7),
                (h * 0.06, h * 0.88), (h * 0.08, h * 0.9), (h * 0.08, h * 0.93), (0.0, h * 0.93)]
        seg, liq = 4, h * 0.42
    v, f = L.lathe(prof, seg, phase=math.pi / 4 if seg == 4 else 0.0)
    fm = []
    for face in f:
        zc = sum(v[i][2] for i in face) / len(face)
        fm.append(0 if zc < liq else 1)
    c.add("bottle", (v, f), None, mats=[c.m("liquid"), c.m("glass")], face_mats=fm, sharp_deg=35 if seg > 4 else 30,
          bevel=(0.002, 1, 50) if seg == 4 else None)
    ztop = h * 0.93
    c.add("cork", L.lathe([(0.0, ztop - 0.012), (h * 0.058, ztop - 0.012), (h * 0.068, ztop + 0.018),
                           (h * 0.06, ztop + 0.022), (0.0, ztop + 0.022)], 8), "wood", sharp_deg=40)
    if c.orn >= 1:
        neck = [(math.cos(2 * math.pi * k / 10) * h * 0.072, math.sin(2 * math.pi * k / 10) * h * 0.072, h * 0.82)
                for k in range(10)]
        c.add("rope", L.sweep(neck, 0.0022, 5, closed_path=True), "string", sharp_deg=0)
        c.add("seal", L.uv_sphere(h * 0.07, 8, 5, sz=0.5, centre=(0, 0, ztop + 0.022)), "accent")
    if c.orn >= 2 and shape != "square":
        lz = (liq * 0.45) if shape != "round" else R * 1.0
        lr = (R if shape != "round" else R * 0.99) + 0.0008
        c.add("label", ring_geo(lz, lr, 0.032, seg, 0.05), "paper")
    if c.orn >= 3:
        c.add("cap", L.lathe([(0.0, ztop + 0.02), (h * 0.075, ztop + 0.02), (h * 0.05, ztop + 0.04), (0.0, ztop + 0.045)], 8),
              "trim", smooth=False)
    c.sock("socket_grip_main", (0, 0, h * 0.32))
    c.sock("socket_fx_tip", (0, 0, ztop + 0.03), kind="fx")


def build_book(c):
    H = c.g("height", 0.245, 0.06)
    Wd = c.g("width", 0.172, 0.06)
    T = c.g("thickness", 0.05, 0.15)
    bt = 0.006
    cover = {"medieval": "accent", "dark": "grip", "elven": "accent", "scifi": "metal"}[c.style]
    for sx in (1, -1):
        c.add("cover_%d" % (sx > 0), L.box(bt, Wd, H, (sx * (T / 2 - bt / 2), 0, H / 2)), cover, bevel=(0.0018, 2, 30),
              sharp_deg=30)
    sp = []
    for k in range(9):
        a = -math.pi / 2 + math.pi * k / 8
        sp.append((math.sin(a) * T / 2, Wd / 2 - 0.004 + math.cos(a) * T * 0.32))
    sp = [(x, y) for (x, y) in sp]
    spine = L.loft([[(x, y, 0.0) for (x, y) in sp], [(x, y, H) for (x, y) in sp]])
    c.add("spine", spine, cover, sharp_deg=50)
    c.add("pages", L.box(T - 2 * bt, Wd - 0.008, H - 0.01, (0, -0.004, H / 2)), "paper", bevel=(0.001, 1, 30), sharp_deg=30)
    if c.orn >= 1:
        for k in range(4):
            zb = H * (0.18 + 0.213 * k)
            arc = [(math.sin(a) * (T / 2 + 0.002), Wd / 2 - 0.004 + math.cos(a) * (T * 0.32 + 0.002), zb)
                   for a in [-math.pi / 2 + math.pi * j / 6 for j in range(7)]]
            c.add("band_%d" % k, L.sweep(arc, 0.0025, 5), cover, sharp_deg=0)
        for sx in (1, -1):
            for zz in (0.0, H):
                tri = [(-Wd / 2, zz), (-Wd / 2 + 0.03, zz), (-Wd / 2, zz + (0.03 if zz == 0 else -0.03))]
                v, f = L.plate(tri, 0.0015, axes="YZ", centre=sx * (T / 2 + 0.0004))
                c.add("corner_%d_%d" % (sx > 0, zz > 0), (v, f), "trim", bevel=(0.0006, 1, 30))
        emb = {"medieval": "diamond", "dark": "star", "elven": "leaf", "scifi": "diamond"}[c.style]
        pts = [(y, z + H * 0.55) for (y, z) in emblem_outline(emb, Wd * 0.22)]
        v, f = L.plate(pts, 0.002, axes="YZ", centre=T / 2 + 0.001)
        c.add("emblem", (v, f), "trim" if c.style != "scifi" else "emit", sharp_deg=40)
    if c.orn >= 2:
        g = oriented(gem_geo(0.011, 0.006, 8), (1, 0, 0), (T / 2 + 0.0015, 0, H * 0.55))
        c.add("gem", g, "gem", smooth=False)
        strap = [(T / 2 + 0.001, -Wd / 2 + 0.03, H * 0.5), (T / 2 + 0.001, -Wd / 2 - 0.002, H * 0.5),
                 (0.0, -Wd / 2 - 0.006, H * 0.5), (-T / 2 - 0.001, -Wd / 2 - 0.002, H * 0.5), (-T / 2 - 0.001, -Wd / 2 + 0.03, H * 0.5)]
        c.add("clasp", L.sweep(L.catmull(strap, 2), (0.002, 0.012), 4, up_hint=(0, 0, 1), section=L.rounded_rect(2, 2, 0.3, 1)),
              "grip", sharp_deg=40)
    c.sock("socket_grip_main", (0, Wd / 2 - 0.012, H / 2))
    c.sock("socket_fx_tip", (0, -Wd / 4, H + 0.01), kind="fx")


# =============================================================================== sci-fi

def grip_plate(c, name, top_y, bot_y, z0, z1, w=0.03, depth=0.045, grooves=3):
    pts = [(top_y - depth / 2, z1), (top_y + depth / 2, z1), (bot_y + depth / 2 + 0.004, z0 + 0.006),
           (bot_y + depth / 2 - 0.004, z0), (bot_y - depth / 2 + 0.004, z0)]
    front = []
    for k in range(1, grooves * 2):
        t = k / (grooves * 2)
        y = L.lerp(bot_y - depth / 2, top_y - depth / 2, t) - (0.004 if k % 2 else 0.0)
        front.append((y, L.lerp(z0, z1, t)))
    pts += front
    v, f = L.plate(pts, w, axes="YZ")
    return c.add(name, (v, f), "grip", bevel=(0.004, 2, 30), sharp_deg=35)


def build_blaster_pistol(c):
    s = c.g("size", 1.0, 0.05)
    zr0, zr1 = 0.1 * s, 0.155 * s
    c.add("receiver", L.box(0.034 * s, 0.17 * s, zr1 - zr0, (0, 0.015 * s, (zr0 + zr1) / 2)), "metal", bevel=(0.004, 2, 30),
          sharp_deg=30)
    c.add("slide", L.box(0.024 * s, 0.15 * s, 0.014 * s, (0, 0.005 * s, zr1 + 0.006 * s)), "metal_dark", bevel=(0.0025, 1, 30),
          sharp_deg=30)
    bz = zr0 + 0.032 * s
    c.add("barrel", oriented(L.lathe([(0.0, 0.0), (0.012 * s, 0.0), (0.012 * s, 0.1 * s), (0.0, 0.1 * s)], 10), (0, -1, 0),
                             (0, -0.065 * s, bz)), "metal_dark", sharp_deg=35)
    for k in range(3 + (c.orn >= 2)):
        c.add("fin_%d" % k, oriented(ring_geo(0.0, 0.017 * s, 0.005 * s, 10), (0, -1, 0), (0, (-0.08 - 0.018 * k) * s, bz)),
              "trim", sharp_deg=35)
    c.add("emitter", oriented(L.lathe([(0.0, 0.0), (0.016 * s, 0.0), (0.014 * s, 0.014 * s), (0.0, 0.014 * s)], 12), (0, -1, 0),
                              (0, -0.162 * s, bz)), "trim", sharp_deg=35)
    c.add("lens", oriented(L.lathe([(0.0, 0.0), (0.009 * s, 0.0), (0.0, 0.003 * s)], 10), (0, -1, 0), (0, -0.176 * s, bz)),
          "emit", smooth=False)
    grip_plate(c, "grip", 0.055 * s, 0.085 * s, 0.0, zr0 + 0.006 * s, w=0.03 * s, depth=0.046 * s)
    guard = [(0.0, 0.034 * s, zr0), (0.0, 0.028 * s, zr0 - 0.035 * s), (0.0, -0.005 * s, zr0 - 0.04 * s),
             (0.0, -0.025 * s, zr0 - 0.02 * s), (0.0, -0.028 * s, zr0)]
    c.add("trigger_guard", L.sweep(L.catmull(guard, 3), (0.003 * s, 0.006 * s), 5, up_hint=(1, 0, 0)), "metal_dark", sharp_deg=0)
    c.add("trigger", L.sweep(L.catmull([(0, 0.008 * s, zr0), (0, 0.0, zr0 - 0.018 * s), (0, 0.008 * s, zr0 - 0.026 * s)], 3),
                             (0.0025 * s, 0.004 * s), 4, up_hint=(1, 0, 0)), "trim", sharp_deg=0)
    c.add("sight_f", L.box(0.004 * s, 0.008 * s, 0.01 * s, (0, -0.06 * s, zr1 + 0.016 * s)), "metal_dark", bevel=(0.001, 1, 30))
    c.add("sight_r", L.box(0.016 * s, 0.008 * s, 0.01 * s, (0, 0.07 * s, zr1 + 0.016 * s)), "metal_dark", bevel=(0.001, 1, 30))
    if c.orn >= 1:
        c.add("cell", L.box(0.008 * s, 0.06 * s, 0.026 * s, (0.019 * s, 0.03 * s, zr0 + 0.025 * s)), "metal_dark",
              bevel=(0.0015, 1, 30))
        c.add("cell_glow", L.box(0.0015 * s, 0.045 * s, 0.006 * s, (0.0235 * s, 0.03 * s, zr0 + 0.025 * s)), "emit", smooth=False)
    if c.orn >= 2:
        c.add("stripe", L.box(0.0352 * s, 0.03 * s, 0.006 * s, (0, -0.045 * s, zr1 - 0.01 * s)), "accent", smooth=False)
    tilt = math.atan2(0.03, 0.105)
    c.sock("socket_grip_main", (0, 0.07 * s, 0.055 * s), Matrix.Rotation(tilt, 3, "X"))
    c.sock("socket_fx_tip", (0, -0.18 * s, bz), RX90, kind="fx", forward="+Z")


def build_blaster_rifle(c):
    s = c.g("size", 1.0, 0.04)
    zr0, zr1 = 0.12 * s, 0.2 * s
    c.add("receiver", L.box(0.05 * s, 0.3 * s, zr1 - zr0, (0, 0.0, (zr0 + zr1) / 2)), "metal", bevel=(0.005, 2, 30), sharp_deg=30)
    bz = zr0 + 0.05 * s
    c.add("shroud", L.box(0.044 * s, 0.27 * s, 0.05 * s, (0, -0.285 * s, bz)), "metal_dark", bevel=(0.004, 2, 30), sharp_deg=30)
    if c.orn >= 1:
        for sx in (1, -1):
            for k in range(5):
                c.add("vent_%d_%d" % (sx > 0, k), L.box(0.002 * s, 0.026 * s, 0.022 * s, (sx * 0.0225 * s, (-0.2 - 0.04 * k) * s, bz)),
                      "emit" if c.orn >= 3 else "dark", smooth=False)
    c.add("barrel", oriented(L.lathe([(0.0, 0.0), (0.011 * s, 0.0), (0.011 * s, 0.12 * s), (0.0, 0.12 * s)], 10), (0, -1, 0),
                             (0, -0.42 * s, bz)), "metal_dark", sharp_deg=35)
    c.add("muzzle", oriented(L.lathe([(0.0, 0.0), (0.017 * s, 0.0), (0.017 * s, 0.04 * s), (0.013 * s, 0.045 * s), (0.0, 0.045 * s)], 8),
                             (0, -1, 0), (0, -0.53 * s, bz)), "trim", bevel=(0.0015, 1, 30), sharp_deg=30)
    c.add("lens", oriented(L.lathe([(0.0, 0.0), (0.009 * s, 0.0), (0.0, 0.003 * s)], 10), (0, -1, 0), (0, -0.577 * s, bz)),
          "emit", smooth=False)
    # skeleton stock
    for name, p0, p1, rr in (("stock_top", (0, 0.14 * s, zr1 - 0.012 * s), (0, 0.42 * s, zr1 - 0.02 * s), 0.009),
                             ("stock_bot", (0, 0.14 * s, zr0 + 0.008 * s), (0, 0.42 * s, zr0 - 0.045 * s), 0.008)):
        c.add(name, L.sweep([p0, p1], (rr * s, rr * 1.3 * s), 6, up_hint=(1, 0, 0), section=L.rounded_rect(2, 2, 0.5, 1)),
              "metal_dark", sharp_deg=35)
    c.add("buttpad", L.box(0.04 * s, 0.03 * s, 0.14 * s, (0, 0.43 * s, zr0 + 0.0)), "grip", bevel=(0.006, 2, 30), sharp_deg=30)
    grip_plate(c, "grip", 0.1 * s, 0.13 * s, 0.0, zr0 + 0.006 * s, w=0.032 * s, depth=0.046 * s)
    guard = [(0.0, 0.078 * s, zr0), (0.0, 0.072 * s, zr0 - 0.035 * s), (0.0, 0.03 * s, zr0 - 0.04 * s), (0.0, 0.01 * s, zr0)]
    c.add("trigger_guard", L.sweep(L.catmull(guard, 3), (0.003 * s, 0.006 * s), 5, up_hint=(1, 0, 0)), "metal_dark", sharp_deg=0)
    c.add("trigger", L.sweep(L.catmull([(0, 0.05 * s, zr0), (0, 0.042 * s, zr0 - 0.018 * s), (0, 0.05 * s, zr0 - 0.026 * s)], 3),
                             (0.0025 * s, 0.004 * s), 4, up_hint=(1, 0, 0)), "trim", sharp_deg=0)
    fz0, fz1 = 0.02 * s, bz - 0.025 * s
    c.add("foregrip", handle_geo(fz0, fz1, 0.015 * s, 0.014 * s, 10, "rings", pitch=0.012, depth=0.0012), "grip", sharp_deg=0)
    for name, z in (("fg_cap", fz0), ("fg_mount", fz1)):
        c.add(name, ring_geo(z, 0.017 * s, 0.008 * s, 10), "metal_dark")
    fgy = -0.26 * s
    for ob in c.parts[-3:]:
        ob.data.transform(Matrix.Translation((0, fgy, 0)))
    c.add("cell", L.box(0.032 * s, 0.05 * s, 0.07 * s, (0, -0.07 * s, zr0 - 0.03 * s)), "metal_dark", bevel=(0.003, 1, 30),
          sharp_deg=30)
    c.add("cell_glow", L.box(0.0335 * s, 0.03 * s, 0.006 * s, (0, -0.07 * s, zr0 - 0.045 * s)), "emit", smooth=False)
    sz = zr1 + 0.035 * s
    c.add("scope", oriented(L.lathe([(0.0, 0.0), (0.016 * s, 0.0), (0.016 * s, 0.03 * s), (0.013 * s, 0.05 * s),
                                     (0.013 * s, 0.12 * s), (0.016 * s, 0.14 * s), (0.016 * s, 0.17 * s), (0.0, 0.17 * s)], 12),
                            (0, -1, 0), (0, 0.07 * s, sz)), "metal_dark", sharp_deg=35)
    c.add("scope_lens", oriented(L.lathe([(0.0, 0.0), (0.013 * s, 0.0), (0.0, 0.002 * s)], 12), (0, -1, 0), (0, -0.1 * s, sz)),
          "glass", smooth=False)
    for k, y in enumerate((0.03, -0.05)):
        c.add("scope_mount_%d" % k, L.box(0.012 * s, 0.014 * s, 0.03 * s, (0, y * s, zr1 + 0.012 * s)), "metal_dark",
              bevel=(0.0015, 1, 30))
    if c.orn >= 2:
        c.add("stripe", L.box(0.0505 * s, 0.04 * s, 0.008 * s, (0, -0.1 * s, zr1 - 0.015 * s)), "accent", smooth=False)
    tilt = math.atan2(0.03, 0.105)
    c.sock("socket_grip_main", (0, 0.115 * s, 0.055 * s), Matrix.Rotation(tilt, 3, "X"))
    c.sock("socket_grip_off", (0, fgy, (fz0 + fz1) / 2))
    c.sock("socket_fx_tip", (0, -0.58 * s, bz), RX90, kind="fx", forward="+Z")


BUILDERS = {
    "sword": build_sword, "greatsword": build_sword, "dagger": build_sword,
    "axe": build_axe, "battle_axe": build_axe, "mace": build_mace, "warhammer": build_warhammer,
    "spear": build_spear, "halberd": build_halberd, "staff": build_staff, "wand": build_wand,
    "bow": build_bow, "crossbow": build_crossbow,
    "shield_round": build_shield, "shield_kite": build_shield, "shield_tower": build_shield,
    "buckler": build_shield, "torch": build_torch, "potion": build_potion, "book": build_book,
    "blaster_pistol": build_blaster_pistol, "blaster_rifle": build_blaster_rifle,
}


# =============================================================================== assembly

def build_item(params):
    """Build one item in the current scene. Returns (item_object, info dict)."""
    typ = params.get("type")
    c = Ctx(typ, params)
    BUILDERS[typ](c)
    item = L.finalize_item(c.parts, "item_%s" % typ)
    shift = L.place_on_ground(item, params.get("origin", "bbox"))
    forward = params.get("forward", "-Y")
    if forward not in ("-Y", "+Y"):
        raise ValueError("forward must be '-Y' (item frame: edge / muzzle -Y) or '+Y' (props: nose +Y)")
    Rf = Matrix.Rotation(math.pi, 3, "Z") if forward == "+Y" else Matrix.Identity(3)
    if forward == "+Y":  # whole item turned 180 deg about Z; sockets keep their frames relative to the geometry
        item.data.transform(Rf.to_4x4())
        item.data.update()
        bpy.context.view_layer.update()
    item["af_item_type"] = typ          # -> glTF extras; attach_prop reads it (shield fitting)
    item["af_item_style"] = c.style
    item["af_forward"] = forward
    for (name, loc, rot3, kind, extras) in c.sockets:
        L.add_item_socket(item, name, Rf @ (loc + shift), Rf @ rot3, kind=kind, size=0.04,
                          frame="grip: +Z thumb side / along shaft, -Y edge-face" if kind == "grip" else
                          "fx: +Z emission direction", **extras)
    bpy.context.view_layer.update()
    lo, hi = af.world_bbox([item])
    tris = af.tri_count([item])
    two = typ in TWO_HANDED
    budget = int(params.get("budget_tris", TWO_HANDED_BUDGET if two else ONE_HANDED_BUDGET))
    sockets = [L.socket_record(ch) for ch in sorted(item.children, key=lambda o: o.name)]
    mats = []
    for slot in item.material_slots:
        m = slot.material
        bsdf = next(n for n in m.node_tree.nodes if n.type == "BSDF_PRINCIPLED")
        mats.append({"name": m.name, "role": m.get("af_role"), "principled_to_output": L.principled_ok(m),
                     "base_color_linear": [round(x, 4) for x in bsdf.inputs["Base Color"].default_value[:3]],
                     "metallic": round(bsdf.inputs["Metallic"].default_value, 3),
                     "roughness": round(bsdf.inputs["Roughness"].default_value, 3),
                     "emission": round(bsdf.inputs["Emission Strength"].default_value, 3)})
    info = {"type": typ, "style": c.style, "seed": c.seed, "ornament": c.orn,
            "hands": 2 if any(s[0] == "socket_grip_off" for s in c.sockets) else 1,
            "budget_class": "two_handed/staff/shield" if two else "one_handed",
            "params": dict(params), "resolved": c.resolved, "tris": tris, "budget_tris": budget,
            "dimensions_m": [round(x, 4) for x in (hi - lo)], "bbox_min": list(lo), "bbox_max": list(hi),
            "parts": len(c.parts), "materials": mats, "sockets": sockets,
            "uv_layers": [u.name for u in item.data.uv_layers],
            "non_manifold_edges": L.non_manifold_edges(item),
            "axes": {"up": "+Z", "main_axis": "+Z", "edge_or_face": forward, "forward": forward,
                     "origin": params.get("origin", "bbox"),
                     "gltf": "export_yup: +Z(Blender) -> +Y(glTF), -Y(Blender) -> +Z(glTF)"}}
    info["params"].pop("batch", None)
    return item, info


def item_checks(item, info):
    names = {s["name"] for s in info["sockets"]}
    checks = [af.check("tris_within_budget", info["tris"] <= info["budget_tris"], info["tris"], info["budget_tris"]),
              af.check("socket_grip_main", "socket_grip_main" in names, sorted(names), "socket_grip_main"),
              af.check("uv_unwrapped", bool(info["uv_layers"]), info["uv_layers"], ">=1"),
              af.check("on_ground", abs(info["bbox_min"][2]) < 1e-4, round(info["bbox_min"][2], 6), 0.0)]
    if info["type"] in ("staff", "wand", "blaster_pistol", "blaster_rifle", "spear", "halberd", "torch", "bow", "crossbow"):
        checks.append(af.check("socket_fx_tip", "socket_fx_tip" in names, sorted(names), "socket_fx_tip"))
    if info["type"] in ("greatsword", "battle_axe", "warhammer", "spear", "halberd", "staff", "crossbow", "blaster_rifle"):
        checks.append(af.check("socket_grip_off", "socket_grip_off" in names, sorted(names), "socket_grip_off"))
    gm = next((s for s in info["sockets"] if s["name"] == "socket_grip_main"), None)
    if gm:
        z = Vector(gm["z_axis"])
        y = Vector(gm["y_axis"])
        x = Vector(gm["x_axis"])
        checks.append(af.check("grip_frame_right_handed", abs(x.cross(y).dot(z) - 1) < 1e-3,
                               round(x.cross(y).dot(z), 4), 1.0))
    bad = [m["name"] for m in info["materials"] if not m.get("principled_to_output")]
    checks.append(af.check("materials_principled", bool(info["materials"]) and not bad,
                           {"slots": len(info["materials"]), "not_principled": bad}, "every slot: Principled BSDF -> Material Output",
                           note="the glTF exporter only writes PBR values from a Principled BSDF wired to the output"))
    dims = info["dimensions_m"]
    checks.append(af.check("plausible_size", 0.05 < max(dims) < 2.6, dims, "0.05..2.6 m", severity="warning"))
    return checks


def export_item(item, out_dir, info, preview=True, preview_size=384):
    os.makedirs(out_dir, exist_ok=True)
    objs = [item] + list(item.children)
    glb = af.export_glb(os.path.join(out_dir, "item.glb"), objects=objs, animations=False, skins=False)
    summ = L.glb_summary(glb)
    info["glb"] = {"sockets": sorted(summ["sockets"]), "materials": summ["materials"], "bytes": summ["bytes"]}
    outputs = {"glb": glb}
    if preview:
        tilt = HERO_ROT.get(info["type"], 25)
        views = ("left", "front", "three_quarter")
        if info["type"].startswith("shield") or info["type"] == "buckler":
            views = ("front", "left", "three_quarter")
        outputs["preview"] = af.render_views(os.path.join(out_dir, "preview.png"), [item], views=views,
                                             size=preview_size)
        outputs["tile"], outputs["tile_extent"] = L.render_tile(os.path.join(out_dir, "_tile.png"), [item],
                                                                "three_quarter", 320, rot_z_deg=tilt)
    outputs["blend"] = af.save_blend(os.path.join(out_dir, "item.blend"))
    with open(os.path.join(out_dir, "item.json"), "w", encoding="utf-8") as fh:
        json.dump(af._jsonable(info), fh, ensure_ascii=False, indent=2)
    outputs["json"] = os.path.join(out_dir, "item.json")
    return outputs


def run(req):
    params = dict(req["params"])
    dims = params.pop("dims", None)  # server shape: per-type dimensions as one object
    if isinstance(dims, dict):
        params.update(dims)
    batch = params.pop("batch", None)
    if isinstance(batch, (int, float)) and not isinstance(batch, bool):  # server shape: N neighbouring seeds
        n = int(batch)
        batch = [{"seed": int(params.get("seed", 0)) + k} for k in range(n)] if n > 1 else None
    out_dir = req["out_dir"]
    if not batch:
        af.reset_scene()
        if not params.get("type"):
            raise ValueError("params.type is required (one of %s)" % ", ".join(TYPES))
        item, info = build_item(params)
        checks = item_checks(item, info)
        outputs = export_item(item, out_dir, info, preview=params.get("preview", True),
                              preview_size=int(params.get("preview_size", 384)))
        outputs.pop("tile", None)
        outputs.pop("tile_extent", None)
        af.log("item built", type=info["type"], tris=info["tris"])
        return {"outputs": outputs, "stats": {"type": info["type"], "style": info["style"], "tris": info["tris"],
                                              "budget_tris": info["budget_tris"], "dimensions_m": info["dimensions_m"],
                                              "sockets": [s["name"] for s in info["sockets"]],
                                              "materials": len(info["materials"])},
                "checks": checks, "warnings": []}
    variants = []
    tiles, labels, extents = [], [], []
    all_checks = []
    real_out = os.path.realpath(out_dir)
    used_names = set()
    for i, var in enumerate(batch):
        p = copy.deepcopy(params)
        p.update(var)
        af.reset_scene()
        item, info = build_item(p)
        name = p.get("name") or "%02d_%s_%s_s%d" % (i, info["type"], info["style"], info["seed"])
        # contract §3: a variant folder never leaves out_dir ('../x', 'a/b', 'C:\\x' -> one safe name)
        name = re.sub(r"[^A-Za-z0-9_-]", "_", str(name)).strip("_") or "variant_%02d" % i
        if name in used_names:
            name = "%s_%02d" % (name, i)
        used_names.add(name)
        sub = os.path.join(out_dir, name)
        real_sub = os.path.realpath(sub)
        if os.path.commonpath([real_out, real_sub]) != real_out or real_sub == real_out:
            raise ValueError("variant folder %r escapes out_dir" % name)
        outs = export_item(item, sub, info, preview=p.get("preview", True), preview_size=int(p.get("preview_size", 320)))
        checks = item_checks(item, info)
        for ch in checks:
            ch["name"] = "%s:%s" % (name, ch["name"])
        all_checks.extend(checks)
        if outs.get("tile"):
            tiles.append(L.load_png(outs["tile"]))
            extents.append(outs.get("tile_extent", 1.0))
            lab2 = "%dT O%d %.2fM" % (info["tris"], info["ornament"], max(info["dimensions_m"]))
            labels.append([("%s %s" % (info["type"], info["resolved"].get("head", ""))).strip(), lab2])
        variants.append({"name": name, "type": info["type"], "style": info["style"], "seed": info["seed"],
                         "tris": info["tris"], "budget_tris": info["budget_tris"], "dir": sub,
                         "glb": outs["glb"], "blend": outs["blend"], "preview": outs.get("preview"),
                         "dimensions_m": info["dimensions_m"], "resolved": info["resolved"],
                         "sockets": [s["name"] for s in info["sockets"]]})
        af.log("variant built", name=name, tris=info["tris"])
    # the pipeline (ForgePipeline.advanceState) takes the first glb / blend of a list as the next input
    outputs = {"variants_dir": out_dir, "glb": [v["glb"] for v in variants], "blend": [v["blend"] for v in variants]}
    if tiles:
        # common scale (default when every variant has the same type): lengths compare across tiles
        mode = params.get("sheet_scale") or ("common" if len({v["type"] for v in variants}) == 1 else "fit")
        scales = [e / max(extents) for e in extents] if mode == "common" else None
        outputs["contact_sheet"] = L.contact_sheet(tiles, labels, os.path.join(out_dir, "contact_sheet.png"),
                                                   cols=int(params.get("sheet_cols", 5)),
                                                   tile=int(params.get("sheet_tile", 256)), scales=scales,
                                                   title=params.get("sheet_title", "ASTRAFORGE ITEM KIT"))
        outputs["preview"] = outputs["contact_sheet"]
    elif variants and variants[0].get("preview"):
        outputs["preview"] = variants[0]["preview"]
    with open(os.path.join(out_dir, "batch.json"), "w", encoding="utf-8") as fh:
        json.dump(af._jsonable({"variants": variants}), fh, ensure_ascii=False, indent=2)
    outputs["batch_json"] = os.path.join(out_dir, "batch.json")
    outputs["json"] = outputs["batch_json"]
    return {"outputs": outputs, "stats": {"variants": len(variants),
                                          "tris": {v["name"]: v["tris"] for v in variants}},
            "checks": all_checks, "warnings": []}
