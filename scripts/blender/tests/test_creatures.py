"""Self-tests for the creatures module (rig_creature + anim_creature).

Synthetic creatures are built from primitives (bmesh) inside the test, exported to a temporary GLB in
a non-default facing, then rigged and animated through the real steps. Tests on real assets are
named test_real_* and return early when the files are missing.

Run (until the shared runner exists):
  blender -b --factory-startup --python-expr "import sys;sys.path[:0]=[r'<root>/scripts/blender',
  r'<root>/scripts/blender/tests'];import test_creatures as t;import af_common as af
  for n in sorted(dir(t)):
      if n.startswith('test_'): af.reset_scene(); getattr(t,n)(); print('PASS',n)"
"""
import json
import math
import os
import tempfile

import bmesh
import bpy
from mathutils import Matrix, Quaternion, Vector

import af_common as af
from steps import _creature_lib as L
from steps import anim_creature, rig_creature

KEEP = os.environ.get("AF_CREATURE_TEST_OUT")       # optional: keep outputs in this folder


# ----------------------------------------------------------------------------- primitives

def _ellipsoid(bm, c, r, segs=20, rings=10, rot=None):
    m = Matrix.Translation(Vector(c)) @ (rot or Matrix.Identity(4)) @ Matrix.Diagonal((r[0], r[1], r[2], 1.0))
    bmesh.ops.create_uvsphere(bm, u_segments=segs, v_segments=rings, radius=1.0, matrix=m)


def _capsule(bm, a, b, r, segs=10, joints=True):
    a, b = Vector(a), Vector(b)
    d = b - a
    q = Vector((0, 0, 1)).rotation_difference(d.normalized())
    m = Matrix.Translation((a + b) * 0.5) @ q.to_matrix().to_4x4()
    bmesh.ops.create_cone(bm, cap_ends=True, segments=segs, radius1=r, radius2=r, depth=d.length, matrix=m)
    if joints:
        for p in (a, b):
            bmesh.ops.create_uvsphere(bm, u_segments=segs, v_segments=6, radius=r,
                                      matrix=Matrix.Translation(p))


def _polyleg(bm, pts, r):
    for a, b in zip(pts[:-1], pts[1:]):
        _capsule(bm, a, b, r)


def _box(bm, c, size, rot=None, cuts=5):
    """Subdivided box (a skinned plank needs inner vertices to bend)."""
    m = Matrix.Translation(Vector(c)) @ (rot or Matrix.Identity(4)) @ Matrix.Diagonal((size[0], size[1], size[2], 1.0))
    tmp = bmesh.new()
    bmesh.ops.create_cube(tmp, size=1.0)
    bmesh.ops.subdivide_edges(tmp, edges=tmp.edges[:], cuts=cuts, use_grid_fill=True)
    tmp.transform(m)
    me = bpy.data.meshes.new("_box_tmp")
    tmp.to_mesh(me)
    tmp.free()
    bm.from_mesh(me)
    bpy.data.meshes.remove(me)


def _finish(bm, name, path, yaw_deg):
    bmesh.ops.rotate(bm, verts=bm.verts, cent=(0, 0, 0), matrix=Matrix.Rotation(math.radians(yaw_deg), 3, "Z"))
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    af.export_glb(path, objects=[ob], animations=False, skins=False)
    bpy.data.objects.remove(ob, do_unlink=True)
    return path


# All builders model the creature facing -Y (head at -Y, left side +X); yaw turns the export.

