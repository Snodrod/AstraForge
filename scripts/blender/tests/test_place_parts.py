"""Self-tests of the place_parts step (copies of a part on a target surface or at explicit poses).

Synthetic tests build their inputs in tempfile.mkdtemp(); test_real_* use assets on disk and return
"skip: ..." when a file is missing. Run:

    blender -b --factory-startup --python scripts/blender/tests/run_tests.py -- place_parts
"""
import importlib
import json
import math
import os
import struct
import tempfile

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector

import af_common as af
from steps import _mesh_lib as ml


# --------------------------------------------------------------------------- helpers

def _tmp():
    return tempfile.mkdtemp(prefix="af_pp_")


def _run(inputs, params, out_dir=None):
    mod = importlib.import_module("steps.place_parts")
    req = {"step": "place_parts", "inputs": inputs, "params": params, "out_dir": out_dir or _tmp()}
    os.makedirs(req["out_dir"], exist_ok=True)
    return af.finish(req, mod.run(req))


def _check(res, name):
    for c in res["checks"]:
        if c["name"] == name:
            return c
    raise AssertionError("check %s missing: %s" % (name, [c["name"] for c in res["checks"]]))


def _save(path, builds, colors=False):
    """builds: list of (name, fn(bm)); each becomes one object of the GLB."""
    af.reset_scene()
    objs = []
    for name, build in builds:
        me = bpy.data.meshes.new(name)
        bm = bmesh.new()
        build(bm)
        bmesh.ops.triangulate(bm, faces=bm.faces)
        bm.to_mesh(me)
        bm.free()
        me.uv_layers.new(name="UVMap")
        if colors:
            a = me.color_attributes.new("Color", "FLOAT_COLOR", "POINT")
            a.data.foreach_set("color", [0.2, 0.4, 0.6, 1.0] * len(me.vertices))
        ob = bpy.data.objects.new(name, me)
        bpy.context.scene.collection.objects.link(ob)
        mat = bpy.data.materials.new(name + "_mat")
        mat.use_nodes = True
        if colors:   # the material uses the attribute, so glTF exports it as COLOR_0
            nt = mat.node_tree
            bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
            ca = nt.nodes.new("ShaderNodeVertexColor")
            ca.layer_name = "Color"
            nt.links.new(ca.outputs["Color"], bsdf.inputs["Base Color"])
        me.materials.append(mat)
        objs.append(ob)
    af.export_glb(path, objects=objs, animations=False, skins=False)
    af.reset_scene()
    return path


def _box(bm, lo, hi):
    lo, hi = Vector(lo), Vector(hi)
    res = bmesh.ops.create_cube(bm, size=1.0)
    vs = res["verts"]
    bmesh.ops.scale(bm, vec=hi - lo, verts=vs)
    bmesh.ops.translate(bm, vec=(lo + hi) / 2, verts=vs)


def _part(path, colors=False):
    """A 'clasp' 10 x 2 x 3 cm, origin at the back-face centre, -Y outward (bone_toggle frame), with a
    marker bump on its +Z top edge so the up direction can be checked."""
    def build(bm):
        _box(bm, (-0.05, -0.02, -0.015), (0.05, 0.0, 0.015))
        _box(bm, (-0.01, -0.02, 0.015), (0.01, -0.01, 0.025))
    return _save(path, [("clasp", build)], colors=colors)


def _verts(ob):
    return ml.mesh_arrays(ob.data)["co"] @ np.array(ob.matrix_world)[:3, :3].T + \
        np.array(ob.matrix_world)[:3, 3]


def _load(path):
    af.reset_scene()
    objs = af.import_any(path)
    return [o for o in objs if o.type == "MESH"]


# --------------------------------------------------------------------------- unit tests

