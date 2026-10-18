"""Self-tests of the humanoid module (rig_humanoid, prepare_mixamo, import_mixamo_rig).

Synthetic tests build a procedural mannequin with known joints (_humanoid_lib.make_mannequin);
test_real_* use assets on disk and return early when a file is missing. Temporary files only in
tempfile.mkdtemp(). Run (until the shared runner exists):
  blender -b --factory-startup --python-expr "import sys;sys.path[:0]=[r'<root>/scripts/blender',
  r'<root>/scripts/blender/tests'];import test_humanoid as t;import af_common as af
  for n in sorted(dir(t)):
      if n.startswith('test_'): af.reset_scene(); getattr(t,n)(); print('PASS',n)"
"""
import json
import math
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import bpy  # noqa: E402
import numpy as np  # noqa: E402
from mathutils import Matrix, Vector  # noqa: E402

import af_common as af  # noqa: E402
from steps import _humanoid_lib as hl  # noqa: E402
from steps import _items_lib as IL  # noqa: E402


# ------------------------------------------------------------------ helpers

def _tmp():
    return tempfile.mkdtemp(prefix="af_humanoid_")


def _mannequin_glb(d, pose="A", rot_z=0.0, **kw):
    af.reset_scene()
    ob, gt = hl.make_mannequin(pose, **kw)
    if rot_z:
        ob.data.transform(Matrix.Rotation(math.radians(rot_z), 4, "Z"))
    path = os.path.join(d, "mannequin_%s.glb" % pose)
    af.export_glb(path, objects=[ob], animations=False, skins=False)
    return path, gt


def _run(step, req):
    import importlib
    mod = importlib.import_module("steps." + step)
    req.setdefault("params", {})
    req.setdefault("inputs", {})
    req["step"] = step
    res = mod.run(req)
    full = {"ok": True, "checks": [], "warnings": [], "outputs": {}, "stats": {}}
    full.update(res)
    failed = [c for c in full["checks"] if not c["ok"] and c.get("severity", "error") == "error"]
    if failed:
        full["ok"] = False
    full["failed"] = [c["name"] for c in failed]
    return full


def _detect(path, params=None):
    req = {"inputs": {"mesh": path}, "params": dict(params or {}), "out_dir": _tmp()}
    if path.lower().endswith(".blend") and not req["params"].get("objects"):
        req["inputs"] = {"blend": path}
        af.open_blend(path)
    w = []
    body, P, info = hl.prepare_body(req, w)
    lm, P = hl.run_landmarks(body, P, float(req["params"].get("height_m", 1.8)), req["params"], w, info)
    shutil.rmtree(req["out_dir"], ignore_errors=True)
    return lm, info, w


def _check(res, name):
    return next((c for c in res["checks"] if c["name"] == name), None)


# ------------------------------------------------------------------ synthetic

def test_landmarks_mannequin_poses():
    d = _tmp()
    try:
        for pose, cls in (("A", "A"), ("T", "T"), ("down", "arms_down")):
            path, gt = _mannequin_glb(d, pose)
            lm, info, _ = _detect(path)
            assert info["facing"]["detected"] == "-Y", info["facing"]
            assert lm.pose == cls, (pose, lm.pose)
            assert lm.fail is None and lm.overall > 0.5, (lm.fail, lm.overall)
            J = lm.joints_m()
            for k in ("LeftForeArm", "RightForeArm", "LeftHand", "RightHand", "LeftLeg", "RightLeg"):
                err = float(np.linalg.norm(J[k] - gt[k]))
                assert err < 0.05, (pose, k, err)
            for k in ("LeftArm", "RightArm", "LeftUpLeg", "RightUpLeg"):
                err = float(np.linalg.norm(J[k] - gt[k]))
                assert err < 0.07, (pose, k, err)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_facing_detection_rotated():
    d = _tmp()
    try:
        af.reset_scene()
        ob, _ = hl.make_mannequin("A")
        ob.data.transform(Matrix.Rotation(math.radians(90), 4, "Z"))   # now faces +X
        path = os.path.join(d, "turned.glb")
        af.export_glb(path, objects=[ob], animations=False, skins=False)
        lm, info, _ = _detect(path)
        assert info["facing"]["detected"] == "+X", info["facing"]
        assert lm.pose == "A"
        lm2, info2, _ = _detect(path, {"front": "+X"})
        assert info2["facing"]["confidence"] == 1.0
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_bone_roll_convention():
    af.reset_scene()
    _, gt = hl.make_mannequin("A")
    j = dict(gt)
    j["Spine"] = (j["Hips"] + j["Neck"]) * 0.5 - np.array([0, 0, 0.1])
    j["Spine1"] = (j["Hips"] + j["Neck"]) * 0.5
    j["Spine2"] = (j["Hips"] + j["Neck"]) * 0.5 + np.array([0, 0, 0.12])
    j["HeadTop_End"] = np.array([0, 0, 1.8])
    for s, sg in (("Left", 1), ("Right", -1)):
        j[s + "Shoulder"] = np.array([sg * 0.04, 0, j[s + "Arm"][2] + 0.04])
        j[s + "ToeBase"] = (j[s + "Foot"] + j[s + "Toe_End"]) * 0.5 + np.array([0, 0, -0.02])
    rig = hl.build_armature(j, 1.8)
    assert len(rig.data.bones) == 25
    fwd, up = Vector((0, -1, 0)), Vector((0, 0, 1))
    for bn in rig.data.bones:
        z = (bn.matrix_local.to_3x3() @ Vector((0, 0, 1))).normalized()
        n = hl.short(bn.name)
        if any(k in n for k in ("Foot", "ToeBase", "Toe_End")):
            assert z.dot(up) > 0.6, (n, z)
        else:
            assert z.dot(fwd) > 0.85, (n, z)
    assert not rig.data.bones[hl.b("HeadTop_End")].use_deform
    assert rig.data.bones[hl.b("LeftForeArm")].parent.name == hl.b("LeftArm")


