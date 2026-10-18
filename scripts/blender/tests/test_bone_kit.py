"""Self-tests of the bone_kit step (skeletal hands, bone staff, bone toggle clasp).

Zero-arg test_*() functions, temp files only under tempfile.mkdtemp(); tests on real assets are
test_real_* and return "skip: ..." when the file is missing (contract §10).

    blender -b --factory-startup --python scripts/blender/tests/run_tests.py -- bone_kit

make_evidence(out_dir=None, img_dir=None) renders the comparison with the owner's reference and the
numbers / pictures of docs/forge/bone_kit.md.
"""
import json
import math
import os
import struct
import tempfile

import bpy
import numpy as np
from mathutils import Euler, Vector

import af_common as af
from steps import _bone_kit_lib as B
from steps import bone_kit as K

# regions of the reference (x0, y0, x1, y1 in its 2752 x 1536 pixels)
REF_CROPS = {"hand_open": (1040, 650, 1210, 900), "grip": (1740, 520, 1900, 660), "staff_head": (1790, 30, 1960, 420),
             "clasps": (1380, 370, 1560, 680), "staff_full": (1690, 30, 1990, 1520)}


def _tmp():
    return tempfile.mkdtemp(prefix="af_bone_kit_")


def _build(params):
    af.reset_scene()
    return K.build_part(params)


def _errors(checks):
    return [c for c in checks if not c["ok"] and c.get("severity", "error") == "error"]


def _glb_json(path):
    with open(path, "rb") as fh:
        fh.read(12)
        clen, _ = struct.unpack("<II", fh.read(8))
        return json.loads(fh.read(clen).decode("utf-8"))


def _verts(ob):
    co, _ = B.mesh_arrays(ob, world=True)
    return co


def _dist_to_axis(P, p0, d):
    d = np.asarray(d, float)
    d = d / np.linalg.norm(d)
    v = np.asarray(P, float) - np.asarray(p0, float)
    return np.linalg.norm(v - np.outer(v @ d, d), axis=1)


# =============================================================================== hand

def test_hand_bones_mixamo_names_and_hierarchy():
    for side in ("l", "r"):
        objs, info = _build({"part": "skeleton_hand", "side": side})
        names = [b["name"] for b in info["bones"]]
        assert sorted(names) == sorted(K.hand_bone_names(side)), names
        hand = "mixamorig:%sHand" % K.SIDE_WORD[side]
        finger = [n for n in names if n != hand]
        # contract §5: {Thumb,Index,Middle,Ring,Pinky}{1..4} = 20 finger bones per hand, plus <Side>Hand
        assert len(finger) == 20 and len(names) == 21, len(names)
        arm = [o for o in objs if o.type == "ARMATURE"][0]
        for f in K.FINGERS:
            for k in range(1, 5):
                b = arm.data.bones[K.bname(side, f, k)]
                assert b.parent.name == (hand if k == 1 else K.bname(side, f, k - 1)), b.name
        assert all(n.startswith("mixamorig:") for n in names)
        assert _errors(K.part_checks(info)) == []


def test_hand_rigid_weights_every_piece_exactly_one_bone():
    objs, info = _build({"part": "skeleton_hand", "pose": "relaxed", "rings": ["thumb", "index", "ring"]})
    ob = info["_main"]
    gname = {g.index: g.name for g in ob.vertex_groups}
    for rec in info["_records"]:
        names = set()
        for i in range(rec["v0"], rec["v1"]):
            gs = [(gname[g.group], g.weight) for g in ob.data.vertices[i].groups]
            assert len(gs) == 1 and abs(gs[0][1] - 1.0) < 1e-6, (rec["name"], gs)
            names.add(gs[0][0])
        assert names == {rec["bone"]}, (rec["name"], names)
    recs = info["_records"]
    phal = [r for r in recs if r["kind"] == "phalanx"]
    assert len(phal) == 14 and len({r["bone"] for r in phal}) == 14          # one bone per phalanx
    assert len([r for r in recs if r["kind"] == "carpal"]) == 8
    assert len([r for r in recs if r["kind"] == "metacarpal"]) == 5
    assert all(not r["bone"].endswith("4") for r in recs)                    # *4 = Mixamo end bones
    assert info["weights"]["bad_vertices"] == 0 and not info["weights"]["bad_pieces"]


