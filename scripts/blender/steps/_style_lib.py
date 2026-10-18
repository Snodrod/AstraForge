"""_style_lib - per-character motion style and procedural humanoid deaths (AstraForge Forge).

Used by step motion_style (docs/forge/motion_style.md). Input: a humanoid rig whose clips were written
by retarget (one layered action per clip, keys on every frame for every bone). Every clip is read into
a frame list of LOCAL rotations (parent-relative, from world rotations) plus the hips position, styled,
put back to world rotations, the legs are re-solved by the analytic two-bone IK of _anim_lib (feet on
their - possibly stride-scaled - trajectories, foot orientation kept, never below the rest ankle height)
and written back with _anim_lib.write_action.

Style knobs (neutral value -> effect):
  tempo 1.0      playback speed (0.8..1.25); loops are resampled onto a whole number of frames, so the
                 seam stays closed; the in-place ground speed of a walk scales with it
  stride 1.0     leg swing amplitude of locomotion: foot excursion around its mean (relative to the
                 input hips track) x stride, step height x sqrt(stride); the hips drop when a leg
                 cannot reach. Planted feet still move at one constant velocity -> no skating
  arm_swing 1.0  arm swing amplitude (Arm/ForeArm local rotation around the clip mean)
  lean 0.0       spine pitch, +1 = 12 deg forward, -1 = 12 deg back (head keeps 60 % of its gaze)
  hunch 0.0      0..1 rounded upper back, shoulders forward, neck forward, chin up
  head_bob 1.0   vertical bob of the hips/head around the mean (walk; idle breathing)
  sway 1.0       lateral sway of the hips around the mean
  limp 0.0       0..1 antalgic gait on limp_side: the hips dip and the torso lurches while that leg
                 bears weight, that foot drags (lower step). Stride and timing stay symmetric, so the
                 planted feet do not skate
  swagger 0.0    0..1 lean back, chest out, elbows out, more pelvis yaw and sway
  stiffness 1.0  >1 damps, <1 exaggerates every spine/neck/head/shoulder/arm motion (amplitude 1/stiffness)
Per-clip weights (CATEGORY_WEIGHTS) decide how much of each knob a category takes (a walk takes all,
an attack keeps its arms and legs, a death keeps its own timing).

Held props: a long prop rigid to a hand that points UP at rest (staff, banner, spear, bow) stays
upright in idle / walk / hit (the wrist compensates the arm swing that a 22-bone clip without hand keys
would pass to it), and no prop tip goes below the floor (the hand turns the prop until it rests on the
floor - a staff planted on the ground, a sword lying beside the body).

Deaths (procedural, DEATH_VARIANTS): fall_back, fall_forward, fall_left, fall_right, kneel_collapse,
spin_fall, slump_sit and crumble (undead: collapse down through the knees and the spine). Timing is
physical: anticipation / hit reaction, a fall whose tilt follows the inverted-pendulum equation
(slow start, fast end), an impact with a small rebound, a settle; a frame whose lowest skinned point
(random sample of the body, props included) is below the floor lifts the body, and after the impact
the body is set exactly onto the floor (it ends lying on it, no gap, no penetration).
"""
import hashlib
import math
import random

import bpy
import numpy as np
from mathutils import Matrix, Quaternion, Vector

try:
    from steps import _anim_lib as A
except ImportError:  # imported with steps/ itself on sys.path
    import _anim_lib as A

STYLE_KEYS = ("tempo", "stride", "arm_swing", "lean", "hunch", "head_bob", "sway", "limp", "swagger",
              "stiffness")
NEUTRAL = {"tempo": 1.0, "stride": 1.0, "arm_swing": 1.0, "lean": 0.0, "hunch": 0.0, "head_bob": 1.0,
           "sway": 1.0, "limp": 0.0, "swagger": 0.0, "stiffness": 1.0}
RANGES = {"tempo": (0.8, 1.25), "stride": (0.7, 1.35), "arm_swing": (0.2, 2.0), "lean": (-1.0, 1.0),
          "hunch": (0.0, 1.0), "head_bob": (0.3, 2.0), "sway": (0.2, 2.5), "limp": (0.0, 1.0),
          "swagger": (0.0, 1.0), "stiffness": (0.5, 1.8)}
CATEGORIES = ("idle", "walk", "run", "attack", "cast", "hit", "death", "other")
# share of every knob a clip category takes (multiplies the deviation from neutral)
_ALL = {k: 1.0 for k in STYLE_KEYS}
CATEGORY_WEIGHTS = {
    "walk": dict(_ALL),
    "run": dict(_ALL),
    "idle": dict(_ALL, stride=0.0, arm_swing=0.5, limp=0.5, tempo=0.8),
    "attack": dict(_ALL, stride=0.0, arm_swing=0.0, head_bob=0.0, sway=0.0, limp=0.0, lean=0.6, hunch=0.6,
                   swagger=0.3, stiffness=0.6, tempo=0.6),
    "cast": dict(_ALL, stride=0.0, arm_swing=0.0, head_bob=0.0, sway=0.0, limp=0.0, lean=0.6, hunch=0.6,
                 swagger=0.3, stiffness=0.6, tempo=0.6),
    "hit": dict(_ALL, stride=0.0, arm_swing=0.0, head_bob=0.0, sway=0.0, limp=0.0, lean=0.5, hunch=0.5,
                swagger=0.0, stiffness=0.7, tempo=0.7),
    "death": {k: 0.0 for k in STYLE_KEYS},
    "other": dict(_ALL, stride=0.0, limp=0.0, arm_swing=0.5),
}
DEATH_VARIANTS = ("fall_back", "fall_forward", "fall_left", "fall_right", "kneel_collapse", "spin_fall",
                  "slump_sit", "crumble")
LIVING_DEATHS = DEATH_VARIANTS[:-1]
G = 9.81
UPRIGHT_WEIGHT = {"idle": 0.9, "walk": 0.85, "run": 0.8, "hit": 0.6, "other": 0.5}


# =================================================================== seeds and style resolution

def seed_int(seed):
    """Stable integer from a number or any string (file name)."""
    if isinstance(seed, (int, float)) and not isinstance(seed, bool):
        return int(seed) & 0x7FFFFFFF
    return int(hashlib.sha1(str(seed).encode("utf-8")).hexdigest()[:8], 16)


BUILD_BIAS = {
    # multiplicative/additive nudges of the seeded draw by body build (roles.json "build")
    "heavy": {"tempo": -0.08, "stride": -0.1, "arm_swing": -0.25, "stiffness": 0.2, "head_bob": -0.15},
    "armored": {"tempo": -0.04, "stride": -0.04, "arm_swing": -0.15, "stiffness": 0.12},
    "robed": {"stride": -0.08, "arm_swing": -0.2, "sway": 0.15},
    "light": {"tempo": 0.06, "stride": 0.06, "arm_swing": 0.15},
    "skeleton": {"stiffness": 0.25, "hunch": 0.25, "tempo": 0.04, "head_bob": 0.2},
    "zombie": {"limp": 0.5, "hunch": 0.4, "tempo": -0.12, "stiffness": -0.15, "lean": 0.3},
}


def style_from_seed(seed, build=None):
    """Deterministic style draw: every knob somewhere in a plausible band, a limp or a swagger only for
    some characters (so most walks stay healthy), nudged by the body build."""
    rng = random.Random(seed_int(seed) ^ 0x5EED)
    s = {
        "tempo": rng.uniform(0.86, 1.16),
        "stride": rng.uniform(0.8, 1.24),
        "arm_swing": rng.uniform(0.45, 1.55),
        "lean": rng.uniform(-0.55, 0.85),
        "hunch": rng.uniform(0.35, 0.9) if rng.random() < 0.35 else rng.uniform(0.0, 0.2),
        "head_bob": rng.uniform(0.6, 1.5),
        "sway": rng.uniform(0.5, 1.7),
        "limp": rng.uniform(0.35, 0.85) if rng.random() < 0.16 else 0.0,
        "limp_side": rng.choice(("l", "r")),
        "swagger": rng.uniform(0.3, 0.9) if rng.random() < 0.25 else 0.0,
        "stiffness": rng.uniform(0.75, 1.35),
    }
    for k, d in (BUILD_BIAS.get(str(build or "").lower()) or {}).items():
        s[k] = s[k] + d
    for k, (lo, hi) in RANGES.items():
        s[k] = round(float(min(max(s[k], lo), hi)), 3)
    return s


def resolve_style(params):
    """Global style: neutral, or drawn from params.seed (+ params.build); explicit knobs win.
    Returns (style, source map {knob: 'param' | 'seed' | 'neutral'})."""
    seed = params.get("seed")
    base = dict(NEUTRAL, limp_side="l")
    src = {k: "neutral" for k in base}
    if seed is not None:
        base.update(style_from_seed(seed, params.get("build")))
        src = {k: "seed" for k in base}
    for k in list(STYLE_KEYS) + ["limp_side"]:
        if params.get(k) is not None:
            base[k] = params[k]
            src[k] = "param"
    for k, (lo, hi) in RANGES.items():
        v = float(base[k])
        if not lo - 1e-9 <= v <= hi + 1e-9:
            raise ValueError("%s must be within %g..%g, got %g" % (k, lo, hi, v))
        base[k] = v
    side = str(base.get("limp_side", "l")).lower()[:1]
    if side not in ("l", "r"):
        raise ValueError("limp_side must be l or r")
    base["limp_side"] = side
    return base, src


def clip_style(style, category, override=None):
    """Style for one clip: the global deviation from neutral x the category weight; a per-clip override
    dict sets knobs absolutely (weights do not apply to it)."""
    w = CATEGORY_WEIGHTS.get(category, CATEGORY_WEIGHTS["other"])
    out = {"limp_side": style["limp_side"]}
    for k in STYLE_KEYS:
        out[k] = NEUTRAL[k] + (style[k] - NEUTRAL[k]) * w.get(k, 1.0)
    for k, v in (override or {}).items():
        if k in STYLE_KEYS or k == "limp_side":
            out[k] = v
    return out


def death_choice(params, seed, undead=False):
    """'library' | variant name | 'auto' (seeded pick; crumble only for undead)."""
    d = str(params.get("death", "library")).lower()
    if d in ("library", "keep", "none"):
        return "library"
    if d == "auto":
        pool = list(DEATH_VARIANTS if undead else LIVING_DEATHS)
        return random.Random(seed_int(seed if seed is not None else 0) ^ 0xDEA7).choice(pool)
    if d not in DEATH_VARIANTS:
        raise ValueError("death must be library, auto or one of %s" % ", ".join(DEATH_VARIANTS))
    return d


# =================================================================== reading clips

def category_of(name, meta=None):
    if meta and meta.get("category"):
        return meta["category"]
    base = name[:-5] if name.endswith("-loop") else name
    c = A.guess_category(base)
    return c if c in CATEGORIES else "other"


