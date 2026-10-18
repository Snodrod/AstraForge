"""Self-tests of the paint_from_concept step and _paint_lib.

A synthetic "character" (cylinder body, sphere head, a shield box in front of its left side) is
rendered flat-coloured from a known orthographic camera (yaw 15 deg) into an RGBA "concept"; the
same geometry without colour is painted from it and the texture is checked where the colours must
land: front and back of the red chest and blue legs, the skin head, the body behind the shield
(mirror fill, not the shield's gold), a cyan glowing eye (emission). test_real_* use the pilot
assets on disk and return "skip: ..." when they are missing. Run:

    blender -b --factory-startup --python scripts/blender/tests/run_tests.py -- paint_from_concept
"""
import importlib
import math
import os
import tempfile
import time

import bmesh
import bpy
import mathutils
import numpy as np

import af_common as af
from steps import _paint_align as pal
from steps import _paint_flat as pfl
from steps import _paint_lib as pl
from steps import _paint_qa as pqa

YAW = 15.0
RED = (0.8, 0.1, 0.1)
BLUE = (0.1, 0.2, 0.8)
SKIN = (0.6, 0.4, 0.3)
GOLD = (0.8, 0.65, 0.2)
CYAN = (0.0, 1.0, 1.0)
REAL = {"knight": ("clean_knight/clean.blend", "e4aef2bf-2ef5-4e73-a838-06de3ca3cdaa.png"),
        "bone": ("clean_bone/clean.blend", "dce6bd27-f7aa-4e82-b705-0ce94735ff30.png")}


# --------------------------------------------------------------------------- helpers

def _tmp():
    return tempfile.mkdtemp(prefix="af_paint_")


def _run(inputs, params, out_dir=None, asset=None):
    mod = importlib.import_module("steps.paint_from_concept")
    params = dict({"qa_vlm": "off"}, **params)       # no network in tests unless a test mocks it
    req = {"step": "paint_from_concept", "inputs": inputs, "params": params, "out_dir": out_dir or _tmp()}
    if asset:
        req["asset"] = asset
    os.makedirs(req["out_dir"], exist_ok=True)
    return af.finish(req, mod.run(req))


def _check(res, name):
    for c in res["checks"]:
        if c["name"] == name:
            return c
    raise AssertionError("check %s missing: %s" % (name, [c["name"] for c in res["checks"]]))


def _view_dir(yaw, pitch=0.0):
    a, e = math.radians(yaw), math.radians(pitch)
    return mathutils.Vector((-math.sin(a) * math.cos(e), math.cos(a) * math.cos(e), -math.sin(e)))


def _build_character(with_colors):
    """Body (cylinder r 0.3, z 0..1.4, fine rings) + head sphere + shield box (separate piece) in front of the
    character's left side (+X). with_colors: flat materials RED chest (z > 0.9), BLUE below, SKIN head, GOLD shield."""
    me = bpy.data.meshes.new("Body")
    bm = bmesh.new()
    bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=48, radius1=0.3, radius2=0.3, depth=1.4)
    bmesh.ops.translate(bm, vec=(0, 0, 0.7), verts=bm.verts[:])
    vertical = [e for e in bm.edges if abs(e.verts[0].co.z - e.verts[1].co.z) > 1.0]
    bmesh.ops.subdivide_edges(bm, edges=vertical, cuts=27, use_grid_fill=False)
    bmesh.ops.scale(bm, vec=(1.0, 0.7, 1.0), verts=bm.verts[:])          # torso wider than deep
    before = set(bm.verts)
    bmesh.ops.create_cube(bm, size=1.0)                                 # sword bar pointing at the viewer:
    bar = [v for v in bm.verts if v not in before]                      # its projected length gives the yaw
    bmesh.ops.scale(bm, vec=(0.08, 0.9, 0.08), verts=bar)
    bmesh.ops.translate(bm, vec=(-0.4, -0.5, 0.95), verts=bar)
    before = set(bm.verts)
    bmesh.ops.create_uvsphere(bm, u_segments=32, v_segments=16, radius=0.18)
    bmesh.ops.translate(bm, vec=(0, 0, 1.58), verts=[v for v in bm.verts if v not in before])
    before = set(bm.verts)
    bmesh.ops.create_cube(bm, size=1.0)
    box = [v for v in bm.verts if v not in before]
    bmesh.ops.scale(bm, vec=(0.3, 0.04, 0.6), verts=box)
    bmesh.ops.translate(bm, vec=(0.28, -0.5, 0.8), verts=box)
    bmesh.ops.triangulate(bm, faces=bm.faces[:])
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new("Body", me)
    bpy.context.scene.collection.objects.link(ob)
    if with_colors:
        mats = {}
        for name, col in (("red", RED), ("blue", BLUE), ("skin", SKIN), ("gold", GOLD), ("steel", (0.6, 0.6, 0.62))):
            m = bpy.data.materials.new(name)
            m.diffuse_color = tuple(pl.srgb_to_linear(np.array(col)).tolist()) + (1.0,)
            me.materials.append(m)
            mats[name] = len(me.materials) - 1
        idx = []
        for f in me.polygons:
            c = f.center
            if c.x < -0.35:
                idx.append(mats["steel"])
            elif c.y < -0.45:
                idx.append(mats["gold"])
            elif c.z > 1.42:
                idx.append(mats["skin"])
            elif c.z > 0.9:
                idx.append(mats["red"])
            else:
                idx.append(mats["blue"])
        me.polygons.foreach_set("material_index", idx)
        me.update()
    return ob


