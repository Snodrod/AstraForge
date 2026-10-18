"""separate — split one mesh into named parts.

inputs.mesh or inputs.blend: .glb/.gltf/.fbx/.obj/.stl/.blend (all meshes are joined first; a rig
is dropped, importer helpers such as the glTF bone-shape Icosphere are deleted).

params:
  mode            'loose' | 'material' | 'regions' (default 'loose')
  height_m, rotate_z_deg, ground, center
                  optional normalisation BEFORE splitting (ground/center default true), so region
                  boxes are given in the final metres (contract §4: Z up, feet on z=0, X/Y centred)
  merge_distance  weld before splitting (default 1e-5 * height; 0 = off) — glTF meshes are split
                  along UV seams and would otherwise fall apart into UV islands in 'loose' mode
  loose:    min_fraction (default 0.01 of all vertices): smaller pieces are joined into one part
            "small" (or dropped with drop_small=true); names: optional list, biggest first
  material: one part per material, named after the material
  regions:  regions: [{"name": "staff", "min": [x,y,z], "max": [x,y,z]}, ...] in metres.
            The mesh is bisected along the box planes (only near the box), faces inside go to the
            part, and the cut openings on both sides are filled (caps take the rim UVs and
            material), so a fused prop becomes its own object. It is watertight only where the
            source was: source holes / non-manifold edges inside the box stay. Several boxes
            with the same name (and kind) are one region (their union), e.g. a staff above and
            below a fist. rest_name (default "body") names what is left.
            Per region:
              kind   "part" (default: the faces become a part) | "delete" (the faces are removed,
                     no part: an old prop, a hidden cap) | "slit" (open cut: the mesh is ripped
                     along a plane inside the box, nothing removed, nothing capped - a sleeve fused
                     to the robe side deforms on its own; see plane / gap_m)
              caps   (part / delete; default params.caps = true) false leaves the cut openings
                     open on both sides (an open hem) instead of capping them
              facing [x, y, z] + facing_min (0.5): only faces whose normal . facing >= facing_min
                     count as inside (e.g. the down-facing bottom of a robe: [0, 0, -1])
              plane  (slit) {"axis": "x", "value": -0.22} or {"point": [x,y,z], "normal": [x,y,z]};
                     default: x = box centre. The cut runs only inside the box and ends at its
                     sides (the mesh stays joined beyond)
              curve  (slit, instead of plane) {"axis": "x", "along": "z", "points": [[z0, x0],
                     [z1, x1], ...]}: the cut surface x = f(z), piecewise linear, extruded along
                     the third axis - follows a crease that bends
              width_m (slit) 0 (default): a rip - nothing removed, coincident open rims;
                     > 0: a slab of this width around the surface is removed, which also cuts a
                     fused web whose pinch lines wander a few mm off the surface; caps (default
                     true) then closes each side with its own flat cap (no hole shows, the sides
                     stay apart)
              gap_m  (rip) the two rims are pulled apart by this (default params.slit_gap_m =
                     0.001 x height), so welds of later steps cannot sew the slit shut
  slit:     same as regions, kind defaults to "slit"
  caps      default true (regions: cut openings capped)
  origin          'world' (default: parts keep their place and reassemble) | 'center' | 'bottom'
  preview         default true (assembled in random colours | exploded, textured), preview_size

Socket empties of the input (socket_* / game_socket, e.g. socket_fx_eye_l/r) are kept in place
(they follow the normalisation) and exported with the parts.
outputs: glb (parts.glb, one named object per part), blend (parts.blend), preview.
stats.parts: per part tris / verts / bbox / loose parts / open edges / caps / volume;
stats.regions: per region (part / delete_<name> / slit_<name>) faces, caps, ripped edges, rim length.
"""
import os
import time

import bpy
import mathutils

import af_common as af
from steps import _mesh_lib as ml


def _part_stats(ob):
    m = ml.mesh_metrics([ob])
    item = {"name": ob.name, "tris": m["tris"], "verts": m["verts"], "bbox_min": m["bbox_min"],
            "bbox_max": m["bbox_max"], "size": m["size"], "loose_parts": m["loose_parts"],
            "boundary_edges": m["boundary_edges"], "non_manifold_edges": m["non_manifold_edges"],
            "materials": [x.name for x in ob.data.materials if x is not None]}
    item["closed"] = m["boundary_edges"] == 0 and m["non_manifold_edges"] == 0
    item["volume_m3"] = round(ml.signed_volume(ob), 6) if item["closed"] else None
    return item


