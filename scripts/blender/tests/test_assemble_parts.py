"""Self-tests of the assemble_parts step and _parts_lib (docs/forge/parts.md).

A synthetic character (two leg boxes, a torso box, two T-pose arm boxes, a sphere head) is voxel-remeshed
into one closed "base" mesh; its front silhouette is splatted through a known orthographic camera into an
RGBA "reference". Part meshes are made in the generator's frame (bbox centred, long side 2) with deliberate
errors - a wrong aspect, a thinner depth, a turn by 180 deg - and the step has to put them back: scale and
shift from the silhouette, depth from the base, joints cut, missing parts filled from the base. Run:

    blender -b --factory-startup --python scripts/blender/tests/run_tests.py -- assemble_parts
"""
import importlib
import json
import math
import os
import tempfile

import bmesh
import bpy
import mathutils
import numpy as np

import af_common as af
from steps import _mesh_lib as ml
from steps import _paint_lib as pl
from steps import _parts_lib as pt

S_PX = 400.0           # reference camera: px per metre
SIZE = 800             # reference image side, px
CZ = 0.9               # camera centre height, m
TX = TY = 400.0


def _tmp():
    return tempfile.mkdtemp(prefix="af_parts_")


def _add_box(bm, lo, hi):
    before = set(bm.verts)
    bmesh.ops.create_cube(bm, size=1.0)
    new = [v for v in bm.verts if v not in before]
    bmesh.ops.scale(bm, vec=tuple(h - l for l, h in zip(lo, hi)), verts=new)
    bmesh.ops.translate(bm, vec=tuple((l + h) / 2 for l, h in zip(lo, hi)), verts=new)


def _add_sphere(bm, centre, radius, u=48, v=24):
    before = set(bm.verts)
    bmesh.ops.create_uvsphere(bm, u_segments=u, v_segments=v, radius=radius)
    new = [x for x in bm.verts if x not in before]
    bmesh.ops.translate(bm, vec=centre, verts=new)


def _mesh_object(name, bm):
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    return ob


def _base(lobe=True):
    """Closed union of boxes + sphere head (1.8 m, T-pose); a lobe behind the head breaks the front / back symmetry."""
    bm = bmesh.new()
    _add_box(bm, (-0.16, -0.07, 0.0), (-0.02, 0.07, 0.82))
    _add_box(bm, (0.02, -0.07, 0.0), (0.16, 0.07, 0.82))
    _add_box(bm, (-0.2, -0.1, 0.8), (0.2, 0.1, 1.4))
    _add_box(bm, (0.18, -0.05, 1.25), (0.85, 0.05, 1.4))
    _add_box(bm, (-0.85, -0.05, 1.25), (-0.18, 0.05, 1.4))
    _add_sphere(bm, (0, 0, 1.6), 0.2)
    if lobe:
        _add_sphere(bm, (0, 0.2, 1.62), 0.12, 24, 12)
    ob = _mesh_object("Base", bm)
    ml.voxel_remesh(ob, 0.01)
    ml.triangulate_object(ob)
    ml.recalc_normals(ob)
    return ob


def _camera(size=SIZE):
    return pl.OrthoCam(0.0, 0.0, S_PX, TX, TY, (size, size), 0.0, (0.0, 0.0, CZ))


def _concept(path, base_ob):
    """RGBA reference: alpha = silhouette of the base through the known camera (exact triangle raster)."""
    V, T = pt.world_arrays(base_ob)
    cam = _camera()
    sil = pt.silhouette(V, T, cam, (SIZE, SIZE), factor=1)
    img = np.zeros((SIZE, SIZE, 4), np.float32)
    img[sil] = (0.6, 0.55, 0.5, 1.0)
    ml.save_png(path, img[::-1])
    return sil


def _px_box(x0, x1, z0, z1):
    u0, u1 = S_PX * x0 + TX, S_PX * x1 + TX
    v0, v1 = -S_PX * (z1 - CZ) + TY, -S_PX * (z0 - CZ) + TY
    return [int(math.floor(u0)), int(math.floor(v0)), int(math.ceil(u1)) - 1, int(math.ceil(v1)) - 1]


# world boxes of the five parts (m): neighbours overlap in slabs (the margins of scripts/parts)
BOXES = {"head": (-0.25, 0.25, 1.4, 1.84), "torso": (-0.26, 0.26, 0.72, 1.54), "arm_l": (0.1, 0.9, 1.2, 1.45),
         "arm_r": (-0.9, -0.1, 1.2, 1.45), "legs": (-0.2, 0.2, -0.02, 0.88)}


