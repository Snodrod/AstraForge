"""import_mixamo_rig - take an FBX rigged on mixamo.com and make it a forge asset.

inputs.fbx (or inputs.mesh): the file downloaded from Mixamo ("FBX Binary", "With Skin"; T-pose or
any clip). Mixamo works in centimetres and names bones mixamorig:* (mixamorig1:/2: when several
characters were rigged in one session).
params:
  height_m 1.8          mesh height after normalisation (feet on z=0, hips over x=y=0)
  texture_source ""     GLB with the original high-res textures (Mixamo returns the 256-1024 px
                        upload texture); the "hires_retex" idea of an earlier
                        in-house script
  retex "auto"          material (replace the material with the GLB one: same UV layout because the
                        upload came from that model) | image (swap the image in texture nodes only) |
                        auto (material when both sides have one material, else image)
  keep_animation true   keep the clip baked in the FBX as an action (exported as a glTF animation)
  clip ""               clip name for that animation (default: the FBX file name, e.g. "idle");
  loop "auto"           true/false or auto (idle/walk/run/loop/breath in the name): looping clips are
                        named "<clip>-loop" (contract 7: Godot's import marks them looping)
  sockets true          contract 6 human sockets (thumb side found from Thumb1 or the front axis)
  max_influences 4      weights limited/renormalised for glTF
outputs: rigged.glb, rigged.blend, preview.png
Bone rolls stay Mixamo's own (the clips of that character then play 1:1 by name); they differ from
rig_humanoid's documented convention - see docs/forge/humanoid.md.
"""
import os
import re

import bpy
import numpy as np
from mathutils import Matrix

import af_common as af
from steps import _humanoid_lib as hl

PREFIX_RE = re.compile(r"^mixamorig\d*:")


def _rename_prefix(rig, meshes):
    renamed = 0
    for bn in rig.data.bones:
        nn = PREFIX_RE.sub(hl.PFX, bn.name)
        if nn != bn.name:
            old = bn.name
            bn.name = nn       # Blender renames vertex groups and action paths with the bone
            renamed += 1
            for ob in meshes:
                g = ob.vertex_groups.get(old)
                if g is not None:
                    g.name = nn
    return renamed


def _scale_root_motion(rig, k):
    """Location channels of an armature's actions are in bone space and do not follow an applied
    object scale - rescale them."""
    n = 0
    for act in bpy.data.actions:
        for layer in getattr(act, "layers", []):
            for strip in layer.strips:
                for bag in strip.channelbags:
                    for fc in bag.fcurves:
                        if fc.data_path.endswith(".location"):
                            for kp in fc.keyframe_points:
                                kp.co[1] *= k
                                kp.handle_left[1] *= k
                                kp.handle_right[1] *= k
                            n += 1
        for fc in getattr(act, "fcurves", []) or []:
            if fc.data_path.endswith(".location"):
                for kp in fc.keyframe_points:
                    kp.co[1] *= k
                    kp.handle_left[1] *= k
                    kp.handle_right[1] *= k
                n += 1
    return n


def _apply_rig_transform(rig, meshes, M):
    """Bake an object-level transform M into the armature and its meshes (rest pose stays valid)."""
    s = M.to_scale()[0]
    for ob in meshes:
        mw = ob.matrix_world.copy()
        ob.parent = None
        ob.matrix_world = M @ mw
    rig.matrix_world = M @ rig.matrix_world
    bpy.context.view_layer.update()
    hl.select_only(meshes + [rig])
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    for ob in meshes:
        ob.parent = rig
        ob.matrix_parent_inverse.identity()
        if not any(m.type == "ARMATURE" for m in ob.modifiers):
            m = ob.modifiers.new("Armature", "ARMATURE")
            m.object = rig
        for m in ob.modifiers:
            if m.type == "ARMATURE":
                m.object = rig
    bpy.context.view_layer.update()
    return s


