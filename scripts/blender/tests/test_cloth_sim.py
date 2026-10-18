"""Self-tests of step cloth_sim (_cloth_lib): segmentation of loose garments, simulation timeline and seamless
loops, capsule fitting, the proxy -> garment transfer, per-clip caches switched with the clip, cloth bones in the
GLB, the pass-through without garments, and a real batch character (test_real_*: skipped without the asset).

Synthetic: the procedural mannequin of _humanoid_lib (with a robe) rigged by rig_humanoid, two procedural clips
written here (a swinging walk loop and a fall) - no clip library needed.
Run: blender -b --factory-startup --python scripts/blender/tests/run_tests.py -- cloth_sim
"""
import atexit
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
from mathutils import Matrix, Quaternion, Vector  # noqa: E402

import af_common as af  # noqa: E402
from steps import _anim_lib as A  # noqa: E402
from steps import _cloth_lib as CL  # noqa: E402
from steps import _export_lib as xl  # noqa: E402

_CACHE = {}
LICH = r"C:/AstraForge/library/assets/e91bad4d-4ad9-40f5-aef9-d4f788d82705/v001/05_motion_style/styled.blend"


_TMP = []


def _tmp():
    d = tempfile.mkdtemp(prefix="af_cloth_")
    _TMP.append(d)
    atexit.register(shutil.rmtree, d, True)      # fallback: the shared runner ends with os._exit
    return d


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


def _procedural_clips(rig):
    """walk-loop (24 frames, legs and arms swing, hips sway; last frame = first) and death (30 frames: the body
    pitches forward and drops), written for every bone like retarget's actions."""
    tr = A.TargetRig(rig, A.map_armature(rig)[1])
    names = [b.name for b in tr.order]
    c = tr.cname

    def keys(n, fn):
        out = {nm: {"loc": [], "rot": []} for nm in names}
        for f in range(n):
            pose = fn(f)
            for nm in names:
                loc, q = pose.get(nm, (Vector(), Quaternion()))
                out[nm]["loc"].append(loc)
                out[nm]["rot"].append(q)
        return out

    def walk(f):
        ph = 2 * math.pi * f / 23.0
        p = {}
        for s, sg in (("Left", 1.0), ("Right", -1.0)):
            if c.get(s + "UpLeg"):
                p[c[s + "UpLeg"]] = (Vector(), Quaternion((1, 0, 0), 0.45 * sg * math.sin(ph)))
            if c.get(s + "Leg"):
                p[c[s + "Leg"]] = (Vector(), Quaternion((1, 0, 0), max(0.0, -0.6 * sg * math.sin(ph))))
            if c.get(s + "Arm"):
                p[c[s + "Arm"]] = (Vector(), Quaternion((1, 0, 0), -0.35 * sg * math.sin(ph)))
        p[c["Hips"]] = (Vector((0.0, 0.02 * math.cos(2 * ph), 0.0)), Quaternion((0, 1, 0), 0.08 * math.sin(ph)))
        return p

    def death(f):
        t = min(1.0, f / 20.0)
        e = t * t * (3 - 2 * t)
        return {c["Hips"]: (Vector((0.0, -0.75 * e, 0.0)), Quaternion((1, 0, 0), 1.35 * e))}

    a1 = A.write_action(rig, "walk-loop", keys(24, walk), 24)
    a1.use_cyclic = True
    a2 = A.write_action(rig, "death", keys(30, death), 30)
    for a in (a1, a2):
        a.use_fake_user = True
    return a1, a2


def _mannequin(robe=True):
    """Mannequin (robe) -> rig_humanoid -> two procedural clips: the animated .blend (cached)."""
    from steps import _humanoid_lib as hl
    key = "robe" if robe else "plain"
    if key in _CACHE and os.path.exists(_CACHE[key]):
        return _CACHE[key]
    d = _tmp()
    af.reset_scene()
    ob, _gt = hl.make_mannequin("A", robe=robe)
    glb = os.path.join(d, "mannequin.glb")
    af.export_glb(glb, objects=[ob], animations=False, skins=False)
    rig = _run("rig_humanoid", {"inputs": {"mesh": glb}, "params": {"deform_test": False, "intersections": False},
                                "out_dir": os.path.join(d, "rig")})
    assert rig["ok"], rig["failed"]
    af.open_blend(rig["outputs"]["blend"])
    arm = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    _procedural_clips(arm)
    path = af.save_blend(os.path.join(d, "animated.blend"))
    _CACHE[key] = path
    return path