def test_hand_skinning_moves_only_the_posed_finger():
    objs, info = _build({"part": "skeleton_hand", "side": "r", "pose": "open"})
    ob = info["_main"]
    arm = [o for o in objs if o.type == "ARMATURE"][0]
    rest = _verts_eval(ob)
    pb = arm.pose.bones[K.bname("r", "index", 2)]
    pb.rotation_mode = "XYZ"
    pb.rotation_euler = Euler((math.radians(40), 0, 0))
    bpy.context.view_layer.update()
    posed = _verts_eval(ob)
    moved = np.linalg.norm(posed - rest, axis=1) > 1e-6
    for rec in info["_records"]:
        m = moved[rec["v0"]:rec["v1"]]
        should = rec["name"] in ("index_middle", "index_distal")
        assert (m.all() if should else not m.any()), rec["name"]


def _verts_eval(ob):
    dg = bpy.context.evaluated_depsgraph_get()
    ev = ob.evaluated_get(dg)
    me = ev.to_mesh()
    co = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", co)
    ev.to_mesh_clear()
    return co.reshape(-1, 3)


def test_hand_dimensions_scale_and_budget():
    objs, info = _build({"part": "skeleton_hand", "pose": "open", "curl": 0.0, "finger_spread_deg": 0.0})
    assert abs(info["chain_length_m"] - 0.19) < 1e-4, info["chain_length_m"]
    rec = next(r for r in info["_records"] if r["name"] == "middle_distal")
    tip = np.linalg.norm(_verts(info["_main"])[rec["v0"]:rec["v1"]], axis=1).max()
    assert abs(tip - 0.19) < 0.19 * 0.015, tip                     # wrist crease (origin) -> middle fingertip
    assert info["tris"] <= 3000
    big = _build({"part": "skeleton_hand", "pose": "open", "curl": 0.0, "finger_spread_deg": 0.0, "length_m": 0.25})[1]
    for a, b in zip(info["dimensions_m"], big["dimensions_m"]):
        assert abs(b / a - 0.25 / 0.19) < 0.01, (a, b)
    # proportions: middle finger (MCP -> tip) ~ 0.43-0.47 of the hand length, thumb tip beside the index
    full = _build({"part": "skeleton_hand", "rings": list(K.FINGERS)})[1]
    assert full["tris"] <= 3000, full["tris"]                       # every finger with a ring still fits
    for pose in K.POSES:
        inf = _build({"part": "skeleton_hand", "pose": pose, "side": "l"})[1]
        assert inf["tris"] <= 3000, (pose, inf["tris"])
        assert _errors(K.part_checks(inf)) == [], (pose, _errors(K.part_checks(inf)))


def test_hand_socket_frame_contract():
    for side in ("l", "r"):
        for pose in ("open", "grip"):
            objs, info = _build({"part": "skeleton_hand", "side": side, "pose": pose})
            s = info["sockets"][0]
            assert s["name"] == "socket_mount_hand_%s" % side
            assert s["parent_bone"] == "mixamorig:%sHand" % K.SIDE_WORD[side]
            ortho, hand = B.frame_errors(s)
            assert ortho < 1e-4 and abs(hand - 1.0) < 1e-4, (ortho, hand)
            # +Z out of the fist on the thumb side (thumb points to character forward -Y in the hand frame),
            # -Y along the knuckles (fingers hang along -Z), X = Y x Z
            assert Vector(s["z_axis"]).dot(Vector((0, -1, 0))) > 0.999, s["z_axis"]
            assert Vector(s["y_axis"]).dot(Vector((0, 0, 1))) > 0.999, s["y_axis"]
            # the socket sits on the palm side of the hand: right palm +X, left palm -X
            palm = 1.0 if side == "r" else -1.0
            assert s["location"][0] * palm > 0.01, s["location"]


