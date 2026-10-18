"""prepare_mixamo - FBX ready for manual upload to mixamo.com.

Ported from an earlier in-house script (single mesh,
faces -Y = FBX +Z front, feet on 0, height in cm, embedded texture, seam weld). Notes carried over
from that pipeline: browser uploads over ~10 MB fail, full-resolution uploads time out
on Mixamo's side - use params.lite (<= 12000 tris, 256 px texture); Tripo GLBs are split along UV
seams into ~100 pieces and Mixamo's auto-rigger fails on them ("Unknown error while generating
motion") - the weld fixes it. The forge never goes online: the owner uploads the file by hand.

inputs.mesh (any format af_common.import_any reads); inputs.rigged (optional rigged.glb/.blend from
rig_humanoid) for the pre-rigged FBX.
params:
  height_m 1.8, front "auto" (| -Y | +Y | +X | -X), height_ref "head"
  lite false            true -> tris 12000, tex 256
  tris 18000 / tex 1024 (full) - explicit values override lite
  weld 0.0001           metres, 0 disables
  center "trunk"        trunk | bbox | head - where x = 0 goes (Mixamo's marker symmetry line)
  prerigged false       also write mixamo_prerigged.fbx with the mixamorig skeleton and weights so
                        Mixamo skips its auto-rigger (from inputs.rigged or by running rig_humanoid
                        into <out_dir>/rig/)
  rig_params {}         params for that rig_humanoid run
  force_prerigged false the pre-rigged FBX is written only when that rig passes its QA checks (error
                        check prerigged_rig_ok otherwise); true writes it anyway (check -> warning)
outputs: mixamo_upload.fbx, mixamo_prerigged.fbx (optional), preview.png
"""
import copy
import os

import bpy
import numpy as np

import af_common as af
from steps import _humanoid_lib as hl

UPLOAD_LIMIT = 10_000_000


def _textures(limit):
    out = []
    for img in bpy.data.images:
        if img.source not in ("FILE", "GENERATED") or img.size[0] == 0:
            continue
        w, h = img.size
        if w > limit or h > limit:
            k = limit / float(max(w, h))
            img.scale(max(1, int(w * k)), max(1, int(h * k)))
        try:
            img.pack()
        except RuntimeError:
            pass
        out.append({"name": img.name, "size": list(img.size)})
    return out


def _decimate(ob, limit):
    tris = hl.tri_count(ob)
    if tris <= limit:
        return tris
    arm_mods = [(m.name, m.object) for m in ob.modifiers if m.type == "ARMATURE"]
    for m in list(ob.modifiers):
        ob.modifiers.remove(m)
    mod = ob.modifiers.new("AF_Decimate", "DECIMATE")
    mod.ratio = max(0.01, limit / float(tris))
    hl.select_only(ob)
    bpy.ops.object.modifier_apply(modifier=mod.name)
    for name, target in arm_mods:
        m = ob.modifiers.new(name, "ARMATURE")
        m.object = target
    return hl.tri_count(ob)


def _export(path, objects, rigged):
    hl.select_only(objects)
    kw = dict(filepath=path, use_selection=True, apply_unit_scale=True,
              apply_scale_options="FBX_SCALE_ALL", axis_forward="-Z", axis_up="Y",
              path_mode="COPY", embed_textures=True, bake_anim=False, add_leaf_bones=False,
              mesh_smooth_type="FACE")
    if rigged:
        kw.update(object_types={"ARMATURE", "MESH"}, primary_bone_axis="Y", secondary_bone_axis="X",
                  use_armature_deform_only=False, armature_nodetype="NULL")
    else:
        kw.update(object_types={"MESH"})
    bpy.ops.export_scene.fbx(**kw)
    return path


def _recentre(body, mode, H):
    v = hl.mesh_verts(body)
    if mode == "bbox":
        cx = (v[:, 0].min() + v[:, 0].max()) * 0.5
    elif mode == "head":
        band = v[v[:, 2] > 0.85 * H]
        cx = (np.percentile(band[:, 0], 5) + np.percentile(band[:, 0], 95)) * 0.5 if len(band) else 0.0
    else:
        return 0.0
    from mathutils import Matrix
    body.data.transform(Matrix.Translation((-cx, 0.0, 0.0)))
    body.data.update()
    return float(cx)


