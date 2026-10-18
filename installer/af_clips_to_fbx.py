"""ActionForge clips (GLB, Quaternius skeleton, CC0) -> FBX for the AstraForge clip library (clip_library imports FBX).

    blender -b --factory-startup --python af_clips_to_fbx.py -- <actionforge_dl> <out_fbx_dir>

Each animations/*.glb: skeleton and a single animation only -> <out>/<id>.fbx (bones kept as-is: pelvis, upperarm_l, fingers;
AstraForge maps them to the Mixamo canon itself, _anim_lib._ue_table). Writes <out>/files.json for clip_library import
(name, category, tags, source=quaternius) from the ActionForge manifest.
"""
import json
import os
import sys

import bpy

argv = sys.argv[sys.argv.index("--") + 1:]
src, out = os.path.abspath(argv[0]), os.path.abspath(argv[1])
os.makedirs(out, exist_ok=True)
man = json.load(open(os.path.join(src, "manifests", "library.json"), encoding="utf-8"))
CAT = {"locomotion": None, "combat": "attack", "reactions": "hit"}
files = []
for cid, c in man["clips"].items():
    rel = c["file"].split("?")[0]
    glb = os.path.join(src, rel)
    if not os.path.exists(glb) or c.get("deprecated"):
        continue
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=glb)
    arm = next((o for o in bpy.context.scene.objects if o.type == "ARMATURE"), None)
    if arm is None or not bpy.data.actions:
        print("SKIP", cid)
        continue
    for o in list(bpy.context.scene.objects):
        if o.type == "MESH":
            bpy.data.objects.remove(o, do_unlink=True)
    act = bpy.data.actions[0]
    # in place: remove the root bone translation (ActionForge "Root travels"); the step toward the enemy is done by the combat director;
    # otherwise clip retargeting mistakes forward travel for lift (a strike carried the character 2.5 m upward)
    fcs = getattr(act, "fcurves", None)
    if fcs is None:                                   # Blender 5: channels live in action layers
        fcs = [fc for l in act.layers for st in l.strips for cb in st.channelbags for fc in cb.fcurves]
    for fc in list(fcs):
        if fc.data_path == 'pose.bones["root"].location':
            for kp in fc.keyframe_points:
                kp.co[1] = 0.0
                kp.handle_left[1] = kp.handle_right[1] = 0.0
    if not arm.animation_data:
        arm.animation_data_create()
    arm.animation_data.action = act
    if hasattr(arm.animation_data, "action_slot") and getattr(act, "slots", None):
        arm.animation_data.action_slot = act.slots[0]
    f0, f1 = (int(round(x)) for x in act.frame_range)
    bpy.context.scene.frame_start, bpy.context.scene.frame_end = f0, f1
    bpy.ops.object.select_all(action="DESELECT")
    arm.select_set(True)
    bpy.context.view_layer.objects.active = arm
    path = os.path.join(out, "quaternius-" + cid.replace("_", "-") + ".fbx") if "--library" in argv else os.path.join(out, cid + ".fbx")
    bpy.ops.export_scene.fbx(filepath=path, use_selection=True, object_types={"ARMATURE"}, add_leaf_bones=False,
                             bake_anim=True, bake_anim_use_all_actions=False, bake_anim_use_nla_strips=False,
                             bake_anim_force_startend_keying=True, armature_nodetype="NULL")
    tags = list(c.get("tags") or [])
    name = c.get("label") or cid
    low = cid.lower()
    category = ("idle" if "idle" in low else "walk" if ("walk" in low or "jog" in low) else "run" if "sprint" in low
                else "death" if "death" in low else "hit" if low.startswith("hit") else "cast" if "spell" in low
                else "attack" if any(k in low for k in ("sword", "punch", "melee", "shield_dash", "shield_oneshot", "pistol_shoot", "throw", "zombie_scratch"))
                else "other")
    files.append({"path": path, "name": name, "category": category, "tags": tags + ["actionforge", "quaternius", "cc0"],
                  "source": "quaternius"})
    print("CLIP", cid, f0, f1, category)
json.dump(files, open(os.path.join(out, "files.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)
print("TOTAL", len(files))