def build_insect(path, yaw_deg=90.0):
    bm = bmesh.new()
    _ellipsoid(bm, (0, -0.43, 0.23), (0.065, 0.07, 0.06))                   # head
    _ellipsoid(bm, (0, -0.22, 0.22), (0.09, 0.15, 0.075))                   # thorax
    _ellipsoid(bm, (0, 0.18, 0.24), (0.12, 0.27, 0.11))                     # abdomen
    _capsule(bm, (0.03, -0.49, 0.22), (0.06, -0.55, 0.16), 0.012)           # mandibles
    _capsule(bm, (-0.03, -0.49, 0.22), (-0.06, -0.55, 0.16), 0.012)
    for y0, dy in ((-0.31, (-0.08, -0.15, -0.18)), (-0.22, (0.0, 0.0, 0.0)), (-0.13, (0.08, 0.16, 0.2))):
        for s in (1, -1):
            pts = [(s * 0.07, y0, 0.21), (s * 0.27, y0 + dy[0], 0.34), (s * 0.43, y0 + dy[1], 0.05),
                   (s * 0.49, y0 + dy[2], 0.012)]
            _polyleg(bm, pts, 0.018)
    return _finish(bm, "insect", path, yaw_deg)


def build_spider(path, yaw_deg=0.0):
    bm = bmesh.new()
    _ellipsoid(bm, (0, -0.1, 0.22), (0.12, 0.15, 0.08))                     # cephalothorax
    _ellipsoid(bm, (0, -0.24, 0.24), (0.07, 0.06, 0.055))                   # head bump / eyes
    _ellipsoid(bm, (0, 0.27, 0.27), (0.17, 0.22, 0.15))                     # abdomen
    _capsule(bm, (0.035, -0.28, 0.22), (0.04, -0.31, 0.15), 0.016)          # chelicerae
    _capsule(bm, (-0.035, -0.28, 0.22), (-0.04, -0.31, 0.15), 0.016)
    legs = [(-0.18, (-0.22, -0.38, -0.44)), (-0.11, (-0.07, -0.14, -0.16)),
            (-0.04, (0.07, 0.14, 0.16)), (0.03, (0.22, 0.4, 0.46))]
    for y0, dy in legs:
        for s in (1, -1):
            pts = [(s * 0.09, y0, 0.21), (s * 0.36, y0 + dy[0], 0.44), (s * 0.6, y0 + dy[1], 0.07),
                   (s * 0.66, y0 + dy[2], 0.012)]
            _polyleg(bm, pts, 0.016)
    return _finish(bm, "spider", path, yaw_deg)


def build_serpent(path, yaw_deg=180.0):
    bm = bmesh.new()
    n_ring, n_seg = 12, 70
    rings = []
    for i in range(n_seg + 1):
        u = i / n_seg                                       # 0 at the neck, 1 at the tail tip
        y = -0.9 + 1.8 * u
        x = 0.1 * math.sin(math.pi * 1.6 * u)
        r = 0.065 * (1.0 - 0.85 * max(0.0, u - 0.35) / 0.65) if u > 0.35 else 0.058 + 0.02 * u
        z = r
        ring = []
        for k in range(n_ring):
            a = 2 * math.pi * k / n_ring
            ring.append(bm.verts.new((x + r * math.cos(a), y, z + r * math.sin(a))))
        rings.append(ring)
    for i in range(n_seg):
        for k in range(n_ring):
            bm.faces.new((rings[i][k], rings[i][(k + 1) % n_ring], rings[i + 1][(k + 1) % n_ring], rings[i + 1][k]))
    bm.faces.new(rings[-1][::-1])
    bm.faces.new(rings[0])
    _ellipsoid(bm, (0, -0.98, 0.1), (0.075, 0.11, 0.045))                   # upper head
    _ellipsoid(bm, (0, -1.0, 0.018), (0.055, 0.095, 0.016))                 # lower jaw (open mouth)
    _ellipsoid(bm, (0, -0.91, 0.06), (0.05, 0.04, 0.045))                   # jaw hinge / throat
    return _finish(bm, "serpent", path, yaw_deg)


