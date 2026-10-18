"""_anim_lib - Mixamo clip library and retargeting core (AstraForge Forge, module "mixamo").

Ported from earlier in-house rig scripts (generalised):
  * retargeting - per-bone swing rest correction, character frame C, hip-height scale k, analytic
    two-bone leg IK, Blender 5 layered action write, foot-slide / hand-twist metrics;
  * scene helpers - assign_action, lock_root_xy, fcurves_of, sample_times;
  * retime_action.
All project path assumptions are removed. Bone names on both sides go through a canonical
(Mixamo short-name) map, so Mixamo (mixamorig:/mixamorig1:/bare), AstraForge browser template,
Rigify DEF-/ORG-/metarig and UE-style skeletons can be mixed freely.

Differences from the original (each one fixes something it solved with per-unit hacks):
  * the rest correction swings a bone towards its *chain child* (Foot->ToeBase, ForeArm->Hand...);
    leaf bones whose tail direction is an importer heuristic (Hand without fingers, Head, ToeBase)
    inherit the swing of their parent instead of aligning that arbitrary tail (the original bent wrists by
    ~27 deg on T-pose clips and patched hand bones per unit);
  * in-place / root extraction / loop seam / speed are applied to the world-space trajectories
    *before* the IK pass, so the feet stay consistent with the processed hips;
  * the knee pole fallback is the character's forward axis, not the bone's local Z (roll-free).
"""
import glob
import hashlib
import json
import math
import os
import re
import shutil
import struct
import time

import bpy
from mathutils import Matrix, Quaternion, Vector

import af_common as af

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
def _default_library():
    """Clip library: ASTRA_FORGE_LIBRARY/animations (set by the server), else C:/AstraForge/library on
    Windows (short paths, the forge default since 11.10.2026), else <project>/data/library/animations."""
    env = os.environ.get("ASTRA_FORGE_LIBRARY")
    if env:
        return os.path.join(env, "animations")
    if os.name == "nt" and os.path.isdir("C:/AstraForge/library"):
        return os.path.join("C:/AstraForge/library", "animations")
    return os.path.join(PROJECT_ROOT, "data", "library", "animations")


DEFAULT_LIBRARY = _default_library()

ID_KINDS = ("objects", "armatures", "meshes", "materials", "images", "textures", "actions",
            "collections", "node_groups", "cameras", "lights")

# =========================================================================== canonical names

SIDES = ("Left", "Right")
CORE = ["Hips", "Spine", "Spine1", "Spine2", "Neck", "Head", "HeadTop_End"]
for _s in SIDES:
    CORE += [_s + n for n in ("Shoulder", "Arm", "ForeArm", "Hand", "UpLeg", "Leg", "Foot",
                              "ToeBase", "Toe_End")]
FINGERS = ("Thumb", "Index", "Middle", "Ring", "Pinky")
CANON = CORE + ["%sHand%s%d" % (s, f, i) for s in SIDES for f in FINGERS for i in range(1, 5)]
CANON_SET = set(CANON)
CANON_LC = {c.lower(): c for c in CANON}
# bones a usable humanoid clip needs on both sides
ESSENTIAL = ["Hips", "Spine", "Head", "LeftArm", "LeftForeArm", "RightArm", "RightForeArm",
             "LeftUpLeg", "LeftLeg", "LeftFoot", "RightUpLeg", "RightLeg", "RightFoot"]

# rest-correction reference: the bone direction is head -> head of the first of these that exists
# in both skeletons (a real joint, unlike importer-made tails of leaf bones)
CHAIN_NEXT = {"Hips": ("Spine", "Spine1", "Spine2"), "Spine": ("Spine1", "Spine2", "Neck"),
              "Spine1": ("Spine2", "Neck"), "Spine2": ("Neck",), "Neck": ("Head",),
              "Head": ("HeadTop_End",)}
for _s in SIDES:
    CHAIN_NEXT.update({
        _s + "Shoulder": (_s + "Arm",), _s + "Arm": (_s + "ForeArm",),
        _s + "ForeArm": (_s + "Hand",),
        _s + "Hand": (_s + "HandMiddle1", _s + "HandIndex1", _s + "HandRing1"),
        _s + "UpLeg": (_s + "Leg",), _s + "Leg": (_s + "Foot",), _s + "Foot": (_s + "ToeBase",),
        _s + "ToeBase": (_s + "Toe_End",)})
    for _f in FINGERS:
        for _i in (1, 2, 3):
            CHAIN_NEXT["%sHand%s%d" % (_s, _f, _i)] = ("%sHand%s%d" % (_s, _f, _i + 1),)

# stick figure for UI previews (<id>.poses.json)
KEY_JOINTS = ["Hips", "Spine", "Spine2", "Neck", "Head", "HeadTop",
              "LeftArm", "LeftForeArm", "LeftHand", "LeftHandTip",
              "RightArm", "RightForeArm", "RightHand", "RightHandTip",
              "LeftUpLeg", "LeftLeg", "LeftFoot", "LeftToeBase",
              "RightUpLeg", "RightLeg", "RightFoot", "RightToeBase"]
STICKS = [("Hips", "Spine"), ("Spine", "Spine2"), ("Spine2", "Neck"), ("Neck", "Head"),
          ("Head", "HeadTop")]
for _s in SIDES:
    STICKS += [("Spine2", _s + "Arm"), (_s + "Arm", _s + "ForeArm"), (_s + "ForeArm", _s + "Hand"),
               (_s + "Hand", _s + "HandTip"), ("Hips", _s + "UpLeg"), (_s + "UpLeg", _s + "Leg"),
               (_s + "Leg", _s + "Foot"), (_s + "Foot", _s + "ToeBase")]


def mirror_name(canon):
    if canon.startswith("Left"):
        return "Right" + canon[4:]
    if canon.startswith("Right"):
        return "Left" + canon[5:]
    return canon


def _astra_table(names):
    """AstraForge browser humanoid template (src/rig-core.js createSkeleton); also VRM-like names."""
    t = {"Hips": "Hips", "Spine": "Spine", "Chest": "Spine2", "Neck": "Neck", "Head": "Head",
         "HeadEnd": "HeadTop_End"}
    if "UpperChest" in names:  # VRM: Spine, Chest, UpperChest
        t.update({"Chest": "Spine1", "UpperChest": "Spine2"})
    for s in SIDES:
        t.update({s + "Shoulder": s + "Shoulder", s + "UpperArm": s + "Arm",
                  s + "LowerArm": s + "ForeArm", s + "Hand": s + "Hand",
                  s + "UpperLeg": s + "UpLeg", s + "LowerLeg": s + "Leg", s + "Foot": s + "Foot",
                  s + "Toes": s + "ToeBase", s + "ToesEnd": s + "Toe_End"})
    return t


def _ue_table(names_lc):
    t = {"pelvis": "Hips", "neck_01": "Neck", "head": "Head"}
    if "spine_05" in names_lc:  # UE5 manny
        t.update({"spine_01": "Spine", "spine_03": "Spine1", "spine_05": "Spine2"})
    else:
        t.update({"spine_01": "Spine", "spine_02": "Spine1", "spine_03": "Spine2"})
    for s, x in (("Left", "l"), ("Right", "r")):
        t.update({"clavicle_" + x: s + "Shoulder", "upperarm_" + x: s + "Arm",
                  "lowerarm_" + x: s + "ForeArm", "hand_" + x: s + "Hand",
                  "thigh_" + x: s + "UpLeg", "calf_" + x: s + "Leg", "foot_" + x: s + "Foot",
                  "ball_" + x: s + "ToeBase"})
        for f in FINGERS:
            for i in (1, 2, 3):
                t["%s_0%d_%s" % (f.lower(), i, x)] = "%sHand%s%d" % (s, f, i)
    return t


def _rigify_table(bare):
    t = {"spine": "Hips", "spine.001": "Spine", "spine.002": "Spine1", "spine.003": "Spine2",
         "spine.004": "Neck", "spine.006": "Head"}
    if "spine.006" not in bare and "spine.005" in bare:
        t["spine.005"] = "Head"
    for s, x in (("Left", "L"), ("Right", "R")):
        t.update({"shoulder." + x: s + "Shoulder", "upper_arm." + x: s + "Arm",
                  "forearm." + x: s + "ForeArm", "hand." + x: s + "Hand",
                  "thigh." + x: s + "UpLeg", "shin." + x: s + "Leg", "foot." + x: s + "Foot",
                  "toe." + x: s + "ToeBase"})
        for i in (1, 2, 3):
            t["thumb.0%d.%s" % (i, x)] = "%sHandThumb%d" % (s, i)
            for f in FINGERS[1:]:
                t["f_%s.0%d.%s" % (f.lower(), i, x)] = "%sHand%s%d" % (s, f, i)
    return t


def map_bone_names(names):
    """Canonical map {bone_name: canonical Mixamo short name} and the detected naming scheme.

    Schemes vote by the number of distinct canonical bones they explain; the winner maps first,
    the others only fill canonical bones still missing (never twice the same canonical)."""
    names = list(names)
    nameset = set(names)
    per = {}
    # mixamo: prefix mixamorig:/mixamorig1:/mixamorig_ (any case) or the exact bare short name
    m = {}
    for n in names:
        base = re.sub(r"\.\d{3}$", "", n)
        mm = re.match(r"^mixamorig\d*[:_](.+)$", base, re.I)
        c = CANON_LC.get(mm.group(1).lower()) if mm else (base if base in CANON_SET else None)
        if c:
            m[n] = c
    per["mixamo"] = m
    astra = _astra_table(nameset)
    per["astraforge"] = {n: astra[re.sub(r"\.\d{3}$", "", n)] for n in names
                         if re.sub(r"\.\d{3}$", "", n) in astra}
    lc = {re.sub(r"\.\d{3}$", "", n).lower(): n for n in names}
    ue = _ue_table(set(lc))
    per["ue"] = {orig: ue[k] for k, orig in lc.items() if k in ue}
    # rigify: DEF- beats bare metarig names beats ORG-
    rank = {"DEF": 0, "": 1, "ORG": 2}
    bare = {}
    for n in names:
        mm = re.match(r"^(DEF|ORG)-(.+)$", n)
        pre, b = (mm.group(1), mm.group(2)) if mm else ("", n)
        if b not in bare or rank[pre] < rank[bare[b][0]]:
            bare[b] = (pre, n)
    rg = _rigify_table(set(bare))
    per["rigify"] = {orig: rg[b] for b, (pre, orig) in bare.items() if b in rg}

    def score(d):
        return len(set(d.values()))
    order = sorted(per, key=lambda k: (-score(per[k]), list(per).index(k)))
    out, used = {}, set()
    for scheme in order:
        for n in names:  # keep armature order -> deterministic
            c = per[scheme].get(n)
            if c and c not in used and n not in out:
                out[n] = c
                used.add(c)
    scheme = order[0] if score(per[order[0]]) else "unknown"
    return scheme, out


def map_armature(arm):
    return map_bone_names([b.name for b in arm.data.bones])


# =========================================================================== scene helpers

def fcurves_of(action):
    """F-curves of a Blender 4.4+/5.0 layered action (action.fcurves no longer exists)."""
    out = []
    for layer in action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                out.extend(bag.fcurves)
    return out