def run(req):
    os.makedirs(req["out_dir"], exist_ok=True)
    params = req["params"]
    H = float(params.get("height_m", 1.8))
    lite = bool(params.get("lite", False))
    tris_limit = int(params.get("tris", 12000 if lite else 18000))
    tex_limit = int(params.get("tex", 256 if lite else 1024))
    warnings, outputs, stats, checks = [], {}, {}, []

    prerigged_src = req["inputs"].get("rigged")
    if params.get("prerigged") and not prerigged_src:
        # run the local auto-rigger first, into <out_dir>/rig
        sub = copy.deepcopy(req)
        sub["step"] = "rig_humanoid"
        sub["out_dir"] = os.path.join(req["out_dir"], "rig")
        os.makedirs(sub["out_dir"], exist_ok=True)
        sub["params"] = dict(params.get("rig_params") or {})
        for k in ("height_m", "front", "height_ref"):
            if k in params:
                sub["params"].setdefault(k, params[k])
        from steps import rig_humanoid
        rres = rig_humanoid.run(sub)
        rig_failed = [c["name"] for c in rres.get("checks", [])
                      if not c["ok"] and c.get("severity", "error") == "error"]
        rig_ok = bool(rres.get("ok", True)) and not rig_failed
        force = bool(params.get("force_prerigged", False))
        stats["rig_humanoid"] = {"ok": rig_ok, "error": rres.get("error"), "failed_checks": rig_failed}
        blend = rres.get("outputs", {}).get("blend")
        outputs["rig_dir"] = sub["out_dir"]
        if rig_ok and blend:
            prerigged_src = blend
            checks.append(af.check("prerigged_rig_ok", True, [], "no failed error checks"))
        else:
            # a rig that failed QA must not reach Mixamo silently
            why = ", ".join(rig_failed) or rres.get("error") or "no rig"
            if blend and force:
                prerigged_src = blend
                warnings.append("rig_humanoid failed QA (%s); mixamo_prerigged.fbx written anyway "
                                "(params.force_prerigged)" % why)
            else:
                warnings.append("rig_humanoid failed QA (%s); mixamo_prerigged.fbx not written%s" % (
                    why, " (params.force_prerigged=true writes it anyway)" if blend else ""))
            checks.append(af.check("prerigged_rig_ok", False, rig_failed or rres.get("error"),
                                   "no failed error checks", severity="warning" if force else "error",
                                   note="see %s/result checks" % sub["out_dir"]))

    # ---- upload mesh (no skeleton): Mixamo's auto-rigger places its own markers
    body, P, prep = hl.prepare_body(req, warnings)
    shift = _recentre(body, params.get("center", "trunk"), H)
    welded = hl.weld(body, float(params.get("weld", 0.0001))) if float(params.get("weld", 0.0001)) > 0 else 0
    tris = _decimate(body, tris_limit)
    texs = _textures(tex_limit)
    path = af.out_path(req, "mixamo_upload.fbx")
    _export(path, [body], rigged=False)
    size = os.path.getsize(path)
    outputs["fbx"] = path
    outputs["preview"] = af.render_views(af.out_path(req, "preview.png"), [body],
                                         views=("front", "left", "three_quarter"), size=320)
    stats.update({"tris": tris, "bytes": size, "fits_upload": size < UPLOAD_LIMIT, "lite": lite,
                  "tris_limit": tris_limit, "texture_limit": tex_limit, "textures": texs,
                  "welded_verts": welded, "facing": prep["facing"], "scale": prep["scale"],
                  "height_m": H, "center": params.get("center", "trunk"), "center_shift_m": round(shift, 4)})
    checks.append(af.check("fbx_size", size < UPLOAD_LIMIT, size, UPLOAD_LIMIT,
                           note="browser upload limit"))
    checks.append(af.check("tris", tris <= tris_limit, tris, tris_limit))

    # ---- optional pre-rigged FBX (mixamorig skeleton + weights)
    if prerigged_src:
        af.reset_scene()
        if prerigged_src.lower().endswith(".blend"):
            af.open_blend(prerigged_src)
            objs = list(bpy.context.scene.objects)
        else:
            objs = af.import_any(prerigged_src)
        rigs = af.armatures(objs)
        meshes = [o for o in af.meshes(objs) if o.find_armature() in rigs] or af.meshes(objs)
        if not rigs or not meshes:
            raise ValueError("inputs.rigged has no armature/mesh: %s" % prerigged_src)
        rig = rigs[0]
        names = [bn.name for bn in rig.data.bones]
        if not all(n.startswith(hl.PFX) for n in names):
            warnings.append("prerigged skeleton has non-mixamorig bone names")
        rt = 0
        deform = [bn.name for bn in rig.data.bones if bn.use_deform]
        for ob in meshes:
            rt += _decimate(ob, tris_limit)
            Wm = hl.read_weights(ob, deform)    # decimation merges weights: back to <= 4
            hl.write_weights(ob, deform, hl.limit_normalize(Wm, 4))
        _textures(tex_limit)
        for o in list(bpy.context.scene.objects):   # sockets/empties are not for Mixamo
            if o.type not in ("ARMATURE", "MESH"):
                bpy.data.objects.remove(o, do_unlink=True)
        bpy.context.view_layer.update()
        p2 = af.out_path(req, "mixamo_prerigged.fbx")
        _export(p2, [rig] + meshes, rigged=True)
        s2 = os.path.getsize(p2)
        outputs["fbx_prerigged"] = p2
        stats["prerigged"] = {"source": prerigged_src, "bones": len(names), "tris": rt, "bytes": s2,
                              "fits_upload": s2 < UPLOAD_LIMIT}
        checks.append(af.check("prerigged_fbx_size", s2 < UPLOAD_LIMIT, s2, UPLOAD_LIMIT))
    return {"outputs": outputs, "stats": stats, "checks": checks, "warnings": warnings}