def _separate_op(ob, kind):
    af.select_only([ob])
    with bpy.context.temp_override(object=ob, active_object=ob, selected_objects=[ob],
                                   selected_editable_objects=[ob]):
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.mesh.separate(type=kind)
        bpy.ops.object.mode_set(mode="OBJECT")
    return [o for o in bpy.context.scene.objects if o.type == "MESH"]


def _split_loose(ob, p, warnings):
    total = len(ob.data.vertices)
    parts = _separate_op(ob, "LOOSE")
    parts.sort(key=lambda o: len(o.data.vertices), reverse=True)
    frac = float(p.get("min_fraction", 0.01))
    big = [o for o in parts if len(o.data.vertices) >= frac * total]
    small = [o for o in parts if o not in big]
    names = list(p.get("names") or [])
    out = []
    for i, o in enumerate(big):
        o.name = ml.safe_name(names[i] if i < len(names) else "part_%02d" % i)
        out.append(o)
    if small:
        if p.get("drop_small"):
            for o in small:
                ml.remove_object(o)
            warnings.append("dropped %d small pieces (< %.3g of the vertices)" % (len(small), frac))
        else:
            joined = af.join(small, name="small")
            out.append(joined)
    return out


def _split_material(ob):
    parts = _separate_op(ob, "MATERIAL")
    used = set()
    for o in parts:
        mats = [m for m in o.data.materials if m is not None]
        idx = o.data.polygons[0].material_index if len(o.data.polygons) else 0
        mat = o.data.materials[idx] if idx < len(o.data.materials) else None
        base = ml.safe_name(mat.name if mat else "no_material", "material")
        name, k = base, 1
        while name in used:
            k += 1
            name = "%s_%d" % (base, k)
        used.add(name)
        o.name = name
        if mats and mat is not None:  # keep only the material the part actually uses
            o.data.materials.clear()
            o.data.materials.append(mat)
            for poly in o.data.polygons:
                poly.material_index = 0
    return parts


KINDS = ("part", "delete", "slit")


def _parse_regions(p, h):
    """params.regions -> ordered groups {name: {kind, caps, facing, facing_min, boxes, slits}}.
    Entries with the same name and kind are one region (union of boxes); every slit entry keeps
    its own plane."""
    regions = p.get("regions") or []
    if not regions:
        raise ValueError("mode '%s' needs params.regions: [{name, min, max}]" % p.get("mode"))
    default_kind = "slit" if p.get("mode") == "slit" else "part"
    default_caps = bool(p.get("caps", True))
    gap_default = p.get("slit_gap_m")
    gap_default = 0.001 * h if gap_default is None else float(gap_default)
    groups, order = {}, []
    for i, r in enumerate(regions):
        kind = str(r.get("kind", default_kind)).lower()
        if kind not in KINDS:
            raise ValueError("region %d: kind must be one of %s" % (i, ", ".join(KINDS)))
        name = ml.safe_name(r.get("name") or "%s_%d" % (kind, i))
        lo = [float(v) for v in r["min"]]
        hi = [float(v) for v in r["max"]]
        if any(hi[k] <= lo[k] for k in range(3)):
            raise ValueError("region %s: max must be greater than min on every axis" % name)
        key = (name, kind)
        if key not in groups:
            groups[key] = {"name": name, "kind": kind, "boxes": [], "slits": [],
                           "caps": bool(r.get("caps", default_caps)),
                           "facing": r.get("facing"), "facing_min": float(r.get("facing_min", 0.5))}
            order.append(key)
        g = groups[key]
        if kind == "slit":
            surf = ml.SlitSurface(r.get("curve") or r.get("plane"), [(lo, hi)])
            width = float(r.get("width_m", 0.0))
            if width < 0:
                raise ValueError("region %s: width_m must be >= 0" % name)
            g["slits"].append({"box": (lo, hi), "surface": surf, "width_m": width,
                               "gap_m": float(r.get("gap_m", gap_default if width <= 0 else 0.0)),
                               "caps": bool(r.get("caps", True))})
        else:
            g["boxes"].append((lo, hi))
    return [groups[k] for k in order]