def _render_concept(path, yaw=YAW, size=512, eye=True):
    """Flat Workbench render of the coloured character from the orthographic concept camera (RGBA PNG)."""
    ob = _build_character(True)
    objs = [ob]
    d = _view_dir(yaw)
    if eye:
        em = bpy.data.meshes.new("eye")
        bm = bmesh.new()
        bmesh.ops.create_uvsphere(bm, u_segments=12, v_segments=8, radius=0.03)
        bm.to_mesh(em)
        bm.free()
        eo = bpy.data.objects.new("eye", em)
        bpy.context.scene.collection.objects.link(eo)
        eo.location = mathutils.Vector((0, 0, 1.62)) - d * 0.17
        m = bpy.data.materials.new("cyan")
        m.diffuse_color = CYAN + (1.0,)
        em.materials.append(m)
        objs.append(eo)
    scene = bpy.context.scene
    cam_data = bpy.data.cameras.new("concept_cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = 2.2
    cam = bpy.data.objects.new("concept_cam", cam_data)
    scene.collection.objects.link(cam)
    cam.location = mathutils.Vector((0, 0, 0.9)) - d * 6
    cam.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()
    scene.camera = cam
    r = scene.render
    r.engine = af.engine_id("WORKBENCH")
    r.resolution_x = r.resolution_y = size
    r.film_transparent = True
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGBA"
    scene.view_settings.view_transform = "Standard"
    sh = scene.display.shading
    sh.light = "FLAT"
    sh.color_type = "MATERIAL"
    sh.show_cavity = False
    r.filepath = path
    bpy.ops.render.render(write_still=True)
    af.reset_scene()
    return path


def _plain_glb(path):
    af.reset_scene()
    ob = _build_character(False)
    af.add_socket("socket_fx_head", location=(0, 0, 1.8), kind="fx")
    af.export_glb(path, objects=list(bpy.context.scene.objects), animations=False, skins=False)
    af.reset_scene()
    return path


def _texture_at_vertices(glb, png, select):
    """Mean base-colour texel (sRGB) at the centroids of the triangles chosen by select(centroid, normal) of
    the painted GLB (centroids, not corners: a corner of a big flat face sits on the faded silhouette edge)."""
    af.reset_scene()
    af.import_any(glb)
    ob = [o for o in bpy.context.scene.objects if o.type == "MESH"][0]
    d = pl.mesh_data(ob)
    tex = pl.load_rgba(png)[::-1]          # bottom row first, like UVs
    T = tex.shape[0]
    co = d["co"][d["tv"]].mean(axis=1)
    nrm = d["cn"].mean(axis=1)
    nrm /= np.maximum(np.linalg.norm(nrm, axis=1, keepdims=True), 1e-9)
    uv = d["uv"].mean(axis=1)
    sel = select(co, nrm)
    assert sel.sum() >= 1, "no triangles selected"
    j = np.clip((uv[sel, 0] * T).astype(int), 0, T - 1)
    i = np.clip((uv[sel, 1] * T).astype(int), 0, T - 1)
    return tex[i, j, :3].mean(axis=0)


def _near(col, ref, tol=0.2):
    return float(np.abs(np.asarray(col) - np.asarray(ref)).max()) <= tol


# --------------------------------------------------------------------------- library

def test_raster_zbuffer_pull_push():
    # two overlapping triangles: the nearer depth wins, the label buffer names it
    u = np.array([2, 30, 2, 2, 30, 2], dtype=np.float32)
    v = np.array([2, 2, 30, 2, 2, 30], dtype=np.float32)
    z = np.array([5, 5, 5, 1, 1, 1], dtype=np.float32)
    T = np.array([[0, 1, 2], [3, 4, 5]])
    zb, lab = pl.zbuffer(u, v, z, T, 32, 32, tri_label=np.array([7, 9]))
    inside = np.isfinite(zb)
    assert 300 < inside.sum() < 420, inside.sum()
    assert np.allclose(zb[inside], 1.0) and (lab[inside] == 9).all()
    # a unit square in UV covers the whole texture
    sq = np.array([[[0, 0], [1, 0], [1, 1]], [[0, 0], [1, 1], [0, 1]]], dtype=np.float64)
    tri, b1, b2 = pl.raster_uv(sq, 64)
    assert (tri >= 0).all()
    assert pl.uv_overlap(sq) < 0.01
    assert pl.uv_overlap(np.concatenate([sq, sq])) > 0.9
    # pull-push fills every hole with nearby colours
    img = np.zeros((64, 64, 3), dtype=np.float32)
    valid = np.zeros((64, 64), dtype=bool)
    img[:, :32] = (1, 0, 0)
    img[:, 32:] = (0, 0, 1)
    valid[::8, ::8] = True
    out = pl.pull_push(img, valid)
    assert out[30, 2, 0] > 0.8 and out[30, 60, 2] > 0.8


def test_emission_mask_cool_glow_not_gold():
    h, w = 200, 120
    rgb = np.zeros((h, w, 3), dtype=np.float32)
    rgb[:] = (0.35, 0.33, 0.3)                       # grey armour
    mask = np.zeros((h, w), dtype=bool)
    mask[10:190, 20:100] = True
    rgb[40:46, 50:56] = (0.2, 1.0, 0.6)              # glowing green eye
    rgb[100:130, 30:90] = (0.95, 0.78, 0.25)         # bright gold plate (paint, not light)
    rgb[150:160, 40:80] = (0.25, 0.35, 0.9)          # blue cloth band, darker than its glow
    m, info = pl.emission_mask(rgb, mask, 180)
    assert m[42, 52], info
    assert not m[110:120, 40:80].any(), "gold must not glow"
    assert info["blobs"] >= 1


def test_uv_report_and_unpremultiply():
    af.reset_scene()
    ob = _build_character(False)
    d = pl.mesh_data(ob)
    ok, info = pl.uv_report(d)
    assert not ok and info["reason"] == "no UV layer"
    from steps import _mesh_lib as ml
    ml.smart_uv(ob)
    ok, info = pl.uv_report(pl.mesh_data(ob))
    assert ok, info
    # stacked islands are not usable for a unique bake
    me = ob.data
    uv = np.zeros(len(me.loops) * 2)
    me.uv_layers.active.data.foreach_get("uv", uv)
    uv = uv.reshape(-1, 2)
    uv[:] = uv[np.arange(len(uv)) % 3]               # everything onto the first triangles
    me.uv_layers.active.data.foreach_set("uv", uv.ravel())
    ok, info = pl.uv_report(pl.mesh_data(ob))
    assert not ok, info
    # premultiplied edge pixels are divided back, straight ones kept
    px = np.zeros((40, 40, 4), dtype=np.float32)
    px[..., 3] = np.linspace(0, 1, 40)[None, :]
    px[..., 0] = 0.8 * px[..., 3]
    out, pre = pl.unpremultiply(px)
    assert pre and abs(float(out[5, 30, 0]) - 0.8) < 0.02
    px[..., 0] = 0.8
    out, pre = pl.unpremultiply(px)
    assert not pre


# --------------------------------------------------------------------------- step

def test_paint_synthetic_character():
    tmp = _tmp()
    concept = _render_concept(os.path.join(tmp, "concept.png"))
    glb = _plain_glb(os.path.join(tmp, "plain.glb"))
    t0 = time.time()
    res = _run({"mesh": glb, "concept": concept}, {"size": 512, "mirror": "x", "preview_size": 192},
               out_dir=os.path.join(tmp, "out"))
    took = time.time() - t0
    assert res["ok"], res.get("error")
    s = res["stats"]
    cam = s["camera"]
    assert abs(cam["yaw_deg"] - YAW) < 3.0, cam
    assert abs(cam["pitch_deg"]) < 2.5, cam
    assert s["iou"] >= 0.95, s["iou"]
    assert s["uv"]["Body"]["unwrapped"].startswith("smart_uv")
    assert s["glb"]["base_color_materials"] == 1 and s["glb"]["images"] >= 1
    assert _check(res, "glb_base_color_texture")["ok"]
    assert s["color_mae"] is not None and s["color_mae"] <= 0.06, s["color_mae"]
    assert "socket_fx_head" in s["sockets_kept"]
    for k in ("glb", "blend", "basecolor", "compare", "camera"):
        assert os.path.isfile(res["outputs"][k]), k
    assert s["emission"]["texels"] > 0 and os.path.isfile(res["outputs"]["emission"]), s["emission"]
    assert took < 60, took
    d = _view_dir(YAW)
    toward = -np.array(d)
    png = res["outputs"]["basecolor"]
    out_glb = res["outputs"]["glb"]
    body = lambda co: (np.hypot(co[:, 0], co[:, 1]) < 0.32) & (co[:, 2] > 0.05) & (co[:, 2] < 1.38)

    def side(sign, z0, z1, extra=None):
        def f(co, n):
            fac = n @ toward
            m = body(co) & (sign * fac > 0.8) & (co[:, 2] > z0) & (co[:, 2] < z1)
            return m & extra(co) if extra is not None else m
        return f
    front_chest = _texture_at_vertices(out_glb, png, side(1, 1.0, 1.3, lambda co: co[:, 0] < 0.0))
    front_legs = _texture_at_vertices(out_glb, png, side(1, 0.2, 0.45))
    back_chest = _texture_at_vertices(out_glb, png, side(-1, 1.0, 1.3))
    back_legs = _texture_at_vertices(out_glb, png, side(-1, 0.2, 0.45))
    got = {"front_chest": front_chest, "front_legs": front_legs, "back_chest": back_chest, "back_legs": back_legs}
    print("paint colours", {k: [round(float(x), 3) for x in v] for k, v in got.items()})
    assert _near(front_chest, RED, 0.12), got
    assert _near(front_legs, BLUE, 0.12), got
    assert _near(back_chest, RED, 0.2), got       # through-fill: the front seen through the body
    assert _near(back_legs, BLUE, 0.2), got
    # the body behind the shield (front-facing, occluded) takes its mirror twin, not the shield's gold
    hidden = _texture_at_vertices(out_glb, png, lambda co, n: body(co) & ((n @ toward) > 0.6) & (co[:, 0] > 0.12)
                                  & (co[:, 0] < 0.24) & (co[:, 2] > 0.6) & (co[:, 2] < 0.85))
    assert _near(hidden, BLUE, 0.25), ("behind the shield", hidden)
    # the back of the head takes the head (no through-projection of the face / eye)
    head_back = _texture_at_vertices(out_glb, png, lambda co, n: (co[:, 2] > 1.5) & ((n @ toward) < -0.7))
    assert _near(head_back, SKIN, 0.2), ("head back", head_back)
    shield_front = _texture_at_vertices(out_glb, png, lambda co, n: (co[:, 1] < -0.45) & (co[:, 0] > 0) & ((n @ toward) > 0.7))
    assert _near(shield_front, GOLD, 0.15), ("shield", shield_front)


def test_paint_blend_keeps_sockets_and_emissive_meshes():
    tmp = _tmp()
    concept = _render_concept(os.path.join(tmp, "concept.png"), eye=False)
    af.reset_scene()
    ob = _build_character(False)
    # an existing eye with its own glowing material stays as it is
    em = bpy.data.meshes.new("eye_l")
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=8, v_segments=6, radius=0.02)
    bm.to_mesh(em)
    bm.free()
    eye = bpy.data.objects.new("eye_l", em)
    bpy.context.scene.collection.objects.link(eye)
    eye.location = (0.05, -0.17, 1.62)
    glow = bpy.data.materials.new("eye_glow")
    glow.use_nodes = True
    bsdf = [n for n in glow.node_tree.nodes if n.type == "BSDF_PRINCIPLED"][0]
    bsdf.inputs["Emission Color"].default_value = (0.2, 1.0, 0.3, 1.0)
    bsdf.inputs["Emission Strength"].default_value = 5.0
    em.materials.append(glow)
    af.add_socket("socket_fx_chest", location=(0, -0.3, 1.2), kind="fx")
    blend = af.save_blend(os.path.join(tmp, "in.blend"))
    af.reset_scene()
    res = _run({"blend": blend, "concept": concept}, {"size": 256, "preview": False, "emission": "none",
                                                      "concept_max_px": 384}, out_dir=os.path.join(tmp, "out"))
    assert res["ok"], res.get("error")
    s = res["stats"]
    assert s["targets"] == ["Body"] and s["kept_meshes"] == ["eye_l"], (s["targets"], s["kept_meshes"])
    assert "socket_fx_chest" in s["sockets_kept"]
    assert "compare" not in res["outputs"]
    assert s["mask"]["scaled_from"] == [512, 512] and s["concept_size"] == [384, 384], s["mask"]
    af.open_blend(res["outputs"]["blend"])
    assert bpy.data.objects["eye_l"].data.materials[0].name == "eye_glow"
    body = bpy.data.objects["Body"]
    img = [n.image for n in body.data.materials[0].node_tree.nodes if n.type == "TEX_IMAGE"][0]
    assert img.packed_file is not None, "texture packed into the .blend"
    assert "socket_fx_chest" in bpy.data.objects
    # an explicit camera (from paint_camera.json) is reused without a fit
    import json
    with open(res["outputs"]["camera"], "r", encoding="utf-8") as fh:
        cam = json.load(fh)
    af.reset_scene()
    res2 = _run({"blend": blend, "concept": concept}, {"size": 256, "preview": False, "camera": cam, "concept_max_px": 384},
                out_dir=os.path.join(tmp, "out2"))
    assert res2["ok"] and abs(res2["stats"]["iou"] - s["iou"]) < 0.01


