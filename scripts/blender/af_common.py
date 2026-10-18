"""Shared helpers for AstraForge Blender steps (Blender 5.0.1, bundled Python 3.11 + numpy).

Contract: docs/FORGE-CONTRACT.md. Only the integrator edits this file; steps keep their own
helpers in their own modules (steps/_<module>_lib.py).
"""
import json
import math
import os
import sys
import time

import bpy
import mathutils

T0 = time.time()
MESH_EXT = (".glb", ".gltf", ".fbx", ".obj", ".stl", ".blend")


# --------------------------------------------------------------------------- protocol

def load_request(path):
    with open(path, "r", encoding="utf-8-sig") as fh:
        req = json.load(fh)
    req.setdefault("inputs", {})
    req.setdefault("params", {})
    if not req.get("out_dir"):
        raise ValueError("request.out_dir is required")
    req["out_dir"] = os.path.abspath(req["out_dir"])
    os.makedirs(req["out_dir"], exist_ok=True)
    return req


def log(msg, **data):
    """Progress line for server/blender.mjs (forwarded to the job log and UI)."""
    payload = {"t": round(time.time() - T0, 2), "msg": str(msg)}
    payload.update(data)
    print("AFLOG " + json.dumps(payload, ensure_ascii=False), flush=True)


def out_path(req, name):
    return os.path.join(req["out_dir"], name)


def check(name, ok, value=None, limit=None, severity="error", note=None):
    item = {"name": name, "ok": bool(ok), "value": _jsonable(value), "limit": _jsonable(limit),
            "severity": severity}
    if note:
        item["note"] = note
    return item


def finish(req, result):
    """Normalise the step result, write result.json and print the AFRESULT line."""
    res = {"ok": True, "step": req.get("step"), "outputs": {}, "stats": {}, "checks": [],
           "warnings": []}
    res.update(result or {})
    failed = [c for c in res["checks"] if not c.get("ok") and c.get("severity", "error") == "error"]
    if failed:
        res["ok"] = False
        res.setdefault("error", "checks failed: " + ", ".join(c["name"] for c in failed))
    res["seconds"] = round(time.time() - T0, 2)
    res = _jsonable(res)
    with open(out_path(req, "result.json"), "w", encoding="utf-8") as fh:
        json.dump(res, fh, ensure_ascii=False, indent=2)
    print("AFRESULT " + json.dumps(res, ensure_ascii=False), flush=True)
    sys.stdout.flush()
    return res


