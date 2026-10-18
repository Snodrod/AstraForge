"""assemble_parts - join separately generated body parts into one model (docs/forge/parts.md).

The whole-body Hunyuan3D-2mv latent gives a head and hands only a few voxels. scripts/parts/ cuts the
front / back references into head, torso, arms and legs, generates every part from a crop that fills the
generator's frame (several times the voxels per part) and writes parts.json. This step places those part
meshes on the whole-body base mesh and fuses them.

inputs:
  mesh | blend    the whole-body base mesh (.glb/.gltf/.fbx/.obj/.blend, e.g. Hunyuan mesh_mv_<seed>.glb): it
                  gives the depth (Y) of every part and fills the place of a part that has no mesh
  concept         the FRONT reference (RGBA, alpha = subject) the parts were cut from: the camera is fitted to
                  its silhouette, so reference pixels map to metres (params.concept_file on the CLI)
  parts           parts.json of scripts/parts/parts_prep.py + parts_generate.py (params.parts_file on the CLI);
                  entries {name, mesh, front_box}; params.parts: [{name, mesh, box: [x0, y0, x1, y1]}] adds more

Algorithm:
  1. base: joined, scaled to height_m, feet on z = 0, centred in X / Y (like cleanup).
  2. front camera: orthographic, yaw = pitch = 0, scale + offset fitted to the reference silhouette by IoU
     (the fit of paint_from_concept with the rotation locked: a Hunyuan mesh made from the reference is front
     aligned). Pixel boxes of the parts become world boxes (X, Z).
  3. every part mesh (its own Hunyuan frame, bbox centred, long side 2) is registered:
       X / Z  scale per axis and shift from the silhouette: bbox init, then Nelder-Mead on the IoU of the part's
              front projection with the reference silhouette inside the box (the reference is the truth for
              the front view - the part was generated from exactly that crop; the whole-body base is less exact
              there: its helmet is smaller and blobbier);
       Y      no image pins the depth: the shift lays the part's front / back depth maps on the base's (median),
              the scale takes the base's front-to-back thickness (depth_scale base, clamped 0.8..1.25);
       ICP    optional (refine icp): symmetric trimmed ICP, rigid (rotation <= icp_max_rot_deg) + uniform scale,
              against base surface samples inside the box expanded by box_expand; the residual is reported in
              every mode. A part with a high residual is also tried turned 180 deg about Z.
  4. joints: where two boxes overlap in a slab (the margins of scripts/parts) each part is cut at the line of
     the slab where the two cross-sections agree best, +- joint_overlap_m (Boolean, Manifold solver): the parts
     overlap by a thin slab only; without it the full margins show every difference of the surfaces as a flat
     plate. Debris below island_min is dropped.
  5. a part without a mesh: the base inside its box (minus the joint cuts) takes its place (fill_gaps).
  6. fuse: all parts + base pieces joined and voxel remeshed (the union: overlapping shells vanish), optional
     collapse decimation (target_tris), normals, height_m / feet on z = 0 / centred again.

params (all optional):
  height_m 1.8           base and result height
  voxel_m null           voxel size of the union, m (default 0.0016 x height; a part is generated at up to
                         ~0.5 mm, the whole body at ~3.5 mm: coarser voxels wash the part detail out)
  target_tris null       collapse decimation of the result to <= this many triangles
  refine "silhouette"    silhouette | icp | none (see 3)
  depth_scale "base"     base | part (the part's own proportions) | a factor on them
  trim_overlap true, joint_overlap_m null (2 voxels), island_min 0.002
  box_expand 0.012       fraction of the height; icp_iters 40, icp_trim 0.8, icp_samples 15000,
                         icp_max_rot_deg 6, icp_scale_range [0.8, 1.25]
  icp_warn_m 0.012       warning part_residual above this trimmed RMS to the base
  try_flip true, flip_rms_m 0   (the turned part is tried when the residual is above this; 0 = always)
  fill_gaps true, gap_min_tris 1500
  camera "fit"           "fit" | {px_per_m, offset_px: [x, y], centre: [x, y, z]} (parts_fit.json -> camera)
  pick {}                {part: seed | mesh path}: which mesh of parts.json to use
  shade_auto_smooth_deg 40   smooth shading with sharp edges above this angle (0 = flat, null = leave)
  export_mv false        also assembled_mv.glb in the normalisation of the generator's meshes (bbox centred
                         at the origin, long side 2): drop-in seed for the studio's mesh_mv_<seed>.glb
  preview true, preview_size 384, head_closeup true

outputs: glb (assembled.glb), blend (assembled.blend: Body + the hidden base), preview (preview.png: base row
  over assembled row: front, back, side + head close-up), parts_fit (parts_fit.json), glb_mv (export_mv).
stats: camera, iou (base / assembled vs the reference, assembled vs base front), parts (scale xyz, offset,
  per-part silhouette IoU, depth shift, thickness ratio, residuals, trim), joints, remainder, tris, timings_s.
checks: parts_found (error), watertight (error: open edges), manifold (warning), no_nan_verts, height_matches,
  grounded, silhouette_iou_min (error, IoU >= 0.85), silhouette_iou (warning: not below the base by more than
  0.01), part_residual_<name> (warning), part_flipped_<name> (warning).
"""
import json
import math
import os
import time