def assign_action(obj, action):
    """Assign an action together with its slot (in 5.0 curves do not apply without a slot)."""
    if obj.animation_data is None:
        obj.animation_data_create()
    obj.animation_data.action = action
    if action is not None and len(action.slots):
        if obj.animation_data.action_slot is None or \
                obj.animation_data.action_slot not in list(action.slots):
            obj.animation_data.action_slot = action.slots[0]


def sample_times(start, end, count, loop):
    """Evenly spaced times; for a loop the last sample does not repeat the first."""
    span = end - start
    if count <= 1:
        return [start]
    step = span / count if loop else span / (count - 1)
    return [start + step * i for i in range(count)]


def lock_root_xy(action, bone, rig=None):
    """Remove horizontal root travel from an already written action (curves stay constant).

    The original assumed Mixamo bone axes (local X and Z horizontal). With `rig` given, the horizontal local
    axes of `bone` are found from its rest orientation in world space instead."""
    axes = (0, 2)
    if rig is not None:
        b = rig.data.bones[bone]
        R = rot3(rig.matrix_world @ b.matrix_local)
        axes = tuple(i for i in range(3)
                     if abs((R @ Vector([1.0 if j == i else 0.0 for j in range(3)])).z) < 0.5)
    path = 'pose.bones["%s"].location' % bone
    for fc in fcurves_of(action):
        if fc.data_path == path and fc.array_index in axes and len(fc.keyframe_points):
            first = fc.keyframe_points[0].co[1]
            for kp in fc.keyframe_points:
                kp.co[1] = first
                kp.handle_left[1] = first
                kp.handle_right[1] = first
            fc.update()
    return axes


def new_layered_action(name, rig):
    act = bpy.data.actions.new(name)
    act.use_fake_user = True
    slot = act.slots.new(id_type="OBJECT", name=rig.name)
    layer = act.layers.new("Layer")
    strip = layer.strips.new(type="KEYFRAME")
    bag = strip.channelbag(slot, ensure=True)
    return act, slot, bag


def channelbag(action):
    return action.layers[0].strips[0].channelbag(action.slots[0], ensure=True)


def retime_action(rig, action, n_frames, warp):
    """Generalised retime: output frame i (0..n_frames-1) takes
    the source action value at frame warp(i); keys are linear, quaternions re-normalised per key.
    The new action replaces `action` under the same name and is returned."""
    name = action.name
    action.name = name + "__src"
    src_bag = channelbag(action)
    act, slot, bag = new_layered_action(name, rig)
    frames = list(range(int(n_frames)))
    for fc in src_bag.fcurves:
        nf = bag.fcurves.new(fc.data_path, index=fc.array_index)
        if fc.group is not None:
            nf.group = bag.groups.get(fc.group.name) or bag.groups.new(fc.group.name)
        nf.keyframe_points.add(len(frames))
        co = []
        for n in frames:
            co += [float(n), fc.evaluate(float(warp(n)))]
        nf.keyframe_points.foreach_set("co", co)
        for kp in nf.keyframe_points:
            kp.interpolation = "LINEAR"
        nf.update()
    quats = {}
    for nf in bag.fcurves:
        if nf.data_path.endswith("rotation_quaternion"):
            quats.setdefault(nf.data_path, [None] * 4)[nf.array_index] = nf
    for fcs in quats.values():
        if None in fcs:
            continue
        for k in range(len(frames)):
            q = Quaternion([fc.keyframe_points[k].co[1] for fc in fcs])
            q.normalize()
            for i, fc in enumerate(fcs):
                fc.keyframe_points[k].co[1] = q[i]
        for fc in fcs:
            fc.update()
    for o in bpy.data.objects:
        ad = o.animation_data
        if ad is not None and ad.action == action:
            assign_action(o, act)
    bpy.data.actions.remove(action)
    act.use_frame_range = True
    act.frame_start, act.frame_end = 0, max(1, len(frames) - 1)
    return act


# =========================================================================== retargeting

def rot3(m):
    """Pure rotation of a matrix with (uniform) scale."""
    return m.to_3x3().normalized().to_quaternion()


def snapshot():
    return {k: set(getattr(bpy.data, k)) for k in ID_KINDS}


def cleanup(before, keep=()):
    for k in ID_KINDS:
        coll = getattr(bpy.data, k)
        for idb in [i for i in coll if i not in before[k] and i not in keep]:
            try:
                coll.remove(idb)
            except Exception:
                pass
    bpy.context.view_layer.update()


def import_fbx_clip(path, drop_meshes=True):
    """Import an animation FBX. Returns (armature, action, fps, has_skin)."""
    scene = bpy.context.scene
    before_obj = set(bpy.data.objects)
    before_act = set(bpy.data.actions)
    bpy.ops.import_scene.fbx(filepath=path, automatic_bone_orientation=False)
    fps = scene.render.fps / (scene.render.fps_base or 1.0)
    new = [o for o in bpy.data.objects if o not in before_obj]
    arms = [o for o in new if o.type == "ARMATURE"]
    if not arms:
        raise RuntimeError("no armature in FBX: %s" % path)
    src = max(arms, key=lambda o: len(o.data.bones))
    has_skin = any(o.type == "MESH" for o in new)
    if drop_meshes:
        for o in new:
            if o.type == "MESH":
                bpy.data.objects.remove(o, do_unlink=True)
        bpy.context.view_layer.update()
    act = src.animation_data.action if src.animation_data else None
    if act is None:
        acts = [a for a in bpy.data.actions if a not in before_act]
        if not acts:
            raise RuntimeError("no animation in FBX: %s" % path)
        act = max(acts, key=lambda a: a.frame_range[1] - a.frame_range[0])
    assign_action(src, act)
    for pb in src.pose.bones:
        if len(pb.constraints):
            pb.constraints.clear()
    return src, act, fps, has_skin


def char_frame(points):
    """Character frame: X = character's left, Z = up, Y = Z x X (backward for a -Y facing body).

    Up is world +Z (contract section 4: characters stand Z-up), so C between two characters is a
    pure yaw. Taking up = Neck - Hips: a hunched A-pose bind and a straight T-pose of the SAME
    skeleton then differ by a pitch and the whole clip is tilted (feet skate on a slope). The
    body axis is used only when a rig lies more than 45 deg off vertical."""
    lat = None
    for a, b in (("LeftUpLeg", "RightUpLeg"), ("LeftArm", "RightArm"),
                 ("LeftShoulder", "RightShoulder")):
        if a in points and b in points and (points[a] - points[b]).length > 1e-6:
            lat = (points[a] - points[b]).normalized()
            break
    if lat is None:
        lat = Vector((1, 0, 0))
    up = Vector((0, 0, 1))
    top = points.get("Neck", points.get("Head", points.get("Spine2")))
    if top is not None and "Hips" in points:
        body = top - points["Hips"]
        if body.length > 1e-6 and body.normalized().dot(up) < math.cos(math.radians(45)):
            up = body.normalized()
    lat = lat - up * lat.dot(up)
    lat = lat.normalized() if lat.length > 1e-9 else Vector((1, 0, 0))
    fwd = up.cross(lat).normalized()
    return Matrix((lat, fwd, up)).transposed()  # columns


def rest_heads(arm, amap):
    W = arm.matrix_world
    return {c: W @ arm.data.bones[n].head_local for n, c in amap.items()}


def rest_height(arm, amap):
    """Hip height at rest (world): mean height of the hip joints (UpLeg heads; the Hips head when
    there are no legs) over the lowest foot/toe head. The hip joints, not the Hips head, decide
    how far the legs reach (the AstraForge template puts them 5 cm below the Hips head, Mixamo
    2 cm). Tails are not used: Mixamo leaf tails point anywhere (37 cm below the
    floor was measured on one model)."""
    heads = rest_heads(arm, amap)
    if "Hips" not in heads:
        return 1.0
    zs = [p.z for c, p in heads.items() if c.endswith(("Foot", "ToeBase", "Toe_End"))]
    if not zs:
        zs = [(arm.matrix_world @ b.head_local).z for b in arm.data.bones]
    legs = [heads[c].z for c in ("LeftUpLeg", "RightUpLeg") if c in heads]
    top = sum(legs) / len(legs) if legs else heads["Hips"].z
    return max(top - min(zs), 1e-4)


def order_bones(arm):
    out = []

    def walk(b):
        out.append(b)
        for c in b.children:
            walk(c)
    for b in arm.data.bones:
        if b.parent is None:
            walk(b)
    return out


def twist_deg(q):
    """Twist about the bone axis (Y) of a local rotation, degrees in (-180, 180]."""
    q = q.normalized()
    return math.degrees(2.0 * math.atan2(q.y, q.w)) if abs(q.w) + abs(q.y) > 1e-9 else 0.0


def wrap180(a):
    return (a + 180.0) % 360.0 - 180.0


def twist_span(vals):
    if not vals:
        return None
    ref = vals[0]
    rel = [wrap180(v - ref) for v in vals]
    return round(max(rel) - min(rel), 1)


def solve_leg(qw, M, Wt, names, target, pole_fallback, reach=None):
    """Analytic two-bone IK in world space: knee stays in the FK knee plane, ankle goes to target.
    Changes the world rotations of thigh and shin in `qw` by shortest swings (roll is kept)."""
    up, lo, ft = names
    H = Wt @ M[up].translation
    K = Wt @ M[lo].translation
    A = Wt @ M[ft].translation
    l1, l2 = (K - H).length, (A - K).length
    if l1 < 1e-6 or l2 < 1e-6:
        return A
    v = target - H
    if reach is not None:
        reach.append(max(0.0, v.length - (l1 + l2) * 0.9995))
    d = min(max(v.length, abs(l1 - l2) + 1e-4), (l1 + l2) * 0.9995)
    u = v.normalized() if v.length > 1e-9 else (A - H).normalized()
    pole = (K - H) - u * (K - H).dot(u)
    if pole.length < 1e-3 * l1:
        pole = pole_fallback - u * pole_fallback.dot(u)
    p = pole.normalized()
    ca = max(-1.0, min(1.0, (l1 * l1 + d * d - l2 * l2) / (2.0 * l1 * d)))
    a = math.acos(ca)
    K2 = H + (u * math.cos(a) + p * math.sin(a)) * l1
    A2 = H + u * d
    s1 = (K - H).rotation_difference(K2 - H)
    qw[up] = s1 @ qw[up]
    shin_dir = s1 @ (A - K)
    s2 = shin_dir.rotation_difference(A2 - K2)
    qw[lo] = s2 @ s1 @ qw[lo]
    return A2