def _jsonable(v):
    if isinstance(v, (mathutils.Vector, mathutils.Euler, mathutils.Quaternion)):
        return [round(float(x), 6) for x in v]
    if isinstance(v, mathutils.Matrix):
        return [[round(float(x), 6) for x in row] for row in v]
    if isinstance(v, dict):
        return {str(k): _jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [_jsonable(x) for x in v]
    if isinstance(v, float):
        return v if math.isfinite(v) else None
    if hasattr(v, "item"):  # numpy scalar
        try:
            return v.item()
        except Exception:
            return str(v)
    return v


# --------------------------------------------------------------------------- scene I/O

def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    scene.unit_settings.system = "METRIC"
    scene.unit_settings.scale_length = 1.0
    return scene


def open_blend(path):
    bpy.ops.wm.open_mainfile(filepath=os.path.abspath(path))
    return bpy.context.scene


def save_blend(path):
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        bpy.context.preferences.filepaths.save_version = 0  # no .blend1 next to outputs
    except Exception:
        pass
    bpy.ops.wm.save_as_mainfile(filepath=path, compress=True, copy=False)
    return path


def importer_helpers():
    """Objects importers add that are not part of the model: bone custom shapes and the glTF
    importer's 'glTF_not_exported' collection (its bone-shape Icosphere is ~2 m wide)."""
    out = set()
    for a in bpy.data.objects:
        if a.type == "ARMATURE" and a.pose is not None:
            for pb in a.pose.bones:
                if pb.custom_shape is not None:
                    out.add(pb.custom_shape)
    coll = bpy.data.collections.get("glTF_not_exported")
    if coll is not None:
        out.update(coll.all_objects)
    return out


def purge_importer_helpers(objects=None):
    """Delete importer helpers; returns the given objects without them."""
    helpers = importer_helpers()
    for a in bpy.data.objects:
        if a.type == "ARMATURE" and a.pose is not None:
            for pb in a.pose.bones:
                if pb.custom_shape in helpers:
                    pb.custom_shape = None
    names = sorted(o.name for o in helpers)
    for o in list(helpers):
        data = o.data
        bpy.data.objects.remove(o, do_unlink=True)
        if isinstance(data, bpy.types.Mesh) and data.users == 0:
            bpy.data.meshes.remove(data)
    coll = bpy.data.collections.get("glTF_not_exported")
    if coll is not None:
        bpy.data.collections.remove(coll)
    if names:
        log("removed importer helpers", objects=names)
    bpy.context.view_layer.update()
    return [o for o in (objects or []) if _alive(o)]


def _alive(o):
    try:
        return o is not None and o.name is not None and o.name in bpy.data.objects
    except ReferenceError:
        return False


def import_any(path, purge_helpers=True):
    """Import a mesh/scene file and return the newly created objects (importer helpers removed)."""
    path = os.path.abspath(path)
    ext = os.path.splitext(path)[1].lower()
    before = set(bpy.data.objects)
    if ext in (".glb", ".gltf"):
        bpy.ops.import_scene.gltf(filepath=path)
    elif ext == ".fbx":
        bpy.ops.import_scene.fbx(filepath=path, automatic_bone_orientation=False)
    elif ext == ".obj":
        bpy.ops.wm.obj_import(filepath=path)
    elif ext == ".stl":
        bpy.ops.wm.stl_import(filepath=path)
    elif ext == ".blend":
        with bpy.data.libraries.load(path, link=False) as (src, dst):
            dst.objects = list(src.objects)
        for ob in dst.objects:
            if ob is not None:
                bpy.context.scene.collection.objects.link(ob)
    else:
        raise ValueError("unsupported input format: %s" % ext)
    created = [o for o in bpy.data.objects if o not in before]
    if purge_helpers:
        created = purge_importer_helpers(created)
    log("imported", file=os.path.basename(path), objects=len(created))
    return created


def export_glb(path, objects=None, animations=True, skins=True, morph=False, extras=True,
               apply=True, yup=True, animation_mode="ACTIONS", force_sampling=True, **extra):
    """Export GLB. objects=None exports the visible scene; otherwise only those objects."""
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    kw = dict(filepath=path, export_format="GLB", export_yup=yup, export_apply=apply,
              export_extras=extras, export_normals=True, export_texcoords=True,
              export_materials="EXPORT", export_cameras=False, export_lights=False,
              export_animations=animations, export_skins=skins, export_morph=morph)
    if animations:
        kw.update(export_animation_mode=animation_mode, export_force_sampling=force_sampling)
    if objects is not None:
        select_only(objects)
        kw["use_selection"] = True
    else:
        kw["use_visible"] = True
    kw.update(extra)
    frame = bpy.context.scene.frame_current
    try:
        bpy.ops.export_scene.gltf(**kw)
    finally:
        bpy.context.scene.frame_set(frame)
    return path


def export_fbx(path, objects=None, mixamo=False, **extra):
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    kw = dict(filepath=path, apply_unit_scale=True, bake_space_transform=False,
              add_leaf_bones=False, use_armature_deform_only=True, path_mode="COPY",
              embed_textures=True)
    if objects is not None:
        select_only(objects)
        kw["use_selection"] = True
    if mixamo:
        kw.update(bake_anim=False, object_types={"ARMATURE", "MESH"})
    kw.update(extra)
    bpy.ops.export_scene.fbx(**kw)
    return path


def select_only(objects):
    bpy.context.view_layer.update()
    for o in bpy.context.view_layer.objects:
        if o is not None:
            o.select_set(False)
    objects = [o for o in objects if _alive(o)]
    for o in objects:
        o.hide_set(False)
        o.select_set(True)
    if objects:
        bpy.context.view_layer.objects.active = objects[0]


# --------------------------------------------------------------------------- geometry

def meshes(objects=None):
    src = objects if objects is not None else bpy.context.scene.objects
    return [o for o in src if o.type == "MESH"]


def armatures(objects=None):
    src = objects if objects is not None else bpy.context.scene.objects
    return [o for o in src if o.type == "ARMATURE"]


def world_bbox(objects=None):
    pts = []
    for o in meshes(objects):
        pts.extend(o.matrix_world @ mathutils.Vector(c) for c in o.bound_box)
    if not pts:
        return mathutils.Vector((0, 0, 0)), mathutils.Vector((0, 0, 0))
    lo = mathutils.Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = mathutils.Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return lo, hi


def tri_count(objects=None):
    total = 0
    dg = bpy.context.evaluated_depsgraph_get()
    for o in meshes(objects):
        ev = o.evaluated_get(dg)
        me = ev.to_mesh()
        try:
            me.calc_loop_triangles()
            total += len(me.loop_triangles)
        finally:
            ev.to_mesh_clear()
    return total


def scene_stats(objects=None):
    lo, hi = world_bbox(objects)
    size = hi - lo
    arms = armatures(objects)
    return {
        "objects": len(objects if objects is not None else bpy.context.scene.objects),
        "meshes": len(meshes(objects)),
        "tris": tri_count(objects),
        "verts": sum(len(o.data.vertices) for o in meshes(objects)),
        "bbox_min": lo, "bbox_max": hi, "size": size,
        "armatures": [{"name": a.name, "bones": len(a.data.bones)} for a in arms],
        "actions": [{"name": ac.name, "frames": list(ac.frame_range)} for ac in bpy.data.actions],
    }


def apply_transforms(objects, location=True, rotation=True, scale=True):
    select_only(objects)
    bpy.ops.object.transform_apply(location=location, rotation=rotation, scale=scale)


def join(objects, name=None):
    objs = meshes(objects)
    if not objs:
        return None
    if len(objs) == 1:
        if name:
            objs[0].name = name
        return objs[0]
    select_only(objs)
    bpy.ops.object.join()
    ob = bpy.context.view_layer.objects.active
    if name:
        ob.name = name
    return ob


def normalize(objects, height=None, ground=True, center=True, rotate_z_deg=0.0):
    """Scale to height (m), put the lowest point on z=0 and centre X/Y. Applies transforms."""
    roots = [o for o in objects if o.parent is None]
    if rotate_z_deg:
        rot = mathutils.Matrix.Rotation(math.radians(rotate_z_deg), 4, "Z")
        for o in roots:  # matrix form works for EULER and QUATERNION rotation modes alike
            o.matrix_world = rot @ o.matrix_world
    bpy.context.view_layer.update()
    lo, hi = world_bbox(objects)
    size = hi - lo
    s = 1.0
    if height and size.z > 1e-6:
        s = float(height) / size.z
    pivot = mathutils.Vector(((lo.x + hi.x) / 2 if center else 0.0,
                              (lo.y + hi.y) / 2 if center else 0.0,
                              lo.z if ground else 0.0))
    for o in roots:
        o.matrix_world = (mathutils.Matrix.Scale(s, 4) @ mathutils.Matrix.Translation(-pivot)
                          @ o.matrix_world)
    bpy.context.view_layer.update()
    mesh_roots = [o for o in objects if o.type == "MESH"]
    if mesh_roots and not armatures(objects):
        apply_transforms(mesh_roots)
    return s


def add_socket(name, parent=None, bone=None, location=(0, 0, 0), rotation=(0, 0, 0), kind=None,
               size=0.05, **extras):
    """Contract §6: empty `socket_<kind>_<place>` with extras.game_socket (and kind/size_class)."""
    if not name.startswith("socket_"):
        raise ValueError("socket names must start with 'socket_': %s" % name)
    emp = bpy.data.objects.new(name, None)
    emp.empty_display_type = "ARROWS"
    emp.empty_display_size = size
    bpy.context.scene.collection.objects.link(emp)
    if parent is not None:
        emp.parent = parent
        if bone:
            emp.parent_type = "BONE"
            emp.parent_bone = bone
    emp.location = location
    emp.rotation_euler = rotation
    emp["game_socket"] = name
    if kind:
        emp["kind"] = kind
    for k, v in extras.items():
        emp[k] = v
    return emp


# --------------------------------------------------------------------------- previews

VIEWS = {
    # name: (camera direction from target, up axis)
    "front": (mathutils.Vector((0, -1, 0)), "Z"),
    "back": (mathutils.Vector((0, 1, 0)), "Z"),
    "left": (mathutils.Vector((-1, 0, 0)), "Z"),
    "right": (mathutils.Vector((1, 0, 0)), "Z"),
    "top": (mathutils.Vector((0, 0, 1)), "Y"),
    "three_quarter": (mathutils.Vector((-0.7, -0.7, 0.45)).normalized(), "Z"),
}


def engine_id(preferred="EEVEE"):
    items = {e.identifier for e in bpy.types.RenderSettings.bl_rna.properties["engine"].enum_items}
    for cand in {"EEVEE": ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"),
                 "CYCLES": ("CYCLES",), "WORKBENCH": ("BLENDER_WORKBENCH",)}[preferred]:
        if cand in items:
            return cand
    return "BLENDER_WORKBENCH"


def set_engine(scene, preferred="WORKBENCH"):
    """Assign the render engine; falls back to Workbench if the engine is unavailable."""
    names = {"EEVEE": ("BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"), "CYCLES": ("CYCLES",),
             "WORKBENCH": ("BLENDER_WORKBENCH",)}.get(preferred, (preferred,))
    for cand in names:
        try:
            scene.render.engine = cand
            return cand
        except TypeError:
            continue
    scene.render.engine = "BLENDER_WORKBENCH"
    return "BLENDER_WORKBENCH"


def render_views(path, objects=None, views=("front", "left", "back", "three_quarter"), size=512,
                 engine="WORKBENCH", margin=1.08, frame=None, color="MATERIAL", keep_tiles=False):
    """Orthographic contact sheet of the given objects (one row of views) with alpha."""
    scene = bpy.context.scene
    if frame is not None:
        scene.frame_set(frame)
    bpy.context.view_layer.update()
    lo, hi = world_bbox(objects)
    centre = (lo + hi) / 2
    extent = max((hi - lo).length, 1e-3)
    cam_data = bpy.data.cameras.new("af_preview_cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = extent * margin
    cam_data.clip_end = extent * 20
    cam = bpy.data.objects.new("af_preview_cam", cam_data)
    scene.collection.objects.link(cam)
    prev_cam = scene.camera
    scene.camera = cam
    r = scene.render
    set_engine(scene, engine)
    r.resolution_x = r.resolution_y = int(size)
    r.resolution_percentage = 100
    r.film_transparent = True
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGBA"
    if r.engine == "BLENDER_WORKBENCH":
        sh = scene.display.shading
        sh.light = "STUDIO"
        sh.color_type = color  # MATERIAL | TEXTURE | VERTEX | OBJECT | SINGLE
        sh.show_cavity = True
        sh.show_object_outline = False
    tiles = []
    tmp_dir = os.path.join(os.path.dirname(os.path.abspath(path)), "_views")
    os.makedirs(tmp_dir, exist_ok=True)
    import numpy as np
    for name in views:
        direction, up = VIEWS[name]
        cam.location = centre + direction * extent * 3
        if name == "top":
            cam.rotation_euler = (0.0, 0.0, 0.0)
        else:
            look = (centre - cam.location).normalized()
            cam.rotation_euler = look.to_track_quat("-Z", "Y").to_euler()
        one = os.path.join(tmp_dir, "%s.png" % name)
        r.filepath = one
        bpy.ops.render.render(write_still=True)
        img = bpy.data.images.load(one, check_existing=False)
        px = np.array(img.pixels[:], dtype=np.float32).reshape(img.size[1], img.size[0], 4)
        tiles.append(px)
        bpy.data.images.remove(img)
    sheet = np.concatenate(tiles, axis=1)
    out = bpy.data.images.new("af_sheet", width=sheet.shape[1], height=sheet.shape[0], alpha=True)
    out.pixels = sheet.ravel()
    out.filepath_raw = os.path.abspath(path)
    out.file_format = "PNG"
    out.save()
    bpy.data.images.remove(out)
    scene.camera = prev_cam
    bpy.data.objects.remove(cam)
    bpy.data.cameras.remove(cam_data)
    if not keep_tiles:
        import shutil
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return os.path.abspath(path)


def enable_addon(name, default_set=True):
    import addon_utils
    addon_utils.enable(name, default_set=default_set)
    return name