def test_hand_grip_wraps_cylinder():
    for R in (0.018, 0.025):
        objs, info = _build({"part": "skeleton_hand", "side": "l", "pose": "grip", "grip_radius_m": R,
                             "rings": ["index", "middle", "ring"]})
        ax = info["grip_axis"]
        co = _verts(info["_main"])
        bones = [r for r in info["_records"] if r["kind"] in ("phalanx", "metacarpal", "ring")]
        dmin = min(_dist_to_axis(co[r["v0"]:r["v1"]], ax["point"], ax["direction"]).min() for r in bones)
        assert dmin >= R - 0.0006, (R, dmin)                                   # nothing cuts into the shaft
        for f in ("index", "middle", "ring", "pinky"):
            r = next(x for x in info["_records"] if x["name"] == f + "_distal")
            d = _dist_to_axis(co[r["v0"]:r["v1"]], ax["point"], ax["direction"]).min()
            assert d - R < 0.004, (R, f, d - R)                                # fingertips wrap onto it
            ang = info["joint_angles_deg"][f]
            assert ang[1] + ang[2] + ang[3] > 70, (f, ang)
        s = info["sockets"][0]
        assert _dist_to_axis([s["location"]], ax["point"], ax["direction"])[0] < 1e-4
        assert abs(Vector(s["z_axis"]).dot(Vector(ax["direction"]))) > 0.9999
        assert info["overlaps"] == [], info["overlaps"]
        assert _errors(K.part_checks(info)) == []


def test_hand_poses_distinct_and_clean():
    tips = {}
    for pose in K.POSES:
        objs, info = _build({"part": "skeleton_hand", "pose": pose, "side": "r", "seed": 2})
        assert info["overlaps"] == [], (pose, info["overlaps"])
        co = _verts(info["_main"])
        r = next(x for x in info["_records"] if x["name"] == "middle_distal")
        tips[pose] = co[r["v0"]:r["v1"]].mean(0)
    # curled poses bring the fingertip closer to the wrist
    d = {k: float(np.linalg.norm(v)) for k, v in tips.items()}
    assert d["fist"] < d["claw"] < d["open"] and d["relaxed"] < d["open"], d
    assert d["fist"] < 0.6 * d["open"], d


def test_hand_mirror_symmetry_and_seed():
    l = _verts(_build({"part": "skeleton_hand", "side": "l", "seed": 5})[1]["_main"])
    r = _verts(_build({"part": "skeleton_hand", "side": "r", "seed": 5})[1]["_main"])
    assert l.shape == r.shape
    assert np.allclose(l * np.array([-1, 1, 1]), r, atol=1e-6)
    l2 = _verts(_build({"part": "skeleton_hand", "side": "l", "seed": 5})[1]["_main"])
    l3 = _verts(_build({"part": "skeleton_hand", "side": "l", "seed": 6})[1]["_main"])
    assert np.allclose(l, l2) and not np.allclose(l, l3)


# =============================================================================== staff