class Rig:
    """TargetRig plus what style needs: parents, descendants, canonical names, character axes."""

    def __init__(self, rig):
        scheme, tmap = A.map_armature(rig)
        self.tr = A.TargetRig(rig, tmap)
        tr = self.tr
        self.rig = rig
        self.names = [b.name for b in tr.order]
        self.parent = {b.name: (b.parent.name if b.parent else None) for b in tr.order}
        self.children = {n: [] for n in self.names}
        for n, p in self.parent.items():
            if p:
                self.children[p].append(n)
        hips = rig.data.bones[tr.hips]
        if hips.parent is not None:
            p = hips.parent
            while p.parent is not None:
                p = p.parent
            tr.root = p.name
        self.c = tr.cname
        F = tr.frame
        self.lat = Vector(F.col[0]).normalized()       # character left
        self.up = Vector((0.0, 0.0, 1.0))
        fwd = -Vector(F.col[1])
        fwd.z = 0.0
        self.fwd = fwd.normalized()
        self.lat = self.up.cross(self.fwd).normalized()     # character left (+X for a -Y facing body)
        self.rest_ankle_z = {s: tr.heads[s + "Foot"].z for s in tr.legs if (s + "Foot") in tr.heads}
        self.rest_toe_z = {s: tr.heads[s + "ToeBase"].z for s in tr.legs if (s + "ToeBase") in tr.heads}
        self.leg_len = {}
        for s, nm in tr.legs.items():
            h, k, a = (tr.heads.get(s + x) for x in ("UpLeg", "Leg", "Foot"))
            if h is not None and k is not None and a is not None:
                self.leg_len[s] = ((k - h).length, (a - k).length)

    def bone(self, canon):
        return self.c.get(canon)

    def descendants(self, n):
        out, stack = [], [n]
        while stack:
            x = stack.pop()
            out.append(x)
            stack.extend(self.children.get(x, []))
        return out


def read_action(R, action, n):
    """Basis (loc, quat) per bone per frame from the action's curves (frames 0..n-1)."""
    curves = {}
    for fc in A.fcurves_of(action):
        curves[(fc.data_path, fc.array_index)] = fc
    frames = []
    for f in range(n):
        basis = {}
        for nm in R.names:
            pre = 'pose.bones["%s"].' % nm
            loc = Vector([curves[(pre + "location", i)].evaluate(f) if (pre + "location", i) in curves
                          else 0.0 for i in range(3)])
            q = Quaternion([curves[(pre + "rotation_quaternion", i)].evaluate(f)
                            if (pre + "rotation_quaternion", i) in curves else (1.0 if i == 0 else 0.0)
                            for i in range(4)])
            q.normalize()
            basis[nm] = (loc, q)
        frames.append(basis)
    return frames


def fk(R, basis):
    """Armature-space matrices from a basis dict."""
    tr, M = R.tr, {}
    for b in tr.order:
        loc, q = basis[b.name]
        base = (M[b.parent.name] @ tr.rel[b.name]) if b.parent else tr.rel[b.name].copy()
        M[b.name] = base @ Matrix.Translation(loc) @ q.to_matrix().to_4x4()
    return M


class Pose:
    """World rotations of every bone + hips (and root) head world positions."""
    __slots__ = ("qw", "hips", "root")

    def __init__(self, qw, hips, root=None):
        self.qw, self.hips, self.root = qw, hips, root

    def copy(self):
        return Pose({k: q.copy() for k, q in self.qw.items()}, self.hips.copy(),
                    self.root.copy() if self.root is not None else None)


def pose_from_basis(R, basis):
    tr = R.tr
    M = fk(R, basis)
    qw = {n: A.rot3(tr.W @ M[n]) for n in R.names}
    hips = tr.W @ M[tr.hips].translation
    root = (tr.W @ M[tr.root].translation) if tr.root else None
    return Pose(qw, hips, root)


def build(R, pose):
    """(M armature matrices, basis) of a world pose."""
    tr = R.tr
    qa = {n: tr.W_rot_inv @ q for n, q in pose.qw.items()}
    root_arm = (tr.W_inv @ pose.root) if (tr.root and pose.root is not None) else None
    return tr.build_pose(qa, tr.W_inv @ pose.hips, root_arm)


def joint(R, M, canon):
    nm = R.bone(canon)
    return (R.tr.W @ M[nm].translation) if nm else None


# local <-> world -------------------------------------------------------------------------------

def to_local(R, pose):
    L = {}
    for n in R.names:
        p = R.parent[n]
        L[n] = (pose.qw[p].inverted() @ pose.qw[n]) if p else pose.qw[n].copy()
    return L


def to_world(R, L, hips, root=None):
    qw = {}
    for n in R.names:
        p = R.parent[n]
        qw[n] = (qw[p] @ L[n]) if p else L[n].copy()
        qw[n].normalize()
    return Pose(qw, hips, root)


def apply_world_delta(R, pose, canon, D):
    """Rotate a bone (about its head) and its whole subtree by the world rotation D."""
    nm = R.bone(canon) if canon in R.c else canon
    if nm is None or nm not in pose.qw:
        return
    for d in R.descendants(nm):
        pose.qw[d] = D @ pose.qw[d]


def swing_bone(R, pose, M, canon, child_canon, target_dir, weight=1.0):
    """Swing a bone (subtree follows) so the direction head->child head moves toward target_dir."""
    a, b_ = joint(R, M, canon), joint(R, M, child_canon)
    if a is None or b_ is None or (b_ - a).length < 1e-6 or target_dir.length < 1e-9:
        return
    cur = (b_ - a).normalized()
    tgt = cur.slerp(target_dir.normalized(), weight) if weight < 1.0 else target_dir.normalized()
    apply_world_delta(R, pose, canon, cur.rotation_difference(tgt))


def qpow(q, f):
    """Scale the angle of a rotation (f may exceed 1: exaggeration)."""
    q = q.copy()
    if q.w < 0:
        q.negate()
    ang = 2.0 * math.acos(max(-1.0, min(1.0, q.w)))
    if ang < 1e-9:
        return Quaternion()
    axis = Vector((q.x, q.y, q.z))
    if axis.length < 1e-12:
        return Quaternion()
    return Quaternion(axis.normalized(), ang * f)


def qmean(qs):
    acc = Vector((0.0, 0.0, 0.0, 0.0))
    ref = qs[0]
    for q in qs:
        v = Vector((q.w, q.x, q.y, q.z))
        if q.dot(ref) < 0:
            v = -v
        acc += v
    m = Quaternion((acc[0], acc[1], acc[2], acc[3]))
    m.normalize()
    return m


def smooth_series(vals, win=2, loop=False):
    n = len(vals)
    out = []
    for i in range(n):
        acc, k = 0.0, 0
        for j in range(i - win, i + win + 1):
            if loop:
                jj = j % max(n - 1, 1)
            else:
                jj = min(max(j, 0), n - 1)
            acc += vals[jj]
            k += 1
        out.append(acc / k)
    if loop and n > 1:
        out[-1] = out[0]
    return out


# =================================================================== held props (rigid to a hand)

def held_props(R, meshes, min_len=0.45):
    """Props skinned rigidly to a hand bone (weight >= 0.99, farther than 12 cm from the wrist), from the
    bind pose: {side: {"tips": [hand-local tip_a, tip_b], "len", "axis_rest", "upright"}}. upright: the
    prop axis is near vertical at rest and it reaches > 0.3 m above the hand (staff, banner, spear,
    bow) - a blade hanging from the fist is not upright."""
    tr = R.tr
    out = {}
    for s in ("Left", "Right"):
        hn = R.bone(s + "Hand")
        if hn is None:
            continue
        hb = R.rig.data.bones[hn]
        head = tr.W @ hb.head_local
        pts = []
        for ob in meshes:
            vg = ob.vertex_groups.get(hn)
            if vg is None:
                continue
            gi = vg.index
            Mo = ob.matrix_world
            for v in ob.data.vertices:
                for g in v.groups:
                    if g.group == gi and g.weight >= 0.99:
                        p = Mo @ v.co
                        if (p - head).length > 0.12:
                            pts.append(tuple(p))
                        break
        if len(pts) < 30:
            continue
        P = np.array(pts)
        c = P.mean(0)
        _, sv, vt = np.linalg.svd(P - c, full_matrices=False)
        d = vt[0]
        t = (P - c) @ d
        length = float(t.max() - t.min())
        if length < min_len:
            continue
        # the real extreme vertices (a curved bow's tip is off the straight axis)
        a_w = Vector(P[int(np.argmin(t))].tolist())
        b_w = Vector(P[int(np.argmax(t))].tolist())
        Minv = (tr.W @ hb.matrix_local).inverted()
        axis = (b_w - a_w).normalized()
        if axis.z < 0:
            axis = -axis
        top = max(a_w.z, b_w.z)
        # a spread of the prop's own vertices for the floor check (a bow's limb apex is off the chord)
        rng = np.random.default_rng(3)
        pick = rng.choice(len(P), size=min(len(P), 96), replace=False)
        cloud = [Minv @ Vector(P[k].tolist()) for k in pick.tolist()] + [Minv @ a_w, Minv @ b_w]
        out[s] = {"tips": [Minv @ a_w, Minv @ b_w], "cloud": cloud, "len": length, "axis_rest": axis,
                  "hand_rest_q": A.rot3(tr.W @ hb.matrix_local),
                  "upright": bool(abs(axis.z) > 0.75 and top - head.z > 0.3), "samples": len(pts)}
    return out


def constrain_props(R, pose, props, upright_w=0.0, floor_z=0.0, margin=0.01, flat_w=0.0, lift_arm=False):
    """Hands holding props: (a) upright props turn back toward their rest axis by upright_w (the wrist
    takes the arm swing), (b) flat_w turns a prop toward lying flat (a dead body's weapon), (c) a prop tip
    below the floor turns the hand until the tip rests on it."""
    if not props:
        return 0
    tr = R.tr
    fixes = 0
    M, _ = build(R, pose)
    for s, pr in props.items():
        hn = R.bone(s + "Hand")
        if hn is None:
            continue
        if upright_w > 0 and pr["upright"]:
            Qh = pose.qw[hn]
            cur = (Qh @ pr["hand_rest_q"].inverted()) @ pr["axis_rest"]
            tgt = cur.slerp(pr["axis_rest"], upright_w)
            apply_world_delta(R, pose, hn, cur.rotation_difference(tgt))
            M, _ = build(R, pose)
        if flat_w > 0:
            Mh = tr.W @ M[hn]
            ta, tb = Mh @ pr["tips"][0], Mh @ pr["tips"][1]
            cur = tb - ta
            hz = Vector((cur.x, cur.y, 0.0))
            if cur.length > 1e-6 and hz.length > 1e-3 * cur.length:
                tgt = cur.normalized().slerp(hz.normalized(), min(flat_w, 1.0))
                apply_world_delta(R, pose, hn, cur.normalized().rotation_difference(tgt))
                M, _ = build(R, pose)
        Mh = tr.W @ M[hn]
        wrist = Mh.translation
        tips = [Mh @ t for t in pr.get("cloud", pr["tips"])]
        low = min(tips, key=lambda p: p.z)
        if lift_arm and pr["upright"] and low.z < floor_z + margin - 1e-4:
            # a staff stays upright: the arm lifts the wrist (two-bone IK, the hand keeps its world
            # orientation) until the staff's foot rests on the floor - tilting it would look broken
            nm = [R.bone(s + x) for x in ("Arm", "ForeArm", "Hand")]
            if None not in nm:
                need = floor_z + margin - low.z
                sh = tr.W @ M[nm[0]].translation
                el = tr.W @ M[nm[1]].translation
                keep = {d: pose.qw[d].copy() for d in R.descendants(nm[2])}
                pole = el - (sh + wrist) * 0.5
                A.solve_leg(pose.qw, M, tr.W, nm, wrist + Vector((0.0, 0.0, need)),
                            pole if pole.length > 1e-6 else -R.fwd)
                for d, q in keep.items():
                    pose.qw[d] = q
                M, _ = build(R, pose)
                Mh = tr.W @ M[hn]
                wrist = Mh.translation
                low = min((Mh @ t for t in pr.get("cloud", pr["tips"])), key=lambda q: q.z)
                fixes += 1
        for _ in range(3):
            if low.z >= floor_z + margin - 1e-4:
                break
            v = low - wrist
            L = v.length
            zt = floor_z + margin - wrist.z
            if L < 1e-6 or abs(zt) >= L:
                break
            hv = Vector((v.x, v.y, 0.0))
            if hv.length < 1e-6:
                hv = R.fwd.copy()
            hv.normalize()
            v2 = hv * math.sqrt(max(L * L - zt * zt, 0.0)) + Vector((0.0, 0.0, zt))
            apply_world_delta(R, pose, hn, v.rotation_difference(v2))
            M, _ = build(R, pose)
            Mh = tr.W @ M[hn]
            low = min((Mh @ t for t in pr.get("cloud", pr["tips"])), key=lambda q: q.z)
            fixes += 1
        continue
        v = low - wrist
        L = v.length
        zt = floor_z + margin - wrist.z
        if L < 1e-6 or abs(zt) >= L:
            continue
        hv = Vector((v.x, v.y, 0.0))
        if hv.length < 1e-6:
            hv = R.fwd.copy()
        hv.normalize()
        v2 = hv * math.sqrt(max(L * L - zt * zt, 0.0)) + Vector((0.0, 0.0, zt))
        apply_world_delta(R, pose, hn, v.rotation_difference(v2))
        M, _ = build(R, pose)
        fixes += 1
    return fixes