import bpy
import mathutils
import numpy as np

import af_common as af
from steps import _mesh_lib as ml
from steps import _paint_lib as pl
from steps import _parts_lib as pt
from steps import _replace_lib as rl

IOU_FAIL = 0.85


def _open_input(req):
    inputs = req.get("inputs") or {}
    if inputs.get("mesh"):
        src = os.path.abspath(str(inputs["mesh"]))
        if not os.path.isfile(src):
            raise FileNotFoundError("inputs.mesh not found: %s" % src)
        if src.lower().endswith(".blend"):
            af.open_blend(src)
        else:
            af.reset_scene()
            af.import_any(src)
        return src
    blend = inputs.get("blend")
    if not blend:
        raise ValueError("inputs.mesh or inputs.blend is required")
    blend = os.path.abspath(str(blend))
    if not os.path.isfile(blend):
        raise FileNotFoundError("inputs.blend not found: %s" % blend)
    if os.path.normcase(os.path.abspath(bpy.data.filepath or "")) != os.path.normcase(blend):
        af.open_blend(blend)
    return blend


def _weld(ob):
    """glTF import splits vertices at normal / UV seams: the topology is open, the Boolean solver wants a
    closed manifold. Weld them (1e-5 of the object size)."""
    lo, hi = af.world_bbox([ob])
    ml.merge_by_distance(ob, 1e-5 * max(float(max(hi - lo)), 1e-6))


def _import_part(path, name, warnings):
    """One generated part -> a single mesh object in world space with applied transforms."""
    objs = af.import_any(path)
    objs, _helpers = ml.purge_import_helpers(objs)
    objs = ml.prepare_meshes(objs, warnings)
    if not objs:
        raise ValueError("no mesh in part file %s" % path)
    ob = af.join(objs, name="part_" + name)
    ob.data.name = "part_" + name
    bpy.context.view_layer.update()
    _weld(ob)
    return ob


def _render_rows(path, base, asm, lo, hi, head_box, size, closeup):
    """Preview sheet: base row over assembled row; front, back, side and a head close-up."""
    views = ("front", "back", "right")
    rows = []
    for ob in (base, asm):
        _p, tiles = ml.render_sheet(af.out_path({"out_dir": os.path.dirname(path)}, "_row.png"), [ob], views=views,
                                    size=size, bbox=(lo, hi), color_type="MATERIAL")
        if closeup and head_box is not None:
            _p, t2 = ml.render_sheet(af.out_path({"out_dir": os.path.dirname(path)}, "_row.png"), [ob],
                                     views=("front", "three_quarter"), size=size, bbox=head_box, color_type="MATERIAL")
            tiles = tiles + t2
        rows.append(np.concatenate(tiles, axis=1))
    try:
        os.remove(af.out_path({"out_dir": os.path.dirname(path)}, "_row.png"))
    except OSError:
        pass
    # Blender arrays are bottom row first: the LAST row of the stack is the top of the picture
    sheet = np.concatenate([rows[1], rows[0]], axis=0)
    return ml.side_by_side(path, [sheet])


