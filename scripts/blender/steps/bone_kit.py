"""bone_kit — game-ready skeletal parts for undead characters (contract §3-§6).

Request: params.part one of PARTS
  skeleton_hand  side l|r (default r), pose open|relaxed|claw|fist|grip (default open), length_m (wrist
                 crease to middle fingertip, default 0.19 for a 1.8 m figure; everything scales from it),
                 finger_spread_deg, curl (0..1: one number, a list of 5 [thumb..pinky] or {finger: c}),
                 grip_radius_m (0.018; the grip pose wraps the fingers around that cylinder and every pose
                 puts socket_mount_hand_<side> on its axis), rings (fingers wearing a gold ring, e.g.
                 ["thumb", "index", "middle"]), style clean|aged, seed, origin wrist (default) | ground.
                 27 bones as lathe/loft meshes: 8 carpals, 5 metacarpals, 14 phalanges, small joint gaps.
                 Armature with Mixamo names mixamorig:<Left|Right>Hand + Hand{Thumb..Pinky}{1..4} (21
                 bones), rest pose = generated pose, every piece weighted 1.0 to exactly one bone.
                 Hand frame: wrist joint at the origin, fingers hang along -Z, thumb toward -Y (character
                 forward, contract §4), palm toward the body (+X for the right hand, -X for the left).
  bone_staff     length_m (2.05), grip_fraction (0.62), grip_off_fraction (0.45), segments 3|4,
                 shaft_radius_m (0.018), crystal_height_m (0.21, clamped 0.18..0.24), crystal_radius_m,
                 talons 3|4, bend (0..2), style, seed, origin bbox|axis. Item frame (§6): stands on z = 0,
                 shaft +Z, front talon -Y; socket_grip_main / socket_grip_off on the shaft centre line
                 (+Z = shaft tangent), socket_fx_tip at the crystal centre.
  bone_toggle    length_m (0.10), gold rings|caps|none, loop (true), style, seed. Origin = centre of
                 the back face, local -Y = outward surface normal, bone along X.
  params.batch   list of param dicts (merged over the top-level params) or a count of neighbouring
                 seeds -> one sub-folder per variant + contact_sheet.png + batch.json.
  params.preview true (default), preview_size (384).
Outputs (single): part.glb, part.blend, part.json, preview.png.
"""
import copy
import json
import math
import os
import random
import re

import bpy
import numpy as np
from mathutils import Matrix, Vector

import af_common as af
from steps import _bone_kit_lib as B

PARTS = ("skeleton_hand", "bone_staff", "bone_toggle")
BUDGET = {"skeleton_hand": 3000, "bone_staff": 4500, "bone_toggle": 600}
FINGERS = ("thumb", "index", "middle", "ring", "pinky")
MIX = {"thumb": "Thumb", "index": "Index", "middle": "Middle", "ring": "Ring", "pinky": "Pinky"}
SIDE_WORD = {"l": "Left", "r": "Right"}
POSES = ("open", "relaxed", "claw", "fist", "grip")
GRIP_FRAME = "grip: +Z out of the fist on the thumb side / along the shaft, -Y along the knuckles (fingers), X = Y x Z"


def hand_bone_names(side):
    w = SIDE_WORD[side]
    names = ["mixamorig:%sHand" % w]
    for f in FINGERS:
        names += ["mixamorig:%sHand%s%d" % (w, MIX[f], k) for k in range(1, 5)]
    return names


def bname(side, finger=None, k=None):
    w = SIDE_WORD[side]
    if finger is None:
        return "mixamorig:%sHand" % w
    return "mixamorig:%sHand%s%d" % (w, MIX[finger], k)


# =============================================================================== hand anatomy
# Nominal hand (lengths in units of the hand length H = wrist crease -> middle fingertip). Build frame
# B of a LEFT hand: x radial (thumb side), y distal (along the fingers), z dorsal (back of the hand).
# Metacarpal and phalanx lengths follow adult means (19 cm hand: MC2 6.6 cm, MC3 6.4, MC4 5.7, MC5 5.1,
# MC1 4.5; proximal 3.9/4.4/4.1/3.2 + thumb 3.1; middle 2.3/2.8/2.7/1.9; distal 1.7/1.9/1.85/1.6 +
# thumb 2.3 cm). The whole hand is rescaled so the straight middle chain equals length_m exactly.
HAND = {
    "index": dict(mc=0.345, ph=(0.205, 0.121, 0.089), base=(0.070, 0.160, 0.000), fan=7.0, tilt=-2.5, w=1.00),
    "middle": dict(mc=0.335, ph=(0.232, 0.147, 0.100), base=(0.022, 0.170, 0.006), fan=1.5, tilt=0.0, w=1.04),
    "ring": dict(mc=0.300, ph=(0.216, 0.142, 0.097), base=(-0.022, 0.168, 0.000), fan=-5.0, tilt=-2.5, w=0.95),
    "pinky": dict(mc=0.270, ph=(0.168, 0.100, 0.084), base=(-0.064, 0.158, -0.010), fan=-11.0, tilt=-5.5, w=0.84),
    "thumb": dict(mc=0.237, ph=(0.163, 0.121), base=(0.110, 0.122, -0.036), w=1.18),
}
S_MC = 0.0265                      # metacarpal shaft half-width (H units)
S_PH = (0.0297, 0.0248, 0.0198)    # proximal / middle / distal phalanx shaft half-widths
GAP = 0.0065                       # joint gap (1.2 mm on a 19 cm hand)
CARPALS = [  # name, centre, radii (H units), rotation about z (deg)
    ("scaphoid", (0.078, 0.052, -0.006), (0.034, 0.048, 0.028), 32),
    ("lunate", (0.022, 0.046, 0.006), (0.028, 0.032, 0.030), 0),
    ("triquetrum", (-0.030, 0.052, 0.002), (0.026, 0.028, 0.026), -15),
    ("pisiform", (-0.042, 0.062, -0.036), (0.016, 0.018, 0.016), 0),
    ("trapezium", (0.094, 0.112, -0.016), (0.028, 0.029, 0.026), 25),
    ("trapezoid", (0.058, 0.124, 0.002), (0.020, 0.026, 0.024), 0),
    ("capitate", (0.018, 0.118, 0.003), (0.025, 0.042, 0.029), 0),
    ("hamate", (-0.030, 0.114, -0.002), (0.026, 0.038, 0.028), -8),
]
JOINT_MAX = {"mcp": 95.0, "pip": 110.0, "dip": 85.0, "t_mcp": 70.0, "t_ip": 90.0}
# finger poses: default curl per finger (thumb..pinky), spread (deg), thumb (abduction radial, palmar,
# pronation, CMC flexion) in degrees; thumb MCP / IP flexion come from the thumb curl
POSE_TABLE = {
    "open": dict(curl=(0.15, 0.17, 0.20, 0.24, 0.28), spread=9.0, thumb=(8.0, 20.0, 78.0, 0.0)),
    "relaxed": dict(curl=(0.25, 0.24, 0.32, 0.40, 0.48), spread=4.0, thumb=(28.0, 34.0, 74.0, 6.0)),
    "claw": dict(curl=(0.55, 0.70, 0.72, 0.74, 0.76), spread=15.0, thumb=(44.0, 26.0, 64.0, 0.0)),
    "fist": dict(curl=(1.0, 1.0, 1.0, 1.0, 1.0), spread=2.0, thumb=None),
    "grip": dict(curl=None, spread=None, thumb=None),
}


def joff(c_prev, prof):
    """Distance from a joint's rotation centre to the next bone's base rim: the head radius of the
    proximal bone + the joint gap - the depth of the next bone's base dish (the rim surrounds the head)."""
    return c_prev + GAP - prof["pole0"][0]


def st(y, w, d, **kw):
    out = {"y": y, "w": w, "d": d}
    out.update(kw)
    return out


def mc_profile(L, s):
    # the head is a sphere (radius c) around the MCP rotation centre, so the phalanx base can roll
    # around it in flexion / abduction without cutting into it
    c, ch = 1.25 * s, -0.20 * s
    S = [st(0.0, 1.08 * s, 1.40 * s, sq=0.08), st(0.5 * s, 1.14 * s, 1.52 * s, sq=0.10),
         st(1.6 * s, 0.98 * s, 1.08 * s), st(0.55 * L, 0.92 * s, 0.95 * s, ridge=0.07, ridge_at=math.pi / 2),
         st(L - 2.35 * s, 1.02 * s, 1.08 * s, cb=-0.10 * s),
         st(L - 1.70 * s, 1.17 * s, 1.22 * s, cb=ch),
         st(L - c, 1.25 * s, 1.30 * s, cb=ch),
         st(L - 0.55 * s, 1.03 * s, 1.06 * s, cb=ch),
         st(L - 0.18 * s, 0.64 * s, 0.66 * s, cb=ch)]
    return dict(st=S, pole0=(-0.15 * s, 0.0, 0.0), pole1=(L, 0.0, ch), c=c, cb_head=ch, bow=0.15 * s)


def phalanx_profile(L, s, short=False):
    S = [st(0.0, 1.12 * s, 1.02 * s, lobe=0.04, flat=0.15, cb=-0.06 * s),
         st(0.45 * s, 1.22 * s, 1.12 * s, lobe=0.05, flat=0.25, cb=-0.06 * s),
         st(1.35 * s, 1.10 * s, 0.90 * s, flat=0.40)]
    S += [st(0.57 * L, 0.98 * s, 0.78 * s, flat=0.48)] if short else \
        [st(0.46 * L, 0.97 * s, 0.78 * s, flat=0.50), st(0.68 * L, 1.00 * s, 0.78 * s, flat=0.45)]
    S += [st(L - 1.55 * s, 1.14 * s, 0.84 * s, lobe=0.12, flat=0.30, cb=-0.03 * s),
          st(L - 0.85 * s, 1.22 * s, 0.92 * s, lobe=0.22, flat=0.20, cb=-0.05 * s),
          st(L - 0.30 * s, 0.98 * s, 0.72 * s, lobe=0.18, flat=0.15, cb=-0.06 * s)]
    return dict(st=S, pole0=(0.42 * s, 0.0, -0.04 * s), pole1=(L, 0.0, -0.08 * s), c=0.80 * s, cb_head=0.0,
                bow=0.12 * s)


def distal_profile(L, s):
    S = [st(0.0, 1.22 * s, 0.98 * s, flat=0.15, cb=-0.05 * s),
         st(0.42 * s, 1.32 * s, 1.04 * s, flat=0.25, cb=-0.05 * s),
         st(1.25 * s, 0.98 * s, 0.80 * s, flat=0.35),
         st(0.55 * L, 0.74 * s, 0.62 * s, flat=0.40),
         st(L - 0.95 * s, 0.95 * s, 0.56 * s, flat=0.55, cb=-0.04 * s),
         st(L - 0.35 * s, 0.72 * s, 0.45 * s, flat=0.50, cb=-0.06 * s)]
    return dict(st=S, pole0=(0.40 * s, 0.0, -0.04 * s), pole1=(L, 0.0, -0.12 * s), c=0.0, cb_head=0.0,
                bow=0.06 * s)


def claw_profile(L, s):
    """Pointed claw (last talon segment of the staff head)."""
    S = [st(0.0, 1.15 * s, 1.00 * s, flat=0.10, cb=-0.04 * s),
         st(0.42 * s, 1.25 * s, 1.06 * s, flat=0.15, cb=-0.04 * s),
         st(1.25 * s, 0.98 * s, 0.86 * s, flat=0.20),
         st(0.50 * L, 0.66 * s, 0.58 * s, flat=0.25),
         st(0.78 * L, 0.36 * s, 0.32 * s, flat=0.20)]
    return dict(st=S, pole0=(0.40 * s, 0.0, -0.04 * s), pole1=(L, 0.0, -0.05 * s), c=0.0, cb_head=0.0,
                bow=0.10 * s)


