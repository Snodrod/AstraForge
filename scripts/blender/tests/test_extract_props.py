"""Self-tests of the extract_props step and _props_lib (held props cut out of a generated humanoid).

Synthetic: the rig's procedural mannequin (_humanoid_lib.make_mannequin) holding a sword hanging from the right
fist, a staff through the left fist or a bow beside the body - one voxel-remeshed surface, fused like a Hunyuan
generation. The sword must come out as its own closed item with the fist left on the body, the staff in two
pieces bridged through the fist, the bow with its limbs; the gate, the kit replacement, the caps' winding,
the hints parser and the round trip extract_props -> rig_humanoid -> attach_prop (extracted, skin_separate:
the item comes back exactly where it was and stays rigid on the hand over a clip) are checked.
test_real_* use the batch assets on disk and return "skip: ..." when they are missing. Run:

    blender -b --factory-startup --python scripts/blender/tests/run_tests.py -- extract_props
"""
import importlib
import json
import math
import os
import shutil
import tempfile

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector

import af_common as af
from steps import _humanoid_lib as hl
from steps import _props_lib as PL

REAL_KNIGHT3 = r"C:/AstraForge/library/assets/b0de2731-c7bb-48f9-82a6-700dfb09cc1a/v001/02_paint_from_concept/painted.blend"
REAL_CAPTAIN = r"C:/AstraForge/library/assets/3fe8a674-c75f-4271-9424-d0eb8ed04f21/v001/02_paint_from_concept/painted.blend"


def _tmp():
    return tempfile.mkdtemp(prefix="af_extract_")


def _run(step, req):
    mod = importlib.import_module("steps." + step)
    req.setdefault("params", {})
    req.setdefault("inputs", {})
    req["step"] = step
    os.makedirs(req["out_dir"], exist_ok=True)
    res = mod.run(req)
    full = {"ok": True, "checks": [], "warnings": [], "outputs": {}, "stats": {}}
    full.update(res)
    failed = [c for c in full["checks"] if not c["ok"] and c.get("severity", "error") == "error"]
    full["ok"] = full.get("ok", True) and not failed
    full["failed"] = [c["name"] for c in failed]
    return full


def _mannequin_blend(d, **kw):
    af.reset_scene()
    ob, gt = hl.make_mannequin("A", **kw)
    ob.name = "Body"
    path = os.path.join(d, "mannequin.blend")
    af.save_blend(path)
    return path, gt


