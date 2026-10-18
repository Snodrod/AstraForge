"""Self-checks of the "mixamo" module: steps/retarget.py, steps/clip_library.py, steps/_anim_lib.py.

Synthetic tests build their own source clip (Mixamo-named T-pose armature exported to FBX) and
targets in tempfile.mkdtemp(); test_real_* use the clip library on disk and
return early when they are missing.

Run (until the shared runner exists):
  blender -b --factory-startup --python-expr "import sys;sys.path[:0]=[r'<root>/scripts/blender',
    r'<root>/scripts/blender/tests'];import test_mixamo as t;import af_common as af
  for n in sorted(dir(t)):
      if n.startswith('test_'): af.reset_scene(); getattr(t,n)(); print('PASS',n)"
"""
import json
import math
import os
import re
import shutil
import tempfile

import bpy
from mathutils import Matrix, Quaternion, Vector

import af_common as af
from steps import _anim_lib as lib
from steps import clip_library
from steps import retarget

LIBRARY = lib.DEFAULT_LIBRARY


def tmpdir(tag):
    return tempfile.mkdtemp(prefix="af_mx_%s_" % tag)


def request(out_dir, inputs, params):
    return {"step": "retarget", "inputs": inputs, "params": params, "out_dir": out_dir}


def finish(req, res):
    """Same normalisation af_run applies (writes result.json, decides ok)."""
    return af.finish(req, res)


# =========================================================================== builders

MIXAMO_TPOSE = {
    # name: (head, tail, parent)
    "Hips": ((0, 0, 0.86), (0, 0, 0.96), None),
    "Spine": ((0, 0, 0.96), (0, 0, 1.08), "Hips"),
    "Spine1": ((0, 0, 1.08), (0, 0, 1.20), "Spine"),
    "Spine2": ((0, 0, 1.20), (0, 0, 1.40), "Spine1"),
    "Neck": ((0, 0, 1.40), (0, 0, 1.47), "Spine2"),
    "Head": ((0, 0, 1.47), (0, 0, 1.65), "Neck"),
}
for _s, _x in (("Left", 1), ("Right", -1)):
    MIXAMO_TPOSE.update({
        _s + "Shoulder": ((0.06 * _x, 0, 1.33), (0.17 * _x, 0, 1.33), "Spine2"),
        _s + "Arm": ((0.17 * _x, 0, 1.33), (0.43 * _x, 0, 1.33), _s + "Shoulder"),
        _s + "ForeArm": ((0.43 * _x, 0, 1.33), (0.68 * _x, 0, 1.33), _s + "Arm"),
        _s + "Hand": ((0.68 * _x, 0, 1.33), (0.78 * _x, 0.03, 1.33), _s + "ForeArm"),
        _s + "UpLeg": ((0.09 * _x, 0, 0.84), (0.09 * _x, 0, 0.47), "Hips"),
        _s + "Leg": ((0.09 * _x, 0, 0.47), (0.09 * _x, 0.0, 0.08), _s + "UpLeg"),
        _s + "Foot": ((0.09 * _x, 0.0, 0.08), (0.09 * _x, -0.12, 0.02), _s + "Leg"),
        _s + "ToeBase": ((0.09 * _x, -0.12, 0.02), (0.09 * _x, -0.20, 0.02), _s + "Foot"),
    })

# different proportions, arms hanging down, deliberately odd bone rolls
ARMS_DOWN = {
    "Hips": ((0, 0.01, 1.00), (0, 0.01, 1.08), None),
    "Spine": ((0, 0.01, 1.08), (0, 0.01, 1.18), "Hips"),
    "Spine1": ((0, 0.01, 1.18), (0, 0.01, 1.28), "Spine"),
    "Spine2": ((0, 0.01, 1.28), (0, 0.01, 1.48), "Spine1"),
    "Neck": ((0, 0.01, 1.48), (0, 0.0, 1.55), "Spine2"),
    "Head": ((0, 0.0, 1.55), (0, 0.0, 1.75), "Neck"),
}
for _s, _x in (("Left", 1), ("Right", -1)):
    ARMS_DOWN.update({
        _s + "Shoulder": ((0.05 * _x, 0.0, 1.45), (0.19 * _x, 0.0, 1.45), "Spine2"),
        _s + "Arm": ((0.19 * _x, 0.0, 1.45), (0.21 * _x, 0.0, 1.17), _s + "Shoulder"),
        _s + "ForeArm": ((0.21 * _x, 0.0, 1.17), (0.22 * _x, -0.01, 0.93), _s + "Arm"),
        _s + "Hand": ((0.22 * _x, -0.01, 0.93), (0.22 * _x, -0.01, 0.85), _s + "ForeArm"),
        _s + "UpLeg": ((0.11 * _x, 0.01, 0.98), (0.11 * _x, 0.0, 0.53), "Hips"),
        _s + "Leg": ((0.11 * _x, 0.0, 0.53), (0.11 * _x, 0.02, 0.09), _s + "UpLeg"),
        _s + "Foot": ((0.11 * _x, 0.02, 0.09), (0.11 * _x, -0.12, 0.02), _s + "Leg"),
        _s + "ToeBase": ((0.11 * _x, -0.12, 0.02), (0.11 * _x, -0.21, 0.02), _s + "Foot"),
    })


def astraforge_template(h=1.8, w=None):
    """Bone layout of src/rig-core.js createSkeleton(type='humanoid') in Blender axes
    (three.js x, y-up, z-forward -> Blender x, -z, y); tails go to the first child.
    w = model width: createSkeleton uses max(bbox width, 0.35 h); a T-pose model is ~h wide
    (default), an arms-down one gets the 0.35 h minimum and stubby horizontal arms."""
    w, d = (h if w is None else w), h * 0.25
    pts = {"Root": (0, 0, 0), "Hips": (0, 0, h * .5), "Spine": (0, 0, h * .63),
           "Chest": (0, 0, h * .73), "Neck": (0, 0, h * .84), "Head": (0, 0, h * .92),
           "HeadEnd": (0, 0, h)}
    par = {"Root": None, "Hips": "Root", "Spine": "Hips", "Chest": "Spine", "Neck": "Chest",
           "Head": "Neck", "HeadEnd": "Head"}
    for s, x in (("Left", 1), ("Right", -1)):
        pts.update({s + "Shoulder": (x * w * .16, 0, h * .76), s + "UpperArm": (x * w * .27, 0, h * .76),
                    s + "LowerArm": (x * w * .38, 0, h * .76), s + "Hand": (x * w * .47, 0, h * .76),
                    s + "HandEnd": (x * w * .5, 0, h * .76),
                    s + "UpperLeg": (x * w * .09, 0, h * .47), s + "LowerLeg": (x * w * .09, 0, h * .26),
                    s + "Foot": (x * w * .09, 0, h * .055), s + "Toes": (x * w * .09, -d * .4, h * .035),
                    s + "ToesEnd": (x * w * .09, -d * .5, h * .035)})
        par.update({s + "Shoulder": "Chest", s + "UpperArm": s + "Shoulder",
                    s + "LowerArm": s + "UpperArm", s + "Hand": s + "LowerArm",
                    s + "HandEnd": s + "Hand", s + "UpperLeg": "Hips",
                    s + "LowerLeg": s + "UpperLeg", s + "Foot": s + "LowerLeg",
                    s + "Toes": s + "Foot", s + "ToesEnd": s + "Toes"})
    child = {}
    for n, p in par.items():
        if p and p not in child and not n.endswith(("Shoulder", "UpperLeg")):
            child[p] = n
    child["Chest"] = "Neck"
    child["Hips"] = "Spine"
    layout = {}
    for n, p in pts.items():
        head = Vector(p)
        if n in child:
            tail = Vector(pts[child[n]])
        else:  # leaf: continue the parent direction
            pp = Vector(pts[par[n]]) if par[n] else head - Vector((0, 0, 0.1))
            dirv = (head - pp).normalized() if (head - pp).length > 1e-6 else Vector((0, 0, 1))
            tail = head + dirv * 0.05
        layout[n] = (tuple(head), tuple(tail), par[n])
    return layout