def _retex(meshes, path, mode, warnings):
    before_o, before_m, before_i = set(bpy.data.objects), set(bpy.data.materials), set(bpy.data.images)
    bpy.ops.import_scene.gltf(filepath=os.path.abspath(path))
    for o in [o for o in bpy.data.objects if o not in before_o]:
        bpy.data.objects.remove(o, do_unlink=True)
    mats = [m for m in bpy.data.materials if m not in before_m]
    imgs = [i for i in bpy.data.images if i not in before_i and i.size[0] > 0]
    slots = [s for ob in meshes for s in ob.material_slots]
    if mode == "auto":
        mode = "material" if (len(mats) == 1 and len({s.material for s in slots}) <= 1) else "image"
    info = {"mode": mode, "source": path}
    if mode == "material" and mats:
        for s in slots:
            s.material = mats[0]
        info["material"] = mats[0].name
    else:
        if not imgs:
            warnings.append("texture_source has no images: %s" % path)
            return info
        hi = max(imgs, key=lambda im: im.size[0] * im.size[1])
        replaced = []
        for s in slots:
            m = s.material
            if m is None or not m.use_nodes:
                continue
            for n in m.node_tree.nodes:
                if n.type == "TEX_IMAGE" and n.image is not None and n.image is not hi:
                    replaced.append((m.name, n.image.name, list(n.image.size)))
                    n.image = hi
        info.update({"image": hi.name, "size": list(hi.size), "replaced": replaced})
    for img in list(bpy.data.images):
        if img.users == 0:
            bpy.data.images.remove(img)
    for m in list(bpy.data.materials):
        if m.users == 0:
            bpy.data.materials.remove(m)
    return info


LOOP_WORDS = ("idle", "walk", "run", "loop", "breath", "jog", "sprint")


def _name_clip(rig, path, params):
    """Contract 7: one glTF animation per clip, named by the clip, '-loop' suffix for looping clips.
    Mixamo names its action 'Armature|mixamo.com|Layer0'; rename the armature's action (and its NLA
    tracks/strips) and drop actions that drive nothing in the exported rig."""
    base = os.path.splitext(os.path.basename(path))[0]
    clip = str(params.get("clip") or base).strip()
    clip = re.sub(r"[^A-Za-z0-9_.-]+", "_", clip).strip("_") or "clip"
    loop = params.get("loop", "auto")
    if loop in (None, "auto"):
        loop = any(w in clip.lower() for w in LOOP_WORDS)
    name = clip[:-5] if clip.endswith("-loop") else clip
    name = name + "-loop" if loop else name
    ad = rig.animation_data
    act = ad.action if ad else None
    if act is None:
        cands = [a for a in bpy.data.actions if a.users > 0] or list(bpy.data.actions)
        act = max(cands, key=lambda a: a.frame_range[1] - a.frame_range[0]) if cands else None
        if act is not None:
            rig.animation_data_create().action = act
    if act is None:
        return None
    old = act.name
    act.name = name
    if rig.animation_data:
        for tr in rig.animation_data.nla_tracks:
            tr.name = name
            for st in tr.strips:
                st.name = name
    for a in list(bpy.data.actions):
        if a is not act and a.users == 0:
            bpy.data.actions.remove(a)
    return {"name": act.name, "from": old, "loop": bool(loop),
            "frames": [float(act.frame_range[0]), float(act.frame_range[1])]}


