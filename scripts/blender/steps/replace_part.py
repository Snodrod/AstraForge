"""replace_part - swap a region of a mesh (e.g. a smoothed head) for a separately generated part.

Region-of-interest reconstruction: a detail that got too few pixels of the generator input in the
full-body pass (a skull, a hand, a staff head) is generated alone from a high-resolution crop and
put back in place of the old geometry.

inputs:
  mesh | blend    the body (.glb/.gltf/.fbx/.obj/.stl/.blend; all meshes are joined, a rig dropped)
  part            the new part (.glb/...), e.g. a Hunyuan3D result generated from a crop; it must
                  face the same way as the body in the generator frame (front = -Y)
params:
  height_m, rotate_z_deg, ground (true), center (true)
                  optional normalisation of the body BEFORE everything else; region / cut_plane
                  are in these final metres (contract §4)
  region          {"min": [x,y,z], "max": [x,y,z]} (required): box around the part to replace
  cut_plane       {"axis": "z", "value": 1.50} or {"point": [...], "normal": [...]}; the part is
                  on the +normal side ("side": "-" flips). Default: z plane through region.min.
                  Faces of the body on the +side that form pieces with >= min_inside (0.5) of
                  their area inside the box are removed (a slightly tight box still takes the
                  whole head; a collar poking into the box stays).
  align           'icp' (default) | 'bbox' | 'none' ('none': the part keeps its own transform -
                  after part_transform - no scaling to the removed region: a procedural part such as
                  a bone_kit hand is placed by its pose, not fitted to a fat old paddle)
                  'icp' / 'bbox' - initial similarity from robust extents of the upper
                  60 % of both surfaces (top + width/depth, no rotation), then symmetric trimmed
                  ICP with a uniform scale between the part and the removed surface;
                  icp_rotation (false: the crop and the body share the camera, so the part
                  keeps its generator orientation; true lets ICP rotate it towards the old
                  surface), icp_iters (60), icp_trim (0.85), samples (6000),
                  max_rotation_deg (30), max_rms_rel (0.04 of the region diagonal, warning)
  part_transform  {"location": [x,y,z], "rot_deg": [x,y,z] | "matrix": 3x3 rows, "scale": s}: pose of
                  the part applied first (e.g. a bone_kit hand: wrist at the origin -> at the wrist)
  keep_materials  false (default: without a texture the part gets one flat part_fill material);
                  true keeps the part's own materials (gold rings, vertex-coloured aged bone); UV
                  name and colour attributes are matched with the body (white where missing)
  cut_part        true (default): the part is trimmed at the cut plane + seam_gap and bridged to the
                  body; false: the part stays whole and the body opening on the plane is capped (a
                  hand of separate bones has no neck to bridge)
  part_tris       triangle budget of the part (default 20000; 0 = keep), part_min_island (0.0005
                  of the part's vertices: crumbs are deleted before decimation), crumb_fraction
                  (0.002: the same after the cut), part_rotate_z_deg
  hang_m          body pieces under the cut plane that touch it only where the removed part did
                  and reach at most this far down are removed too (the old jaw dipping under a
                  neck cut); default 0.25 of the region height
  seam_gap        the part is cut this far above the cut plane (default 0.02 of the region
                  height). Its pieces below that cut whose section lies inside a body loop (its
                  own neck stub, with any base plate) are dropped, others hanging at most
                  keep_hang (default = hang_m) stay (the new jaw). The body's and the part's
                  biggest open loops are bridged with a short band of faces; extra loops capped
  texture         {"image": path, "crop_px": [l,t,r,b] (optional), "mode": "front_projection",
                   "facing_min": 0.12, "fade": 0.05, "occlusion": false}: orthographic projection
                  of the crop along +Y in the part's generator frame (the crop's camera), the crop
                  registered by the subject silhouette (alpha, or a border-colour key for JPG).
                  Faces facing the camera (occlusion true: and seeing it unobstructed) get UVs into
                  the crop, slot part_front_projection; the rest gets slot part_back_fill with the
                  median colour of the subject's outer band, into which the photo fades over the
                  outer `fade` of the subject width (those pixels land on grazing faces)
  eyes            {"auto": true, "color": [0.25, 1.0, 0.35] (linear), "strength": 6,
                   "radius_factor": 0.22, "remove_inner_islands": true, "snap_to_glow": true}:
                  the deepest pair of side-by-side front concavities in the upper part of the new
                  part (raycast depth grid, camera yaw searched in -45..45, holes measured as
                  closing(height) - height) get emissive spheres (slot eye_glow, radius
                  radius_factor x socket radius) and sockets socket_fx_eye_l / socket_fx_eye_r
                  (+Z outward = perpendicular to the eye line, contract §6). Small loose islands
                  inside the sockets (generated eyeballs) are deleted before the projection, so
                  the sockets show their dark interior. With a texture, each sphere moves onto the
                  green glow spot of the crop inside its socket; depth from the detection
  name            name of the result object (default "replaced")
  preview         true: compare.png (reference | old front | new front | new 3/4 | new side,
                  textured and untextured rows), preview_size (384)

outputs: replaced.glb (one mesh + eye sockets), replaced.blend, part_aligned.glb (the new part
alone, aligned and cut), part_projection.png, compare.png, compare_views.png (old vs new 3/4 and
profile)
stats: tris_before / tris_after, removed region, alignment (bbox and final point-to-surface RMS,
scale, rotation), seam (bridged loops, caps, open edges), texture registration, eyes (socket depth
at the eyes, new vs old surface), compare (front silhouette IoU / colour MAE vs the reference crop,
old vs new head, rows above the cut). See docs/forge/replace_part.md.
"""
import math
import os
import time