# =================================================================== legs

def solve_legs(R, pose, targets, foot_q=None, pole=None):
    """Two-bone IK of both legs to ankle targets {side: Vector}; the foot keeps foot_q (world) if given.
    Returns the number of unreachable legs."""
    tr = R.tr
    M, _ = build(R, pose)
    miss_n = 0
    if pole is None:
        hq = pose.qw[tr.hips] @ tr.rest_q[tr.hips].inverted()
        pole = hq @ R.fwd
    for s, nm in tr.legs.items():
        t = targets.get(s)
        if t is None:
            continue
        miss = []
        A.solve_leg(pose.qw, M, tr.W, nm, t, pole, miss)
        if miss and miss[0] > 0.005:
            miss_n += 1
        # children of the shin followed qw changes of up/lo only through their own world rotations:
        # re-express the foot (and toes) after the swing
        if foot_q is not None and s in foot_q:
            D = foot_q[s] @ pose.qw[nm[2]].inverted()
            apply_world_delta(R, pose, nm[2], D)
        M, _ = build(R, pose)
    return miss_n


def leg_ik_world(R, pose, targets, foot_q):
    """solve_legs with the subtree fix: solve_leg changes only thigh/shin world rotations, so the foot's
    world rotation (and its children) must be restored explicitly afterwards."""
    tr = R.tr
    saved = {}
    for s, nm in tr.legs.items():
        for d in R.descendants(nm[2]):
            saved[d] = pose.qw[d].copy()
    res = solve_legs(R, pose, targets, None)
    for s, nm in tr.legs.items():
        for d in R.descendants(nm[2]):
            pose.qw[d] = saved[d]
        if foot_q and s in foot_q:
            D = foot_q[s] @ pose.qw[nm[2]].inverted()
            for d in R.descendants(nm[2]):
                pose.qw[d] = D @ pose.qw[d]
    return res


def _lift_about(R, pose, pivot_canon_or_name, pivot, q, zr):
    """Turn a bone (subtree follows) about its head so point q rises to height zr."""
    v = q - pivot
    zz = zr - pivot.z
    if v.length < 1e-6 or abs(zz) >= v.length:
        return False
    hv = Vector((v.x, v.y, 0.0))
    if hv.length < 1e-6:
        return False
    hv.normalize()
    v2 = hv * math.sqrt(v.length ** 2 - zz * zz) + Vector((0.0, 0.0, zz))
    apply_world_delta(R, pose, pivot_canon_or_name, v.rotation_difference(v2))
    return True


def foot_floor(R, pose, tol=0.006, sides=None):
    """Feet out of the floor: the toe tip below its rest height bends the toes up (ToeBase about the ball:
    the planted ball does not move); a ball below its rest height pitches the whole foot about the ankle,
    only on the given sides (None = all) - where a lowered step height met the input's heel-off pitch."""
    tr = R.tr
    M, _ = build(R, pose)
    n = 0
    for s, nm in tr.legs.items():
        tb, te = R.bone(s + "ToeBase"), R.bone(s + "Toe_End")
        if tb and (sides is None or s in sides):
            ank = tr.W @ M[nm[2]].translation
            ball = tr.W @ M[tb].translation
            zr = R.rest_toe_z.get(s, 0.02)
            if ball.z < zr - tol and _lift_about(R, pose, nm[2], ank, ball, zr):
                M, _ = build(R, pose)
                n += 1
        if tb and te and (s + "Toe_End") in tr.heads:
            ball = tr.W @ M[tb].translation
            tip = tr.W @ M[te].translation
            zr = tr.heads[s + "Toe_End"].z
            if tip.z < zr - tol and _lift_about(R, pose, tb, ball, tip, zr):
                M, _ = build(R, pose)
                n += 1
    return n


def feet_world(R, M):
    tr = R.tr
    out = {}
    for s, nm in tr.legs.items():
        tn = R.bone(s + "ToeBase")
        out[s] = {"ankle": tr.W @ M[nm[2]].translation,
                  "toe": (tr.W @ M[tn].translation) if tn else None}
    return out


# =================================================================== style application

def resample(R, poses_L, hips, roots, tempo, loop):
    """Time-resample local rotations and positions by tempo (> 1 faster). Loops keep both ends."""
    n = len(poses_L)
    if abs(tempo - 1.0) < 1e-4 or n < 3:
        return poses_L, hips, roots, 1.0
    m = max(3, int(round((n - 1) / tempo)) + 1)
    real = (n - 1) / (m - 1)
    out_L, out_h, out_r = [], [], []
    for i in range(m):
        t = i * real
        a = min(int(math.floor(t)), n - 2)
        f = t - a
        Li = {}
        for nm in R.names:
            qa, qb = poses_L[a][nm], poses_L[a + 1][nm]
            if qa.dot(qb) < 0:
                qb = -qb
            Li[nm] = qa.slerp(qb, f)
        out_L.append(Li)
        out_h.append(hips[a].lerp(hips[a + 1], f))
        out_r.append(roots[a].lerp(roots[a + 1], f) if roots[a] is not None else None)
    if loop:
        out_L[-1] = {k: q.copy() for k, q in out_L[0].items()}
        out_h[-1] = out_h[0].copy()
        if out_r[0] is not None:
            out_r[-1] = out_r[0].copy()
    return out_L, out_h, out_r, real


SPINE = ("Spine", "Spine1", "Spine2")
UPPER = ("Spine", "Spine1", "Spine2", "Neck", "Head", "LeftShoulder", "RightShoulder", "LeftArm",
         "RightArm", "LeftForeArm", "RightForeArm")


def contact_weights(feet_tracks, tol=0.025):
    """Per side 0..1: the ankle near its lowest height (stance)."""
    out = {}
    for s, tr_ in feet_tracks.items():
        zs = [p.z for p in tr_["ankle"]]
        zmin = min(zs)
        raw = [1.0 - A.smoothstep((z - zmin) / tol) for z in zs]
        out[s] = raw
    return out