def _split_regions(ob, p, warnings, checks, info_out, h=1.0):
    groups = _parse_regions(p, h)
    rest_name = ml.safe_name(p.get("rest_name") or "body")
    parts = []
    for g in groups:
        name, kind = g["name"], g["kind"]
        tag = name if kind == "part" else "%s_%s" % (kind, name)
        if kind == "slit":
            per = []
            for s in g["slits"]:
                info = ml.slit(ob, [s["box"]], s["surface"], width=s["width_m"], gap=s["gap_m"],
                               caps=s["caps"])
                info.update(box=[list(s["box"][0]), list(s["box"][1])])
                per.append(info)
            ripped = sum(i["cut"] for i in per)
            info_out[tag] = {"kind": "slit", "slits": per, "cut": ripped,
                             "rim_length_m": round(sum(i["rim_length_m"] for i in per), 4)}
            checks.append(af.check("slit_%s_cut" % name, ripped > 0, ripped, ">0",
                                   note="edges ripped (width 0) or faces removed (slab) inside the box"))
            open_rims = sum((i.get("caps") or {}).get("open_rim_edges", 0) for i in per
                            if i.get("width_m", 0) > 0 and (i.get("caps") or {}).get("chains"))
            if any(i.get("width_m", 0) > 0 and i.get("caps") for i in per):
                checks.append(af.check("slit_%s_capped" % name, open_rims <= 8, open_rims, 8, severity="warning",
                                       note="rim edges left open after the side caps (the two ends of a slab "
                                            "stay open by design)"))
            if not ripped:
                warnings.append("slit %s: the plane crossed no faces inside its box" % name)
            continue
        part, info = ml.cut_by_boxes(ob, g["boxes"], name, caps=g["caps"], facing=g["facing"],
                                     facing_min=g["facing_min"], make_part=(kind == "part"))
        info["boxes"] = g["boxes"]
        info["kind"] = kind
        if g["facing"] is not None:
            info["facing"] = [float(v) for v in g["facing"]]
            info["facing_min"] = g["facing_min"]
        info_out[tag] = info
        if kind == "delete":
            checks.append(af.check("delete_%s_nonempty" % name, info["faces_inside"] > 0,
                                   info["faces_inside"], ">0", note="faces deleted"))
            if not info["faces_inside"]:
                warnings.append("delete region %s caught no faces" % name)
            elif g["caps"]:
                checks.append(af.check("delete_%s_capped" % name, info["rest_open_seam_edges"] == 0,
                                       info["rest_open_seam_edges"], 0, severity="warning",
                                       note="cut edges left open (the source had a hole on the cut)"))
            continue
        checks.append(af.check("region_%s_nonempty" % name, part is not None,
                               info["faces_inside"], ">0"))
        if part is None:
            warnings.append("region %s caught no faces" % name)
            continue
        ml.triangulate_object(part)
        if g["caps"]:
            checks.append(af.check("region_%s_capped" % name, info["open_seam_edges"] == 0,
                                   info["open_seam_edges"], 0, severity="warning",
                                   note="cut edges left open (the source had a hole on the cut)"))
        parts.append(part)
    ml.triangulate_object(ob)
    ob.name = rest_name
    ob.data.name = rest_name
    if len(ob.data.polygons):
        parts.append(ob)
    else:
        warnings.append("nothing left outside the regions")
        ml.remove_object(ob)
    return parts


def _set_origin(ob, mode):
    if mode not in ("center", "bottom"):
        return
    lo, hi = af.world_bbox([ob])
    pivot = (lo + hi) / 2
    if mode == "bottom":
        pivot.z = lo.z
    ob.data.transform(mathutils.Matrix.Translation(-pivot))
    ob.location = ob.location + pivot