def test_staff_dimensions_budget_sockets():
    objs, info = _build({"part": "bone_staff"})
    L = 2.05
    assert abs(info["dimensions_m"][2] - L) < 0.005 * L, info["dimensions_m"]
    assert info["tris"] <= 4500, info["tris"]
    socks = {s["name"]: s for s in info["sockets"]}
    g = socks["socket_grip_main"]
    assert abs(g["location"][2] / L - 0.62) < 0.003, g["location"]
    for s in info["sockets"]:
        ortho, hand = B.frame_errors(s)
        assert ortho < 1e-4 and abs(hand - 1) < 1e-4
    assert Vector(g["z_axis"]).z > 0.995 and Vector(g["y_axis"]).y > 0.99      # item frame: +Z up, -Y front
    # the grip is on the shaft centre line, on a constant-radius stretch away from the joints
    assert abs(info["grip_shaft_radius_m"] - 0.018) < 0.0027, info["grip_shaft_radius_m"]
    assert min(abs(z - g["location"][2]) for z in info["joints_z"]) > 0.06, info["joints_z"]
    # socket_fx_tip at the crystal centre, crystal 0.18..0.24 m tall
    fx = socks["socket_fx_tip"]
    assert (Vector(fx["location"]) - Vector(info["crystal"]["centre"])).length < 1e-4
    assert 0.18 <= info["crystal"]["height_m"] <= 0.24
    ob = info["_main"]
    cr = next(r for r in info["_records"] if r["name"] == "crystal")
    pts = _verts(ob)[cr["v0"]:cr["v1"]]
    assert pts[:, 2].min() < fx["location"][2] < pts[:, 2].max()
    assert _errors(K.part_checks(info)) == []


def test_staff_materials_crystal_emissive_and_ivory():
    objs, info = _build({"part": "bone_staff"})
    mats = {m["name"]: m for m in info["materials"]}
    assert set(mats) == {"Bone", "Crystal"}, list(mats)
    cm = mats["Crystal"]
    assert cm["emission_strength"] > 0 and cm["roughness"] <= 0.2 and cm["principled_to_output"]
    r, g, b = cm["emission_color_linear"]
    assert g > r * 1.5 and g > b * 1.5                                          # green glow
    bm = mats["Bone"]
    assert np.allclose(bm["base_color_linear"], B.hex_lin("#CDBE9C")[:3], atol=1e-3)
    assert abs(bm["roughness"] - 0.7) < 1e-6 and bm["metallic"] == 0.0
    ob = info["_main"]
    names = [s.material.name for s in ob.material_slots]
    assert "Crystal" in names
    idx = names.index("Crystal")
    flat = [p for p in ob.data.polygons if p.material_index == idx]
    assert flat and not any(p.use_smooth for p in flat)                       # faceted crystal
    assert len(flat) < len(ob.data.polygons)


def test_staff_variants_and_grip_fraction():
    for p in ({"grip_fraction": 0.5, "length_m": 1.7}, {"segments": 4, "talons": 3, "seed": 7},
              {"crystal_height_m": 0.30, "bend": 2.0, "seed": 3}):
        params = dict(p, part="bone_staff")
        objs, info = _build(params)
        L = params.get("length_m", 2.05)
        g = next(s for s in info["sockets"] if s["name"] == "socket_grip_main")
        assert abs(g["location"][2] / L - params.get("grip_fraction", 0.62)) < 0.003, (p, g["location"])
        assert info["tris"] <= 4500 and abs(info["dimensions_m"][2] - L) < 0.005 * L, (p, info["tris"])
        assert 0.18 <= info["crystal"]["height_m"] <= 0.24                         # clamped
        assert _errors(K.part_checks(info)) == [], (p, _errors(K.part_checks(info)))
    a = _verts(_build({"part": "bone_staff", "seed": 1})[1]["_main"])
    b = _verts(_build({"part": "bone_staff", "seed": 1})[1]["_main"])
    c = _verts(_build({"part": "bone_staff", "seed": 2})[1]["_main"])
    assert np.allclose(a, b) and (a.shape != c.shape or not np.allclose(a, c))


# =============================================================================== toggle

