"""rig_creature — automatic skeleton + skin weights for non-human creatures (contract §5, §6).

inputs.mesh   path to .glb/.gltf/.fbx/.obj/.stl/.blend, or
inputs.blend  a .blend (e.g. from cleanup); mesh wins when both are given. Hierarchies, empties,
              armatures, skins and shape keys are stripped: only the rest-shape meshes are kept.
params
  template      'auto' (default) | quadruped | winged_quadruped | bird | hexapod | octopod | serpent | custom
  height_m      target height (metres) or
  length_m      target body length along the facing axis (metres); none = keep the native size
  facing        'auto' (default) or the direction the HEAD points in the input: '-Y' | '+Y' | '-X' | '+X'
  head_dir      same labels as facing (the server sends this one; 'auto' = detect) or an [x, y] vector;
                overrides facing when it is not 'auto'
  tail          'auto' (default) | false | <int bones>
  jaw           'auto' (default, only if an open mouth is found) | true (force) | false
  weights       'auto' (default: bone heat, fallback to chain weights) | 'heat' | 'distance'. Heat runs on
                a copy scaled to span >= 4.5 m and median edge >= 5 cm (Blender's solver fails silently on
                small triangles); when auto rejects heat, the reason is in warnings and rig.json
  custom_chains [{name, parent, points:[[x,y,z],..], bones?:[...], side?:'l'|'r', kind?}]  (template custom;
                points in the OUTPUT frame: metres after normalisation, creature facing -Y)
  leg_ratios    {"front": {"mid": .5, "paw": .16}, "back": {...}} heights of carpus/hock and paw joint
                as fractions of the belly height (mammals); {"mid": .45} for birds
  voxel_res     voxels along the longest side (default 112)
  symmetrize    true (default): average mirrored leg joints when the mesh is near-symmetric
  preview       true (default)
outputs: rigged.glb, rigged.blend, rig.json, preview.png
The creature ends up facing -Y (left side +X), ground at z=0, centred in X/Y (contract §4).
"""
import json
import math
import os

import bpy
from mathutils import Matrix

import af_common as af
from steps import _creature_lib as L