def _cloth(blend, params, d):
    af.open_blend(blend)
    p = dict({"preview": False}, **params)
    return _run("cloth_sim", {"inputs": {"blend": blend}, "params": p, "out_dir": d})


# ------------------------------------------------------------------ pure

def test_sim_timeline_loop_and_oneshot():
    tl = CL.sim_timeline(31, True, preroll=10)
    P = 30
    assert tl["period"] == P and tl["warm"] == P and tl["blend"] == 10
    assert len(tl["times"]) == 10 + P + P + 1
    o = tl["offset"]
    assert tl["times"][o] == 0.0 and tl["times"][o + P] == 0.0          # clip time 0 and one period later
    assert all(t == tl["times"][0] for t in tl["times"][:10])            # the hold
    one = CL.sim_timeline(20, False, preroll=12, from_bind=5)
    assert one["offset"] == 17 and len(one["times"]) == 37
    assert one["blend_in"][0] == 0.0 and one["blend_in"][5] == 1.0 and one["blend_in"][2] < 1.0


def test_clip_window_seam_is_closed():
    tl = CL.sim_timeline(25, True, preroll=4)
    F = len(tl["times"])
    # a drifting signal: the raw cycle does not close, the window must
    S = np.zeros((F, 3, 3), np.float32)
    for f in range(F):
        S[f] += 0.01 * f + np.sin(2 * math.pi * tl["times"][f] / 24.0)
    w = CL.clip_window(S, tl, 25)
    assert w.shape == (25, 3, 3)
    assert np.allclose(w[24], w[0])
    jumps = np.abs(np.diff(w[:, 0, 0]))
    assert jumps.max() < 4 * np.median(jumps) + 1e-6       # no pop at the seam
    one = CL.clip_window(S, CL.sim_timeline(10, False, preroll=4), 10)
    assert np.allclose(one[0], S[4])


def test_capsules_distance_and_fit():
    a, b = np.array([0.0, 0.0, 0.0]), np.array([0.0, 0.0, 1.0])
    P = np.array([[0.2, 0.0, 0.5], [0.0, 0.0, 1.3], [0.05, 0.0, 0.5]])
    sd, t = CL.capsule_sd(P, a, b, 0.1, 0.1)
    assert abs(sd[0] - 0.1) < 1e-9 and abs(sd[1] - 0.2) < 1e-9 and sd[2] < 0
    caps, shrunk = CL.fit_capsules([("Spine", "s", a, b, 0.1, 0.1)], P, margin=0.01)
    assert "Spine" in shrunk
    sd2, _ = CL.capsule_sd(P, a, b, caps[0][4], caps[0][5])
    assert sd2[2] >= 0.0099 or caps[0][4] <= 0.045 + 1e-9   # outside now (or at the minimum radius)


def _grid(name, n=8, z=1.0, size=1.0):
    me = bpy.data.meshes.new(name)
    verts = [(size * i / (n - 1) - size / 2, 0.0, z - size * j / (n - 1)) for j in range(n) for i in range(n)]
    faces = [(j * n + i, j * n + i + 1, (j + 1) * n + i + 1, (j + 1) * n + i) for j in range(n - 1) for i in range(n - 1)]
    me.from_pydata(verts, [], faces)
    me.update()
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    return ob


