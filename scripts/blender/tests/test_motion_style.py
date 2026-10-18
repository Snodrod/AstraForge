"""Self-tests of step motion_style (_style_lib): style resolution, per-clip weights, stride without skating,
upright held props, seamless loops and the procedural deaths ending on the floor.

Synthetic: the procedural mannequin of _humanoid_lib is rigged by rig_humanoid and gets library clips by
retarget (test_real_*: they need the clip library on disk and return early without it).
Run: blender -b --factory-startup --python scripts/blender/tests/run_tests.py -- motion_style
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
from mathutils import Quaternion, Vector  # noqa: E402

import af_common as af  # noqa: E402
from steps import _anim_lib as A  # noqa: E402
from steps import _humanoid_lib as hl  # noqa: E402
from steps import _style_lib as SL  # noqa: E402

LIB = A.DEFAULT_LIBRARY
CLIPS = ["mixamo-militia-idle", "mixamo-militia-walk", "mixamo-militia-death"]
_CACHE = {}


def _tmp():
    return tempfile.mkdtemp(prefix="af_style_")


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
    full["ok"] = not failed and full.get("ok", True)
    full["failed"] = [c["name"] for c in failed]
    return full


def _check(res, name):
    return next((c for c in res["checks"] if c["name"] == name), None)


def _have_library():
    idx = os.path.join(LIB, "index.json")
    if not os.path.exists(idx):
        return False
    ids = {c["id"] for c in json.load(open(idx, encoding="utf-8")).get("clips", [])}
    return all(c in ids for c in CLIPS)


def _animated(staff=False, robe=False):
    """Mannequin -> rig_humanoid -> retarget (militia idle / walk / death): the animated .blend (cached)."""
    key = ("staff" if staff else "plain") + ("_robe" if robe else "")
    if key in _CACHE and os.path.exists(_CACHE[key]):
        return _CACHE[key]
    d = _tmp()
    af.reset_scene()
    ob, _gt = hl.make_mannequin("A", staff=staff, robe=robe)
    glb = os.path.join(d, "mannequin.glb")
    af.export_glb(glb, objects=[ob], animations=False, skins=False)
    rig = _run("rig_humanoid", {"inputs": {"mesh": glb}, "params": {"deform_test": False, "intersections": False},
                                "out_dir": os.path.join(d, "rig")})
    assert rig["ok"], rig["failed"]
    af.open_blend(rig["outputs"]["blend"])
    ret = _run("retarget", {"inputs": {"blend": rig["outputs"]["blend"]}, "out_dir": os.path.join(d, "ret"), "params": {
        "root_motion": "lock", "preview": False, "library": LIB, "clips": [
            {"source": "library:" + CLIPS[0], "name": "idle", "loop": True},
            {"source": "library:" + CLIPS[1], "name": "walk", "loop": True, "in_place": True},
            {"source": "library:" + CLIPS[2], "name": "death", "loop": False}]}})
    assert ret["ok"], ret["failed"]
    _CACHE[key] = ret["outputs"]["blend"]
    return _CACHE[key]


def _style(blend, params, d):
    af.open_blend(blend)
    p = dict({"preview": False}, **params)
    return _run("motion_style", {"inputs": {"blend": blend}, "params": p, "out_dir": d})


def _clips(res):
    return {c["gltf_name"]: c for c in json.load(open(res["outputs"]["clips"], encoding="utf-8"))["clips"]}


# ------------------------------------------------------------------ pure

def test_style_from_seed_is_deterministic_and_varied():
    a = SL.style_from_seed("Knight_holding_sword_and_shield_20261010233748_2.jpg")
    b = SL.style_from_seed("Knight_holding_sword_and_shield_20261010233748_2.jpg")
    c = SL.style_from_seed("Knight_holding_sword_and_shield_20261010233748_3.jpg")
    assert a == b
    assert any(abs(a[k] - c[k]) > 0.05 for k in ("tempo", "stride", "arm_swing", "lean", "sway")), (a, c)
    for k, (lo, hi) in SL.RANGES.items():
        assert lo <= a[k] <= hi, (k, a[k])
    # 60 seeds: knobs spread over their bands, some limp, some swagger, most do not
    draws = [SL.style_from_seed("unit_%d" % i) for i in range(60)]
    tempos = sorted(s["tempo"] for s in draws)
    assert tempos[-1] - tempos[0] > 0.2
    limping = sum(1 for s in draws if s["limp"] > 0)
    assert 2 <= limping <= 20, limping
    # build bias: heavy is slower and stiffer than light for the same seed
    h, l_ = SL.style_from_seed("x", "heavy"), SL.style_from_seed("x", "light")
    assert h["tempo"] < l_["tempo"] and h["stiffness"] > l_["stiffness"]
    z = SL.style_from_seed("x", "zombie")
    assert z["limp"] >= 0.5 or SL.style_from_seed("x")["limp"] > 0


def test_resolve_style_explicit_wins_and_ranges_checked():
    s, src = SL.resolve_style({"seed": 5, "tempo": 1.2, "limp_side": "r"})
    assert s["tempo"] == 1.2 and src["tempo"] == "param" and src["stride"] == "seed" and s["limp_side"] == "r"
    n, nsrc = SL.resolve_style({})
    assert n["tempo"] == 1.0 and n["lean"] == 0.0 and nsrc["tempo"] == "neutral"
    for bad in ({"tempo": 2.0}, {"stride": 0.1}, {"limp_side": "x"}):
        try:
            SL.resolve_style(bad)
            raise AssertionError("accepted %s" % bad)
        except ValueError:
            pass


def test_category_weights_and_death_choice():
    s, _ = SL.resolve_style({"tempo": 1.2, "stride": 1.3, "arm_swing": 1.6, "lean": 1.0})
    w = SL.clip_style(s, "walk")
    a = SL.clip_style(s, "attack")
    d = SL.clip_style(s, "death")
    assert w["stride"] == 1.3 and w["arm_swing"] == 1.6
    assert a["stride"] == 1.0 and a["arm_swing"] == 1.0 and abs(a["lean"] - 0.6) < 1e-9
    assert all(d[k] == SL.NEUTRAL[k] for k in SL.STYLE_KEYS)
    assert SL.clip_style(s, "walk", {"stride": 0.8})["stride"] == 0.8
    living = {SL.death_choice({"death": "auto"}, "seed_%d" % i, undead=False) for i in range(80)}
    undead = {SL.death_choice({"death": "auto"}, "seed_%d" % i, undead=True) for i in range(80)}
    assert "crumble" not in living and len(living) >= 6, living
    assert "crumble" in undead, undead
    assert SL.death_choice({}, 1) == "library" and SL.death_choice({"death": "spin_fall"}, 1) == "spin_fall"


def test_qpow_and_resample_keep_loop_ends():
    q = Quaternion((0.0, 0.0, 1.0), math.radians(30.0))
    assert abs(math.degrees(SL.qpow(q, 2.0).angle) - 60.0) < 1e-4
    assert abs(math.degrees(SL.qpow(q, 0.5).angle) - 15.0) < 1e-4

    class _R:
        names = ["a"]
    Ls = [{"a": Quaternion((0.0, 0.0, 1.0), math.radians(10.0 * i))} for i in range(21)]
    Ls[-1] = {"a": Ls[0]["a"].copy()}
    hips = [Vector((0.0, 0.0, 1.0 + 0.01 * math.sin(i))) for i in range(21)]
    hips[-1] = hips[0].copy()
    L2, h2, _r, real = SL.resample(_R(), Ls, hips, [None] * 21, 1.2, True)
    assert len(L2) == int(round(20 / 1.2)) + 1 and abs(real - 20 / (len(L2) - 1)) < 1e-9
    assert L2[0]["a"].rotation_difference(L2[-1]["a"]).angle < 1e-6 and (h2[0] - h2[-1]).length < 1e-9


# ------------------------------------------------------------------ on a rigged mannequin

def test_real_style_changes_motion_keeps_feet_floor_and_seams():
    """Explicit style on idle/walk: GLB names kept (-loop), seams closed, the walk is faster and longer
    (ground speed x stride x tempo) with no more skating than the input, nothing under the floor."""
    if not _have_library():
        return
    blend = _animated()
    d = _tmp()
    try:
        base = _style(blend, {"apply": []}, os.path.join(d, "neutral"))
        res = _style(blend, {"tempo": 1.15, "stride": 1.25, "lean": 0.6, "hunch": 0.5, "sway": 1.6, "arm_swing": 1.5,
                             "limp": 0.5, "limp_side": "r"}, os.path.join(d, "styled"))
        assert res["ok"], res["failed"]
        c = _clips(res)
        assert set(c) == {"idle-loop", "walk-loop", "death"}
        assert sorted(_check(res, "gltf_animations")["value"]) == ["death", "idle-loop", "walk-loop"]
        for n in ("idle-loop", "walk-loop"):
            assert _check(res, "loop_seam_" + n)["ok"], _check(res, "loop_seam_" + n)
        w, w0 = c["walk-loop"], _clips(base)["walk-loop"]
        assert w["frames"] < w0["frames"], (w["frames"], w0["frames"])              # tempo 1.15
        assert w["ground_speed_mps"] > 1.15 * w["ground_speed_in_mps"], (w["ground_speed_mps"], w["ground_speed_in_mps"])
        assert w["foot_skate_cm"] <= max(2.0, 1.3 * w["foot_skate_in_cm"]), (w["foot_skate_cm"], w["foot_skate_in_cm"])
        assert w["floor"]["body_min_z_m"] >= -0.02, w["floor"]
        assert c["idle-loop"]["foot_skate_cm"] <= max(2.0, 1.3 * c["idle-loop"]["foot_skate_in_cm"])
        # the style really changed the pose: hips height/lean differ from the neutral run on the same frame
        af.open_blend(res["outputs"]["blend"])
        rig = next(o for o in bpy.context.scene.objects if o.type == "ARMATURE")
        R = SL.Rig(rig)
        styled = SL.pose_from_basis(R, SL.read_action(R, bpy.data.actions["idle-loop"], 1)[0])
        af.open_blend(base["outputs"]["blend"])
        rig = next(o for o in bpy.context.scene.objects if o.type == "ARMATURE")
        R0 = SL.Rig(rig)
        neutral = SL.pose_from_basis(R0, SL.read_action(R0, bpy.data.actions["idle-loop"], 1)[0])
        sp = R.bone("Spine2")
        ang = math.degrees(styled.qw[sp].rotation_difference(neutral.qw[sp]).angle)
        assert ang > 5.0, ang
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_real_seeds_give_different_walks():
    if not _have_library():
        return
    blend = _animated()
    d = _tmp()
    try:
        styles = []
        for i, seed in enumerate(("Knight_holding_sword_and_shield_2.jpg", "Knight_holding_sword_and_shield_3.jpg")):
            res = _style(blend, {"seed": seed, "build": "armored", "apply": ["walk"]}, os.path.join(d, str(i)))
            assert res["ok"], res["failed"]
            styles.append((res["stats"]["style"], _clips(res)["walk-loop"]))
        (s1, w1), (s2, w2) = styles
        assert s1 != s2
        assert w1["frames"] != w2["frames"] or abs(w1["ground_speed_mps"] - w2["ground_speed_mps"]) > 0.03, (w1, w2)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_real_all_death_variants_end_on_the_floor_and_differ():
    """Every procedural death: impact after a physical fall time, the last frame lies ON the floor
    (death_on_floor), no frame deep under it, and the variants end in different places/poses."""
    if not _have_library():
        return
    blend = _animated()
    d = _tmp()
    try:
        ends = {}
        for v in SL.DEATH_VARIANTS:
            res = _style(blend, {"seed": "dead_" + v, "death": v, "undead": v == "crumble", "apply": []},
                         os.path.join(d, v))
            assert res["ok"], (v, res["failed"])
            c = _clips(res)["death"]
            info = c["death_info"]
            assert c["mode"] == "procedural_death" and c["death"] == v
            assert 0.4 < info["impact_s"] < 1.8, (v, info["impact_s"])
            assert _check(res, "death_on_floor_death")["ok"], (v, _check(res, "death_on_floor_death"))
            assert c["floor"]["body_min_z_m"] >= -0.03, (v, c["floor"])
            af.open_blend(res["outputs"]["blend"])
            rig = next(o for o in bpy.context.scene.objects if o.type == "ARMATURE")
            R = SL.Rig(rig)
            act = bpy.data.actions["death"]
            n = int(round(act.frame_range[1])) + 1
            last = SL.pose_from_basis(R, SL.read_action(R, act, n)[-1])
            ends[v] = (last.hips.copy(), last.qw[R.bone("Spine2")].copy())
        names = list(ends)
        for i in range(len(names)):
            for j in range(i + 1, len(names)):
                (ha, qa), (hb, qb) = ends[names[i]], ends[names[j]]
                differ = (ha - hb).length > 0.1 or math.degrees(qa.rotation_difference(qb).angle) > 20.0
                assert differ, (names[i], names[j])
        # lying variants end low, the sitting slump sits, the falls lie
        assert ends["fall_back"][0].z < 0.45 and ends["fall_forward"][0].z < 0.45
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_real_staff_stays_upright_in_the_walk():
    """22-bone militia clips have no hand keys: without hold_props a staff follows the forearm swing; with it
    the wrist compensates and the staff stays near its rest axis (and its foot never under the floor)."""
    if not _have_library():
        return
    blend = _animated(staff=True)
    d = _tmp()
    try:
        tilt = {}
        for hold in (False, True):
            res = _style(blend, {"hold_props": hold, "arm_swing": 1.8, "apply": ["walk"]}, os.path.join(d, str(hold)))
            assert res["ok"], res["failed"]
            af.open_blend(res["outputs"]["blend"])
            rig = next(o for o in bpy.context.scene.objects if o.type == "ARMATURE")
            R = SL.Rig(rig)
            props = SL.held_props(R, A.skinned_meshes(rig))
            assert "Left" in props and props["Left"]["upright"], props
            pr = props["Left"]
            act = bpy.data.actions["walk-loop"]
            n = int(round(act.frame_range[1])) + 1
            worst = 0.0
            hn = R.bone("LeftHand")
            for b in SL.read_action(R, act, n):
                p = SL.pose_from_basis(R, b)
                ax = (p.qw[hn] @ pr["hand_rest_q"].inverted()) @ pr["axis_rest"]
                worst = max(worst, math.degrees(ax.angle(Vector((0.0, 0.0, 1.0)))))
            tilt[hold] = worst
            if hold:
                assert _clips(res)["walk-loop"]["floor"]["body_min_z_m"] >= -0.02
        assert tilt[True] < tilt[False] - 5.0 and tilt[True] < 25.0, tilt
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _glb_weights(path):
    """{animation: [morph weights over time]} of the first morph target of a GLB."""
    import struct
    b = open(path, "rb").read()
    n = struct.unpack_from("<I", b, 12)[0]
    js = json.loads(b[20:20 + n])
    bin0 = 20 + n + 8
    out = {}
    for a in js.get("animations", []):
        ch = next((c for c in a["channels"] if c["target"]["path"] == "weights"), None)
        if ch is None:
            continue
        acc = js["accessors"][a["samplers"][ch["sampler"]]["output"]]
        bv = js["bufferViews"][acc["bufferView"]]
        off = bin0 + bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
        out[a["name"]] = list(struct.unpack_from("<%df" % acc["count"], b, off))
    return out, [m.get("extras", {}).get("targetNames") for m in js.get("meshes", [])]


def test_real_clip_deform_qa_measures_the_real_clips():
    """motion_style measures torn surface on every final clip (every 2nd frame, the pose-test limits): the clean
    mannequin passes on idle / walk / death with the numbers in clips.json. Mutation: the left hand weighted to
    the chest (a fused sleeve/prop) tears on the real clips - the step fails with clip_torn_* (error gate) and
    only reports it with deform_gate warning."""
    if not _have_library():
        return
    blend = _animated()
    d = _tmp()
    try:
        res = _style(blend, {"seed": "qa_clean"}, os.path.join(d, "clean"))
        assert res["ok"], res["failed"]
        c = _clips(res)
        for n in ("idle-loop", "walk-loop", "death"):
            q = c[n]["deform"]
            assert q["ok"] and q["worst_torn_frac"] <= q["worst_limit"], (n, q)
            assert q["frames_measured"] >= c[n]["frames"] // 2, (n, q["frames_measured"], c[n]["frames"])
            assert _check(res, "clip_torn_" + n)["ok"] and _check(res, "clip_torn_" + n)["severity"] == "error"
        assert set(res["stats"]["deform_qa"]) == {"idle-loop", "walk-loop", "death"}
        # mutation: left hand vertices follow Spine2 instead of the hand
        af.open_blend(blend)
        rig = next(o for o in bpy.context.scene.objects if o.type == "ARMATURE")
        body = A.skinned_meshes(rig)[0]
        groups = {g.index: g.name for g in body.vertex_groups}
        spine = body.vertex_groups[hl.b("Spine2")]
        moved = []
        for v in body.data.vertices:
            best = max(v.groups, key=lambda g: g.weight, default=None)
            if best is not None and groups.get(best.group) == hl.b("LeftHand"):
                moved.append(v.index)
        assert len(moved) > 50, len(moved)
        for g in body.vertex_groups:
            g.remove(moved)
        spine.add(moved, 1.0, "REPLACE")
        bad_blend = os.path.join(d, "fused.blend")
        af.save_blend(bad_blend)
        bad = _style(bad_blend, {"seed": "qa_clean"}, os.path.join(d, "fused"))
        assert not bad["ok"] and any(n.startswith("clip_torn_") for n in bad["failed"]), bad["failed"]
        worst = max(v["worst_torn_frac"] for v in bad["stats"]["deform_qa"].values())
        assert worst > 0.01, bad["stats"]["deform_qa"]
        warn = _style(bad_blend, {"seed": "qa_clean", "deform_gate": "warning"}, os.path.join(d, "fused_w"))
        assert warn["ok"], warn["failed"]
        assert any(not ch["ok"] and ch["severity"] == "warning" for ch in warn["checks"] if ch["name"].startswith("clip_torn_"))
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_real_robe_death_settles_and_exports_morph():
    """A robed body lying after a procedural death: the robe cone used to stick up like a bell (pilot lich). The
    corrective shape key af_death_settle lowers the cloth onto the legs / floor at the end of the death; it is
    driven by a Hips custom property the death ramps 0 -> 1 (other clips key 0), and the GLB carries it as
    morph-weight animation. The body still ends on the floor."""
    if not _have_library():
        return
    blend = _animated(robe=True)
    d = _tmp()
    try:
        res = _style(blend, {"seed": "robe", "death": "fall_back", "deform_gate": "warning"}, os.path.join(d, "robe"))
        assert res["ok"], res["failed"]
        c = _clips(res)["death"]
        st = c["death_info"]["robe_settle"]
        assert st["applied"] and st["vertices"] > 100, st
        assert st["bell_height_after_cm"] < 0.5 * st["bell_height_cm"], st
        assert _check(res, "death_on_floor_death")["ok"], _check(res, "death_on_floor_death")
        w, names = _glb_weights(res["outputs"]["glb"])
        assert any(t and "af_death_settle" in t for t in names), names
        assert abs(w["death"][0]) < 1e-3 and w["death"][-1] > 0.99, (w["death"][:3], w["death"][-3:])
        for n in ("idle-loop", "walk-loop"):
            assert max(abs(x) for x in w[n]) < 1e-3, (n, w[n][:4])
        # without settle: no shape key, the robe stays a stiff cone
        res0 = _style(blend, {"seed": "robe", "death": "fall_back", "deform_gate": "warning", "robe_settle": False},
                      os.path.join(d, "robe0"))
        assert "robe_settle" not in _clips(res0)["death"]["death_info"]
        w0, names0 = _glb_weights(res0["outputs"]["glb"])
        assert not w0 and not any(names0), (list(w0), names0)
    finally:
        shutil.rmtree(d, ignore_errors=True)