def run(req):
    p = req["params"]
    inputs = req.get("inputs", {})
    src = inputs.get("mesh") or inputs.get("blend")
    if not src:
        raise ValueError("rig_creature needs inputs.mesh or inputs.blend")
    af.reset_scene()                  # a blend opened by af_run is re-read through import_any below
    objs = af.import_any(src)
    ob, welded = L.prepare_mesh(objs)
    af.log("mesh prepared", verts=len(ob.data.vertices), welded=welded)
    warnings = []
    # ---------------------------------------------------------------- facing + scale
    head, finfo = L.detect_facing(ob, p)
    yaw = L.yaw_to_minus_y(head)
    if abs(yaw) > 1e-6:
        ob.matrix_world = Matrix.Rotation(math.radians(yaw), 4, "Z") @ ob.matrix_world
        af.apply_transforms([ob])
    finfo.update({"head_in_input": L.head_dir_label(head), "rotated_deg": round(yaw, 3)})
    if finfo.get("confidence", 1.0) < 0.5:
        warnings.append("facing detection is uncertain (%.2f): pass params.facing" % finfo["confidence"])
    lo, hi = af.world_bbox([ob])
    size = hi - lo
    target_h = None
    if p.get("height_m"):
        target_h = float(p["height_m"])
    elif p.get("length_m"):
        target_h = float(size.z) * float(p["length_m"]) / max(float(size.y), 1e-9)
    else:
        warnings.append("no height_m/length_m: native size kept")
    scale = af.normalize([ob], height=target_h)
    af.log("normalised", scale=round(scale, 5), yaw=round(yaw, 2))
    # ---------------------------------------------------------------- analysis
    A = L.analyse(ob, p)
    auto_t, evidence, tconf = L.classify(A)
    template = p.get("template", "auto") or "auto"
    if template == "auto":
        template = auto_t
    elif template not in L.TEMPLATES:
        raise ValueError("unknown template %r" % template)
    elif template != auto_t and template != "custom":
        warnings.append("requested template %s, geometry looks like %s" % (template, auto_t))
    af.log("analysed", template=template, auto=auto_t, pairs=len(A["_pairs"]), wings=len(A["_wings"]))
    lay = L.build_layout(A, template, p)
    warnings += lay.warnings
    arm = L.build_armature(lay)
    winfo = L.skin(ob, arm, lay, A, p.get("weights", "auto"))
    af.log("skinned", mode=winfo.get("mode"))
    rej = winfo.get("heat", {}).get("rejected")
    if rej and winfo.get("mode") == "distance_fallback":
        warnings.append("bone heat rejected (%s): distance weights used instead" % rej)
    elif rej:
        warnings.append("bone heat kept although it %s (weights=heat)" % rej)
    deform = [n for n in lay.order if lay.bones[n]["deform"]]
    sockets = L.place_sockets(lay, arm, ob, A, template)
    lay.conf["facing"] = finfo.get("confidence", 1.0)
    lay.conf["template"] = tconf if template == auto_t else 0.5
    conf_all = L.overall_confidence(lay.conf)
    meta = L.rig_meta(lay, A, template, {
        "template_auto": auto_t, "template_evidence": evidence, "facing": finfo, "scale": round(scale, 6),
        "weights": winfo, "sockets": sockets, "confidence_overall": conf_all,
        "analysis": {"cell_m": round(A["cell"], 5), "grid": A["grid_shape"], "solid_voxels": A["solid_voxels"],
                     "opening_radius_cells": A["opening_radius_cells"], "contacts": A["contacts"],
                     "contact_merges": A.get("contact_merges", 0),
                     "torso_halfwidth_m": round(A["torso_halfwidth"], 4), "front_tip": A["front_tip"],
                     "rear_tip": A["rear_tip"]},
        "frame": "metres, Z up, creature faces -Y, left side +X, ground z=0",
        "source": os.path.abspath(src),
    })
    arm["af_rig"] = json.dumps(meta, ensure_ascii=False)
    ob["af_creature_mesh"] = True
    # ---------------------------------------------------------------- outputs
    outputs = {}
    outputs["blend"] = af.save_blend(af.out_path(req, "rigged.blend"))
    sock_objs = [o for o in bpy.data.objects if o.get("game_socket")]
    outputs["glb"] = af.export_glb(af.out_path(req, "rigged.glb"), objects=[arm, ob] + sock_objs,
                                   animations=False)
    if p.get("preview", True):
        outputs["preview"] = L.render_rig_preview(af.out_path(req, "preview.png"), ob, arm, sock_objs,
                                                  size=int(p.get("size", 384)))
    rig_json = af.out_path(req, "rig.json")
    with open(rig_json, "w", encoding="utf-8") as fh:
        json.dump(meta, fh, ensure_ascii=False, indent=1)
    outputs["rig_json"] = rig_json
    # ---------------------------------------------------------------- checks
    need = len(L.LEG_POSITIONS.get(template, [])) * 2
    have = len(lay.legs)
    checks = [
        af.check("unweighted_vertices", winfo["unweighted_after"] == 0, winfo["unweighted_after"], 0),
        af.check("max_influences", winfo["max_influences"] <= 4, winfo["max_influences"], 4),
        af.check("legs_fitted", have == need or template in ("serpent", "custom"), have, need),
        af.check("bones_without_vertices", len(winfo["bones_without_verts"]) == 0,
                 winfo["bones_without_verts"], [], severity="warning"),
        af.check("facing_confidence", finfo.get("confidence", 1.0) >= 0.5, finfo.get("confidence"), 0.5,
                 severity="warning"),
        af.check("template_matches_geometry", template == auto_t or template == "custom", auto_t, template,
                 severity="warning"),
        af.check("rig_confidence", conf_all >= 0.6, conf_all, 0.6, severity="warning"),
    ]
    if template in L.WINGED:
        checks.append(af.check("wings_fitted", len(lay.wings) == 2, len(lay.wings), 2))
        folded = [w["side"] for w in lay.wings if w.get("folded")]
        checks.append(af.check("wings_separate", not folded, folded, [], severity="warning",
                               note="folded wings: bones fitted along the upper flank, no fly/glide clips"))
    stats = af.scene_stats([ob, arm])
    stats.update({"template": template, "bones": len(lay.order), "deform_bones": len(deform),
                  "weights_mode": winfo.get("mode"), "confidence": conf_all, "sockets": len(sockets)})
    return {"outputs": outputs, "stats": stats, "checks": checks, "warnings": warnings}