def test_frame_from_normal_axes_and_roll():
    from steps import place_parts as pp
    R = pp.frame_from_normal((0, -1, 0), (0, 0, 1))
    # bone_toggle on a -Y facing surface: local X -> world X, local Y -> +Y (into the surface), Z up
    assert np.allclose(np.array(R), np.eye(3), atol=1e-6), R
    R = pp.frame_from_normal((0, -1, 0), (0, 0, 1), roll_deg=90)
    z = R @ Vector((0, 0, 1))
    # +90 deg counter-clockwise seen from outside (from -Y): up turns to world +X... seen from the
    # front (camera at -Y looking +Y, +X to the right) counter-clockwise takes +Z to -X
    assert (z - Vector((-1, 0, 0))).length < 1e-6, z
    assert abs(R.determinant() - 1) < 1e-6
    # tilted surface: -Y stays along the outward normal
    n = Vector((0.3, -0.9, 0.2)).normalized()
    R = pp.frame_from_normal(n, (0, 0, 1))
    assert ((R @ Vector((0, -1, 0))) - n).length < 1e-6
    assert (R @ Vector((0, 0, 1))).z > 0.9
    # surface facing straight up: falls back to +Y as up, still a rotation
    R = pp.frame_from_normal((0, 0, 1), (0, 0, 1))
    assert abs(R.determinant() - 1) < 1e-6 and ((R @ Vector((0, -1, 0))) - Vector((0, 0, 1))).length < 1e-6


def test_place_on_tilted_plane():
    d = _tmp()
    part = _part(os.path.join(d, "part.glb"))
    ang = math.radians(25.0)

    def tgt(bm):   # a 1 m slab whose front face is tilted 25 deg about Z (normal (sin, -cos, 0))
        _box(bm, (-0.5, 0.0, -0.5), (0.5, 0.2, 0.5))
        bmesh.ops.rotate(bm, verts=bm.verts, cent=(0, 0, 0), matrix=Matrix.Rotation(ang, 3, "Z"))
    target = _save(os.path.join(d, "target.glb"), [("slab", tgt)])
    res = _run({"mesh": target, "part": part}, {"points": [[0.1, 0.2]], "join": False,
                                                 "preview": True}, os.path.join(d, "out"))
    assert res["ok"], res
    rec = res["stats"]["points"][0]
    n_true = np.array([math.sin(ang), -math.cos(ang), 0.0])
    assert np.dot(rec["normal"], n_true) > 0.9999, rec
    # the back-face centre lies on the slab surface
    p = np.array(rec["position"])
    assert abs(np.dot(p, n_true)) < 1e-4, (p, np.dot(p, n_true))
    assert abs(p[2] - 0.2) < 1e-6
    objs = {o.name: o for o in _load(res["outputs"]["glb"])}
    assert set(objs) == {"slab", "part_1"}, objs.keys()   # default name: <part file stem>_<i>
    v = _verts(objs["part_1"])
    # nothing of the part inside the slab (back face exactly on it), front 2 cm out
    s = v @ n_true
    assert s.min() > -1e-4 and abs(s.max() - 0.02) < 1e-4, (s.min(), s.max())
    # the marker bump is on the world-up side
    assert v[:, 2].max() > p[2] + 0.024
    assert os.path.isfile(res["outputs"]["preview"]) and os.path.isfile(res["outputs"]["blend"])


def test_footprint_bridges_a_groove_center_sinks():
    d = _tmp()
    part = _part(os.path.join(d, "part.glb"))

    def tgt(bm):   # two ridges (front at y = -0.03) with a 4 cm deep groove between them
        _box(bm, (-0.5, 0.0, -0.3), (0.5, 0.3, 0.3))
        _box(bm, (-0.5, -0.03, -0.3), (-0.012, 0.0, 0.3))
        _box(bm, (0.012, -0.03, -0.3), (0.5, 0.0, 0.3))
    target = _save(os.path.join(d, "target.glb"), [("robe", tgt)])
    foot = _run({"mesh": target, "part": part}, {"points": [[0.0, 0.0]], "preview": False})
    cent = _run({"mesh": target, "part": part}, {"points": [[0.0, 0.0]], "contact": "center",
                                                  "preview": False})
    assert foot["ok"] and cent["ok"], (foot, cent)
    pf = foot["stats"]["points"][0]
    pc = cent["stats"]["points"][0]
    # footprint: rests on the ridge fronts (y = -0.03) and bridges the groove
    assert abs(pf["position"][1] + 0.03) < 1e-4, pf
    assert abs(pf["rest_offset_m"] - 0.03) < 1e-4 and pf["gap_max_m"] > 0.029, pf
    assert pf["penetration"]["max_depth_m"] < 1e-3, pf["penetration"]
    # centre contact: the part sits at the groove bottom and cuts into both ridges
    assert abs(pc["position"][1]) < 1e-4, pc
    assert pc["penetration"]["inside_share"] > 0.2, pc["penetration"]
    assert not _check(cent, "penetration")["ok"]
    assert _check(foot, "penetration")["ok"]