def _finish_profile(prof, L, jitter):
    """Seeded width jitter + bow (dorsal convexity) on the inner stations; drops stations whose y is not
    increasing (very short bones)."""
    out = []
    last = -1e9
    n = len(prof["st"])
    for i, s in enumerate(prof["st"]):
        if s["y"] <= last + 1e-6:
            continue
        s = dict(s)
        s["w"] *= jitter[0]
        s["d"] *= jitter[1]
        if 0 < i < n - 1:
            s["cb"] = s.get("cb", 0.0) + prof["bow"] * math.sin(math.pi * B.clamp(s["y"] / L, 0, 1))
        out.append(s)
        last = s["y"]
    prof = dict(prof)
    prof["st"] = out
    return prof


def palmar_samples(prof, offset):
    """Local (x, y, z) surface samples of a bone (8 per station: the palmar half densely, the dorsal
    half sparsely), shifted by offset along y (cached). Used for contact / clearance tests."""
    if "_palm" not in prof:
        pts = []
        for s in prof["st"]:
            sec = B.section(12, s["w"], s["d"], s.get("lobe", 0.0), s.get("flat", 0.0), s.get("tri", 0.0),
                            s.get("sq", 0.0), s.get("ridge", 0.0), s.get("ridge_at", -math.pi / 2))
            for i in (0, 2, 4, 6, 7, 8, 9, 10, 11):
                a, b = sec[i]
                pts.append((a, s["y"], s.get("cb", 0.0) + b))
        prof["_palm"] = np.array(pts)
    out = prof["_palm"].copy()
    out[:, 1] += offset
    return out


def centre_samples(prof, offset):
    """Local (y, z, radius) along the centre line (for capsule clearance between digits)."""
    return [(offset + s["y"], s.get("cb", 0.0), max(s["w"], s["d"]) * 0.95) for s in prof["st"]]