def build_armature(name, layout, prefix="", roll=0.0, deform_skip=()):
    arm_data = bpy.data.armatures.new(name)
    rig = bpy.data.objects.new(name, arm_data)
    bpy.context.scene.collection.objects.link(rig)
    af.select_only([rig])
    bpy.ops.object.mode_set(mode="EDIT")
    eb = arm_data.edit_bones
    for n, (h, t, p) in layout.items():
        b = eb.new(prefix + n)
        b.head, b.tail = Vector(h), Vector(t)
        b.roll = roll
        b.use_deform = n not in deform_skip
    for n, (h, t, p) in layout.items():
        if p:
            eb[prefix + n].parent = eb[prefix + p]
    bpy.ops.object.mode_set(mode="OBJECT")
    return rig


UE_NAMES = {"Hips": "pelvis", "Spine": "spine_01", "Spine1": "spine_02", "Spine2": "spine_03",
            "Neck": "neck_01", "Head": "head"}
for _s, _x in (("Left", "l"), ("Right", "r")):
    UE_NAMES.update({_s + "Shoulder": "clavicle_" + _x, _s + "Arm": "upperarm_" + _x,
                     _s + "ForeArm": "lowerarm_" + _x, _s + "Hand": "hand_" + _x,
                     _s + "UpLeg": "thigh_" + _x, _s + "Leg": "calf_" + _x,
                     _s + "Foot": "foot_" + _x, _s + "ToeBase": "ball_" + _x})


def rename_layout(layout, table):
    return {table[n]: (h, t, table[p] if p else None) for n, (h, t, p) in layout.items()}


def animate_source(rig, prefix="mixamorig:", frames=31, travel=0.8, amp=1.0, extra=None,
                   names=None):
    """Periodic test motion (frame 1 == frame `frames` except the Hips travel along -Y).
    names: canonical -> bone name (default prefix + canonical)."""
    scene = bpy.context.scene
    scene.render.fps = 30
    scene.frame_start, scene.frame_end = 1, frames
    pb = rig.pose.bones
    for p in pb:
        p.rotation_mode = "QUATERNION"
    if names is not None:
        prefix = ""
        pb = {c: rig.pose.bones[n] for c, n in names.items()}
        bones = {c: rig.data.bones[n] for c, n in names.items()}
    else:
        bones = rig.data.bones

    def local_q(name, axis, deg):
        R = bones[prefix + name].matrix_local.to_3x3()
        return Quaternion(R.inverted() @ Vector(axis), math.radians(deg * amp))

    period = frames - 1
    # frame 0 = exact rest: an armature-only FBX export writes the pose of the current frame as
    # the bone rest, so the export runs with frame 0 current (the importer shifts frames by +1)
    for p in rig.pose.bones:
        p.location = (0, 0, 0)
        p.rotation_quaternion = Quaternion()
        p.keyframe_insert("location", frame=0)
        p.keyframe_insert("rotation_quaternion", frame=0)
    for f in range(1, frames + 1):
        ph = 2 * math.pi * (f - 1) / period
        R = bones[prefix + "Hips"].matrix_local.to_3x3()
        hips = pb[prefix + "Hips"]
        hips.location = R.inverted() @ Vector((0.0, -travel * (f - 1) / period,
                                               0.02 * math.cos(2 * ph)))
        hips.keyframe_insert("location", frame=f)
        rots = {
            "Hips": ((0, 0, 1), 8 * math.sin(ph)),
            "Spine1": ((0, 0, 1), 10 * math.sin(ph)),
            "Head": ((1, 0, 0), 10 * math.sin(2 * ph)),
            "LeftArm": ((0, 1, 0), 50 + 20 * math.sin(ph)),
            "RightArm": ((0, 1, 0), -(35 + 25 * math.sin(ph + 1.0))),
            "LeftForeArm": ((0, 0, 1), 30 * (1 + math.sin(ph)) / 2),
            "RightForeArm": ((0, 0, 1), -45 * (1 + math.cos(ph)) / 2),
            "LeftUpLeg": ((1, 0, 0), -25 * math.sin(ph)),
            "RightUpLeg": ((1, 0, 0), 25 * math.sin(ph)),
            "LeftLeg": ((1, 0, 0), 20 * (1 + math.cos(ph)) / 2),
            "RightLeg": ((1, 0, 0), 20 * (1 - math.cos(ph)) / 2),
        }
        for n, (axis, deg) in (extra or {}).items():
            rots[n] = (axis, deg * math.sin(ph) if deg else 0.0)
        for n, (axis, deg) in rots.items():
            b = pb[prefix + n]
            b.rotation_quaternion = local_q(n, axis, deg)
            b.keyframe_insert("rotation_quaternion", frame=f)


def synthetic_clip_fbx(folder, name="synthetic_walk.fbx", amp=1.0, travel=0.8, layout=None,
                       extra=None, names=None):
    """Export a Mixamo-named (or `names`-renamed, e.g. UE_NAMES) T-pose armature with test motion
    to FBX, return the path."""
    keep = set(bpy.data.objects)
    if names:
        rig = build_armature("SrcRig", rename_layout(layout or MIXAMO_TPOSE, names))
    else:
        rig = build_armature("SrcRig", layout or MIXAMO_TPOSE, prefix="mixamorig:")
    animate_source(rig, amp=amp, travel=travel, extra=extra, names=names)
    return _export_clip(rig, folder, name, keep)


def _export_clip(rig, folder, name, keep):
    path = os.path.join(folder, name)
    bpy.context.scene.frame_start = 1
    bpy.context.scene.frame_set(0)  # rest written from frame 0, clip baked from 1
    af.select_only([rig])
    bpy.ops.export_scene.fbx(filepath=path, use_selection=True, object_types={"ARMATURE"},
                             add_leaf_bones=False, bake_anim=True, bake_anim_use_all_actions=False,
                             bake_anim_use_nla_strips=False, bake_anim_force_startend_keying=True,
                             bake_anim_simplify_factor=0.0)
    for o in [o for o in bpy.data.objects if o not in keep]:
        bpy.data.objects.remove(o, do_unlink=True)
    for a in list(bpy.data.actions):
        bpy.data.actions.remove(a)
    bpy.context.view_layer.update()
    return path


def _aim(pb, direction):
    """Rotate a pose bone (armature at identity) so its Y axis points along `direction`."""
    M = pb.matrix.copy()
    cur = (M.to_3x3() @ Vector((0, 1, 0))).normalized()
    R = cur.rotation_difference(direction.normalized()).to_matrix() @ M.to_3x3()
    N = R.to_4x4()
    N.translation = M.translation
    pb.matrix = N
    bpy.context.view_layer.update()