def build_bird(path, yaw_deg=-90.0):
    bm = bmesh.new()
    tilt = Matrix.Rotation(math.radians(-12), 4, "X")
    _ellipsoid(bm, (0, 0.0, 0.43), (0.11, 0.18, 0.12), rot=tilt)            # body
    _capsule(bm, (0, -0.1, 0.5), (0, -0.16, 0.66), 0.045)                   # neck
    _ellipsoid(bm, (0, -0.19, 0.7), (0.055, 0.07, 0.055))                   # head
    _capsule(bm, (0, -0.25, 0.69), (0, -0.33, 0.67), 0.016)                 # beak
    _box(bm, (0, 0.25, 0.39), (0.16, 0.2, 0.025), rot=Matrix.Rotation(math.radians(15), 4, "X"))  # tail fan
    for s in (1, -1):                                                        # half-spread wings
        # three planks that overlap by 2 cm at the same height (the earlier tilted planks did not even
        # touch at rest: a 5 cm vertical step at the elbow that looked like a skinning tear in flight)
        _box(bm, (s * 0.19, -0.03, 0.5), (0.26, 0.16, 0.025))
        _box(bm, (s * 0.415, 0.0, 0.505), (0.23, 0.15, 0.022))
        _box(bm, (s * 0.6, 0.04, 0.51), (0.18, 0.12, 0.02))
        hip, ankle, mtp = (s * 0.06, 0.02, 0.33), (s * 0.06, 0.075, 0.16), (s * 0.06, 0.02, 0.03)
        _capsule(bm, hip, ankle, 0.02)
        _capsule(bm, ankle, mtp, 0.014)
        for ang in (-25, 0, 25):                                             # three toes forward
            a = math.radians(ang)
            _capsule(bm, mtp, (mtp[0] + 0.085 * math.sin(a), mtp[1] - 0.085 * math.cos(a), 0.012), 0.011)
        _capsule(bm, mtp, (mtp[0], mtp[1] + 0.045, 0.012), 0.01)              # hallux
    return _finish(bm, "bird", path, yaw_deg)


def build_bird_folded(path, yaw_deg=180.0):
    """Bird at rest with folded wings (flattened ellipsoids against the flanks, tips over the tail base)
    and feet whose three forward toes and hallux touch the ground ~8 cm apart: two ground clusters per
    foot that only join above the ground, through the metatarsal joint."""
    bm = bmesh.new()
    tilt = Matrix.Rotation(math.radians(-12), 4, "X")
    _ellipsoid(bm, (0, 0.0, 0.43), (0.11, 0.18, 0.12), rot=tilt)            # body
    _capsule(bm, (0, -0.1, 0.5), (0, -0.16, 0.66), 0.045)                   # neck
    _ellipsoid(bm, (0, -0.19, 0.7), (0.055, 0.07, 0.055))                   # head
    _capsule(bm, (0, -0.25, 0.69), (0, -0.33, 0.67), 0.016)                 # beak
    _box(bm, (0, 0.25, 0.39), (0.16, 0.2, 0.025), rot=Matrix.Rotation(math.radians(15), 4, "X"))  # tail fan
    for s in (1, -1):
        _ellipsoid(bm, (s * 0.1, 0.06, 0.47), (0.032, 0.2, 0.075),
                   rot=Matrix.Rotation(math.radians(-8), 4, "X"))           # folded wing
        hip, ankle, mtp = (s * 0.06, 0.02, 0.33), (s * 0.06, 0.075, 0.17), (s * 0.06, 0.02, 0.05)
        _capsule(bm, hip, ankle, 0.02)
        _capsule(bm, ankle, mtp, 0.014)
        for ang in (-25, 0, 25):                                             # three toes forward
            a = math.radians(ang)
            k = (mtp[0] + 0.035 * math.sin(a), mtp[1] - 0.035 * math.cos(a), 0.012)
            e = (mtp[0] + 0.095 * math.sin(a), mtp[1] - 0.095 * math.cos(a), 0.012)
            _polyleg(bm, [mtp, k, e], 0.011)
        _polyleg(bm, [mtp, (mtp[0], mtp[1] + 0.04, 0.012), (mtp[0], mtp[1] + 0.075, 0.012)], 0.01)  # hallux
    return _finish(bm, "bird_folded", path, yaw_deg)