class Hand:
    def __init__(self, p):
        self.p = p
        self.side = p.get("side", "r")
        if self.side not in SIDE_WORD:
            raise ValueError("side must be 'l' or 'r'")
        self.pose = p.get("pose", "open")
        if self.pose not in POSES:
            raise ValueError("pose must be one of %s" % ", ".join(POSES))
        self.H = float(p.get("length_m", 0.19))
        if not 0.02 < self.H < 2.0:
            raise ValueError("length_m out of range: %r" % self.H)
        self.seed = int(p.get("seed", 0))
        self.rng = random.Random(self.seed * 7919 + 1013)
        self.Rg_m = float(p.get("grip_radius_m", 0.018))
        self.style = p.get("style", "clean")
        if self.style not in ("clean", "aged"):
            raise ValueError("style must be clean or aged")
        self.rings = [r for r in (p.get("rings") or [])]
        for r in self.rings:
            if r not in FINGERS:
                raise ValueError("rings: unknown finger %r" % r)
        self.warnings = []
        self._dims()

    # ------------------------------------------------------------------ dimensions
    def _dims(self):
        rng = self.rng
        self.D = {}
        for f in FINGERS:
            h = HAND[f]
            lj = [1.0 + 0.03 * (rng.random() - 0.5) for _ in range(4)]
            wj = (1.0 + 0.06 * (rng.random() - 0.5), 1.0 + 0.06 * (rng.random() - 0.5))
            d = {"L_mc": h["mc"] * lj[0], "base": Vector(h["base"])}
            if f == "thumb":
                s_mc = S_MC * h["w"]
                d["R_mc"] = Matrix.Identity(3)
                d["mc"] = _finish_profile(mc_profile(d["L_mc"], s_mc), d["L_mc"], wj)
                Lp, Ld = h["ph"][0] * lj[1], h["ph"][1] * lj[2]
                d["L"] = (Lp, Ld)
                d["prof"] = (_finish_profile(phalanx_profile(Lp, S_PH[0] * h["w"]), Lp, wj),
                             _finish_profile(distal_profile(Ld, S_PH[2] * h["w"] * 1.06), Ld, wj))
            else:
                s_mc = S_MC * h["w"]
                d["R_mc"] = B.rot_z(-h["fan"]) @ B.rot_x(h["tilt"])
                d["mc"] = _finish_profile(mc_profile(d["L_mc"], s_mc), d["L_mc"], wj)
                L3 = tuple(h["ph"][k] * lj[k + 1] for k in range(3))
                d["L"] = L3
                d["prof"] = (_finish_profile(phalanx_profile(L3[0], S_PH[0] * h["w"]), L3[0], wj),
                             _finish_profile(phalanx_profile(L3[1], S_PH[1] * h["w"], short=True), L3[1], wj),
                             _finish_profile(distal_profile(L3[2], S_PH[2] * h["w"]), L3[2], wj))
            d["fan"] = h.get("fan", 0.0)
            self.D[f] = d
        segs, tip = self.finger_fk("middle", (0.0, 0.0, 0.0, 0.0))
        self.chain_nominal = tip.length
        self.u = self.H / self.chain_nominal          # metres per nominal unit
        self.Rg = self.Rg_m / self.u                  # grip radius in nominal units
        self.contact_gap = 0.0006 / self.u * (self.H / 0.19)
        self.w_palm = 2.0      # grip solver: pull the cylinder into the palm
        self.w_joint = 2e-7    # grip solver: joint distribution term (per deg^2, nominal units^2)

    # ------------------------------------------------------------------ kinematics
    def mcp_joint(self, f):
        d = self.D[f]
        Rm = d["R_mc"]
        pr = d["mc"]
        return d["base"] + Rm.col[1] * (d["L_mc"] - pr["c"]) + Rm.col[2] * pr["cb_head"]

    def finger_fk(self, f, ang):
        """ang = (spread_radial, mcp, pip, dip) degrees. Returns (segments, tip). Segment: dict(kind,
        prof, origin, R, J (joint centre at the base), L)."""
        d = self.D[f]
        Rm = d["R_mc"]
        segs = [dict(kind="mc", prof=d["mc"], origin=d["base"].copy(), R=Rm.copy(), J=d["base"].copy(), L=d["L_mc"])]
        J = self.mcp_joint(f)
        c_prev = d["mc"]["c"]
        R = Rm @ B.rot_z(-ang[0])
        for k, kind in enumerate(("prox", "mid", "dist")):
            R = R @ B.rot_x(-ang[k + 1])
            prof = d["prof"][k]
            origin = J + R.col[1] * joff(c_prev, prof)
            segs.append(dict(kind=kind, prof=prof, origin=origin, R=R.copy(), J=J.copy(), L=d["L"][k]))
            J = origin + R.col[1] * (d["L"][k] - prof["c"])
            c_prev = prof["c"]
        tip = segs[-1]["origin"] + segs[-1]["R"].col[1] * d["L"][2]
        return segs, tip

    def thumb_fk(self, ang):
        """ang = (abduction radial, palmar abduction, pronation, CMC flexion, MCP flexion, IP flexion)."""
        d = self.D["thumb"]
        a_r, a_p, psi, f_cmc, f_mcp, f_ip = ang
        J = d["base"].copy()
        R = B.rot_z(-a_r) @ B.rot_x(-a_p) @ B.rot_y(psi) @ B.rot_x(-f_cmc)
        g_cmc = GAP * 0.8
        segs = [dict(kind="mc", prof=d["mc"], origin=J + R.col[1] * g_cmc, R=R.copy(), J=J.copy(), L=d["L_mc"])]
        J = segs[0]["origin"] + R.col[1] * (d["L_mc"] - d["mc"]["c"]) + R.col[2] * d["mc"]["cb_head"]
        c_prev = d["mc"]["c"]
        for k, (kind, fl) in enumerate((("prox", f_mcp), ("dist", f_ip))):
            R = R @ B.rot_x(-fl)
            prof = d["prof"][k]
            origin = J + R.col[1] * joff(c_prev, prof)
            segs.append(dict(kind=kind, prof=prof, origin=origin, R=R.copy(), J=J.copy(), L=d["L"][k]))
            J = origin + R.col[1] * (d["L"][k] - prof["c"])
            c_prev = prof["c"]
        tip = segs[-1]["origin"] + segs[-1]["R"].col[1] * d["L"][1]
        return segs, tip

    # ------------------------------------------------------------------ cylinder helpers
    def _cyl(self, C0, A):
        self.C0 = Vector(C0)
        self.A = Vector(A).normalized()

    def _dist_axis(self, P):
        """P: (..., 3) numpy -> distance to the cylinder axis."""
        C0 = np.array(self.C0)
        A = np.array(self.A)
        v = P - C0
        return np.linalg.norm(v - (v @ A)[..., None] * A, axis=-1)

    def _touch(self, J, R0, samples, lo, hi, step=0.5):
        """Smallest flexion (deg) in [lo, hi] about R0's x for which the segment (palmar sample points,
        local (y, z) relative to J after the flexion) touches the cylinder. Returns (theta, clearance)."""
        th = np.radians(np.arange(lo, hi + 1e-9, step))
        x = samples[:, 0]
        a = samples[:, 1]
        b = samples[:, 2]
        c, s = np.cos(th)[:, None], np.sin(th)[:, None]
        y2 = a[None, :] * c + b[None, :] * s
        z2 = -a[None, :] * s + b[None, :] * c
        Xc, Yc, Zc = np.array(R0.col[0]), np.array(R0.col[1]), np.array(R0.col[2])
        P = np.array(J)[None, None, :] + x[None, :, None] * Xc + y2[..., None] * Yc + z2[..., None] * Zc
        clr = (self._dist_axis(P) - self.Rg - self.contact_gap).min(axis=1)
        hit = np.where(clr <= 0)[0]
        if len(hit) == 0:
            return float(hi), float(clr[-1])
        i = int(hit[0])
        if i == 0:
            return float(lo), float(clr[0])
        # linear interpolation of the crossing between steps i-1 and i
        t = clr[i - 1] / max(clr[i - 1] - clr[i], 1e-12)
        theta = math.degrees(th[i - 1] + (th[i] - th[i - 1]) * t)
        return theta, 0.0

    def _seg_clearance(self, seg, offset=0.0):
        pts = palmar_samples(seg["prof"], offset)
        R = np.array(seg["R"])
        P = np.array(seg["origin"])[None, :] + pts @ R.T
        return float((self._dist_axis(P) - self.Rg - self.contact_gap).min())

    def wrap_finger(self, f):
        """Grip: spread cancels the metacarpal fan (flexion plane perpendicular to the axis), then MCP,
        PIP, DIP each flex until their phalanx touches the cylinder (tangent wrapping)."""
        d = self.D[f]
        # keep 40 % of the metacarpal fan: flexion planes nearly perpendicular to the axis, the digits still
        # diverge a little so knuckles and rings of neighbouring fingers never touch
        spread = -0.6 * d["fan"] - float(self.p.get("grip_tilt_deg", 0.0))
        Rm = d["R_mc"] @ B.rot_z(-spread)
        J = self.mcp_joint(f)
        c_prev = d["mc"]["c"]
        R = Rm
        angles = [spread]
        clears = []
        lims = (JOINT_MAX["mcp"], JOINT_MAX["pip"], JOINT_MAX["dip"])
        for k in range(3):
            prof = d["prof"][k]
            smp = palmar_samples(prof, joff(c_prev, prof))
            if k == 0 and f in self.rings:
                ya, r_in, r_out, wid, cbm = self.ring_spec(f)
                t = np.linspace(0, 2 * np.pi, 24, endpoint=False)
                rb = r_out
                ring = np.stack([rb * np.cos(t), np.full(24, ya + joff(c_prev, prof)), cbm + rb * np.sin(t)], 1)
                smp = np.concatenate([smp, ring], 0)
            th, clr = self._touch(J, R, smp, 0.0, lims[k])
            angles.append(th)
            clears.append(clr)
            R = R @ B.rot_x(-th)
            origin = J + R.col[1] * joff(c_prev, prof)
            J = origin + R.col[1] * (d["L"][k] - prof["c"])
            c_prev = prof["c"]
        return tuple(angles), clears

    def ring_spec(self, f):
        """Gold ring on the proximal phalanx of finger f: (y along the bone, r_in, r_out, width, cb)."""
        d = self.D[f]
        pr = d["prof"][0]
        L = d["L"][0]
        # neighbouring rings are staggered along the bone so they never stack side by side in a grip
        stagger = {"thumb": 0.0, "index": -0.05, "middle": 0.05, "ring": -0.05, "pinky": 0.05}[f]
        ya = L * (float(self.p.get("ring_at", 0.66)) + stagger)
        sts = pr["st"]
        w = cb = None
        for a, b in zip(sts, sts[1:]):
            if a["y"] <= ya <= b["y"]:
                t = (ya - a["y"]) / max(b["y"] - a["y"], 1e-9)
                w = max(B.lerp(a["w"], b["w"], t), B.lerp(a["d"], b["d"], t))
                cb = B.lerp(a.get("cb", 0.0), b.get("cb", 0.0), t)
        if w is None:
            w, cb = max(sts[len(sts) // 2]["w"], sts[len(sts) // 2]["d"]), 0.0
        r_in = w * 1.02 + 0.0016             # nominal units (H = 1): ~0.3 mm clearance
        return ya, r_in, r_in + 0.0032, 0.019, cb

    def mc_clearance(self):
        out = 1e9
        for f in ("index", "middle", "ring", "pinky"):
            d = self.D[f]
            seg = dict(prof=d["mc"], origin=d["base"], R=d["R_mc"])
            out = min(out, self._seg_clearance(seg, 0.0))
        return out

    def solve_cylinder(self):
        """Place the grip cylinder (axis along the knuckle row, optional tilt) so the fingers wrap it with
        the smallest stand-off and the palm (metacarpal heads) does not cut into it."""
        tilt = float(self.p.get("grip_tilt_deg", 0.0))
        A = B.rot_z(tilt) @ Vector((1, 0, 0))
        Lp = self.D["middle"]["L"][0]
        J3 = self.mcp_joint("middle")
        zp = min(self._palm_z(f) for f in ("index", "middle", "ring", "pinky"))

        def energy(yc, zc):
            self._cyl((0.0, yc, zc), A)
            e = 0.0
            for f in ("index", "middle", "ring", "pinky"):
                ang, clears = self.wrap_finger(f)
                for c in clears:
                    e += c * c if c >= 0 else 100.0 * c * c
                # a power grip spreads the flexion over the three joints (no hook grip with a straight
                # proximal phalanx and a flat DIP)
                e += self.w_joint * (max(0.0, 35.0 - ang[1]) ** 2 + max(0.0, 15.0 - ang[3]) ** 2)
            cm = self.mc_clearance()
            e += (1000.0 * cm * cm) if cm < 0 else self.w_palm * cm * cm
            return e
        best = None
        ys = J3.y + np.linspace(-0.3, 0.8, 12) * Lp
        zs = zp - self.Rg - np.linspace(-0.05, 0.7, 12) * Lp
        for yc in ys:
            for zc in zs:
                e = energy(yc, zc)
                if best is None or e < best[0]:
                    best = (e, yc, zc)
        dy, dz = (ys[1] - ys[0]), (zs[0] - zs[1])
        for _ in range(3):
            e0, y0, z0 = best
            for yc in y0 + np.linspace(-1, 1, 7) * dy:
                for zc in z0 + np.linspace(-1, 1, 7) * dz:
                    e = energy(yc, zc)
                    if e < best[0]:
                        best = (e, yc, zc)
            dy, dz = dy / 3, dz / 3
        self._cyl((0.0, best[1], best[2]), A)
        self.cyl_energy = best[0]
        return self.C0, self.A

    def _palm_z(self, f):
        d = self.D[f]
        seg = dict(prof=d["mc"], origin=d["base"], R=d["R_mc"])
        pts = palmar_samples(seg["prof"], 0.0)
        P = np.array(seg["origin"])[None, :] + pts @ np.array(seg["R"]).T
        return float(P[:, 2].min())

    # ------------------------------------------------------------------ thumb
    def _finger_capsules(self, segs_by_f, kinds=("prox", "mid", "dist")):
        pts, rad = [], []
        for f, segs in segs_by_f.items():
            for sg in segs:
                if sg["kind"] not in kinds:
                    continue
                for (y, z, r) in centre_samples(sg["prof"], 0.0):
                    pts.append(tuple(sg["origin"] + sg["R"].col[1] * y + sg["R"].col[2] * z))
                    rad.append(r)
        return np.array(pts), np.array(rad)

    def _thumb_capsules(self, segs):
        pts, rad = [], []
        for sg in segs:
            for (y, z, r) in centre_samples(sg["prof"], 0.0):
                pts.append(tuple(sg["origin"] + sg["R"].col[1] * y + sg["R"].col[2] * z))
                rad.append(r)
        return np.array(pts), np.array(rad)

    def _collision(self, tp, tr, fp, fr):
        if not len(fp):
            return 0.0
        D = np.linalg.norm(tp[:, None, :] - fp[None, :, :], axis=-1)
        deficit = (tr[:, None] + fr[None, :] + GAP * 0.5) - D
        return float(np.sum(np.clip(deficit, 0, None) ** 2))

    def _descend(self, x0, energy, steps=(10.0, 5.0, 2.5, 1.0), bounds=None, iters=6):
        x = list(x0)
        e = energy(x)
        for st_ in steps:
            for _ in range(iters):
                improved = False
                for i in range(len(x)):
                    for sgn in (1, -1):
                        y = list(x)
                        y[i] += sgn * st_
                        if bounds:
                            y[i] = B.clamp(y[i], bounds[i][0], bounds[i][1])
                        ey = energy(y)
                        if ey < e - 1e-12:
                            x, e = y, ey
                            improved = True
                if not improved:
                    break
        return x, e

    def _ccw_angle(self, P):
        """Angle of P around the grip axis, counter-clockwise from 'above the knuckles' (rad, 0..2pi).
        The fingers wrap the other way (their tips end at ~190-230 deg); the thumb wraps toward them."""
        e1 = Vector((0, 0, 1))
        e1 = (e1 - self.A * e1.dot(self.A)).normalized()
        e2 = self.A.cross(e1)
        v = Vector(P) - self.C0
        return math.atan2(-v.dot(e2), v.dot(e1)) % (2 * math.pi)

    def thumb_grip(self, finger_segs):
        """Power grip thumb: wraps the cylinder radially beside the index finger, the opposite way to the
        fingers, its pad ending over the index middle phalanx; no contact with the fingers."""
        fp, fr = self._finger_capsules(finger_segs)
        mp, mr = self._finger_capsules(finger_segs, kinds=("mc",))
        d = self.D["thumb"]
        mi = [s for s in finger_segs["index"] if s["kind"] == "mid"][0]
        mid_i = mi["origin"] + mi["R"].col[1] * (mi["L"] * 0.5)
        target_ang = self._ccw_angle(mid_i)
        x_t = self.mcp_joint("index").x + S_PH[0] * 1.3 + S_PH[0] * HAND["thumb"]["w"] * 1.2 + GAP
        p0, p1 = d["prof"]
        s0 = palmar_samples(p0, 0.0)
        if "thumb" in self.rings:
            ya, r_in, r_out, wid, cbm = self.ring_spec("thumb")
            t = np.linspace(0, 2 * np.pi, 24, endpoint=False)
            rb = r_out
            s0 = np.concatenate([s0, np.stack([rb * np.cos(t), np.full(24, ya), cbm + rb * np.sin(t)], 1)], 0)

        def solve(x):
            a_r, a_p, psi, f_cmc = x
            segs, _ = self.thumb_fk((a_r, a_p, psi, f_cmc, 0.0, 0.0))
            c_mc = d["mc"]["c"]
            J = segs[1]["J"]
            R0 = segs[0]["R"]
            sm0 = s0.copy()
            sm0[:, 1] += joff(c_mc, p0)
            th1, cl1 = self._touch(J, R0, sm0, 0.0, JOINT_MAX["t_mcp"])
            R1 = R0 @ B.rot_x(-th1)
            o1 = J + R1.col[1] * joff(c_mc, p0)
            J2 = o1 + R1.col[1] * (d["L"][0] - p0["c"])
            th2, cl2 = self._touch(J2, R1, palmar_samples(p1, joff(p0["c"], p1)), 0.0, JOINT_MAX["t_ip"])
            return (a_r, a_p, psi, f_cmc, th1, th2), cl1, cl2

        def energy(x):
            ang, cl1, cl2 = solve(x)
            segs, tip = self.thumb_fk(ang)
            cm = self._seg_clearance(segs[0], 0.0)
            e = (100.0 * cm * cm if cm < 0 else 0.05 * cm * cm)
            for c in (cl1, cl2):
                e += c * c if c >= 0 else 100.0 * c * c
            tp, tr = self._thumb_capsules(segs[1:])
            e += 300.0 * self._collision(tp, tr, fp, fr)
            tp0, tr0 = self._thumb_capsules(segs)
            e += 100.0 * self._collision(tp0, tr0, mp, mr)
            dseg = segs[-1]
            pad = dseg["origin"] + dseg["R"].col[1] * (dseg["L"] * 0.6)
            da = self._ccw_angle(pad) - target_ang
            e += 2e-4 * da * da + 0.05 * (pad.x - x_t) ** 2
            return e
        # coarse grid over the CMC pose, then coordinate descent from the two best cells
        cells = []
        for a_r in (-10.0, 5.0, 20.0, 35.0):
            for a_p in (15.0, 35.0, 55.0, 75.0, 95.0):
                for psi in (40.0, 70.0, 100.0, 125.0):
                    for f_cmc in (-5.0, 15.0, 35.0):
                        x = (a_r, a_p, psi, f_cmc)
                        cells.append((energy(x), x))
        cells.sort(key=lambda c_: c_[0])
        best = None
        for _, start in cells[:2]:
            x, e = self._descend(start, energy, steps=(5.0, 2.5, 1.0), bounds=[(-15, 50), (0, 100), (20, 130), (-10, 50)])
            if best is None or e < best[1]:
                best = (x, e)
        ang, cl1, cl2 = solve(best[0])
        return ang, best[1]

    def thumb_fist(self, finger_segs):
        fp, fr = self._finger_capsules(finger_segs)
        mp, mr = self._finger_capsules(finger_segs, kinds=("mc",))
        mi = [s for s in finger_segs["index"] if s["kind"] == "mid"][0]
        mm = [s for s in finger_segs["middle"] if s["kind"] == "mid"][0]
        r_t = S_PH[2] * HAND["thumb"]["w"]

        def mid(sg):
            return sg["origin"] + sg["R"].col[1] * (sg["L"] * 0.5)
        dors = (mi["R"].col[2] + mm["R"].col[2]).normalized()
        T = mid(mi) * 0.6 + mid(mm) * 0.4 + dors * (S_PH[1] * 1.1 + r_t * 1.1 + GAP)

        def energy(x):
            segs, tip = self.thumb_fk(tuple(x))
            dseg = segs[-1]
            pad = dseg["origin"] + dseg["R"].col[1] * (dseg["L"] * 0.5)
            e = (pad - T).length_squared
            tp, tr = self._thumb_capsules(segs[1:])
            e += 50.0 * self._collision(tp, tr, fp, fr)
            tp0, tr0 = self._thumb_capsules(segs)
            e += 50.0 * self._collision(tp0, tr0, mp, mr)
            e += 1e-6 * (x[4] ** 2 + x[5] ** 2)
            return e
        x, e = self._descend((15.0, 40.0, 85.0, 20.0, 30.0, 30.0), energy,
                             bounds=[(-10, 70), (0, 100), (20, 130), (-10, 50), (0, JOINT_MAX["t_mcp"]),
                                     (0, JOINT_MAX["t_ip"])])
        return tuple(x), e

    def thumb_fixed(self, base, curl, finger_segs, claw=False):
        f_mcp = 55.0 * curl
        f_ip = (35.0 + 45.0 * curl) if claw else 75.0 * curl
        x0 = (base[0], base[1], base[2], base[3], f_mcp, f_ip)
        fp, fr = self._finger_capsules(finger_segs)
        mp, mr = self._finger_capsules(finger_segs, kinds=("mc",))

        def coll(x):  # thumb phalanges against the finger phalanges and metacarpals
            segs, _ = self.thumb_fk(tuple(x))
            tp, tr = self._thumb_capsules(segs[1:])
            return self._collision(tp, tr, fp, fr) + self._collision(tp, tr, mp, mr)
        if coll(x0) <= 1e-12:
            return x0
        x, _ = self._descend(x0, lambda x: 1000.0 * coll(x) + 1e-5 * sum((a - b) ** 2 for a, b in zip(x, x0)),
                             steps=(4.0, 2.0, 1.0))
        return tuple(x)

    # ------------------------------------------------------------------ pose
    def curls(self, default):
        c = self.p.get("curl")
        if c is None:
            return list(default)
        if isinstance(c, (int, float)):
            return [float(c)] * 5
        if isinstance(c, dict):
            return [float(c.get(f, default[i])) for i, f in enumerate(FINGERS)]
        c = list(c)
        if len(c) != 5:
            raise ValueError("curl list needs 5 values (thumb, index, middle, ring, pinky)")
        return [B.clamp(float(x), 0.0, 1.0) for x in c]

    def solve_pose(self):
        self.solve_cylinder()
        pose = self.pose
        tab = POSE_TABLE[pose]
        jit = lambda: 2.5 * (self.rng.random() - 0.5)   # noqa: E731 (seeded joint jitter, deg)
        self.angles = {}
        finger_segs = {}
        if pose == "grip":
            if self.p.get("curl") is not None:
                self.warnings.append("curl is ignored by the grip pose (the fingers wrap the cylinder)")
            for f in ("index", "middle", "ring", "pinky"):
                ang, _ = self.wrap_finger(f)
                self.angles[f] = ang
                finger_segs[f], _ = self.finger_fk(f, ang)
            self.angles["thumb"], self.thumb_energy = self.thumb_grip(finger_segs)
        else:
            curls = self.curls(tab["curl"])
            spread = float(self.p.get("finger_spread_deg", tab["spread"]))
            share = {"index": 1.0, "middle": 0.15, "ring": -0.55, "pinky": -1.1}
            for i, f in enumerate(("index", "middle", "ring", "pinky")):
                c = curls[i + 1]
                if pose == "claw":
                    a = (-6.0 + 16.0 * c, 100.0 * c, 80.0 * c)
                else:
                    a = (85.0 * c, 100.0 * c, 70.0 * c)
                a = tuple(B.clamp(x + jit(), -10.0, lim) for x, lim in
                          zip(a, (JOINT_MAX["mcp"], JOINT_MAX["pip"], JOINT_MAX["dip"])))
                ang = (spread * share[f] + jit(),) + a
                self.angles[f] = ang
                finger_segs[f], _ = self.finger_fk(f, ang)
            if pose == "fist":
                self.angles["thumb"], self.thumb_energy = self.thumb_fist(finger_segs)
            else:
                self.angles["thumb"] = self.thumb_fixed(tab["thumb"], curls[0], finger_segs, claw=(pose == "claw"))
        self.segs = {f: self.finger_fk(f, self.angles[f]) for f in ("index", "middle", "ring", "pinky")}
        self.segs["thumb"] = self.thumb_fk(self.angles["thumb"])

    # ------------------------------------------------------------------ build
    def world_map(self):
        MB = Matrix(((0, 0, 1), (-1, 0, 0), (0, -1, 0)))  # columns: x_B -> -Y, y_B -> -Z, z_B -> +X
        S = Matrix.Diagonal((-1.0, 1.0, 1.0)) if self.side == "r" else Matrix.Identity(3)
        return S @ MB

    def build(self, mats):
        """Create the hand mesh + armature + socket in the current scene. Returns (mesh, armature,
        socket, info)."""
        self.solve_pose()
        M3 = self.world_map() * self.u
        M = M3.to_4x4()
        pieces = []
        n = 8
        side = self.side
        # carpals -> Hand
        for (nm, c, r, rz) in CARPALS:
            jit = [1.0 + 0.10 * (self.rng.random() - 0.5) for _ in range(3)]
            v, f = B.ellipsoid(c, (r[0] * jit[0] * 1.24, r[1] * jit[1] * 1.16, r[2] * jit[2] * 1.22), B.rot_z(rz), seg=6, rings=5,
                               jitter=0.18, rng=self.rng)
            pieces.append(B.Piece("carpal_" + nm, v, f, "bone", bname(side), kind="carpal"))
        bones = []
        mcp3 = self.mcp_joint("middle")
        bones.append(dict(name=bname(side), head=Vector((0, 0, 0)), tail=mcp3, roll=Vector((0, 0, 1)), parent=None))
        for f in FINGERS:
            segs, tip = self.segs[f]
            names = ["mc", "proximal", "middle", "distal"] if f != "thumb" else ["metacarpal", "proximal", "distal"]
            for i, sg in enumerate(segs):
                R = sg["R"]
                pr = sg["prof"]
                v, fc = B.bone_from_stations(pr["st"], sg["origin"], R.col[0], R.col[1], R.col[2], n,
                                             pole0=pr["pole0"], pole1=pr["pole1"], phase=math.pi / n)
                if f == "thumb":
                    bone = bname(side, f, i + 1)
                    kind = ("metacarpal", "phalanx", "phalanx")[i]
                else:
                    bone = bname(side) if i == 0 else bname(side, f, i)
                    kind = "metacarpal" if i == 0 else "phalanx"
                label = "%s_%s" % (f, "metacarpal" if (i == 0) else names[i])
                pieces.append(B.Piece(label, v, fc, "bone", bone, kind=kind))
            # armature chain
            if f == "thumb":
                joints = [segs[0]["J"], segs[1]["J"], segs[2]["J"], tip]
                Rs = [segs[0]["R"], segs[1]["R"], segs[2]["R"]]
            else:
                joints = [segs[1]["J"], segs[2]["J"], segs[3]["J"], tip]
                Rs = [segs[1]["R"], segs[2]["R"], segs[3]["R"]]
            parent = bname(side)
            for k in range(3):
                nm = bname(side, f, k + 1)
                bones.append(dict(name=nm, head=joints[k], tail=joints[k + 1], roll=Rs[k].col[2], parent=parent))
                parent = nm
            ext = Rs[2].col[1] * (self.D[f]["L"][-1] * 0.35)
            bones.append(dict(name=bname(side, f, 4), head=tip, tail=tip + ext, roll=Rs[2].col[2], parent=parent))
            # gold ring on the proximal phalanx
            if f in self.rings:
                sg = segs[1]
                ya, r_in, r_out, wid, cbm = self.ring_spec(f)
                c = sg["origin"] + sg["R"].col[1] * ya + sg["R"].col[2] * cbm
                v, fc = B.band(c, sg["R"].col[1], r_in, r_out, wid, seg=8, bead=0.0, up_hint=sg["R"].col[2])
                pieces.append(B.Piece("%s_ring" % f, v, fc, "gold", bname(side, f, 2 if f == "thumb" else 1),
                                      kind="ring"))
        # nominal -> world
        for pc in pieces:
            pc.transformed(M)
        if self.side == "r":
            for pc in pieces:  # a mirror flips the winding; recalc in build_mesh fixes it, keep the order tidy
                pc.faces = [tuple(reversed(f)) for f in pc.faces]
        W = lambda v: M3 @ Vector(v)   # noqa: E731
        shift = Vector((0, 0, 0))
        origin = self.p.get("origin", "wrist")
        if origin == "ground":
            allv = np.array([v for pc in pieces for v in pc.verts])
            lo, hi = allv.min(0), allv.max(0)
            shift = Vector((-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]))
            T = Matrix.Translation(shift)
            for pc in pieces:
                pc.transformed(T)
        elif origin != "wrist":
            raise ValueError("origin must be wrist or ground")
        mesh, records = B.build_mesh("bone_hand_%s" % side, pieces, mats)
        arm = self.build_armature(bones, W, shift)
        mesh.parent = arm
        mod = mesh.modifiers.new("Armature", "ARMATURE")
        mod.object = arm
        # socket on the cylinder axis between the middle and ring finger planes
        xs = 0.5 * (self.mcp_joint("middle").x + self.mcp_joint("ring").x)
        t = (xs - self.C0.x) / self.A.x if abs(self.A.x) > 1e-6 else 0.0
        P = self.C0 + self.A * t
        Zb = self.A
        Yb = -(Vector((0, 1, 0)) - Zb * Zb.y).normalized()
        Zw = (self.world_map() @ Zb).normalized()
        Yw = (self.world_map() @ Yb).normalized()
        rot = B.frame_from(z=Zw, y=Yw)
        sock = B.add_socket("socket_mount_hand_%s" % side, W(P) + shift, rot, parent=arm, bone=bname(side),
                            kind="mount", frame=GRIP_FRAME, grip_radius_m=round(self.Rg_m, 5))
        axis_w = {"point": list(W(self.C0) + shift), "direction": list(Zw), "radius_m": self.Rg_m}
        info = {"records": records, "bones": bones, "axis": axis_w, "shift": shift}
        return mesh, arm, sock, info

    def build_armature(self, bones, W, shift):
        name = "bone_hand_%s_rig" % self.side
        data = bpy.data.armatures.new(name)
        arm = bpy.data.objects.new(name, data)
        bpy.context.scene.collection.objects.link(arm)
        af.select_only([arm])
        bpy.ops.object.mode_set(mode="EDIT")
        ebs = {}
        mapv = self.world_map()
        for b in bones:
            eb = data.edit_bones.new(b["name"])
            eb.head = W(b["head"]) + shift
            eb.tail = W(b["tail"]) + shift
            eb.align_roll((mapv @ Vector(b["roll"])).normalized())
            ebs[b["name"]] = eb
        for b in bones:
            if b["parent"]:
                eb = ebs[b["name"]]
                eb.parent = ebs[b["parent"]]
                eb.use_connect = (eb.head - eb.parent.tail).length < 1e-6
        bpy.ops.object.mode_set(mode="OBJECT")
        data.display_type = "STICK"
        return arm


# =============================================================================== staff

def long_bone(name, z0, z1, r, bot, shaft, top, n, bow_amp, bow_dir, pole_bot=0.0, pole_top=0.0, roll=0.0):
    """Long bone along +Z from z0 to z1. bot / top: [(dist_from_end_in_r, w_r, d_r, extras)]; shaft:
    [(u, w_r, d_r, extras)]. Sections use A = +X (width), B = -Y (front). Bow: centre offset
    bow_amp * sin(pi u) along bow_dir (angle in XY)."""
    L = z1 - z0
    stations = []
    for (dr, w, d, ex) in bot:
        stations.append((dr * r, w, d, ex))
    for (u, w, d, ex) in shaft:
        stations.append((u * L, w, d, ex))
    for (dr, w, d, ex) in top:
        stations.append((L - dr * r, w, d, ex))
    stations.sort(key=lambda s: s[0])
    out = []
    last = -1e9
    for (y, w, d, ex) in stations:
        if y <= last + 1e-5:
            continue
        u = y / L
        bx, by = bow_amp * math.sin(math.pi * u) * math.cos(bow_dir), bow_amp * math.sin(math.pi * u) * math.sin(bow_dir)
        s = st(y, w * r, d * r, ca=bx, cb=-by)
        s.update(ex)
        out.append(s)
        last = y
    A = Vector((math.cos(roll), math.sin(roll), 0.0))
    Bv = Vector((math.sin(roll), -math.cos(roll), 0.0))
    v, f = B.bone_from_stations(out, (0, 0, z0), A, Vector((0, 0, 1)), Bv, n,
                                pole0=(-pole_bot * r, 0.0, 0.0), pole1=(L + pole_top * r, 0.0, 0.0), phase=math.pi / n)
    return B.Piece(name, v, f, "bone", None, kind="long_bone"), out


def centre_at(stations, z0, z):
    """Centre (x, y) of a long bone at height z (interpolated station offsets)."""
    y = z - z0
    for a, b in zip(stations, stations[1:]):
        if a["y"] <= y <= b["y"]:
            t = (y - a["y"]) / max(b["y"] - a["y"], 1e-9)
            ca = B.lerp(a.get("ca", 0.0), b.get("ca", 0.0), t)
            cb = B.lerp(a.get("cb", 0.0), b.get("cb", 0.0), t)
            w = B.lerp(a["w"], b["w"], t)
            return Vector((ca, -cb, z)), w
    s = stations[0] if y < stations[0]["y"] else stations[-1]
    return Vector((s.get("ca", 0.0), -s.get("cb", 0.0), z)), s["w"]


def build_staff(p, mats):
    rng = random.Random(int(p.get("seed", 0)) * 7919 + 2027)
    L = float(p.get("length_m", 2.05))
    if not 0.6 < L < 4.0:
        raise ValueError("length_m out of range: %r" % L)
    gf = float(p.get("grip_fraction", 0.62))
    gof = float(p.get("grip_off_fraction", 0.45))
    nseg = int(p.get("segments", 3))
    if nseg not in (3, 4):
        raise ValueError("segments must be 3 or 4")
    r = float(p.get("shaft_radius_m", 0.018))
    hc = B.clamp(float(p.get("crystal_height_m", 0.21)), 0.18, 0.24)
    rc = float(p.get("crystal_radius_m", 0.038))
    ntal = int(p.get("talons", 4))
    if ntal not in (3, 4):
        raise ValueError("talons must be 3 or 4")
    bend = float(p.get("bend", 1.0))
    warnings = []
    jr = lambda a=0.06: 1.0 + a * (rng.random() * 2 - 1)   # noqa: E731
    zg, zgo = gf * L, gof * L
    hand_half = 0.05 * L / 2.05 + 0.01
    # layout from the top (reference: talons ~0.8 of the crystal, the crystal bottom ~40 % up the talons,
    # talon tips at ~45-50 % of the crystal height): crystal, claw palm (cup), upper bone, joints
    tal_len = float(p.get("talon_length_m", hc * 0.82))
    z_cb = L - hc                    # crystal bottom tip
    h_cup = 0.068 * hc / 0.21        # claw palm (block the talons grow from)
    z_ct = z_cb + 0.022              # block top (the crystal tip sits 2.2 cm inside it)
    z_cup0 = z_ct - h_cup
    z_tb = z_cup0 + 0.22 * h_cup     # talon bases on the block lobes
    zC1 = z_cup0 - 0.003
    # joints of the shaft (bottom -> top) as fractions, moved out of both hand zones
    fr = [0.33, 0.664] if nseg == 3 else [0.215, 0.43, 0.664]
    joints = [f * L for f in fr]
    epi = 2.8 * r
    for i, zj in enumerate(joints):
        for zh in (zg, zgo):
            if abs(zj - zh) < hand_half + epi:
                joints[i] = zh + hand_half + epi if zj >= zh - 0.01 else zh - hand_half - epi
    joints = sorted(joints)
    gap = 0.004
    pieces = []
    n = 14
    bone_ranges = [(0.0, joints[0] - gap / 2)]
    for a, b in zip(joints, joints[1:]):
        bone_ranges.append((a + gap / 2, b - gap / 2))
    bone_ranges.append((joints[-1] + gap / 2, zC1))
    # ---- profiles (in r units)
    # ends get a dome ring close to the pole so joints read as rounded knobs, not cut cylinders
    foot = [(0.12, 0.72, 0.68, {}), (0.45, 1.36, 1.26, {}), (1.1, 1.78, 1.62, {"lobe": 0.10}),
            (2.0, 1.72, 1.56, {"lobe": 0.12}), (3.1, 1.30, 1.20, {}),
            (4.6, 1.02, 0.96, {"tri": 0.05, "tri_phase": math.pi / 2})]
    plateau = [(4.6, 1.05, 1.0, {"tri": 0.07, "tri_phase": math.pi / 2}),
               (3.0, 1.42, 1.26, {"ridge": 0.28, "ridge_at": math.pi / 2}),
               (1.8, 2.05, 1.56, {"lobe": 0.08}), (0.85, 2.30, 1.72, {"lobe": 0.12}),
               (0.30, 2.12, 1.58, {"lobe": 0.10}), (0.07, 1.35, 1.0, {"lobe": 0.06})]
    condyles = [(0.07, 0.95, 0.66, {"lobe": 0.40}), (0.35, 1.78, 1.18, {"lobe": 0.38}),
                (0.95, 2.28, 1.62, {"lobe": 0.32}), (1.9, 2.18, 1.62, {"lobe": 0.24}),
                (3.0, 1.62, 1.32, {"lobe": 0.10}), (4.4, 1.10, 1.02, {})]
    knob = [(4.0, 1.05, 1.0, {}), (2.7, 1.40, 1.30, {}), (1.7, 1.98, 1.78, {"lobe": 0.14}),
            (0.85, 2.08, 1.88, {"lobe": 0.16}), (0.30, 1.62, 1.45, {"lobe": 0.10}), (0.06, 0.92, 0.82, {})]
    flare = [(9.0, 1.0, 1.0, {}), (6.0, 1.30, 1.20, {}), (4.2, 1.80, 1.55, {"lobe": 0.10}),
             (2.7, 2.40, 2.05, {"lobe": 0.20}), (1.4, 2.65, 2.25, {"lobe": 0.24}),
             (0.5, 2.40, 2.05, {"lobe": 0.24}), (0.1, 1.45, 1.25, {"lobe": 0.20})]
    tib_shaft = [(u, 0.95, 0.90, {"tri": 0.11, "tri_phase": math.pi / 2}) for u in (0.2, 0.35, 0.5, 0.65, 0.8)]
    fem_shaft = [(u, 1.0, 1.0, {"ridge": 0.12, "ridge_at": -math.pi / 2}) for u in (0.18, 0.32, 0.46, 0.6, 0.74, 0.86)]
    up_shaft = [(u, 1.0, 1.0, {"ridge": 0.10, "ridge_at": -math.pi / 2}) for u in (0.22, 0.4, 0.58)]
    specs = []
    for i, (z0, z1) in enumerate(bone_ranges):
        last = i == len(bone_ranges) - 1
        first = i == 0
        if first:
            bot, top, sh = foot, plateau, tib_shaft
            if nseg == 4:
                top = knob
        elif last:
            bot, top, sh = condyles, flare, up_shaft
        else:
            bot, top, sh = condyles, (plateau if (nseg == 4 and i == 1) else knob), fem_shaft
        rr = r * (0.95 if last else 1.0) * (1.0 if (z0 - 0.06 <= zg <= z1 + 0.06) else jr(0.05))
        bow = bend * (z1 - z0) * rng.uniform(0.006, 0.012)
        bdir = rng.uniform(0, 2 * math.pi)
        # keep the bone that carries the main grip almost straight there (tangent close to +Z)
        if z0 <= zg <= z1:
            bow *= 0.5
        pc, sts = long_bone("staff_bone_%d" % (i + 1), z0, z1, rr, bot, sh, top, n, bow, bdir)
        pieces.append(pc)
        specs.append(dict(z0=z0, z1=z1, r=rr, st=sts))
    # ---- patella-like knob in front of each joint whose upper bone starts with condyles
    for i, zj in enumerate(joints):
        rr = specs[i + 1]["r"]
        cz = zj + 1.0 * rr
        v, f = B.ellipsoid((0.0, -(1.62 * rr + 0.50 * rr + 0.002), cz), (1.05 * rr * jr(), 0.52 * rr, 1.25 * rr * jr()),
                           seg=8, rings=5, jitter=0.06, rng=rng)
        pieces.append(B.Piece("patella_%d" % (i + 1), v, f, "bone", None, kind="knob"))
    # ---- fibula alongside the upper bone (the dark groove of the reference)
    up = specs[-1]
    zf0 = up["z0"] + 3.4 * up["r"]
    zf1 = up["z1"] - 3.4 * up["r"]
    fside = 1.0 if rng.random() < 0.5 else -1.0
    path = []
    for k in range(9):
        u = k / 8
        z = B.lerp(zf0, zf1, u)
        c, w = centre_at(up["st"], up["z0"], z)
        # follows the shaft with a visible groove, sinks into the epiphyses at both ends
        off = (0.85 * w + 0.30 * up["r"] + 0.0035) + 0.25 * up["r"] * math.sin(math.pi * u)
        path.append((c.x + fside * off, c.y + 0.25 * up["r"], z))
    rf = 0.36 * up["r"]

    def fib_sec(i, u):
        k = 1.0 + 0.55 * math.exp(-(u / 0.1) ** 2) + 0.65 * math.exp(-((1 - u) / 0.1) ** 2)
        return {"w": rf * k, "d": rf * k * 0.9}
    v, f = B.sweep_sections(path, fib_sec, 7, up_hint=(0, 0, 1), tip0=Vector(path[0]) - Vector((0, 0, rf)),
                            tip1=Vector(path[-1]) + Vector((0, 0, rf)))
    pieces.append(B.Piece("fibula", v, f, "bone", None, kind="long_bone"))
    # ---- knee-like condyle knobs on both sides of the upper bone's top (under the claw palm)
    up_r = specs[-1]["r"]
    for sx in (-1, 1):
        v, f = B.ellipsoid((sx * 2.35 * up_r, 0.1 * up_r, zC1 - 1.2 * up_r),
                           (1.45 * up_r * jr(), 1.50 * up_r, 1.65 * up_r * jr()), seg=8, rings=5, jitter=0.06, rng=rng)
        pieces.append(B.Piece("head_condyle_%s" % ("l" if sx < 0 else "r"), v, f, "bone", None, kind="knob"))
    # ---- claw palm: a lobed block, one lobe per talon (the talons' knobby bases sit on the lobes)
    talon_angles = [math.pi / 2 + 2 * math.pi * k / ntal for k in range(ntal)]  # first = front (-Y)
    lob = (lambda a_: {"sq": a_}) if ntal == 4 else (lambda a_: {"tri": a_, "tri_phase": math.pi / 2})
    rcup = r * 1.0
    cup_st = [st(0.0, 1.50 * rcup, 1.50 * rcup, **lob(0.08)), st(0.2 * h_cup, 1.80 * rcup, 1.80 * rcup, **lob(0.16)),
              st(0.5 * h_cup, 1.95 * rcup, 1.95 * rcup, **lob(0.20)), st(0.8 * h_cup, 1.85 * rcup, 1.85 * rcup, **lob(0.18)),
              st(1.0 * h_cup, 1.55 * rcup, 1.55 * rcup, **lob(0.12))]
    v, f = B.bone_from_stations(cup_st, (0, 0, z_cup0), Vector((1, 0, 0)), Vector((0, 0, 1)), Vector((0, -1, 0)), 16,
                                pole0=(-0.35 * rcup, 0, 0), pole1=(h_cup - 0.55 * rcup, 0, 0))
    pieces.append(B.Piece("claw_palm", v, f, "bone", None, kind="knob"))
    # ---- crystal outline (needed for the talon clearance)
    sides = 7
    phase = rng.uniform(0, math.pi / 3)
    rad = [rc * jr(0.16) for _ in range(sides)]
    tilt = B.rot_x(rng.uniform(-3, 3)) @ B.rot_y(rng.uniform(-3, 3))
    # natural quartz-like prism: tapered foot, slightly bulged body, two-step termination
    levels = [(0.10, 0.62), (0.24, 0.94), (0.42, 1.0), (0.60, 0.97), (0.73, 0.88), (0.83, 0.62), (0.91, 0.34)]

    def crystal_r(z):  # conservative crystal radius at height z (for talon clearance)
        zf = (z - z_cb) / hc
        prof_ = [(0.0, 0.0)] + levels + [(1.0, 0.2)]
        for (za, ra), (zb, rb) in zip(prof_, prof_[1:]):
            if za <= zf <= zb:
                return rc * 1.12 * B.lerp(ra, rb, (zf - za) / (zb - za)) + abs(z - z_cb) * 0.06
        return 0.0
    # ---- talons: three phalanx bones each (knobby joints), leaning out then curling in over the crystal
    s_t = 0.92 * r
    seg_l = [0.42 * tal_len, 0.32 * tal_len, 0.30 * tal_len]
    tg = 0.0025
    for k, ang in enumerate(talon_angles):
        o = Vector((math.cos(ang), -math.sin(ang), 0.0))  # B = -Y: angle pi/2 -> front (-Y)
        front = k == 0
        side = Vector((0, 0, 1)).cross(o).normalized()
        st_k = s_t * (1.1 if front else 1.0) * jr(0.05)
        profs = [_finish_profile(phalanx_profile(seg_l[0], st_k), seg_l[0], (1.0, 1.0)),
                 _finish_profile(phalanx_profile(seg_l[1], st_k * 0.86, short=True), seg_l[1], (1.0, 1.0)),
                 _finish_profile(claw_profile(seg_l[2], st_k * 0.78), seg_l[2], (1.0, 1.0))]
        lean = (4.0 if front else 12.0) * jr(0.12)
        bends = ((8.0, 24.0) if front else (18.0, 40.0))
        S0 = Vector((0, 0, z_tb)) + o * (1.95 * rcup)
        for attempt in range(16):
            angs = [lean, lean - bends[0], lean - bends[0] - bends[1]]
            dirs = [(Vector((0, 0, 1)) * math.cos(math.radians(a_)) + o * math.sin(math.radians(a_))).normalized()
                    for a_ in angs]
            origin = S0.copy()
            chain = []
            for j in range(3):
                Y = dirs[j]
                Bd = side.cross(Y).normalized()          # "dorsal" faces away from the crystal
                chain.append((origin.copy(), Y, Bd, profs[j]))
                if j < 2:
                    Jc = origin + Y * (seg_l[j] - profs[j]["c"])
                    origin = Jc + dirs[j + 1] * (profs[j]["c"] + tg - profs[j + 1]["pole0"][0])
            # clearance against the crystal (cradle with ~3 mm gap)
            bad = False
            for (org, Y, Bd, pr) in chain:
                for s_ in pr["st"]:
                    q = org + Y * s_["y"] + Bd * s_.get("cb", 0.0)
                    need = crystal_r(q.z)
                    if need > 0 and Vector((q.x, q.y, 0)).length < need + max(s_["w"], s_["d"]) + 0.003:
                        bad = True
                        break
                if bad:
                    break
            if not bad:
                break
            lean += 2.0
        for j, (org, Y, Bd, pr) in enumerate(chain):
            v, f = B.bone_from_stations(pr["st"], org, side, Y, Bd, 8, pole0=pr["pole0"], pole1=pr["pole1"],
                                        phase=math.pi / 8)
            pieces.append(B.Piece("talon_%d_%d" % (k + 1, j + 1), v, f, "bone", None, kind="talon"))
    # crystal mesh
    rings = []
    for j, (zf, sc) in enumerate(levels):
        ring = []
        for i in range(sides):
            a = phase + 2 * math.pi * i / sides + 0.05 * j + (math.pi / sides if zf > 0.8 else 0.0)
            rr_ = rad[i] * sc * jr(0.07)
            zz = zf * hc + (rng.uniform(-0.012, 0.012) * hc if 0 < j < len(levels) - 1 else 0.0)
            ring.append(tuple(tilt @ Vector((rr_ * math.cos(a), rr_ * math.sin(a), zz)) + Vector((0, 0, z_cb))))
        rings.append(ring)
    apex = tilt @ Vector((rc * 0.22, -rc * 0.12, hc)) + Vector((0, 0, z_cb))
    bottom = tilt @ Vector((0, 0, 0)) + Vector((0, 0, z_cb))
    v, f = B.loft(rings, bottom, apex)
    pieces.append(B.Piece("crystal", v, f, "crystal", None, smooth=False, kind="crystal"))
    item, records = B.build_mesh("bone_staff", pieces, mats, sharp_deg=50.0)
    # ---- sockets (item frame)
    lo, hi = af.world_bbox([item])
    origin = p.get("origin", "bbox")
    shift = Vector((-(lo.x + hi.x) / 2, -(lo.y + hi.y) / 2, -lo.z)) if origin == "bbox" else Vector((0, 0, -lo.z))
    if origin not in ("bbox", "axis"):
        raise ValueError("origin must be bbox or axis")
    item.data.transform(Matrix.Translation(shift))
    item.data.update()
    bpy.context.view_layer.update()

    def shaft_frame(z):
        for sp in specs:
            if sp["z0"] <= z <= sp["z1"]:
                c0, w = centre_at(sp["st"], sp["z0"], z - 0.004)
                c1, _ = centre_at(sp["st"], sp["z0"], z + 0.004)
                c, w = centre_at(sp["st"], sp["z0"], z)
                return c, (c1 - c0).normalized(), w
        raise ValueError("grip height %.3f is not on a shaft bone" % z)
    sockets = []
    for nm, z in (("socket_grip_main", zg), ("socket_grip_off", zgo)):
        c, tan, w = shaft_frame(z)
        rot = B.frame_from(z=tan, y=(0, 1, 0))
        sockets.append(B.add_socket(nm, c + shift, rot, parent=item, kind="grip", frame=GRIP_FRAME,
                                    shaft_radius_m=round(w, 5)))
    ctr = (bottom + apex) / 2
    sockets.append(B.add_socket("socket_fx_tip", ctr + shift, Matrix.Identity(3), parent=item, kind="fx",
                                frame="fx: +Z emission direction (up the staff)"))
    grip_c, _, grip_w = shaft_frame(zg)
    info = {"records": records, "joints_z": [round(z + shift.z, 4) for z in joints],
            "bones_z": [[round(a + shift.z, 4), round(b + shift.z, 4)] for a, b in bone_ranges],
            "crystal": {"height_m": round(hc, 4), "radius_m": round(rc, 4), "bottom_z": round(z_cb + shift.z, 4),
                        "centre": list(ctr + shift)},
            "grip_shaft_radius_m": round(grip_w, 5), "talons": ntal, "segments": nseg, "warnings": warnings,
            "shift": list(shift)}
    return item, sockets, info


def _catmull(pts, n_seg=4):
    P = [Vector(p) for p in pts]
    m = len(P)
    out = []
    for i in range(m - 1):
        p0 = P[i - 1] if i > 0 else P[0]
        p1, p2 = P[i], P[i + 1]
        p3 = P[i + 2] if i + 2 < m else P[-1]
        for k in range(n_seg):
            t = k / n_seg
            t2, t3 = t * t, t * t * t
            out.append(0.5 * ((2 * p1) + (-p0 + p2) * t + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2
                              + (-p0 + 3 * p1 - 3 * p2 + p3) * t3))
    out.append(P[-1])
    return [tuple(p) for p in out]


# =============================================================================== toggle

def build_toggle(p, mats):
    rng = random.Random(int(p.get("seed", 0)) * 7919 + 3301)
    Lt = float(p.get("length_m", 0.10))
    if not 0.02 < Lt < 0.5:
        raise ValueError("length_m out of range: %r" % Lt)
    gold = p.get("gold", "rings")
    if gold not in ("rings", "caps", "none"):
        raise ValueError("gold must be rings, caps or none")
    loop = bool(p.get("loop", True))
    jr = lambda a=0.05: 1.0 + a * (rng.random() * 2 - 1)   # noqa: E731
    rs = float(p.get("bone_radius_m", Lt * 0.056))
    hl = Lt / 2
    pieces = []
    k = Lt / 0.10
    # shaft along X (stations y = x + hl), section A = +Z (height), B = -Y (front)
    x_knob = hl - 0.115 * Lt * jr()
    shaft_st = [st(hl - x_knob + 0.004 * k, 1.45 * rs, 1.2 * rs, lobe=0.15),
                st(0.30 * Lt, 1.06 * rs, 1.0 * rs), st(0.5 * Lt, 0.98 * rs * jr(), 0.95 * rs),
                st(0.70 * Lt, 1.06 * rs, 1.0 * rs), st(Lt - (hl - x_knob) - 0.004 * k, 1.45 * rs, 1.2 * rs, lobe=0.15)]

    def shaft_r(x):  # shaft half-height at x (for the gold fittings)
        y = x + hl
        for a, b in zip(shaft_st, shaft_st[1:]):
            if a["y"] <= y <= b["y"]:
                return B.lerp(a["w"], b["w"], (y - a["y"]) / (b["y"] - a["y"]))
        return shaft_st[0]["w"]
    v, f = B.bone_from_stations(shaft_st, (-hl, 0, 0), Vector((0, 0, 1)), Vector((1, 0, 0)), Vector((0, -1, 0)), 8,
                                pole0=(shaft_st[0]["y"] - 1.1 * rs, 0, 0), pole1=(shaft_st[-1]["y"] + 1.1 * rs, 0, 0),
                                phase=math.pi / 8)
    pieces.append(B.Piece("toggle_shaft", v, f, "bone", None, kind="long_bone"))
    # knob lobes: two per end (dog-bone silhouette from the front)
    rl = 0.098 * Lt
    for sx in (-1, 1):
        for sz in (-1, 1):
            c = (sx * (x_knob + 0.2 * rl), 0.0, sz * 0.78 * rl * jr(0.04))
            v, f = B.ellipsoid(c, (rl * 1.05 * jr(), rl * 0.82, rl * 0.98), B.rot_y(sx * sz * 20), seg=7, rings=5,
                               jitter=0.05, rng=rng)
            pieces.append(B.Piece("knob_%s%s" % ("l" if sx < 0 else "r", "b" if sz < 0 else "t"), v, f, "bone", None,
                                  kind="knob"))
    # gold rings / caps
    if gold == "rings":
        for sx in (-1, 1):
            xc = sx * 0.24 * Lt
            r_in = shaft_r(xc) * 1.0 + 0.0002 * k
            v, f = B.band((xc, 0, 0), (1, 0, 0), r_in, r_in + 0.0018 * k, 0.07 * Lt, seg=8, bead=0.0)
            pieces.append(B.Piece("gold_ring_%s" % ("l" if sx < 0 else "r"), v, f, "gold", None, kind="ring"))
    elif gold == "caps":
        for sx in (-1, 1):
            x0 = sx * (x_knob - 0.17 * Lt)
            x1 = sx * (x_knob + 0.02 * Lt)
            # closed gold sleeve over the inner half of the knob end (the bone lobes poke out of it)
            prof = [(shaft_r(x0) * 1.08, 0.0), (rs * 1.30, 0.30), (rl * 1.45, 0.68), (rl * 1.80, 0.90), (rl * 1.55, 1.0)]
            rings_ = []
            for (rad_, t) in prof:
                xx = B.lerp(x0, x1, t)
                rings_.append([(xx, rad_ * 0.92 * math.sin(2 * math.pi * kk / 8 + math.pi / 8),
                                rad_ * math.cos(2 * math.pi * kk / 8 + math.pi / 8)) for kk in range(8)])
            v, f = B.loft(rings_, tip0=(x0 - sx * 0.002 * k, 0.0, 0.0), tip1=(x1 + sx * 0.004 * k, 0.0, 0.0))
            pieces.append(B.Piece("gold_cap_%s" % ("l" if sx < 0 else "r"), v, f, "gold", None, kind="cap"))
    if loop:
        # cord loop around the shaft centre, stretched toward the back (it is what gets sewn on)
        rt_l = 0.0021 * k
        clear = shaft_r(0.0) + rt_l + 0.0004 * k
        y_f, y_b = -clear, clear + 0.007 * k
        cy, ay = 0.5 * (y_b + y_f), 0.5 * (y_b - y_f)
        az = clear / math.sqrt(max(1e-6, 1.0 - (cy / ay) ** 2))
        path = []
        for kk in range(10):
            a = 2 * math.pi * kk / 10
            path.append((0.0, cy + ay * math.cos(a), az * math.sin(a)))
        v, f = B.planar_loop(path, (1, 0, 0), rt_l, seg=5)
        pieces.append(B.Piece("loop", v, f, "gold", None, kind="loop"))
    ob, records = B.build_mesh("bone_toggle", pieces, mats, sharp_deg=60.0)
    # origin: centre of the back face (max Y), bbox centred in X / Z
    lo, hi = af.world_bbox([ob])
    shift = Vector((-(lo.x + hi.x) / 2, -hi.y, -(lo.z + hi.z) / 2))
    ob.data.transform(Matrix.Translation(shift))
    ob.data.update()
    bpy.context.view_layer.update()
    ob["af_placement"] = "origin = back-face centre; local -Y = outward surface normal; bone along X"
    info = {"records": records, "gold": gold, "loop": loop, "bone_radius_m": round(rs, 5), "shift": list(shift)}
    return ob, [], info


# =============================================================================== assembly / export

def _materials_info(ob):
    return [B.material_record(s.material) for s in ob.material_slots if s.material]


def build_part(params):
    """Build one part in the current scene. Returns (objects_to_export, info)."""
    part = params.get("part")
    if part not in PARTS:
        raise ValueError("params.part must be one of %s" % ", ".join(PARTS))
    style = params.get("style", "clean")
    mats = B.Materials(style, params.get("palette"))
    arm = None
    warnings = []
    extra = {}
    if part == "skeleton_hand":
        h = Hand(params)
        mesh, arm, sock, hinfo = h.build(mats)
        socks = [sock]
        warnings += h.warnings
        records = hinfo["records"]
        extra = {"side": h.side, "pose": h.pose, "length_m": h.H, "scale_m_per_unit": h.u,
                 "grip_axis": hinfo["axis"], "joint_angles_deg": {f: [round(a, 2) for a in v] for f, v in h.angles.items()},
                 "hand_frame": "wrist joint at the origin (origin=wrist), fingers along -Z, thumb toward -Y, "
                               "palm toward +X (right) / -X (left)",
                 "bone_roll": "bone local Z = dorsal side of its segment (back of the hand)"}
        objs = [arm, mesh, sock]
        main = mesh
    elif part == "bone_staff":
        main, socks, sinfo = build_staff(params, mats)
        records = sinfo.pop("records")
        warnings += sinfo.pop("warnings", [])
        extra = sinfo
        objs = [main] + socks
    else:
        main, socks, tinfo = build_toggle(params, mats)
        records = tinfo.pop("records")
        extra = tinfo
        objs = [main]
    # vertex colours (aged: AO + stains; also gives the VERTEX preview its colours)
    scale_ref = float(params.get("length_m", {"skeleton_hand": 0.19, "bone_staff": 2.05, "bone_toggle": 0.1}[part]))
    ao_dist = {"skeleton_hand": scale_ref * 0.10, "bone_staff": 0.07, "bone_toggle": scale_ref * 0.2}[part]
    if style == "aged":
        B.paint_vertex_colors(main, records, mats, style, seed=int(params.get("seed", 0)), ao_dist=ao_dist)
    main["af_part"] = part
    main["af_style"] = style
    bpy.context.view_layer.update()
    lo, hi = af.world_bbox([main])
    tris = af.tri_count([main])
    budget = int(params.get("budget_tris", BUDGET[part]))
    info = {"part": part, "style": style, "seed": int(params.get("seed", 0)), "params": {k: v for k, v in params.items() if k != "batch"},
            "tris": tris, "budget_tris": budget, "dimensions_m": [round(x, 5) for x in (hi - lo)],
            "bbox_min": [round(x, 5) for x in lo], "bbox_max": [round(x, 5) for x in hi],
            "pieces": len(records), "materials": _materials_info(main),
            "sockets": [B.socket_record(s) for s in socks],
            "uv_layers": [u.name for u in main.data.uv_layers],
            "vertex_colors": [a.name for a in main.data.color_attributes],
            "warnings": warnings}
    info.update(extra)
    info["piece_list"] = [{k: r[k] for k in ("name", "bone", "material", "kind", "tris")} for r in records]
    if arm is not None:
        info["bones"] = bone_records(arm)
        info["weights"] = weight_report(main, records)
        info["overlaps"] = B.piece_overlaps(main, records, skip=_hand_overlap_skip)
        info["grip_contact"] = grip_contact(main, records, extra["grip_axis"])
        info["chain_length_m"] = round(h.chain_nominal * h.u, 5)
    elif part == "bone_staff":
        info["overlaps"] = []
    info["_objects"] = objs
    info["_main"] = main
    info["_records"] = records
    info["_mats"] = mats
    return objs, info


def _hand_overlap_skip(a, b):
    """Pieces bound to the same bone never move against each other (carpal block + metacarpal bases of
    the Hand bone, a ring on its phalanx); the thumb metacarpal may touch the trapezium (CMC joint)."""
    if a["bone"] == b["bone"]:
        return True
    kinds = {a["kind"], b["kind"]}
    if kinds == {"carpal", "metacarpal"}:
        return True
    if {a["name"], b["name"]} == {"thumb_metacarpal", "index_metacarpal"}:  # bases meet at the CMC joints
        return True
    return False


def bone_records(arm):
    out = []
    mw = arm.matrix_world
    for b in arm.data.bones:
        out.append({"name": b.name, "parent": b.parent.name if b.parent else None,
                    "head": [round(x, 5) for x in mw @ b.head_local], "tail": [round(x, 5) for x in mw @ b.tail_local],
                    "length_m": round(b.length, 5), "connected": b.use_connect,
                    "z_axis": [round(x, 4) for x in (mw.to_3x3() @ b.z_axis).normalized()]})
    return out


def weight_report(ob, records):
    """Every vertex: exactly one group, weight 1.0; every piece: all its vertices in its own bone group."""
    gname = {g.index: g.name for g in ob.vertex_groups}
    bad_vertices = 0
    groups_of = []
    for v in ob.data.vertices:
        gs = [(gname[g.group], g.weight) for g in v.groups if g.weight > 0]
        groups_of.append(gs)
        if len(gs) != 1 or abs(gs[0][1] - 1.0) > 1e-6:
            bad_vertices += 1
    bad_pieces = []
    per_bone = {}
    for r in records:
        names = {groups_of[i][0][0] for i in range(r["v0"], r["v1"]) if len(groups_of[i]) == 1}
        if names != {r["bone"]}:
            bad_pieces.append(r["name"])
        per_bone.setdefault(r["bone"], []).append(r["name"])
    return {"vertices": len(ob.data.vertices), "bad_vertices": bad_vertices, "bad_pieces": bad_pieces,
            "pieces_per_bone": per_bone, "groups": sorted(g.name for g in ob.vertex_groups)}


def grip_contact(ob, records, axis):
    co, _ = B.mesh_arrays(ob, world=True)
    P0 = np.array(axis["point"])
    A = np.array(axis["direction"])
    A = A / np.linalg.norm(A)
    R = axis["radius_m"]
    out = {"radius_m": R, "fingers": {}, "pieces": {}}
    mins = []
    for r in records:
        if r["kind"] not in ("phalanx", "metacarpal", "ring", "carpal"):
            continue
        v = co[r["v0"]:r["v1"]] - P0
        d = np.linalg.norm(v - (v @ A)[:, None] * A, axis=1)
        mins.append(float(d.min()))
        out["pieces"][r["name"]] = round(float(d.min()) - R, 5)
        if r["name"].endswith("_distal"):
            out["fingers"][r["name"].split("_")[0]] = round(float(d.min()) - R, 5)
    out["min_clearance_m"] = round(min(mins) - R, 5) if mins else None
    worst = sorted(out["pieces"].items(), key=lambda kv: kv[1])[:4]
    out["closest"] = worst
    return out


def part_checks(info):
    part = info["part"]
    ch = [af.check("tris_within_budget", info["tris"] <= info["budget_tris"], info["tris"], info["budget_tris"]),
          af.check("uv_unwrapped", bool(info["uv_layers"]), info["uv_layers"], ">=1")]
    bad = [m["name"] for m in info["materials"] if not m["principled_to_output"]]
    ch.append(af.check("materials_principled", not bad, bad, "Principled BSDF -> Material Output"))
    for s in info["sockets"]:
        ortho, hand = B.frame_errors(s)
        ch.append(af.check("socket_frame_%s" % s["name"], ortho < 1e-3 and abs(hand - 1) < 1e-3,
                           {"ortho_err": round(ortho, 6), "x.(y x z)": round(hand, 5)}, "orthonormal, right-handed"))
    p = info["params"]
    if part == "skeleton_hand":
        names = set(hand_bone_names(info["side"]))
        have = {b["name"] for b in info["bones"]}
        ch.append(af.check("mixamo_hand_bones", have == names, len(have), "%d (Hand + 20 finger bones)" % len(names)))
        w = info["weights"]
        ch.append(af.check("rigid_weights", w["bad_vertices"] == 0 and not w["bad_pieces"],
                           {"bad_vertices": w["bad_vertices"], "bad_pieces": w["bad_pieces"]}, 0))
        ch.append(af.check("length_m", abs(info["chain_length_m"] - info["length_m"]) < 0.002 * info["length_m"] + 1e-6,
                           info["chain_length_m"], info["length_m"]))
        ch.append(af.check("no_piece_overlaps", not info["overlaps"], info["overlaps"][:6], 0, severity="warning"))
        s = info["sockets"][0]
        side = info["side"]
        z_ok = Vector(s["z_axis"]).dot(Vector((0, -1, 0))) > 0.95
        ch.append(af.check("socket_thumb_side", z_ok, s["z_axis"], "+Z ~ thumb side (-Y in the hand frame)"))
        if info["pose"] == "grip":
            gc = info["grip_contact"]
            ch.append(af.check("grip_no_penetration", gc["min_clearance_m"] >= -0.0006, {"min": gc["min_clearance_m"], "closest": gc["closest"]}, ">= -0.6 mm"))
            far = {k: v for k, v in gc["fingers"].items() if k != "thumb" and v > 0.004 * info["length_m"] / 0.19}
            ch.append(af.check("grip_fingertips_wrap", not far, gc["fingers"],
                               "index..pinky distal phalanx <= 4 mm from the cylinder (thumb reported only)",
                               severity="warning"))
    elif part == "bone_staff":
        L = float(p.get("length_m", 2.05))
        ch.append(af.check("length_m", abs(info["dimensions_m"][2] - L) < 0.005 * L, info["dimensions_m"][2], L))
        g = next(s for s in info["sockets"] if s["name"] == "socket_grip_main")
        gf = float(p.get("grip_fraction", 0.62))
        ch.append(af.check("grip_fraction", abs(g["location"][2] / L - gf) < 0.003, round(g["location"][2] / L, 4), gf))
        ch.append(af.check("grip_frame_up", Vector(g["z_axis"]).z > 0.995, g["z_axis"], "+Z up the shaft"))
        cm = [m for m in info["materials"] if m["name"] == "Crystal"]
        ch.append(af.check("crystal_emissive", bool(cm) and cm[0]["emission_strength"] > 0 and cm[0]["roughness"] <= 0.2,
                           cm[0] if cm else None, "slot 'Crystal', emission > 0, roughness <= 0.2"))
        ch.append(af.check("crystal_height", 0.18 - 1e-6 <= info["crystal"]["height_m"] <= 0.24 + 1e-6,
                           info["crystal"]["height_m"], "0.18..0.24"))
        r = float(p.get("shaft_radius_m", 0.018))
        ch.append(af.check("grip_shaft_radius", abs(info["grip_shaft_radius_m"] - r) < 0.15 * r,
                           info["grip_shaft_radius_m"], r, severity="warning"))
        names = {s["name"] for s in info["sockets"]}
        ch.append(af.check("sockets", {"socket_grip_main", "socket_fx_tip"} <= names, sorted(names),
                           "socket_grip_main, socket_fx_tip"))
        ch.append(af.check("on_ground", abs(info["bbox_min"][2]) < 1e-4, info["bbox_min"][2], 0.0))
    else:
        L = float(p.get("length_m", 0.10))
        ch.append(af.check("length_m", abs(info["dimensions_m"][0] - L) < 0.12 * L, info["dimensions_m"][0], L))
        ch.append(af.check("origin_back_face", abs(info["bbox_max"][1]) < 1e-5 and abs(info["bbox_min"][0] + info["bbox_max"][0]) < 1e-4
                           and abs(info["bbox_min"][2] + info["bbox_max"][2]) < 1e-4,
                           [info["bbox_min"], info["bbox_max"]], "max Y = 0, X / Z centred"))
        if info.get("gold") != "none" or info.get("loop"):
            gm = [m for m in info["materials"] if m["name"] == "Gold"]
            ch.append(af.check("gold_metallic", bool(gm) and gm[0]["metallic"] >= 0.99, gm[0]["metallic"] if gm else None, 1.0))
    return ch


def export_part(objs, info, out_dir, preview=True, preview_size=384, tile=False):
    os.makedirs(out_dir, exist_ok=True)
    main = info["_main"]
    has_arm = any(o.type == "ARMATURE" for o in objs)
    glb = af.export_glb(os.path.join(out_dir, "part.glb"), objects=objs, animations=False, skins=has_arm)
    info["glb"] = glb_summary(glb)
    outputs = {"glb": glb}
    color = "VERTEX" if info["style"] == "aged" else "MATERIAL"
    if color == "VERTEX":   # preview-only tint of gold / crystal (exported COLOR_0 keeps them white)
        B.preview_tint(main, info["_records"], info["_mats"], on=True)
    if preview:
        part = info["part"]
        if part == "skeleton_hand":
            views = ("left", "front", "right", "three_quarter")
            outputs["preview"] = af.render_views(os.path.join(out_dir, "preview.png"), [main], views=views,
                                                 size=preview_size, color=color)
        elif part == "bone_staff":
            outputs["preview"] = staff_preview(os.path.join(out_dir, "preview.png"), main, info, preview_size, color)
        else:
            outputs["preview"] = af.render_views(os.path.join(out_dir, "preview.png"), [main],
                                                 views=("front", "top", "three_quarter"), size=preview_size, color=color)
    if tile:
        view = "three_quarter"
        if info["part"] == "skeleton_hand":
            view = "left" if info["side"] == "r" else "right"   # back of the hand
        outputs["tile"] = af.render_views(os.path.join(out_dir, "_tile.png"), [main], views=(view,), size=320,
                                          color=color)
    if color == "VERTEX":
        B.preview_tint(main, info["_records"], info["_mats"], on=False)
    outputs["blend"] = af.save_blend(os.path.join(out_dir, "part.blend"))
    with open(os.path.join(out_dir, "part.json"), "w", encoding="utf-8") as fh:
        json.dump(af._jsonable({k: v for k, v in info.items() if not k.startswith("_")}), fh, ensure_ascii=False, indent=2)
    outputs["json"] = os.path.join(out_dir, "part.json")
    return outputs


def staff_preview(path, main, info, size, color):
    """Three full views + a close-up of the claw head (framing box around the crystal)."""
    full = af.render_views(path, [main], views=("front", "left", "three_quarter"), size=size, color=color)
    c = Vector(info["crystal"]["centre"])
    me = bpy.data.meshes.new("bk_frame")
    hw = 0.13
    v = [(c.x + sx * hw, c.y + sy * hw, c.z + sz * 0.19 - 0.05) for sx in (-1, 1) for sy in (-1, 1) for sz in (-1, 1)]
    me.from_pydata(v, [], [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)])
    box = bpy.data.objects.new("bk_frame", me)
    bpy.context.scene.collection.objects.link(box)
    box.hide_render = True
    head = os.path.join(os.path.dirname(path), "_head.png")
    try:
        af.render_views(head, [box], views=("three_quarter",), size=size, color=color, margin=1.0)
    finally:
        bpy.data.objects.remove(box)
        bpy.data.meshes.remove(me)
    a = B.load_png(full)
    b = B.load_png(head)
    out = np.concatenate([a, B.resize(b, a.shape[0], a.shape[0])], axis=1)
    os.remove(head)
    return B.save_png(out, path)