def planted_clip_fbx(folder, name="planted_idle.fbx", frames=31, sway=(0.06, 0.03), drop=0.04):
    """Standing clip whose feet never move (like a typical hit clip): the hips sway sideways and
    forward/back and bob, the spine and arms move, and both legs are solved analytically so the
    ankles stay exactly at their rest positions and the feet keep their rest orientation."""
    keep = set(bpy.data.objects)
    P = "mixamorig:"
    rig = build_armature("SrcRig", MIXAMO_TPOSE, prefix=P)
    scene = bpy.context.scene
    scene.render.fps = 30
    scene.frame_start, scene.frame_end = 1, frames
    pbs, bones = rig.pose.bones, rig.data.bones
    for p in pbs:
        p.rotation_mode = "QUATERNION"
        p.location = (0, 0, 0)
        p.rotation_quaternion = Quaternion()
        p.keyframe_insert("location", frame=0)
        p.keyframe_insert("rotation_quaternion", frame=0)

    def local_q(name, axis, deg):
        R = bones[P + name].matrix_local.to_3x3()
        return Quaternion(R.inverted() @ Vector(axis), math.radians(deg))
    for f in range(1, frames + 1):
        ph = 2 * math.pi * (f - 1) / (frames - 1)
        for p in pbs:
            p.location = (0, 0, 0)
            p.rotation_quaternion = Quaternion()
        R = bones[P + "Hips"].matrix_local.to_3x3()
        pbs[P + "Hips"].location = R.inverted() @ Vector(
            (sway[0] * math.sin(ph), sway[1] * math.sin(2 * ph), -drop + 0.01 * math.cos(2 * ph)))
        pbs[P + "Spine1"].rotation_quaternion = local_q("Spine1", (0, 0, 1), 12 * math.sin(ph))
        pbs[P + "LeftArm"].rotation_quaternion = local_q("LeftArm", (0, 1, 0), 55 + 10 * math.sin(ph))
        pbs[P + "RightArm"].rotation_quaternion = local_q("RightArm", (0, 1, 0), -60)
        bpy.context.view_layer.update()
        for s in ("Left", "Right"):
            up, lo, ft = pbs[P + s + "UpLeg"], pbs[P + s + "Leg"], pbs[P + s + "Foot"]
            l1 = bones[up.name].length
            l2 = bones[lo.name].length
            J = up.head.copy()
            A = bones[ft.name].head_local.copy()
            d = (A - J).length
            u = (A - J).normalized()
            fw = Vector((0, -1, 0))
            pole = (fw - u * fw.dot(u)).normalized()
            a = math.acos(max(-1.0, min(1.0, (l1 * l1 + d * d - l2 * l2) / (2 * l1 * d))))
            K = J + (u * math.cos(a) + pole * math.sin(a)) * l1
            _aim(up, K - J)
            _aim(lo, A - lo.head)
            M = bones[ft.name].matrix_local.copy()
            M.translation = ft.head.copy()
            ft.matrix = M
            bpy.context.view_layer.update()
        for p in pbs:
            p.keyframe_insert("location", frame=f)
            p.keyframe_insert("rotation_quaternion", frame=f)
    return _export_clip(rig, folder, name, keep)


def glb_ankle_excursion(glb, anim, foot_names):
    """Independent end-to-end check: import the exported GLB, play the glTF animation `anim`
    (the importer names the action after it) and return the largest XY distance (m) of each
    ankle from its position on the first frame."""
    before_obj, before_act = set(bpy.data.objects), set(bpy.data.actions)
    bpy.ops.import_scene.gltf(filepath=glb)
    new = [o for o in bpy.data.objects if o not in before_obj]
    arm = next(o for o in new if o.type == "ARMATURE")
    fresh = [a for a in bpy.data.actions if a not in before_act]
    acts = [a for a in fresh if re.match(r"^%s(\.\d{3})?$" % re.escape(anim), a.name)]
    assert acts, (anim, [a.name for a in fresh])
    act = acts[0]
    if arm.animation_data is None:
        arm.animation_data_create()
    arm.animation_data.action = act
    slot = next((s for s in act.slots if arm.name in s.name_display), act.slots[0])
    arm.animation_data.action_slot = slot
    f0, f1 = int(math.floor(act.frame_range[0])), int(math.ceil(act.frame_range[1]))
    start, worst = {}, {n: 0.0 for n in foot_names}
    for f in range(f0, f1 + 1):
        bpy.context.scene.frame_set(f)
        for n in foot_names:
            p = arm.matrix_world @ arm.pose.bones[n].head
            if n not in start:
                start[n] = p.copy()
            worst[n] = max(worst[n], math.hypot(p.x - start[n].x, p.y - start[n].y))
    for o in new:
        bpy.data.objects.remove(o, do_unlink=True)
    for a in [a for a in bpy.data.actions if a not in before_act]:
        bpy.data.actions.remove(a)
    return worst


def skinned_target(layout, prefix="", roll=0.0, name="Target", deform_skip=()):
    rig = build_armature(name, layout, prefix=prefix, roll=roll, deform_skip=deform_skip)
    retarget.make_stick_mesh(rig).name = name + "_body"
    return rig


def save_target_blend(folder, layout, **kw):
    af.reset_scene()
    skinned_target(layout, **kw)
    path = os.path.join(folder, "target.blend")
    af.save_blend(path)
    return path


def world_dirs(rig, amap, frame, pairs):
    bpy.context.scene.frame_set(frame)
    names = {c: n for n, c in amap.items()}
    W = rig.matrix_world
    out = {}
    for a, b in pairs:
        if a in names and b in names:
            pa = (W @ rig.pose.bones[names[a]].matrix).translation
            pb = (W @ rig.pose.bones[names[b]].matrix).translation
            out[a] = (pb - pa).normalized()
    return out


ARM_PAIRS = [("LeftArm", "LeftForeArm"), ("LeftForeArm", "LeftHand"),
             ("RightArm", "RightForeArm"), ("RightForeArm", "RightHand"),
             ("Spine2", "Neck")]


def compare_with_source(rig, tmap, action_name, src_path, f0, n, mirror=False):
    """Independent check: replay the written action and the re-imported source FBX, compare
    world directions of arm segments frame by frame. Returns max angle (deg)."""
    act = bpy.data.actions[action_name]
    lib.assign_action(rig, act)
    before = set(bpy.data.objects)
    src, sact, fps, _ = lib.import_fbx_clip(src_path)
    _, smap = lib.map_armature(src)
    sF = lib.char_frame(lib.rest_heads(src, smap))
    n_lat = Vector(sF.col[0]).normalized()
    worst = 0.0
    for i in range(n):
        td = world_dirs(rig, tmap, i, ARM_PAIRS)
        sd = world_dirs(src, smap, int(f0) + i, ARM_PAIRS)
        for c, v in td.items():
            if mirror:
                m = lib.mirror_name(c)
                if m not in sd:
                    continue
                ref = lib.reflect_vec(sd[m], n_lat)
            else:
                ref = sd.get(c)
            if ref is not None:
                worst = max(worst, math.degrees(v.angle(ref)))
    for o in [o for o in bpy.data.objects if o not in before]:
        bpy.data.objects.remove(o, do_unlink=True)
    bpy.data.actions.remove(sact)
    return worst