def build_dog_hierarchy(path):
    """Kitbashed dog: separate objects under a parent empty with scale 0.01 and a quaternion yaw of 90 deg
    (mesh data in centimetres, head ends up at +X), like many glTF exports from DCC tools."""
    root = bpy.data.objects.new("dog_root", None)
    bpy.context.scene.collection.objects.link(root)
    root.rotation_mode = "QUATERNION"
    root.rotation_quaternion = Quaternion((0, 0, 1), math.radians(90))
    root.scale = (0.01, 0.01, 0.01)
    parts = []

    def part(name, fill):
        bm = bmesh.new()
        fill(bm)
        bmesh.ops.scale(bm, vec=(100, 100, 100), verts=bm.verts)
        me = bpy.data.meshes.new(name)
        bm.to_mesh(me)
        bm.free()
        ob = bpy.data.objects.new(name, me)
        bpy.context.scene.collection.objects.link(ob)
        ob.parent = root
        parts.append(ob)

    part("body", lambda bm: _ellipsoid(bm, (0, 0, 0.55), (0.16, 0.4, 0.17)))
    part("neck", lambda bm: _capsule(bm, (0, -0.32, 0.62), (0, -0.46, 0.8), 0.08))
    part("head", lambda bm: (_ellipsoid(bm, (0, -0.55, 0.84), (0.09, 0.14, 0.09)),
                             _ellipsoid(bm, (0, -0.68, 0.8), (0.05, 0.08, 0.05))))
    part("tail", lambda bm: _capsule(bm, (0, 0.36, 0.62), (0, 0.6, 0.74), 0.035))
    for s, side in ((1, "l"), (-1, "r")):
        x = s * 0.1
        part("leg_front_" + side, lambda bm, x=x: (
            _polyleg(bm, [(x, -0.27, 0.5), (x, -0.3, 0.32), (x, -0.28, 0.1), (x, -0.3, 0.035)], 0.035),
            _ellipsoid(bm, (x, -0.33, 0.03), (0.042, 0.065, 0.03))))
        part("leg_back_" + side, lambda bm, x=x: (
            _polyleg(bm, [(x, 0.27, 0.5), (x, 0.2, 0.32), (x, 0.32, 0.15), (x, 0.3, 0.035)], 0.035),
            _ellipsoid(bm, (x, 0.27, 0.03), (0.042, 0.065, 0.03))))
    bpy.context.view_layer.update()
    af.export_glb(path, objects=[root] + parts, animations=False, skins=False)
    for o in parts + [root]:
        bpy.data.objects.remove(o, do_unlink=True)
    return path


# ----------------------------------------------------------------------------- helpers

def _out(name):
    if KEEP:
        d = os.path.join(KEEP, name)
        os.makedirs(d, exist_ok=True)
        return d
    return tempfile.mkdtemp(prefix="af_creat_%s_" % name)


def _rig(mesh, out, inputs=None, **params):
    req = {"step": "rig_creature", "inputs": inputs or {"mesh": mesh}, "params": params, "out_dir": out}
    res = rig_creature.run(req)
    assert not os.path.isdir(os.path.join(out, "_views")), "render tiles left in out_dir"
    with open(os.path.join(out, "rig.json"), encoding="utf-8") as fh:
        meta = json.load(fh)
    return res, meta


def _anim(blend, out, **params):
    af.open_blend(blend)
    req = {"step": "anim_creature", "inputs": {"blend": blend}, "params": params, "out_dir": out}
    res = anim_creature.run(req)
    with open(os.path.join(out, "clips.json"), encoding="utf-8") as fh:
        clips = json.load(fh)
    return res, clips


def _errors(res):
    return [c for c in res["checks"] if not c["ok"] and c.get("severity", "error") == "error"]


def _bones(meta):
    return {b["name"]: b for b in meta["bones"]}