import bpy
import mathutils
import numpy as np

import af_common as af
from steps import _mesh_lib as ml
from steps import _replace_lib as rl


def _import_meshes(path, name, warnings, sockets=None):
    objs, helpers = ml.purge_import_helpers(af.import_any(path))
    objs = ml.prepare_meshes(objs, warnings, sockets=sockets)
    if not objs:
        raise ValueError("no mesh objects in %s" % path)
    ob = af.join(objs, name=name)
    ob.data.name = name
    return ob, helpers


def _part_input(req):
    part = (req.get("inputs") or {}).get("part")
    if not part:
        raise ValueError("inputs.part is required (the new part mesh)")
    part = os.path.abspath(str(part))
    if not os.path.isfile(part):
        raise FileNotFoundError("inputs.part not found: %s" % part)
    return part


def _part_transform(spec):
    """params.part_transform {"location": [x,y,z], "rot_deg": [x,y,z] (Euler XYZ) | "matrix": 3x3 rows,
    "scale": s} -> 4x4 applied to the part before anything else (align 'none' places a procedural
    part, e.g. a bone_kit hand with its wrist at the origin, by this pose). None -> None."""
    if not spec:
        return None
    loc = mathutils.Vector([float(v) for v in spec.get("location", (0, 0, 0))])
    if spec.get("matrix") is not None:
        R = mathutils.Matrix([[float(v) for v in row] for row in spec["matrix"]])
        if R.determinant() < 1e-6:
            raise ValueError("part_transform.matrix must be a proper rotation")
        R = R.normalized()
    else:
        r = [math.radians(float(v)) for v in spec.get("rot_deg", (0, 0, 0))]
        R = mathutils.Euler(r, "XYZ").to_matrix()
    s = float(spec.get("scale", 1.0))
    if s <= 0:
        raise ValueError("part_transform.scale must be > 0")
    return mathutils.Matrix.Translation(loc) @ R.to_4x4() @ mathutils.Matrix.Scale(s, 4)


def _flat_material(name, color_lin):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = ml.principled_of(mat)
    bsdf.inputs["Base Color"].default_value = tuple(float(c) for c in color_lin) + (1.0,)
    bsdf.inputs["Roughness"].default_value = 0.75
    mat.diffuse_color = tuple(float(c) for c in color_lin) + (1.0,)
    return mat