def _head_part(path, flipped=False, stretch=1.1, thin=0.85):
    """The head as the generator would give it: bbox centred, long side 2, X too wide (aspect error), depth
    thinner than the base's, optionally turned 180 deg about Z (lobe in front)."""
    bm = bmesh.new()
    _add_sphere(bm, (0, 0, 0), 1.0, 64, 32)
    _add_sphere(bm, (0, 0.9, 0.05), 0.6, 32, 16)
    ob = _mesh_object("head_part", bm)
    me = ob.data
    M = mathutils.Matrix.Diagonal((stretch, thin, 1.0, 1.0))
    me.transform(M)
    if flipped:
        me.transform(mathutils.Matrix.Rotation(math.pi, 4, "Z"))
    af.export_glb(path, objects=[ob], animations=False, skins=False)
    return path


def _bbox(ob):
    lo, hi = af.world_bbox([ob])
    return np.array(lo), np.array(hi)


def _setup(tmp, flipped=False):
    base = _base()
    base_glb = os.path.join(tmp, "base.glb")
    concept = os.path.join(tmp, "front.png")
    _concept(concept, base)
    af.export_glb(base_glb, objects=[base], animations=False, skins=False)
    head = _head_part(os.path.join(tmp, "head.glb"), flipped)
    parts = [{"name": "head", "mesh": head, "box": _px_box(*BOXES["head"])}]
    for n in ("torso", "arm_l", "arm_r", "legs"):
        parts.append({"name": n, "mesh": None, "box": _px_box(*BOXES[n])})
    return base_glb, concept, parts


def _run(inputs, params, out_dir):
    mod = importlib.import_module("steps.assemble_parts")
    req = {"step": "assemble_parts", "inputs": inputs, "params": dict({"preview": False}, **params), "out_dir": out_dir}
    os.makedirs(out_dir, exist_ok=True)
    return af.finish(req, mod.run(req))


def _check(res, name):
    for c in res["checks"]:
        if c["name"] == name:
            return c
    raise AssertionError("check %s missing: %s" % (name, [c["name"] for c in res["checks"]]))


# --------------------------------------------------------------------------- library
def test_front_camera_fit_recovers_the_reference_camera():
    base = _base()
    V, T = pt.world_arrays(base)
    sil = pt.silhouette(V, T, _camera(), (SIZE, SIZE), factor=1)
    cam, iou = pt.fit_front_camera(V, T, sil)
    assert iou > 0.97, iou
    assert abs(cam.s - S_PX) / S_PX < 0.01, cam.s
    # a pixel box maps back to metres within a pixel
    wb = pt.px_box_to_world(cam, _px_box(*BOXES["head"]))
    assert max(abs(a - b) for a, b in zip(wb, BOXES["head"])) < 0.01, wb


def test_joint_cuts_find_slabs_and_ignore_corner_touches():
    boxes = {k: BOXES[k] for k in ("head", "torso", "arm_l", "arm_r", "legs")}
    cuts, joints = pt.joint_cuts(boxes, 0.006, 1.8)
    pairs = sorted(tuple(sorted(j["parts"])) for j in joints)
    assert pairs == [("arm_l", "torso"), ("arm_r", "torso"), ("head", "torso"), ("legs", "torso")], pairs
    # the arms give up their inner end, the torso its sleeve stubs; a part never loses more than the slab
    assert any(abs(c[0] - 0.1) < 1e-6 or abs(c[1] - 0.26) < 1e-6 for c in cuts["torso"] + cuts["arm_l"])
    for name, lst in cuts.items():
        b = boxes[name]
        for c in lst:
            assert b[0] - 1e-9 <= c[0] < c[1] <= b[1] + 1e-9 and b[2] - 1e-9 <= c[2] < c[3] <= b[3] + 1e-9, (name, c)
    # a helmet bust touching a sleeve corner is no joint
    corner = {"head": (-0.25, 0.25, 1.4, 1.84), "arm_l": (0.22, 0.9, 1.2, 1.45)}
    assert pt.joint_cuts(corner, 0.006, 1.8)[1] == []


def test_pick_joint_line_chooses_where_the_cross_sections_agree():
    rng = np.random.default_rng(1)

    def tube(x0, x1, radius, n=6000):
        x = rng.uniform(x0, x1, n)
        a = rng.uniform(0, 2 * math.pi, n)
        return np.stack([x, radius * np.cos(a), radius * np.sin(a)], axis=1)
    A = np.concatenate([tube(0.0, 0.3, 0.05), tube(0.3, 0.6, 0.08)])
    B = tube(0.2, 0.7, 0.08)
    c, mism = pt.pick_joint_line(A, B, 0, 0.2, 0.6, (0.2, 0.6, -1.0, 1.0), 0.01)
    assert 0.3 < c <= 0.54 and mism < 0.01, (c, mism)
    # no data: the middle of the slab
    c0, m0 = pt.pick_joint_line(np.zeros((3, 3)), np.zeros((3, 3)), 0, 0.2, 0.6, (0.2, 0.6, -1.0, 1.0), 0.01)
    assert abs(c0 - 0.4) < 1e-9 and m0 is None