def style_clip(R, frames_basis, cat, st, loop, locomotion, props, opts=None):
    """Styled pose list for one clip. frames_basis: basis per input frame. Returns (poses, info)."""
    opts = opts or {}
    tr = R.tr
    pin = [pose_from_basis(R, b) for b in frames_basis]
    L_in = [to_local(R, p) for p in pin]
    hips_in = [p.hips.copy() for p in pin]
    roots_in = [p.root.copy() if p.root is not None else None for p in pin]
    # input feet (world) and foot rotations: the targets of the leg IK
    feet_in, footq_in = [], []
    for p in pin:
        M, _ = build(R, p)
        feet_in.append(feet_world(R, M))
        footq_in.append({s: p.qw[nm[2]].copy() for s, nm in tr.legs.items()})
    tempo = float(st["tempo"])
    L, hips, roots, real = resample(R, L_in, hips_in, roots_in, tempo, loop)
    n = len(L)
    n_in = len(L_in)

    def lerp_idx(seq_fn, i):
        t = i * real
        a = min(int(math.floor(t)), n_in - 2) if n_in > 1 else 0
        f = t - a if n_in > 1 else 0.0
        return seq_fn(a), seq_fn(min(a + 1, n_in - 1)), f
    ank_t, footq_t, toe_t, hips_ref = [], [], [], []
    for i in range(n):
        if real == 1.0 and n == n_in:
            fa, fq, hr = feet_in[i], footq_in[i], hips_in[i]
            ank_t.append({s: fa[s]["ankle"].copy() for s in fa})
            toe_t.append({s: (fa[s]["toe"].copy() if fa[s]["toe"] is not None else None) for s in fa})
            footq_t.append({s: q.copy() for s, q in fq.items()})
            hips_ref.append(hr.copy())
            continue
        A0, A1, f = lerp_idx(lambda k: feet_in[k], i)
        Q0, Q1, _ = lerp_idx(lambda k: footq_in[k], i)
        H0, H1, _ = lerp_idx(lambda k: hips_in[k], i)
        ank_t.append({s: A0[s]["ankle"].lerp(A1[s]["ankle"], f) for s in A0})
        toe_t.append({s: (A0[s]["toe"].lerp(A1[s]["toe"], f) if A0[s]["toe"] is not None else None) for s in A0})
        qd = {}
        for s in Q0:
            qa, qb = Q0[s], Q1[s]
            if qa.dot(qb) < 0:
                qb = -qb
            qd[s] = qa.slerp(qb, f)
        footq_t.append(qd)
        hips_ref.append(H0.lerp(H1, f))
    if loop:
        ank_t[-1] = {s: v.copy() for s, v in ank_t[0].items()}
        footq_t[-1] = {s: q.copy() for s, q in footq_t[0].items()}
        hips_ref[-1] = hips_ref[0].copy()

    # ---- amplitude scaling around the clip mean (stiffness everywhere upstairs, arm swing on arms)
    amp_upper = 1.0 / max(float(st["stiffness"]), 1e-3)
    arm_f = float(st["arm_swing"])
    sw = float(st["swagger"])
    scale = {}
    for c in UPPER:
        nm = R.bone(c)
        if nm is None:
            continue
        f = amp_upper
        if c.endswith(("Arm", "ForeArm")) and "Shoulder" not in c:
            f *= arm_f * (1.0 + 0.4 * sw)
            side = "Left" if c.startswith("Left") else "Right"
            if props.get(side, {}).get("upright"):
                f *= 0.6    # a hand on a staff swings less
        scale[nm] = f
    means = {nm: qmean([L[i][nm] for i in range(n)]) for nm in scale}
    for i in range(n):
        for nm, f in scale.items():
            if abs(f - 1.0) < 1e-4:
                continue
            m = means[nm]
            L[i][nm] = m @ qpow(m.inverted() @ L[i][nm], f)

    # ---- hips: bob, sway, swagger yaw, limp dip (locomotion); breathing/sway for idle
    hz = [h.z for h in hips]
    hx = [h.dot(R.lat) for h in hips]
    mz, mx = sum(hz) / n, sum(hx) / n
    bob, sway = float(st["head_bob"]), float(st["sway"]) * (1.0 + 0.6 * sw)
    limp = float(st["limp"])
    bad = "Left" if st["limp_side"] == "l" else "Right"
    good = "Right" if bad == "Left" else "Left"
    cw = contact_weights({s: {"ankle": [a[s] for a in ank_t]} for s in tr.legs}) if (limp > 0 and tr.legs) else {}
    hips2 = []
    for i, h in enumerate(hips):
        dz = (hz[i] - mz) * (bob - 1.0)
        dx = (hx[i] - mx) * (sway - 1.0)
        if limp > 0 and cw:
            if locomotion:
                dz -= 0.045 * limp * cw[bad][i]
                dx += 0.025 * limp * cw[bad][i] * (1.0 if bad == "Left" else -1.0)
            elif cat == "idle":
                dx += 0.02 * limp * (1.0 if good == "Left" else -1.0)
                dz -= 0.01 * limp
        hips2.append(h + Vector((0.0, 0.0, dz)) + R.lat * dx)
    if loop:
        hips2[-1] = hips2[0].copy()
    # pelvis yaw amplitude (swagger) around its mean
    hn = tr.hips
    if sw > 0 and locomotion:
        m = means.get(hn) or qmean([L[i][hn] for i in range(n)])
        for i in range(n):
            d = m.inverted() @ L[i][hn]
            L[i][hn] = m @ qpow(d, 1.0 + 0.8 * sw)

    # ---- stride: during a stance the foot is the input foot moved by an offset growing at (stride - 1) x
    # the treadmill velocity, so every planted point (heel, flat foot, ball) moves at stride x that
    # velocity - one constant ground speed, no skating; across the swing the offset returns (the foot
    # travels further). Step height in the swing x sqrt(stride), lower on a limping leg.
    stride = float(st["stride"])
    targets = []
    for i in range(n):
        targets.append({s: ank_t[i][s].copy() for s in ank_t[i]})
    stance = {s: [False] * n for s in tr.legs}
    swing_scaled = []
    if locomotion and tr.legs and (abs(stride - 1.0) > 1e-4 or limp > 0):
        src = {s: {"ankle": [ank_t[i][s] for i in range(n)], "toe": [toe_t[i][s] for i in range(n)]}
               for s in tr.legs}
        v_tm = A.source_ground_velocity(src)          # per frame, the planted feet slide at it
        for s in tr.legs:
            az = [ank_t[i][s].z for i in range(n)]
            tz = [toe_t[i][s].z if toe_t[i][s] is not None else 1e9 for i in range(n)]
            amin, tmin = min(az), min(tz)
            st_ = [(az[i] - amin < 0.025) or (tz[i] - tmin < 0.02) for i in range(n)]
            stance[s] = st_
            # stance segments (circular for loops)
            segs, i0 = [], None
            for i in range(n):
                if st_[i] and i0 is None:
                    i0 = i
                if not st_[i] and i0 is not None:
                    segs.append([i0, i - 1])
                    i0 = None
            if i0 is not None:
                segs.append([i0, n - 1])
            if loop and len(segs) > 1 and segs[0][0] == 0 and segs[-1][1] == n - 1:
                first = segs.pop(0)                       # wraps around the seam
                segs[-1] = [segs[-1][0], first[1] + (n - 1)]
            off = [None] * n
            k = stride - 1.0
            for a, b_ in segs:
                mid = 0.5 * (a + b_)
                for j in range(a, b_ + 1):
                    jj = j % (n - 1) if (loop and j >= n - 1) else j
                    off[jj] = v_tm * (k * (j - mid))
            if loop and off[0] is not None:
                off[n - 1] = off[0]
            known = [i for i in range(n) if off[i] is not None]
            if not known:
                continue
            # swing: blend from the end of one stance offset to the start of the next
            span = n - 1 if loop else n
            for i in range(n):
                if off[i] is not None:
                    continue
                prev = max([j for j in known if j < i] or [None], key=lambda x: -1 if x is None else x)
                nxt = min([j for j in known if j > i] or [None], key=lambda x: 10 ** 9 if x is None else x)
                if prev is None and loop:
                    prev = max(known) - span
                if nxt is None and loop:
                    nxt = min(known) + span
                if prev is None or nxt is None:
                    off[i] = off[prev % span] if prev is not None else off[nxt % span]
                    continue
                w = A.smoothstep((i - prev) / max(nxt - prev, 1))
                off[i] = off[prev % span].lerp(off[nxt % span], w)
            zf = R.rest_ankle_z.get(s, 0.0)
            lift = math.sqrt(max(stride, 1e-3)) * ((1.0 - 0.65 * limp) if s == bad else 1.0)
            for i in range(n):
                a_ = ank_t[i][s]
                z = a_.z if st_[i] else zf + max(a_.z - zf, 0.0) * lift
                targets[i][s] = Vector((a_.x + off[i].x, a_.y + off[i].y, max(z, zf)))
            swing_scaled.append(s)
        if loop:
            targets[-1] = {s: v.copy() for s, v in targets[0].items()}

    # ---- additive spine/shoulder/arm deltas (lean, hunch, swagger, limp lurch), then legs
    lean = float(st["lean"]) - 0.5 * sw
    hunch = float(st["hunch"])
    # a clip that already walks bent over (zombie shuffle, combat crouch) takes less extra forward lean
    # and hunch: past 12 deg of mean trunk lean the forward knobs fade out by 27 deg
    fwd_lean = []
    for p in pin:
        M, _ = build(R, p)
        h, nk = joint(R, M, "Hips"), joint(R, M, "Neck")
        if h is not None and nk is not None and (nk - h).length > 1e-6:
            v = (nk - h).normalized()
            fwd_lean.append(math.degrees(math.atan2(v.dot(R.fwd), max(v.z, 1e-6))))
    mean_lean = sum(fwd_lean) / len(fwd_lean) if fwd_lean else 0.0
    fade = 1.0 - min(1.0, max(0.0, (mean_lean - 12.0) / 15.0))
    if lean > 0:
        lean *= fade
    hunch *= fade
    poses = []
    drops = []
    for i in range(n):
        p = to_world(R, L[i], hips2[i], roots[i])
        lat = R.lat
        if abs(lean) > 1e-4:
            for c, wgt in zip(SPINE, (0.4, 0.35, 0.25)):
                apply_world_delta(R, p, c, Quaternion(lat, math.radians(12.0) * lean * wgt))
            for c, wgt in (("Neck", 0.3), ("Head", 0.3)):
                apply_world_delta(R, p, c, Quaternion(lat, -math.radians(12.0) * lean * wgt))
        if hunch > 1e-4 or sw > 1e-4:
            apply_world_delta(R, p, "Spine2", Quaternion(lat, math.radians(9.0 * hunch - 6.0 * sw)))
            apply_world_delta(R, p, "Neck", Quaternion(lat, math.radians(15.0 * hunch)))
            apply_world_delta(R, p, "Head", Quaternion(lat, -math.radians(17.0 * hunch)))
            M, _ = build(R, p)
            for s in ("Left", "Right"):
                sh, arm = s + "Shoulder", s + "Arm"
                a, b_ = joint(R, M, sh), joint(R, M, arm)
                if a is None or b_ is None:
                    continue
                v = b_ - a
                v2 = v + R.fwd * (0.35 * hunch - 0.3 * sw) * v.length - R.up * 0.12 * hunch * v.length
                apply_world_delta(R, p, sh, v.rotation_difference(v2))
            if sw > 1e-4:
                M, _ = build(R, p)
                for s, sg in (("Left", 1.0), ("Right", -1.0)):
                    a, b_ = joint(R, M, s + "Arm"), joint(R, M, s + "ForeArm")
                    if a is None or b_ is None:
                        continue
                    v = b_ - a
                    apply_world_delta(R, p, s + "Arm", v.rotation_difference(v + lat * sg * 0.25 * sw * v.length))
        if limp > 0 and locomotion and cw:
            roll = math.radians(7.0) * limp * cw[bad][i] * (1.0 if bad == "Left" else -1.0)
            apply_world_delta(R, p, "Spine1", Quaternion(R.fwd, -roll))
        # legs out of reach: the heel lifts first (up to 5 cm), then the hips drop (up to 7 cm)
        drop = 0.0
        M, _ = build(R, p)
        for s, nm in tr.legs.items():
            hj = tr.W @ M[nm[0]].translation
            l1, l2 = R.leg_len.get(s, (0.45, 0.45))
            Lr = (l1 + l2) * 0.985
            t = targets[i][s]
            dxy = math.hypot(t.x - hj.x, t.y - hj.y)
            if dxy < Lr:
                need = (hj.z - t.z) - math.sqrt(Lr * Lr - dxy * dxy)
                if need > 0 and locomotion:
                    up_ = min(need, 0.05)
                    targets[i][s] = Vector((t.x, t.y, t.z + up_))
                    need -= up_
                drop = max(drop, min(need, 0.07))
        drops.append(drop)
        poses.append(p)
    if any(d > 0 for d in drops):
        wmax = [max(drops[max(0, k - 3): k + 4]) for k in range(n)]
        drops = [max(a, b_) for a, b_ in zip(smooth_series(wmax, 3, loop), drops)]
    unreached = 0
    up_w = UPRIGHT_WEIGHT.get(cat, 0.0) if opts.get("hold_props", True) else 0.0
    prop_fixes = 0
    for i, p in enumerate(poses):
        if drops[i] > 1e-5:
            p.hips = p.hips - Vector((0.0, 0.0, drops[i]))
        if tr.legs:
            fq = footq_t[i]
            tg = {}
            for s in tr.legs:
                t = targets[i][s]
                zf = R.rest_ankle_z.get(s, 0.0)
                tg[s] = Vector((t.x, t.y, max(t.z, zf))) if opts.get("floor", True) else t
            unreached += leg_ik_world(R, p, tg, fq)
            if opts.get("floor", True):
                # toes bend up out of the floor everywhere; the foot pitches only in the swing of a leg
                # whose step height this step changed (stride / limp)
                foot_floor(R, p, sides=[s for s in swing_scaled if not stance[s][i]])
        prop_fixes += constrain_props(R, p, props, up_w, lift_arm=True)
    if loop and poses:
        poses[-1] = poses[0].copy()
    info = {"frames_in": n_in, "frames": n, "tempo_real": round(1.0 / real, 4) if real else 1.0,
            "clip_trunk_lean_deg": round(mean_lean, 1), "forward_knobs_scale": round(fade, 3),
            "hips_drop_max_cm": round(100 * max(drops or [0.0]), 1), "ik_unreached_frames": unreached,
            "prop_fixes": prop_fixes, "src_feet": [{s: {"ankle": a[s], "toe": toe_t[k][s]} for s in a}
                                                    for k, a in enumerate(ank_t)]}
    return poses, info


# =================================================================== procedural deaths

def _pend(theta0, omega0, L, theta_end, dt=1.0 / 240.0, tmax=3.0, k_extra=0.0):
    """Inverted pendulum tilt (rod of length L about its foot): theta'' = (3g/2L) sin(theta) + k_extra.
    Returns sample times and angles until theta_end."""
    ts, th = [0.0], [theta0]
    t, a, w = 0.0, theta0, omega0
    while a < theta_end and t < tmax:
        acc = 1.5 * G / L * math.sin(max(a, 0.02)) + k_extra
        w += acc * dt
        a += w * dt
        t += dt
        ts.append(t)
        th.append(min(a, theta_end))
    return ts, th


def _interp(ts, vs, t):
    if t <= ts[0]:
        return vs[0]
    if t >= ts[-1]:
        return vs[-1]
    i = int(np.searchsorted(ts, t)) - 1
    f = (t - ts[i]) / max(ts[i + 1] - ts[i], 1e-9)
    return vs[i] + (vs[i + 1] - vs[i]) * f


def ease_in(x):
    x = min(max(x, 0.0), 1.0)
    return x * x


def ease_out(x):
    x = min(max(x, 0.0), 1.0)
    return 1.0 - (1.0 - x) ** 2


def ss(x):
    return A.smoothstep(x)