def _snap_eyes_to_glow(det, tex_stats, mapping, sim, part):
    """Move each detected eye (in the reference camera plane x/z) onto the glowing spot of the
    reference crop that lies inside its socket; depth stays from the socket detection but never
    behind the socket's back wall. Exact for icp_rotation false (rotation is ignored)."""
    blobs = tex_stats.get("glow_blobs_px") or []
    if not blobs:
        return
    a, bx, bz = mapping
    s, _R, t = sim
    bvh, _co, _tri = rl.bvh_of(part)
    for k in ("left", "right"):
        e = det[k]
        best = None
        for col, row, _n in blobs:
            x = s * (col - bx) / a + t[0]
            z = s * (bz - row) / a + t[2]
            d = math.hypot(x - e["position"][0], z - e["position"][2])
            if d <= 1.5 * e["socket_radius_m"] and (best is None or d < best[0]):
                best = (d, x, z)
        if best is None:
            continue
        _d, x, z = best
        y = float(e["position"][1])
        hit = bvh.ray_cast(mathutils.Vector((x, y - 1.0, z)), mathutils.Vector((0, 1, 0)), 3.0)
        if hit[0] is not None:
            y = min(y, float(hit[0][1]) - 1.1 * e["radius"])
        new = np.array([x, y, z])
        e["snapped_mm"] = round(1000 * float(np.linalg.norm(new - e["position"])), 2)
        e["position"] = new