def test_rig_humanoid_end_to_end():
    d = _tmp()
    try:
        path, gt = _mannequin_glb(d, "A")
        out = os.path.join(d, "out")
        res = _run("rig_humanoid", {"inputs": {"mesh": path}, "params": {"intersections": False},
                                    "out_dir": out})
        assert res["ok"], (res["failed"], res.get("error"))
        for k in ("glb", "blend", "markers_json", "markers_png", "poses_png"):
            assert os.path.exists(res["outputs"][k]), k
        assert res["stats"]["bones"] == 25
        assert _check(res, "unweighted_vertices")["value"] == 0
        assert _check(res, "max_influences")["value"] <= 4
        mk = json.load(open(res["outputs"]["markers_json"], encoding="utf-8"))
        for k in hl.MARKER_KEYS:
            assert len(mk["markers"][k]) == 2 and k in mk["confidence"]
        assert mk["pose"] == "A" and "LeftHand" in mk["joints"]
        assert len(res["stats"]["sockets"]) == 9      # contract 6 human list, shield mounts included
        assert res["stats"]["shield_mounts"]["socket_mount_shield_l"]["standoff_m"] <= 0.12
        assert mk["projection"]["image"] == {"width": 1280, "height": 640}, mk["projection"]
        # re-import: skin with 25 joints and socket extras
        af.reset_scene()
        objs = af.import_any(res["outputs"]["glb"])
        arms = af.armatures(objs)
        assert len(arms) == 1 and len(arms[0].data.bones) == 25
        socks = {o.get("game_socket"): o for o in objs if o.get("game_socket")}
        for n in ("socket_mount_hand_r", "socket_mount_hand_l", "socket_mount_shield_r", "socket_mount_shield_l",
                  "socket_mount_back", "socket_mount_hip_l", "socket_mount_hip_r", "socket_fx_head",
                  "socket_fx_chest"):
            assert n in socks, n
        # shield mounts hang off the hand bones; their frame keeps a canonical shield upright, facing out
        for side, out_x in (("l", 1.0), ("r", -1.0)):
            sm = socks["socket_mount_shield_" + side]
            assert sm.parent_type == "BONE" and sm.parent_bone.endswith(("Left" if side == "l" else "Right") + "Hand")
            bpy.context.view_layer.update()
            R3 = sm.matrix_world.to_3x3().normalized()
            assert (R3 @ Vector((0, 0, 1))).z > 0.9, (side, R3)
            assert (R3 @ Vector((0, -1, 0))).x * out_x > 0.5, (side, R3)
        # hand frame: +Z thumb side (front for a hanging A-pose hand), -Y along the fingers (down)
        bpy.context.view_layer.update()
        M = socks["socket_mount_hand_l"].matrix_world
        assert M.col[2].to_3d().normalized().dot(Vector((0, -1, 0))) > 0.7
        assert (-M.col[1].to_3d()).normalized().dot(Vector((0, 0, -1))) > 0.5
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_staff_rigid_and_grip_socket():
    d = _tmp()
    try:
        path, _ = _mannequin_glb(d, "A", staff=True)
        res = _run("rig_humanoid", {"inputs": {"mesh": path},
                                    "params": {"intersections": False, "deform_test": False},
                                    "out_dir": os.path.join(d, "out")})
        rigid = [r for r in res["stats"]["rigid"] if r["kind"] == "prop"]
        assert rigid and rigid[0]["bone"] == hl.b("LeftHand"), res["stats"]["rigid"]
        assert rigid[0]["vertices"] > 100
        c = _check(res, "rigid_prop_LeftHand")
        assert c and c["ok"], c
        ang = res["stats"]["grip_socket_vs_prop_axis_deg"]["socket_mount_hand_l"]
        assert ang < 10.0, ang
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_robe_blend_stride():
    d = _tmp()
    try:
        path, _ = _mannequin_glb(d, "A", robe=True)
        res = _run("rig_humanoid", {"inputs": {"mesh": path}, "params": {"intersections": False},
                                    "out_dir": os.path.join(d, "out")})
        assert res["stats"]["robe"] is True
        assert res["stats"]["robe_blend"]["vertices_blended"] > 100
        c = _check(res, "robe_stride_tear")
        assert c is not None and c["ok"], c
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_markers_override():
    d = _tmp()
    try:
        path, gt = _mannequin_glb(d, "A")
        mk = {"l_wrist": [0.40, 0.95], "r_wrist": [-0.40, 0.95], "l_knee": [0.12, 0.47],
              "joints": {"Head": [0.0, 0.0, 1.57]}}
        lm, _, _ = _detect(path, {"markers": mk})
        J = lm.joints_m()
        assert abs(J["LeftHand"][0] - 0.40) < 1e-3 and abs(J["LeftHand"][2] - 0.95) < 1e-3
        assert abs(J["RightHand"][0] + 0.40) < 1e-3
        assert abs(J["LeftLeg"][2] - 0.47) < 1e-3
        assert abs(J["Head"][2] - 1.57) < 1e-3
        assert lm.src["l_wrist"] == "user" and lm.conf["l_wrist"] == 1.0
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_legless_fails_gracefully():
    d = _tmp()
    try:
        path, _ = _mannequin_glb(d, "A", legless=True)
        out = os.path.join(d, "out")
        res = _run("rig_humanoid", {"inputs": {"mesh": path}, "params": {}, "out_dir": out})
        assert not res["ok"] and "rig not built" in res.get("error", ""), res.get("error")
        assert os.path.exists(res["outputs"]["markers_json"])
        assert os.path.exists(res["outputs"]["markers_png"])
        assert not os.path.exists(os.path.join(out, "rigged.glb"))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_fingers_skipped_on_mittens():
    d = _tmp()
    try:
        path, _ = _mannequin_glb(d, "A")
        res = _run("rig_humanoid", {"inputs": {"mesh": path},
                                    "params": {"fingers": True, "deform_test": False},
                                    "out_dir": os.path.join(d, "out")})
        assert res["stats"]["bones"] == 25
        assert any("fingers skipped" in w for w in res["warnings"]), res["warnings"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _textured_mannequin(d):
    af.reset_scene()
    ob, _ = hl.make_mannequin("A")
    img = bpy.data.images.new("tex1024", 1024, 1024)
    img.pixels = np.tile(np.array([0.6, 0.3, 0.2, 1.0], dtype=np.float32), 1024 * 1024)
    p_img = os.path.join(d, "tex1024.png")
    img.filepath_raw = p_img
    img.file_format = "PNG"
    img.save()
    mat = ob.data.materials[0]
    mat.use_nodes = True
    nt = mat.node_tree
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = img
    bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
    nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    ob.data.uv_layers.new(name="UVMap")
    path = os.path.join(d, "textured.glb")
    af.export_glb(path, objects=[ob], animations=False, skins=False)
    return path


def test_prepare_mixamo_lite():
    d = _tmp()
    try:
        path = _textured_mannequin(d)
        out = os.path.join(d, "out")
        res = _run("prepare_mixamo", {"inputs": {"mesh": path}, "params": {"lite": True}, "out_dir": out})
        assert res["ok"], res["failed"]
        assert res["stats"]["tris"] <= 12000
        assert all(max(t["size"]) <= 256 for t in res["stats"]["textures"]), res["stats"]["textures"]
        assert res["stats"]["fits_upload"]
        af.reset_scene()
        objs = af.import_any(res["outputs"]["fbx"])
        lo, hi = af.world_bbox(objs)
        assert abs(hi.z - lo.z - 1.8) < 0.02 and abs(lo.z) < 0.01, (lo, hi)
        assert not af.armatures(objs)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_prepare_mixamo_prerigged_and_import_roundtrip():
    d = _tmp()
    try:
        path, _ = _mannequin_glb(d, "A")
        out = os.path.join(d, "pm")
        res = _run("prepare_mixamo", {"inputs": {"mesh": path},
                                      "params": {"lite": True, "prerigged": True,
                                                 "rig_params": {"deform_test": False}},
                                      "out_dir": out})
        assert res["ok"], res["failed"]
        assert res["stats"]["prerigged"]["bones"] == 25
        fbx = res["outputs"]["fbx_prerigged"]
        # emulate a Mixamo download: second-character prefix and centimetre scale
        af.reset_scene()
        objs = af.import_any(fbx)
        rig = af.armatures(objs)[0]
        for bn in rig.data.bones:
            bn.name = bn.name.replace("mixamorig:", "mixamorig1:")
        cm = os.path.join(d, "from_mixamo.fbx")
        af.export_fbx(cm, objects=[rig] + af.meshes(objs), global_scale=100.0, mixamo=True)
        res2 = _run("import_mixamo_rig", {"inputs": {"fbx": cm}, "params": {"height_m": 1.75},
                                          "out_dir": os.path.join(d, "imr")})
        assert res2["ok"], res2["failed"]
        assert res2["stats"]["renamed_bones"] == 25
        assert _check(res2, "height")["ok"]
        assert len(res2["stats"]["sockets"]) == 9      # the same add_human_sockets (shield mounts too)
        af.reset_scene()
        objs = af.import_any(res2["outputs"]["glb"])
        names = [bn.name for bn in af.armatures(objs)[0].data.bones]
        assert all(n.startswith("mixamorig:") for n in names), names[:3]
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------ real assets


class _Comp:
    def __init__(self, k, n, c):
        self.k, self.n, self.c = k, n, np.asarray(c, dtype=np.float64)


class _Chain:
    ab = [0, 1]

    def __init__(self):
        self.all = []

    def add(self, k, n, x, y=0.0):
        self.all.append(_Comp(k, n, (x, y, 0.1 + 0.005 * k)))
        return len(self.all) - 1


def test_prop_chain_axis_decimation_fragments_vs_bow():
    """_chain_axis (smoke 10.10): a straight shaft whose slices pick up small off-axis fragments (finger
    tips, cross-sections split by decimation) stays straight; a bow limb with its string (two real
    components per level) and a curved chain stay rejected."""
    rng = np.random.default_rng(5)
    staff = _Chain()
    g = [staff.add(k, 80, 0.3 + rng.normal(0, 0.0012)) for k in range(60)]
    g += [staff.add(k, 9, 0.325) for k in (5, 14, 23, 31, 40, 52)]         # fragments beside the shaft
    for k in (0, 1, 58, 59):                                              # fist at the ends of the chain
        staff.all[g[k]].c[0] += 0.015
    cents = np.array([staff.all[i].c for i in g])
    _, _, _, resid = hl._line_fit(cents)
    assert float(np.sqrt(np.mean(resid ** 2))) > 0.006, "the plain centroid fit fails here (the smoke case)"
    c, d, rms, used = hl._chain_axis(staff, g)
    assert rms < 0.003 and abs(d[2]) > 0.99 and abs(c[0] - 0.3) < 0.002, (rms, d, c)
    assert len(used) <= 60
    bow = _Chain()
    gb = []
    for k in range(80):
        t = k / 79.0
        gb.append(bow.add(k, 50, 0.3 + 0.05 * (1 - (2 * t - 1) ** 2)))   # limb, sagitta 0.05 H
        gb.append(bow.add(k, 45, 0.3))                                     # string
    assert hl._chain_axis(bow, gb)[2] > 0.006
    arc = _Chain()
    ga = [arc.add(k, 60, 0.3 + 0.04 * (1 - (2 * k / 79.0 - 1) ** 2)) for k in range(80)]
    assert hl._chain_axis(arc, ga)[2] > 0.006, "trimming does not straighten a curve"


def _glb_animation_names(path):
    import struct
    with open(path, "rb") as fh:
        data = fh.read()
    n = struct.unpack_from("<I", data, 12)[0]
    js = json.loads(data[20:20 + n].decode("utf-8"))
    return [a.get("name") for a in js.get("animations", [])]


def _arm_heads_from_fbx(path, H):
    """Arm heads of a rigged FBX in its own frame scaled to height H (feet on 0, x from the Hips)."""
    af.reset_scene()
    objs = af.import_any(path)
    rig = af.armatures(objs)[0]
    co = np.concatenate([hl.mesh_verts(o) for o in af.meshes(objs)])
    z0, h = float(co[:, 2].min()), float(co[:, 2].max() - co[:, 2].min())
    k = H / h
    hips = rig.matrix_world @ next(bn for bn in rig.data.bones if bn.name.endswith("Hips")).head_local
    out = {}
    for side in ("Left", "Right"):
        bn = next(bn for bn in rig.data.bones if bn.name.endswith(side + "Arm"))
        p = rig.matrix_world @ bn.head_local
        out[side] = np.array([(p.x - hips.x) * k, (p.z - z0) * k])
    return out


# ------------------------------------------------------------------ review fixes (round 2)

def test_rotated_mannequin_yaw_fixed():
    """An input turned 30 deg about Z is rotated back before the landmarks: same joints, symmetric."""
    d = _tmp()
    try:
        path, gt = _mannequin_glb(d, "A", rot_z=30.0)
        lm, info, w = _detect(path)
        assert info["yaw"]["corrected"] and abs(info["yaw"]["residual_deg"] - 30.0) < 2.0, info["yaw"]
        assert any("turned" in x for x in w), w
        assert lm.fail is None and lm.pose == "A"
        assert lm.arm_sym_max < 0.03, lm.arm_sym
        J = lm.joints_m()
        for k in ("LeftHand", "RightHand", "LeftForeArm", "RightForeArm", "LeftLeg", "RightLeg"):
            err = float(np.linalg.norm(J[k] - gt[k]))
            assert err < 0.05, (k, err)
        # an axis-aligned input is left alone
        path2, _ = _mannequin_glb(d, "down")
        _, info2, _ = _detect(path2)
        assert not info2["yaw"]["corrected"] and abs(info2["yaw"]["residual_deg"]) < 2.0
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_blade_down_sword_socket():
    """Synthetic sword hanging from the right fist: rigid to RightHand, hand socket +Z down the
    blade (contract 6), alignment measured against grip -> blade tip."""
    d = _tmp()
    try:
        path, _ = _mannequin_glb(d, "A", sword=True)
        res = _run("rig_humanoid", {"inputs": {"mesh": path},
                                    "params": {"intersections": False, "deform_test": False},
                                    "out_dir": os.path.join(d, "out")})
        assert res["ok"], (res["failed"], res.get("error"))
        st = res["stats"]
        props = [r for r in st["rigid"] if r["kind"] == "prop"]
        assert props and props[0]["bone"] == hl.b("RightHand") and props[0]["vertices"] > 100, st["rigid"]
        rule = st["grip_socket_rule"]["socket_mount_hand_r"]
        assert rule["rule"] == "blade_to_far_end", rule
        assert rule["socket_z"][2] < -0.9, rule              # +Z points down along the blade
        assert st["grip_socket_vs_prop_axis_deg"]["socket_mount_hand_r"] < 10
        assert _check(res, "props_rigid")["ok"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_prepare_mixamo_refuses_failed_rig():
    """prerigged=true on a figure whose rig fails: no pre-rigged FBX, error check prerigged_rig_ok."""
    d = _tmp()
    try:
        path, _ = _mannequin_glb(d, "A", legless=True)
        res = _run("prepare_mixamo", {"inputs": {"mesh": path},
                                      "params": {"lite": True, "prerigged": True},
                                      "out_dir": os.path.join(d, "pm")})
        c = _check(res, "prerigged_rig_ok")
        assert c is not None and not c["ok"] and c["severity"] == "error", c
        assert not res["ok"] and "fbx_prerigged" not in res["outputs"]
        assert os.path.exists(res["outputs"]["fbx"])          # the plain upload FBX is still there
        assert any("not written" in w for w in res["warnings"]), res["warnings"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _bow_rig_asserts(res, side="Left", min_vertices=150):
    assert res["ok"], (res["failed"], res.get("error"))
    bows = [r for r in res["stats"]["rigid"] if r["kind"] == "bow"]
    assert len(bows) == 1 and bows[0]["bone"] == hl.b(side + "Hand") and bows[0]["shape"] == "bow", res["stats"]["rigid"]
    assert bows[0]["vertices"] >= min_vertices, bows
    for name in ("arm_symmetry", "landmark_proportions", "props_rigid", "rigid_bow_" + side + "Hand"):
        assert _check(res, name)["ok"], (name, _check(res, name))
    assert res["stats"]["arm_symmetry_max_h"] <= hl.ARM_SYM_MAX, res["stats"]["arm_symmetry_max_h"]
    # the hand socket runs up the riser (+Z along the bow's chord, upward)
    ang = res["stats"]["grip_socket_vs_prop_axis_deg"]["socket_mount_hand_" + side[0].lower()]
    assert ang < 10.0, ang
    return bows[0]


def test_bow_held_beside_body_rigid_quiver_ignored():
    """Mannequin with a 1.3 m bow in the left fist and a quiver on the back: the bow is a held curved prop
    (rigid to LeftHand, out of the landmarks, symmetry passes), the quiver is not taken. Mutation: with
    find_bows disabled the wrist lands on the limb and the rig is refused for arm asymmetry (the archer
    behaviour before 11.10)."""
    d = _tmp()
    try:
        path, gt = _mannequin_glb(d, "A", bow=True, quiver=True)
        params = {"intersections": False, "deform_test": False}
        res = _run("rig_humanoid", {"inputs": {"mesh": path}, "params": dict(params), "out_dir": os.path.join(d, "out")})
        bw = _bow_rig_asserts(res)
        assert 1.15 < bw["length_m"] < 1.45 and 0.03 < bw["sagitta_m"] < 0.09, bw
        mk = json.load(open(res["outputs"]["markers_json"], encoding="utf-8"))
        assert len(mk["bows"]) == 1 and mk["bows"][0]["held_by"] == "Left", mk["bows"]
        # the wrist is on the arm (ground truth), not on the bow limb
        err = float(np.linalg.norm(np.array(mk["joints"]["LeftHand"]) - gt["LeftHand"]))
        assert err < 0.08, err
        orig = hl.Landmarks.find_bows
        hl.Landmarks.find_bows = lambda self: False
        try:
            res0 = _run("rig_humanoid", {"inputs": {"mesh": path}, "params": dict(params),
                                         "out_dir": os.path.join(d, "out0")})
        finally:
            hl.Landmarks.find_bows = orig
        assert not res0["ok"] and not _check(res0, "arm_symmetry")["ok"], (res0["failed"], res0["stats"].get("arm_symmetry_max_h"))
        # quiver alone: no bow
        path2, _ = _mannequin_glb(d, "A", quiver=True)
        lm, _, _ = _detect(path2)
        assert not lm.bow_props, lm.info.get("bows")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------ integration round (10.10, evening)


def _load_rigged(path):
    if path.lower().endswith(".blend"):
        af.open_blend(path)
        objs = list(bpy.context.scene.objects)
    else:
        af.reset_scene()
        objs = af.import_any(path)
    arm = next(o for o in objs if o.type == "ARMATURE")
    arm.data.pose_position = "REST"
    bpy.context.view_layer.update()
    body = max(IL.skinned_meshes(arm), key=lambda o: len(o.data.vertices))
    socks = IL.find_sockets()
    return arm, body, socks


def _mat_err(A, B):
    return max(abs(A[i][j] - B[i][j]) for i in range(4) for j in range(4))


def _kit_item(out, params):
    from steps import item_kit as K
    af.reset_scene()
    item, info = K.build_item(params)
    K.export_item(item, out, info, preview=False)
    return os.path.join(out, "item.glb")


def _attach(character, atts, out):
    from steps import attach_prop as A
    res = A.run({"inputs": {"character": character}, "params": {"attachments": atts, "size": 200},
                 "out_dir": out})
    rep = json.load(open(res["outputs"]["json"], encoding="utf-8"))
    return res, rep


# ------------------------------------------------------------------ evidence (not a test)

def _png_rgb(arr, dst):
    """Opaque, maximally compressed RGB PNG of a top-down RGBA array (docs evidence)."""
    h, w = arr.shape[:2]
    im = bpy.data.images.new("af_evidence", width=w, height=h, alpha=False)
    im.pixels = np.ascontiguousarray(IL.over_bg(arr)[::-1]).ravel()
    st = bpy.context.scene.render.image_settings
    st.file_format, st.color_mode, st.compression = "PNG", "RGB", 100
    im.save_render(os.path.abspath(dst), scene=bpy.context.scene)
    bpy.data.images.remove(im)
    return {"path": dst, "kb": round(os.path.getsize(dst) / 1024.0, 1), "px": [w, h]}


# --------------------------------------------------------------------------- v2 fixes (10.10)

def test_input_sockets_survive_rig_and_grip_dirs():
    """Sockets of the input follow the body into the rig frame: socket_fx_eye_l becomes a child of the
    bone that drives the skull (Head), an input socket_mount_hand_l keeps its frame instead of being
    rebuilt; params.grip_dirs points +Z of a rebuilt hand socket exactly ("down" for the right hand)."""
    d = _tmp()
    try:
        af.reset_scene()
        ob, gt = hl.make_mannequin("A")
        # the mannequin is shifted / scaled on the way in: the sockets must follow
        ob.data.transform(Matrix.Translation((0.3, -0.2, 0.0)) @ Matrix.Scale(0.5, 4))
        eye = af.add_socket("socket_fx_eye_l", location=(0.3 + 0.5 * 0.035, -0.2 + 0.5 * -0.09, 0.5 * 1.70),
                            kind="fx")
        W = Vector(gt["LeftHand"]) + (Vector(gt["LeftHandTip"]) - Vector(gt["LeftHand"])) * 0.4
        hand = af.add_socket("socket_mount_hand_l", location=(0.3 + 0.5 * W.x, -0.2 + 0.5 * W.y, 0.5 * W.z),
                             rotation=(0.0, 0.0, 0.0), kind="mount", grip_radius_m=0.02)
        path = os.path.join(d, "m.glb")
        af.export_glb(path, objects=[ob, eye, hand], animations=False, skins=False)
        res = _run("rig_humanoid", {"inputs": {"mesh": path}, "out_dir": os.path.join(d, "out"),
                                    "params": {"intersections": False, "deform_test": False,
                                               "grip_dirs": {"r": "down"}}})
        assert res["ok"], (res["failed"], res.get("error"))
        st = res["stats"]
        assert st["input_sockets"] == ["socket_fx_eye_l", "socket_mount_hand_l"], st["input_sockets"]
        assert st["sockets_from_input"]["socket_fx_eye_l"]["bone"] == "Head", st["sockets_from_input"]
        assert st["hand_socket_frame"]["socket_mount_hand_l"]["source"] == "input", st["hand_socket_frame"]
        assert len(st["sockets"]) == 10
        af.reset_scene()
        objs = af.import_any(res["outputs"]["glb"])
        socks = {o.get("game_socket"): o for o in objs if o.get("game_socket")}
        bpy.context.view_layer.update()
        e = socks["socket_fx_eye_l"]
        assert e.parent_type == "BONE" and e.parent_bone.endswith("Head"), (e.parent_type, e.parent_bone)
        # back in the rig frame (x2, re-centred): the eye lands where the mannequin's eye is
        assert (e.matrix_world.translation - Vector((0.035, -0.09, 1.70))).length < 0.02, e.matrix_world.translation
        h = socks["socket_mount_hand_l"]
        assert h.parent_bone.endswith("LeftHand")
        assert (h.matrix_world.translation - W).length < 0.02, (h.matrix_world.translation, W)
        R = h.matrix_world.to_3x3().normalized()
        assert (R @ Vector((0, 0, 1))).z > 0.999, R          # the input frame (identity rotation) is kept
        assert abs(h.get("grip_radius_m", 0) - 0.02) < 1e-6
        r = socks["socket_mount_hand_r"].matrix_world.to_3x3().normalized() @ Vector((0, 0, 1))
        assert r.z < -0.999, r                               # grip_dirs r: down
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_small_islands_kept_out_of_bone_heat_and_bound_rigidly():
    """Loose little pieces (a skeletal hand of separate bones, beads) make Blender's heat solver give
    up on the whole mesh; they stay out of the proxy and ride rigidly on the nearest bone."""
    d = _tmp()
    try:
        af.reset_scene()
        ob, gt = hl.make_mannequin("A")
        import bmesh
        bm = bmesh.new()
        bm.from_mesh(ob.data)
        tip = Vector(gt["RightHandTip"])
        for k in range(12):          # 'finger bones': a cluster of cubes 2 mm apart below the right hand
            res_ = bmesh.ops.create_cube(bm, size=0.012)
            bmesh.ops.translate(bm, vec=tip + Vector((0.014 * (k % 3) - 0.014, 0.0, -0.014 * (k // 3))),
                                verts=res_["verts"])
        for k in range(3):           # beads in front of the chest
            res_ = bmesh.ops.create_cube(bm, size=0.02)
            bmesh.ops.translate(bm, vec=Vector((0.0, -0.16, 1.15 + 0.025 * k)), verts=res_["verts"])
        bm.to_mesh(ob.data)
        bm.free()
        path = os.path.join(d, "m.glb")
        af.export_glb(path, objects=[ob], animations=False, skins=False)
        res = _run("rig_humanoid", {"inputs": {"mesh": path}, "out_dir": os.path.join(d, "out"),
                                    "params": {"intersections": False}})
        assert res["ok"], (res["failed"], res.get("error"))
        si = res["stats"]["skin"]["small_islands"]
        assert si["clusters"] == 2 and si["vertices"] >= 15 * 8, si
        bones = sorted(c["bone"] for c in si["bound"])
        assert bones[0].startswith("RightHand") and bones[1].startswith("Spine"), si["bound"]
        assert res["stats"]["skin"]["attempts"][0]["unweighted_frac"] < 0.002, res["stats"]["skin"]["attempts"]
        assert _check(res, "unweighted_vertices")["value"] == 0
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_repair_for_heat_drops_nonmanifold_faces():
    af.reset_scene()
    import bmesh
    me = bpy.data.meshes.new("t")
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=1.0)
    bm.edges.ensure_lookup_table()
    e = bm.edges[0]
    a, b_ = e.verts
    c = bm.verts.new((0.0, 0.0, 2.0))
    bm.faces.new((a, b_, c))          # a third face on that edge
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new("t", me)
    bpy.context.scene.collection.objects.link(ob)
    info = hl.repair_for_heat(ob, 1.8)
    assert info["nonmanifold_faces"] >= 3, info
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    assert not any(len(e.link_faces) > 2 for e in bm.edges)
    bm.free()


NECRO_V2_MARKERS = {"l_wrist": [0.480082, -0.142343, 1.157233], "l_elbow": [0.385688, 0.093641, 1.187847],
                    "r_wrist": [-0.380193, -0.125434, 0.991049],
                    "joints": {"LeftHandTip": [0.556372, -0.206123, 1.146663],
                               "RightHandTip": [-0.406863, -0.156064, 0.899669]}}


def test_oblique_sword_held_forward_rigid_to_hand():
    """A flat 8 cm blade held forward, down and out from the right fist (the pose of the generated knights):
    the vertical/X/Y slice passes do not see it (it crosses them obliquely, wider than their limit) and it
    used to become part of the arm. find_blades takes it from the diagonal slicings: a fist on it, a free tip
    beyond the bare arm's reach. The prop is rigid to RightHand, the wrist stays on the arm, the hand socket
    +Z runs toward the blade tip. Mutation: without find_blades the blade is not a prop and the right wrist
    lands far from the fist."""
    d = _tmp()
    try:
        path, gt = _mannequin_glb(d, "A", sword="oblique")
        params = {"intersections": False, "deform_test": False}
        res = _run("rig_humanoid", {"inputs": {"mesh": path}, "params": dict(params), "out_dir": os.path.join(d, "out")})
        assert res["ok"], (res["failed"], res.get("error"))
        props = [r for r in res["stats"]["rigid"] if r["kind"] == "prop"]
        assert len(props) == 1 and props[0]["bone"] == hl.b("RightHand") and props[0]["vertices"] > 100, props
        assert 0.7 < props[0]["length_m"] < 1.05, props
        mk = json.load(open(res["outputs"]["markers_json"], encoding="utf-8"))
        assert mk["props"][0]["oblique"] and mk["props"][0]["held_by"] == "Right", mk["props"]
        blades = [b for b in mk["detector"]["blades"] if b.get("accepted")]
        assert len(blades) == 1 and blades[0]["tip_reach_h"] > hl.BLADE_REACH, mk["detector"]["blades"]
        err = float(np.linalg.norm(np.array(mk["joints"]["RightHand"]) - gt["RightHand"]))
        assert err < 0.08, err
        rule = res["stats"]["grip_socket_rule"]["socket_mount_hand_r"]
        assert rule["rule"] == "blade_to_far_end", rule
        assert res["stats"]["grip_socket_vs_prop_axis_deg"]["socket_mount_hand_r"] < 15, res["stats"]["grip_socket_vs_prop_axis_deg"]
        assert _check(res, "props_rigid")["ok"]
        orig = hl.Landmarks.find_blades
        hl.Landmarks.find_blades = lambda self: False
        try:
            lm0, _, _ = _detect(path)
        finally:
            hl.Landmarks.find_blades = orig
        assert not lm0.props, [p.get("oblique") for p in lm0.props]
        err0 = float(np.linalg.norm(lm0.joints_m()["RightHand"] - gt["RightHand"]))
        assert err0 > 0.15 or lm0.fail is not None, (err0, lm0.fail)
        # the plain mannequin gets no oblique prop (limbs are not blades)
        path2, _ = _mannequin_glb(d, "A")
        lm2, _, _ = _detect(path2)
        assert not lm2.props and not [b for b in lm2.info.get("blades", []) if b.get("accepted")], lm2.info.get("blades")
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------ 12.10: front prior, hard-surface armour

def _coloured_mannequin(d, rgb, name="coloured"):
    """The A-pose mannequin with a uniform base-colour texture (UVMap, image node -> Base Color), as a GLB."""
    af.reset_scene()
    ob, _ = hl.make_mannequin("A")
    img = bpy.data.images.new(name, 64, 64)
    img.pixels = np.tile(np.array(list(rgb) + [1.0], dtype=np.float32), 64 * 64)
    p_img = os.path.join(d, name + ".png")
    img.filepath_raw = p_img
    img.file_format = "PNG"
    img.save()
    mat = ob.data.materials[0]
    mat.use_nodes = True
    nt = mat.node_tree
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = img
    bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
    nt.links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
    ob.data.uv_layers.new(name="UVMap")
    path = os.path.join(d, name + ".glb")
    af.export_glb(path, objects=[ob], animations=False, skins=False)
    return path


def test_front_prior_beats_back_facing_answer():
    """front_prior -Y (generated front-view concepts): a detector answer of +Y loses to the prior, with a warning;
    without the prior the detector's answer stands."""
    d = _tmp()
    try:
        path, _ = _mannequin_glb(d, "A", rot_z=180.0)
        _, info, w = _detect(path)
        assert info["facing"]["detected"] == "+Y", info["facing"]
        _, info2, w2 = _detect(path, {"front_prior": "-Y"})
        assert info2["facing"]["detected"] == "-Y" and info2["facing"]["overridden"] == "+Y", info2["facing"]
        assert any("front_prior" in x for x in w2), w2
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_hard_surface_plates_rigid_on_grey_armour():
    """A mannequin painted steel grey: hard_surface on makes plate regions rigid (weight 1.0 on one bone each,
    no stretch inside them in any test pose, while the same triangles stretched with the smooth weights);
    a brown (skin / cloth) mannequin under auto keeps its smooth weights."""
    d = _tmp()
    try:
        grey = _coloured_mannequin(d, (0.55, 0.56, 0.58), "steel")
        res = _run("rig_humanoid", {"inputs": {"mesh": grey},
                                    "params": {"intersections": False, "hard_surface": "on", "deform_gate": "warning"},
                                    "out_dir": os.path.join(d, "out_grey")})
        assert res["ok"], (res["failed"], res.get("error"))
        hs = res["stats"]["hard_surface"]
        assert hs["enabled"] and hs["plates"] >= 6 and hs["rigid_area_frac"] > 0.05, hs
        assert hs["metal_area_frac"] > 0.9, hs
        for pose, v in hs["pose_test_hard"].items():
            assert v["plate_rubber_pct"] == 0.0, (pose, v)
        assert max(v["plate_rubber_pct"] for v in hs["pose_test_soft"].values()) > 0.0, hs["pose_test_soft"]
        assert {"Head", "Spine2"} & set(hs["by_bone"]), hs["by_bone"]
        brown = _coloured_mannequin(d, (0.6, 0.3, 0.2), "skin")
        res2 = _run("rig_humanoid", {"inputs": {"mesh": brown},
                                     "params": {"intersections": False, "deform_test": False, "hard_surface": "auto"},
                                     "out_dir": os.path.join(d, "out_brown")})
        hs2 = res2["stats"]["hard_surface"]
        assert not hs2["enabled"] and hs2["metal_area_frac"] < 0.05, hs2
        res3 = _run("rig_humanoid", {"inputs": {"mesh": brown},
                                     "params": {"intersections": False, "deform_test": False, "hard_surface": "auto",
                                                "build": "armored"},
                                     "out_dir": os.path.join(d, "out_brown_armored")})
        hs3 = res3["stats"]["hard_surface"]
        assert hs3["enabled"] and hs3["plates"] == 0, hs3      # armored, but nothing is painted metal
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_shoulders_measured_against_the_solid_head_top():
    """A crown / halo / crest above the head counts into the head top and pushed the shoulders below 0.70 H
    ("not a humanoid": the crusader 0.67-0.69). solid_head_top() is the highest slice whose largest component
    near the midline covers >= 40 % of the fullest head cross-section (on a bare head it is the head top itself);
    sanity() measures out-of-range shoulders against it as well."""
    d = _tmp()
    try:
        path, _ = _mannequin_glb(d, "A")
        lm, info, w = _detect(path)
        solid = lm.solid_head_top()
        assert solid is not None and abs(solid - lm.top) < 0.02, (solid, lm.top)
        z0 = lm.shoulder_z
        # shoulders under 0.70 of a spiky top, but in range against the solid head under the spikes
        lm.shoulder_z = 0.69
        lm.solid_head_top = lambda min_fill=0.4: 0.93
        lm.fail = None
        lm.sanity()
        assert lm.fail is None or "shoulders" not in lm.fail, lm.fail
        assert abs(lm.info["solid_head_top"]["shoulders_vs_solid_top"] - 0.69 / 0.93) < 1e-3, lm.info["solid_head_top"]
        # a figure whose shoulders are low against the solid head too is still refused
        lm.shoulder_z = 0.60
        lm.solid_head_top = lambda min_fill=0.4: 0.97
        lm.fail = None
        lm.sanity()
        assert lm.fail and "shoulders at 0.60" in lm.fail, lm.fail
        assert 0.70 <= z0 <= 0.88
    finally:
        shutil.rmtree(d, ignore_errors=True)