def ramp(t, t0, t1, fn=ss):
    if t1 <= t0:
        return 1.0 if t >= t1 else 0.0
    return fn((t - t0) / (t1 - t0))


def settle_osc(t, t_imp, amp, freq=6.0, damp=9.0):
    """Damped rebound after an impact: 0 before it."""
    if t < t_imp:
        return 0.0
    x = t - t_imp
    return amp * math.sin(2 * math.pi * freq * x) * math.exp(-damp * x)


class DeathPlan:
    """Timing of a procedural death (seconds), drawn from the seed: the hit reaction, the fall whose
    tilt follows the inverted pendulum (slow start, fast end), the impact and the settle."""

    def __init__(self, variant, rng, height):
        self.variant = variant
        self.h = height
        j = lambda a, b: rng.uniform(a, b)
        self.rng = rng
        self.dir_jitter = math.radians(j(-18.0, 18.0))
        self.yaw_total = 0.0
        self.side_roll = 0.0
        self.arms = rng.choice(("fling", "limp", "reach"))
        self.head_turn = math.radians(j(35.0, 70.0)) * rng.choice((-1.0, 1.0))
        self.jitter = 0.0
        self.leg_bias = j(0.6, 1.0)              # one knee bends more than the other
        k = j(0.92, 1.12)                         # global timing spread
        if variant in ("fall_back", "fall_forward", "fall_left", "fall_right", "spin_fall"):
            self.t_hit = 0.12 * k
            self.t_fall0 = j(0.12, 0.24) * k      # knees start to give, the body starts to tip
            L = (1.5 if variant != "spin_fall" else 1.35) * height / 1.8
            ts, th = _pend(math.radians(2.0), j(0.45, 0.95), L, math.radians(j(80.0, 88.0)))
            self.tilt_ts = [self.t_fall0 + t for t in ts]
            self.tilt_vs = th
            self.theta_end = th[-1]
            self.t_imp = self.tilt_ts[-1]
            self.knee_max = math.radians(j(25.0, 50.0))
            self.release = math.radians(j(20.0, 32.0))
            if variant == "spin_fall":
                self.yaw_total = math.radians(j(110.0, 200.0)) * rng.choice((-1.0, 1.0))
            self.duration = self.t_imp + j(0.75, 1.0)
        elif variant == "kneel_collapse":
            self.t_hit = 0.1 * k
            self.t_knees = j(0.4, 0.52) * k          # knees hit the ground
            self.t_pause = self.t_knees + j(0.18, 0.4)
            L = 0.85 * height / 1.8
            ts, th = _pend(math.radians(3.0), j(0.35, 0.7), L, math.radians(j(74.0, 84.0)))
            self.tilt_ts = [self.t_pause + t for t in ts]
            self.tilt_vs = th
            self.theta_end = th[-1]
            self.t_imp = self.tilt_ts[-1]
            self.side_roll = math.radians(j(-25.0, 25.0))
            self.duration = self.t_imp + j(0.7, 0.9)
        elif variant == "slump_sit":
            self.t_hit = 0.1 * k
            self.t_sit = j(0.45, 0.58) * k           # pelvis on the floor
            self.t_imp = self.t_sit
            self.slump_t = self.t_sit + j(0.25, 0.45)
            self.side_roll = math.radians(j(-22.0, 22.0))
            self.back = rng.random() < 0.4           # slumps back onto the floor instead of forward
            self.theta_end = math.radians(j(62.0, 75.0)) if self.back else 0.0
            self.duration = self.slump_t + j(1.0, 1.3)
        elif variant == "crumble":
            self.t_hit = 0.08 * k
            self.t_stagger = j(0.28, 0.45) * k
            self.t_down = self.t_stagger + j(0.42, 0.55)   # the heap reaches the floor
            self.t_imp = self.t_down
            self.side_roll = math.radians(j(-20.0, 20.0))
            self.jitter = j(0.6, 1.0)
            self.theta_end = math.radians(j(12.0, 24.0))
            self.duration = self.t_down + j(0.8, 1.1)
        else:
            raise ValueError("unknown death variant %s" % variant)


def skin_low(tr, M, gs):
    """(lowest z, dominant bone of that sample) of the skin samples for pose matrices M."""
    if not len(gs["P"]):
        return None, None
    S = np.zeros((len(gs["bones"]), 4, 4))
    for i, nm in enumerate(gs["bones"]):
        S[i] = np.array(tr.W @ M[nm] @ gs["rest_inv"][i]) if nm in M else np.eye(4)
    P = np.concatenate([gs["P"], np.ones((len(gs["P"]), 1))], axis=1)
    out = np.zeros((len(P), 4))
    for k in range(4):
        out += gs["BW"][:, k:k + 1] * np.einsum("nij,nj->ni", S[gs["BI"][:, k]], P)
    j = int(np.argmin(out[:, 2]))
    return float(out[j, 2]), gs["bones"][int(gs["BI"][j, int(np.argmax(gs["BW"][j]))])]


def skin_points(tr, M, gs):
    """World positions of all skin samples and their dominant bone names for pose matrices M."""
    S = np.zeros((len(gs["bones"]), 4, 4))
    for i, nm in enumerate(gs["bones"]):
        S[i] = np.array(tr.W @ M[nm] @ gs["rest_inv"][i]) if nm in M else np.eye(4)
    P = np.concatenate([gs["P"], np.ones((len(gs["P"]), 1))], axis=1)
    out = np.zeros((len(P), 4))
    for k in range(4):
        out += gs["BW"][:, k:k + 1] * np.einsum("nij,nj->ni", S[gs["BI"][:, k]], P)
    dom = gs["BI"][np.arange(len(P)), np.argmax(gs["BW"], axis=1)]
    return out[:, :3], dom


LIMB_SEGMENTS = ("UpLeg", "Leg", "Foot", "ToeBase", "Arm", "ForeArm", "Hand")


def segment_floor(R, p, gs, margin=0.004):
    """Limb segments out of the floor by their own skin: the lowest sample of each segment (thigh, shin,
    foot, toes, upper arm, forearm, hand, in that order) below the floor turns that bone about its head
    until the sample rests on the floor. Returns the number of turns."""
    if gs is None or not len(gs["P"]):
        return 0
    tr = R.tr
    fixes = 0
    bidx = {nm: i for i, nm in enumerate(gs["bones"])}
    for seg in LIMB_SEGMENTS:
        for s in ("Left", "Right"):
            nm = R.bone(s + seg)
            if nm is None or nm not in bidx:
                continue
            for _ in range(2):
                M, _ = build(R, p)
                pts, dom = skin_points(tr, M, gs)
                sel = np.nonzero(dom == bidx[nm])[0]
                if not len(sel):
                    break
                j = sel[int(np.argmin(pts[sel, 2]))]
                if pts[j, 2] >= -margin:
                    break
                head = tr.W @ M[nm].translation
                if not _lift_about(R, p, nm, head, Vector(pts[j].tolist()), margin):
                    break
                fixes += 1
    return fixes


CORE_CANON = ("Hips", "Spine", "Spine1", "Spine2", "Neck", "Head", "LeftShoulder", "RightShoulder",
              "LeftUpLeg", "RightUpLeg")    # thighs: only above the cloth line (cloth_sample_mask)


def _subset(gs, m):
    return {"P": gs["P"][m], "BI": gs["BI"][m], "BW": gs["BW"][m], "bones": gs["bones"], "rest_inv": gs["rest_inv"]}


def cloth_threshold(R):
    """Rest height under which skin carried by the Hips (weight >= 0.25) is robe / skirt / coat tail cloth:
    5 cm under the hip joints."""
    tr = R.tr
    hz = [tr.heads[s + "UpLeg"].z for s in ("Left", "Right") if (s + "UpLeg") in tr.heads]
    return (sum(hz) / len(hz) if hz else tr.heads["Hips"].z) - 0.05


def cloth_sample_mask(R, gs):
    """Skin samples of robe/skirt cloth: below the hip joints and carried by the Hips with >= 0.25 (the
    robe blend of rig_humanoid gives 0.35; thigh flesh has little Hips weight). Linear blend skinning
    keeps such cloth a stiff cone: a lying body would rest on it propped up, so the floor fits of the
    deaths ignore it (and report how deep it folds through the floor instead)."""
    if gs is None or not len(gs["P"]):
        return None
    hn = R.tr.hips
    if hn not in gs["bones"]:
        return np.zeros(len(gs["P"]), dtype=bool)
    hi = gs["bones"].index(hn)
    w_h = (gs["BW"] * (gs["BI"] == hi)).sum(axis=1)
    return (gs["P"][:, 2] < cloth_threshold(R)) & (w_h >= 0.25)


def core_samples(R, gs):
    """The skin samples whose dominant bone is the trunk or head, without robe cloth (the body that must
    rest on the floor; arms, legs and props are put onto the floor by limb IK and per-segment turns)."""
    if gs is None or not len(gs["P"]):
        return None
    core = {R.bone(c) for c in CORE_CANON if R.bone(c)}
    idx = [gs["bones"].index(b) for b in core if b in gs["bones"]]
    dom = gs["BI"][np.arange(len(gs["BI"])), np.argmax(gs["BW"], axis=1)]
    m = np.isin(dom, idx) & ~cloth_sample_mask(R, gs)
    if m.sum() < 20:
        return gs
    return _subset(gs, m)


def _limb_floor(R, p, height):
    """Wrists/elbows and ankles/knees of a posed body above the floor (two-bone IK keeping the hand / foot
    world orientation; a joint still under it swings its upper bone up). Returns the number of fixes."""
    tr = R.tr
    fixes = 0
    up = R.up
    for chain, zmin_tip, zmin_mid in ((("Arm", "ForeArm", "Hand"), 0.035, 0.03), (("UpLeg", "Leg", "Foot"), None, 0.05)):
        for s in ("Left", "Right"):
            nm = [R.bone(s + x) for x in chain]
            if None in nm:
                continue
            zt = zmin_tip if zmin_tip is not None else R.rest_ankle_z.get(s, 0.07)
            zt *= height / 1.8 if zmin_tip is not None else 1.0
            M, _ = build(R, p)
            tip = tr.W @ M[nm[2]].translation
            mid = tr.W @ M[nm[1]].translation
            if tip.z < zt:
                keep = {d: p.qw[d].copy() for d in R.descendants(nm[2])}
                pole = (mid - (tr.W @ M[nm[0]].translation))
                A.solve_leg(p.qw, M, tr.W, nm, Vector((tip.x, tip.y, zt)), pole if pole.length > 1e-6 else up)
                for d, q in keep.items():
                    p.qw[d] = q
                fixes += 1
                M, _ = build(R, p)
                mid = tr.W @ M[nm[1]].translation
            if mid.z < zmin_mid:
                root = tr.W @ M[nm[0]].translation
                v = mid - root
                hv = Vector((v.x, v.y, 0.0))
                zz = zmin_mid - root.z
                if v.length > 1e-6 and abs(zz) < v.length:
                    if hv.length < 1e-6:
                        hv = R.fwd.copy()
                    hv.normalize()
                    v2 = hv * math.sqrt(v.length ** 2 - zz * zz) + Vector((0.0, 0.0, zz))
                    apply_world_delta(R, p, nm[0], v.rotation_difference(v2))
                    fixes += 1
    # hands: the hand tip (bone tail) above the floor, turning the hand about the wrist
    M, _ = build(R, p)
    for s in ("Left", "Right"):
        hn = R.bone(s + "Hand")
        if hn is None:
            continue
        b_ = R.rig.data.bones[hn]
        Mh = tr.W @ M[hn]
        tip = Mh @ Vector((0.0, b_.length, 0.0))
        if tip.z < 0.02 and _lift_about(R, p, hn, Mh.translation, tip, 0.02):
            fixes += 1
            M, _ = build(R, p)
    fixes += foot_floor(R, p)
    return fixes