def test_hunyuan_export_is_bbox_centred_with_long_side_two():
    base = _base()
    path = os.path.join(_tmp(), "mv.glb")
    info = pt.export_hunyuan(base, path)
    af.reset_scene()
    objs = af.import_any(path)
    lo, hi = af.world_bbox(objs)
    size = hi - lo
    assert abs(max(size) - 2.0) < 1e-4, size
    assert abs(size.z - 2.0) < 1e-4 and np.abs(np.array((lo + hi) / 2)).max() < 1e-3, (lo, hi)
    assert info["scale"] > 1.0


# --------------------------------------------------------------------------- the step
def test_head_part_is_registered_by_silhouette_and_depth_and_the_rest_comes_from_the_base():
    tmp = _tmp()
    base_glb, concept, parts = _setup(tmp)
    res = _run({"mesh": base_glb, "concept": concept}, {"parts": parts, "voxel_m": 0.01}, os.path.join(tmp, "out"))
    assert res["ok"], res.get("error")
    st = res["stats"]
    head = [p for p in st["parts"] if p["name"] == "head"][0]
    # scale per axis from the silhouette: the sphere is r = 1 in the part frame, the base's head r = 0.2
    sx, sy, sz = head["scale"]
    assert abs(sz - 0.2) / 0.2 < 0.03, head["scale"]
    assert abs(sx - 0.2 / 1.1) / (0.2 / 1.1) < 0.04, head["scale"]          # the stretched X is undone
    assert head["iou_final"] > 0.95, head["iou_final"]
    # depth: the base is thicker than the part (thin = 0.85): the scale takes the base's thickness
    assert 1.05 < head["thickness_ratio_base_part"] < 1.4, head["thickness_ratio_base_part"]
    assert abs(head["thickness_ratio_after"] - 1.0) < 0.15, head
    lo, hi = np.array(head["bbox_min"]), np.array(head["bbox_max"])
    centre = (lo + hi) / 2
    assert abs(centre[0]) < 0.01 and abs(centre[2] - 1.6) < 0.015, centre
    assert not head["flipped"]
    # the other four parts have no mesh: the base fills their boxes
    assert sorted(p["name"] for p in st["remainder"]["parts"]) == ["arm_l", "arm_r", "legs", "torso"], st["remainder"]
    assert st["remainder"]["tris"] > 1500
    assert any("no mesh" in w for w in res["warnings"])
    # result: watertight, 1.8 m, on the floor, silhouette like the reference
    assert _check(res, "watertight")["ok"] and _check(res, "height_matches")["ok"] and _check(res, "grounded")["ok"]
    assert st["iou"]["assembled_vs_reference"] > 0.95, st["iou"]
    assert os.path.isfile(res["outputs"]["glb"]) and os.path.isfile(res["outputs"]["parts_fit"])
    doc = json.load(open(res["outputs"]["parts_fit"], encoding="utf-8"))
    assert len(doc["joints"]) >= 2 and doc["parts"][0]["name"] == "head"


def test_part_generated_turned_back_to_front_is_turned_back():
    tmp = _tmp()
    base_glb, concept, parts = _setup(tmp, flipped=True)
    res = _run({"mesh": base_glb, "concept": concept}, {"parts": parts, "voxel_m": 0.01}, os.path.join(tmp, "out"))
    assert res["ok"], res.get("error")
    head = [p for p in res["stats"]["parts"] if p["name"] == "head"][0]
    assert head["flipped"], head
    assert head["residual_rms_m"] < 0.02, head["residual_rms_m"]
    assert _check(res, "part_flipped_head")["severity"] == "warning"


def test_refine_icp_and_none_modes_run_and_report_the_residual():
    tmp = _tmp()
    base_glb, concept, parts = _setup(tmp)
    for mode in ("icp", "none"):
        res = _run({"mesh": base_glb, "concept": concept}, {"parts": parts, "voxel_m": 0.012, "refine": mode},
                   os.path.join(tmp, "out_" + mode))
        assert res["ok"], (mode, res.get("error"))
        head = [p for p in res["stats"]["parts"] if p["name"] == "head"][0]
        assert head["mode"] == mode and head["residual_rms_m"] is not None
        if mode == "icp":
            assert "icp" in head and head["icp"]["iterations"] >= 1


def test_parts_json_missing_file_is_an_error_and_no_parts_means_the_voxel_remeshed_base():
    tmp = _tmp()
    base_glb, concept, _parts = _setup(tmp)
    try:
        _run({"mesh": base_glb, "concept": concept, "parts": os.path.join(tmp, "nope.json")}, {}, os.path.join(tmp, "o1"))
    except FileNotFoundError as exc:
        assert "inputs.parts" in str(exc)
    else:
        raise AssertionError("a missing parts.json must raise")
    res = _run({"mesh": base_glb, "concept": concept}, {"voxel_m": 0.012}, os.path.join(tmp, "o2"))
    assert not res["ok"] and not _check(res, "parts_found")["ok"]       # nothing placed: reported as an error check
    assert any("no parts" in w for w in res["warnings"])
    assert res["stats"]["iou"]["assembled_vs_reference"] > 0.9           # but the fallback mesh is the base