def run(req):
    p = req["params"]
    warnings = []
    timings = {}
    t_all = time.time()

    def lap(name, t0):
        timings[name] = round(time.time() - t0, 2)
        af.log(name, seconds=timings[name])

    inputs = req.get("inputs") or {}
    concept_path = inputs.get("concept") or p.get("concept_file")
    if not concept_path or not os.path.isfile(str(concept_path)):
        raise ValueError("inputs.concept (the front reference image) is required: %s" % concept_path)
    parts_path = inputs.get("parts") or p.get("parts_file")
    if parts_path and not inputs.get("parts"):
        inputs = dict(inputs, parts=parts_path)
    height = float(p.get("height_m", 1.8))
    expand = float(p.get("box_expand", 0.012)) * height

    # ------------------------------------------------------------------ base
    t0 = time.time()
    src_path = _open_input(req)
    objs = ml.prepare_meshes([o for o in bpy.context.scene.objects if o.type in ("MESH", "ARMATURE", "EMPTY")], warnings)
    if not objs:
        raise ValueError("no mesh objects in %s" % src_path)
    base = af.join(objs, name="Base")
    base.data.name = "Base"
    af.normalize([base], height=height, ground=True, center=True)
    _weld(base)
    bpy.context.view_layer.update()
    Vb, Tb = pt.world_arrays(base)
    lap("base", t0)

    # ------------------------------------------------------------------ reference + camera
    t0 = time.time()
    px = pl.load_rgba(str(concept_path))
    px, _premult = pl.unpremultiply(px)
    fit_mask, _col, mask_info = pl.concept_masks(px)
    cam_in = p.get("camera", "fit")
    if isinstance(cam_in, dict) and cam_in.get("px_per_m"):
        off = cam_in.get("offset_px") or [0, 0]
        centre = cam_in.get("centre") or [0.0, 0.0, height / 2]
        cam = pl.OrthoCam(0.0, 0.0, cam_in["px_per_m"], off[0], off[1], (fit_mask.shape[1], fit_mask.shape[0]), 0.0, centre)
        iou_base = pt.iou_front(Vb, Tb, cam, fit_mask)
    else:
        cam, iou_base = pt.fit_front_camera(Vb, Tb, fit_mask)
    lap("camera", t0)
    af.log("front camera", iou=round(iou_base, 4), px_per_m=round(cam.s, 2))

    # ------------------------------------------------------------------ parts
    voxel = p.get("voxel_m")
    voxel = float(voxel) if voxel else 0.0016 * height
    parts = pt.read_parts(inputs, p, warnings)
    present = [x for x in parts if x["mesh"]]
    missing = [x for x in parts if not x["mesh"]]
    placed = []
    fit_report = []
    t_icp = time.time()
    c_base = pt.face_centroids(Vb, Tb)
    opt = {"samples": int(p.get("icp_samples", 15000)), "expand": expand, "refine": p.get("refine", "silhouette"), "depth_scale": p.get("depth_scale", "base"),
           "iters": int(p.get("icp_iters", 40)), "trim": float(p.get("icp_trim", 0.8)),
           "max_rot_deg": float(p.get("icp_max_rot_deg", 6.0)), "scale_range": p.get("icp_scale_range", [0.8, 1.25]),
           "try_flip": bool(p.get("try_flip", True)), "flip_rms_m": float(p.get("flip_rms_m", 0.0))}
    for part in present:
        t0 = time.time()
        name = part["name"]
        wb = pt.px_box_to_world(cam, part["box"])
        subj = pt.subject_world_box(cam, fit_mask, part["box"])
        ob = _import_part(part["mesh"], name, warnings)
        Vp, Tp = pt.world_arrays(ob)
        ys = pt.slice_y(Vb, cam, fit_mask, part["box"])
        y_mid = float(np.mean(np.percentile(ys, [2, 98]))) if len(ys) else float(np.median(Vb[:, 1]))
        try:
            M, info = pt.register_part(Vp, Tp, Vb, Tb, c_base, cam, fit_mask, part["box"], wb, subj, y_mid, opt)
        except ValueError as exc:
            warnings.append("part %s: %s; the base fills its box" % (name, exc))
            ml.remove_object(ob)
            missing.append(part)
            continue
        ob.data.transform(mathutils.Matrix(M.tolist()))
        ob.data.update()
        bpy.context.view_layer.update()
        lo, hi = af.world_bbox([ob])
        scale = [float(np.linalg.norm(M[:3, k])) for k in range(3)]
        rec = {"name": name, "mesh": part["mesh"], "box_px": part["box"],
               "box_m": [round(v, 5) for v in wb], "subject_m": [round(v, 5) for v in subj],
               "scale": [round(v, 6) for v in scale], "scale_uniform": round(float(np.cbrt(np.prod(scale))), 6),
               "aspect_vs_generator": info["aspect_init"],
               "offset_m": [round(float(v), 5) for v in M[:3, 3]], "mode": info["mode"],
               "flipped": info["flipped"], "iou_init": info["iou_init"], "iou_silhouette": info["iou_silhouette"],
               "iou_final": info["iou_final"], "depth_shift_m": info["depth_shift_m"],
               "thickness_ratio_base_part": info["thickness_ratio"], "thickness_ratio_after": info["thickness_ratio_after"],
               "residual_rms_m": round(info["residual_rms_m"], 6), "residual_median_m": round(info["residual_median_m"], 6),
               "inlier_5mm": round(info["inlier_5mm"], 4), "samples": info["samples"],
               "bbox_min": [round(float(v), 4) for v in lo], "bbox_max": [round(float(v), 4) for v in hi],
               "part_tris": int(len(Tp)), "seconds": round(time.time() - t0, 2)}
        if "icp" in info:
            rec["icp"] = info["icp"]
        # how far the placed part's X / Z extent leaves the box it was cut from
        rec["box_overshoot_m"] = [round(float(max(wb[0] - lo.x, hi.x - wb[1], 0.0)), 5),
                                  round(float(max(wb[2] - lo.z, hi.z - wb[3], 0.0)), 5)]
        fit_report.append(rec)
        placed.append((part, ob, wb))
        af.log("part placed", part=name, rms_mm=round(1000 * rec["residual_rms_m"], 2), iou=rec["iou_final"],
               aspect=rec["aspect_vs_generator"], flipped=rec["flipped"])
    timings["place_parts"] = round(time.time() - t_icp, 2)

    # ------------------------------------------------------------------ joints: trim the overlaps
    t0 = time.time()
    boxes = {x["name"]: pt.px_box_to_world(cam, x["box"]) for x in parts}
    joints = []
    cuts = {k: [] for k in boxes}
    eps = float(p.get("joint_overlap_m") or 2.0 * voxel)
    if p.get("trim_overlap", True) is not False and len(boxes) > 1:
        pts = {part["name"]: pt.world_arrays(ob)[0] for part, ob, _wb in placed}
        cuts, joints = pt.joint_cuts(boxes, eps, height, pts)
        for part, ob, _wb in placed:
            tris0 = ml.tris_of(ob)
            try:
                solver, islands = pt.trim_part(ob, boxes[part["name"]], cuts[part["name"]], float(p.get("island_min", 0.002)))
            except RuntimeError as exc:
                warnings.append("part %s: trimming failed (%s), the full overlap is kept" % (part["name"], exc))
                continue
            rec = next(r for r in fit_report if r["name"] == part["name"])
            rec.update({"cuts_m": [[round(float(v), 5) for v in c] for c in cuts[part["name"]]], "tris_trimmed": ml.tris_of(ob),
                        "tris_before_trim": tris0, "islands_dropped": islands, "trim_solver": solver})
    lap("trim", t0)

    # ------------------------------------------------------------------ the base where no part exists
    # a part without a mesh (not generated, failed, rejected): the base inside its box (planar caps at the
    # box faces; the neighbours' parts reach into that box, so the volumes overlap at the joints)
    t0 = time.time()
    remainder = {"parts": [], "tris": 0}
    pieces = []
    min_tris = int(p.get("gap_min_tris", 1500))
    if placed and p.get("fill_gaps", True) is not False:
        for part in missing:
            piece, nt, solver = pt.base_piece(base, boxes[part["name"]], cuts[part["name"]], "Base_" + part["name"], min_tris)
            remainder["parts"].append({"name": part["name"], "tris": nt, "solver": solver})
            remainder["tris"] += nt
            if piece is not None:
                pieces.append(piece)
        if not missing and len(parts) < len(pt.PART_NAMES):
            warnings.append("only %d of %d standard parts are listed: nothing fills the places of the others" % (len(parts), len(pt.PART_NAMES)))
    rest_ob = None
    if not placed:
        warnings.append("no parts with a mesh: the result is the voxel-remeshed base")
        rest_ob = ml.duplicate(base, "Base_copy")
    lap("remainder", t0)

    # ------------------------------------------------------------------ fuse
    t0 = time.time()
    fuse_objs = [ob for _p, ob, _w in placed] + pieces
    if rest_ob is not None:
        fuse_objs.append(rest_ob)
    asm = af.join(fuse_objs, name="Assembled")
    asm.data.name = "Assembled"
    tris_in = ml.tris_of(asm)
    vt = ml.voxel_remesh(asm, voxel)
    lap("voxel_remesh", t0)
    if vt == 0:
        raise RuntimeError("voxel remesh produced an empty mesh (voxel_m=%g too large?)" % voxel)
    imin = float(p.get("island_min", 0.002))
    islands_final = ml.remove_small_islands(asm, imin)[0] if imin > 0 else 0
    ml.triangulate_object(asm)
    ml.recalc_normals(asm)
    tris_voxel = ml.tris_of(asm)
    stages = []
    target = p.get("target_tris")
    if target:
        t0 = time.time()
        stages = ml.decimate_to(asm, int(target))
        ml.recalc_normals(asm)
        lap("decimate", t0)
    ml.apply_shading(asm, p.get("shade_auto_smooth_deg", 40))
    af.normalize([asm], height=height, ground=True, center=True)
    bpy.context.view_layer.update()
    metrics = ml.mesh_metrics([asm])
    # base for the previews and the metrics keeps its normalised frame; the assembled result moved by the re-normalisation
    Va, Ta = pt.world_arrays(asm)

    # ------------------------------------------------------------------ metrics
    t0 = time.time()
    cam_a, iou_asm = pt.fit_front_camera(Va, Ta, fit_mask)
    sil_b = pt.silhouette(Vb, Tb, cam, fit_mask.shape)
    sil_a = pt.silhouette(Va, Ta, cam, fit_mask.shape)
    n = min(sil_b.shape[0], sil_a.shape[0])
    iou_vs_base = pl.mask_iou(sil_a[:n, :n], sil_b[:n, :n])
    lap("metrics", t0)

    # ------------------------------------------------------------------ outputs
    outputs = {}
    t0 = time.time()
    outputs["glb"] = af.export_glb(af.out_path(req, "assembled.glb"), objects=[asm], animations=False, skins=False)
    mv = None
    if p.get("export_mv"):
        mv = pt.export_hunyuan(asm, af.out_path(req, "assembled_mv.glb"))
        outputs["glb_mv"] = af.out_path(req, "assembled_mv.glb")
    lap("export_glb", t0)
    fit_doc = {"format": "astraforge.parts_fit/1", "height_m": height, "voxel_m": voxel,
               "camera": dict(cam.to_dict(), iou=round(iou_base, 4)), "parts": fit_report, "joints": joints,
               "joint_overlap_m": eps, "remainder": remainder,
               "iou": {"base_vs_reference": round(iou_base, 4), "assembled_vs_reference": round(iou_asm, 4),
                       "assembled_vs_base": round(iou_vs_base, 4)}}
    with open(af.out_path(req, "parts_fit.json"), "w", encoding="utf-8") as fh:
        json.dump(fit_doc, fh, indent=1, ensure_ascii=False)
    outputs["parts_fit"] = af.out_path(req, "parts_fit.json")
    if p.get("preview", True):
        t0 = time.time()
        lo, hi = af.world_bbox([asm])
        head_box = None
        head = next((pl_ for pl_ in placed if pl_[0]["name"] == "head"), None)
        if head is not None and p.get("head_closeup", True):
            wb = head[2]
            head_box = (mathutils.Vector((wb[0], -0.16 * height / 1.8, wb[2])), mathutils.Vector((wb[1], 0.16 * height / 1.8, wb[3])))
        outputs["preview"] = _render_rows(af.out_path(req, "preview.png"), base, asm, lo, hi, head_box,
                                          int(p.get("preview_size", 384)), head_box is not None)
        lap("preview", t0)
    # the base stays in the .blend (hidden from render) so the result can be compared again
    base.hide_viewport = True
    base.hide_render = True
    asm.name = "Body"
    asm.data.name = "Body"
    ml.purge_orphans()
    outputs["blend"] = af.save_blend(af.out_path(req, "assembled.blend"))

    # ------------------------------------------------------------------ checks
    checks = [af.check("parts_found", bool(placed), len(placed), 1)]
    checks.append(af.check("no_nan_verts", metrics["nan_verts"] == 0, metrics["nan_verts"], 0))
    size_z = metrics["size"][2]
    checks.append(af.check("height_matches", abs(size_z - height) <= 1e-3 * height, round(size_z, 5), height))
    checks.append(af.check("grounded", abs(metrics["bbox_min"][2]) <= 1e-4 * max(size_z, 1e-6), metrics["bbox_min"][2], 0.0))
    checks.append(af.check("watertight", metrics["boundary_edges"] == 0, metrics["boundary_edges"], 0,
                           note="open edges of the fused mesh"))
    checks.append(af.check("manifold", metrics["non_manifold_edges"] == 0, metrics["non_manifold_edges"], 0,
                           severity="warning", note="edges shared by 3+ faces"))
    checks.append(af.check("silhouette_iou_min", iou_asm >= IOU_FAIL, round(iou_asm, 4), IOU_FAIL,
                           note="assembled front silhouette vs the front reference"))
    checks.append(af.check("silhouette_iou", iou_asm >= iou_base - 0.01, round(iou_asm, 4), round(iou_base - 0.01, 4),
                           severity="warning", note="not below the base mesh IoU by more than 0.01"))
    warn_m = float(p.get("icp_warn_m", 0.012))
    for rec in fit_report:
        if rec["residual_rms_m"] is not None:
            checks.append(af.check("part_residual_" + rec["name"], rec["residual_rms_m"] <= warn_m, rec["residual_rms_m"],
                                   warn_m, severity="warning", note="trimmed ICP RMS to the base, m"))
        if rec.get("flipped"):
            checks.append(af.check("part_flipped_" + rec["name"], False, True, False, severity="warning",
                                   note="the part was generated turned 180 deg about Z and was turned back"))

    timings["total"] = round(time.time() - t_all, 2)
    stats = {"camera": cam.to_dict(), "iou": fit_doc["iou"], "mask": mask_info, "parts": fit_report,
             "remainder": remainder, "voxel_m": voxel, "tris_before_voxel": tris_in, "tris_after_voxel": tris_voxel,
             "tris": metrics["tris"], "verts": metrics["verts"], "boundary_edges": metrics["boundary_edges"],
             "non_manifold_edges": metrics["non_manifold_edges"], "loose_parts": metrics["loose_parts"],
             "size": metrics["size"], "decimation": stages, "islands_dropped_final": islands_final, "height_m": round(size_z, 5), "base_tris": int(len(Tb)),
             "mv_export": mv, "input": src_path, "timings_s": timings}
    return {"outputs": outputs, "stats": stats, "checks": checks, "warnings": warnings}