def _arm_dirs(mode, side, up, fwd, variant):
    if mode == "fling":
        return (side * 0.7 + up * 0.45 + fwd * (0.35 if variant != "fall_forward" else -0.2),
                side * 0.5 + up * 0.7 + fwd * 0.2)
    if mode == "reach":
        return (fwd * 0.75 + side * 0.3 - up * 0.25, fwd * 0.9 - up * 0.2 + side * 0.1)
    return (-up * 0.9 + side * 0.25 + fwd * 0.15, -up * 0.85 + fwd * 0.35)   # limp


def _onto_plane(v, n, fallback):
    w = v - n * v.dot(n)
    if w.length < 0.3 * max(v.length, 1e-9):
        w = fallback - n * fallback.dot(n)
    if w.length < 1e-9:
        return v
    return w.normalized() - n * 0.12


def make_death(R, start_pose, variant, seed, fps, props, gs, height=1.8):
    """Procedural death poses from start_pose (usually the first frame of the library death, i.e. the
    stance the character dies from). Returns (poses, info)."""
    tr = R.tr
    rng = random.Random(seed_int(seed) ^ 0xD1E5 ^ (DEATH_VARIANTS.index(variant) * 7919))
    plan = DeathPlan(variant, rng, height)
    n = int(round(plan.duration * fps)) + 1
    up, fwd0, lat0 = R.up, R.fwd, R.lat
    p0 = start_pose
    M0, _ = build(R, p0)
    feet0 = feet_world(R, M0)
    footq0 = {s: p0.qw[nm[2]].copy() for s, nm in tr.legs.items()}
    ground_c = sum((feet0[s]["ankle"] for s in feet0), Vector((0.0, 0.0, 0.0))) / max(len(feet0), 1)
    ground_c.z = 0.0
    base_dir = {"fall_back": -fwd0, "fall_forward": fwd0, "fall_left": lat0, "fall_right": -lat0,
                "spin_fall": -fwd0, "kneel_collapse": fwd0, "slump_sit": -fwd0, "crumble": fwd0}[variant]
    dir_ = Quaternion(up, plan.dir_jitter) @ base_dir
    dir_.z = 0.0
    dir_.normalize()
    axis_fall = up.cross(dir_).normalized()
    # body-frame direction that ends up pointing at the sky once the body lies (for arms on the floor)
    R_end = Quaternion(axis_fall, getattr(plan, "theta_end", 0.0))
    if plan.yaw_total:
        R_end = R_end @ Quaternion(up, plan.yaw_total)
    sky_b = R_end.inverted() @ up
    gs_core = core_samples(R, gs)
    cm = cloth_sample_mask(R, gs)
    gs_body = _subset(gs, ~cm) if (cm is not None and cm.any() and (~cm).sum() >= 20) else gs
    poses, core_low = [], []
    tilt_of = {}
    info = {"variant": variant, "impact_s": round(plan.t_imp, 3), "arms": plan.arms,
            "direction_deg": round(math.degrees(math.atan2(dir_.y, dir_.x)), 1)}
    for i in range(n):
        t = i / fps
        p = p0.copy()
        hit = ramp(t, 0.0, plan.t_hit, ease_out) * (1.0 - ramp(t, plan.t_hit, plan.t_hit + 0.25))
        spine_pitch = spine_roll = 0.0
        head_pitch = head_roll = head_yaw = 0.0
        hip_flex = {"Left": 0.0, "Right": 0.0}
        knee_flex = {"Left": 0.0, "Right": 0.0}
        hips_drop = 0.0
        planted = 1.0
        arm_mode, arm_w, sprawl = plan.arms, 0.0, 0.0
        yaw, tilt = 0.0, 0.0
        # the pivot: the edge of the body on the falling side (the back, the chest, the outer hip)
        pivot = ground_c + dir_ * (0.2 if variant in ("fall_left", "fall_right") else
                                   0.15 if variant == "fall_forward" else 0.12) * height / 1.8
        axis = axis_fall
        tilt_mode = "global"            # global | upper (spine and up) | no_shins (shins stay on the floor)
        relax = 0.0                     # legs back to their rest line under the pelvis (lying)
        v = variant
        lb = plan.leg_bias
        if v in ("fall_back", "fall_forward", "fall_left", "fall_right", "spin_fall"):
            tilt = _interp(plan.tilt_ts, plan.tilt_vs, t) + settle_osc(t, plan.t_imp, -math.radians(4.0))
            back = v in ("fall_back", "spin_fall")
            spine_pitch = math.radians(-14.0 if back else 14.0) * hit
            if v in ("fall_left", "fall_right"):
                spine_roll = math.radians(12.0) * hit * (1.0 if v == "fall_left" else -1.0)
            head_pitch = math.radians(-18.0 if back else 20.0) * hit
            fr = ramp(t, plan.t_fall0, plan.t_imp, ease_in)
            after = ramp(t, plan.t_imp, plan.t_imp + 0.4)
            for s, b_ in (("Left", 1.0), ("Right", lb)):
                k_ = plan.knee_max * b_ * fr * (1.0 - (0.7 if v == "fall_forward" else 0.5) * after)
                knee_flex[s] = k_
                if v == "fall_forward":
                    hip_flex[s] = 0.6 * k_ * (1.0 - after) + math.radians(6.0) * after
                elif v in ("fall_left", "fall_right"):
                    hip_flex[s] = (0.6 + 0.6 * after) * k_
                else:
                    hip_flex[s] = 0.6 * k_ * (1.0 - 0.6 * after)
            planted = 1.0 - ramp(tilt, plan.release * 0.6, plan.release * 1.3)
            if v == "spin_fall":
                yaw = plan.yaw_total * ramp(t, 0.02, plan.t_imp * 0.75, ease_out)
            head_pitch += settle_osc(t, plan.t_imp, math.radians(14.0), 4.0, 7.0) * (-1.0 if back else 1.0)
            head_yaw = plan.head_turn * ramp(t, plan.t_imp, plan.t_imp + 0.35)
            arm_w = ramp(t, 0.03, plan.t_imp * 0.8)
            sprawl = ramp(t, plan.t_imp - 0.1, plan.t_imp + 0.3)
            relax = ramp(t, plan.t_imp - 0.15, plan.t_imp + 0.35) * (0.8 if v in ("fall_left", "fall_right") else 1.0)
        elif v == "kneel_collapse":
            drop_w = ramp(t, plan.t_hit, plan.t_knees, ease_in)
            unfold = ramp(t, plan.t_pause, plan.t_imp + 0.3)
            for s, b_ in (("Left", 1.0), ("Right", 0.9 + 0.1 * lb)):
                knee_flex[s] = math.radians(100.0 - 45.0 * unfold) * drop_w * b_
                hip_flex[s] = math.radians(12.0) * drop_w * (1.0 - unfold)
            spine_pitch = math.radians(10.0) * drop_w + math.radians(-8.0) * hit
            head_pitch = math.radians(25.0) * drop_w
            planted = 1.0 - ramp(t, plan.t_hit, plan.t_hit + 0.1)
            tilt = (_interp(plan.tilt_ts, plan.tilt_vs, t) if t > plan.t_pause else 0.0) \
                + settle_osc(t, plan.t_imp, -math.radians(3.0))
            spine_roll = plan.side_roll * ramp(t, plan.t_pause, plan.t_imp)
            spine_roll += math.radians(4.0) * math.sin(2 * math.pi * 2.2 * t) * ramp(t, plan.t_knees, plan.t_pause) \
                * (1.0 - ramp(t, plan.t_pause, plan.t_pause + 0.2))
            head_yaw = plan.head_turn * ramp(t, plan.t_imp, plan.t_imp + 0.3)
            arm_mode, arm_w = "limp", ramp(t, plan.t_hit, plan.t_knees)
            sprawl = ramp(t, plan.t_imp - 0.15, plan.t_imp + 0.3)
            relax = 0.7 * ramp(t, plan.t_imp, plan.t_imp + 0.5)
            pivot = None            # the knees, measured on the posed body below
            tilt_mode = "no_shins"
        elif v == "slump_sit":
            sit = ramp(t, plan.t_hit, plan.t_sit, ease_in)
            hips_drop = sit + settle_osc(t, plan.t_sit, 0.04, 5.0, 10.0)
            slump = ramp(t, plan.t_sit, plan.slump_t + 0.5)
            if plan.back:
                tilt = plan.theta_end * ramp(t, plan.slump_t, plan.slump_t + 0.6, ease_in) \
                    + settle_osc(t, plan.slump_t + 0.6, -math.radians(3.0))
                spine_pitch = math.radians(-6.0) * slump
                tilt_mode = "upper"
                sprawl = ramp(t, plan.slump_t + 0.3, plan.slump_t + 0.8)
            else:
                spine_pitch = math.radians(38.0) * slump
            spine_roll = plan.side_roll * slump
            head_pitch = math.radians(45.0 if not plan.back else 10.0) * slump
            head_yaw = plan.head_turn * 0.5 * slump
            arm_mode, arm_w = "limp", ramp(t, plan.t_hit, plan.t_sit)
        elif v == "crumble":
            st_ = ramp(t, 0.0, plan.t_stagger)
            dn = ramp(t, plan.t_stagger, plan.t_down, ease_in)
            hips_drop = 0.08 * st_ + 0.92 * dn + settle_osc(t, plan.t_down, 0.03, 7.0, 12.0)
            for s, b_ in (("Left", 1.0), ("Right", 0.85 + 0.15 * lb)):
                knee_flex[s] = (math.radians(140.0) * dn + math.radians(12.0) * st_) * b_
                hip_flex[s] = math.radians(80.0) * dn * b_
            spine_pitch = math.radians(55.0) * dn + math.radians(-6.0) * hit
            spine_roll = plan.side_roll * dn
            head_pitch = math.radians(40.0) * dn
            planted = 1.0 - ramp(t, plan.t_stagger, plan.t_stagger + 0.15)
            tilt = plan.theta_end * ramp(t, plan.t_down - 0.1, plan.t_down + 0.4)
            pivot = ground_c + dir_ * 0.25
            arm_mode, arm_w = "limp", ramp(t, plan.t_stagger * 0.5, plan.t_down)
            if plan.jitter > 0:      # the magic leaving the bones: a decaying shudder
                amp = plan.jitter * math.radians(5.0) * (1.0 - ramp(t, plan.t_down, plan.t_down + 0.6))
                spine_roll += amp * math.sin(2 * math.pi * 11.0 * t + 1.3)
                head_roll = amp * 1.4 * math.sin(2 * math.pi * 9.0 * t)
        # ---- body frame (after the spin)
        if yaw:
            Ry = Quaternion(up, yaw)
            for nm in R.names:
                p.qw[nm] = Ry @ p.qw[nm]
            fwd, lat = Ry @ fwd0, Ry @ lat0
        else:
            fwd, lat = fwd0, lat0
        for c, wgt in zip(SPINE, (0.35, 0.35, 0.3)):
            if spine_pitch:
                apply_world_delta(R, p, c, Quaternion(lat, spine_pitch * wgt))
            if spine_roll:
                apply_world_delta(R, p, c, Quaternion(fwd, -spine_roll * wgt))
        if head_pitch or head_roll or head_yaw:
            apply_world_delta(R, p, "Neck", Quaternion(lat, head_pitch * 0.4))
            apply_world_delta(R, p, "Head", Quaternion(lat, head_pitch * 0.6))
            hn = R.bone("Head")
            if hn and head_yaw:
                hup = (p.qw[hn] @ tr.rest_q[hn].inverted()) @ up
                apply_world_delta(R, p, hn, Quaternion(hup, head_yaw))
            if hn and head_roll:
                apply_world_delta(R, p, hn, Quaternion(fwd, head_roll))
        if arm_w > 0:
            M, _ = build(R, p)
            sky = (Quaternion(up, yaw) @ sky_b) if yaw else sky_b
            for s, sg in (("Left", 1.0), ("Right", -1.0)):
                side = lat * sg
                # a hand on a long prop (staff, banner, bow) does not fling it wide: it drops along the
                # body (a 1.7 m staff swung out leaves any sprite cell and reads as a throw)
                mode_s = "limp" if (props.get(s, {}).get("len", 0.0) > 1.2 and arm_mode != "limp") else arm_mode
                ua, fa = _arm_dirs(mode_s, side, up, fwd, variant)
                if sprawl > 0:
                    head_dir = up
                    ua = ua.normalized().lerp(_onto_plane(ua.normalized(), sky, side * 0.8 + head_dir * 0.5), sprawl)
                    fa = fa.normalized().lerp(_onto_plane(fa.normalized(), sky, side * 0.6 + head_dir * 0.8), sprawl)
                swing_bone(R, p, M, s + "Arm", s + "ForeArm", ua, min(1.0, arm_w))
                M, _ = build(R, p)
                swing_bone(R, p, M, s + "ForeArm", s + "Hand", fa, min(1.0, arm_w))
                M, _ = build(R, p)
        if tr.legs:
            for s, nm in tr.legs.items():
                if hip_flex.get(s):
                    apply_world_delta(R, p, nm[0], Quaternion(lat, -hip_flex[s]))
                if knee_flex.get(s):
                    apply_world_delta(R, p, nm[1], Quaternion(lat, knee_flex[s]))
            # lying: the legs relax toward their rest line under the pelvis (a combat stance or bent
            # knees left as they were would prop the body up), with a slight natural bend
            if relax > 0:
                hq = p.qw[tr.hips] @ tr.rest_q[tr.hips].inverted()
                tgt = p.copy()
                for s, nm in tr.legs.items():
                    for d in R.descendants(nm[0]):
                        tgt.qw[d] = hq @ tr.rest_q[d]
                    bend = math.radians(10.0 + 14.0 * (1.0 - lb if s == "Right" else 0.0))
                    apply_world_delta(R, tgt, nm[0], Quaternion(lat, -0.4 * bend))
                    apply_world_delta(R, tgt, nm[1], Quaternion(lat, bend))
                blend_subtrees(R, p, tgt, [nm[0] for nm in tr.legs.values()], relax)
                for s, nm in tr.legs.items():
                    for d in R.descendants(nm[0]):
                        p.qw[d] = tgt.qw[d]
        if hips_drop:
            seat = (0.14 if variant == "slump_sit" else 0.2) * height / 1.8
            p.hips = p.hips - Vector((0.0, 0.0, (p0.hips.z - seat) * min(hips_drop, 1.05)))
            if variant == "slump_sit":
                p.hips = p.hips - fwd * 0.16 * min(hips_drop, 1.0)
        if variant == "kneel_collapse":
            drop_w = ramp(t, plan.t_hit, plan.t_knees, ease_in)
            l1 = R.leg_len.get("Left", (0.45, 0.45))[0]
            up_off = p0.hips.z - tr.heads.get("LeftUpLeg", p0.hips).z
            target_h = l1 * 1.0 + 0.055 * height / 1.8 + up_off
            p.hips = p.hips - Vector((0.0, 0.0, max(0.0, p0.hips.z - target_h) * drop_w))
            p.hips = p.hips + up * settle_osc(t, plan.t_knees, 0.025, 6.0, 12.0)
        # planted feet: IK back to the start positions, blended (in local space) with the free legs
        if planted > 1e-3 and tr.legs:
            free = p.copy()
            leg_ik_world(R, p, {s: feet0[s]["ankle"].copy() for s in tr.legs}, footq0)
            if planted < 0.999:
                blend_subtrees(R, free, p, [legs[0] for legs in tr.legs.values()], planted)
        # ---- the fall: a rotation about a ground pivot
        if abs(tilt) > 1e-5:
            if pivot is None:
                Mk, _ = build(R, p)
                ks = [joint(R, Mk, s + "Leg") for s in ("Left", "Right") if R.bone(s + "Leg")]
                pv = (sum(ks, Vector((0.0, 0.0, 0.0))) / len(ks)) if ks else ground_c.copy()
                pivot = Vector((pv.x, pv.y, 0.0)) + dir_ * 0.05
            Rq = Quaternion(axis, tilt)
            if tilt_mode == "upper":
                sp = R.bone("Spine")
                Mk, _ = build(R, p)
                pv = tr.W @ Mk[sp].translation
                for d in R.descendants(sp):
                    p.qw[d] = Rq @ p.qw[d]
            else:
                keep = {}
                if tilt_mode == "no_shins":
                    for s, legs in tr.legs.items():
                        for d in R.descendants(legs[1]):
                            keep[d] = p.qw[d].copy()
                for nm in R.names:
                    p.qw[nm] = Rq @ p.qw[nm]
                p.hips = pivot + Rq @ (p.hips - pivot)
                for d, q in keep.items():
                    p.qw[d] = q
                tilt_of[len(poses)] = (axis.copy(), pivot.copy(), set(keep))
        poses.append(p)
        if gs_core is not None:
            M, _ = build(R, p)
            z_c, b_c = skin_low(tr, M, gs_core)
            core_low.append(z_c)
            if i == n - 1:
                info["settled_trunk_low"] = {"cm": round(100 * z_c, 1) if z_c is not None else None, "bone": b_c}
    # ---- a toppled trunk under the floor tips back about the same pivot (the feet stay at the pivot,
    # the body rests on the floor at a smaller angle); lifting the whole body instead raised the legs
    if gs_core is not None and tilt_of:
        dl = [0.0] * len(poses)
        for i, (ax_, pv_, keep_) in tilt_of.items():
            if core_low[i] is None or core_low[i] >= 0.008:
                continue

            def low_at(delta):
                q_ = poses[i].copy()
                Rb = Quaternion(ax_, -delta)
                for nm in R.names:
                    if nm not in keep_:
                        q_.qw[nm] = Rb @ q_.qw[nm]
                q_.hips = pv_ + Rb @ (q_.hips - pv_)
                M_, _ = build(R, q_)
                return A.skin_low_z(tr, M_, gs_core)
            # only a small tip-back (12 deg): more would leave the body propped half-upright; a trunk
            # still under the floor after it is lifted instead
            lo_d, hi_d = 0.0, math.radians(12.0)
            if low_at(hi_d) < 0.008:
                continue          # tipping back cannot free it: the lift below does, the body stays lying
            for _ in range(12):
                mid = 0.5 * (lo_d + hi_d)
                if low_at(mid) < 0.008:
                    lo_d = mid
                else:
                    hi_d = mid
            dl[i] = hi_d
        wmax = [max(dl[max(0, k - 2): k + 3]) for k in range(len(dl))]
        dl = [max(a, b_) for a, b_ in zip(smooth_series(wmax, 2), dl)]
        for i, d_ in enumerate(dl):
            if d_ <= 1e-5 or i not in tilt_of:
                continue
            ax_, pv_, keep_ = tilt_of[i]
            Rb = Quaternion(ax_, -d_)
            p = poses[i]
            for nm in R.names:
                if nm not in keep_:
                    p.qw[nm] = Rb @ p.qw[nm]
            p.hips = pv_ + Rb @ (p.hips - pv_)
            M, _ = build(R, p)
            core_low[i] = A.skin_low_z(tr, M, gs_core)
        info["tilt_back_deg"] = round(math.degrees(max(dl)), 1)
    # ---- the trunk onto the floor: never below it; after the impact exactly on it
    t_imp_f = int(round(plan.t_imp * fps))
    lift = [0.0] * n
    if core_low:
        raw = []
        for i, z in enumerate(core_low):
            z = 0.0 if z is None else z
            # 8 mm above: the 2000-sample skin estimate misses the true lowest vertex by up to ~1 cm
            raw.append(-z + 0.008 if i >= t_imp_f else max(0.0, -z + 0.004))
        lift = list(raw)
        for i in range(n):
            if t_imp_f - 3 <= i <= t_imp_f + 3:
                lo, hi = max(0, i - 3), min(n, i + 4)
                lift[i] = sum(raw[lo:hi]) / (hi - lo)
            if i < t_imp_f:
                lift[i] = max(lift[i], raw[i])
        info["trunk_lowest_before_fix_cm"] = round(100 * min(z for z in core_low if z is not None), 1)
    limb_fixes, prop_fixes = 0, 0
    full_low = []
    for i, p in enumerate(poses):
        if abs(lift[i]) > 1e-6:
            p.hips = p.hips + Vector((0.0, 0.0, lift[i]))
        limb_fixes += _limb_floor(R, p, height)
        flat = ramp(i / fps, plan.t_imp - 0.05, plan.t_imp + 0.35)
        prop_fixes += constrain_props(R, p, props, 0.0, flat_w=flat)
        limb_fixes += segment_floor(R, p, gs_body)
        prop_fixes += constrain_props(R, p, props, 0.0)
        if gs is not None:
            M, _ = build(R, p)
            z_, bone_ = skin_low(tr, M, gs_body)
            full_low.append(z_)
            if z_ is not None and z_ < -0.004:
                info.setdefault("under_floor_bones", {})
                info["under_floor_bones"][bone_] = round(max(info["under_floor_bones"].get(bone_, 0.0), -100 * z_), 1)
    # anything else still under the floor (a shoulder pad, a cape) lifts the whole frame
    if full_low:
        extra = [max(0.0, -(z or 0.0) - 0.004) for z in full_low]
        extra = smooth_series([max(extra[max(0, k - 2): k + 3]) for k in range(n)], 2)
        for i, p in enumerate(poses):
            if extra[i] > 1e-6:
                p.hips = p.hips + Vector((0.0, 0.0, extra[i]))
        info["extra_lift_cm"] = round(100 * max(extra), 1)
    info.update({"trunk_lift_cm": [round(100 * min(lift), 1), round(100 * max(lift), 1)],
                 "limb_floor_fixes": limb_fixes, "prop_floor_fixes": prop_fixes, "impact_frame": t_imp_f})
    for _ in range(int(0.25 * fps)):        # hold the settled pose
        poses.append(poses[-1].copy())
    info["frames"] = len(poses)
    info["duration_s"] = round((len(poses) - 1) / fps, 3)
    return poses, info