def test_paint_missing_concept_is_an_error():
    tmp = _tmp()
    glb = _plain_glb(os.path.join(tmp, "plain.glb"))
    try:
        _run({"mesh": glb}, {})
    except ValueError as e:
        assert "inputs.concept" in str(e)
    else:
        raise AssertionError("no error without inputs.concept")


# --------------------------------------------------------------------------- non-rigid alignment, back, flat, QA

GREEN = (0.1, 0.7, 0.2)
GREY = (0.45, 0.45, 0.5)


def _add_box(bm, size, shift, mat, rot_y_deg=0.0, pivot=None):
    """Axis-aligned box (sx, sy, sz) centred at `shift`, optionally rotated about the Y axis around `pivot`;
    every face gets material index `mat`. Returns the new vertices."""
    before = set(bm.verts)
    bmesh.ops.create_cube(bm, size=1.0)
    new = [v for v in bm.verts if v not in before]
    bmesh.ops.scale(bm, vec=size, verts=new)
    bmesh.ops.translate(bm, vec=shift, verts=new)
    if rot_y_deg:
        bmesh.ops.rotate(bm, verts=new, cent=pivot, matrix=mathutils.Matrix.Rotation(math.radians(rot_y_deg), 3, "Y"))
    ids = set(new)
    for f in bm.faces:
        if all(v in ids for v in f.verts):
            f.material_index = mat
    return new