def _assert_rig(res, meta, template, legs, wings=0):
    assert not _errors(res), _errors(res)
    assert meta["template"] == template, (meta["template"], template)
    assert len(meta["legs"]) == legs, len(meta["legs"])
    assert len(meta["wings"]) == wings, len(meta["wings"])
    b = _bones(meta)
    for n in ("root", "pelvis", "head"):
        assert n in b, n
    # contract §4: the creature faces -Y -> head in front of the pelvis
    assert b["head"]["tail"][1] < b["pelvis"]["head"][1] - 0.05, (b["head"]["tail"], b["pelvis"]["head"])
    # left side is +X
    for lg in meta["legs"]:
        x = lg["contact"][0]
        assert (x > 0) == (lg["side"] == "l"), lg
    assert meta["weights"]["max_influences"] <= 4
    assert meta["weights"]["unweighted_after"] == 0
    for name in ("socket_fx_head", "socket_fx_mouth"):
        assert any(s["name"] == name for s in meta["sockets"]), name
    for k in ("rigged.glb", "rigged.blend", "rig.json", "preview.png"):
        assert os.path.isfile(os.path.join(os.path.dirname(res["outputs"]["blend"]), k)), k


def _assert_anim(res, clips, expect):
    assert not _errors(res), _errors(res)
    names = [c["name"] for c in clips["clips"]]
    assert sorted(clips["glb_animations"]) == sorted(names), clips["glb_animations"]
    for e in expect:
        assert e in names, (e, names)
    lim = clips["limits"]
    for c in clips["clips"]:
        assert c["ground_penetration_cm"] <= lim["ground_penetration_cm"], c
        if c.get("foot_slide_cm") is not None:
            assert c["foot_slide_cm"] <= lim["foot_slide_cm"], c
        if c["loop"]:
            assert c["name"].endswith("-loop")
            assert c["loop_seam_error_cm"] <= lim["loop_seam_error_cm"], c
            assert c["seam_accel_cm"] <= c["seam_accel_limit_cm"], c
    assert os.path.isfile(res["outputs"]["poses"])
    assert not os.path.isdir(os.path.join(os.path.dirname(res["outputs"]["poses"]), "_views"))


# ----------------------------------------------------------------------------- unit tests

def test_foot_offset_contacts_at_ground_speed():
    """Swing leaves and meets the ground with the stance speed (no slip at touch-down)."""
    duty, sweep = 0.65, 1.0
    eps = 1e-4
    a, _ = L.foot_offset(duty - eps, 0.0, duty, sweep, 0.1)
    b, _ = L.foot_offset(duty + eps, 0.0, duty, sweep, 0.1)
    v_st = (b.y - a.y) / (2 * eps)
    assert abs(v_st - sweep / duty) < 0.02 * sweep / duty, v_st
    p0, s0 = L.foot_offset(0.0, 0.0, duty, sweep, 0.1)
    p1, s1 = L.foot_offset(1.0 - 1e-9, 0.0, duty, sweep, 0.1)
    assert s0 is None and abs(p0.y - p1.y) < 1e-3 and abs(p1.z) < 1e-3


def test_two_bone_ik_reaches_and_bends_toward_pole():
    A = Vector((0, 0, 1.0))
    T = Vector((0, -0.3, 0.2))
    J, Te, err, ratio = L.two_bone(A, T, 0.5, 0.5, Vector((0, -1, 0)))
    assert err < 1e-6 and ratio < 1.0
    assert abs((J - A).length - 0.5) < 1e-6 and abs((T - J).length - 0.5) < 1e-6
    mid = (A + T) * 0.5
    assert (J - mid).dot(Vector((0, -1, 0))) > 0      # knee on the pole side


def test_facing_param_labels():
    """head_dir arrives from the server as a label with default 'auto'; vectors still work."""
    assert L._facing_param("auto", "head_dir") is None
    assert L._facing_param("", "head_dir") is None and L._facing_param(None, "facing") is None
    assert list(L._facing_param("+x", "head_dir")) == [1.0, 0.0]
    assert list(L._facing_param("-Y", "facing")) == [0.0, -1.0]
    d = L._facing_param([0, 2, 0], "head_dir")
    assert abs(d[1] - 1.0) < 1e-9
    try:
        L._facing_param("north", "head_dir")
    except ValueError:
        pass
    else:
        raise AssertionError("bad label accepted")