def blend_subtrees(R, pa, pb, roots_, w):
    """In place on pb: the subtrees under roots_ get local rotations slerped from pa (w = 0) to pb
    (w = 1), recomposed from their (shared) parents."""
    for r in roots_:
        sub = R.descendants(r)                # parent first
        loc = {}
        for d in sub:
            par = R.parent[d]
            la = pa.qw[par].inverted() @ pa.qw[d] if par else pa.qw[d]
            lb = pb.qw[par].inverted() @ pb.qw[d] if par else pb.qw[d]
            if la.dot(lb) < 0:
                lb = -lb
            loc[d] = la.slerp(lb, w)
        for d in sub:
            par = R.parent[d]
            pb.qw[d] = (pb.qw[par] @ loc[d]) if par else loc[d]
            pb.qw[d].normalize()


def blend_poses(R, a, b, w):
    """Per-frame blend of two pose lists (same length after padding): slerp rotations, lerp hips."""
    n = max(len(a), len(b))
    out = []
    for i in range(n):
        pa = a[min(i, len(a) - 1)]
        pb = b[min(i, len(b) - 1)]
        qw = {}
        for nm in R.names:
            qa, qb = pa.qw[nm], pb.qw[nm]
            if qa.dot(qb) < 0:
                qb = -qb
            qw[nm] = qa.slerp(qb, w)
        out.append(Pose(qw, pa.hips.lerp(pb.hips, w),
                        pa.root.lerp(pb.root, w) if pa.root is not None and pb.root is not None else pa.root))
    return out


