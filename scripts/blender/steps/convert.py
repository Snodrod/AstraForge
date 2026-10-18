"""convert — any supported input to GLB / FBX / OBJ, optionally a Mixamo-ready FBX.

inputs.mesh or inputs.blend: .glb/.gltf/.fbx/.obj/.stl/.blend. A rig and its animation are kept
(except for OBJ and Mixamo); importer helpers such as the glTF bone-shape Icosphere are deleted.

params:
  format          'glb' (default) | 'fbx' | 'obj'
  name            output basename (default "model")
  height_m        scale so the bbox height equals it (omit = keep)
  rotate_z_deg    rotation about Z
  ground, center  default true when height_m is given or mixamo is on, false otherwise
                  (a plain conversion keeps the original placement)
  mixamo          true -> Mixamo-upload FBX (forces format 'fbx'), ported from an
                  earlier in-house script: one welded mesh "Body" without rig, facing -Y
                  (FBX +Z), feet at z=0, height default 1.8 m, FBX units cm (FBX_SCALE_ALL),
                  textures embedded and limited in size, file kept under the 10 MB browser
                  upload limit when possible
  mixamo_max_tris decimation budget for the Mixamo mesh (default 50000)
  mixamo_weld_m   weld distance (default 1e-4; Mixamo's auto-skinner fails on UV-split meshes)
  mixamo_center   'bbox' (default) | 'head' — X centre on the head column, for props on one side
  texture_max     max texture side for the Mixamo FBX (default 2048, halves if the file is
                  over 10 MB, down to 512)

outputs: glb|fbx|obj, blend (converted.blend), textures (FBX/OBJ: the maps written next to it).
checks: round-trip re-import of the written file (triangles, mesh count, height, base colour,
armature); with height_m also height_matches (round trip vs params.height_m, 1e-3 relative)
and grounded (round-trip z_min = 0) when grounding is on.
"""
import math
import os

import bpy

import af_common as af
from steps import _mesh_lib as ml

MIXAMO_UPLOAD_LIMIT = 10_000_000


def _head_center_x(ob):
    """Ported from an earlier in-house script (--center head): the most populated
    contiguous run of 2 cm X bins in the 75-86 % height band is the head; its centre becomes
    X = 0 so a staff or shield on one side does not shift Mixamo's symmetry line."""
    co = ml.mesh_arrays(ob.data)["co"]
    z0, h = float(co[:, 2].min()), float(co[:, 2].max() - co[:, 2].min())
    band = co[(co[:, 2] > z0 + 0.75 * h) & (co[:, 2] < z0 + 0.86 * h), 0]
    if not len(band):
        return None
    step = 0.02 * max(h / 1.8, 1e-3)
    bins = {}
    for x in band.tolist():
        k = int(math.floor(x / step))
        bins[k] = bins.get(k, 0) + 1
    runs, cur = [], []
    for b in sorted(bins):
        if cur and b != cur[-1] + 1:
            runs.append(cur)
            cur = []
        cur.append(b)
    runs.append(cur)
    best = max(runs, key=lambda r: sum(bins[b] for b in r))
    return (best[0] + best[-1] + 1) * step / 2


def _images_in_use(objs):
    imgs = []
    for o in af.meshes(objs):
        for m in o.data.materials:
            if m is None or m.node_tree is None:
                continue
            for n in m.node_tree.nodes:
                if n.type == "TEX_IMAGE" and n.image is not None and n.image not in imgs:
                    if n.image.size[0] > 0:
                        imgs.append(n.image)
    return imgs


def _export_textures(objs, tex_dir):
    """Write every used map next to the output and rebind the nodes to those files: the OBJ
    exporter references them, and the FBX exporter only embeds images that have a file path
    (glTF imports keep textures packed with an empty path)."""
    os.makedirs(tex_dir, exist_ok=True)
    out, remap = [], {}
    for img in _images_in_use(objs):
        ext = ".jpg" if img.file_format == "JPEG" else ".png"
        path = os.path.join(tex_dir, ml.safe_name(img.name, "texture") + ext)
        if img.file_format not in ("JPEG", "PNG"):
            img.file_format = "PNG"
        img.save(filepath=path, save_copy=True)
        new = bpy.data.images.load(path, check_existing=False)
        new.colorspace_settings.name = img.colorspace_settings.name
        new.alpha_mode = img.alpha_mode
        remap[img] = new
        out.append(path)
    for o in af.meshes(objs):
        for m in o.data.materials:
            if m is None or m.node_tree is None:
                continue
            for n in m.node_tree.nodes:
                if n.type == "TEX_IMAGE" and n.image in remap:
                    n.image = remap[n.image]
    return out