def _extract(d, blend, params):
    af.open_blend(blend)
    res = _run("extract_props", {"inputs": {"blend": blend}, "params": dict({"preview": False}, **params),
                                 "out_dir": os.path.join(d, "extract")})
    assert res["ok"], (res["failed"], res.get("error"))
    with open(res["outputs"]["props_json"], "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    return res, doc


def _glb_nodes(path):
    from steps import _items_lib as IL
    return IL.glb_json(path).get("nodes", [])


# ----------------------------------------------------------------------------- pure helpers

def test_hints_parser():
    assert PL.words_to_types("banner + sun scepter") == ["banner", "staff"]
    assert PL.words_to_types("two swords") == ["sword", "sword"]
    assert PL.words_to_types("loaded crossbow") == ["crossbow"]
    assert PL.words_to_types("tall longbow") == ["bow"]
    assert PL.words_to_types("unarmed") == []
    h = PL.parse_hints({"hand_weapon": "sword", "hand": "r", "shield": True,
                        "notes": "heater shield and a banner pole in the left hand"})
    assert h["by_side"]["Right"] == ["sword"] and h["shield_side"] == "Left", h
    assert h["by_side"]["Left"] == ["banner"], h     # the notes name a second held item
    h2 = PL.parse_hints({"hand_weapon": "two swords", "hand": "both"})
    assert h2["by_side"] == {"Left": ["sword"], "Right": ["sword"]}, h2
    h3 = PL.parse_hints({"hand_weapon": "banner + staff", "hand": "l"})
    assert h3["by_side"] == {"Left": ["banner"], "Right": ["staff"]}, h3
    # notes about things that are not held do not add items
    h4 = PL.parse_hints({"hand_weapon": "banner pole", "hand": "r", "notes": "sword sheathed, mace at the hip"})
    assert h4["by_side"] == {"Left": [], "Right": ["banner"]}, h4


def test_palette_from_colours():
    cols = [(np.array([0.72, 0.73, 0.75]), 0.6), (np.array([0.30, 0.18, 0.10]), 0.25), (np.array([0.80, 0.62, 0.20]), 0.15)]
    pal = PL.palette_from_colors(cols, "weapon")

    def near(hx, rgb):
        v = [int(hx[i:i + 2], 16) / 255.0 for i in (1, 3, 5)]
        return max(abs(a - b_) for a, b_ in zip(v, rgb)) < 0.01
    assert near(pal["metal"], (0.72, 0.73, 0.75)), pal     # the bright low-saturation colour
    assert near(pal["grip"], (0.30, 0.18, 0.10)), pal      # the dark warm one
    assert near(pal["trim"], (0.80, 0.62, 0.20)), pal      # gold


def test_cap_winding_both_sides_closed():
    """A cut through a closed sphere: both parts are closed and their caps face outward (positive volume)."""
    d = _tmp()
    try:
        af.reset_scene()
        bpy.ops.mesh.primitive_uv_sphere_add(segments=24, ring_count=16, radius=0.3, location=(0, 0, 0.5))
        ob = bpy.context.active_object
        lab = np.full(len(ob.data.vertices), -1)
        co = hl.mesh_verts(ob)
        lab[co[:, 2] > 0.6] = 0
        it = {"type": "staff", "hand": "r", "planes": [], "axis": None}
        objs, info = PL.cut(ob, lab, [it], 1.8)
        part = objs[0][0]
        assert part is not None
        from steps import _mesh_lib as ml
        for o in (ob, part):
            q = PL.mesh_quality(o, 1.8)
            assert q["open_edges"] == 0, (o.name, q)
            assert ml.signed_volume(o) > 0, (o.name, ml.signed_volume(o))
        assert info["body_cap_faces"] > 0 and objs[0][1]["cap_faces"] > 0
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_gate_catches_shredded_and_bad_size():
    af.reset_scene()
    bm = bmesh.new()
    rng = np.random.default_rng(1)
    for _ in range(12):
        bmesh.ops.create_cube(bm, size=0.03, matrix=Matrix.Translation(Vector(rng.uniform(-0.3, 0.3, 3).tolist())))
    me = bpy.data.meshes.new("shreds")
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new("shreds", me)
    bpy.context.scene.collection.objects.link(ob)
    it = {"type": "sword", "grip_mode": "end", "dims": {"length_m": 0.6, "width_m": 0.6, "grip_to_far_end_m": 0.5}}
    ok, nums, reasons = PL.gate(ob, it, 1.8)
    assert not ok
    assert any("shredded" in r for r in reasons), reasons
    assert any("wide" in r for r in reasons), reasons


# ----------------------------------------------------------------------------- synthetic characters

def test_mannequin_sword_extracted_fist_stays():
    """Sword hanging from the right fist: one item, gate passed, socket_grip_main in the GLB, the blade gone from
    the body, the fist still on it, the placement socket on the body with af_bone RightHand."""
    d = _tmp()
    try:
        blend, gt = _mannequin_blend(d, sword=True)
        res, doc = _extract(d, blend, {"hints": {"hand_weapon": "sword", "hand": "r"}})
        items = doc["items"]
        assert [i["id"] for i in items] == ["r_sword"], [i["id"] for i in items]
        it = items[0]
        assert it["source"] == "extracted" and it["gate"]["ok"], it["gate"]
        assert 0.75 < it["dims"]["length_m"] < 1.15, it["dims"]
        assert it["grip_mode"] == "end"
        names = {n.get("name") for n in _glb_nodes(it["files"]["glb"])}
        assert "socket_grip_main" in names, names
        # +Z of the grip frame runs down the blade (the rig's blade rule)
        assert it["z_axis"][2] < -0.8, it["z_axis"]
        # the body: no blade left along the axis below the fist, the fist still there
        af.open_blend(res["outputs"]["blend"])
        body = bpy.data.objects["Body"]
        V = hl.mesh_verts(body)
        g = np.array(it["grip_m"])
        z = np.array(it["z_axis"])
        rel = V - g
        t = rel @ z
        dist = np.linalg.norm(rel - np.outer(t, z), axis=1)
        blade_left = int(((t > 0.25) & (t < 0.8) & (dist < 0.03)).sum())
        assert blade_left == 0, blade_left
        fist = int((np.linalg.norm(rel, axis=1) < 0.06).sum())
        assert fist > 20, fist
        ps = bpy.data.objects.get("socket_prop_r_1")
        assert ps is not None and ps.get("af_bone") == "RightHand" and ps.get("af_grip_mode") == "end"
        assert (ps.matrix_world.translation - Vector(it["grip_m"])).length < 1e-3
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_mannequin_staff_two_pieces_bridged():
    """Staff through the left fist: a shaft grip, two pieces by construction (the gate counts both), the bridge
    handle through the fist, socket_fx_tip and socket_grip_off (long item)."""
    d = _tmp()
    try:
        blend, _ = _mannequin_blend(d, staff=True)
        res, doc = _extract(d, blend, {"hints": {"hand_weapon": "staff", "hand": "l"}})
        it = next(i for i in doc["items"] if i["hand"] == "l")
        assert it["type"] == "staff" and it["grip_mode"] == "shaft", (it["type"], it["grip_mode"])
        assert it["gate"]["ok"], it["gate"]
        assert it["gate"]["numbers"]["pieces_expected"] == 2
        assert it["bridge"] and it["bridge"]["radius_m"] > 0.008
        names = {n.get("name") for n in _glb_nodes(it["files"]["glb"])}
        assert {"socket_grip_main", "socket_fx_tip", "socket_grip_off"} <= names, names
        assert it["z_axis"][2] > 0.9, it["z_axis"]       # a staff: +Z up the shaft
        assert 1.6 < it["dims"]["length_m"] < 2.2, it["dims"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_replace_always_builds_kit_of_measured_size():
    d = _tmp()
    try:
        blend, _ = _mannequin_blend(d, sword=True)
        res, doc = _extract(d, blend, {"hints": {"hand_weapon": "sword", "hand": "r"}, "replace": "always"})
        it = doc["items"][0]
        assert it["source"] == "kit", it["source"]
        kp = it["kit"]["params"]
        assert kp["type"] in ("sword", "greatsword")
        assert abs(kp["blade_length"] - (it["dims"]["grip_to_far_end_m"] - 0.03)) < 0.02, (kp, it["dims"])
        assert "metal" in kp["palette"], kp["palette"]
        names = {n.get("name") for n in _glb_nodes(it["files"]["glb"])}
        assert "socket_grip_main" in names
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_round_trip_extract_rig_attach_skin_separate():
    """extract_props -> rig_humanoid (the placement socket rides the hand bone, the arm is solved on its grip) ->
    attach_prop extracted (skin_separate): the item's grip lands on its placement (< 1 mm), its mesh is skinned
    100 % to RightHand, the placement socket is gone; over a clip swinging the arm the item stays on the hand
    (clip_props_qa grip drift < 1 mm)."""
    d = _tmp()
    try:
        blend, _ = _mannequin_blend(d, sword=True)
        res, doc = _extract(d, blend, {"hints": {"hand_weapon": "sword", "hand": "r"}})
        it = doc["items"][0]
        af.open_blend(res["outputs"]["blend"])
        rig = _run("rig_humanoid", {"inputs": {"blend": res["outputs"]["blend"]},
                                    "params": {"intersections": False, "deform_test": False, "hard_surface": "off"},
                                    "out_dir": os.path.join(d, "rig")})
        assert rig["ok"], (rig["failed"], rig.get("error"))
        assert rig["stats"]["sockets_from_input"]["socket_prop_r_1"]["bone"] == "RightHand"
        assert rig["stats"]["known_grips_from_sockets"] == ["Right"]
        att = _run("attach_prop", {"inputs": {"character": rig["outputs"]["blend"], "extracted": res["outputs"]["props_json"]},
                                   "params": {"extracted_mode": "skin_separate", "clips_check": False},
                                   "out_dir": os.path.join(d, "attach")})
        assert att["ok"], (att["failed"], att.get("error"))
        a0 = att["stats"]["attachments"][0]
        assert a0["mode"] == "skin_separate" and a0["fit"] == "placement" and a0["mount"] == "socket_mount_hand_r", a0
        assert a0["rest_grip_to_hand_m"] < 0.04, a0
        af.open_blend(att["outputs"]["blend"])
        assert "socket_prop_r_1" not in bpy.data.objects
        arm = next(o for o in bpy.data.objects if o.type == "ARMATURE")
        items = [o for o in bpy.data.objects if o.type == "MESH" and o.get("af_prop_item")]
        assert len(items) == 1
        ob = items[0]
        assert [g.name for g in ob.vertex_groups] == ["mixamorig:RightHand"]
        assert any(m.type == "ARMATURE" and m.object == arm for m in ob.modifiers)
        hs = bpy.data.objects["socket_mount_hand_r"]
        Mp = Matrix(doc["items"][0]["placement_matrix"])
        # placement (extract frame) and the refit hand socket agree up to the rig's re-centring
        assert abs(hs.matrix_world.to_3x3().col[2].dot(Mp.to_3x3().col[2])) > 0.999
        # a clip: the right arm swings, the item rides the hand
        act = bpy.data.actions.new("swing")
        arm.animation_data_create()
        arm.animation_data.action = act
        pb = arm.pose.bones["mixamorig:RightArm"]
        pb.rotation_mode = "XYZ"
        for f, ang in ((1, 0.0), (6, 50.0), (12, -30.0)):
            pb.rotation_euler = (math.radians(ang), 0.0, 0.0)
            pb.keyframe_insert("rotation_euler", frame=f)
        rep = PL.clip_props_qa(frames_per_clip=6)
        c = rep["clips"]["swing"]
        assert c["max_grip_drift_m"] < 1e-3, c
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_extracted_shield_fit_over_clips():
    """An extracted shield goes on the canonical mount fitted over the clips (attach after retarget): props.json ->
    attachments with fit 'clips' for the shield only; the fit drops the death and leans the face to the front
    (front_bias): a hand hanging at the side of a -Y facing body gets a board facing out AND forward, not edge-on
    to the camera."""
    from steps import attach_prop as AP
    from steps import _items_lib as IL
    d = _tmp()
    try:
        files = {}
        for k in ("shield", "sword"):
            files[k] = os.path.join(d, k + ".glb")
            open(files[k], "wb").write(b"x")
        pj = os.path.join(d, "props.json")
        with open(pj, "w", encoding="utf-8") as fh:
            json.dump({"items": [
                {"id": "l_shield", "kind": "shield", "type": "shield", "hand": "l", "placement_socket": "socket_prop_l_1",
                 "files": {"glb": files["shield"]}},
                {"id": "r_sword", "kind": "weapon", "type": "sword", "hand": "r", "placement_socket": "socket_prop_r_1",
                 "files": {"glb": files["sword"]}}]}, fh)
        atts, _ = AP.extracted_attachments(pj, "skin_separate", "clips")
        fits = {a["extracted"]["id"]: a["fit"] for a in atts}
        assert fits == {"l_shield": "clips", "r_sword": "placement"}, fits
        atts, _ = AP.extracted_attachments(pj, "skin_separate")
        assert {a["fit"] for a in atts} == {"placement"}
        assert AP.extracted_clip_weight("death") == 0 and AP.extracted_clip_weight("idle-loop") == 2.0
        assert AP.extracted_clip_weight("attack") == 1.0
        # synthetic poses: left hand hanging at the side (x +0.3), chest above the body centre; the body is one
        # far-away triangle (no penetration term)
        from mathutils.bvhtree import BVHTree
        co = np.array([[5.0, 5.0, 5.0], [5.1, 5.0, 5.0], [5.0, 5.1, 5.0]])
        tri = np.array([[0, 1, 2]])
        bvh = BVHTree.FromPolygons(co.tolist(), tri.tolist(), all_triangles=True)
        masks = {"l": {"hand": np.zeros(3), "forearm": np.zeros(3), "arm": np.zeros(3)}}
        H = Matrix.Translation((0.3, 0.0, 0.9))
        pose = lambda clip: {"clip": clip, "frame": 1, "H": {"l": H.copy()}, "chest": Vector((0.0, 0.0, 1.3)),
                             "co": co, "tri": tri, "bvh": bvh}
        poses = [pose("rest"), pose("idle-loop"), pose("walk-loop"), pose("death")]
        proxy = {"hand": "l", "co_g": np.zeros((3, 3)), "tri_g": tri, "is_shield": True, "up_g": IL.CANON_UP,
                 "face_g": IL.CANON_FACE}
        for bias, want in ((0.0, Vector((1, 0, 0))), (1.0, Vector((1, -1, 0)).normalized())):
            R, info = IL.fit_rotation(proxy, poses, "clips", Matrix.Identity(4), 1.0, masks, w_rest=0.05,
                                      clip_weight=AP.extracted_clip_weight, front_bias=bias)
            assert info["poses"] == 3, info          # the death is dropped
            face = (H.to_3x3() @ R.to_3x3() @ IL.CANON_FACE).normalized()
            up = (H.to_3x3() @ R.to_3x3() @ IL.CANON_UP).normalized()
            assert face.dot(want) > 0.95 and up.z > 0.95, (bias, face, up)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_no_props_passes_through():
    """A mannequin without props: no item, the body unchanged in size, attach_prop extracted is a pass-through."""
    d = _tmp()
    try:
        blend, _ = _mannequin_blend(d)
        res, doc = _extract(d, blend, {"hints": {"hand_weapon": "unarmed", "hand": "none"}})
        assert doc["items"] == []
        assert res["outputs"]["items"] == []
        af.open_blend(res["outputs"]["blend"])
        rig = _run("rig_humanoid", {"inputs": {"blend": res["outputs"]["blend"]},
                                    "params": {"intersections": False, "deform_test": False},
                                    "out_dir": os.path.join(d, "rig")})
        assert rig["ok"], rig["failed"]
        att = _run("attach_prop", {"inputs": {"character": rig["outputs"]["blend"], "extracted": res["outputs"]["props_json"]},
                                   "params": {"extracted_mode": "skin_separate"}, "out_dir": os.path.join(d, "attach")})
        assert att["ok"] and att["stats"]["attachments"] == [], att
        assert os.path.exists(att["outputs"]["blend"])
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ----------------------------------------------------------------------------- real assets

def test_real_knight3_sword_and_shield():
    """Knight #3 of the 11.10 batch (painted, 40k tris): sword and kite shield extracted (gate passed), the sword
    0.8-1.1 m with its guard, the shield a board (thickness < 0.3 x size)."""
    if not os.path.exists(REAL_KNIGHT3):
        return "skip: " + REAL_KNIGHT3
    d = _tmp()
    try:
        res, doc = _extract(d, REAL_KNIGHT3, {"front_prior": "-Y", "hints": {"hand_weapon": "sword", "hand": "r", "shield": True,
                                                                              "faction": "order", "build": "armored"}})
        ids = sorted(i["id"] for i in doc["items"])
        assert ids == ["l_shield", "r_sword"], ids
        sw = next(i for i in doc["items"] if i["id"] == "r_sword")
        sh = next(i for i in doc["items"] if i["id"] == "l_shield")
        assert sw["source"] == "extracted" and sw["gate"]["ok"], sw["gate"]
        assert 0.8 < sw["dims"]["length_m"] < 1.15, sw["dims"]
        assert sw["dims"]["width_m"] > 0.12, sw["dims"]          # the crossguard went with the blade
        assert sh["source"] == "extracted" and sh["gate"]["ok"], sh["gate"]
        big = max(sh["dims"]["width_m"], sh["dims"]["height_m"])
        assert sh["dims"]["thickness_m"] < 0.3 * big, sh["dims"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_real_captain_banner_by_hint():
    """Order captain (the rig refused it: the arm traced into the banner cloth): the pole is taken as held by the
    roles hint, extended through the cloth, and pole + cloth (+ the floating finial) come out as one banner item;
    the mace at the left hip comes out as a belt item."""
    if not os.path.exists(REAL_CAPTAIN):
        return "skip: " + REAL_CAPTAIN
    d = _tmp()
    try:
        res, doc = _extract(d, REAL_CAPTAIN, {"front_prior": "-Y", "hints": {"hand_weapon": "banner pole", "hand": "r",
                                                                              "notes": "banner in the right hand"}})
        b = next(i for i in doc["items"] if i["hand"] == "r")
        assert b["type"] == "banner" and b["source"] == "extracted", (b["type"], b["source"])
        assert b["grip_mode"] == "shaft", b["grip_mode"]
        assert b["dims"]["length_m"] > 1.7 and b["dims"]["width_m"] > 0.3, b["dims"]   # the cloth came along
        # the mace hanging at the left hip (the left arm was traced down its haft): its own item on the Hips
        m = next((i for i in doc["items"] if i["hand"] == "hip"), None)
        assert m is not None and m["source"] == "extracted", [i["id"] for i in doc["items"]]
        assert doc["detector"]["fragments_absorbed"] > 100, doc["detector"]["fragments_absorbed"]   # the finial
    finally:
        shutil.rmtree(d, ignore_errors=True)