def assert_common(res, names, clips_json, skate_max=None, preview=True):
    """Shared assertions. skate_max: {gltf_name: cm} for clips whose foot_skate is known to exceed
    the warning limit max(2, 1.3 x source) - they must still stay under that bound."""
    failed = [c for c in res["checks"] if not c["ok"] and c.get("severity") == "error"]
    assert not failed, failed
    assert res["ok"], res.get("error")
    summ = lib.glb_summary(res["outputs"]["glb"])
    got = [a["name"] for a in summ["animations"]]
    assert sorted(got) == sorted(names), (got, names)
    assert len(got) == len(names)
    assert all(a["nan"] == 0 for a in summ["animations"]), summ["animations"]
    assert summ["skins"] and summ["skinned_primitives"] >= 1, summ
    assert any(c["name"] == "gltf_sockets" and c["ok"] for c in res["checks"])
    with open(clips_json, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    for c in doc["clips"]:
        assert c["nan"] == 0
        assert c["eval_vs_internal_mm"] < 1.0, c
        assert abs(c["gltf_duration"] - c["duration"]) < 0.02, c
        if c["loop"]:
            assert c["seam_rot_deg"] <= 0.5 and c["seam_pos_mm"] <= 1.0, c
            assert c["gltf_name"].endswith("-loop"), c
        else:
            assert not c["gltf_name"].endswith("-loop"), c
        if c["in_place"] == "strict":
            assert c["hips_drift_m"] <= 0.001, c          # hips XY constant
        elif c["in_place"]:
            assert c["hips_end_offset_m"] <= 0.001, c     # no net travel, sway kept
        lim = (skate_max or {}).get(c["gltf_name"], max(2.0, 1.3 * c["foot_skate_src_cm"]))
        assert c["foot_skate_cm"] <= lim + 1e-6, (c["gltf_name"], c["foot_skate_cm"], lim)
        if not c["in_place"]:  # hips motion kept: a planted source foot stays planted
            assert c["foot_plant_drift_cm"] <= 1.3 * c["foot_plant_drift_src_cm"] + 1.0, c
    if preview:
        assert os.path.exists(res["outputs"]["preview"])
        assert os.path.getsize(res["outputs"]["preview"]) > 10000
    return doc, summ


# =========================================================================== unit tests

def test_map_bone_names_schemes():
    mix = ["mixamorig:" + c for c in lib.CORE]
    sch, m = lib.map_bone_names(mix)
    assert sch == "mixamo" and m["mixamorig:LeftForeArm"] == "LeftForeArm"
    sch, m = lib.map_bone_names(["mixamorig1:Hips", "mixamorig1:LeftUpLeg", "mixamorig1:Spine"])
    assert m["mixamorig1:Hips"] == "Hips" and m["mixamorig1:LeftUpLeg"] == "LeftUpLeg"
    sch, m = lib.map_bone_names(["Hips", "Spine", "Spine1", "LeftArm", "LeftHandIndex1"])
    assert sch == "mixamo" and m["LeftHandIndex1"] == "LeftHandIndex1"
    sch, m = lib.map_bone_names(list(astraforge_template()))
    assert sch == "astraforge", sch
    assert m["Chest"] == "Spine2" and m["LeftUpperArm"] == "LeftArm"
    assert m["LeftLowerLeg"] == "LeftLeg" and m["RightToes"] == "RightToeBase"
    assert m["HeadEnd"] == "HeadTop_End" and "Root" not in m and "LeftHandEnd" not in m
    ue = ["root", "pelvis", "spine_01", "spine_02", "spine_03", "neck_01", "head", "clavicle_l",
          "upperarm_l", "lowerarm_l", "hand_l", "thigh_r", "calf_r", "foot_r", "ball_r",
          "index_01_l", "upperarm_twist_01_l"]
    sch, m = lib.map_bone_names(ue)
    assert sch == "ue" and m["pelvis"] == "Hips" and m["spine_03"] == "Spine2"
    assert m["calf_r"] == "RightLeg" and m["index_01_l"] == "LeftHandIndex1"
    assert "root" not in m and "upperarm_twist_01_l" not in m
    sch, m = lib.map_bone_names(ue + ["spine_04", "spine_05"])
    assert m["spine_05"] == "Spine2" and m["spine_03"] == "Spine1" and "spine_02" not in m
    rg = ["root", "DEF-spine", "DEF-spine.001", "DEF-spine.002", "DEF-spine.003", "DEF-spine.004",
          "DEF-spine.005", "DEF-spine.006", "DEF-upper_arm.L", "DEF-upper_arm.L.001",
          "DEF-forearm.L", "DEF-hand.L", "ORG-hand.L", "DEF-thigh.R", "DEF-shin.R", "DEF-foot.R",
          "DEF-toe.R", "DEF-f_index.01.L", "DEF-thumb.02.R", "DEF-shoulder.L"]
    sch, m = lib.map_bone_names(rg)
    assert sch == "rigify", sch
    assert m["DEF-spine"] == "Hips" and m["DEF-spine.006"] == "Head"
    assert m["DEF-hand.L"] == "LeftHand" and "ORG-hand.L" not in m
    assert m["DEF-toe.R"] == "RightToeBase" and m["DEF-thumb.02.R"] == "RightHandThumb2"
    assert "DEF-upper_arm.L.001" not in m and "DEF-spine.005" not in m


def test_retime_action_normalizes():
    rig = build_armature("R", {"A": ((0, 0, 0), (0, 0, 1), None), "B": ((0, 0, 1), (0, 0, 2), "A")})
    keys = {"A": {"loc": [Vector()] * 3, "rot": [Quaternion(), Quaternion((0, 0, 1), 1.0),
                                                 Quaternion((0, 0, 1), 2.0)]},
            "B": {"loc": [Vector()] * 3, "rot": [Quaternion()] * 3}}
    act = lib.write_action(rig, "clip", keys, 3)
    assert len(lib.fcurves_of(act)) == 2 * 10
    new = lib.retime_action(rig, act, 9, lambda i: i / 4.0)
    assert new.name == "clip" and tuple(new.frame_range) == (0.0, 8.0)
    lib.assign_action(rig, new)
    for f in range(9):
        bpy.context.scene.frame_set(f)
        q = rig.pose.bones["A"].rotation_quaternion
        assert abs(q.magnitude - 1.0) < 1e-4
    bpy.context.scene.frame_set(4)
    ang = rig.pose.bones["A"].rotation_quaternion.angle
    assert abs(ang - 1.0) < 1e-3, ang


def test_synthetic_clip_onto_arms_down_rig():
    """(c) without disk assets: T-pose source, arms-down target with other proportions and bone
    rolls; loop+in_place, extract root motion, mirror. Proves the per-bone rest correction."""
    d = tmpdir("synth")
    src = synthetic_clip_fbx(d)
    blend = save_target_blend(d, ARMS_DOWN, prefix="mixamorig:", roll=0.7)
    req = request(os.path.join(d, "out"), {"blend": blend}, {
        "root_motion": "extract",
        "clips": [{"source": src, "name": "walk", "loop": True, "in_place": True},
                  {"source": src, "name": "walk_travel", "loop": True, "in_place": False},
                  {"source": src, "name": "walk_mirror", "mirror": True, "loop": False,
                   "in_place": True}]})
    res = finish(req, retarget.run(req))
    doc, summ = assert_common(res, ["walk-loop", "walk_travel-loop", "walk_mirror"],
                              res["outputs"]["clips"])
    assert "root" in summ["joint_names"], summ["joint_names"]
    by = {c["gltf_name"]: c for c in doc["clips"]}
    # travel 0.8 m in the source, scaled by the hip-height ratio
    k = by["walk-loop"]["k_hip"]
    assert abs(k - (0.98 - 0.02) / (0.84 - 0.02)) < 0.005, k  # hip joints over the toes
    assert abs(by["walk_travel-loop"]["root_travel_m"] - 0.8 * k) < 0.03, by["walk_travel-loop"]
    assert by["walk-loop"]["root_travel_m"] == 0.0
    assert by["walk-loop"]["arm_dir_err_deg"] < 1.0, by["walk-loop"]
    # independent check against the source: arms point where the source arms point
    rig = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    _, tmap = lib.map_armature(rig)
    n = by["walk-loop"]["frames"]
    f0 = by["walk-loop"]["trim"][0]
    err = compare_with_source(rig, tmap, "walk-loop", src, f0, n - 6)  # tail = seam blend
    assert err < 3.0, err
    err_m = compare_with_source(rig, tmap, "walk_mirror", src, f0, n)
    assert err_m > 10.0, err_m  # mirrored differs from the source...
    err_r = compare_with_source(rig, tmap, "walk_mirror", src, f0, n, mirror=True)
    assert err_r < 3.0, err_r   # ...and equals the reflected source
    # naive name-to-name copy of local rotations would leave the arms ~90 deg off
    assert by["walk-loop"]["seam_rot_deg"] < 0.01
    shutil.rmtree(d, ignore_errors=True)


def test_degenerate_source_neck():
    """Source Neck of zero length (head == Head head, like some Mixamo rigs) with a big neck
    rotation: the target's real neck must not be bent by it, the head keeps the source's world
    rotation (found on one rig: the head was folded into the chest)."""
    d = tmpdir("degen")
    layout = dict(MIXAMO_TPOSE)
    layout["Spine2"] = ((0, 0, 1.20), (0, 0, 1.47), "Spine1")
    layout["Neck"] = ((0, 0, 1.47), (0, 0, 1.50), "Spine2")
    src = synthetic_clip_fbx(d, layout=layout, extra={"Neck": ((1, 0, 0), 35.0)})
    blend = save_target_blend(d, ARMS_DOWN, prefix="mixamorig:")
    req = request(os.path.join(d, "out"), {"blend": blend},
                  {"preview": False, "clips": [{"source": src, "name": "c", "loop": False}]})
    res = finish(req, retarget.run(req))
    assert res["ok"], res.get("error")
    doc = json.load(open(res["outputs"]["clips"], encoding="utf-8"))
    assert doc["clips"][0]["degenerate_source_bones"] == ["Neck"], doc["clips"][0]
    rig = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    lib.assign_action(rig, bpy.data.actions["c"])
    before = set(bpy.data.objects)
    s_arm, sact, _, _ = lib.import_fbx_clip(src)
    f0 = doc["clips"][0]["trim"][0]
    worst_head, worst_neck = 0.0, 0.0
    for i in (0, 7, 15):  # 7 ~ peak of the neck rotation
        bpy.context.scene.frame_set(i)
        nb = rig.pose.bones["mixamorig:Neck"]
        worst_neck = max(worst_neck, math.degrees(nb.matrix_basis.to_quaternion().angle))
        deltas = []
        for arm, frame in ((rig, i), (s_arm, int(f0) + i)):
            bpy.context.scene.frame_set(frame)
            b = arm.pose.bones["mixamorig:Head"]
            W = arm.matrix_world
            deltas.append(lib.rot3(W @ b.matrix) @ lib.rot3(W @ b.bone.matrix_local).inverted())
        a = math.degrees(deltas[0].rotation_difference(deltas[1]).angle)
        worst_head = max(worst_head, min(a, 360 - a))
    assert worst_neck < 0.5, worst_neck      # target neck follows its parent
    assert worst_head < 1.0, worst_head      # head world rotation = source head
    for o in [o for o in bpy.data.objects if o not in before]:
        bpy.data.objects.remove(o, do_unlink=True)
    shutil.rmtree(d, ignore_errors=True)


def test_speed_fps_trim():
    """speed halves the frame count, trim cuts the source range, fps resamples (one GLB = one
    sampling rate: a second fps in the same request is overridden with a warning)."""
    d = tmpdir("speed")
    src = synthetic_clip_fbx(d)  # source frames 2..32 at 30 fps
    blend = save_target_blend(d, ARMS_DOWN, prefix="mixamorig:")
    req = request(os.path.join(d, "o1"), {"blend": blend}, {"preview": False, "clips": [
        {"source": src, "name": "fast", "speed": 2.0, "loop": False},
        {"source": src, "name": "cut", "trim": [12, 22], "loop": False},
        {"source": src, "name": "other_fps", "fps": 24, "loop": False}]})
    res = finish(req, retarget.run(req))
    assert res["ok"], res.get("error")
    by = {c["gltf_name"]: c for c in json.load(open(res["outputs"]["clips"], encoding="utf-8"))["clips"]}
    assert by["fast"]["frames"] == 16 and abs(by["fast"]["duration"] - 0.5) < 1e-6, by["fast"]
    assert by["cut"]["frames"] == 11 and by["cut"]["trim"] == [12.0, 22.0], by["cut"]
    assert by["other_fps"]["fps"] == 30 and any("fps" in w for w in res["warnings"])
    req = request(os.path.join(d, "o2"), {"blend": blend}, {"preview": False, "clips": [
        {"source": src, "name": "f24", "fps": 24, "loop": False}]})
    res = finish(req, retarget.run(req))
    c = json.load(open(res["outputs"]["clips"], encoding="utf-8"))["clips"][0]
    assert c["frames"] == 25 and abs(c["gltf_duration"] - 1.0) < 0.01, c
    shutil.rmtree(d, ignore_errors=True)


FEET = ["mixamorig:LeftFoot", "mixamorig:RightFoot"]


def test_planted_feet_default_lock():
    """Regression (review blocker): a standing clip whose source feet never move, retargeted with
    the DEFAULT params (root_motion 'lock'), must keep the feet planted in the exported GLB.
    in_place:true removes only net travel (none here) -> still planted; in_place:'strict' (old
    behaviour: all hips XY removed) skates by the sway and the foot_skate check must say so."""
    d = tmpdir("plant")
    src = planted_clip_fbx(d)
    blend = save_target_blend(d, ARMS_DOWN, prefix="mixamorig:")
    req = request(os.path.join(d, "out"), {"blend": blend}, {"preview": False, "clips": [
        {"source": src, "name": "idle"},
        {"source": src, "name": "idle_trend", "in_place": True, "loop": False},
        {"source": src, "name": "idle_strict", "in_place": "strict", "loop": False},
        {"source": src, "name": "walk_like", "locomotion": True, "loop": False}]})
    res = finish(req, retarget.run(req))
    doc, _ = assert_common(res, ["idle-loop", "idle_trend", "idle_strict", "walk_like"],
                           res["outputs"]["clips"], preview=False,
                           skate_max={"idle_strict": 1e9, "walk_like": 1e9})
    by = {c["gltf_name"]: c for c in doc["clips"]}
    idle = by["idle-loop"]
    assert idle["root_motion"] == "keep" and idle["in_place"] is False, idle
    assert idle["foot_plant_drift_src_cm"] < 0.2 and idle["hips_drift_m"] > 0.05, idle
    for n in ("idle-loop", "idle_trend"):
        assert by[n]["foot_plant_drift_cm"] <= 1.0 and by[n]["foot_skate_cm"] <= 2.0, by[n]
    st = by["idle_strict"]
    assert st["hips_drift_m"] <= 0.001 and st["foot_plant_drift_cm"] > 3.0, st
    assert by["walk_like"]["in_place"] is True  # locomotion -> in place by default
    chk = {c["name"]: c for c in res["checks"]}
    assert chk["foot_skate_idle-loop"]["ok"] and not chk["foot_skate_idle_strict"]["ok"]
    # independent, end to end: re-import animated.glb and play the glTF animations
    ex = glb_ankle_excursion(res["outputs"]["glb"], "idle-loop", FEET)
    assert max(ex.values()) <= 0.01, ex
    ex = glb_ankle_excursion(res["outputs"]["glb"], "idle_strict", FEET)
    assert max(ex.values()) > 0.03, ex
    shutil.rmtree(d, ignore_errors=True)


def test_sockets_and_children_survive_export():
    """Review: animated.glb must keep the rig's whole hierarchy - contract section 6 sockets on
    bones (af.add_socket, as rig_humanoid makes them), a prop on a socket, a mesh parented to the
    body mesh."""
    d = tmpdir("sock")
    src = synthetic_clip_fbx(d)
    af.reset_scene()
    rig = skinned_target(ARMS_DOWN, prefix="mixamorig:")
    body = bpy.data.objects["Target_body"]
    hand = af.add_socket("socket_mount_hand_r", parent=rig, bone="mixamorig:RightHand",
                         location=(0, 0.04, 0), kind="mount", size_class="M")
    af.add_socket("socket_fx_head", parent=rig, bone="mixamorig:Head", kind="fx")
    for name, parent in (("Prop_sword", hand), ("Belt_pouch", body)):
        me = bpy.data.meshes.new(name)
        me.from_pydata([(0, 0, 0), (0.05, 0, 0), (0, 0.05, 0), (0, 0, 0.3)], [],
                       [(0, 1, 2), (0, 1, 3), (1, 2, 3), (0, 2, 3)])
        ob = bpy.data.objects.new(name, me)
        bpy.context.scene.collection.objects.link(ob)
        ob.parent = parent
    blend = os.path.join(d, "target.blend")
    af.save_blend(blend)
    req = request(os.path.join(d, "out"), {"blend": blend}, {"preview": False, "clips": [
        {"source": src, "name": "walk", "loop": True}]})
    res = finish(req, retarget.run(req))
    assert_common(res, ["walk-loop"], res["outputs"]["clips"], preview=False)
    summ = lib.glb_summary(res["outputs"]["glb"])
    assert summ["sockets"] == ["socket_fx_head", "socket_mount_hand_r"], summ["sockets"]
    assert {"Prop_sword", "Belt_pouch", "socket_mount_hand_r"} <= set(summ["non_joint_nodes"])
    js, _ = lib.read_glb(res["outputs"]["glb"])
    nodes = js["nodes"]
    idx = next(i for i, n in enumerate(nodes) if n.get("name") == "socket_mount_hand_r")
    parent = next(n for n in nodes if idx in n.get("children", []))
    assert parent["name"] == "mixamorig:RightHand", parent["name"]  # follows the hand joint
    ex = nodes[idx]["extras"]
    assert ex["game_socket"] == "socket_mount_hand_r" and ex["kind"] == "mount", ex
    shutil.rmtree(d, ignore_errors=True)


def test_foreign_actions_kept_out_of_glb():
    """Review: only the target rig's own actions are removed; a prop's animation and another
    armature's action stay in animated.blend and do not leak into animated.glb."""
    d = tmpdir("acts")
    src = synthetic_clip_fbx(d)
    af.reset_scene()
    rig = skinned_target(ARMS_DOWN, prefix="mixamorig:")
    pb = rig.pose.bones["mixamorig:Spine"]
    pb.rotation_mode = "QUATERNION"
    for f, a in ((1, 0.0), (10, 0.4)):
        pb.rotation_quaternion = Quaternion((0, 0, 1), a)
        pb.keyframe_insert("rotation_quaternion", frame=f)
    rig.animation_data.action.name = "OldClip"
    me = bpy.data.meshes.new("Crate")
    me.from_pydata([(0, 0, 0), (1, 0, 0), (0, 1, 0)], [], [(0, 1, 2)])
    crate = bpy.data.objects.new("Crate", me)
    bpy.context.scene.collection.objects.link(crate)
    crate.location = (2, 0, 0)
    crate.keyframe_insert("location", frame=1)
    crate.location = (2, 0, 1)
    crate.keyframe_insert("location", frame=20)
    crate.animation_data.action.name = "CrateBob"
    other = build_armature("Other", {"Bone": ((3, 0, 0), (3, 0, 1), None)})
    ob = other.pose.bones["Bone"]
    ob.rotation_mode = "QUATERNION"
    for f, a in ((1, 0.0), (15, 1.0)):
        ob.rotation_quaternion = Quaternion((1, 0, 0), a)
        ob.keyframe_insert("rotation_quaternion", frame=f)
    other.animation_data.action.name = "OtherWave"
    blend = os.path.join(d, "target.blend")
    af.save_blend(blend)
    req = request(os.path.join(d, "o1"), {"blend": blend}, {"preview": False, "clips": [
        {"source": src, "name": "walk", "loop": True}]})
    res = finish(req, retarget.run(req))
    assert_common(res, ["walk-loop"], res["outputs"]["clips"], preview=False)
    names = {a.name for a in bpy.data.actions}
    assert {"CrateBob", "OtherWave"} <= names and "OldClip" not in names, names
    assert bpy.data.objects["Crate"].animation_data.action.name == "CrateBob"
    assert any("OldClip" in w for w in res["warnings"]), res["warnings"]
    # keep_existing_actions: the rig's old clip is exported too, foreign actions still are not
    req = request(os.path.join(d, "o2"), {"blend": blend}, {"preview": False,
                                                           "keep_existing_actions": True,
                                                           "clips": [{"source": src,
                                                                      "name": "walk",
                                                                      "loop": True}]})
    res = finish(req, retarget.run(req))
    got = sorted(a["name"] for a in lib.glb_summary(res["outputs"]["glb"])["animations"])
    assert got == ["OldClip", "walk-loop"], got
    shutil.rmtree(d, ignore_errors=True)


def test_rig_transform_warning_and_apply():
    """Review: an armature object with scale 1.5 is reported (warning check) and, with
    params.apply_transforms, baked so the GLB armature node carries no scale."""
    d = tmpdir("xform")
    src = synthetic_clip_fbx(d)
    af.reset_scene()
    rig = skinned_target(ARMS_DOWN, prefix="mixamorig:")
    sock = af.add_socket("socket_mount_hand_r", parent=rig, bone="mixamorig:RightHand",
                         location=(0, 0.04, 0), kind="mount")
    rig.scale = (1.5, 1.5, 1.5)
    bpy.context.view_layer.update()
    rest_box = _rest_bbox(rig, [bpy.data.objects["Target_body"]])
    sock_rest = _rest_world(rig, sock)
    blend = os.path.join(d, "target.blend")
    af.save_blend(blend)
    clip = [{"source": src, "name": "walk", "loop": True}]
    req = request(os.path.join(d, "o1"), {"blend": blend}, {"preview": False, "clips": clip})
    res = finish(req, retarget.run(req))
    doc, summ = assert_common(res, ["walk-loop"], res["outputs"]["clips"], preview=False)
    chk = next(c for c in res["checks"] if c["name"] == "rig_transform_identity")
    assert not chk["ok"] and chk["severity"] == "warning" and res["ok"], chk
    assert any(n["name"] == "Target" and n["scale"] for n in summ["transformed_nodes"]), summ
    k_scaled = doc["clips"][0]["k_hip"]
    req = request(os.path.join(d, "o2"), {"blend": blend}, {"preview": False, "clips": clip,
                                                           "apply_transforms": True})
    res = finish(req, retarget.run(req))
    doc, summ = assert_common(res, ["walk-loop"], res["outputs"]["clips"], preview=False)
    chk = next(c for c in res["checks"] if c["name"] == "rig_transform_identity")
    assert chk["ok"], chk
    assert not any(n["name"] == "Target" for n in summ["transformed_nodes"]), summ
    js, _ = lib.read_glb(res["outputs"]["glb"])
    sn = next(n for n in js["nodes"] if n.get("name") == "socket_mount_hand_r")
    assert all(abs(x - 1.0) < 1e-4 for x in sn.get("scale", [1, 1, 1])), sn  # unit frame
    c = doc["clips"][0]
    assert abs(c["k_hip"] - k_scaled) < 1e-3, (c["k_hip"], k_scaled)  # same character size
    assert c["arm_dir_err_deg"] < 1.0, c
    rig = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    assert lib.transform_state(rig)["identity"]
    box = _rest_bbox(rig, [bpy.data.objects["Target_body"]])
    for a, b in zip(rest_box, box):  # the bake moved nothing: same rest body in the world
        assert (a - b).length < 1e-3, (rest_box, box)
    # the bone-parented socket stays where it was (transform_apply alone moves it 13 cm)
    moved = (_rest_world(rig, bpy.data.objects["socket_mount_hand_r"]) - sock_rest).length
    assert moved < 1e-3, moved
    shutil.rmtree(d, ignore_errors=True)


def _rest_world(rig, obj):
    keep = rig.data.pose_position
    rig.data.pose_position = "REST"
    bpy.context.view_layer.update()
    p = obj.matrix_world.translation.copy()
    rig.data.pose_position = keep
    bpy.context.view_layer.update()
    return p


def _rest_bbox(rig, meshes):
    keep = rig.data.pose_position
    rig.data.pose_position = "REST"
    bpy.context.view_layer.update()
    box = lib._evaluated_bbox(meshes)
    rig.data.pose_position = keep
    bpy.context.view_layer.update()
    return box


def test_clip_library_synthetic():
    d = tmpdir("lib")
    a = synthetic_clip_fbx(d, "Walk Synthetic.fbx")
    b = synthetic_clip_fbx(d, "walk_copy_reexport.fbx")      # same motion, new file
    shutil.copy2(a, os.path.join(d, "walk_same_bytes.fbx"))   # same bytes
    c = synthetic_clip_fbx(d, "Big Attack.fbx", amp=1.6, travel=0.0)
    ue = synthetic_clip_fbx(d, "UE Walk.fbx", amp=0.7, names=UE_NAMES)  # not a Mixamo skeleton
    library = os.path.join(d, "library")
    req = {"step": "clip_library", "params": {"action": "scan", "folder": d, "pattern": "*.fbx",
                                               "library": library}, "out_dir": os.path.join(d, "o1")}
    res = finish(req, clip_library.run(req))
    assert res["ok"] and res["stats"]["files"] == 5 and res["stats"]["duplicates"] == 2, res["stats"]
    assert not os.path.exists(library)  # scan copies nothing
    req["params"]["action"] = "import"
    req["out_dir"] = os.path.join(d, "o2")
    res = finish(req, clip_library.run(req))
    assert res["stats"]["new"] == 3 and res["stats"]["duplicates"] == 2, res["stats"]
    idx = lib.load_index(library)
    assert "redistribut" in idx["licence"]["note"]
    ids = sorted(e["id"] for e in idx["clips"])
    assert ids == ["fbx-ue-walk", "mixamo-big-attack", "mixamo-walk-synthetic"], ids
    for e in idx["clips"]:
        for key in ("id", "name", "category", "source", "file", "skeleton", "frames", "fps",
                    "root_motion", "tags", "imported_from", "has_skin", "sha1"):
            assert key in e, (key, e)
        assert os.path.exists(os.path.join(library, e["file"]))
        assert os.path.exists(os.path.join(library, e["poses"]))
        assert e["frames"] == 31
        # one value drives source, id prefix and sub-folder
        assert e["id"].startswith(e["source"] + "-"), e
        assert e["file"] == "%s/%s.fbx" % (e["source"], e["id"]), e
        assert e["poses"] == "%s/%s.poses.json" % (e["source"], e["id"]), e
        if e["id"].startswith("fbx-"):
            assert e["source"] == "fbx" and e["skeleton"] == "ue", e
        else:
            assert e["source"] == "mixamo" and e["skeleton"] == "mixamo", e
    walk = lib.library_entry(library, "mixamo-walk-synthetic")
    assert walk["category"] == "walk" and walk["root_motion"] is True
    assert len(walk["also_found"]) == 2
    att = lib.library_entry(library, "mixamo-big-attack")
    assert att["category"] == "attack" and att["root_motion"] is False
    poses = json.load(open(os.path.join(library, walk["poses"]), encoding="utf-8"))
    assert len(poses["samples"]) == 12 and len(poses["joints"]) >= 18
    assert all(len(s["p"]) == len(poses["joints"]) for s in poses["samples"])
    # re-import: nothing new (sha1 / motion against the library)
    req["out_dir"] = os.path.join(d, "o3")
    res = finish(req, clip_library.run(req))
    assert res["stats"]["new"] == 0 and res["stats"]["duplicates"] == 5, res["stats"]
    # retarget from a library id
    blend = save_target_blend(d, ARMS_DOWN, prefix="mixamorig:")
    rq = request(os.path.join(d, "o4"), {"blend": blend}, {
        "library": library, "preview": False,
        "clips": [{"source": "library:mixamo-big-attack"}, {"source": "mixamo-walk-synthetic"},
                  {"source": "library:fbx-ue-walk"}]})
    rr = finish(rq, retarget.run(rq))
    assert rr["ok"], rr.get("error")
    names = sorted(a["name"] for a in lib.glb_summary(rr["outputs"]["glb"])["animations"])
    assert names == ["big-attack", "ue-walk-loop", "walk-synthetic-loop"], names
    ue_rec = next(c for c in json.load(open(rr["outputs"]["clips"], encoding="utf-8"))["clips"]
                  if c["name"] == "ue-walk")
    assert ue_rec["source_skeleton"] == "ue" and ue_rec["arm_dir_err_deg"] < 1.0, ue_rec
    # preview of a non-mixamo entry writes into its own folder
    req["params"].update(action="preview", ids=["fbx-ue-walk"])
    res = finish(req, clip_library.run(req))
    assert lib.library_entry(library, "fbx-ue-walk")["poses"] == "fbx/fbx-ue-walk.poses.json"
    # preview and remove
    req["params"].update(action="preview", ids=["mixamo-big-attack"])
    res = finish(req, clip_library.run(req))
    assert res["stats"]["previews"] == 1
    req["params"].update(action="remove", ids=["mixamo-big-attack"])
    res = finish(req, clip_library.run(req))
    assert res["stats"]["removed"] == ["mixamo-big-attack"]
    assert not os.path.exists(os.path.join(library, "mixamo", "mixamo-big-attack.fbx"))
    assert len(lib.load_index(library)["clips"]) == 2
    shutil.rmtree(d, ignore_errors=True)


# =========================================================================== real assets

def _real_clipset(idle, walk, attack, hit, death):
    paths = [idle, walk, attack, hit, death]
    if not all(paths):
        return None
    return [{"source": idle, "name": "idle"},
            {"source": walk, "name": "walk", "loop": True, "in_place": True},
            {"source": attack, "name": "attack"},
            {"source": hit, "name": "hit"},
            {"source": death, "name": "death"}]


NAMES5 = ["idle-loop", "walk-loop", "attack", "hit", "death"]


def test_real_library_index():
    """The clip library on disk: contract fields, files present, unique sha1."""
    p = lib.index_path(LIBRARY)
    if not os.path.exists(p):
        return
    idx = lib.load_index(LIBRARY)
    cats = {"idle", "walk", "run", "attack", "cast", "hit", "death", "jump", "emote", "other"}
    shas = set()
    for e in idx["clips"]:
        assert e["category"] in cats, e
        assert e["id"].startswith("mixamo-") and e["source"] == "mixamo", e
        assert os.path.exists(os.path.join(LIBRARY, e["file"])), e["file"]
        assert e["sha1"] not in shas
        shas.add(e["sha1"])
        assert e["frames"] > 1 and e["fps"] > 0
    assert len(idx["clips"]) >= 30


# =========================================================================== v2 retarget options (10.10)

def turned_clip_fbx(folder, name="turned.fbx", frames=21, yaw_deg=40.0, drop=0.0, arm_down_deg=0.0, pitch_deg=0.0):
    """Standing clip turned yaw_deg about Z (an archer's sideways stance), the hips `drop` m below rest
    (a crouch), the left arm lowered by arm_down_deg from the T-pose."""
    keep = set(bpy.data.objects)
    P = "mixamorig:"
    rig = build_armature("SrcRig", MIXAMO_TPOSE, prefix=P)
    scene = bpy.context.scene
    scene.render.fps = 30
    scene.frame_start, scene.frame_end = 1, frames
    pbs, bones = rig.pose.bones, rig.data.bones
    for p in pbs:
        p.rotation_mode = "QUATERNION"
        p.location = (0, 0, 0)
        p.rotation_quaternion = Quaternion()
        p.keyframe_insert("location", frame=0)
        p.keyframe_insert("rotation_quaternion", frame=0)

    def local_q(name, axis, deg):
        R = bones[P + name].matrix_local.to_3x3()
        return Quaternion(R.inverted() @ Vector(axis), math.radians(deg))
    for f in range(1, frames + 1):
        ph = 2 * math.pi * (f - 1) / (frames - 1)
        R = bones[P + "Hips"].matrix_local.to_3x3()
        pbs[P + "Hips"].location = R.inverted() @ Vector((0.0, 0.0, -drop + 0.005 * math.sin(ph)))
        pbs[P + "Hips"].rotation_quaternion = (local_q("Hips", (1, 0, 0), pitch_deg) @
                                               local_q("Hips", (0, 0, 1), yaw_deg + 3 * math.sin(ph)))
        pbs[P + "LeftArm"].rotation_quaternion = local_q("LeftArm", (0, 1, 0), arm_down_deg)
        for p in pbs:
            p.keyframe_insert("location", frame=f)
            p.keyframe_insert("rotation_quaternion", frame=f)
    return _export_clip(rig, folder, name, keep)


def _eval(blend, action, frame, bones):
    af.open_blend(blend)
    rig = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    lib.assign_action(rig, bpy.data.actions[action])
    bpy.context.scene.frame_set(frame)
    return {b: rig.matrix_world @ rig.pose.bones[b].matrix for b in bones}


def test_facing_hips_drop_floor_hand_floor_and_ground():
    d = tmpdir("v2")
    try:
        turned = turned_clip_fbx(d, "turned.fbx", yaw_deg=40.0)
        crouch = turned_clip_fbx(d, "crouch.fbx", yaw_deg=0.0, drop=0.12)
        sink = turned_clip_fbx(d, "sink.fbx", yaw_deg=0.0, drop=0.9, pitch_deg=-90.0)   # lying, partly under z=0
        arm = turned_clip_fbx(d, "arm.fbx", yaw_deg=0.0, drop=0.12, arm_down_deg=80.0)
        blend = save_target_blend(d, ARMS_DOWN, prefix="mixamorig:")
        req = request(os.path.join(d, "out"), {"blend": blend}, {"preview": False, "clips": [
            {"source": turned, "name": "keep", "loop": False},
            {"source": turned, "name": "rest", "loop": False, "facing": "rest"},
            {"source": crouch, "name": "drop1", "loop": False},
            {"source": crouch, "name": "drop0", "loop": False, "hips_drop": 0.0},
            {"source": sink, "name": "sink", "loop": False},
            {"source": sink, "name": "sink_ground", "loop": False, "ground": "mesh"},
            {"source": arm, "name": "arm", "loop": False},
            {"source": arm, "name": "arm_floor", "loop": False, "hand_floor": {"l": 0.05}}]})
        res = finish(req, retarget.run(req))
        assert res["ok"], [c for c in res["checks"] if not c["ok"]]
        doc = json.load(open(res["outputs"]["clips"], encoding="utf-8"))
        by = {c["gltf_name"]: c for c in doc["clips"]}
        assert by["keep"]["facing"] is None and abs(by["rest"]["facing"]["yaw_removed_deg"] - 40.0) < 2.0, by["rest"]
        assert by["drop0"]["hips_drop"]["source_lowest_cm"] < -8.0, by["drop0"]
        for c in by.values():
            assert "floor" in c and c["floor"]["frames_sampled"] >= 2, c
        assert by["sink_ground"]["ground"]["lifted_frames"] > 0, by["sink_ground"]
        assert by["sink"]["floor"]["body_min_z_m"] < -0.05, by["sink"]["floor"]
        assert by["sink_ground"]["floor"]["body_min_z_m"] > -0.005, by["sink_ground"]["floor"]
        assert by["arm_floor"]["hand_floor_frames"]["Left"] > 0, by["arm_floor"]
        P = "mixamorig:"
        blend_out = res["outputs"]["blend"]
        rest_hips = Vector(ARMS_DOWN["Hips"][0])
        mid = 10
        # facing: keep turns the hips ~40 deg (hip line measured from the UpLeg heads), rest faces -Y again
        for name, want in (("keep", 40.0), ("rest", 0.0)):
            M = _eval(blend_out, name, mid, [P + "LeftUpLeg", P + "RightUpLeg"])
            lat = M[P + "LeftUpLeg"].translation - M[P + "RightUpLeg"].translation
            yaw = math.degrees(math.atan2(lat.y, lat.x))
            assert abs(abs(yaw) - want) < 6.0, (name, yaw, lat)
        # hips_drop 0: the hips stay at their rest height; 1: they go down with the source
        z1 = _eval(blend_out, "drop1", mid, [P + "Hips"])[P + "Hips"].translation.z
        z0 = _eval(blend_out, "drop0", mid, [P + "Hips"])[P + "Hips"].translation.z
        assert z1 < rest_hips.z - 0.08 and abs(z0 - rest_hips.z) < 0.01, (z1, z0, rest_hips.z)
        # hand_floor: the left wrist stays within 5 cm of its rest height, without it it drops
        w_rest = Vector(ARMS_DOWN["LeftHand"][0]).z
        wa = _eval(blend_out, "arm", mid, [P + "LeftHand"])[P + "LeftHand"].translation.z
        wf = _eval(blend_out, "arm_floor", mid, [P + "LeftHand"])[P + "LeftHand"].translation.z
        assert wf >= w_rest - 0.05 - 1e-3 and wf > wa + 0.01, (wa, wf, w_rest)
    finally:
        shutil.rmtree(d, ignore_errors=True)