def run(req):
    os.makedirs(req["out_dir"], exist_ok=True)
    params = req["params"]
    H = float(params.get("height_m", 1.8))
    warnings, outputs, stats, checks = [], {}, {}, []
    path = req["inputs"].get("fbx") or req["inputs"].get("mesh")
    if not path or not os.path.exists(path):
        raise FileNotFoundError("inputs.fbx not found: %s" % path)
    af.reset_scene()
    objs = af.import_any(path)
    rigs = af.armatures(objs)
    if not rigs:
        raise ValueError("no armature in %s - is this a Mixamo download 'With Skin'?" % path)
    rig = max(rigs, key=lambda a: len(a.data.bones))
    meshes = [o for o in af.meshes(objs) if o.find_armature() == rig]
    if not meshes:
        raise ValueError("no skinned mesh in %s (download 'With Skin')" % path)
    for o in list(bpy.context.scene.objects):
        if o not in meshes and o is not rig:
            bpy.data.objects.remove(o, do_unlink=True)
    bpy.context.view_layer.update()
    renamed = _rename_prefix(rig, meshes)
    bones = [bn.name for bn in rig.data.bones]
    missing = [n for n in hl.BONES25 if hl.b(n) not in bones and n not in hl.NON_DEFORM]
    if missing:
        warnings.append("skeleton lacks Mixamo bones: %s" % ", ".join(missing))
    src_scale = [round(x, 4) for x in rig.matrix_world.to_scale()]
    # 1) bake the FBX object transform (cm scale, axis rotation) into rest data
    keep_anim = bool(params.get("keep_animation", True))
    _apply_rig_transform(rig, meshes, Matrix.Identity(4))
    s_obj = src_scale[0]
    if keep_anim and abs(s_obj - 1.0) > 1e-4:
        _scale_root_motion(rig, s_obj)
    # 2) normalise height / ground / centre on the hips (rest pose)
    if rig.animation_data and not keep_anim:
        rig.animation_data.action = None
    saved = rig.data.pose_position
    rig.data.pose_position = "REST"
    bpy.context.view_layer.update()
    v = np.concatenate([hl.mesh_verts(m) for m in meshes])
    lo, hi = v.min(0), v.max(0)
    k = H / max(float(hi[2] - lo[2]), 1e-9)
    hips = rig.data.bones.get(hl.b("Hips"))
    hx, hy = (float(hips.head_local.x), float(hips.head_local.y)) if hips else \
        ((lo[0] + hi[0]) * 0.5, (lo[1] + hi[1]) * 0.5)
    M = Matrix.Scale(k, 4) @ Matrix.Translation((-hx, -hy, -float(lo[2])))
    _apply_rig_transform(rig, meshes, M)
    if keep_anim and abs(k - 1.0) > 1e-4:
        _scale_root_motion(rig, k)
    rig.data.pose_position = saved
    bpy.context.view_layer.update()
    # facing check (Mixamo exports face +Z in FBX = -Y in Blender)
    body = meshes[0] if len(meshes) == 1 else max(meshes, key=lambda m: len(m.data.vertices))
    P, _ = hl.sample_surface(hl.mesh_verts(body), hl.mesh_tris(body), 60000)
    plo, phi = P.min(0), P.max(0)
    front, fconf, _ = hl.detect_facing((P - [(plo[0] + phi[0]) / 2, (plo[1] + phi[1]) / 2, plo[2]]) /
                                       max(phi[2] - plo[2], 1e-9))
    if front != "-Y":
        warnings.append("character seems to face %s (confidence %.2f), expected -Y" % (front, fconf))
    # retexture
    if params.get("texture_source"):
        stats["retex"] = _retex(meshes, params["texture_source"], params.get("retex", "auto"), warnings)
    # sockets
    rig.data.pose_position = "REST"
    bpy.context.view_layer.update()
    names = [bn.name for bn in rig.data.bones if bn.use_deform]
    maxinf = int(params.get("max_influences", 4))
    for ob in meshes:      # glTF skins carry 4 influences; keep the 4 largest, renormalised
        Wm = hl.read_weights(ob, names)
        if (Wm > 1e-4).sum(1).max(initial=0) > maxinf:
            hl.write_weights(ob, names, hl.limit_normalize(Wm, maxinf))
    W = hl.read_weights(body, names)
    sockets = {}
    if params.get("sockets", True):
        sockets = hl.add_human_sockets(rig, body, names, W, H, thumb="auto")
    rig.data.pose_position = saved
    unweighted = int((W.sum(1) < 1e-6).sum())
    nb = len(rig.data.bones)
    checks.append(af.check("mixamo_bones", not missing, len(bones), "25 core Mixamo bones",
                           note=", ".join(missing) or None))
    checks.append(af.check("unweighted_vertices", unweighted == 0, unweighted, 0, severity="warning"))
    v2 = np.concatenate([hl.mesh_verts(m) for m in meshes])
    checks.append(af.check("height", abs(float(v2[:, 2].max() - v2[:, 2].min()) - H) < 0.01 * H,
                           round(float(v2[:, 2].max() - v2[:, 2].min()), 4), H))
    clip_info = None
    if keep_anim and bpy.data.actions:
        clip_info = _name_clip(rig, path, params)
    actions = [a.name for a in bpy.data.actions]
    outputs["preview"] = af.render_views(af.out_path(req, "preview.png"), meshes,
                                         views=("front", "left", "three_quarter"), size=320)
    export = [rig] + meshes + list(sockets.values())
    outputs["glb"] = af.export_glb(af.out_path(req, "rigged.glb"), objects=export,
                                   animations=bool(keep_anim and actions))
    outputs["blend"] = af.save_blend(af.out_path(req, "rigged.blend"))
    stats.update({"bones": nb, "fingers": any("Thumb" in n for n in bones), "renamed_bones": renamed,
                  "source_object_scale": src_scale, "scale_to_height": round(k, 6),
                  "facing": {"detected": front, "confidence": fconf}, "actions": actions,
                  "tris": af.tri_count(meshes), "sockets": sorted(sockets), "height_m": H,
                  "bone_rolls": "Mixamo native (unchanged)", "clip": clip_info})
    return {"outputs": outputs, "stats": stats, "checks": checks, "warnings": warnings}