def test_toggle_dimensions_origin_budget_gold():
    for gold in ("rings", "caps", "none"):
        objs, info = _build({"part": "bone_toggle", "gold": gold})
        assert info["tris"] <= 600, (gold, info["tris"])
        assert abs(info["dimensions_m"][0] - 0.10) < 0.012, info["dimensions_m"]
        lo, hi = info["bbox_min"], info["bbox_max"]
        assert abs(hi[1]) < 1e-5 and lo[1] < -0.015                         # back face at y = 0, body toward -Y
        assert abs(lo[0] + hi[0]) < 1e-4 and abs(lo[2] + hi[2]) < 1e-4      # centred on the back face
        gm = [m for m in info["materials"] if m["name"] == "Gold"]
        assert gm and gm[0]["metallic"] == 1.0 and gm[0]["roughness"] <= 0.4   # the loop is gold too
        assert _errors(K.part_checks(info)) == []
    big = _build({"part": "bone_toggle", "length_m": 0.14})[1]
    assert abs(big["dimensions_m"][0] - 0.14) < 0.017


# =============================================================================== export / protocol

def test_run_protocol_hand_glb_skin_and_socket():
    out = _tmp()
    res = K.run({"params": {"part": "skeleton_hand", "side": "r", "pose": "grip", "rings": ["index"]},
                 "out_dir": out, "inputs": {}})
    assert not _errors(res["checks"]), _errors(res["checks"])
    for k in ("glb", "blend", "json", "preview"):
        assert os.path.isfile(res["outputs"][k]), k
    g = _glb_json(res["outputs"]["glb"])
    assert len(g["skins"]) == 1
    joints = [g["nodes"][j]["name"] for j in g["skins"][0]["joints"]]
    assert sorted(joints) == sorted(K.hand_bone_names("r"))
    sock = next(i for i, n in enumerate(g["nodes"]) if (n.get("extras") or {}).get("game_socket") == "socket_mount_hand_r")
    parent = next(n["name"] for n in g["nodes"] if sock in (n.get("children") or []))
    assert parent == "mixamorig:RightHand"
    prim = g["meshes"][0]["primitives"][0]
    assert "JOINTS_0" in prim["attributes"] and "WEIGHTS_0" in prim["attributes"]
    with open(res["outputs"]["json"], encoding="utf-8") as fh:
        pj = json.load(fh)
    assert len(pj["bones"]) == 21 and pj["sockets"][0]["name"] == "socket_mount_hand_r"


def test_run_protocol_staff_and_toggle_outputs():
    for params in ({"part": "bone_staff"}, {"part": "bone_toggle", "gold": "caps"}):
        out = _tmp()
        res = K.run({"params": params, "out_dir": out, "inputs": {}})
        assert not _errors(res["checks"]), _errors(res["checks"])
        for k in ("glb", "blend", "json", "preview"):
            assert os.path.isfile(res["outputs"][k]), k
        g = _glb_json(res["outputs"]["glb"])
        socks = {(n.get("extras") or {}).get("game_socket") for n in g["nodes"]} - {None}
        if params["part"] == "bone_staff":
            assert {"socket_grip_main", "socket_grip_off", "socket_fx_tip"} <= socks
            assert "Crystal" in [m["name"] for m in g["materials"]]
            em = next(m for m in g["materials"] if m["name"] == "Crystal")
            assert max(em.get("emissiveFactor", [0, 0, 0])) > 0
        else:
            assert "Gold" in [m["name"] for m in g["materials"]]