def test_synth_multiobject_dog_and_rerig():
    """glTF hierarchy (parent empty, scale 0.01, quaternion yaw) with server default params at native size;
    then the module's own rigged.glb (armature + socket empties) and rigged.blend fed back in."""
    out = _out("dog")
    mesh = build_dog_hierarchy(os.path.join(out, "dog.glb"))
    server_defaults = {"template": "auto", "length_m": None, "height_m": None, "head_dir": "auto",
                       "custom_chains": [], "tail": "auto"}
    res, meta = _rig(mesh, os.path.join(out, "rig"), **server_defaults)
    _assert_rig(res, meta, "quadruped", 4)
    assert meta["facing"]["head_in_input"] == "+X", meta["facing"]
    assert 1.0 < meta["size"]["length_m"] < 1.8, meta["size"]          # world size kept (0.01 x cm data)
    # 41 overlapping closed parts: Blender's heat solver fails on them -> named fallback, not silent
    if meta["weights"]["mode"] == "distance_fallback":
        assert meta["weights"]["heat"].get("rejected"), meta["weights"]
        assert any(w.startswith("bone heat rejected") for w in res["warnings"]), res["warnings"]
    assert any(n.startswith("tail_") for n in _bones(meta))
    assert "jaw" not in _bones(meta), "overlapping head + muzzle shells are not an open mouth"
    # own output back in: armature, skin, socket empties are stripped, the rig is rebuilt
    res2, meta2 = _rig(res["outputs"]["glb"], os.path.join(out, "rerig_glb"), length_m=1.2)
    _assert_rig(res2, meta2, "quadruped", 4)
    assert meta2["facing"]["head_in_input"] == "-Y", meta2["facing"]
    assert abs(meta2["size"]["length_m"] - 1.2) < 0.02, meta2["size"]
    # pipeline form: inputs.blend only (cleanup -> rig_creature)
    res3, meta3 = _rig(None, os.path.join(out, "rerig_blend"), inputs={"blend": res["outputs"]["blend"]},
                       head_dir="-Y")
    _assert_rig(res3, meta3, "quadruped", 4)
    ares, clips = _anim(res["outputs"]["blend"], os.path.join(out, "anim"), clips=["walk-loop", "run-loop"])
    _assert_anim(ares, clips, ["walk-loop", "run-loop"])


def test_synth_bird_folded_wings():
    """Folded wings and toes+hallux that touch the ground as separate clusters: still a 2-legged bird."""
    out = _out("bird_folded")
    mesh = build_bird_folded(os.path.join(out, "bird_folded.glb"), yaw_deg=180.0)
    res, meta = _rig(mesh, os.path.join(out, "rig"), height_m=0.5)
    _assert_rig(res, meta, "bird", 2, wings=2)
    assert meta["analysis"]["contact_merges"] >= 2, meta["analysis"]
    assert all(w["folded"] for w in meta["wings"]), meta["wings"]
    assert any("folded" in w for w in res["warnings"]), res["warnings"]
    b = _bones(meta)
    for side in ("l", "r"):                                  # folded wings lie along the flank, tip behind
        w1, w3 = b["wing_%s_1" % side], b["wing_%s_3" % side]
        assert w3["tail"][1] > w1["head"][1] + 0.1, (w1, w3)
        assert (w1["head"][0] > 0) == (side == "l")
    for lg in meta["legs"]:                                   # the toe is in front of the paw joint
        assert lg["contact"][1] < lg["paw_head"][1] + 0.01, lg
    ares, clips = _anim(res["outputs"]["blend"], os.path.join(out, "anim"),
                        clips=["idle-loop", "walk-loop", "run-loop", "fly-loop", "hit", "death"])
    _assert_anim(ares, clips, ["walk-loop", "run-loop", "death"])
    assert "fly-loop" not in clips["glb_animations"]
    assert any(w.startswith("clip fly-loop skipped") for w in ares["warnings"]), ares["warnings"]