class TargetRig:
    """Static data of the target armature: hierarchy, rest frames, canonical map."""

    def __init__(self, rig, tmap):
        self.rig = rig
        self.tmap = dict(tmap)
        self.cname = {c: n for n, c in self.tmap.items()}
        if "Hips" not in self.cname:
            raise RuntimeError("target armature %s has no Hips-like bone" % rig.name)
        self.order = order_bones(rig)
        self.rel = {b.name: (b.parent.matrix_local.inverted() @ b.matrix_local) if b.parent
                    else b.matrix_local.copy() for b in self.order}
        self.W = rig.matrix_world.copy()
        self.W_inv = self.W.inverted()
        self.W_rot = rot3(self.W)
        self.W_rot_inv = self.W_rot.inverted()
        self.heads = rest_heads(rig, self.tmap)
        self.rest_q = {b.name: rot3(self.W @ b.matrix_local) for b in self.order}
        self.height = rest_height(rig, self.tmap)
        self.frame = char_frame(self.heads)
        self.hips = self.cname["Hips"]
        self.root = None  # animated root bone for root-motion extraction
        self.legs = {}
        for s in SIDES:
            nm = [self.cname.get(s + x) for x in ("UpLeg", "Leg", "Foot")]
            if all(nm):
                self.legs[s] = nm

    def build_pose(self, qa, hips_arm, root_arm=None):
        """Pose matrices (armature space) and bases from armature-space rotations of mapped bones,
        the hips head position and (optionally) the root bone head position."""
        M, basis = {}, {}
        for b in self.order:
            base = (M[b.parent.name] @ self.rel[b.name]) if b.parent else self.rel[b.name].copy()
            loc = Vector((0.0, 0.0, 0.0))
            if b.name == self.root and root_arm is not None:
                loc = base.inverted() @ root_arm
            elif b.name == self.hips and hips_arm is not None:
                loc = base.inverted() @ hips_arm
            if b.name in qa:
                q = rot3(base).inverted() @ qa[b.name]
            else:
                q = Quaternion()
            q.normalize()
            M[b.name] = base @ Matrix.Translation(loc) @ q.to_matrix().to_4x4()
            basis[b.name] = (loc, q)
        return M, basis


def is_descendant(bone, ancestor):
    p = bone.parent
    while p is not None:
        if p == ancestor:
            return True
        p = p.parent
    return False


def reflect_quat(q, n):
    v = Vector((q.x, q.y, q.z))
    v2 = -v + 2.0 * v.dot(n) * n
    return Quaternion((q.w, v2.x, v2.y, v2.z))


def reflect_vec(v, n):
    return v - 2.0 * v.dot(n) * n


def smoothstep(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3.0 - 2.0 * x)