def floor_fix(R, poses, gs, contact_from=None):
    """Lift every frame whose lowest skinned point is below z = 0; from frame `contact_from` on also lower
    a floating body onto the floor. Returns (min before, max lift)."""
    if gs is None:
        return None, 0.0
    lows = []
    for p in poses:
        M, _ = build(R, p)
        lows.append(A.skin_low_z(R.tr, M, gs))
    if not lows or all(z is None for z in lows):
        return None, 0.0
    lift = []
    for i, z in enumerate(lows):
        z = 0.0 if z is None else z
        lift.append(-z if (contact_from is not None and i >= contact_from) else max(0.0, -z))
    for p, d in zip(poses, lift):
        if abs(d) > 1e-6:
            p.hips = p.hips + Vector((0.0, 0.0, d))
    return min(z for z in lows if z is not None), max(lift)


# =================================================================== writing

def write_poses(R, name, poses, loop):
    """Action from world poses (sign-continuous quaternions, loops closed)."""
    keys = {b.name: {"loc": [], "rot": []} for b in R.tr.order}
    prev = {}
    for p in poses:
        _, basis = build(R, p)
        for b in R.tr.order:
            loc, q = basis[b.name]
            if b.name in prev and prev[b.name].dot(q) < 0.0:
                q.negate()
            prev[b.name] = q.copy()
            keys[b.name]["loc"].append(loc.copy())
            keys[b.name]["rot"].append(q)
    if loop and len(poses) > 1:
        for b in R.tr.order:
            k = keys[b.name]
            k["loc"][-1] = k["loc"][0].copy()
            q0 = k["rot"][0].copy()
            if q0.dot(k["rot"][-2]) < 0:
                q0.negate()
            k["rot"][-1] = q0
    act = A.write_action(R.rig, name, keys, len(poses))
    act.use_cyclic = bool(loop)
    return act


# =================================================================== QA: deformation on the real clips

# same limits as rig_humanoid's pose test (_humanoid_lib.TORN_LIMITS): share of the POSED surface made of
# triangles grown > 3x; 1 % in ordinary poses (arms forward / squat / stride class), 8 % while an arm is
# raised above the head (arms-up class: armpit stretch of linear blend skinning), 4 % of the robe surface
CLIP_TORN_LIMITS = {"main": 0.01, "arms_up": 0.08, "robe": 0.04}
# the pose test has two reference points: arms forward (upper arm 90 deg from hanging) -> main limit, arms up
# (170 deg) -> arms-up limit. A frame takes the limit of its most raised upper arm, linear between them.
ARM_RAISE_DEG = (90.0, 170.0)


def clip_limit(elev_deg, lim):
    """Torn-area limit of a frame whose most raised upper arm is elev_deg from hanging down."""
    a0, a1 = ARM_RAISE_DEG
    w = min(1.0, max(0.0, (elev_deg - a0) / (a1 - a0)))
    return lim["main"] + (lim["arms_up"] - lim["main"]) * w


def _mesh_rest(ob):
    """World rest positions (base mesh = bind pose) and loop triangles of a skinned mesh."""
    me = ob.data
    nv = len(me.vertices)
    co = np.empty(nv * 3)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    M = np.array(ob.matrix_world, dtype=np.float64)
    co = co @ M[:3, :3].T + M[:3, 3]
    me.calc_loop_triangles()
    tri = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("vertices", tri)
    return co, tri.reshape(-1, 3)


def _mesh_posed(ob, dg):
    src = ob.evaluated_get(dg)
    me = src.to_mesh()
    try:
        nv = len(me.vertices)
        co = np.empty(nv * 3)
        me.vertices.foreach_get("co", co)
    finally:
        src.to_mesh_clear()
    co = co.reshape(-1, 3)
    M = np.array(ob.matrix_world, dtype=np.float64)
    return co @ M[:3, :3].T + M[:3, 3]


def _dominant_groups(ob):
    """Per vertex the name of the vertex group with the largest weight (None without groups)."""
    gi = {g.index: g.name for g in ob.vertex_groups}
    out = [None] * len(ob.data.vertices)
    for v in ob.data.vertices:
        best, bw = None, 0.0
        for g in v.groups:
            if g.weight > bw and g.group in gi:
                best, bw = gi[g.group], g.weight
        out[v.index] = best
    return out


class ClipDeformQA:
    """Torn / stretched surface of the character on its ACTUAL clips (after retarget + motion_style).

    The rig_humanoid pose test bends the arms straight up / forward, a deep squat and a long stride - poses
    typical game clips mostly never reach, so a fused cape that tears there may be fine in the game.
    Here every Nth frame of every final clip is evaluated (armature modifier and shape keys included) and
    measured exactly like the pose test: triangles whose area grew more than 3x ("torn"), as a share of the
    POSED surface; floor caps and sliver triangles excluded. A frame takes the main limit while both upper arms
    are within 90 deg of hanging down (the arms-forward reference of the pose test), the arms-up limit at
    170 deg (its arms-up reference) and a linear limit in between (clip_limit); robe/skirt cloth
    (vertices carried by the Hips below the hip joints - the cloth mask of the deaths) also has its own share
    against the robe limit. Per clip: worst frame (relative to its limit), its torn share, the bones
    carrying the tear, the frames over a limit."""

    def __init__(self, R, meshes, cloth_masks=None):
        self.R = R
        self.meshes = []
        H = max(float(R.tr.heads["HeadTop_End"].z) if "HeadTop_End" in R.tr.heads else 1.8, 0.5)
        for ob in meshes:
            if not len(ob.data.vertices) or not len(ob.data.polygons):
                continue
            co, tris = _mesh_rest(ob)
            T0 = co[tris]
            A0 = np.linalg.norm(np.cross(T0[:, 1] - T0[:, 0], T0[:, 2] - T0[:, 0]), axis=1) * 0.5
            ok = (A0 > 0.25 * float(np.median(A0))) & ~(T0[:, :, 2].max(1) < 0.006 * H)
            cm = (cloth_masks or {}).get(ob.name)
            robe = (cm[tris].any(1) & ok) if (cm is not None and len(cm) == len(co)) else np.zeros(len(tris), bool)
            self.meshes.append({"ob": ob, "tris": tris, "A0": A0, "ok": ok, "robe": robe, "dom": None})
        self.robe_tris = int(sum(int(m["robe"].sum()) for m in self.meshes))
        self.tri_count = int(sum(int(m["ok"].sum()) for m in self.meshes))
        # a robe worth its own limit: at least 3 % of the measured surface triangles
        self.has_robe = self.robe_tris > 0.03 * max(self.tri_count, 1)

    def _arm_elevation(self):
        """Largest angle (deg) of an upper arm from pointing straight down, current frame."""
        rig = self.R.rig
        Mw = rig.matrix_world
        worst = 0.0
        for s in ("Left", "Right"):
            nm = self.R.bone(s + "Arm")
            if nm is None:
                continue
            pb = rig.pose.bones[nm]
            v = (Mw @ pb.tail) - (Mw @ pb.head)
            if v.length < 1e-6:
                continue
            worst = max(worst, math.degrees(math.acos(max(-1.0, min(1.0, -v.z / v.length)))))
        return worst

    def measure(self, action, n, every=2, limits=None):
        lim = dict(CLIP_TORN_LIMITS, **(limits or {}))
        scene = bpy.context.scene
        A.assign_action(self.R.rig, action)
        frames = sorted(set(list(range(0, n, max(1, int(every)))) + [n - 1]))
        res = {"frames_measured": len(frames), "every": int(every), "worst_torn_frac": 0.0, "worst_frame": None,
               "worst_class": None, "worst_robe_frac": 0.0 if self.has_robe else None, "worst_robe_frame": None,
               "arms_up_frames": 0, "worst_stretched_frac": 0.0, "ok": True, "over": []}
        worst = None
        for f in frames:
            scene.frame_set(f)
            dg = bpy.context.evaluated_depsgraph_get()
            elev = self._arm_elevation()
            f_lim = clip_limit(elev, lim)
            cls = "main" if elev <= ARM_RAISE_DEG[0] else ("arms_up" if elev >= ARM_RAISE_DEG[1] else "raised_%d" % round(elev))
            if cls != "main":
                res["arms_up_frames"] += 1
            a_tot = a_torn = a_str = r_tot = r_torn = 0.0
            per = []
            for m in self.meshes:
                co = _mesh_posed(m["ob"], dg)
                if len(co) != len(m["ob"].data.vertices):
                    continue
                T1 = co[m["tris"]]
                A1 = np.linalg.norm(np.cross(T1[:, 1] - T1[:, 0], T1[:, 2] - T1[:, 0]), axis=1) * 0.5
                ar = A1 / np.maximum(m["A0"], 1e-15)
                torn = m["ok"] & (ar > 3.0)
                a_tot += float(A1[m["ok"]].sum())
                a_torn += float(A1[torn].sum())
                a_str += float(A1[m["ok"] & (ar > 1.8)].sum())
                if m["robe"].any():
                    r_tot += float(A1[m["robe"]].sum())
                    r_torn += float(A1[m["robe"] & (ar > 3.0)].sum())
                per.append((m, torn, A1))
            frac = a_torn / max(a_tot, 1e-12)
            rfrac = (r_torn / r_tot) if r_tot > 0 else None
            res["worst_stretched_frac"] = max(res["worst_stretched_frac"], a_str / max(a_tot, 1e-12))
            if frac > f_lim:
                res["over"].append([int(f), cls, round(frac, 5)])
            if worst is None or frac / f_lim > worst[0]:
                worst = (frac / f_lim, f, cls, frac, per, f_lim)
            if self.has_robe and rfrac is not None:
                if rfrac > (res["worst_robe_frac"] or 0.0):
                    res["worst_robe_frac"], res["worst_robe_frame"] = rfrac, int(f)
                if rfrac > lim["robe"]:
                    res["over"].append([int(f), "robe", round(rfrac, 5)])
        if worst is not None:
            _, f, cls, frac, per, f_lim = worst
            res.update({"worst_torn_frac": round(frac, 5), "worst_frame": int(f), "worst_class": cls,
                        "worst_limit": round(f_lim, 4)})
            acc = {}
            for m, torn, A1 in per:
                if not torn.any():
                    continue
                if m["dom"] is None:
                    m["dom"] = _dominant_groups(m["ob"])
                for ti in np.nonzero(torn)[0].tolist():
                    bn = m["dom"][int(m["tris"][ti, 0])] or "?"
                    acc[bn] = acc.get(bn, 0.0) + float(A1[ti])
            if acc:
                res["torn_by_bone_m2"] = {k.replace("mixamorig:", ""): round(v, 4)
                                          for k, v in sorted(acc.items(), key=lambda kv: -kv[1])[:4]}
        res["worst_stretched_frac"] = round(res["worst_stretched_frac"], 5)
        if res["worst_robe_frac"] is not None:
            res["worst_robe_frac"] = round(res["worst_robe_frac"], 5)
        res["ok"] = not res["over"]
        res["over_frames"] = len({o[0] for o in res["over"]})
        res["over"] = res["over"][:12]
        return res