def test_scale_roll_names_join_and_colours():
    d = _tmp()
    part = _part(os.path.join(d, "part.glb"), colors=True)
    target = _save(os.path.join(d, "target.glb"),
                   [("body", lambda bm: _box(bm, (-0.5, 0.0, -0.5), (0.5, 0.4, 0.5)))])
    res = _run({"mesh": target, "part": part},
               {"points": [{"at": [-0.2, 0.0], "name": "a"},
                           {"at": [0.2, 0.0], "name": "b", "scale": 2.0, "roll_deg": 90}],
                "name": "dressed", "preview": False}, os.path.join(d, "out"))
    assert res["ok"], res
    objs = _load(res["outputs"]["glb"])
    assert [o.name for o in objs] == ["dressed"], [o.name for o in objs]
    me = objs[0].data
    assert "Color" in me.color_attributes
    # target vertices got white, part vertices kept their colour
    attr = me.color_attributes["Color"]
    col = np.empty(len(attr.data) * 4)
    attr.data.foreach_get("color", col)
    col = col.reshape(-1, 4)
    if attr.domain == "CORNER":   # per loop -> per vertex (glTF import may use either domain)
        lv = np.empty(len(me.loops), dtype=np.int64)
        me.loops.foreach_get("vertex_index", lv)
        per_v = np.ones((len(me.vertices), 4))
        per_v[lv] = col
        col = per_v
    v = _verts(objs[0])
    body = v[:, 1] > 0.05
    assert np.allclose(col[body, :3], 1.0, atol=1e-3), col[body][:3]
    clasp = v[:, 1] < -0.005
    assert np.all(col[clasp, 2] < 0.9), (col[clasp][:3], [(a.name, a.domain, a.data_type) for a in me.color_attributes])
    a, b = res["stats"]["points"]
    assert a["name"] == "a" and b["name"] == "b" and b["scale"] == 2.0
    # b is rolled 90 deg: its long axis is vertical and twice as long (0.2 m)
    pts_b = v[(v[:, 0] > 0.1) & (v[:, 1] < -0.001)]
    ext = pts_b.max(0) - pts_b.min(0)
    assert abs(ext[2] - 0.2) < 2e-3 and ext[0] < 0.085, ext
    assert abs(pts_b[:, 1].min() + 0.04) < 1e-3, pts_b[:, 1].min()   # 2 cm thick x 2, back face on y = 0
    uvs = [l.name for l in me.uv_layers]
    assert uvs == ["UVMap"], uvs


def test_target_objects_filter_snap_none_and_errors():
    d = _tmp()
    part = _part(os.path.join(d, "part.glb"))
    target = _save(os.path.join(d, "parts.glb"),
                   [("body", lambda bm: _box(bm, (-0.5, 0.0, -0.5), (0.5, 0.4, 0.5))),
                    ("old_staff", lambda bm: _box(bm, (0.6, -0.1, -0.5), (0.65, -0.05, 0.5)))])
    res = _run({"mesh": target, "part": part},
               {"target_objects": ["body"], "join": False, "preview": False,
                "points": [{"at": [0.0, 0.1, 0.2], "snap": "none", "rot_deg": [0, 0, 90], "name": "posed"}]})
    assert res["ok"], res
    assert res["stats"]["target_objects_dropped"] == ["old_staff"]
    objs = {o.name: o for o in _load(res["outputs"]["glb"])}
    assert set(objs) == {"body", "posed"}, objs.keys()
    v = _verts(objs["posed"])
    ext = v.max(0) - v.min(0)
    # rotated 90 deg about Z: the 10 cm axis is along Y now, origin at (0, 0.1, 0.2)
    assert abs(ext[1] - 0.10) < 1e-4 and abs(ext[0] - 0.02) < 1e-4, ext
    assert np.allclose(objs["posed"].location, (0, 0.1, 0.2), atol=1e-6)
    # a ray that misses -> points_hit error, step not ok
    miss = _run({"mesh": target, "part": part}, {"points": [[3.0, 0.0]], "preview": False})
    assert not miss["ok"] and not _check(miss, "points_hit")["ok"]
    for bad, msg in (({"points": []}, "points"), ({"points": [[0, 0]], "ray": "up"}, "ray"),
                     ({"points": [{"at": [0, 0], "snap": "none"}]}, "3D point"),
                     ({"points": [[0, 0]], "target_objects": ["nope"]}, "target_objects")):
        try:
            _run({"mesh": target, "part": part}, dict(bad, preview=False))
        except ValueError as exc:
            assert msg in str(exc), (msg, exc)
        else:
            raise AssertionError("no error for %s" % bad)