def retarget_clip(tr, src, smap, times, opts):
    """Retarget one imported source armature onto TargetRig `tr` at source frames `times`.

    opts: leg_ik (bool), mirror (bool), loop (bool), loop_blend (frames), facing ('keep' | 'rest':
    the clip's mean yaw of facing_bone - Hips by default - is removed, so a sideways stance such as an
    archer idle faces the character's rest direction), hips_drop (1.0: scale of the hips going below
    their rest height; 0 keeps them at rest height - a robe hem weighted to the hips then stays on the
    floor in a crouching idle), floor (true: the leg IK targets never go below the target's rest
    ankle / ball heights), hand_floor ({"Left"|"Right": dz}: two-bone arm IK keeps that wrist at most
    dz below its rest height, the hand keeps its world orientation - a staff held in it does not dip
    into the floor), motion
    ('in_place' - straight start->end travel removed, sway kept | 'in_place_strict' - all
    horizontal hips motion removed | 'keep' | 'extract'), seam_travel (bool: a kept/extracted loop
    also closes its world travel at the seam - standing loops). Returns (keys, metrics, travel)
    where keys[bone] = {"loc": [...], "rot": [...]} per output frame and travel[i] is the world
    horizontal hips travel of the source (scaled by k), relative to the first frame."""
    scene = bpy.context.scene
    rig = tr.rig
    Ws = src.matrix_world.copy()
    sname = {c: n for n, c in smap.items()}
    if "Hips" not in sname:
        raise RuntimeError("source clip has no Hips bone")
    mirror = bool(opts.get("mirror"))
    s_heads = rest_heads(src, smap)
    min_s = 0.02 * rest_height(src, smap)

    def degenerate(c):
        # a source bone whose chain children all start at its own head (one archer rig's Neck
        # coincides with Head): its rotation moves nothing on the source, so copying it would
        # bend the target's real bone - the target bone follows its parent instead
        kids = [x for x in CHAIN_NEXT.get(c, ()) if x in s_heads]
        return bool(kids) and all((s_heads[x] - s_heads[c]).length < min_s for x in kids)
    degen = sorted(c for c in sname if degenerate(c))
    mapped = {b.name: tr.tmap[b.name] for b in tr.order
              if tr.tmap.get(b.name) in sname and tr.tmap[b.name] not in degen}
    s_rest_q = {c: rot3(Ws @ src.data.bones[n].matrix_local) for c, n in sname.items()}
    Fs = char_frame(s_heads)
    C = (tr.frame @ Fs.transposed()).to_quaternion()
    C_inv = C.inverted()
    k = tr.height / max(rest_height(src, smap), 1e-6)
    n_lat = Vector(Fs.col[0]).normalized()  # source sagittal plane normal (for mirror)

    # ---- virtual rest of every mapped target bone (swing towards the chain child). A reference
    # segment shorter than 2% of the hip height is ignored: some Mixamo skeletons (an archer rig)
    # have a zero-length Neck whose head->Head direction is numerical noise.
    min_t = 0.02 * tr.height
    swing, vrest, dir_bones = {}, {}, {}
    for b in tr.order:
        c = mapped.get(b.name)
        if c is None:
            continue
        sw = None
        for nxt in CHAIN_NEXT.get(c, ()):
            tn = tr.cname.get(nxt)
            if tn is None or nxt not in sname or not is_descendant(rig.data.bones[tn], b):
                continue
            dt = tr.heads[nxt] - tr.heads[c]
            ds = C @ (s_heads[nxt] - s_heads[c])
            if dt.length > min_t and ds.length > min_s:
                sw = dt.rotation_difference(ds)
                dir_bones[b.name] = (c, nxt)
                break
        if sw is None and c in ("Neck", "Head", "HeadTop_End"):
            # no usable reference: rest poses keep the head upright and facing forward, so the
            # head/neck keep their own rest world orientation (inheriting the spine swing would
            # pitch the head when the target's rest spine and neck are not in line)
            sw = Quaternion()
        if sw is None:
            p = b.parent
            while p is not None and p.name not in swing:
                p = p.parent
            if p is not None:
                sw = swing[p.name]
            else:  # original behaviour for a root without a chain child: align own Y axes
                qs = s_rest_q[c]
                sw = (tr.rest_q[b.name] @ Vector((0, 1, 0))).rotation_difference(
                    C @ (qs @ Vector((0, 1, 0))))
        swing[b.name] = sw
        vrest[b.name] = sw @ tr.rest_q[b.name]

    p_rest_s = s_heads["Hips"]
    p_rest_t = tr.heads["Hips"]

    def src_point(pos):
        if mirror:
            pos = p_rest_s + reflect_vec(pos - p_rest_s, n_lat)
        return p_rest_t + (C @ (pos - p_rest_s)) * k

    # ---- pass 1: sample the source
    n = len(times)
    qw = {bn: [] for bn in mapped}
    hips_t, ank_t = [], {s: [] for s in tr.legs}
    toe_t = {s: [] for s in tr.legs}
    src_feet = {s: {"ankle": [], "toe": []} for s in tr.legs}
    src_twist = {s: [] for s in SIDES}
    src_dirs = []
    for t in times:
        f = int(math.floor(t))
        scene.frame_set(f, subframe=float(t - f))
        sp = {c: Ws @ src.pose.bones[nm].matrix for c, nm in sname.items()}
        for bn, c in mapped.items():
            cs = mirror_name(c) if mirror else c
            if cs not in sp:
                cs = c
            delta = rot3(sp[cs]) @ s_rest_q[cs].inverted()
            if mirror:
                delta = reflect_quat(delta, n_lat)
            qw[bn].append(C @ delta @ C_inv @ vrest[bn])
        hips_t.append(src_point(sp["Hips"].translation))
        for s in tr.legs:
            cs = mirror_name(s + "Foot") if mirror else s + "Foot"
            ank_t[s].append(src_point(sp[cs].translation) if cs in sp else None)
            ct = mirror_name(s + "ToeBase") if mirror else s + "ToeBase"
            toe_t[s].append(src_point(sp[ct].translation) if ct in sp else None)
            src_feet[s]["ankle"].append(ank_t[s][-1])
            src_feet[s]["toe"].append(toe_t[s][-1])
        for s in SIDES:
            h, fa = sname.get(s + "Hand"), sname.get(s + "ForeArm")
            if h and fa:
                sh, sf = src.data.bones[h], src.data.bones[fa]
                ql = (rot3(sp[s + "ForeArm"]) @ rot3(sf.matrix_local).inverted() @
                      rot3(sh.matrix_local)).inverted() @ rot3(sp[s + "Hand"])
                src_twist[s].append(twist_deg(ql))
        if not mirror:
            src_dirs.append({c: (sp[nx].translation - sp[c].translation)
                             for bn, (c, nx) in dir_bones.items()})

    # ---- facing: remove the clip's mean yaw (about the vertical through the target's rest hips)
    R_face = Quaternion()
    face_info = None
    fbone = tr.cname.get(opts.get("facing_bone") or "Hips")
    if opts.get("facing", "keep") == "rest" and fbone in qw and qw[fbone]:
        fwd_w = -Vector(tr.frame.col[1])
        fwd_w.z = 0.0
        if fwd_w.length > 1e-9:
            fwd_w.normalize()
            sx = sy = 0.0
            for q in qw[fbone]:
                d = (q @ vrest[fbone].inverted()) @ fwd_w
                d.z = 0.0
                if d.length < 1e-9:
                    continue
                a = math.atan2(fwd_w.cross(d).z, fwd_w.dot(d))
                sx, sy = sx + math.cos(a), sy + math.sin(a)
            yaw = math.atan2(sy, sx) if (sx or sy) else 0.0
            R_face = Quaternion((0.0, 0.0, 1.0), -yaw)
            piv = Vector((p_rest_t.x, p_rest_t.y, 0.0))
            for bn in qw:
                qw[bn] = [R_face @ q for q in qw[bn]]
            hips_t = [piv + R_face @ (h - piv) for h in hips_t]
            for s in tr.legs:
                ank_t[s] = [piv + R_face @ (a - piv) if a is not None else None for a in ank_t[s]]
                toe_t[s] = [piv + R_face @ (a - piv) if a is not None else None for a in toe_t[s]]
                src_feet[s] = {"ankle": list(ank_t[s]), "toe": list(toe_t[s])}
            face_info = {"bone": fbone, "yaw_removed_deg": round(math.degrees(yaw), 2)}

    # ---- hips_drop: how much of the hips going below their rest height is kept
    hips_drop = float(opts.get("hips_drop", 1.0))
    drop_info = None
    if hips_drop != 1.0:
        lows = [h.z - p_rest_t.z for h in hips_t]
        hips_t = [Vector((h.x, h.y, p_rest_t.z + dz * hips_drop)) if dz < 0 else h
                  for h, dz in zip(hips_t, lows)]
        drop_info = {"scale": hips_drop, "source_lowest_cm": round(100 * min(lows), 1),
                     "kept_lowest_cm": round(100 * min(0.0, min(lows)) * hips_drop, 1)}

    # ---- root travel (world horizontal) and body-relative trajectories.
    # 'in_place' removes only the straight start->end travel (the constant velocity the game
    # supplies, clips.json ground_speed_mps) and keeps the sway/lurch around it: removing the sway
    # too ('in_place_strict', the original behaviour) makes planted feet skate by the sway when the game moves
    # the character at a constant speed.
    motion = opts.get("motion", "in_place")
    travel = [Vector(((p - hips_t[0]).x, (p - hips_t[0]).y, 0.0)) for p in hips_t]
    if motion == "in_place":
        removed = [travel[-1] * (i / max(n - 1, 1)) for i in range(n)]
    else:
        removed = [v.copy() for v in travel]
    hips_b = [p - tr_ for p, tr_ in zip(hips_t, removed)]
    ank_b = {s: [(a - tr_) if a is not None else None for a, tr_ in zip(v, removed)]
             for s, v in ank_t.items()}
    toe_b = {s: [(a - tr_) if a is not None else None for a, tr_ in zip(v, removed)]
             for s, v in toe_t.items()}

    # ---- loop seam: distribute the last->first difference over the tail (slerp of the delta)
    blend = 0
    if opts.get("loop") and n >= 4:
        blend = max(1, min(int(opts.get("loop_blend", 8)), n // 3))
        for bn, qs in qw.items():
            d = qs[0] @ qs[-1].inverted()
            if d.w < 0:
                d.negate()
            for j in range(n - 1 - blend, n):
                w = smoothstep((j - (n - 1 - blend)) / blend)
                if w > 0:
                    qs[j] = Quaternion().slerp(d, w) @ qs[j]
        dh = hips_b[0] - hips_b[-1]
        for j in range(n - 1 - blend, n):
            w = smoothstep((j - (n - 1 - blend)) / blend)
            hips_b[j] = hips_b[j] + dh * w
        for s, v in list(ank_b.items()) + list(toe_b.items()):
            if v[0] is None or v[-1] is None:
                continue
            da = v[0] - v[-1]
            for j in range(n - 1 - blend, n):
                w = smoothstep((j - (n - 1 - blend)) / blend)
                v[j] = v[j] + da * w

    if blend and opts.get("seam_travel") and motion in ("keep", "extract"):
        # a standing loop (idle) keeps its hips sway in the world: the small last->first travel
        # difference is closed over the same tail, so body AND feet loop without a pop. Never done
        # for locomotion: there the travel is the forward progress, not a seam error.
        dtr = travel[0] - travel[-1]
        for j in range(n - 1 - blend, n):
            w = smoothstep((j - (n - 1 - blend)) / blend)
            travel[j] = travel[j] + dtr * w

    def add_travel(d):
        return {s: [(a + t) if a is not None else None for a, t in zip(v, travel)]
                for s, v in d.items()}
    if motion in ("keep", "extract"):
        hips_w = [h + t for h, t in zip(hips_b, travel)]
        ank_w, toe_w = add_travel(ank_b), add_travel(toe_b)
        root_tr = travel if motion == "extract" else None
    else:
        hips_w, ank_w, toe_w, root_tr = hips_b, ank_b, toe_b, None

    # ---- foot pin: the ball of the foot (ToeBase head) is pinned while the source ball is on
    # the ground, the ankle otherwise (smoothed over 5 frames). With other foot proportions an
    # ankle-only pin lets the ball skate during heel-off.
    foot_pin = opts.get("foot_pin", "ball")
    wball, toe_vec = {}, {}
    for s, nm in tr.legs.items():
        tn = tr.cname.get(s + "ToeBase")
        zs = [p.z for p in toe_t[s] if p is not None]
        if foot_pin != "ball" or tn is None or len(zs) != n or (s + "ToeBase") not in tr.heads:
            continue
        zmin = min(zs)
        raw_w = [1.0 if p.z - zmin < 0.02 else 0.0 for p in toe_t[s]]
        wball[s] = [sum(raw_w[max(0, j - 2): j + 3]) / len(raw_w[max(0, j - 2): j + 3])
                    for j in range(n)]
        toe_vec[s] = (tn, tr.heads[s + "ToeBase"] - tr.heads[s + "Foot"])

    root_rest_w = None
    if root_tr is not None:
        if tr.root is None:
            raise RuntimeError("root motion extraction needs a root bone (see prepare_rig)")
        root_rest_w = tr.W @ rig.data.bones[tr.root].head_local

    # ---- pass 2: pose, leg IK, keys
    leg_ik = bool(opts.get("leg_ik", True)) and bool(tr.legs)
    floor = bool(opts.get("floor", True))
    floor_z = {s: (tr.heads[s + "Foot"].z if (s + "Foot") in tr.heads else None) for s in tr.legs}
    ball_z = {s: (tr.heads[s + "ToeBase"].z if (s + "ToeBase") in tr.heads else None) for s in tr.legs}
    floor_frames = {s: 0 for s in tr.legs}
    hand_floor = {}
    for s, dz in (opts.get("hand_floor") or {}).items():
        nm = [tr.cname.get(s + x) for x in ("Arm", "ForeArm", "Hand")]
        if all(nm) and (s + "Hand") in tr.heads:
            hand_floor[s] = (nm, tr.heads[s + "Hand"].z - float(dz))
    hand_floor_frames = {s: 0 for s in hand_floor}
    gs = opts.get("ground_samples") if opts.get("ground") == "mesh" else None
    frame_state, low_z = [], []
    fwd_t = -Vector(tr.frame.col[1])  # character forward (knees bend this way)
    keys = {b.name: {"loc": [], "rot": []} for b in tr.order}
    feet = {s: [] for s in tr.legs}
    tgt_twist = {s: [] for s in SIDES}
    ik_offset, ik_offset_ball = {}, {}
    unreached = {s: 0 for s in tr.legs}
    worst_miss = 0.0
    prev_q = {}
    dir_err = 0.0
    hips_name = tr.hips
    for i in range(n):
        qf = {bn: qs[i].copy() for bn, qs in qw.items()}
        hips_arm = tr.W_inv @ hips_w[i]
        root_arm = (tr.W_inv @ (root_rest_w + root_tr[i])) if root_tr is not None else None
        qa = {bn: tr.W_rot_inv @ q for bn, q in qf.items()}
        M, basis = tr.build_pose(qa, hips_arm, root_arm)
        if leg_ik:
            hips_delta = (qf[hips_name] @ vrest[hips_name].inverted()) if hips_name in qf \
                else Quaternion()
            pole = hips_delta @ fwd_t
            changed = False
            for s, nm in tr.legs.items():
                if ank_w[s][i] is None or nm[0] not in qf or nm[1] not in qf:
                    continue
                ankle_fk = tr.W @ M[nm[2]].translation
                if s not in ik_offset:  # target's own stance, taken from the first frame
                    ik_offset[s] = ankle_fk - ank_w[s][0]
                target = ank_w[s][i] + ik_offset[s]
                if s in wball and nm[2] in qf and toe_w[s][i] is not None:
                    tn, rvec = toe_vec[s]
                    if s not in ik_offset_ball:
                        ik_offset_ball[s] = (tr.W @ M[tn].translation) - toe_w[s][0]
                    D = (qf[nm[2]] @ tr.rest_q[nm[2]].inverted()) @ rvec
                    ball = toe_w[s][i] + ik_offset_ball[s]
                    if floor and ball_z[s] is not None and ball.z < ball_z[s]:
                        ball = Vector((ball.x, ball.y, ball_z[s]))
                    from_ball = ball - D
                    target = target.lerp(from_ball, wball[s][i])
                if floor and floor_z[s] is not None and target.z < floor_z[s] - 1e-4:
                    target = Vector((target.x, target.y, floor_z[s]))
                    floor_frames[s] += 1
                miss = []
                solve_leg(qf, M, tr.W, nm, target, pole, miss)
                if miss and miss[0] > 0.005:
                    unreached[s] += 1
                    worst_miss = max(worst_miss, miss[0])
                changed = True
            if changed:
                qa = {bn: tr.W_rot_inv @ q for bn, q in qf.items()}
                M, basis = tr.build_pose(qa, hips_arm, root_arm)
        if hand_floor:
            moved = False
            for s, (nm, zmin) in hand_floor.items():
                if any(x not in qf for x in nm):
                    continue
                wrist = tr.W @ M[nm[2]].translation
                if wrist.z >= zmin - 1e-4:
                    continue
                solve_leg(qf, M, tr.W, nm, Vector((wrist.x, wrist.y, zmin)), -fwd_t)
                hand_floor_frames[s] += 1
                moved = True
            if moved:
                qa = {bn: tr.W_rot_inv @ q for bn, q in qf.items()}
                M, basis = tr.build_pose(qa, hips_arm, root_arm)
        if gs is not None:
            frame_state.append(({bn: q.copy() for bn, q in qa.items()}, hips_arm.copy(),
                                root_arm.copy() if root_arm is not None else None))
            low_z.append(skin_low_z(tr, M, gs))
        for b in tr.order:
            loc, q = basis[b.name]
            if b.name in prev_q and prev_q[b.name].dot(q) < 0.0:
                q.negate()
            prev_q[b.name] = q.copy()
            keys[b.name]["loc"].append(loc.copy())
            keys[b.name]["rot"].append(q)
        for s, nm in tr.legs.items():
            feet[s].append(tr.W @ M[nm[2]].translation)
        for s in SIDES:
            h = tr.cname.get(s + "Hand")
            if h and h in mapped:
                tgt_twist[s].append(twist_deg(basis[h][1]))
        if src_dirs:
            for bn, (c, nx) in dir_bones.items():
                if not c.endswith(("Arm", "ForeArm")):
                    continue
                if hand_floor and any(bn in nm_ for nm_, _ in hand_floor.values()):
                    continue   # the arm IK changes these on purpose
                td = tr.W_rot @ (M[tr.cname[nx]].translation - M[bn].translation)
                sd = R_face @ (C @ src_dirs[i][c])
                if td.length > 1e-9 and sd.length > 1e-9:
                    dir_err = max(dir_err, math.degrees(td.angle(sd)))

    # ---- ground "mesh": a frame whose lowest skinned point (a sample of the body vertices) is below
    # the floor lifts the whole body by that much (smoothed over +-2 frames) - a robe lying in a death
    # clip rests on the floor instead of sinking half into it. The feet lift with it.
    ground_info = None
    if gs is not None and low_z:
        tol = float(opts.get("ground_tolerance", 0.0))
        need = [max(0.0, -(z + tol)) if z is not None else 0.0 for z in low_z]
        win = [max(need[max(0, j - 2): j + 3]) for j in range(n)]
        lift = [sum(win[max(0, j - 2): j + 3]) / len(win[max(0, j - 2): j + 3]) for j in range(n)]
        lift = [max(a, b_) for a, b_ in zip(lift, need)]
        lifted = 0
        for i, d in enumerate(lift):
            if d <= 1e-5:
                continue
            qa_i, h_i, r_i = frame_state[i]
            h2 = h_i + tr.W_rot_inv @ Vector((0.0, 0.0, d))
            M2, basis2 = tr.build_pose(qa_i, h2, r_i)
            for b in tr.order:
                loc, q = basis2[b.name]
                prevq = keys[b.name]["rot"][i - 1] if i > 0 else None
                if prevq is not None and prevq.dot(q) < 0.0:
                    q.negate()
                keys[b.name]["loc"][i] = loc.copy()
                keys[b.name]["rot"][i] = q
            for s, nm in tr.legs.items():
                feet[s][i] = tr.W @ M2[nm[2]].translation
            hips_w[i] = hips_w[i] + Vector((0.0, 0.0, d))
            lifted += 1
        ground_info = {"mode": "mesh", "samples": int(len(gs["P"])), "lifted_frames": lifted,
                       "max_lift_cm": round(100 * max(lift), 1),
                       "lowest_before_cm": round(100 * min(z for z in low_z if z is not None), 1)}

    metrics = {
        "k_hip": round(k, 4), "leg_ik": leg_ik, "loop_blend_frames": blend,
        "mapped_bones": sorted(mapped), "unmapped_bones": [b.name for b in tr.order
                                                         if b.name not in mapped],
        "source_bones": len(src.data.bones), "degenerate_source_bones": degen,
        "arm_dir_err_deg": None if mirror else round(dir_err, 2),
        "hand_twist_deg": {s: {"source": twist_span(src_twist[s]),
                               "target": twist_span(tgt_twist[s])} for s in SIDES},
        "ik_targets": {s: [a + ik_offset.get(s, Vector()) if a is not None else None
                           for a in v] for s, v in ank_w.items()},
        "feet_internal": feet,
        "src_feet": src_feet,
        "hips_internal": hips_w,
        "C_angle_deg": round(math.degrees(C.angle), 2),
        "ball_pinned_frames": {s: sum(1 for w in v if w > 0.5) for s, v in wball.items()},
        "ik_unreached_frames": unreached, "ik_worst_miss_cm": round(worst_miss * 100, 1),
        "facing": face_info, "hips_drop": drop_info, "ground": ground_info,
        "floor_clamped_frames": floor_frames if floor else None,
        "hand_floor_frames": hand_floor_frames or None,
    }
    return keys, metrics, travel


def _mesh_world_min_z(ob, dg=None):
    """Lowest world z of a mesh object (evaluated - skinned - when dg is given)."""
    import numpy as np
    src = ob.evaluated_get(dg) if dg is not None else ob
    me = src.to_mesh() if dg is not None else ob.data
    try:
        nv = len(me.vertices)
        if nv == 0:
            return None
        co = np.empty(nv * 3, dtype=np.float64)
        me.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        M = np.array(ob.matrix_world, dtype=np.float64)
        z = co @ M[2, :3] + M[2, 3]
        return float(z.min())
    finally:
        if dg is not None:
            src.to_mesh_clear()


def floor_report(rig, meshes, props, action, n, samples=16):
    """Lowest point of the skinned body and of the props (meshes held under the rig) over `samples`
    frames of `action` (0..n-1). z = 0 is the floor (contract section 4). Returns a dict."""
    scene = bpy.context.scene
    assign_action(rig, action)
    frames = sorted({int(round(i * (n - 1) / max(samples - 1, 1))) for i in range(max(samples, 2))})
    body_min = prop_min = None
    body_f = prop_f = None
    for f in frames:
        scene.frame_set(f)
        dg = bpy.context.evaluated_depsgraph_get()
        for ob in meshes:
            z = _mesh_world_min_z(ob, dg)
            if z is not None and (body_min is None or z < body_min):
                body_min, body_f = z, f
        for ob in props:
            z = _mesh_world_min_z(ob, None)
            if z is not None and (prop_min is None or z < prop_min):
                prop_min, prop_f = z, f
    return {"frames_sampled": len(frames),
            "body_min_z_m": None if body_min is None else round(body_min, 4), "body_min_frame": body_f,
            "props_min_z_m": None if prop_min is None else round(prop_min, 4), "props_min_frame": prop_f,
            "props": [o.name for o in props]}


def skin_samples(rig, meshes, n=2000, seed=0):
    """A random sample of the skinned body vertices for the ground constraint: world rest positions,
    up to 4 bone influences each (rig bone names) and the inverse rest matrices of those bones."""
    import numpy as np
    rng = np.random.default_rng(seed)
    P, BI, BW = [], [], []
    bones = [b.name for b in rig.data.bones]
    bidx = {nm: i for i, nm in enumerate(bones)}
    total = max(1, sum(len(m.data.vertices) for m in meshes))
    for ob in meshes:
        me = ob.data
        groups = {g.index: g.name for g in ob.vertex_groups}
        nv = len(me.vertices)
        if nv == 0:
            continue
        take = rng.choice(nv, size=min(nv, max(1, int(n * nv / total))), replace=False)
        M = ob.matrix_world
        for vi in take.tolist():
            v = me.vertices[vi]
            inf = sorted(((g.weight, groups.get(g.group)) for g in v.groups if groups.get(g.group) in bidx),
                         reverse=True)[:4]
            tot = sum(w for w, _ in inf)
            if tot <= 1e-6:
                continue
            P.append(tuple(M @ v.co))
            BI.append([bidx[nm] for _, nm in inf] + [0] * (4 - len(inf)))
            BW.append([w / tot for w, _ in inf] + [0.0] * (4 - len(inf)))
    W = rig.matrix_world
    rest_inv = [(W @ b.matrix_local).inverted() for b in rig.data.bones]
    return {"P": np.array(P, dtype=np.float64).reshape(-1, 3), "BI": np.array(BI, dtype=np.int64).reshape(-1, 4),
            "BW": np.array(BW, dtype=np.float64).reshape(-1, 4), "bones": bones, "rest_inv": rest_inv}


def skin_low_z(tr, M, gs):
    """Lowest world z of the sampled skinned vertices for pose matrices M (armature space)."""
    import numpy as np
    if not len(gs["P"]):
        return None
    S = np.zeros((len(gs["bones"]), 4, 4))
    for i, nm in enumerate(gs["bones"]):
        S[i] = np.array(tr.W @ M[nm] @ gs["rest_inv"][i]) if nm in M else np.eye(4)
    P = np.concatenate([gs["P"], np.ones((len(gs["P"]), 1))], axis=1)
    out = np.zeros((len(P), 4))
    for k in range(4):
        out += gs["BW"][:, k:k + 1] * np.einsum("nij,nj->ni", S[gs["BI"][:, k]], P)
    return float(out[:, 2].min())


def write_action(rig, name, keys, n):
    """Layered (Blender 4.4+/5.0) action with linear keys on frames 0..n-1 for every bone:
    location, rotation_quaternion and scale (unmapped bones get rest values, so switching clips
    never inherits values from a previous clip)."""
    old = bpy.data.actions.get(name)
    if old is not None:
        old.name = name + "__old"
    act, slot, bag = new_layered_action(name, rig)
    frames = list(range(n))

    def curve(path, idx, values, grp):
        fc = bag.fcurves.new(path, index=idx)
        fc.group = grp
        fc.keyframe_points.add(n)
        co = []
        for f, v in zip(frames, values):
            co += [float(f), float(v)]
        fc.keyframe_points.foreach_set("co", co)
        for kp in fc.keyframe_points:
            kp.interpolation = "LINEAR"
        fc.update()

    for bname, kv in keys.items():
        grp = bag.groups.new(bname)
        base = 'pose.bones["%s"].' % bname
        for i in range(3):
            curve(base + "location", i, [v[i] for v in kv["loc"]], grp)
        for i in range(4):
            curve(base + "rotation_quaternion", i, [q[i] for q in kv["rot"]], grp)
        for i in range(3):
            curve(base + "scale", i, [1.0] * n, grp)
    act.use_frame_range = True
    act.frame_start, act.frame_end = 0, max(1, n - 1)
    if old is not None:
        for o in bpy.data.objects:
            ad = o.animation_data
            if ad is not None and ad.action == old:
                assign_action(o, act)
        bpy.data.actions.remove(old)
    return act


# =========================================================================== target helpers

def find_target_rig(objects=None):
    """Armature with the most canonical bones (Hips required). Returns (rig, scheme, map)."""
    src = objects if objects is not None else bpy.context.scene.objects
    best = None
    for o in src:
        if o.type != "ARMATURE":
            continue
        scheme, amap = map_armature(o)
        if "Hips" not in amap.values():
            continue
        if best is None or len(amap) > len(best[2]):
            best = (o, scheme, amap)
    if best is None:
        raise RuntimeError("no humanoid armature with a Hips-like bone found")
    return best


def skinned_meshes(rig):
    out = []
    for o in bpy.context.scene.objects:
        if o.type != "MESH":
            continue
        if o.parent == rig or any(m.type == "ARMATURE" and m.object == rig for m in o.modifiers):
            out.append(o)
    return out


def _add_unique(lst, items):
    for o in items:
        if o is not None and o not in lst:
            lst.append(o)


def export_objects(rig, meshes):
    """Everything that belongs to the character in the GLB: the rig with its whole hierarchy
    (bone-parented socket empties, props parented to sockets, child meshes), skinned meshes bound
    only by modifier with their children, and every contract section 6 socket of the scene (a socket
    outside the rig hierarchy is still exported, with a note). Returns (objects, notes)."""
    objs, notes = [rig], []
    _add_unique(objs, rig.children_recursive)
    for m in meshes:
        _add_unique(objs, [m] + list(m.children_recursive))
    for o in bpy.context.scene.objects:
        if "game_socket" in o.keys() and o not in objs:
            _add_unique(objs, [o] + list(o.children_recursive))
            notes.append("socket %s is not in the hierarchy of %s - exported, but it does not "
                         "follow the animation" % (o.name, rig.name))
    objs = [o for o in objs if o.type not in ("CAMERA", "LIGHT")]
    return objs, notes


def _slot_users(action):
    users = []
    for sl in action.slots:
        try:
            _add_unique(users, sl.users())
        except Exception:
            pass
    return users


def _bone_channels(action):
    names = set()
    for fc in fcurves_of(action):
        m = re.match(r'^pose\.bones\["(.+?)"\]', fc.data_path)
        if m:
            names.add(m.group(1))
    return names


def rig_actions(rig, meshes=()):
    """Actions that animate the target rig or its skinned meshes (as objects): assigned or in an NLA
    strip, a slot used by them, or an orphan armature action whose bone channels are this rig's
    bones (left-overs of an import or of an earlier retarget). Shape-key actions and actions of
    other objects or armatures are not returned."""
    ids = [rig] + list(meshes)
    out = []
    for o in ids:
        ad = o.animation_data
        if ad is None:
            continue
        _add_unique(out, [ad.action])
        for t in ad.nla_tracks:
            _add_unique(out, [st.action for st in t.strips])
    bones = {b.name for b in rig.data.bones}
    for a in bpy.data.actions:
        if a in out:
            continue
        users = _slot_users(a)
        if any(u in ids for u in users):
            out.append(a)
            continue
        if users:
            continue  # animates something else
        names = _bone_channels(a)
        if names and len(names & bones) >= 0.8 * len(names):
            out.append(a)
    return out


class gltf_only_actions:
    """Context manager for the GLB export: only `keep` actions reach the file.

    The Blender 5.0 glTF exporter (ACTIONS mode, single armature) adds EVERY armature action of the
    file to the rig, and exports the assigned action of every other exported object as its own
    animation. Inside the block the actions of exported non-rig objects are detached, NLA tracks are
    muted and all other actions sit on the exporter's own action filter; everything is restored on
    exit, so the saved .blend keeps the unrelated animation."""

    def __init__(self, keep, objects, rig):
        self.keep, self.objects, self.rig = list(keep), list(objects), rig
        self.notes, self._saved, self._muted, self._registered = [], [], [], False

    def __enter__(self):
        for o in self.objects:
            ad = o.animation_data
            if ad is None:
                continue
            if o != self.rig and ad.action is not None:
                self._saved.append((o, ad.action, ad.action_slot))
                self.notes.append("animation %s of %s is not exported (only the clips of %s)" %
                                  (ad.action.name, o.name, self.rig.name))
                ad.action = None
            for t in ad.nla_tracks:
                if not t.mute:
                    t.mute = True
                    self._muted.append(t)
        drop = [a for a in bpy.data.actions if a not in self.keep]
        if drop:
            try:
                if not hasattr(bpy.types.Scene, "gltf_action_filter"):
                    import io_scene_gltf2
                    bpy.types.Scene.gltf_action_filter = bpy.props.CollectionProperty(
                        type=io_scene_gltf2.GLTF2_filter_action)
                    bpy.types.Scene.gltf_action_filter_active = bpy.props.IntProperty()
                    self._registered = True
                flt = bpy.data.scenes[0].gltf_action_filter
                for a in drop:
                    it = flt.add()
                    it.action = a
                    it.keep = False
            except Exception as exc:  # exporter changed: report, do not fail the step
                self.notes.append("could not exclude %d unrelated action(s) from the GLB: %s" %
                                  (len(drop), exc))
        return self

    def __exit__(self, *exc):
        try:
            if hasattr(bpy.types.Scene, "gltf_action_filter"):
                bpy.data.scenes[0].gltf_action_filter.clear()
                if self._registered:
                    del bpy.types.Scene.gltf_action_filter
                    del bpy.types.Scene.gltf_action_filter_active
        except Exception:
            pass
        for t in self._muted:
            t.mute = False
        for o, act, slot in self._saved:
            o.animation_data.action = act
            if slot is not None:
                try:
                    o.animation_data.action_slot = slot
                except Exception:
                    pass
        return False


def transform_state(obj):
    """Object-level rotation (deg) and scale of matrix_world (contract section 4 wants 0 / 1)."""
    loc, rot, sc = obj.matrix_world.decompose()
    a = math.degrees(rot.angle)
    return {"rotation_deg": round(min(a, 360.0 - a), 3), "scale": [round(s, 5) for s in sc],
            "location": [round(v, 4) for v in loc],
            "identity": min(a, 360.0 - a) < 0.01 and max(abs(s - 1.0) for s in sc) < 1e-4}


def apply_rig_transforms(rig, meshes):
    """Bake the rotation and scale of the rig and of the meshes of its hierarchy into the data
    (bone rests, vertices), as Ctrl+A on all of them. Location is kept. Every other object of the
    hierarchy (bone-parented socket empties, props) keeps its world matrix: transform_apply does
    not compensate bone-parented children (a hand socket moved 13 cm on a 1.5x rig)."""
    objs = [rig]
    _add_unique(objs, [o for o in rig.children_recursive if o.type == "MESH"])
    _add_unique(objs, meshes)
    rest = []
    for o in [rig] + list(meshes):
        _add_unique(rest, [c for c in o.children_recursive if c not in objs])
    def depth(o):
        k = 0
        while o.parent is not None:
            o, k = o.parent, k + 1
        return k
    keep = sorted(((o, o.matrix_world.copy()) for o in rest), key=lambda it: depth(it[0]))
    af.select_only(objs)
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    bpy.context.view_layer.update()
    for o, m in keep:  # parents before children
        if "game_socket" in o.keys():  # a socket is a frame (size = size_class): unit scale
            loc, rot, _sc = m.decompose()
            m = Matrix.LocRotScale(loc, rot, Vector((1.0, 1.0, 1.0)))
        o.matrix_world = m
        bpy.context.view_layer.update()
    return [o.name for o in objs]


def _edit_rig(rig):
    for o in bpy.context.view_layer.objects:
        o.select_set(False)
    rig.hide_set(False)
    rig.hide_viewport = False
    rig.select_set(True)
    bpy.context.view_layer.objects.active = rig
    bpy.ops.object.mode_set(mode="EDIT")


def prepare_rig(rig, hips_name, want_root=False):
    """Make the Hips translatable (disconnect) and, for root-motion extraction, find or create a
    non-deforming root bone above the Hips. Returns (root_bone_name or None, notes)."""
    notes = []
    hips = rig.data.bones[hips_name]
    root = None
    if want_root and hips.parent is not None:
        p = hips.parent
        while p.parent is not None:
            p = p.parent
        root = p.name
        notes.append("root motion goes to existing bone %s" % root)
    need_edit = hips.use_connect or (want_root and root is None)
    if need_edit:
        _edit_rig(rig)
        eb = rig.data.edit_bones
        if eb[hips_name].use_connect:
            eb[hips_name].use_connect = False
            notes.append("Hips was connected to its parent; disconnected so it can translate")
        if want_root and root is None:
            W_inv = rig.matrix_world.inverted()
            head = W_inv @ Vector((0.0, 0.0, 0.0))
            up = (W_inv.to_3x3() @ Vector((0.0, 0.0, 0.15)))
            name = "root" if "root" not in eb else "af_root"
            r = eb.new(name)
            r.head = head
            r.tail = head + up
            r.roll = 0.0
            r.use_deform = False
            eb[hips_name].parent = r
            eb[hips_name].use_connect = False
            root = name
            notes.append("added non-deforming bone '%s' at the origin as Hips parent" % name)
        bpy.ops.object.mode_set(mode="OBJECT")
    for pb in rig.pose.bones:
        if pb.rotation_mode != "QUATERNION":
            pb.rotation_mode = "QUATERNION"
    return root, notes


# =========================================================================== evaluation / QA

def _finite_matrix(m):
    return all(math.isfinite(x) for row in m for x in row)


def foot_slide(tracks, fps, tol=0.02, step_speed=0.5):
    """Skating of the foot's contact point, relative to the best constant-velocity ground (an
    in-place walk's treadmill motion is not counted).

    tracks: {side: {"ankle": [Vector], "toe": [Vector or None]}} in the ground frame. Contact
    candidate per frame: the ball (ToeBase head) within `tol` of its lowest height, else the ankle
    (heel strike) within `tol` of its lowest height; only consecutive frames with the same contact
    point pair up. Pairs moving faster than `step_speed` m/s relative to the ground are deliberate
    steps or drags (counted as `moving` frames, not as skating). Returns a dict:
    slide_cm (sum of the slow residual motion), raw_cm (all candidate motion without ground
    compensation), ground_speed_mps, moving_frames."""
    pairs = []
    for tr in tracks.values():
        if isinstance(tr, list):
            tr = {"ankle": tr}
        ank = tr.get("ankle") or []
        toe = tr.get("toe") or [None] * len(ank)
        mins = {}
        for key, seq in (("ankle", ank), ("toe", toe)):
            zs = [p.z for p in seq if p is not None]
            mins[key] = min(zs) if zs else None

        def contact(i):
            t = toe[i] if i < len(toe) else None
            if t is not None and mins["toe"] is not None and t.z - mins["toe"] < tol:
                return "toe", t
            a = ank[i]
            if a is not None and mins["ankle"] is not None and a.z - mins["ankle"] < tol:
                return "ankle", a
            return None, None
        prev = contact(0) if ank else (None, None)
        for i in range(1, len(ank)):
            cur = contact(i)
            if cur[0] is not None and cur[0] == prev[0]:
                pairs.append(Vector((cur[1].x - prev[1].x, cur[1].y - prev[1].y)))
            prev = cur
    if not pairs:
        return {"slide_cm": 0.0, "raw_cm": 0.0, "ground_speed_mps": 0.0, "moving_frames": 0}
    xs = sorted(p.x for p in pairs)
    ys = sorted(p.y for p in pairs)
    v = Vector((xs[len(xs) // 2], ys[len(ys) // 2]))
    lim = step_speed / max(fps, 1e-6)
    slide, moving = 0.0, 0
    for p in pairs:
        r = (p - v).length
        if r < lim:
            slide += r
        else:
            moving += 1
    return {"slide_cm": round(slide * 100.0, 1),
            "raw_cm": round(sum(p.length for p in pairs) * 100.0, 1),
            "ground_speed_mps": round(v.length * fps, 3), "moving_frames": moving}


def _low(seq, tol):
    zs = [p.z for p in seq if p is not None]
    if not zs:
        return None
    zmin = min(zs)
    return [p is not None and p.z - zmin < tol for p in seq]


def source_ground_velocity(src_feet, tol=0.02):
    """Per-frame XY velocity of the ground under the source feet: the median step of the low
    contact point (ball, else ankle). ~0 for standing clips and clips with root motion; for an
    "In Place" walk the planted feet slide backwards at the treadmill speed."""
    steps = []
    for tr in src_feet.values():
        for key in ("toe", "ankle"):
            seq = tr.get(key) or []
            low = _low(seq, tol)
            if low is None:
                continue
            for i in range(1, len(seq)):
                if low[i] and low[i - 1]:
                    steps.append(Vector((seq[i].x - seq[i - 1].x, seq[i].y - seq[i - 1].y, 0.0)))
            break
    if not steps:
        return Vector((0.0, 0.0, 0.0))
    xs = sorted(p.x for p in steps)
    ys = sorted(p.y for p in steps)
    return Vector((xs[len(xs) // 2], ys[len(ys) // 2], 0.0))


def source_contacts(src_feet, fps, v, tol=0.02, speed=0.3):
    """Contact frames of the SOURCE: per side a list of 'toe' | 'ankle' | None. A frame is a contact
    of the ball (ToeBase head; ankle when the ball is not) when that point is within `tol` of its
    lowest height and slower than `speed` m/s in the source's ground frame (moving at `v` per frame,
    see source_ground_velocity). Only the source decides; the target is never speed-gated."""
    lim = speed / max(fps, 1e-6)
    out = {}
    for s, tr in src_feet.items():
        ank = tr.get("ankle") or []
        n = len(ank)
        res = [None] * n
        lows = {k: _low(tr.get(k) or [], tol) for k in ("toe", "ankle")}
        for i in range(n):
            for key in ("toe", "ankle"):
                seq, low = tr.get(key) or [], lows[key]
                if low is None or len(seq) != n or not low[i]:
                    continue
                a, b = max(0, i - 1), min(n - 1, i + 1)
                if seq[a] is None or seq[b] is None or b == a:
                    continue
                d = Vector((seq[b].x - seq[a].x, seq[b].y - seq[a].y, 0.0)) - v * (b - a)
                if d.length <= lim * (b - a):
                    res[i] = key
                    break
        out[s] = res
    return out


def contact_skate(tracks, contacts, offset=None):
    """XY motion of a foot's contact point on the source contact frames.

    tracks: {side: {"ankle": [Vector], "toe": [Vector|None]}}; contacts from source_contacts;
    offset[i] (Vector) is added to every point (the frame the motion is judged in: the game's
    constant ground motion for an in-place locomotion clip, -v*i for the source's ground frame).
    skate = path length between consecutive contact frames with the same contact point;
    drift = the largest distance from the start of a contact run (a planted foot that wanders).
    Returns {"skate_cm", "drift_cm" (max over sides), "per_side": {side: {...}}}."""
    per = {}
    for s, cs in contacts.items():
        tr = tracks.get(s)
        if tr is None:
            continue

        def pt(key, i):
            seq = tr.get(key) or []
            p = seq[i] if i < len(seq) else None
            if p is None:
                return None
            if offset is not None:
                p = p + offset[i]
            return Vector((p.x, p.y))
        skate = drift = 0.0
        frames = 0
        run = None  # (key, start point, previous point)
        for i, key in enumerate(cs):
            p = pt(key, i) if key else None
            if p is None:
                run = None
                continue
            frames += 1
            if run is not None and run[0] == key:
                skate += (p - run[2]).length
                drift = max(drift, (p - run[1]).length)
                run = (key, run[1], p)
            else:
                run = (key, p, p)
        per[s] = {"skate_cm": round(skate * 100.0, 1), "drift_cm": round(drift * 100.0, 1),
                  "contact_frames": frames}
    return {"skate_cm": max([v["skate_cm"] for v in per.values()] or [0.0]),
            "drift_cm": max([v["drift_cm"] for v in per.values()] or [0.0]),
            "per_side": per}


def evaluate_action(tr, action, n, root_travel=False, hips_travel=False):
    """Play the written action on the real rig and measure it (independent of the internal
    data): NaN, loop seam (first vs last basis), hips world track, ankle world tracks."""
    scene = bpy.context.scene
    rig = tr.rig
    assign_action(rig, action)
    pbs = rig.pose.bones
    hips = pbs[tr.hips]
    feet = {s: pbs[nm[2]] for s, nm in tr.legs.items()}
    toes = {s: pbs.get(tr.cname.get(s + "ToeBase", "")) for s in tr.legs}
    W = rig.matrix_world
    scale = W.to_scale()[0]
    nan = 0
    hips_tr = []
    feet_tr = {s: {"ankle": [], "toe": []} for s in feet}
    first = last = None
    for i in range(n):
        scene.frame_set(i)
        for pb in pbs:
            if not _finite_matrix(pb.matrix):
                nan += 1
        hips_tr.append((W @ hips.matrix).translation.copy())
        for s, pb in feet.items():
            feet_tr[s]["ankle"].append((W @ pb.matrix).translation.copy())
            tb = toes.get(s)
            feet_tr[s]["toe"].append((W @ tb.matrix).translation.copy() if tb else None)
        if i == 0 or i == n - 1:
            snap = {pb.name: (pb.matrix_basis.to_quaternion(), pb.matrix_basis.translation.copy())
                    for pb in pbs}
            if i == 0:
                first = snap
            if i == n - 1:
                last = snap
    seam_rot = seam_pos = 0.0
    if first and last:
        for name, (q0, l0) in first.items():
            q1, l1 = last[name]
            a = math.degrees(q0.rotation_difference(q1).angle)
            seam_rot = max(seam_rot, min(a, 360.0 - a))
            if name == tr.root and root_travel:
                continue
            if name == tr.hips and hips_travel:
                continue
            seam_pos = max(seam_pos, (l1 - l0).length * scale)
    h0 = hips_tr[0]
    drift = max(math.hypot(p.x - h0.x, p.y - h0.y) for p in hips_tr) if hips_tr else 0.0
    return {"nan": nan, "seam_rot_deg": round(seam_rot, 4), "seam_pos_mm": round(seam_pos * 1000, 3),
            "hips_track": hips_tr, "feet_tracks": feet_tr, "hips_drift_m": round(drift, 4)}


# =========================================================================== GLB reader

def read_glb(path):
    """(json, bin) of a .glb file."""
    with open(path, "rb") as fh:
        data = fh.read()
    magic, _ver, _length = struct.unpack_from("<4sII", data, 0)
    if magic != b"glTF":
        raise ValueError("not a GLB: %s" % path)
    off = 12
    js, binc = None, b""
    while off < len(data):
        clen, ctype = struct.unpack_from("<I4s", data, off)
        chunk = data[off + 8: off + 8 + clen]
        if ctype == b"JSON":
            js = json.loads(chunk.decode("utf-8"))
        elif ctype == b"BIN\x00":
            binc = chunk
        off += 8 + clen
    return js, binc


def _accessor_floats(js, binc, idx):
    import numpy as np
    acc = js["accessors"][idx]
    if acc.get("componentType") != 5126 or "bufferView" not in acc:
        return None
    bv = js["bufferViews"][acc["bufferView"]]
    ncomp = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT4": 16}[acc["type"]]
    start = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
    stride = bv.get("byteStride", 0) or 4 * ncomp
    count = acc["count"]
    if stride == 4 * ncomp:
        return np.frombuffer(binc, dtype="<f4", count=count * ncomp, offset=start)
    rows = [np.frombuffer(binc, dtype="<f4", count=ncomp, offset=start + i * stride)
            for i in range(count)]
    return np.concatenate(rows) if rows else np.zeros(0)


def glb_summary(path):
    """Animations (name, duration, channels, NaN count), skins and skinned primitives."""
    import numpy as np
    js, binc = read_glb(path)
    anims = []
    for a in js.get("animations", []):
        dur, nan = 0.0, 0
        for s in a.get("samplers", []):
            acc = js["accessors"][s["input"]]
            if "max" in acc:
                dur = max(dur, float(acc["max"][0]))
            out = _accessor_floats(js, binc, s["output"])
            if out is not None:
                nan += int(np.count_nonzero(~np.isfinite(out)))
        anims.append({"name": a.get("name"), "duration": round(dur, 4),
                      "channels": len(a.get("channels", [])), "nan": nan})
    skins = [{"name": s.get("name"), "joints": len(s.get("joints", []))}
             for s in js.get("skins", [])]
    skinned = 0
    for m in js.get("meshes", []):
        for p in m.get("primitives", []):
            if "JOINTS_0" in p.get("attributes", {}) and "WEIGHTS_0" in p.get("attributes", {}):
                skinned += 1
    joint_names = []
    for s in js.get("skins", []):
        joint_names += [js["nodes"][j].get("name") for j in s.get("joints", [])]
    nodes = js.get("nodes", [])
    joints = {j for s in js.get("skins", []) for j in s.get("joints", [])}
    sockets = sorted({(n.get("extras") or {}).get("game_socket") for n in nodes
                      if isinstance((n.get("extras") or {}).get("game_socket"), str)})
    others = [n.get("name") for i, n in enumerate(nodes) if i not in joints]
    scaled = [{"name": n.get("name"), "scale": n.get("scale"), "rotation": n.get("rotation")}
              for i, n in enumerate(nodes) if i not in joints and (
                  any(abs(x - 1.0) > 1e-4 for x in n.get("scale", [1, 1, 1])) or
                  any(abs(x - y) > 1e-4 for x, y in zip(n.get("rotation", [0, 0, 0, 1]),
                                                        [0, 0, 0, 1])))]
    return {"animations": anims, "skins": skins, "skinned_primitives": skinned,
            "joint_names": joint_names, "sockets": sockets, "non_joint_nodes": others,
            "transformed_nodes": scaled}


# =========================================================================== categories / tags

CATEGORY_RULES = [
    ("death", ("death", "dying", "die", "dead", "knockout")),
    ("hit", ("hit", "impact", "damage", "hurt", "react", "flinch", "stagger")),
    ("cast", ("cast", "spell", "magic", "conjur", "summon", "channel")),
    ("attack", ("attack", "atk", "slash", "stab", "thrust", "punch", "kick", "strike", "swing",
                "shoot", "shot", "combo", "bite", "smash", "throw", "lunge")),
    ("jump", ("jump", "leap")),
    ("run", ("run", "sprint", "jog", "dash")),
    ("walk", ("walk", "strafe", "step", "crouch_walk", "sneak")),
    ("idle", ("idle", "breath", "stand", "sit", "rest", "wait", "block")),
    ("emote", ("emote", "wave", "danc", "cheer", "taunt", "salute", "bowing", "clap", "laugh",
               "victory", "talk")),
]
TAG_RULES = {
    "sword": ("sword", "sns", "slash", "blade", "swordplay", "katana"),
    "shield": ("shield", "sns", "block"),
    "bow": ("bow", "shoot", "arrow", "archer", "draw_arrow", "aim"),
    "spear": ("spear", "bayonet", "thrust", "pike", "lance", "polearm"),
    "magic": ("cast", "spell", "magic", "necro", "acolyte", "mage", "wizard"),
    "zombie": ("zombie", "undead", "ghoul"),
    "mounted": ("rider", "mounted", "riding", "horse"),
    "torch": ("torch",),
    "unarmed": ("punch", "kick", "boxing", "unarmed"),
}


def _tokens(text):
    return "_" + re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_") + "_"


def guess_category(text):
    t = _tokens(text)
    for cat, words in CATEGORY_RULES:
        for w in words:
            if ("_" + w) in t or (w in t and len(w) > 4):
                return cat
    return "other"


def guess_tags(text):
    t = _tokens(text)
    tags = []
    for tag, words in TAG_RULES.items():
        if any(("_" + w.strip("_")) in t for w in words):
            tags.append(tag)
    return tags


def slug(text):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "clip"


def clip_name_from_path(path):
    """'<unit> <stem>' when the file sits under .../<unit>/anims/..., else the stem."""
    parts = os.path.normpath(path).replace("\\", "/").split("/")
    stem = os.path.splitext(parts[-1])[0]
    unit = None
    if "anims" in parts:
        i = len(parts) - 1 - parts[::-1].index("anims")
        if i > 0:
            unit = parts[i - 1]
    words = re.sub(r"[_\-]+", " ", stem).strip()
    return ("%s %s" % (unit.replace("_", " "), words)) if unit else words


def default_loop(category):
    return category in ("idle", "walk", "run")


# categories the game moves through the world: root_motion "lock" plays them in place and the
# game moves the character at ground_speed_mps. Everything else keeps its hips motion under "lock"
# (feet planted in the source stay planted).
LOCOMOTION = ("walk", "run")


def is_locomotion(category, entry=None, clip=None):
    for d in (clip or {}, entry or {}):
        if "locomotion" in d:
            return bool(d["locomotion"])
    if "locomotion" in ((entry or {}).get("tags") or []):
        return True
    return category in LOCOMOTION


# =========================================================================== library index

LICENCE = {
    "source": "Adobe Mixamo (mixamo.com)",
    "note": ("Mixamo characters and animations may be used royalty-free in your own personal, "
             "commercial and non-profit projects (games, films). The raw FBX files must not be "
             "redistributed, sold or shared as standalone assets - keep this library local "
             "(data/library is git-ignored) and ship only the baked game exports."),
    "downloads": "only the owner downloads from mixamo.com; the forge never goes online",
}


def index_path(library):
    return os.path.join(library, "index.json")


def load_index(library):
    p = index_path(library)
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8-sig") as fh:
            idx = json.load(fh)
    else:
        idx = {}
    idx.setdefault("version", 1)
    idx["licence"] = LICENCE
    idx.setdefault("clips", [])
    return idx


def save_index(library, idx):
    os.makedirs(library, exist_ok=True)
    idx["updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    idx["licence"] = LICENCE
    p = index_path(library)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(idx, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, p)
    return p


def library_entry(library, clip_id):
    idx = load_index(library)
    for e in idx["clips"]:
        if e["id"] == clip_id:
            return e
    return None


def resolve_clip_source(source, library):
    """Absolute .fbx path or 'library:<id>' / '<id>'. Returns (path, entry or None)."""
    s = str(source)
    if s.lower().endswith(".fbx") and (os.path.isabs(s) or os.path.exists(s)):
        if not os.path.exists(s):
            raise FileNotFoundError("clip file not found: %s" % s)
        return os.path.abspath(s), None
    cid = s.split(":", 1)[1] if s.startswith("library:") else s
    e = library_entry(library, cid)
    if e is None:
        raise KeyError("clip '%s' is not in the library %s" % (cid, library))
    path = os.path.join(library, e["file"])
    if not os.path.exists(path):
        raise FileNotFoundError("library file missing for %s: %s" % (cid, path))
    return path, e


def sha1_file(path):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def joint_world(arm, amap, frame_times):
    """World positions of KEY_JOINTS at the given frame times: {joint: [Vector...]}."""
    scene = bpy.context.scene
    names = {c: n for n, c in amap.items()}
    W = arm.matrix_world
    out = {j: [] for j in KEY_JOINTS}
    for t in frame_times:
        f = int(math.floor(t))
        scene.frame_set(f, subframe=float(t - f))
        for j in KEY_JOINTS:
            if j.endswith("Tip") or j == "HeadTop":
                base = j[:-3] if j.endswith("Tip") else "Head"
                nm = names.get(base)
                if nm is None:
                    out[j].append(None)
                    continue
                pb = arm.pose.bones[nm]
                out[j].append((W @ pb.tail))
            else:
                nm = names.get(j)
                out[j].append((W @ arm.pose.bones[nm].head) if nm else None)
    return out


def fbx_metadata(path, samples=12):
    """Import one clip FBX into an empty scene and describe it (no files written)."""
    af.reset_scene()
    src, act, fps, has_skin = import_fbx_clip(path)
    scheme, amap = map_armature(src)
    f0, f1 = int(math.floor(act.frame_range[0])), int(math.ceil(act.frame_range[1]))
    frames = f1 - f0 + 1
    names = {c: n for n, c in amap.items()}
    W = src.matrix_world
    scene = bpy.context.scene
    travel = 0.0
    if "Hips" in names:
        hb = src.pose.bones[names["Hips"]]
        scene.frame_set(f0)
        p0 = (W @ hb.matrix).translation.copy()
        for f in range(f0, f1 + 1):
            scene.frame_set(f)
            p = (W @ hb.matrix).translation
            travel = max(travel, math.hypot(p.x - p0.x, p.y - p0.y))
    name = clip_name_from_path(path)
    category = guess_category(name)
    times = sample_times(f0, f1, samples, default_loop(category))
    tracks = joint_world(src, amap, times)
    # rest pose: arm elevation tells T-pose (0 deg) from A-pose (~-45 deg)
    arm_elev = None
    if "LeftArm" in names and "LeftForeArm" in names:
        d = (W @ src.data.bones[names["LeftForeArm"]].head_local -
             W @ src.data.bones[names["LeftArm"]].head_local).normalized()
        arm_elev = round(math.degrees(math.asin(max(-1.0, min(1.0, d.z)))), 1)
    rest_h = None
    if "Head" in names:
        rest_h = round((W @ src.data.bones[names["Head"]].tail_local).z, 3)
    meta = {"path": os.path.abspath(path), "name": name, "category": category,
            "tags": guess_tags(name), "skeleton": scheme, "bones": len(src.data.bones),
            "mapped": len(amap), "frames": frames, "frame_range": [f0, f1], "fps": fps,
            "duration": round((frames - 1) / fps, 4) if fps else None, "has_skin": has_skin,
            "hips_travel_m": round(travel, 3), "root_motion": travel > 0.2,
            "rest_arm_elevation_deg": arm_elev, "rest_height_m": rest_h,
            "size": os.path.getsize(path), "sha1": sha1_file(path)}
    pose = {"times": times, "tracks": tracks}
    return meta, pose


def poses_json(meta, pose, clip_id):
    """Stick-figure data for UI previews; coordinates glTF-style (Y up, character faces +Z),
    metres, XZ relative to the first-sample hips."""
    tracks = pose["tracks"]
    h0 = tracks["Hips"][0]
    joints = [j for j in KEY_JOINTS if any(p is not None for p in tracks[j])]
    samples = []
    fps = meta["fps"] or 30.0
    f0 = meta["frame_range"][0]
    for i, t in enumerate(pose["times"]):
        pts = []
        for j in joints:
            p = tracks[j][i]
            if p is None:
                pts.append(None)
            else:
                pts.append([round(p.x - h0.x, 4), round(p.z, 4), round(-(p.y - h0.y), 4)])
        samples.append({"frame": round(t, 3), "t": round((t - f0) / fps, 4), "p": pts})
    return {"id": clip_id, "fps": fps, "frames": meta["frames"], "axes": "gltf_y_up_face_+z",
            "units": "m", "joints": joints,
            "sticks": [[a, b] for a, b in STICKS if a in joints and b in joints],
            "samples": samples}


def poses_match(pa, pb, tol=0.003):
    """Same motion (sampled world joints) within tol metres."""
    if pa["frames"] != pb["frames"] or pa["joints"] != pb["joints"] or \
            len(pa["samples"]) != len(pb["samples"]):
        return False
    worst = 0.0
    for sa, sb in zip(pa["samples"], pb["samples"]):
        for a, b in zip(sa["p"], sb["p"]):
            if (a is None) != (b is None):
                return False
            if a is None:
                continue
            worst = max(worst, max(abs(x - y) for x, y in zip(a, b)))
            if worst > tol:
                return False
    return True


# =========================================================================== preview grid

def _load_rgba(path):
    import numpy as np
    img = bpy.data.images.load(path, check_existing=False)
    px = np.array(img.pixels[:], dtype=np.float32).reshape(img.size[1], img.size[0], 4)
    bpy.data.images.remove(img)
    return px


def _save_rgba(path, arr):
    out = bpy.data.images.new("af_grid", width=arr.shape[1], height=arr.shape[0], alpha=True)
    out.pixels = arr.ravel()
    out.filepath_raw = os.path.abspath(path)
    out.file_format = "PNG"
    out.save()
    bpy.data.images.remove(out)
    return os.path.abspath(path)


def _evaluated_bbox(meshes):
    import numpy as np
    dg = bpy.context.evaluated_depsgraph_get()
    lo = np.array([1e9, 1e9, 1e9])
    hi = -lo
    for o in meshes:
        ev = o.evaluated_get(dg)
        me = ev.to_mesh()
        try:
            n = len(me.vertices)
            if n:
                co = np.empty(n * 3, dtype=np.float64)
                me.vertices.foreach_get("co", co)
                co = co.reshape(n, 3)
                M = np.array(ev.matrix_world)
                w = co @ M[:3, :3].T + M[:3, 3]
                lo = np.minimum(lo, w.min(0))
                hi = np.maximum(hi, w.max(0))
        finally:
            ev.to_mesh_clear()
    return Vector(lo.tolist()), Vector(hi.tolist())


def _box_object(name, lo, hi, material=None, hide_render=True):
    me = bpy.data.meshes.new(name)
    v = [(x, y, z) for x in (lo.x, hi.x) for y in (lo.y, hi.y) for z in (lo.z, hi.z)]
    faces = [(0, 1, 3, 2), (4, 6, 7, 5), (0, 4, 5, 1), (2, 3, 7, 6), (0, 2, 6, 4), (1, 5, 7, 3)]
    me.from_pydata(v, [], faces)
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    ob.hide_render = hide_render
    if material is not None:
        me.materials.append(material)
    return ob


def render_clip_grid(path, tr, meshes, clips, cols=5, tile=256):
    """One row per clip (top to bottom in `clips` order), `cols` sampled frames per row,
    three-quarter view (af_common.render_views, Workbench), fixed framing per row, a ground
    plate at z=0 and an origin cross so foot contact and drift are visible."""
    import numpy as np
    tmp = os.path.join(os.path.dirname(os.path.abspath(path)), "_preview_tmp")
    os.makedirs(tmp, exist_ok=True)
    mat = bpy.data.materials.new("af_ground")
    mat.diffuse_color = (0.30, 0.32, 0.36, 1.0)
    mat_x = bpy.data.materials.new("af_origin")
    mat_x.diffuse_color = (0.85, 0.25, 0.15, 1.0)
    rows, helpers = [], []
    try:
        for act, loop, n in clips:
            assign_action(tr.rig, act)
            frames = sorted(set(int(round(t)) for t in sample_times(0, n - 1, cols, loop)))
            while len(frames) < cols:
                frames.append(frames[-1])
            lo = Vector((1e9, 1e9, 1e9))
            hi = -lo
            for f in frames:
                bpy.context.scene.frame_set(f)
                a, b = _evaluated_bbox(meshes)
                lo = Vector(map(min, lo, a))
                hi = Vector(map(max, hi, b))
            pad = max((hi - lo).length * 0.04, 0.05)
            g_lo = Vector((lo.x - pad * 3, lo.y - pad * 3, -0.012))
            g_hi = Vector((hi.x + pad * 3, hi.y + pad * 3, -0.002))
            ground = _box_object("af_ground_plate", g_lo, g_hi, mat, hide_render=False)
            w = 0.006 * max(1.0, tr.height)
            cross = [_box_object("af_origin_x", Vector((-0.12, -w, -0.002)), Vector((0.12, w, 0.004)),
                                 mat_x, hide_render=False),
                     _box_object("af_origin_y", Vector((-w, -0.12, -0.002)), Vector((w, 0.12, 0.004)),
                                 mat_x, hide_render=False)]
            proxy = _box_object("af_frame_proxy", Vector((lo.x, lo.y, min(lo.z, 0.0))), hi)
            helpers = [ground, proxy] + cross
            tiles = []
            for j, f in enumerate(frames):
                p = os.path.join(tmp, "r%d_%d.png" % (len(rows), j))
                af.render_views(p, [proxy], views=("three_quarter",), size=tile, frame=f)
                tiles.append(_load_rgba(p))
            rows.append(np.concatenate(tiles, axis=1))
            for h in helpers:
                me = h.data
                bpy.data.objects.remove(h, do_unlink=True)
                bpy.data.meshes.remove(me)
            helpers = []
        bg = np.array([0.80, 0.81, 0.83, 1.0], dtype=np.float32)
        out_rows = []
        for r in rows:
            a = r[..., 3:4]
            comp = r[..., :3] * a + bg[:3] * (1.0 - a)
            comp = np.concatenate([comp, np.ones_like(a)], axis=2)
            comp[:2, :, :3] = 0.35  # separator at the bottom edge of each row
            for c in range(1, cols):
                comp[:, c * tile: c * tile + 1, :3] = 0.65
            out_rows.append(comp)
        grid = np.concatenate(out_rows[::-1], axis=0)  # Blender rows are bottom-up
        _save_rgba(path, grid.astype(np.float32))
    finally:
        for h in helpers:
            try:
                bpy.data.objects.remove(h, do_unlink=True)
            except Exception:
                pass
        bpy.data.materials.remove(mat)
        bpy.data.materials.remove(mat_x)
        shutil.rmtree(tmp, ignore_errors=True)
    return os.path.abspath(path)


def gather_fbx(folder, pattern="**/*.fbx"):
    pat = os.path.join(folder, pattern)
    return sorted(os.path.abspath(p) for p in glob.glob(pat, recursive=True)
                  if p.lower().endswith(".fbx"))