def test_synth_hexapod_insect():
    out = _out("insect")
    mesh = build_insect(os.path.join(out, "insect.glb"), yaw_deg=90.0)
    res, meta = _rig(mesh, os.path.join(out, "rig"), length_m=1.0)
    _assert_rig(res, meta, "hexapod", 6)
    assert meta["facing"]["head_in_input"] == "+X", meta["facing"]
    b = _bones(meta)
    for pos in ("front", "mid", "back"):
        for s in ("l", "r"):
            for k in (1, 2, 3):
                assert "leg_%s_%s_%d" % (pos, s, k) in b
            assert "paw_%s_%s" % (pos, s) in b
            knee = b["leg_%s_%s_1" % (pos, s)]["tail"]
            root = b["leg_%s_%s_1" % (pos, s)]["head"]
            assert knee[2] > root[2], "insect knees must rise above the body attachment"
    ares, clips = _anim(res["outputs"]["blend"], os.path.join(out, "anim"),
                        clips=["idle-loop", "walk-loop", "run-loop", "attack", "hit", "death"])
    _assert_anim(ares, clips, ["walk-loop", "death"])
    walk = [c for c in clips["clips"] if c["name"] == "walk-loop"][0]
    assert walk["gait"] == "walk" and walk["duty"] == 0.55
    # root motion: the root travels one stride per cycle, stance feet stay world-locked
    ares2, clips2 = _anim(res["outputs"]["blend"], os.path.join(out, "anim_rm"), clips=["walk-loop"],
                          in_place=False, speed=0.3)
    _assert_anim(ares2, clips2, ["walk-loop"])
    w2 = clips2["clips"][0]
    assert w2["in_place"] is False and abs(w2["speed_mps"] - 0.3) < 0.03, w2


def test_synth_octopod_spider():
    out = _out("spider")
    mesh = build_spider(os.path.join(out, "spider.glb"), yaw_deg=0.0)
    res, meta = _rig(mesh, os.path.join(out, "rig"), length_m=1.4)
    _assert_rig(res, meta, "octopod", 8)
    names = {lg["id"] for lg in meta["legs"]}
    assert names == {"%s_%s" % (p, s) for p in ("front", "mid", "mid2", "back") for s in ("l", "r")}, names
    assert any(n.startswith("tail_") for n in _bones(meta)), "abdomen bones expected"
    ares, clips = _anim(res["outputs"]["blend"], os.path.join(out, "anim"),
                        clips=["idle-loop", "walk-loop", "run-loop", "attack", "death"])
    _assert_anim(ares, clips, ["walk-loop"])


def test_synth_serpent_with_jaw():
    out = _out("serpent")
    mesh = build_serpent(os.path.join(out, "serpent.glb"), yaw_deg=180.0)
    res, meta = _rig(mesh, os.path.join(out, "rig"), length_m=2.0)
    _assert_rig(res, meta, "serpent", 0)
    b = _bones(meta)
    assert "jaw" in b, "open mouth should give a jaw bone"
    assert sum(1 for n in b if n.startswith("spine_")) >= 4
    assert sum(1 for n in b if n.startswith("tail_")) >= 4
    assert not any(s["name"] == "socket_mount_back" for s in meta["sockets"])
    ares, clips = _anim(res["outputs"]["blend"], os.path.join(out, "anim"),
                        clips=["idle-loop", "walk-loop", "attack", "death"])
    _assert_anim(ares, clips, ["walk-loop"])
    walk = [c for c in clips["clips"] if c["name"] == "walk-loop"][0]
    assert walk["gait"] == "lateral_undulation"
    # root motion: the root travels one wavelength per cycle (seam measured after removing it)
    ares2, clips2 = _anim(res["outputs"]["blend"], os.path.join(out, "anim_rm"), clips=["walk-loop"],
                          in_place=False)
    _assert_anim(ares2, clips2, ["walk-loop"])
    w2 = clips2["clips"][0]
    assert w2["in_place"] is False and w2["speed_mps"] > 0 and w2["stride_m"] == w2["wavelength_m"], w2
    rig = L.Rig(bpy.data.objects["creature_rig"], bpy.data.objects["creature_mesh"])
    act = bpy.data.actions["walk-loop"]
    L.assign_action(rig.arm, act)
    bpy.context.scene.frame_set(w2["keys"] - 1)
    travelled = -rig.arm.pose.bones["root"].matrix.translation.y
    L.assign_action(rig.arm, None)
    assert abs(travelled - w2["stride_m"]) < 0.01 * w2["stride_m"] + 1e-4, (travelled, w2["stride_m"])


