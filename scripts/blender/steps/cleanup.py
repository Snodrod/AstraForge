"""cleanup — turn an AI / high-poly mesh into a clean working mesh (contract §3-4).

inputs.mesh or inputs.blend: .glb/.gltf/.fbx/.obj/.stl/.blend (static meshes; a rig in the input
is dropped, importer helpers such as the glTF bone-shape Icosphere are deleted).

params (all optional):
  height_m               scale so the bbox height equals it (omit = keep the stored size)
  rotate_z_deg           rotation about Z before anything else (e.g. -90 turns +X-facing to -Y)
  ground, center         lowest point to z=0 / bbox centre to X=Y=0 (default true / true)
  join, name             join all meshes into one object called name (default true, "Body")
  merge_distance         weld distance in metres (default 1e-5 * height; 0 = off)
  remove_islands_below   delete loose pieces with fewer than this fraction of all vertices (0 = off)
  target_tris            decimate to <= this many triangles (planar pass + staged collapse;
                         UVs / seams / material borders are kept by Blender's collapse decimator).
                         With join:false the budget is split in proportion to each object's
                         triangles (after the remesh when remeshing); objects keep at least
                         min(own tris, 64), so low-poly pieces are left whole
  symmetry               'X' | 'Y' | 'Z' | null — symmetric collapse
  planar_angle_deg       planar pre-pass angle (default 0.5; 0 = off), only with target_tris
  decimate_stage_ratio   minimum kept share per collapse stage (default 0.3)
  seam_weight            0..1 extra seam protection through a vertex group (default off; see
                         _mesh_lib.decimate_to for why)
  voxel_remesh_m         voxel size in metres (0 = off; ~0.0033 * height recommended, thin
                         parts tear above ~0.004 * height). The remeshed mesh gets a fresh Smart UV
                         and its colour is BAKED from the original with Cycles selected-to-active
                         whenever the original has a colour source (base colour map, any node
                         into Base Color such as a colour attribute, several flat materials, or
                         vertex colours without a material). Photo projection stays a projection:
                         this is a re-bake of the existing colour, not PBR material recovery
  bake_size              baked map size in px (default 2048)
  bake_device            'CPU' (default) | 'GPU'
  bake_normal            also bake a tangent-space normal map from the original (default false);
                         the remeshed mesh is then shaded fully smooth (the map carries the
                         creases) and shade_auto_smooth_deg is not applied to it
  bake_emission          also bake emission (default false)
  bake_samples           Cycles samples per texel (default 4)
  bake_extrusion_m       cage extrusion (default max(2 * voxel, 0.005 * height))
  bake_ray_m             max ray distance (default 2 * extrusion)
  smart_uv_angle_deg     Smart UV angle limit (default 66)
  shade_auto_smooth_deg  smooth shading with sharp edges above this angle (default 40;
                         0 = flat shading; null = leave as imported)
  recalc_normals         make face winding consistent / outward (default true). When remeshing,
                         normals and shading are fixed BEFORE the bake so the shipped mesh shades
                         exactly like the mesh the maps were baked on
  preview                before|after sheet (default true), preview_size (px per view, 512)

outputs: glb (clean.glb), blend (clean.blend), preview (preview.png), baked_* PNGs when remeshing.
stats: before / welded / after mesh metrics, timings, decimation stages, textures, render_compare.
checks (error): tris_within_target, no_nan_verts, height_matches, grounded,
  base_color_texture_kept / uv_kept (no remesh), silhouette_iou_min (IoU >= 0.85);
  (warning): centered, bake_coverage, silhouette_iou (>= 0.97), render_color_mae (<= 0.08).
"""
import os
import time

import af_common as af
from steps import _mesh_lib as ml

SILHOUETTE_WARN = 0.97
SILHOUETTE_FAIL = 0.85
MIN_PART_TRIS = 64