def test_embedded_transfer_is_exact_at_rest_and_rigid():
    px = _grid("px", 6)
    import bmesh
    bm = bmesh.new()
    bm.from_mesh(px.data)
    bmesh.ops.triangulate(bm, faces=list(bm.faces))
    bm.to_mesh(px.data)
    bm.free()
    ren = _grid("ren", 15)
    co = np.empty(len(ren.data.vertices) * 3)
    ren.data.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    co[:, 1] += 0.01 * np.sin(co[:, 0] * 7)                 # the garment is a little off the proxy surface
    ren.data.vertices.foreach_set("co", co.ravel())
    ren.data.update()
    bind = CL.bind_embedded(ren, px)
    V = CL._object_rest(px)
    assert np.abs(CL.transfer_embedded(bind, V) - co).max() < 1e-9
    R = np.array(Matrix.Rotation(0.6, 3, Vector((0.3, 0.2, 0.9)).normalized()))
    moved = CL.transfer_embedded(bind, V @ R.T + np.array([0.4, -0.2, 0.1]))
    assert np.abs(moved - (co @ R.T + np.array([0.4, -0.2, 0.1]))).max() < 1e-6


def test_render_cache_switches_with_the_clip():
    """bake_render_cache + bind_cloth_cache: the clip's keys play, the previous clip's keys are reset (a key
    animated by the last clip keeps its value when its action is unassigned), Armature off while cached."""
    ob = _grid("garment", 4)
    arm = bpy.data.objects.new("Rig", bpy.data.armatures.new("Rig"))
    bpy.context.scene.collection.objects.link(arm)
    m = ob.modifiers.new("Armature", "ARMATURE")
    m.object = arm
    rest = CL._object_rest(ob)
    a_up = np.stack([rest + np.array([0, 0, 0.1 * f]) for f in range(5)])
    b_side = np.stack([rest + np.array([0.1 * f, 0, 0]) for f in range(3)])
    mapping = CL.bake_render_cache(ob, [("clipA", a_up), ("clipB", b_side)])
    assert set(mapping) == {"clipA", "clipB"}
    for nm in ("clipA", "clipB"):
        bpy.data.actions.new(nm)
    xl.bind_cloth_cache(["clipA"])
    bpy.context.scene.frame_set(4)
    assert np.allclose(CL.read_points(ob), a_up[4], atol=1e-5) and not m.show_render
    bpy.context.scene.frame_set(2)
    xl.bind_cloth_cache(["clipB"])
    bpy.context.scene.frame_set(1)
    assert np.allclose(CL.read_points(ob), b_side[1], atol=1e-5)
    xl.bind_cloth_cache(None)
    bpy.context.scene.frame_set(1)
    assert np.allclose(CL.read_points(ob), rest, atol=1e-5) and m.show_render


# ------------------------------------------------------------------ synthetic character

def test_segment_finds_the_mannequin_robe():
    blend = _mannequin(robe=True)
    af.open_blend(blend)
    rig = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    body = max((o for o in bpy.data.objects if o.type == "MESH"), key=lambda o: len(o.data.vertices))
    sk = CL.Skeleton(rig)
    seg = CL.segment(body, sk, {"build": "robed"})
    regions = seg["stats"]["regions"]
    assert any(k in regions for k in ("robe", "skirt")), regions
    z = seg["P"][:, 2]
    m = seg["mask"]
    assert z[m].max() < sk.hip_z + 0.06 * sk.H                   # nothing above the waist band
    assert (seg["pin"][m & (z > sk.hip_z)] == 1.0).all()           # the waist band follows the pelvis
    low = m & (z < sk.knee_z)
    assert low.any() and float(np.median(seg["pin"][low])) < 0.2   # the hem is free
    off = CL.segment(body, sk, {"cloth": False})
    assert not off["mask"].any()
    box = CL.segment(body, sk, {"cloth": [{"kind": "cape", "min": [-1, 0.0, 1.0], "max": [1, 1, 1.4], "only": True}]})
    assert set(box["stats"]["regions"]) == {"cape"}