def _mixamo_prepare(objs, p, warnings, stats):
    objs = ml.prepare_meshes(objs, warnings)
    if not objs:
        raise ValueError("no meshes to send to Mixamo")
    body = af.join(objs, name="Body")
    body.data.name = "Body"
    ml.rotate_z([body], float(p.get("rotate_z_deg") or 0.0))
    af.normalize([body], height=float(p.get("height_m") or 1.8), ground=True, center=True)
    shift = 0.0
    if p.get("mixamo_center", "bbox") == "head":
        cx = _head_center_x(body)
        if cx is not None:
            shift = cx
            for v in body.data.vertices:
                v.co.x -= cx
            body.data.update()
    stats["center_shift_m"] = round(shift, 4)
    ml.clear_custom_normals(body)
    weld = float(p.get("mixamo_weld_m", 1e-4))
    stats["welded_verts"] = ml.merge_by_distance(body, weld) if weld > 0 else 0
    limit = int(p.get("mixamo_max_tris", 50000))
    if ml.tris_of(body) > limit:
        stats["decimation"] = ml.decimate_to(body, limit)
        # decimation trims the extremes a little: restore height and feet on z=0
        lo, hi = af.world_bbox([body])
        k = float(p.get("height_m") or 1.8) / max(hi.z - lo.z, 1e-6)
        for v in body.data.vertices:
            v.co.x *= k
            v.co.y *= k
            v.co.z = (v.co.z - lo.z) * k
        body.data.update()
    ml.smooth_by_angle(body, 40)
    return [body]


def _base_colour_only(objs):
    """Mixamo only previews the colour map; drop the other maps to keep the upload small."""
    for o in af.meshes(objs):
        for m in o.data.materials:
            if m is None or m.node_tree is None:
                continue
            keep = ml.image_into(m, "Base Color")
            for n in list(m.node_tree.nodes):
                if n.type == "TEX_IMAGE" and n != keep:
                    m.node_tree.nodes.remove(n)


def _limit_textures(objs, size):
    changed = []
    for img in _images_in_use(objs):
        w, h = img.size
        if w > size or h > size:
            k = size / float(max(w, h))
            img.scale(max(1, int(w * k)), max(1, int(h * k)))
            changed.append(img.name)
    return changed