def run(req):
    p = req["params"]
    warnings, checks = [], []
    timings = {}
    t_all = time.time()
    mode = p.get("mode", "loose")
    if mode not in ("loose", "material", "regions", "slit"):
        raise ValueError("params.mode must be loose, material, regions or slit")

    src_path = ml.input_path(req)
    af.reset_scene()
    objs, helpers = ml.purge_import_helpers(af.import_any(src_path))
    sock_recs = []
    objs = ml.prepare_meshes(objs, warnings, sockets=sock_recs)
    if not objs:
        raise ValueError("no mesh objects in %s" % src_path)
    source = ml.mesh_metrics(objs)
    ob = af.join(objs, name="source")
    sockets = ml.restore_sockets(sock_recs)
    ml.rotate_z([ob] + sockets, float(p.get("rotate_z_deg") or 0.0))
    af.normalize([ob] + sockets, height=p.get("height_m"), ground=p.get("ground", True),
                 center=p.get("center", True))
    ml.sockets_unit_scale(sockets)
    lo, hi = af.world_bbox([ob])
    h = float(hi.z - lo.z) or 1.0
    merge = p.get("merge_distance")
    merge = 1e-5 * h if merge is None else float(merge)
    merged = 0
    if merge > 0:
        ml.clear_custom_normals(ob)
        merged = ml.merge_by_distance(ob, merge)
        ml.smooth_by_angle(ob, float(p.get("shade_auto_smooth_deg", 40)))
    t0 = time.time()
    region_info = {}
    if mode == "loose":
        parts = _split_loose(ob, p, warnings)
    elif mode == "material":
        parts = _split_material(ob)
    else:
        parts = _split_regions(ob, p, warnings, checks, region_info, h)
    timings["split"] = round(time.time() - t0, 2)
    for o in parts:
        o.data.name = o.name
        _set_origin(o, p.get("origin", "world"))

    part_stats = [_part_stats(o) for o in parts]
    checks.append(af.check("parts_created", len(parts) >= 1, len(parts), ">=1"))
    total_tris = sum(s["tris"] for s in part_stats)
    nan = sum(ml.mesh_metrics([o])["nan_verts"] for o in parts)
    checks.append(af.check("no_nan_verts", nan == 0, nan, 0))

    outputs = {}
    t0 = time.time()
    outputs["glb"] = af.export_glb(af.out_path(req, "parts.glb"), objects=parts + sockets,
                                   animations=False, skins=False)
    timings["export_glb"] = round(time.time() - t0, 2)
    if p.get("preview", True):
        t0 = time.time()
        size = int(p.get("preview_size", 512))
        views = ("front", "three_quarter")
        _, a = ml.render_sheet(af.out_path(req, "_assembled.png"), parts, views=views, size=size,
                               color_type="RANDOM")
        lo_all, hi_all = af.world_bbox(parts)
        centre = (lo_all + hi_all) / 2
        spread = float(p.get("explode", 0.35)) * (hi_all - lo_all).length
        saved = []
        for o in parts:
            c = (af.world_bbox([o])[0] + af.world_bbox([o])[1]) / 2
            d = mathutils.Vector((c.x - centre.x, c.y - centre.y, 0.0))
            if d.length < 1e-6:
                d = mathutils.Vector((0, 0, 0))
            else:
                d = d.normalized() * spread * min(1.0, d.length / max((hi_all - lo_all).x, 1e-6) * 4)
            saved.append((o, o.location.copy()))
            o.location = o.location + d
        bpy.context.view_layer.update()
        _, b = ml.render_sheet(af.out_path(req, "_exploded.png"), parts, views=views, size=size,
                               color_type="AUTO")
        for o, loc in saved:
            o.location = loc
        bpy.context.view_layer.update()
        outputs["preview"] = ml.side_by_side(af.out_path(req, "preview.png"),
                                             [ml.np.concatenate(a, axis=1),
                                              ml.np.concatenate(b, axis=1)])
        for tmp in ("_assembled.png", "_exploded.png"):
            try:
                os.remove(af.out_path(req, tmp))
            except OSError:
                pass
        timings["preview"] = round(time.time() - t0, 2)
    orphans = ml.purge_orphans()
    outputs["blend"] = af.save_blend(af.out_path(req, "parts.blend"))
    timings["total"] = round(time.time() - t_all, 2)
    stats = {"mode": mode, "source": source, "merged_verts": merged, "merge_distance_m": merge,
             "parts": part_stats, "part_count": len(parts), "tris_total": total_tris,
             "regions": region_info, "import_helpers_removed": helpers,
             "sockets_kept": [e.name for e in sockets],
             "orphans_purged": orphans, "input": src_path, "timings_s": timings}
    return {"outputs": outputs, "stats": stats, "checks": checks, "warnings": warnings}