def _subdivide(bm, cuts):
    """Interior vertices (a real mesh has them; every vertex of a bare box sits on the silhouette)."""
    bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=cuts, use_grid_fill=True)


def _tfigure(arm_deg, arm_len, hand_len, with_colors):
    """Torso + head + two legs + ONE arm along +X (shoulder at x = 0.2, z = 1.3) drooping by arm_deg: a red
    sleeve (arm_len) and a skin hand (hand_len) beyond it. Materials: 0 red, 1 blue, 2 skin, 3 grey."""
    me = bpy.data.meshes.new("Body")
    bm = bmesh.new()
    _add_box(bm, (0.4, 0.2, 0.9), (0, 0, 0.95), 1)                       # torso z 0.5..1.4
    _add_box(bm, (0.15, 0.15, 0.2), (0, 0, 1.5), 2)                      # head
    _add_box(bm, (0.12, 0.14, 0.5), (-0.1, 0, 0.25), 3)                  # legs
    _add_box(bm, (0.12, 0.14, 0.5), (0.1, 0, 0.25), 3)
    pivot = (0.2, 0.0, 1.3)
    _add_box(bm, (arm_len, 0.1, 0.1), (0.2 + arm_len / 2, 0, 1.3), 0, arm_deg, pivot)         # sleeve
    _add_box(bm, (hand_len, 0.1, 0.1), (0.2 + arm_len + hand_len / 2, 0, 1.3), 2, arm_deg, pivot)   # hand
    _subdivide(bm, 5)
    bmesh.ops.triangulate(bm, faces=bm.faces[:])
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new("Body", me)
    bpy.context.scene.collection.objects.link(ob)
    if with_colors:
        for name, col in (("red", RED), ("blue", BLUE), ("skin", SKIN), ("grey", GREY)):
            m = bpy.data.materials.new(name)
            m.diffuse_color = tuple(pl.srgb_to_linear(np.array(col)).tolist()) + (1.0,)
            me.materials.append(m)
    return ob


def _slab(with_colors):
    """One box 0.6 x 0.2 x 1.4 (z 0..1.4), faces looking -Y (front) red, +Y (back) green, the rest blue."""
    me = bpy.data.meshes.new("Body")
    bm = bmesh.new()
    _add_box(bm, (0.6, 0.2, 1.4), (0, 0, 0.7), 2)
    for f in bm.faces:
        n = f.normal
        f.material_index = 0 if n.y < -0.5 else (1 if n.y > 0.5 else 2)
    _subdivide(bm, 8)
    bmesh.ops.triangulate(bm, faces=bm.faces[:])
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new("Body", me)
    bpy.context.scene.collection.objects.link(ob)
    if with_colors:
        for name, col in (("front", RED), ("back", GREEN), ("side", BLUE)):
            m = bpy.data.materials.new(name)
            m.diffuse_color = tuple(pl.srgb_to_linear(np.array(col)).tolist()) + (1.0,)
            me.materials.append(m)
    return ob