def run(req):
    p = req["params"]
    warnings = []
    stats = {}
    mixamo = bool(p.get("mixamo"))
    fmt = "fbx" if mixamo else str(p.get("format", "glb")).lower().lstrip(".")
    if fmt not in ("glb", "fbx", "obj"):
        raise ValueError("params.format must be glb, fbx or obj, got %r" % fmt)
    name = ml.safe_name(p.get("name") or "model", "model")

    src_path = ml.input_path(req)
    af.reset_scene()
    objs = af.import_any(src_path)
    objs, stats["import_helpers_removed"] = ml.purge_import_helpers(objs)
    if not ml.model_meshes(objs):
        raise ValueError("no mesh objects in %s" % src_path)
    stats["input"] = ml.mesh_metrics(objs)
    stats["input"]["armatures"] = len(af.armatures(objs))
    stats["input"]["actions"] = len(bpy.data.actions)
    tex_in = ml.texture_report(objs)

    if mixamo:
        objs = _mixamo_prepare(objs, p, warnings, stats)
    else:
        default_place = p.get("height_m") is not None
        ml.rotate_z(objs, float(p.get("rotate_z_deg") or 0.0))
        if (p.get("height_m") or p.get("ground", default_place) or p.get("center", default_place)):
            af.normalize(objs, height=p.get("height_m"), ground=p.get("ground", default_place),
                         center=p.get("center", default_place))
        if fmt == "obj" and (af.armatures(objs) or bpy.data.actions):
            warnings.append("OBJ keeps no rig or animation; only the meshes were written")

    out = af.out_path(req, "%s.%s" % (name, fmt))
    outputs = {}
    has_anim = bool(af.armatures(objs)) or bool(bpy.data.actions)
    if fmt == "glb":
        outputs["glb"] = af.export_glb(out, objects=None, animations=has_anim, skins=has_anim)
    elif fmt == "fbx":
        tex_dir = af.out_path(req, "textures")
        if mixamo:
            _base_colour_only(objs)
            size = int(p.get("texture_max", 2048))
            kw = dict(object_types={"MESH"}, apply_scale_options="FBX_SCALE_ALL",
                      axis_forward="-Z", axis_up="Y", mesh_smooth_type="FACE", bake_anim=False)
            while True:
                stats["textures_scaled"] = _limit_textures(objs, size)
                outputs["textures"] = _export_textures(objs, tex_dir)
                af.export_fbx(out, objects=objs, mixamo=True, **kw)
                if os.path.getsize(out) <= MIXAMO_UPLOAD_LIMIT or size <= 512:
                    break
                size //= 2
            stats["texture_max"] = size
        else:
            outputs["textures"] = _export_textures(objs, tex_dir)
            af.export_fbx(out, objects=None, bake_anim=has_anim)
        outputs["fbx"] = os.path.abspath(out)
    else:
        outputs["textures"] = _export_textures(objs, af.out_path(req, "textures"))
        af.select_only(af.meshes(objs))
        bpy.ops.wm.obj_export(filepath=out, export_selected_objects=True, apply_modifiers=True,
                              export_materials=True, export_uv=True, export_normals=True,
                              path_mode="RELATIVE", forward_axis="NEGATIVE_Z", up_axis="Y")
        outputs["obj"] = os.path.abspath(out)
    stats["output"] = ml.mesh_metrics(list(bpy.context.scene.objects))
    stats["bytes"] = os.path.getsize(out)
    stats["orphans_purged"] = ml.purge_orphans()  # originals replaced by the written maps
    outputs["blend"] = af.save_blend(af.out_path(req, "converted.blend"))

    # ---------------------------------------------------------------- round trip
    want_tris = stats["output"]["tris"]
    want_h = stats["output"]["size"][2]
    want_meshes = stats["output"]["objects"]
    af.reset_scene()
    back = af.import_any(out)
    # the glTF importer adds its bone-shape helper again on re-import; anything else extra
    # (e.g. a helper that slipped into the written file) is counted below
    back, _ = ml.purge_import_helpers(back)
    got = ml.mesh_metrics(back)
    tex_back = ml.texture_report(back)
    stats["roundtrip"] = {"tris": got["tris"], "size": got["size"], "bbox_min": got["bbox_min"],
                          "meshes": got["objects"], "textures": tex_back,
                          "armatures": len(af.armatures(back))}
    checks = [
        af.check("roundtrip_tris", abs(got["tris"] - want_tris) <= max(2, 0.01 * want_tris),
                 got["tris"], want_tris),
        af.check("roundtrip_meshes", got["objects"] == want_meshes, got["objects"], want_meshes),
        af.check("roundtrip_height", abs(got["size"][2] - want_h) <= max(1e-4, 0.01 * want_h),
                 got["size"][2], round(want_h, 5)),
        af.check("no_nan_verts", got["nan_verts"] == 0, got["nan_verts"], 0),
    ]
    want_height = float(p.get("height_m") or (1.8 if mixamo else 0.0))
    if want_height > 0:
        checks.append(af.check("height_matches",
                               abs(got["size"][2] - want_height) <= 1e-3 * want_height,
                               got["size"][2], want_height,
                               note="re-imported height against params.height_m"))
    grounded = True if mixamo else p.get("ground", p.get("height_m") is not None)
    if grounded:
        checks.append(af.check("grounded",
                               abs(got["bbox_min"][2]) <= 1e-4 * max(got["size"][2], 1e-6),
                               got["bbox_min"][2], 0.0, note="re-imported lowest point"))
    had_tex = any(min(v.get("base_color", [0])) > 0 for v in tex_in.values())
    if had_tex:
        kept = any(min(v.get("base_color", [0])) > 0 for v in tex_back.values())
        checks.append(af.check("roundtrip_base_color", kept, kept, True,
                               note="base colour map found again after re-import"))
    if mixamo:
        checks.append(af.check("mixamo_upload_size", stats["bytes"] <= MIXAMO_UPLOAD_LIMIT,
                               stats["bytes"], MIXAMO_UPLOAD_LIMIT, severity="warning"))
        checks.append(af.check("mixamo_single_mesh", len(af.meshes(back)) == 1,
                               len(af.meshes(back)), 1))
    if stats["input"]["armatures"] and fmt != "obj" and not mixamo:
        checks.append(af.check("roundtrip_armature", len(af.armatures(back)) >= 1,
                               len(af.armatures(back)), 1))
    stats["format"] = fmt
    stats["mixamo"] = mixamo
    return {"outputs": outputs, "stats": stats, "checks": checks, "warnings": warnings}
