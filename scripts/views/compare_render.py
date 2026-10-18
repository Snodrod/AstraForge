"""Untextured orthographic renders of GLB meshes for the views comparison (Blender 5, background mode).

    blender -b --factory-startup --python compare_render.py -- <out_dir> <label>=<mesh.glb> [<label>=<mesh.glb> ...]

For every mesh writes <out_dir>/<label>_<view>.png (transparent background, flat studio shading, no texture) for the
views front, left, back, right (strict side/back orthographic, the shape generator's convention: the character looks at -Y
after the glTF import, its left side is +X) and q34 (front three-quarter, 12 degrees up), plus <out_dir>/render_meta.json.

Framing: every view has the same scale, so that the longest extent of the FRONT silhouette is 1280/1536 of the frame, exactly
like the generator's input images; each view is centred on its own silhouette bounding box (as the generator's inputs are).
compare_sheet.py turns the PNGs into silhouette IoU numbers and the contact sheet.
"""
import json
import math
import sys

import bpy
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:]
out_dir = argv[0]
meshes = [a.split("=", 1) for a in argv[1:]]
RES = 1024
VIEWS = {"front": 0.0, "left": 90.0, "back": 180.0, "right": 270.0}


def load(path):
    bpy.ops.wm.read_factory_settings(use_empty=True)
    bpy.ops.import_scene.gltf(filepath=path)
    objs = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    lo = Vector((1e9, 1e9, 1e9))
    hi = Vector((-1e9, -1e9, -1e9))
    for o in objs:
        for v in o.data.vertices:
            w = o.matrix_world @ v.co
            for i in range(3):
                lo[i] = min(lo[i], w[i])
                hi[i] = max(hi[i], w[i])
    return objs, lo, hi


def setup_scene():
    sc = bpy.context.scene
    sc.render.engine = "BLENDER_WORKBENCH"
    sc.render.resolution_x = sc.render.resolution_y = RES
    sc.render.film_transparent = True
    sc.render.image_settings.file_format = "PNG"
    sc.render.image_settings.color_mode = "RGBA"
    sc.view_settings.view_transform = "Standard"
    sh = sc.display.shading
    sh.light = "STUDIO"
    sh.color_type = "SINGLE"
    sh.single_color = (0.80, 0.80, 0.82)
    sh.show_cavity = True
    sh.cavity_type = "BOTH"
    sh.show_object_outline = False
    sh.show_specular_highlight = False
    cam_d = bpy.data.cameras.new("cam")
    cam_d.type = "ORTHO"
    cam = bpy.data.objects.new("cam", cam_d)
    sc.collection.objects.link(cam)
    sc.camera = cam
    return sc, cam, cam_d


def shoot(sc, cam, cam_d, path, yaw, centre, scale, elev=0.0):
    r, e = math.radians(yaw), math.radians(elev)
    d = Vector((math.sin(r) * math.cos(e), -math.cos(r) * math.cos(e), math.sin(e)))
    cam.location = centre + d * 20.0
    cam.rotation_euler = (centre - cam.location).to_track_quat("-Z", "Y").to_euler()
    cam_d.ortho_scale = scale
    cam_d.clip_start, cam_d.clip_end = 0.1, 100.0
    sc.render.filepath = path
    bpy.ops.render.render(write_still=True)


meta = {}
for label, path in meshes:
    objs, lo, hi = load(path)
    sc, cam, cam_d = setup_scene()
    ext = hi - lo
    # front silhouette: x (width) by z (height); long side = 1280 of 1536 of the frame
    scale = max(ext.x, ext.z) * 1536.0 / 1280.0
    ctr = (lo + hi) / 2
    for name, yaw in VIEWS.items():
        if name in ("front", "back"):
            centre = Vector((ctr.x, ctr.y, ctr.z))
        else:
            centre = Vector((ctr.x, ctr.y, ctr.z))
        shoot(sc, cam, cam_d, "%s/%s_%s.png" % (out_dir, label, name), yaw, centre, scale)
    shoot(sc, cam, cam_d, "%s/%s_q34.png" % (out_dir, label), 35.0, ctr, max(ext.x, ext.z) * 1.12, elev=12.0)
    meta[label] = {"lo": list(lo), "hi": list(hi), "scale": scale, "verts": sum(len(o.data.vertices) for o in objs), "tris": sum(len(o.data.polygons) for o in objs)}
    print("RENDERED", label, meta[label])
with open(out_dir + "/render_meta.json", "w") as f:
    json.dump(meta, f, indent=2)