def test_cloth_sim_mannequin_robe_end_to_end():
    blend = _mannequin(robe=True)
    d = _tmp()
    try:
        res = _cloth(blend, {"build": "robed", "preview": True}, os.path.join(d, "out"))
        assert res["ok"], (res["failed"], res.get("error"))
        st = res["stats"]
        assert "lower" in st["groups"], st["groups"]
        for k in ("blend", "glb", "clips", "report", "preview", "segmentation"):
            assert os.path.exists(res["outputs"][k]), k
        rep = json.load(open(res["outputs"]["report"], encoding="utf-8"))
        walk = rep["clips"]["walk-loop"]["groups"]["lower"]
        death = rep["clips"]["death"]["groups"]["lower"]
        assert rep["clips"]["walk-loop"]["mode"] == "cloth" and walk["proxy"]["simulated"]
        assert walk["after"]["penetration_frac"] <= walk["before"]["penetration_frac"] + 0.01
        assert death["death_floor_min_cm"] >= -2.0
        assert rep["timing"]["total_s"] < 300
        # GLB: same clip names, cloth bones animated
        summ = A.glb_summary(res["outputs"]["glb"])
        assert sorted(a["name"] for a in summ["animations"]) == ["death", "walk-loop"]
        js, _bin = A.read_glb(res["outputs"]["glb"])
        bones = [n.get("name", "") for n in js["nodes"]]
        assert any(b.startswith("cloth_lower_") for b in bones), bones[:40]
        assert st["cloth_bones"] > 0
        # the saved scene plays the cache with the clip (render_sprites path)
        af.open_blend(res["outputs"]["blend"])
        rig = next(o for o in bpy.data.objects if o.type == "ARMATURE")
        g = bpy.data.objects["Body_cloth_lower"]
        act = bpy.data.actions["walk-loop"]
        xl.assign_action(rig, act)
        xl.bind_cloth_cache(None)
        bpy.context.scene.frame_set(12)
        skin = CL.read_points(g)
        xl.bind_cloth_cache([act])
        bpy.context.scene.frame_set(12)
        cloth = CL.read_points(g)
        fw = CL.free_weights(g)
        moved = np.linalg.norm(cloth - skin, axis=1)
        assert moved[fw > 0.8].max() > 0.01                       # the free cloth is simulated
        seam = fw < 1e-3
        assert moved[seam].max() < 0.01                           # the welded seam follows the skin
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_cloth_sim_passes_through_without_garments():
    blend = _mannequin(robe=False)
    d = _tmp()
    try:
        res = _cloth(blend, {}, os.path.join(d, "out"))
        assert res["ok"], res["failed"]
        assert not _check(res, "cloth_regions")["ok"]
        assert any("no loose garment" in w for w in res["warnings"])
        assert os.path.exists(res["outputs"]["glb"]) and os.path.exists(res["outputs"]["blend"])
    finally:
        shutil.rmtree(d, ignore_errors=True)


# ------------------------------------------------------------------ real

def test_real_lich_robe_settles_and_stays_out_of_the_body():
    if not os.path.exists(LICH):
        return "skip: lich asset missing"
    d = _tmp()
    try:
        res = _cloth(LICH, {"build": "skeleton", "clips": ["walk", "death"], "cloth_bones": False},
                     os.path.join(d, "out"))
        assert res["ok"], res["failed"]
        rep = json.load(open(res["outputs"]["report"], encoding="utf-8"))
        death = rep["clips"]["death"]["groups"]["lower"]
        walk = rep["clips"]["walk-loop"]["groups"]["lower"]
        assert death["death_lying_height_cm"] < death["death_lying_height_skinned_cm"] - 15   # no stiff bell
        assert death["death_floor_min_cm"] >= -2.0
        assert walk["after"]["penetration_frac"] < walk["before"]["penetration_frac"]
        assert rep["clips"]["idle-loop"]["mode"] == "skinned"
        assert rep["timing"]["total_s"] < 180
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_zz_cleanup_cached_characters():
    """Runs last (alphabetical): removes the cached mannequin builds and any test folder left behind."""
    for d in list(_TMP):
        shutil.rmtree(d, ignore_errors=True)
    _CACHE.clear()