def test_part_frame_warning_and_3d_surface_point():
    d = _tmp()

    def bad(bm):   # geometry behind the back face (local +Y up to 3 cm)
        _box(bm, (-0.05, -0.02, -0.015), (0.05, 0.03, 0.015))
    part = _save(os.path.join(d, "bad.glb"), [("bad", bad)])
    target = _save(os.path.join(d, "t.glb"), [("t", lambda bm: _box(bm, (-0.5, 0.0, -0.5), (0.5, 0.4, 0.5)))])
    # 3D point slightly in front of the surface: the nearest hit on the line through it is used
    res = _run({"mesh": target, "part": part}, {"points": [[0.1, -0.05, 0.0]], "preview": False})
    assert res["ok"]
    assert not _check(res, "part_frame")["ok"]
    assert abs(res["stats"]["points"][0]["position"][1]) < 1e-4


def test_part_and_target_sockets_survive():
    """A part with its own socket (like the socket_mount_hand_l of a bone_kit hand) keeps it with every
    copy (unique names), and the target's sockets (socket_fx_eye_l of replace_part) are kept."""
    d = _tmp()
    af.reset_scene()
    me = bpy.data.meshes.new("hand")
    bm = bmesh.new()
    _box(bm, (-0.03, -0.02, -0.1), (0.03, 0.0, 0.0))
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new("hand", me)
    bpy.context.scene.collection.objects.link(ob)
    s = af.add_socket("socket_mount_hand_l", location=(0.0, -0.01, -0.05), rotation=(math.radians(90), 0, 0),
                      kind="mount", grip_radius_m=0.02)
    part = af.export_glb(os.path.join(d, "hand.glb"), objects=[ob, s], animations=False, skins=False)
    af.reset_scene()
    me = bpy.data.meshes.new("body")
    bm = bmesh.new()
    _box(bm, (-0.5, 0.0, -0.5), (0.5, 0.4, 0.5))
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new("body", me)
    bpy.context.scene.collection.objects.link(ob)
    e = af.add_socket("socket_fx_eye_l", location=(0.1, 0.05, 0.4), kind="fx")
    target = af.export_glb(os.path.join(d, "body.glb"), objects=[ob, e], animations=False, skins=False)
    res = _run({"mesh": target, "part": part},
               {"preview": False, "points": [
                   {"at": [0.2, -0.1, 0.0], "snap": "none", "rot_deg": [0, 0, 90], "name": "a"},
                   {"at": [-0.2, -0.1, 0.0], "snap": "none", "name": "b"}]})
    assert res["ok"], res
    assert res["stats"]["sockets"] == ["socket_fx_eye_l", "socket_mount_hand_l", "socket_mount_hand_l_2"], \
        res["stats"]["sockets"]
    af.reset_scene()
    objs = af.import_any(res["outputs"]["glb"])
    socks = {o.get("game_socket", o.name): o for o in objs if o.type == "EMPTY"}
    assert set(socks) == {"socket_fx_eye_l", "socket_mount_hand_l", "socket_mount_hand_l_2"}, socks.keys()
    assert (socks["socket_fx_eye_l"].matrix_world.translation - Vector((0.1, 0.05, 0.4))).length < 1e-4
    # copy a: rotated 90 deg about Z at (0.2, -0.1, 0): local (0, -0.01, -0.05) -> (0.21, -0.1, -0.05)
    a = socks["socket_mount_hand_l"].matrix_world
    assert (a.translation - Vector((0.21, -0.1, -0.05))).length < 1e-4, a.translation
    z = a.to_3x3().normalized() @ Vector((0, 0, 1))
    assert (z - Vector((1.0, 0.0, 0.0))).length < 1e-3, z    # local +Z: -Y (x90) -> rotated about Z -> +X
    assert abs(socks["socket_mount_hand_l"].get("grip_radius_m", 0) - 0.02) < 1e-6
    b = socks["socket_mount_hand_l_2"].matrix_world
    assert (b.translation - Vector((-0.2, -0.11, -0.05))).length < 1e-4, b.translation