def _render_flat(ob, path, yaw=0.0, size=384, ortho=2.0, cz=0.8):
    """Flat Workbench render of ob with its material colours from the orthographic camera at yaw (RGBA PNG)."""
    d = _view_dir(yaw)
    scene = bpy.context.scene
    cam_data = bpy.data.cameras.new("concept_cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = ortho
    cam = bpy.data.objects.new("concept_cam", cam_data)
    scene.collection.objects.link(cam)
    cam.location = mathutils.Vector((0, 0, cz)) - d * 6
    cam.rotation_euler = d.to_track_quat("-Z", "Y").to_euler()
    scene.camera = cam
    r = scene.render
    r.engine = af.engine_id("WORKBENCH")
    r.resolution_x = r.resolution_y = size
    r.film_transparent = True
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGBA"
    scene.view_settings.view_transform = "Standard"
    sh = scene.display.shading
    sh.light = "FLAT"
    sh.color_type = "MATERIAL"
    sh.show_cavity = False
    r.filepath = path
    bpy.ops.render.render(write_still=True)
    return path


def _glb_of(builder, path):
    af.reset_scene()
    builder()
    af.export_glb(path, objects=list(bpy.context.scene.objects), animations=False, skins=False)
    af.reset_scene()
    return path


FAST = {"size": 256, "mirror": "none", "yaw_range_deg": 0, "pitch_range_deg": 0, "perspective": False,
        "preview": False, "qa_size": 128, "concept_max_px": 384}


def test_align_warp_numpy_silhouettes():
    """Pure maths: a concept whose arms / head differ non-rigidly from the model is bent onto the model."""
    n = 512
    yy, xx = np.mgrid[0:n, 0:n].astype(np.float32)

    def figure(arm_slope, arm_len, head_r):
        body = (np.abs(xx - 256) < 45) & (yy > 150) & (yy < 400)
        legs = ((np.abs(xx - 280) < 18) | (np.abs(xx - 232) < 18)) & (yy >= 400) & (yy < 490)
        head = (xx - 256) ** 2 + (yy - 110) ** 2 < head_r ** 2
        t = xx - 301
        arm_r = (t > 0) & (t < arm_len) & (np.abs(yy - (165 + t * arm_slope)) < 14)
        t2 = 211 - xx
        arm_l = (t2 > 0) & (t2 < arm_len) & (np.abs(yy - (165 + t2 * arm_slope)) < 14)
        hand = ((t >= arm_len - 40) & arm_r) | ((t2 >= arm_len - 40) & arm_l)
        return body | legs | head | arm_r | arm_l, hand
    concept, c_hand = figure(0.0, 150, 40)           # arms straight out, big head
    model, m_hand = figure(0.14, 135, 33)            # arms drooping, shorter, smaller head
    colour = np.zeros((n, n, 4), dtype=np.float32)
    colour[..., :3] = BLUE
    colour[c_hand, :3] = SKIN                         # the hands of the concept are skin-coloured
    colour[concept & ~c_hand & (yy < 190) & (np.abs(xx - 256) > 45), :3] = RED   # sleeves
    colour[..., 3] = concept
    t0 = time.time()
    px, fit, col, info, field = pal.align_concept(colour, concept, concept, model, 360.0, pl.dilate_colors,
                                                  align_max=0.08)
    took = time.time() - t0
    assert info["applied"] and field is not None, info
    assert info["iou_after"] >= info["iou_before"] + 0.04 and info["iou_after"] >= 0.93, info
    assert abs(pal.mask_iou(model, fit) - info["iou_after"]) < 1e-3
    mag = np.sqrt((field ** 2).sum(-1))
    assert float(mag.max()) <= 0.08 * 360.0 + 0.5, float(mag.max())          # capped
    assert info["displacement"]["max_pct"] <= 8.0 + 1e-6 and info["displacement"]["mean_pct"] > 0
    assert info["jacobian_min"] > 0.0, info                                  # no folds
    # the hands of the model get the skin colour, the sleeves the red one (colours moved with the silhouette)
    ys, xs = np.nonzero(m_hand & fit)
    hand_col = px[ys, xs, :3].mean(axis=0)
    assert _near(hand_col, SKIN, 0.2), ("model hands", hand_col)
    sleeve = model & ~m_hand & (yy < 200) & (np.abs(xx - 256) > 60) & (np.abs(xx - 256) < 85)
    assert _near(px[sleeve & fit, :3].mean(axis=0), RED, 0.25), "sleeves"
    assert took < 6, took
    # the identical silhouette: nothing to align, the field is ~0
    px2, fit2, col2, info2, field2 = pal.align_concept(colour, concept, concept, concept, 360.0, pl.dilate_colors)
    assert field2 is None or float(np.abs(field2).max()) < 1.5, info2
    # empty masks do not crash
    e = np.zeros_like(concept)
    assert pal.align_concept(colour, concept, concept, e, 360.0, pl.dilate_colors)[4] is None


def test_paint_align_warp_step():
    """The mesh arm droops 22 degrees where the concept's is horizontal: without the warp the hand colour of the
    concept misses the mesh hand, with align: warp the hand is skin-coloured and the silhouette IoU rises."""
    tmp = _tmp()
    af.reset_scene()
    ob = _tfigure(0.0, 0.3, 0.12, True)
    concept = _render_flat(ob, os.path.join(tmp, "concept.png"))
    glb = _glb_of(lambda: _tfigure(22.0, 0.3, 0.12, False), os.path.join(tmp, "mesh.glb"))
    res = {}
    for mode in ("none", "warp"):
        af.reset_scene()
        res[mode] = _run({"mesh": glb, "concept": concept}, dict(FAST, align=mode, align_max=0.1),
                         out_dir=os.path.join(tmp, "out_" + mode))
        assert res[mode]["ok"], res[mode].get("error")
    sn, sw = res["none"]["stats"], res["warp"]["stats"]
    assert sn["align"] == {"mode": "none"} and "compare" not in res["none"]["outputs"]
    a = sw["align"]["front"]
    assert a["applied"] and a["iou_after"] > a["iou_before"] + 0.01, a
    assert sw["iou_aligned"] > sn["iou"] and sw["iou"] == sn["iou"]
    assert sw["qa"]["metrics"]["iou_front_aligned"] == sw["iou_aligned"]
    assert sw["qa"]["metrics"]["warp_front"]["p95_pct"] > 0

    def hand_colour(res_):
        af.reset_scene()
        af.import_any(res_["outputs"]["glb"])
        ob2 = [o for o in bpy.context.scene.objects if o.type == "MESH"][0]
        d = pl.mesh_data(ob2)
        tex = pl.load_rgba(res_["outputs"]["basecolor"])[::-1]
        T = tex.shape[0]
        co = d["co"][d["tv"]].mean(axis=1)
        nrm = d["cn"].mean(axis=1)
        tip = np.array([0.2 + 0.36 * math.cos(math.radians(22)), 1.3 - 0.36 * math.sin(math.radians(22))])   # hand centre
        sel = (np.linalg.norm(co[:, [0, 2]] - tip, axis=1) < 0.05) & (nrm[:, 1] < -0.7)       # front-facing, at the hand
        assert sel.sum() >= 1, "no hand triangles"
        uv = d["uv"].mean(axis=1)
        j = np.clip((uv[sel, 0] * T).astype(int), 0, T - 1)
        i = np.clip((uv[sel, 1] * T).astype(int), 0, T - 1)
        return tex[i, j, :3].mean(axis=0)
    c_none, c_warp = hand_colour(res["none"]), hand_colour(res["warp"])
    print("hand colours", np.round(c_none, 3), np.round(c_warp, 3))
    assert _near(c_none, RED, 0.2), ("without the warp the sleeve colour lands on the hand", c_none)
    assert _near(c_warp, SKIN, 0.12), ("hand with warp", c_warp)


def test_paint_back_reference_projection():
    tmp = _tmp()
    af.reset_scene()
    ob = _slab(True)
    front = _render_flat(ob, os.path.join(tmp, "front.png"), yaw=0.0)
    back = _render_flat(ob, os.path.join(tmp, "back.png"), yaw=180.0)
    glb = _glb_of(lambda: _slab(False), os.path.join(tmp, "slab.glb"))
    params = dict(FAST, back_yaw_range_deg=0, back_pitch_range_deg=0, uv="smart")
    af.reset_scene()
    plain = _run({"mesh": glb, "concept": front}, params, out_dir=os.path.join(tmp, "plain"))
    af.reset_scene()
    both = _run({"mesh": glb, "concept": front, "concept_back": back}, params, out_dir=os.path.join(tmp, "both"))
    af.reset_scene()
    via_param = _run({"mesh": glb, "concept": front}, dict(params, concept_back_file=back), out_dir=os.path.join(tmp, "p"))
    for r in (plain, both, via_param):
        assert r["ok"], r.get("error")
    sb = both["stats"]
    assert "back" not in plain["stats"] and abs(sb["back"]["camera"]["yaw_deg"] - 180.0) < 1.0, sb["back"]["camera"]
    assert sb["back"]["iou"] > 0.9 and sb["texels"]["back_share"] > 0.1, (sb["back"]["iou"], sb["texels"])
    assert plain["stats"]["texels"]["back_projected"] == 0
    assert "camera_back" in both["outputs"] and os.path.isfile(both["outputs"]["camera_back"])
    assert via_param["stats"]["texels"]["back_share"] > 0.1
    assert _check(both, "concept_back_iou_min")["ok"]
    assert sb["qa"]["metrics"]["iou_back"] > 0.9 and sb["qa"]["metrics"]["color_mae_back"] is not None

    def face_colour(res_, sign):
        af.reset_scene()
        af.import_any(res_["outputs"]["glb"])
        ob2 = [o for o in bpy.context.scene.objects if o.type == "MESH"][0]
        d = pl.mesh_data(ob2)
        tex = pl.load_rgba(res_["outputs"]["basecolor"])[::-1]
        T = tex.shape[0]
        nrm = d["cn"].mean(axis=1)
        co = d["co"][d["tv"]].mean(axis=1)
        sel = (sign * nrm[:, 1] > 0.9) & (np.abs(co[:, 0]) < 0.2) & (co[:, 2] > 0.3) & (co[:, 2] < 1.1)
        uv = d["uv"].mean(axis=1)
        j = np.clip((uv[sel, 0] * T).astype(int), 0, T - 1)
        i = np.clip((uv[sel, 1] * T).astype(int), 0, T - 1)
        return tex[i, j, :3].mean(axis=0)
    back_both = face_colour(both, +1)
    back_plain = face_colour(plain, +1)
    front_both = face_colour(both, -1)
    print("back colours: with back", np.round(back_both, 3), "front only", np.round(back_plain, 3))
    assert _near(back_both, GREEN, 0.15), ("back with the back reference", back_both)
    assert _near(front_both, RED, 0.15), ("front stays", front_both)
    assert _near(back_plain, RED, 0.3), ("front-only run guesses the back from the front", back_plain)


def test_paint_flat_mode_one_colour_per_triangle():
    tmp = _tmp()
    concept = _render_concept(os.path.join(tmp, "concept.png"))
    glb = _plain_glb(os.path.join(tmp, "plain.glb"))
    res = _run({"mesh": glb, "concept": concept}, {"size": 512, "mirror": "x", "preview": False, "qa_size": 128,
                                                   "flat": True, "flat_palette": 5})
    assert res["ok"], res.get("error")
    f = res["stats"]["flat"]
    assert f["enabled"] and len(f["palette"]) == 5 and f["triangles_with_texels"] > 1000, f
    ob = [o for o in bpy.context.scene.objects if o.type == "MESH" and o.name == "Body"][0]
    d = pl.mesh_data(ob)
    tex = pl.load_rgba(res["outputs"]["basecolor"])[::-1]
    T = tex.shape[0]
    tri_of, _b1, _b2 = pl.raster_uv(d["uv"], T)
    flat = np.nonzero(tri_of.ravel() >= 0)[0]
    tri = tri_of.ravel()[flat]
    cols = (tex.reshape(-1, 4)[flat, :3] * 255 + 0.5).astype(np.int32)
    order = np.argsort(tri, kind="stable")
    t_sorted, c_sorted = tri[order], cols[order]
    starts = np.flatnonzero(np.concatenate(([True], t_sorted[1:] != t_sorted[:-1])))
    lo = np.minimum.reduceat(c_sorted, starts, axis=0)
    hi = np.maximum.reduceat(c_sorted, starts, axis=0)
    assert int((hi - lo).max()) <= 1, "a triangle has more than one colour"
    uniq = np.unique(c_sorted[starts], axis=0)
    assert len(uniq) <= 5, len(uniq)
    # the facets still carry the concept: the red chest facing the camera
    co = d["co"][d["tv"]].mean(axis=1)
    nrm = d["cn"].mean(axis=1)
    toward = -np.array(_view_dir(YAW))
    chest = ((nrm @ toward) > 0.8) & (co[:, 2] > 1.0) & (co[:, 2] < 1.3) & (np.hypot(co[:, 0], co[:, 1]) < 0.32) & (co[:, 0] < 0.0)
    chest_tris = np.nonzero(chest)[0]
    tri_ids = t_sorted[starts]
    pos = np.searchsorted(tri_ids, chest_tris)
    ok = (pos < len(starts)) & (tri_ids[np.minimum(pos, len(starts) - 1)] == chest_tris)
    assert ok.sum() > 5
    chest_col = c_sorted[starts][pos[ok]].mean(axis=0) / 255.0
    assert _near(chest_col, np.array(RED), 0.15), chest_col


def test_flat_helpers_palette_and_median():
    rng = np.random.default_rng(3)
    cols = np.array([RED, BLUE, SKIN, GOLD])
    samp = np.concatenate([np.clip(c + rng.normal(0, 0.02, (3000, 3)), 0, 1) for c in cols])
    pal_, share = pfl.kmeans_palette(samp, 4, seed=1)
    pal2, _ = pfl.kmeans_palette(samp, 4, seed=1)
    assert np.array_equal(pal_, pal2), "k-means must be deterministic"
    for c in cols:
        assert np.abs(pal_ - c).max(axis=1).min() < 0.05, (c, pal_)
    assert abs(float(share.sum()) - 1.0) < 1e-6
    assert len(pfl.kmeans_palette(np.tile(np.array([[0.2, 0.3, 0.4]]), (50, 1)), 8)[0]) == 1    # one distinct colour
    assert np.abs(pfl.lab_to_srgb(pfl.srgb_to_lab(samp[:100])) - samp[:100]).max() < 1e-6
    snapped = pfl.snap_to_palette(np.array([[0.75, 0.15, 0.15], [0.12, 0.2, 0.75]]), pal_)
    assert _near(snapped[0], RED, 0.06) and _near(snapped[1], BLUE, 0.06)
    # median per triangle ignores the guessed (unconfident) texels and survives outliers
    tri = np.repeat(np.arange(3), 40)
    colors = np.tile(np.array(RED), (120, 1)).astype(np.float32)
    conf = np.ones(120)
    colors[:10] = BLUE                                   # triangle 0: 10 of 40 outliers
    colors[40:70] = GOLD                                 # triangle 1: 30 guessed texels with another colour
    conf[40:70] = 0.0
    out, info = pfl.triangle_colors(tri, colors, conf, 4)
    assert _near(out[0], RED, 0.01) and _near(out[1], RED, 0.01) and _near(out[2], RED, 0.01)
    assert np.isnan(out[3]).all() and info["triangles_with_texels"] == 3


def test_qa_outputs_metrics_and_sheet():
    tmp = _tmp()
    concept = _render_concept(os.path.join(tmp, "concept.png"))
    glb = _plain_glb(os.path.join(tmp, "plain.glb"))
    res = _run({"mesh": glb, "concept": concept}, {"size": 256, "mirror": "x", "preview": False, "qa_size": 128})
    assert res["ok"], res.get("error")
    for k in ("qa_sheet", "qa"):
        assert os.path.isfile(res["outputs"][k]), k
    sheet = pl.load_rgba(res["outputs"]["qa_sheet"])
    assert sheet.shape[1] == 4 * 128 + 3 * 6 and sheet.shape[0] == 2 * (128 + 22) + 6, sheet.shape   # 6 tiles, 4 per row
    assert sheet[..., :3].std() > 0.05
    import json
    with open(res["outputs"]["qa"], "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    m = doc["metrics"]
    for k in ("iou_front", "iou_front_aligned", "color_mae_front", "texels", "grazing_share"):
        assert k in m, (k, list(m))
    assert 0.0 <= m["grazing_share"] <= 1.0 and set(m["texels"]) >= {"from_concept_share", "blended_share",
                                                                      "fill_share", "from_back_share"}
    assert m["iou_front"] >= 0.95 and "iou_back" not in m
    assert doc["vlm"]["mode"] == "off" and not doc["vlm"]["ran"] and doc["thresholds"]
    assert res["stats"]["qa"]["metrics"] == m
    assert not any(w.startswith("qa_vlm_unavailable") for w in res["warnings"]), res["warnings"]   # off is silent
    # with a back reference the sheet carries the back tiles: 7 tiles, and the compare sheet its back row
    af.reset_scene()
    ob = _slab(True)
    front = _render_flat(ob, os.path.join(tmp, "front.png"), yaw=0.0)
    back = _render_flat(ob, os.path.join(tmp, "back.png"), yaw=180.0)
    glb2 = _glb_of(lambda: _slab(False), os.path.join(tmp, "slab.glb"))
    af.reset_scene()
    res2 = _run({"mesh": glb2, "concept": front, "concept_back": back},
                dict(FAST, back_yaw_range_deg=0, back_pitch_range_deg=0, preview=True, preview_size=96))
    assert res2["ok"], res2.get("error")
    sheet2 = pl.load_rgba(res2["outputs"]["qa_sheet"])
    assert sheet2.shape[:2] == sheet.shape[:2], sheet2.shape            # 7 tiles also fit 2 rows of 4
    assert os.path.isfile(res2["outputs"]["compare"])
    cmp_front = pl.load_rgba(res["outputs"].get("compare") or res2["outputs"]["compare"])
    assert cmp_front.shape[0] > 96 * 2                                   # the back row is there


def test_qa_judge_and_parse_review():
    ok = {"iou_front_aligned": 0.92, "color_mae_front": 0.03, "texels": {"fill_share": 0.7}, "grazing_share": 0.1,
          "warp_front": {"p95_pct": 2.0, "jacobian_min": 0.5, "squeezed_share": 0.0}}
    assert pqa.judge(ok) == []                                             # a front-only run guesses most texels
    bad = dict(ok, iou_front_aligned=0.7, grazing_share=0.5, warp_front={"p95_pct": 7.0, "jacobian_min": 0.05,
                                                                        "squeezed_share": 0.2})
    names = {q["metric"] for q in pqa.judge(bad)}
    assert {"iou_front_aligned", "grazing_share", "warp_front_p95_pct", "warp_front_jacobian_min",
            "warp_front_squeezed_share"} <= names, names
    assert "fill_share" in {q["metric"] for q in pqa.judge(ok, has_back=True)}
    good = '{"issues": [{"view": "SIDE", "part": "cloak", "problem": "stripes"}], "score": 7, "verdict": "fix"}'
    r = pqa.parse_review(good)
    assert r["score"] == 7 and r["verdict"] == "fix" and r["issues"][0]["part"] == "cloak"
    r = pqa.parse_review("Here you go:\n```json\n{\"score\": 14, \"issues\": []}\n```\nthanks")
    assert r["score"] == 10 and r["verdict"] == "ok" and r["issues"] == []          # clamped, verdict derived
    assert pqa.parse_review('{"score": -3, "verdict": "FIX"}')["score"] == 0
    assert pqa.parse_review('{"note": {"x": 1}} then {"score": 5.6, "issues": ["blurry back"]}')["score"] == 6
    assert pqa.parse_review("no json here") is None and pqa.parse_review("") is None
    assert pqa.parse_review('{"verdict": "ok"}') is None


class _FakeOllama:
    """Replaces _paint_qa._request: Ollama + ComfyUI endpoints, records every call."""

    def __init__(self, models=("qwen3-vl:8b",), queue=None, chat=None, fail=None):
        self.calls = []
        self.models = models
        self.queue = queue if queue is not None else {"queue_running": [], "queue_pending": []}
        self.chat = chat
        self.fail = fail or {}

    def __call__(self, method, url, payload=None, timeout=30):
        self.calls.append((method, url, payload, timeout))
        for key, exc in self.fail.items():
            if url.endswith(key):
                raise exc
        if url.endswith("/api/tags"):
            return {"models": [{"name": m, "model": m} for m in self.models]}
        if url.endswith("/queue"):
            return self.queue
        if url.endswith("/free"):
            return {}
        if url.endswith("/api/chat"):
            return self.chat
        raise AssertionError("unexpected request " + url)

    def urls(self):
        return [(m, u.rsplit("/", 1)[-1]) for m, u, _p, _t in self.calls]


def _paint_with_fake(fake, params=None, tmp=None):
    tmp = tmp or _tmp()
    concept = _render_concept(os.path.join(tmp, "concept.png"), eye=False)
    glb = _plain_glb(os.path.join(tmp, "plain.glb"))
    old = pqa._request
    pqa._request = fake
    try:
        return _run({"mesh": glb, "concept": concept},
                    dict({"size": 256, "mirror": "x", "preview": False, "qa_size": 128, "qa_vlm": "auto"}, **(params or {})),
                    out_dir=os.path.join(tmp, "out"))
    finally:
        pqa._request = old


def test_qa_vlm_mocked_review_scores_and_warns():
    import base64
    import json
    chat = {"message": {"role": "assistant", "content":
                        ' [{"view": "MODEL BACK", "part": "cloak", "problem": "stripes on the back"}], '
                        '"score": 3, "verdict": "fix"}'}}
    fake = _FakeOllama(chat=chat)
    res = _paint_with_fake(fake, {"qa_model": "qwen3-vl:8b", "qa_url": "http://ollama.test:11434"})
    assert res["ok"], res.get("error")
    urls = fake.urls()
    # order: model listed -> ComfyUI queue idle -> /free -> chat
    assert urls[:4] == [("GET", "tags"), ("GET", "queue"), ("POST", "free"), ("POST", "chat")], urls
    free = [c for c in fake.calls if c[1].endswith("/free")][0]
    assert free[1].startswith("http://127.0.0.1:8190") and free[2] == {"unload_models": True, "free_memory": True}
    chat_call = [c for c in fake.calls if c[1].endswith("/api/chat")][0]
    assert chat_call[1] == "http://ollama.test:11434/api/chat"
    body = chat_call[2]
    assert body["model"] == "qwen3-vl:8b" and body["stream"] is False and body["keep_alive"] == 0
    assert body["options"]["temperature"] == 0
    user = body["messages"][0]
    assert user["role"] == "user" and len(user["images"]) == 1 and "CONCEPT FRONT" in user["content"]
    assert base64.b64decode(user["images"][0])[:8] == b"\x89PNG\r\n\x1a\n"
    assert '"issues"' in user["content"] and "score" in user["content"] and "verdict" in user["content"]
    vlm = res["stats"]["qa"]["vlm"]
    assert vlm["ran"] and vlm["score"] == 3 and vlm["verdict"] == "fix" and vlm["issues"][0]["part"] == "cloak"
    assert _check(res, "texture_qa")["ok"] is False
    assert any(w.startswith("texture_qa") for w in res["warnings"]), res["warnings"]
    assert not any(w.startswith("qa_vlm_unavailable") for w in res["warnings"])
    with open(res["outputs"]["qa"], "r", encoding="utf-8") as fh:
        assert json.load(fh)["vlm"]["score"] == 3
    # a good answer does not warn; qa_min_score raises the bar; qa_free_comfy false leaves ComfyUI alone
    fake2 = _FakeOllama(chat={"message": {"content": ' [], "score": 8, "verdict": "ok"}'}})
    res2 = _paint_with_fake(fake2)
    assert res2["stats"]["qa"]["vlm"]["score"] == 8 and _check(res2, "texture_qa")["ok"]
    assert not any(w.startswith("texture_qa") for w in res2["warnings"]), res2["warnings"]
    fake3 = _FakeOllama(chat={"message": {"content": ' [], "score": 8, "verdict": "ok"}'}})
    res3 = _paint_with_fake(fake3, {"qa_min_score": 9, "qa_free_comfy": False})
    assert any(w.startswith("texture_qa") for w in res3["warnings"])
    assert ("POST", "free") not in fake3.urls(), "qa_free_comfy false must not touch ComfyUI"


def test_qa_vlm_never_fails_the_step():
    import urllib.error
    # Ollama unreachable
    f1 = _FakeOllama(fail={"/api/tags": urllib.error.URLError("refused")})
    r1 = _paint_with_fake(f1)
    assert r1["ok"] and r1["stats"]["qa"]["vlm"]["ran"] is False
    assert any(w.startswith("qa_vlm_unavailable") for w in r1["warnings"]), r1["warnings"]
    assert [u for u in f1.urls() if u[1] in ("free", "chat")] == []
    # model not installed
    f2 = _FakeOllama(models=("llama3:8b",))
    r2 = _paint_with_fake(f2)
    assert r2["ok"] and any("not installed" in w for w in r2["warnings"]), r2["warnings"]
    # ComfyUI busy: never free / compete for the GPU
    f3 = _FakeOllama(queue={"queue_running": [[1, "x", {}]], "queue_pending": []}, chat={"message": {"content": ""}})
    r3 = _paint_with_fake(f3)
    assert r3["ok"] and any("ComfyUI is busy" in w for w in r3["warnings"]), r3["warnings"]
    assert [u for u in f3.urls() if u[1] in ("free", "chat")] == []
    # the chat call fails / the answer is garbage
    f4 = _FakeOllama(chat=None, fail={"/api/chat": TimeoutError("timed out")})
    r4 = _paint_with_fake(f4)
    assert r4["ok"] and any(w.startswith("qa_vlm_unavailable") for w in r4["warnings"]), r4["warnings"]
    f5 = _FakeOllama(chat={"message": {"content": "I cannot help with that."}})
    r5 = _paint_with_fake(f5)
    assert r5["ok"] and any("unreadable answer" in w for w in r5["warnings"]), r5["warnings"]
    # ComfyUI not running at all: just no /free
    f6 = _FakeOllama(chat={"message": {"content": ' [], "score": 9, "verdict": "ok"}'}}, fail={"/queue": OSError("down")})
    r6 = _paint_with_fake(f6)
    assert r6["stats"]["qa"]["vlm"]["ran"] and ("POST", "free") not in f6.urls()
    # qa_vlm off: no request at all, no warning
    f7 = _FakeOllama()
    r7 = _paint_with_fake(f7, {"qa_vlm": "off"})
    assert f7.calls == [] and r7["ok"] and all("qa_vlm" not in w for w in r7["warnings"])


# --------------------------------------------------------------------------- real assets