def test_synth_bird():
    out = _out("bird")
    mesh = build_bird(os.path.join(out, "bird.glb"), yaw_deg=-90.0)
    res, meta = _rig(mesh, os.path.join(out, "rig"), height_m=0.75)
    _assert_rig(res, meta, "bird", 2, wings=2)
    b = _bones(meta)
    for n in ("wing_l_1", "wing_l_2", "wing_l_3", "wing_r_3", "leg_back_l_3", "paw_back_r", "neck_03"):
        assert n in b, n
    ares, clips = _anim(res["outputs"]["blend"], os.path.join(out, "anim"),
                        clips=["idle-loop", "walk-loop", "fly-loop", "glide-loop", "hit", "death"])
    _assert_anim(ares, clips, ["fly-loop", "glide-loop"])
    # the seam check is a real measurement: a deliberately non-periodic loop must be caught
    rig = L.Rig(bpy.data.objects["creature_rig"], bpy.data.objects["creature_mesh"])
    cb = L.ClipBuilder(rig, {})

    def bad(t, crouch):
        s = cb.spec()
        cb.rot(s, "head", "X", 25.0 * t)                 # drifts instead of returning to the start
        cb.planted(s)
        return s
    clip = L.Clip("bad-loop", True, 24, bad)
    act, _ = L.bake_clip(rig, clip)
    L.assign_action(rig.arm, None)
    V0 = L.eval_verts(rig.ob)
    _, _, E = L.mesh_np(rig.ob)
    q = L.qa_clip(rig, act, clip, V0, E, L.contact_sets(rig, V0))
    assert q["loop_seam_error_cm"] > 1.0, q
    assert q["seam_accel_cm"] > 1.5 * q["interior_accel_max_cm"], q


def test_synth_custom_chain():
    """template=custom: user chains in the output frame, weights from the chain fallback."""
    out = _out("custom")
    bm = bmesh.new()
    _ellipsoid(bm, (0, 0, 0.3), (0.3, 0.3, 0.3))
    _polyleg(bm, [(0, -0.25, 0.3), (0, -0.6, 0.35), (0, -0.9, 0.45)], 0.05)
    mesh = _finish(bm, "blob", os.path.join(out, "blob.glb"), 0.0)
    res, meta = _rig(mesh, os.path.join(out, "rig"), template="custom", facing="-Y", weights="distance",
                     custom_chains=[{"name": "pelvis", "parent": "root", "points": [[0, 0.1, 0.3], [0, -0.1, 0.3]],
                                     "bones": ["pelvis"]},
                                    {"name": "head", "parent": "pelvis", "points": [[0, -0.1, 0.3], [0, -0.3, 0.3]],
                                     "bones": ["head"]},
                                    {"name": "tentacle", "parent": "head",
                                     "points": [[0, -0.3, 0.3], [0, -0.6, 0.35], [0, -0.9, 0.45]]}])
    assert not _errors(res), _errors(res)
    b = _bones(meta)
    assert "tentacle_01" in b and "tentacle_02" in b
    assert meta["weights"]["mode"] == "distance_fallback"


# ----------------------------------------------------------------------------- real assets