def test_aged_vertex_colours_export_as_color0():
    out = _tmp()
    res = K.run({"params": {"part": "skeleton_hand", "style": "aged", "rings": ["index"], "preview": False},
                 "out_dir": out, "inputs": {}})
    g = _glb_json(res["outputs"]["glb"])
    mats = [m["name"] for m in g["materials"]]
    prims = g["meshes"][0]["primitives"]
    bone = next(p for p in prims if mats[p["material"]] == "Bone_aged")
    assert "COLOR_0" in bone["attributes"]
    bm = g["materials"][bone["material"]]
    assert bm.get("pbrMetallicRoughness", {}).get("baseColorFactor", [1, 1, 1, 1])[:3] == [1, 1, 1]
    # the .blend keeps white vertex colours on gold (glTF multiplies COLOR_0 into every primitive)
    af.reset_scene()
    objs, info = K.build_part({"part": "skeleton_hand", "style": "aged", "rings": ["index"]})
    ob = info["_main"]
    attr = ob.data.color_attributes["Color"]
    col = np.empty(len(attr.data) * 4)
    attr.data.foreach_get("color", col)
    col = col.reshape(-1, 4)
    ring = next(r for r in info["_records"] if r["material"] == "gold")
    assert np.allclose(col[ring["v0"]:ring["v1"], :3], 1.0)
    bonev = col[[i for r in info["_records"] if r["material"] == "bone" for i in range(r["v0"], r["v1"])], :3]
    assert bonev.min() < 0.6 * bonev.max()                                    # darker crevices / stains
    # clean style: no vertex colours at all
    res2 = K.run({"params": {"part": "bone_toggle", "preview": False}, "out_dir": _tmp(), "inputs": {}})
    g2 = _glb_json(res2["outputs"]["glb"])
    assert all("COLOR_0" not in p["attributes"] for m in g2["meshes"] for p in m["primitives"])


def test_uv_and_principled_materials_all_parts():
    for params in ({"part": "skeleton_hand", "rings": ["thumb"]}, {"part": "bone_staff"}, {"part": "bone_toggle"},
                   {"part": "bone_staff", "style": "aged"}):
        objs, info = _build(params)
        assert info["uv_layers"] == ["UVMap"], params
        assert info["materials"] and all(m["principled_to_output"] for m in info["materials"]), params
        assert B.non_manifold_edges(info["_main"]) == 0, params


def test_batch_contact_sheet_and_safe_names():
    out = _tmp()
    res = K.run({"params": {"batch": [{"part": "skeleton_hand", "pose": "grip", "side": "l"},
                                      {"part": "bone_staff", "name": "../../escape"},
                                      {"part": "bone_toggle", "gold": "caps"}], "preview": False},
                 "out_dir": out, "inputs": {}})
    assert not _errors(res["checks"]), _errors(res["checks"])
    o = res["outputs"]
    assert os.path.isfile(o["contact_sheet"]) and len(o["glb"]) == 3
    for p in o["glb"]:
        assert os.path.commonpath([os.path.realpath(out), os.path.realpath(p)]) == os.path.realpath(out)
    with open(o["json"], encoding="utf-8") as fh:
        b = json.load(fh)
    assert [v["part"] for v in b["variants"]] == ["skeleton_hand", "bone_staff", "bone_toggle"]
    res2 = K.run({"params": {"part": "bone_toggle", "batch": 3, "seed": 10, "preview": False}, "out_dir": _tmp(),
                  "inputs": {}})
    assert res2["stats"]["variants"] == 3


def test_bad_params_raise():
    for p in ({"part": "skull"}, {"part": "skeleton_hand", "pose": "wave"}, {"part": "skeleton_hand", "side": "x"},
              {"part": "bone_staff", "segments": 5}, {"part": "bone_toggle", "gold": "silver"}):
        try:
            _build(p)
        except ValueError:
            continue
        raise AssertionError("no error for %r" % p)


# =============================================================================== real asset


# =============================================================================== evidence

def _small_png(arr, path, max_kb=400):
    """Save an RGBA top-down array as PNG under max_kb (quantised colours, then downscale if needed)."""
    a = arr.copy()
    for levels in (64, 48, 32, 24):
        q = a.copy()
        q[..., :3] = np.round(q[..., :3] * (levels - 1)) / (levels - 1)
        q[..., 3] = 1.0
        B.save_png(q, path)
        if os.path.getsize(path) <= max_kb * 1024:
            return path
    while os.path.getsize(path) > max_kb * 1024:
        h, w = a.shape[:2]
        a = B.resize(a, int(w * 0.85), int(h * 0.85))
        q = a.copy()
        q[..., :3] = np.round(q[..., :3] * 31) / 31
        q[..., 3] = 1.0
        B.save_png(q, path)
    return path