def run(req):
    p = req["params"]
    warnings, checks = [], []
    timings = {}
    t_all = time.time()
    body_path = ml.input_path(req)
    part_path = _part_input(req)
    region = p.get("region")
    if not region or "min" not in region or "max" not in region:
        raise ValueError("params.region {min: [x,y,z], max: [x,y,z]} is required")
    lo, hi = rl.vec3(region["min"], "region.min"), rl.vec3(region["max"], "region.max")
    if np.any(hi <= lo):
        raise ValueError("region.max must be greater than region.min on every axis")
    align = str(p.get("align", "icp")).lower()
    if align not in ("icp", "bbox", "none"):
        raise ValueError("params.align must be 'icp', 'bbox' or 'none'")
    keep_materials = bool(p.get("keep_materials", False))
    cut_part = bool(p.get("cut_part", True))
    part_T = _part_transform(p.get("part_transform"))
    tex = p.get("texture") or None
    if tex and str(tex.get("mode", "front_projection")) != "front_projection":
        raise ValueError("texture.mode must be 'front_projection'")
    if tex:
        img_path = os.path.abspath(str(tex.get("image") or ""))
        if not os.path.isfile(img_path):
            raise FileNotFoundError("texture.image not found: %s" % img_path)
    name = ml.safe_name(p.get("name") or "replaced", "replaced")
    eyes_cfg = p.get("eyes") or {}
    region_h = float(hi[2] - lo[2])

    # ---------------------------------------------------------------- body
    t0 = time.time()
    af.reset_scene()
    body_socks = []
    body, helpers = _import_meshes(body_path, "body", warnings, sockets=body_socks)
    kept = ml.restore_sockets(body_socks)   # sockets of the body survive (they follow the normalisation)
    ml.rotate_z([body] + kept, float(p.get("rotate_z_deg") or 0.0))
    af.normalize([body] + kept, height=p.get("height_m"), ground=p.get("ground", True),
                 center=p.get("center", True))
    ml.sockets_unit_scale(kept)
    blo, bhi = af.world_bbox([body])
    h_body = float(bhi.z - blo.z) or 1.0
    merge = p.get("merge_distance")
    merge = 1e-5 * h_body if merge is None else float(merge)
    welded_body = 0
    if merge > 0:  # glTF splits vertices along UV seams; weld so the neck cut gives closed loops
        ml.clear_custom_normals(body)
        welded_body = ml.merge_by_distance(body, merge)
        ml.smooth_by_angle(body, float(p.get("shade_auto_smooth_deg", 40)))
    uv_name = body.data.uv_layers.active.name if body.data.uv_layers.active else "UVMap"
    before = ml.mesh_metrics([body])
    boundary_before = rl.boundary_edge_count(body)
    original = ml.duplicate(body, "body_original")
    plane = rl.plane_from_params(p.get("cut_plane"), lo, hi)
    hang = p.get("hang_m")
    hang = 0.25 * region_h if hang is None else float(hang)
    old, rinfo = rl.remove_region(body, lo, hi, plane, min_inside=float(p.get("min_inside", 0.5)),
                                  max_hang=hang)
    checks.append(af.check("region_removed", old is not None, rinfo["removed_faces"], ">0"))
    if old is None:
        raise ValueError("nothing of the body lies in params.region on the part side of the cut "
                         "plane: %s" % rinfo)
    # sockets of the removed region (e.g. the old head's eyes) go with it
    dropped_socks = []
    for e in list(kept):
        c = np.array(e.matrix_world.translation)
        if np.all(c >= lo) and np.all(c <= hi) and float((c - plane[0]) @ plane[1]) >= 0:
            dropped_socks.append(e.name)
            kept.remove(e)
            bpy.data.objects.remove(e, do_unlink=True)
    rinfo["sockets_dropped"] = dropped_socks
    body_loops = rl.plane_loops(body, plane)
    rinfo["body_open_loops"] = [{"edges": L["edges"], "radius_m": round(L["radius"], 4)}
                                for L in body_loops]
    timings["body"] = round(time.time() - t0, 2)

    # ---------------------------------------------------------------- part
    t0 = time.time()
    part_socks = []
    part, _ = _import_meshes(part_path, "part", warnings, sockets=part_socks)
    ml.rotate_z([part], float(p.get("part_rotate_z_deg") or 0.0))
    if part_T is not None:
        part.matrix_world = part_T @ part.matrix_world
    af.apply_transforms([part])
    raw = ml.mesh_metrics([part])
    plo, phi = af.world_bbox([part])
    ml.clear_custom_normals(part)
    welded = ml.merge_by_distance(part, 1e-6 * max((phi - plo).length, 1e-6))
    islands = ml.remove_small_islands(part, float(p.get("part_min_island", 0.0005)))
    budget = int(p.get("part_tris", 20000))
    # working resolution: fine enough for alignment and the cut, the final budget is spent
    # after the neck stub / base plate are gone
    work_tris = max(4 * budget, 100000) if budget > 0 else 0
    dec_log = ml.decimate_to(part, work_tris) if work_tris > 0 else []
    ml.triangulate_object(part)
    inner_removed = 0
    if eyes_cfg.get("auto") and eyes_cfg.get("remove_inner_islands", True):
        det0 = rl.detect_eye_sockets(part)
        inner_removed = rl.remove_inner_islands(part, det0)
    work_metrics = ml.mesh_metrics([part])
    checks.append(af.check("part_nonempty", work_metrics["tris"] > 0, work_metrics["tris"], ">0"))
    timings["part_prep"] = round(time.time() - t0, 2)

    # ---------------------------------------------------------------- alignment
    t0 = time.time()
    ns = int(p.get("samples", 6000))
    tgt = rl.sample_surface(old, ns, seed=1)
    src = rl.sample_surface(part, ns, seed=2)
    diag = float(np.linalg.norm(hi - lo))
    if align == "none":
        # the part's own transform (after part_transform): no similarity at all
        init = (1.0, np.eye(3), np.zeros(3))
        binfo = {"mode": "none"}
    else:
        init, binfo = rl.bbox_init(src, tgt)
    rms_init = rl.surface_rms(part, old, src, tgt, init, plane)[0]
    sim, ainfo = init, {}
    if align == "icp":
        sim_icp, ainfo = rl.icp_similarity(
            src, tgt, init, plane=plane, iters=int(p.get("icp_iters", 60)),
            trim=float(p.get("icp_trim", 0.85)), with_rotation=bool(p.get("icp_rotation", False)))
        rot = rl.rotation_deg(sim_icp[1])
        ratio = sim_icp[0] / init[0]
        sane = rot <= float(p.get("max_rotation_deg", 30)) and 0.75 <= ratio <= 1.33
        checks.append(af.check("align_icp_sane", sane, {"rotation_deg": round(rot, 2),
                                                         "scale_vs_bbox": round(ratio, 4)},
                               {"rotation_deg": p.get("max_rotation_deg", 30),
                                "scale_vs_bbox": [0.75, 1.33]}, severity="warning",
                               note="ICP result rejected, bbox alignment kept" if not sane else None))
        if sane:
            sim = sim_icp
        else:
            warnings.append("ICP ran away (rotation %.1f deg, scale x%.3f); kept the bbox alignment"
                            % (rot, ratio))
    rms_final, rms_fwd, rms_bwd = rl.surface_rms(part, old, src, tgt, sim, plane)
    max_rel = float(p.get("max_rms_rel", 0.04))
    if align != "none":   # with a placed part the old surface is no target (a thin bone hand vs a paddle)
        checks.append(af.check("align_rms", rms_final / diag <= max_rel, round(rms_final / diag, 5),
                               max_rel, severity="warning",
                               note="symmetric point-to-surface RMS / region diagonal"))
    rl.transform_mesh(part, sim)
    # the part's own sockets (e.g. socket_mount_hand_l of a bone_kit hand) ride with it
    Mp = rl.sim_matrix(sim) @ (part_T if part_T is not None else mathutils.Matrix.Identity(4)) @         mathutils.Matrix.Rotation(math.radians(float(p.get("part_rotate_z_deg") or 0.0)), 4, "Z")
    names_taken = {e.name for e in kept}
    part_kept = ml.restore_sockets([r for r in part_socks if r["name"] not in names_taken], transform=Mp)
    align_stats = {"mode": align, "init": binfo, "rms_init_m": round(rms_init, 5),
                   "rms_m": round(rms_final, 5), "rms_rel": round(rms_final / diag, 5),
                   "rms_surface_forward_m": round(rms_fwd, 5),
                   "rms_surface_backward_m": round(rms_bwd, 5),
                   "scale": round(sim[0], 6), "rotation_deg": round(rl.rotation_deg(sim[1]), 3),
                   "translation_m": [round(float(x), 5) for x in sim[2]], "samples": ns,
                   "with_rotation": bool(p.get("icp_rotation", False)) and align == "icp"}
    # the ICP's own numbers are point-to-sample (they carry the sample-spacing floor)
    align_stats.update({("icp_" + k if k.startswith("rms") else k):
                        (round(v, 6) if isinstance(v, float) else v) for k, v in ainfo.items()})
    timings["align"] = round(time.time() - t0, 2)

    # ---------------------------------------------------------------- cut + final budget
    t0 = time.time()
    gap = p.get("seam_gap")
    gap = 0.02 * region_h if gap is None else float(gap)
    keep_hang = hang if p.get("keep_hang") is None else float(p.get("keep_hang"))
    if cut_part:
        trim = rl.trim_by_plane(part, plane, offset=gap, keep_hang=keep_hang, body_loops=body_loops)
    else:   # the part is whole (a procedural hand): nothing is cut, the body opening gets a cap
        trim = {"cut": False}
    crumbs = ml.remove_small_islands(part, float(p.get("crumb_fraction", 0.002))) if cut_part else ()
    trim["crumbs_removed"] = list(crumbs)
    trim["tris_after_cut"] = ml.tris_of(part)
    if budget > 0 and ml.tris_of(part) > budget:
        dec_log += ml.decimate_to(part, budget)
        ml.triangulate_object(part)
        if cut_part:
            trim["snapped_boundary_verts"] = rl.snap_boundary_to_plane(part, plane, gap,
                                                                       max_dist=0.05 * region_h)
    part_metrics = ml.mesh_metrics([part])
    timings["cut"] = round(time.time() - t0, 2)

    # ---------------------------------------------------------------- texture (generator frame)
    # The part was generated from the crop, so in its own frame the camera of the crop looks
    # along +Y: project there (undo the alignment for the projection; UVs travel with the mesh).
    t0 = time.time()
    tex_stats, mapping, ref_px, ref_mask = None, None, None, None
    if tex:
        rl.transform_mesh(part, sim, inverse=True)
        try:
            tex_stats, mapping, ref_px, ref_mask = rl.project_texture(
                part, img_path, crop_px=tex.get("crop_px"),
                out_png=af.out_path(req, "part_projection.png"), uv_name=uv_name,
                facing_min=float(tex.get("facing_min", 0.12)), name="part",
                fade_frac=float(tex.get("fade", 0.05)),
                occlusion=bool(tex.get("occlusion", False)))
        finally:
            rl.transform_mesh(part, sim)
        checks.append(af.check("texture_registration_iou", tex_stats["silhouette_iou"] >= 0.8,
                               tex_stats["silhouette_iou"], 0.8, severity="warning",
                               note="front silhouette of the cut part vs the crop subject"))
    elif keep_materials and any(m is not None for m in part.data.materials):
        # the part's own materials stay (bone_kit: Bone_aged with vertex colours, Gold); UV layer name
        # and colour attributes are matched with the body (white where missing) before the join
        tex_stats = {"mode": "keep_materials",
                     "materials": [m.name for m in part.data.materials if m is not None],
                     "colour_attributes_completed": ml.harmonise_attributes(body, [part])}
    else:
        part.data.materials.clear()
        part.data.materials.append(_flat_material("part_fill", (0.55, 0.5, 0.42)))
        if not part.data.uv_layers:
            part.data.uv_layers.new(name=uv_name)
        else:
            part.data.uv_layers.active.name = uv_name
    timings["texture"] = round(time.time() - t0, 2)

    # ---------------------------------------------------------------- eyes
    t0 = time.time()
    eyes_stats, old_eyes = None, None
    spheres, sockets = [], []
    if eyes_cfg.get("auto"):
        det = rl.detect_eye_sockets(part, radius_factor=float(eyes_cfg.get("radius_factor", 0.22)))
        checks.append(af.check("eyes_found", det.get("found", False), det.get("found", False),
                               True, severity="warning"))
        if det.get("found") and mapping is not None and eyes_cfg.get("snap_to_glow", True):
            _snap_eyes_to_glow(det, tex_stats, mapping, sim, part)
        if det.get("found"):
            color = [float(c) for c in eyes_cfg.get("color", [0.25, 1.0, 0.35])]
            mat = rl.emissive_material("eye_glow", color, float(eyes_cfg.get("strength", 6.0)))
            rot = rl.socket_rotation(det["outward"])
            eyes_stats = {"grid_yaw_deg": det["yaw_deg"], "score": det["score"],
                          "facing_yaw_deg": round(det["facing_yaw_deg"], 2),
                          "outward": [round(x, 4) for x in det["outward"]], "tried": det["tried"],
                          "inner_islands_removed": inner_removed}
            for side in ("l", "r"):
                e = det["left" if side == "l" else "right"]
                spheres.append(rl.make_sphere("eye_glow_%s" % side, e["position"], e["radius"],
                                              mat, uv_name=uv_name))
                s = af.add_socket("socket_fx_eye_%s" % side, location=tuple(e["position"]),
                                  rotation=rot, kind="fx", size=max(0.01, 2 * e["radius"]))
                sockets.append(s)
                eyes_stats[side] = {"position": [round(float(x), 5) for x in e["position"]],
                                    "snapped_to_glow_mm": e.get("snapped_mm"),
                                    "radius_m": round(e["radius"], 5),
                                    "socket_radius_m": round(e["socket_radius_m"], 5),
                                    "socket_depth_mm": round(1000 * e["depth_m"], 2),
                                    "rim_to_bottom_mm": round(1000 * e["rim_to_bottom_m"], 2)}
        else:
            eyes_stats = {"found": False, "tried": det.get("tried")}
            warnings.append("eye sockets not found on the new part")
        # socket depth at the new eye positions, old surface vs new part (same rays)
        if det.get("found"):
            sd_cmp = {}
            for side, k in (("l", "left"), ("r", "right")):
                e = det[k]
                dn = rl.socket_depth_at(part, e["position"], e["socket_radius_m"], det["outward"])
                do = rl.socket_depth_at(old, e["position"], e["socket_radius_m"], det["outward"])
                sd_cmp[side] = {"new_mm": None if dn is None else round(1000 * dn, 2),
                                "old_mm": None if do is None else round(1000 * do, 2)}
            eyes_stats["socket_depth_at_eyes"] = sd_cmp
        # the old surface's own detection, for the report
        try:
            od = rl.detect_eye_sockets(old)
            if od.get("found"):
                old_eyes = {s: {"socket_depth_mm": round(1000 * od[k]["depth_m"], 2),
                                "rim_to_bottom_mm": round(1000 * od[k]["rim_to_bottom_m"], 2),
                                "socket_radius_m": round(od[k]["socket_radius_m"], 5)}
                            for s, k in (("l", "left"), ("r", "right"))}
                old_eyes["facing_yaw_deg"] = round(od["facing_yaw_deg"], 2)
            else:
                old_eyes = {"found": False}
        except Exception as exc:  # noqa: BLE001 - report only
            old_eyes = {"error": str(exc)}
    timings["eyes"] = round(time.time() - t0, 2)

    outputs = {}
    outputs["part_glb"] = af.export_glb(af.out_path(req, "part_aligned.glb"),
                                        objects=[part] + spheres, animations=False, skins=False)

    # ---------------------------------------------------------------- join + seam
    t0 = time.time()
    part_lo, part_hi = rl.used_bbox(part)
    joined = af.join([body, part] + spheres, name=name)
    joined.data.name = name
    seam = rl.bridge_seam(joined, plane, gap)
    ml.triangulate_object(joined)
    boundary_after = rl.boundary_edge_count(joined)
    seam["boundary_edges_before"] = boundary_before
    seam["boundary_edges_after"] = boundary_after
    checks.append(af.check("seam_closed", seam["open_seam_edges"] == 0 and
                           boundary_after <= boundary_before, {"open_seam_edges":
                           seam["open_seam_edges"], "new_boundary_edges":
                           boundary_after - boundary_before}, 0, severity="warning",
                           note="open edges on the cut planes / boundary edges added"))
    sockets = sockets + [e for e in kept + part_kept if e.name not in {s.name for s in sockets}]
    for s in sockets:
        s.parent = joined
        s.matrix_parent_inverse = joined.matrix_world.inverted()
    after = ml.mesh_metrics([joined])
    nan = after["nan_verts"]
    checks.append(af.check("no_nan_verts", nan == 0, nan, 0))
    timings["join"] = round(time.time() - t0, 2)

    # ---------------------------------------------------------------- compare renders
    compare = None
    if p.get("preview", True):
        t0 = time.time()
        size = int(p.get("preview_size", 384))
        flo = np.minimum(lo, np.array(part_lo))
        fhi = np.maximum(hi, np.array(part_hi))
        flo[2] = max(flo[2], float(plane[0][2]) - 0.15 * float(fhi[2] - flo[2]))
        margin = 0.78
        bbox = (mathutils.Vector(flo.tolist()), mathutils.Vector(fhi.tolist()))
        front = (0.0, -1.0, 0.0)
        # three-quarter and profile relative to where the new face looks (eye line), on the side
        # turned away from the reference camera; geometry outside the head box is clipped
        out_dir = np.array(eyes_stats["outward"]) if eyes_stats and "outward" in eyes_stats \
            else np.array([0.0, -1.0, 0.0])
        c1, c2 = rl.rot_z(out_dir, 45), rl.rot_z(out_dir, -45)
        sgn = 1 if np.dot(c1, front) < np.dot(c2, front) else -1
        tq = rl.rot_z(out_dir, 45 * sgn) + np.array([0, 0, 0.2])
        side = rl.rot_z(out_dir, -90 * sgn)
        clip = 0.75 * float(np.linalg.norm(fhi - flo))
        dirs = [front, tq, side]
        old_t = rl.render_dirs(af.out_path(req, "_old.png"), [original], dirs, size, "TEXTURE",
                               bbox, margin, clip=clip)
        new_t = rl.render_dirs(af.out_path(req, "_new.png"), [joined], dirs, size, "TEXTURE",
                               bbox, margin, clip=clip)
        old_s = rl.render_dirs(af.out_path(req, "_olds.png"), [original], dirs, size, "SINGLE",
                               bbox, margin, clip=clip)
        new_s = rl.render_dirs(af.out_path(req, "_news.png"), [joined], dirs, size, "SINGLE",
                               bbox, margin, clip=clip)
        blank = np.zeros((size, size, 4), dtype=np.float32)
        compare = {"views": {"front": list(front), "three_quarter": [round(float(x), 3) for x in tq],
                             "side": [round(float(x), 3) for x in side]}}
        if mapping is not None:
            a_, bx_, bz_ = mapping
            s_, _R, t_ = sim   # rotation ignored: exact for icp_rotation false
            wmap = (a_ / s_, bx_ - a_ * t_[0] / s_, bz_ + a_ * t_[2] / s_)
            ref = rl.reference_in_frame(ref_px, ref_mask, wmap, flo, fhi, size, margin)
            rows = rl.frame_rows_above(flo, fhi, size, margin, float(plane[0][2]))
            compare["old_front_vs_reference"] = rl.tile_compare(ref, old_t[0], rows)
            compare["new_front_vs_reference"] = rl.tile_compare(ref, new_t[0], rows)
        else:
            ref = blank
        sheet = rl.grid_sheet([[ref, old_t[0], new_t[0], new_t[1], new_t[2]],
                               [blank, old_s[0], new_s[0], new_s[1], new_s[2]]])
        outputs["compare"] = ml.save_png(af.out_path(req, "compare.png"), sheet)
        sheet2 = rl.grid_sheet([[old_t[1], new_t[1], old_t[2], new_t[2]],
                                [old_s[1], new_s[1], old_s[2], new_s[2]]])
        outputs["compare_views"] = ml.save_png(af.out_path(req, "compare_views.png"), sheet2)
        timings["preview"] = round(time.time() - t0, 2)

    # ---------------------------------------------------------------- export
    t0 = time.time()
    for ob in (original, old):
        ml.remove_object(ob)
    outputs["glb"] = af.export_glb(af.out_path(req, "replaced.glb"), objects=[joined] + sockets,
                                   animations=False, skins=False)
    ml.purge_orphans()
    outputs["blend"] = af.save_blend(af.out_path(req, "replaced.blend"))
    timings["export"] = round(time.time() - t0, 2)
    timings["total"] = round(time.time() - t_all, 2)

    stats = {
        "input": body_path, "part_input": part_path,
        "tris_before": before["tris"], "tris_after": after["tris"],
        "body_welded_verts": welded_body, "merge_distance_m": merge,
        "body_before": before, "result": after,
        "region": {"min": lo.tolist(), "max": hi.tolist(),
                   "cut_plane": {"point": plane[0].tolist(), "normal": plane[1].tolist()},
                   **rinfo},
        "part": {"raw": {k: raw[k] for k in ("tris", "verts", "loose_parts", "bbox_min",
                                              "bbox_max")},
                 "welded_verts": welded, "islands_removed": list(islands),
                 "work_tris": work_metrics["tris"], "decimate": dec_log,
                 "tris": part_metrics["tris"], "loose_parts": part_metrics["loose_parts"],
                 "inner_islands_removed": inner_removed, "trim": trim, "seam_gap_m": gap,
                 "keep_hang_m": keep_hang, "hang_m": hang},
        "alignment": align_stats, "seam": seam, "texture": tex_stats, "eyes": eyes_stats,
        "old_eyes": old_eyes, "compare": compare, "materials": [m.name for m in
                                                                joined.data.materials if m],
        "sockets": [s.name for s in sockets], "import_helpers_removed": helpers,
        "timings_s": timings,
    }
    return {"outputs": outputs, "stats": stats, "checks": checks, "warnings": warnings}