def glb_summary(path):
    import struct
    with open(path, "rb") as fh:
        fh.read(12)
        clen, _ = struct.unpack("<II", fh.read(8))
        g = json.loads(fh.read(clen).decode("utf-8"))
    nodes = g.get("nodes", [])
    socks = {}
    for i, n in enumerate(nodes):
        ex = n.get("extras") or {}
        if "game_socket" in ex:
            parent = next((nodes[j].get("name") for j, m in enumerate(nodes) if i in (m.get("children") or [])), None)
            socks[ex["game_socket"]] = {"node": n.get("name"), "parent": parent}
    attrs = sorted({a for m in g.get("meshes", []) for pr in m["primitives"] for a in pr["attributes"]})
    joints = []
    for sk in g.get("skins", []):
        joints += [nodes[j].get("name") for j in sk.get("joints", [])]
    return {"bytes": os.path.getsize(path), "materials": [m.get("name") for m in g.get("materials", [])],
            "sockets": socks, "skins": len(g.get("skins", [])), "joints": joints, "attributes": attrs,
            "primitives": [{"material": g["materials"][pr["material"]]["name"] if "material" in pr else None,
                            "attributes": sorted(pr["attributes"])} for m in g.get("meshes", []) for pr in m["primitives"]]}


def stats_of(info):
    s = {"part": info["part"], "tris": info["tris"], "budget_tris": info["budget_tris"],
         "dimensions_m": info["dimensions_m"], "sockets": [x["name"] for x in info["sockets"]],
         "materials": [m["name"] for m in info["materials"]]}
    if info["part"] == "skeleton_hand":
        s.update(bones=len(info["bones"]), pose=info["pose"], side=info["side"])
    return s