def run(req):
    p = req["params"]
    warnings = []
    timings = {}
    t_all = time.time()

    def lap(name, t0):
        timings[name] = round(time.time() - t0, 2)
        af.log(name, seconds=timings[name])

    # ---------------------------------------------------------------- import & normalise
    src_path = ml.input_path(req)
    t0 = time.time()
    af.reset_scene()
    objs = af.import_any(src_path)
    objs, helpers = ml.purge_import_helpers(objs)
    sock_recs = []
    objs = ml.prepare_meshes(objs, warnings, sockets=sock_recs)
    if not objs:
        raise ValueError("no mesh objects in %s" % src_path)
    sockets = ml.restore_sockets(sock_recs)   # socket_* empties of the input (contract §6) survive
    lap("import", t0)
    t0 = time.time()
    before = ml.mesh_metrics(objs)
    tex_before = ml.texture_report(objs)
    lap("metrics_before", t0)

    name = p.get("name") or "Body"
    if p.get("join", True):
        objs = [af.join(objs, name=name)]
        objs[0].data.name = name
    height = p.get("height_m")
    ml.rotate_z(objs + sockets, float(p.get("rotate_z_deg") or 0.0))
    af.normalize(objs + sockets, height=height, ground=p.get("ground", True), center=p.get("center", True))
    ml.sockets_unit_scale(sockets)
    lo, hi = af.world_bbox(objs)
    h = float(hi.z - lo.z) or 1.0

    # "before" render: the input as it came (normalised, original normals), shared framing
    sheet_before = None
    frame = (lo.copy(), hi.copy())
    psize = int(p.get("preview_size", 512))
    views = ("front", "three_quarter")
    if p.get("preview", True):
        t0 = time.time()
        _, sheet_before = ml.render_sheet(af.out_path(req, "_before.png"), objs, views=views,
                                          size=psize, bbox=frame)
        lap("render_before", t0)

    # ---------------------------------------------------------------- weld & islands
    t0 = time.time()
    merge = p.get("merge_distance")
    merge = 1e-5 * h if merge is None else float(merge)
    merged = wire = lone = 0
    for ob in objs:
        ml.clear_custom_normals(ob)
        if merge > 0:
            merged += ml.merge_by_distance(ob, merge)
        w, l = ml.delete_loose_geometry(ob)
        wire += w
        lone += l
    islands_removed = islands_verts = 0
    frac = float(p.get("remove_islands_below") or 0.0)
    if frac > 0:
        for ob in objs:
            n_parts, n_verts = ml.remove_small_islands(ob, frac)
            islands_removed += n_parts
            islands_verts += n_verts
    lap("weld", t0)
    welded = ml.mesh_metrics(objs)

    # ---------------------------------------------------------------- remesh or decimate
    target = p.get("target_tris")
    target = int(target) if target else None
    voxel = float(p.get("voxel_remesh_m") or 0.0)
    symmetry = p.get("symmetry") or None
    recalc = bool(p.get("recalc_normals", True))
    smooth = p.get("shade_auto_smooth_deg", 40)
    stage_ratio = float(p.get("decimate_stage_ratio", 0.3))
    stages = []
    budgets = None
    bake_info = None
    bake_runs = []
    shading = None
    flipped = 0
    outputs = {}
    if voxel > 0:
        if voxel > 0.004 * h:
            warnings.append("voxel_remesh_m=%.4g is %.2f%% of the height: thin parts (fingers, cloth "
                            "edges) tear into holes above ~0.4%%; ~%.4g (0.33%%) is safer, then "
                            "decimate" % (voxel, 100 * voxel / h, 0.0033 * h))
        bake_size = int(p.get("bake_size", 2048))
        extrusion = float(p.get("bake_extrusion_m") or max(2 * voxel, 0.005 * h))
        ray = float(p.get("bake_ray_m") or 2 * extrusion)
        bake_normal = bool(p.get("bake_normal"))
        maps = ["base_color"]
        if bake_normal:
            maps.append("normal")
        if p.get("bake_emission"):
            maps.append("emission")
        # 1) remesh every object
        lows = []
        for i, src in enumerate(objs):
            t0 = time.time()
            low = ml.duplicate(src, src.name + "_remesh")
            vt = ml.voxel_remesh(low, voxel)
            lap("voxel_remesh_%d" % i, t0)
            if vt == 0:
                raise RuntimeError("voxel remesh produced an empty mesh (voxel_remesh_m=%g is too "
                                   "large for this model?)" % voxel)
            if frac > 0:
                ml.remove_small_islands(low, frac)
            lows.append(low)
        # 2) split the budget over the REMESHED triangle counts (they no longer relate to the
        #    source counts) and decimate
        if target:
            budgets = ml.allocate_budget([ml.tris_of(l) for l in lows], target, MIN_PART_TRIS)
            for i, (low, share) in enumerate(zip(lows, budgets)):
                t0 = time.time()
                stages += ml.decimate_to(low, share, symmetry=symmetry, stage_ratio=stage_ratio)
                lap("decimate_%d" % i, t0)
        # 3) final topology, normals and shading BEFORE the bake, then UVs and bake
        shading = "smooth (bake_normal)" if bake_normal else smooth
        new_objs = []
        for i, (src, low) in enumerate(zip(objs, lows)):
            ml.triangulate_object(low)  # bake on the triangles the GLB will carry
            if recalc:
                flipped += ml.recalc_normals(low)
            if bake_normal:
                ml.shade_smooth(low)
            else:
                ml.apply_shading(low, smooth)
            t0 = time.time()
            ml.smart_uv(low, angle_deg=float(p.get("smart_uv_angle_deg", 66.0)))
            lap("smart_uv_%d" % i, t0)
            reason = ml.color_source(src)
            prefix = "baked" if len(objs) == 1 else "baked_%d" % i
            if reason:
                if reason == "attribute":
                    ml.attribute_color_material(src)
                t0 = time.time()
                imgs, info = ml.bake_maps(
                    [src], low, size=bake_size, device=p.get("bake_device", "CPU"), maps=maps,
                    extrusion=extrusion, ray_distance=ray, samples=int(p.get("bake_samples", 4)),
                    out_dir=req["out_dir"], prefix=prefix)
                lap("bake_%d" % i, t0)
                info["miss_fraction"] = round(ml.image_coverage(imgs["base_color"], low) or 0.0, 4)
                info.update(object=src.name, colour_source=reason, size=bake_size,
                            extrusion_m=round(extrusion, 5), ray_m=round(ray, 5), maps=maps)
                bake_runs.append(info)
                for k in maps:
                    outputs[prefix + "_" + k] = os.path.join(req["out_dir"],
                                                             "%s_%s.png" % (prefix, k))
            else:
                bake_runs.append({"object": src.name, "colour_source": None,
                                  "note": "single flat material or none: nothing to bake, the "
                                          "material is kept"})
        for src, low in zip(objs, lows):  # sources go only after every bake is done
            src_name = src.name
            ml.remove_object(src)
            low.name = src_name
            low.data.name = src_name
            new_objs.append(low)
        objs = new_objs
        baked = [b for b in bake_runs if b.get("colour_source")]
        if baked:
            bake_info = dict(baked[0])
            bake_info["miss_fraction"] = max(b["miss_fraction"] for b in baked)
            if len(bake_runs) > 1:
                bake_info["objects"] = bake_runs
    elif target:
        t0 = time.time()
        budgets = ml.allocate_budget([ml.tris_of(o) for o in objs], target, MIN_PART_TRIS)
        for ob, share in zip(objs, budgets):
            stages += ml.decimate_to(ob, share, symmetry=symmetry,
                                     planar_deg=float(p.get("planar_angle_deg", 0.5)),
                                     seam_weight=p.get("seam_weight"), stage_ratio=stage_ratio)
        lap("decimate", t0)

    # ---------------------------------------------------------------- normals & shading
    t0 = time.time()
    if voxel <= 0:  # remeshed objects got theirs before the bake
        shading = smooth
        for ob in objs:
            if recalc:
                flipped += ml.recalc_normals(ob)
            ml.apply_shading(ob, smooth)
    # remesh / decimation / island removal move the bbox: restore the contract placement and
    # height, and move the comparison frame with the model so before/after stay aligned
    _, frame_after = ml.normalize_tracked(objs + sockets, frame, height=height,
                                          ground=p.get("ground", True),
                                          center=p.get("center", True))
    ml.sockets_unit_scale(sockets)
    lap("normals", t0)
    symmetry_pairs = None
    if symmetry:
        symmetry_pairs = round(sum(ml.mirror_pair_fraction(o, symmetry) for o in objs) / len(objs), 4)

    after = ml.mesh_metrics(objs)
    tex_after = ml.texture_report(objs)

    # ---------------------------------------------------------------- outputs
    t0 = time.time()
    outputs["glb"] = af.export_glb(af.out_path(req, "clean.glb"), objects=objs + sockets,
                                   animations=False, skins=False)
    lap("export_glb", t0)
    compare = None
    if p.get("preview", True):
        t0 = time.time()
        _, sheet_after = ml.render_sheet(af.out_path(req, "_after.png"), objs, views=views,
                                         size=psize, bbox=frame_after)
        compare = ml.render_compare(sheet_before, sheet_after)
        outputs["preview"] = ml.side_by_side(af.out_path(req, "preview.png"),
                                             [ml.np.concatenate(sheet_before, axis=1),
                                              ml.np.concatenate(sheet_after, axis=1)])
        for tmp in ("_before.png", "_after.png"):
            try:
                os.remove(af.out_path(req, tmp))
            except OSError:
                pass
        lap("render_after", t0)
    t0 = time.time()
    orphans = ml.purge_orphans()  # e.g. the original 4K maps after a remesh + bake
    outputs["blend"] = af.save_blend(af.out_path(req, "clean.blend"))
    lap("save_blend", t0)

    # ---------------------------------------------------------------- checks
    checks = []
    if target:
        checks.append(af.check("tris_within_target", after["tris"] <= target, after["tris"],
                               target))
    checks.append(af.check("no_nan_verts", after["nan_verts"] == 0, after["nan_verts"], 0))
    size_z = after["size"][2]
    if height:
        checks.append(af.check("height_matches", abs(size_z - float(height)) <= 1e-3 * float(height),
                               round(size_z, 5), float(height)))
    if p.get("ground", True):
        checks.append(af.check("grounded", abs(after["bbox_min"][2]) <= 1e-4 * max(size_z, 1e-6),
                               after["bbox_min"][2], 0.0))
    if p.get("center", True):
        cx = (after["bbox_min"][0] + after["bbox_max"][0]) / 2
        cy = (after["bbox_min"][1] + after["bbox_max"][1]) / 2
        checks.append(af.check("centered", max(abs(cx), abs(cy)) <= 1e-3 * max(size_z, 1e-6),
                               [round(cx, 6), round(cy, 6)], 0.0, severity="warning"))
    in_colour = {k: v["base_color"] for k, v in tex_before.items() if "base_color" in v}
    out_colour = [v["base_color"] for v in tex_after.values() if "base_color" in v]
    if voxel <= 0:
        if in_colour:
            biggest = max(max(s) for s in in_colour.values())
            kept = bool(out_colour) and max(max(s) for s in out_colour) >= biggest
            checks.append(af.check("base_color_texture_kept", kept,
                                   out_colour[0] if out_colour else None, biggest))
        if before["uv_layers"]:
            checks.append(af.check("uv_kept", after["uv_layers"] >= 1, after["uv_layers"], 1))
    elif bake_info:
        checks.append(af.check("bake_coverage", bake_info["miss_fraction"] <= 0.02,
                               bake_info["miss_fraction"], 0.02, severity="warning",
                               note="share of sampled texels left black (ray missed)"))
    if compare and compare["silhouette_iou"] is not None:
        checks.append(af.check("silhouette_iou", compare["silhouette_iou"] >= SILHOUETTE_WARN,
                               compare["silhouette_iou"], SILHOUETTE_WARN, severity="warning",
                               note="before/after alpha IoU, front + 3/4 views"))
        checks.append(af.check("silhouette_iou_min", compare["silhouette_iou"] >= SILHOUETTE_FAIL,
                               compare["silhouette_iou"], SILHOUETTE_FAIL,
                               note="the shape changed too much (collapsed or torn parts)"))
    if compare and compare["color_mae"] is not None:
        checks.append(af.check("render_color_mae", compare["color_mae"] <= 0.08,
                               compare["color_mae"], 0.08, severity="warning",
                               note="before/after mean |RGB| on covered pixels (Workbench)"))
    if welded["non_manifold_edges"] > before["non_manifold_edges"]:
        warnings.append("welding joined touching sheets: non-manifold edges %d -> %d (3+ faces on "
                        "one edge; usually AI cloth layers touching)" % (
                            before["non_manifold_edges"], welded["non_manifold_edges"]))
    if voxel <= 0 and after["non_manifold_edges"] > welded["non_manifold_edges"]:
        warnings.append("decimation added non-manifold edges: %d -> %d" % (
            welded["non_manifold_edges"], after["non_manifold_edges"]))

    timings["total"] = round(time.time() - t_all, 2)
    stats = {
        "before": before, "welded": welded, "after": after,
        "merge_distance_m": merge, "merged_verts": merged, "wire_edges_removed": wire,
        "lone_verts_removed": lone, "islands_removed": islands_removed,
        "island_verts_removed": islands_verts, "flipped_faces": flipped,
        "decimation": stages, "budgets": budgets, "textures_before": tex_before,
        "textures_after": tex_after, "render_compare": compare, "bake": bake_info,
        "shading": shading, "sharp_edges": sum(ml.sharp_edge_count(o) for o in objs),
        "import_helpers_removed": helpers, "orphans_purged": orphans, "timings_s": timings,
        "sockets_kept": [e.name for e in sockets],
        "tris": after["tris"], "height_m": round(size_z, 5), "input": src_path,
    }
    if symmetry_pairs is not None:
        stats["symmetry_pair_fraction"] = symmetry_pairs
        if symmetry_pairs < 0.5:
            warnings.append("only %.0f%% of vertices have a mirror twin: symmetric decimation "
                            "had little to pair" % (100 * symmetry_pairs))
    return {"outputs": outputs, "stats": stats, "checks": checks, "warnings": warnings}