def run(req):
    params = dict(req["params"])
    batch = params.pop("batch", None)
    if isinstance(batch, (int, float)) and not isinstance(batch, bool):
        n = int(batch)
        batch = [{"seed": int(params.get("seed", 0)) + k} for k in range(n)] if n > 1 else None
    out_dir = req["out_dir"]
    if not batch:
        af.reset_scene()
        objs, info = build_part(params)
        checks = part_checks(info)
        outputs = export_part(objs, info, out_dir, preview=params.get("preview", True),
                              preview_size=int(params.get("preview_size", 384)))
        af.log("part built", part=info["part"], tris=info["tris"])
        return {"outputs": outputs, "stats": stats_of(info), "checks": checks, "warnings": info["warnings"]}
    variants, tiles, labels, all_checks, warnings = [], [], [], [], []
    real_out = os.path.realpath(out_dir)
    used = set()
    for i, var in enumerate(batch):
        p = copy.deepcopy(params)
        p.update(var)
        af.reset_scene()
        objs, info = build_part(p)
        tag = {"skeleton_hand": lambda: "%s_%s" % (info["pose"], info["side"]),
               "bone_staff": lambda: "t%d" % info["talons"],
               "bone_toggle": lambda: info["gold"]}[info["part"]]()
        name = p.get("name") or "%02d_%s_%s_s%d" % (i, info["part"], tag, info["seed"])
        name = re.sub(r"[^A-Za-z0-9_-]", "_", str(name)).strip("_") or "variant_%02d" % i
        if name in used:
            name = "%s_%02d" % (name, i)
        used.add(name)
        sub = os.path.join(out_dir, name)
        real_sub = os.path.realpath(sub)
        if os.path.commonpath([real_out, real_sub]) != real_out or real_sub == real_out:
            raise ValueError("variant folder %r escapes out_dir" % name)
        outs = export_part(objs, info, sub, preview=p.get("preview", True), preview_size=int(p.get("preview_size", 320)),
                           tile=True)
        checks = part_checks(info)
        for c in checks:
            c["name"] = "%s:%s" % (name, c["name"])
        all_checks += checks
        warnings += ["%s: %s" % (name, w) for w in info["warnings"]]
        tiles.append(B.load_png(outs["tile"]))
        os.remove(outs.pop("tile"))
        labels.append([("%s %s" % (info["part"].replace("skeleton_", "").replace("bone_", ""), tag))[:21],
                       "%dT %.2fM" % (info["tris"], max(info["dimensions_m"]))])
        variants.append({"name": name, "part": info["part"], "seed": info["seed"], "tris": info["tris"],
                         "budget_tris": info["budget_tris"], "dir": sub, "glb": outs["glb"], "blend": outs["blend"],
                         "preview": outs.get("preview"), "dimensions_m": info["dimensions_m"],
                         "sockets": [s["name"] for s in info["sockets"]]})
        af.log("variant built", name=name, tris=info["tris"])
    outputs = {"variants_dir": out_dir, "glb": [v["glb"] for v in variants], "blend": [v["blend"] for v in variants]}
    outputs["contact_sheet"] = B.contact_sheet(tiles, labels, os.path.join(out_dir, "contact_sheet.png"),
                                               cols=int(params.get("sheet_cols", 4)), tile=int(params.get("sheet_tile", 256)),
                                               title=params.get("sheet_title", "ASTRAFORGE BONE KIT"))
    outputs["preview"] = outputs["contact_sheet"]
    with open(os.path.join(out_dir, "batch.json"), "w", encoding="utf-8") as fh:
        json.dump(af._jsonable({"variants": variants}), fh, ensure_ascii=False, indent=2)
    outputs["json"] = outputs["batch_json"] = os.path.join(out_dir, "batch.json")
    return {"outputs": outputs, "stats": {"variants": len(variants), "tris": {v["name"]: v["tris"] for v in variants}},
            "checks": all_checks, "warnings": warnings}
