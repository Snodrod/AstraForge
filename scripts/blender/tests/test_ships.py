"""Self-tests of the "ships" module: ship_gen, turret_gen, ship_from_mesh (+ _ship_lib helpers).

Zero-argument test_* functions that raise via assert; temp files only under tempfile.mkdtemp() (removed
again when a test passes, kept for inspection when it fails). Tests needing assets outside the repo are
named test_real_* and return "skip: ..." when the file is missing.
Run: blender -b --factory-startup --python scripts/blender/tests/run_tests.py -- ships
"""
import functools
import glob
import json
import math
import os
import shutil
import tempfile

import bpy
import numpy as np
from mathutils import Matrix

import af_common as af
from steps import _ship_lib as sl
from steps import ship_from_mesh, ship_gen, turret_gen

_TMP = []


def _mkdtemp(prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    _TMP.append(d)
    return d


def _clean(fn):
    """Remove the temp folders a test created once it passes."""
    @functools.wraps(fn)
    def wrapper():
        start = len(_TMP)
        out = fn()
        for d in _TMP[start:]:
            shutil.rmtree(d, ignore_errors=True)
        del _TMP[start:]
        return out
    return wrapper


def _run(module, step, params, inputs=None):
    out = _mkdtemp("af_ships_test_")
    req = {"step": step, "inputs": inputs or {}, "params": params, "out_dir": out}
    res = af.finish(req, module.run(req))
    return res, out


def _gen(role, seed, faction="military", **extra):
    p = {"class": role, "seed": seed, "faction": faction, "preview": False}
    p.update(extra)
    return _run(ship_gen, "ship_gen", p)


def _failed(res):
    return [c["name"] for c in res["checks"] if not c["ok"] and c.get("severity", "error") == "error"]


def _assert_ship(res, cls):
    st = res["stats"]
    assert res["ok"], "checks failed: %s" % _failed(res)
    b = sl.BUDGETS[cls]
    assert st["budget_class"] == cls
    assert 0 < st["tris"] <= b["tris"], st["tris"]
    assert st["tris_lod1"] <= min(b["lod1"], 0.35 * st["tris"]), (st["tris_lod1"], st["tris"])
    assert st["tris_lod2"] <= min(b["lod2"], 0.12 * st["tris"]), (st["tris_lod2"], st["tris"])
    assert st["surfaces"] <= b["surfaces"] and st["nodes"] <= b["nodes"] and st["glb_mb"] <= b["mb"]
    assert all(m.startswith("AF_") for m in st["materials"]), st["materials"]
    for name in st["sockets"]:
        assert sl.SOCKET_RE.match(name), name
        assert "_fore_" not in name + "_", name  # place vocabulary: forward / aft
    kinds = {n.split("_")[1] for n in st["sockets"]}
    assert "thruster" in kinds and "turret" in kinds and "fx" in kinds, kinds
    assert st["collision"]["vertices"] <= 64
    for k in ("glb", "lod1", "lod2", "blend", "manifest", "definition"):
        assert os.path.isfile(res["outputs"][k]), k
    man = json.load(open(res["outputs"]["manifest"], encoding="utf-8"))
    assert man["axes"]["godot_forward"] == "-Z" and man["class"] == cls
    sil = st["lod_silhouette_iou"]["lod2"]
    assert min(sil["top"], sil["side"]) >= 0.9, sil
    return st


def _nav(summ):
    s = {x["game_socket"]: x for x in summ["sockets"]}
    port, stbd = s["socket_fx_nav_port"], s["socket_fx_nav_starboard"]
    assert port["position_gltf"][0] < 0 and port["y_gltf"][0] < -0.99, port
    assert stbd["position_gltf"][0] > 0 and stbd["y_gltf"][0] > 0.99, stbd
    assert port["extras"]["side"] == "port" and stbd["extras"]["side"] == "starboard"


# ----------------------------------------------------------------------------- ship_gen
@_clean
def test_ship_gen_ship_s_two_seeds():
    r1, _ = _gen("fighter", 1, "military")
    r2, _ = _gen("interceptor", 2, "sleek")
    s1, s2 = _assert_ship(r1, "ship_s"), _assert_ship(r2, "ship_s")
    assert s1["geometry_hash"] != s2["geometry_hash"]
    assert abs(s1["dimensions_m"]["length"] - 14.0) < 0.42


@_clean
def test_ship_gen_ship_m_two_seeds():
    r1, _ = _gen("corvette", 1, "military")
    r2, _ = _gen("freighter", 2, "industrial")
    s1, s2 = _assert_ship(r1, "ship_m"), _assert_ship(r2, "ship_m")
    assert s1["turrets_placed"] == 4 and s2["turrets_placed"] == 2
    assert any(n.startswith("socket_cargo_") for n in s2["sockets"])


@_clean
def test_ship_gen_ship_l_two_seeds():
    r1, _ = _gen("frigate", 1, "military")
    r2, _ = _gen("carrier", 2, "industrial")
    s1, s2 = _assert_ship(r1, "ship_l"), _assert_ship(r2, "ship_l")
    assert s1["turrets_placed"] == 8
    assert any(n.startswith("socket_docking_hangar") for n in s2["sockets"])


@_clean
def test_ship_gen_seed_variety_silhouette():
    """Seeds 1..3 of one role + faction must differ in silhouette: top AND side mask IoU < 0.85 for every
    pair (the seed walks through the role's three layout families)."""
    worst = []
    for role in sl.ROLES:
        meshes, fams = [], []
        for seed in (1, 2, 3):
            res, _ = _gen(role, seed, "military", lods=False)
            assert res["ok"], (role, seed, _failed(res))
            meshes.append(sl.glb_triangles(res["outputs"]["glb"]))
            fams.append(res["stats"]["layout"]["family"])
        assert len(set(fams)) == 3, (role, fams)
        for i in range(3):
            for j in range(i + 1, 3):
                top, _ = sl.silhouette_iou([meshes[i], meshes[j]], "top", 200)
                side, _ = sl.silhouette_iou([meshes[i], meshes[j]], "side", 200)
                worst.append((max(top, side), role, i + 1, j + 1))
                assert top < 0.85 and side < 0.85, (role, i + 1, j + 1, round(top, 3), round(side, 3))
    print("seed variety: worst pair IoU %.3f %s" % max(worst)[:2])


@_clean
def test_ship_gen_deterministic():
    a, _ = _gen("miner", 7, "industrial", lods=False)
    b, _ = _gen("miner", 7, "industrial", lods=False)
    assert a["stats"]["geometry_hash"] == b["stats"]["geometry_hash"]
    assert a["stats"]["layout"] == b["stats"]["layout"]


@_clean
def test_ship_gen_axes_and_socket_frames():
    res, out = _gen("corvette", 5, "military", lods=False, layout="destroyer")
    assert res["stats"]["layout"]["family"] == "destroyer"
    summ = sl.glb_summary(res["outputs"]["glb"])
    socks = {s["game_socket"]: s for s in summ["sockets"]}
    # glTF: nose -Z, so every thruster sits aft (+Z) and (default frame) points its local -Z at the nose
    thr = [s for s in summ["sockets"] if s["extras"].get("kind") == "thruster"]
    assert thr and all(s["position_gltf"][2] > 0 for s in thr)
    assert all(s["neg_z_gltf"][2] < -0.99 and s["extras"]["frame"] == "thrust" for s in thr)
    # contract 6: nozzle exit diameter on every thruster, consistent with its size class
    for s in thr:
        d = s["extras"]["mount_d_m"]
        assert 0.2 < d < 0.4 * summ["size_gltf"][0], (s["game_socket"], d)
        assert sl.size_class_for(d) == s["extras"]["size_class"] or abs(d - 1.5) < 0.15 or abs(d - 3.0) < 0.3, s
    print("ship_gen corvette thrusters mount_d_m:", {s["game_socket"]: s["extras"]["mount_d_m"] for s in thr})
    # turret frames: local +Y (glTF) = outward normal, local -Z = ship forward
    dor = socks["socket_turret_dorsal_forward"]
    assert dor["y_gltf"][1] > 0.99 and dor["neg_z_gltf"][2] < -0.99, dor
    ven = socks["socket_turret_ventral_forward"]
    assert ven["y_gltf"][1] < -0.99 and ven["neg_z_gltf"][2] < -0.99, ven
    assert dor["extras"]["size_class"] in ("S", "M", "L")
    _nav(summ)
    # root at the hull centre, mirror X = 0
    assert abs(summ["center_offset_gltf"][0]) < 0.01
    assert abs(summ["center_offset_gltf"][2]) < 0.05 * 70


@_clean
def test_ship_gen_nav_lights_every_role():
    for role in sl.ROLES:
        for seed in (1, 2, 3):
            res, _ = _gen(role, seed, "industrial", lods=False)
            assert res["ok"], (role, seed, _failed(res))
            _nav(sl.glb_summary(res["outputs"]["glb"]))


@_clean
def test_ship_gen_thruster_frames():
    for fr in ("thrust", "exhaust"):
        res, _ = _gen("fighter", 4, "sleek", lods=False, thruster_frame=fr)
        assert res["ok"], _failed(res)
        summ = sl.glb_summary(res["outputs"]["glb"])
        thr = [s for s in summ["sockets"] if s["extras"].get("kind") == "thruster"]
        assert thr and all(sl.thruster_frame_ok(s, fr) and s["extras"]["frame"] == fr for s in thr), fr
        man = json.load(open(res["outputs"]["manifest"], encoding="utf-8"))
        rows = [r for r in man["sockets"] if r["kind"] == "thruster"]
        assert rows and all(r["axis"].startswith("frame=%s" % fr) for r in rows)
    # the frames really differ: exhaust is the thrust frame turned 180 deg about up
    a, _ = _gen("fighter", 4, "sleek", lods=False, thruster_frame="thrust")
    b, _ = _gen("fighter", 4, "sleek", lods=False, thruster_frame="exhaust")
    ta = [s for s in sl.glb_summary(a["outputs"]["glb"])["sockets"] if s["extras"].get("kind") == "thruster"]
    tb = [s for s in sl.glb_summary(b["outputs"]["glb"])["sockets"] if s["extras"].get("kind") == "thruster"]
    assert ta[0]["neg_z_gltf"][2] < -0.99 < 0.99 < tb[0]["neg_z_gltf"][2]


@_clean
def test_ship_gen_param_warnings():
    res, _ = _gen("fighter", 2, lods=False, engine_count=9, length_m=40, layout="bogus", greeble_density=3)
    w = " | ".join(res["warnings"])
    assert "engine_count 9 clamped to 6" in w, w
    assert "budget_class ship_m" in w and res["stats"]["budget_class"] == "ship_m", w
    assert "layout 'bogus' unknown" in w and "greeble_density" in w, w
    res, _ = _gen("frigate", 1, lods=False, wing_style="delta", turret_count=30)
    w = " | ".join(res["warnings"])
    assert "wing_style applies to" in w and "turret_count 30 requested" in w, w


@_clean
def test_ship_gen_livery_and_counts():
    res, _ = _gen("gunship", 9, "sleek", engine_count=4, turret_count=6,
                  livery={"armor": "#d03020", "drive": [0.1, 1.0, 0.2]})
    assert res["ok"], _failed(res)
    st = res["stats"]
    assert st["turrets_placed"] == 6 and st["engines"] >= 4
    blend_mat = bpy.data.materials.get("AF_Armor")
    rgb = blend_mat.node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value[:3]
    assert abs(rgb[0] - sl.srgb_to_linear(0xd0 / 255.0)) < 1e-3


# ----------------------------------------------------------------------------- turret_gen
@_clean
def test_turret_gen_s_and_l():
    for size, barrels, fac in (("S", 1, "sleek"), ("L", 4, "military")):
        res, out = _run(turret_gen, "turret_gen", {"size": size, "barrels": barrels, "faction": fac, "seed": 2,
                                                   "preview": False})
        assert res["ok"], _failed(res)
        st = res["stats"]
        b = sl.BUDGETS["turret"]
        assert st["tris"] <= b["tris"] and st["nodes"] <= b["nodes"] and st["surfaces"] <= b["surfaces"]
        names = st["node_names"]
        for n in ["socket_mount", "pivot_yaw", "pivot_pitch"] + ["socket_muzzle_%02d" % (i + 1) for i in range(barrels)]:
            assert n in names, n
        assert sorted(c["name"] for c in st["clips"]) == ["fire_recoil", "pitch_cycle-loop", "yaw_sweep-loop"]
        summ = sl.glb_summary(res["outputs"]["glb"])
        assert len(summ["animations"]) == 3
        assert st["ring_d_m"] <= {"S": 1.5, "M": 3.0, "L": 6.0}[size]
        man = json.load(open(res["outputs"]["manifest"], encoding="utf-8"))
        pitch = next(s for s in man["sockets"] if s["name"] == "pivot_pitch")
        assert pitch["axis"] == "local X" and pitch["range_degrees"][1] > 0
        # contract 6: pivots carry axis + range_degrees but no game_socket; only socket_mount
        # and socket_muzzle_* are sockets
        doc, _, _ = sl.read_glb(res["outputs"]["glb"])
        piv = {n["name"]: n.get("extras") or {} for n in doc["nodes"] if n.get("name", "").startswith("pivot_")}
        assert set(piv) == {"pivot_yaw", "pivot_pitch"}, piv
        assert all("game_socket" not in ex and ex.get("axis") and len(ex["range_degrees"]) == 2 for ex in piv.values()), piv
        assert sorted(x["game_socket"] for x in summ["sockets"]) ==             ["socket_mount"] + ["socket_muzzle_%02d" % (i + 1) for i in range(barrels)], summ["sockets"]
        for k in ("pivots_not_sockets", "turret_sockets_only_mount_muzzles"):
            assert next(c for c in res["checks"] if c["name"] == k)["ok"], k
        mz = [s for s in man["sockets"] if s["kind"] == "muzzle"]
        assert all(s["position_m"][1] > pitch["position_m"][1] for s in mz)


@_clean
def test_turret_gen_clamp_warnings():
    res, _ = _run(turret_gen, "turret_gen", {"size": "M", "barrels": 9, "ring_m": 50, "preview": False,
                                             "lods": False, "clips": False})
    w = " | ".join(res["warnings"])
    assert "barrels 9 clamped to 4" in w and "ring_m 50.00 clamped to 3.00" in w, w
    assert res["stats"]["barrels"] == 4 and res["stats"]["ring_d_m"] == 3.0


# ----------------------------------------------------------------------------- ship_from_mesh
def _synthetic_generated_mesh(out):
    """A ship_gen corvette fused into one mesh, nose turned to -X and scaled to 3 % (unit-less like AI output)."""
    req = {"step": "ship_gen", "inputs": {}, "out_dir": out,
           "params": {"class": "corvette", "seed": 11, "faction": "industrial", "lods": False, "preview": False}}
    ship_gen.run(req)
    af.reset_scene()
    objs = af.import_any(os.path.join(out, "ship.glb"))
    meshes = [o for o in objs if o.type == "MESH" and not o.name.startswith("COL_")]
    bpy.context.view_layer.update()
    wm = {o: o.matrix_world.copy() for o in meshes}
    for o in meshes:
        o.parent = None
        o.matrix_world = wm[o]
    for o in objs:
        if o not in meshes:
            bpy.data.objects.remove(o, do_unlink=True)
    bpy.context.view_layer.update()
    ob = af.join(meshes, "fused")
    ob.matrix_world = Matrix.Rotation(math.radians(90), 4, "Z") @ Matrix.Scale(0.03, 4)
    af.apply_transforms([ob])
    path = os.path.join(out, "generated_like.glb")
    af.export_glb(path, [ob], animations=False, skins=False)
    return path


def _two_engine_primitive(out, name="two_engine.obj"):
    """Mirror-symmetric primitive: box hull + cone nose at -X + two engine cylinders with n-gon caps sticking
    out aft at +X; separate objects, no materials, no UVs, object scale 0.01 (OBJ keeps the n-gons)."""
    af.reset_scene()
    objs = []
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0))
    hull = bpy.context.active_object
    hull.scale = (10.0, 2.0, 1.2)
    objs.append(hull)
    bpy.ops.mesh.primitive_cone_add(vertices=16, radius1=0.9, depth=2.0, location=(-6.0, 0, 0),
                                    rotation=(0, -math.pi / 2, 0))
    objs.append(bpy.context.active_object)
    for y in (-0.6, 0.6):
        bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=0.38, depth=1.4, end_fill_type="NGON",
                                            location=(5.0 - 0.7 + 0.6, y, 0.0), rotation=(0, math.pi / 2, 0))
        objs.append(bpy.context.active_object)
    for o in objs:
        while o.data.uv_layers:
            o.data.uv_layers.remove(o.data.uv_layers[0])
        o.data.materials.clear()
        o.matrix_world = Matrix.Scale(0.01, 4) @ o.matrix_world
    bpy.context.view_layer.update()
    path = os.path.join(out, name)
    af.select_only(objs)
    bpy.ops.wm.obj_export(filepath=path, export_selected_objects=True, export_materials=False, export_uv=False,
                          export_normals=True, apply_modifiers=True)
    return path


@_clean
def test_ship_from_mesh_synthetic_orientation():
    tmp = _mkdtemp("af_ships_src_")
    src = _synthetic_generated_mesh(tmp)
    res, out = _run(ship_from_mesh, "ship_from_mesh", {"class": "corvette", "preview": False}, {"mesh": src})
    assert res["ok"], _failed(res)
    st = res["stats"]
    assert st["orientation"]["nose_confidence"] >= 0.5, st["orientation"]
    assert abs(st["dimensions_m"]["length"] - 70.0) < 2.1
    summ = sl.glb_summary(res["outputs"]["glb"])
    thr = [s for s in summ["sockets"] if s["extras"].get("kind") == "thruster"]
    assert thr and all(s["position_gltf"][2] > 0 for s in thr), thr
    # every nozzle of the source ship is found again
    src_thr = [s for s in sl.glb_summary(os.path.join(tmp, "ship.glb"))["sockets"]
               if s["extras"].get("kind") == "thruster"]
    assert len(thr) == len(src_thr), (len(thr), len(src_thr), [t["position_gltf"] for t in thr])
    for s in src_thr:  # same place within 2 % of the length (source length 70 m -> 70 m)
        assert min(sum((a - b) ** 2 for a, b in zip(s["position_gltf"], t["position_gltf"])) ** 0.5
                   for t in thr) < 0.02 * 70, s
    # mount_d_m (nozzle exit diameter) recovered from the fused mesh vs the generator's value
    for s in src_thr:
        t = min(thr, key=lambda t: sum((a - b) ** 2 for a, b in zip(s["position_gltf"], t["position_gltf"])))
        ds, dt = s["extras"]["mount_d_m"], t["extras"]["mount_d_m"]
        assert 0.5 * ds < dt < 1.3 * ds, (s["game_socket"], ds, t["game_socket"], dt)
    tur = [s for s in summ["sockets"] if s["extras"].get("kind") == "turret"]
    assert len(tur) >= 2 and all(0 <= s["extras"]["confidence"] <= 1 for s in tur)
    assert st["material_mode"] == "classify" and len(st["face_labels"]) >= 3, st["face_labels"]
    assert st["tris"] <= sl.BUDGETS["ship_m"]["tris"]
    _nav(summ)


@_clean
def test_ship_from_mesh_explicit_nose():
    tmp = _mkdtemp("af_ships_src_")
    src = _synthetic_generated_mesh(tmp)
    res, out = _run(ship_from_mesh, "ship_from_mesh", {"class": "ship_m", "nose": "-x", "length_m": 64,
                                                        "preview": False, "lods": False},
                    {"mesh": src})
    assert res["ok"], _failed(res)
    assert res["stats"]["orientation"]["nose_from"] == "params"
    summ = sl.glb_summary(res["outputs"]["glb"])
    thr = [s for s in summ["sockets"] if s["extras"].get("kind") == "thruster"]
    assert thr and all(s["position_gltf"][2] > 0 for s in thr)


@_clean
def test_ship_from_mesh_two_engine_primitive():
    """Review case: symmetric two-engine primitive (multi-object, no materials/UVs, scale 0.01, nose -X,
    n-gon caps) must give exactly two mirrored thrusters and turret sockets on the flat back."""
    tmp = _mkdtemp("af_ships_src_")
    src = _two_engine_primitive(tmp)
    res, out = _run(ship_from_mesh, "ship_from_mesh", {"class": "fighter", "preview": False},
                    {"mesh": src})
    assert res["ok"], _failed(res)
    st = res["stats"]
    assert st["x_symmetry"] > 0.9, st["x_symmetry"]
    summ = sl.glb_summary(res["outputs"]["glb"])
    thr = [s for s in summ["sockets"] if s["extras"].get("kind") == "thruster"]
    assert len(thr) == 2, [(t["game_socket"], t["position_gltf"]) for t in thr]
    (x1, y1, z1), (x2, y2, z2) = thr[0]["position_gltf"], thr[1]["position_gltf"]
    L = st["dimensions_m"]["length"]
    assert x1 * x2 < 0 and abs(x1 + x2) < 0.01 * L and abs(y1 - y2) < 0.01 * L, (x1, x2, y1, y2)
    assert z1 > 0.4 * L and z2 > 0.4 * L  # at the aft end (glTF +Z)
    # engines sit at y = +-0.6 of a 12.6-unit ship -> +-0.6 / 12.6 * L
    assert abs(abs(x1) - 0.6 / 12.6 * L) < 0.02 * L, (x1, L)
    assert {t["game_socket"] for t in thr} == {"socket_thruster_main_port", "socket_thruster_main_starboard"}
    assert all(t["extras"]["confidence"] >= 0.6 for t in thr)
    # nozzle exit diameter: cylinders of radius 0.38 on a 12.6-unit ship
    want = 0.76 / 12.6 * L
    for t in thr:
        assert abs(t["extras"]["mount_d_m"] - want) < 0.25 * want, (t["game_socket"], t["extras"]["mount_d_m"], want)
    tur = [s for s in summ["sockets"] if s["extras"].get("kind") == "turret"]
    assert len(tur) >= 2, tur
    assert all(s["position_gltf"][1] > 0 and abs(s["position_gltf"][0]) < 0.05 * L for s in tur)


@_clean
def test_ship_from_mesh_provenance_and_id():
    tmp = _mkdtemp("af_ships_src_")
    a_dir, b_dir = os.path.join(tmp, "meshy_job_a"), os.path.join(tmp, "plain_b")
    os.makedirs(a_dir)
    os.makedirs(b_dir)
    a = _two_engine_primitive(a_dir, "model.obj")
    b = os.path.join(b_dir, "model.obj")
    with open(a, "rb") as fh:
        data = fh.read()
    with open(b, "wb") as fh:
        fh.write(data + b"\n# variant\n")
    pa, wa = ship_from_mesh.resolve({"inputs": {"mesh": a}, "params": {"class": "fighter"}})
    pb, wb = ship_from_mesh.resolve({"inputs": {"mesh": b}, "params": {"class": "fighter"}})
    assert (pa["source"], pa["generator"]) == ("external", "meshy"), pa
    assert (pb["source"], pb["generator"]) == ("external", "unknown"), pb
    assert pa["asset_id"] != pb["asset_id"] and pa["asset_id"].startswith("ship_s_model_"), (pa["asset_id"],
                                                                                             pb["asset_id"])
    pc, wc = ship_from_mesh.resolve({"inputs": {"mesh": b}, "params": {"class": "fighter", "source": "meshy"}})
    assert pc["source"] == "external" and pc["generator"] == "meshy" and wc
    ph, _ = ship_from_mesh.resolve({"inputs": {"mesh": b}, "params": {"class": "fighter", "source": "hunyuan3d",
                                                                      "generator": "hunyuan3d-2.1"}})
    assert ph["source"] == "hunyuan3d" and ph["generator"] == "hunyuan3d-2.1"


@_clean
def test_voxel_proxy_lod_budget_materials_uv():
    """voxel_proxy_lod: closed remesh of a two-material box with an inner sheet glued along an edge (3 faces on
    one edge - what stops Blender's collapse), decimated to the budget, materials from the nearest source face,
    UVs transferred, silhouette kept."""
    import bmesh
    af.reset_scene()
    bm = bmesh.new()
    bmesh.ops.create_cube(bm, size=2.0)
    bmesh.ops.subdivide_edges(bm, edges=bm.edges[:], cuts=12, use_grid_fill=True)
    bm.verts.ensure_lookup_table()
    for f in bm.faces:
        f.material_index = 0 if f.calc_center_median().y < 0 else 1
    # an inner fin sharing one hull edge (non-manifold) plus a stretched wing
    e = next(e for e in bm.edges if all(abs(v.co.z - 1.0) < 1e-6 and abs(v.co.x - 1.0) < 1e-6 for v in e.verts))
    a, b = e.verts
    c = bm.verts.new((0.2, a.co.y, 0.2))
    d = bm.verts.new((0.2, b.co.y, 0.2))
    bm.faces.new((a, b, d, c))
    me = bpy.data.meshes.new("src")
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new("src", me)
    bpy.context.scene.collection.objects.link(ob)
    for nm in ("A", "B"):
        me.materials.append(bpy.data.materials.new(nm))
    me.uv_layers.new(name="UVMap")
    ob.scale = (3.0, 7.0, 1.0)
    bpy.context.view_layer.update()
    bpy.context.view_layer.objects.active = ob
    ob.select_set(True)
    bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
    n0 = sl.mesh_tris([ob])
    log = []
    prox, info = sl.voxel_proxy_lod(ob, 300, log=log)
    assert prox is not None and info["tris"] <= 300 < n0, (info, n0)
    assert min(info["silhouette_vs_src"].values()) >= 0.85, info
    mi = np.zeros(len(prox.data.polygons), dtype=np.int32)
    prox.data.polygons.foreach_get("material_index", mi)
    front = np.array([p.center.y < 0 for p in prox.data.polygons])
    assert (mi[front] == 0).mean() > 0.9 and (mi[~front] == 1).mean() > 0.9, ((mi[front] == 0).mean(), (mi[~front] == 1).mean())
    assert prox.data.uv_layers and prox.data.uv_layers[0].name == "UVMap"
    assert log and log[0]["stage"] == "voxel_proxy"


def _summ(tris, surfaces=1, worst=None, worst_surf=None, nodes=4, materials=1, mb=0.1):
    return {"triangles": tris, "surfaces_lod0": surfaces, "worst_mesh_triangles": tris if worst is None else worst,
            "worst_mesh_surfaces": surfaces if worst_surf is None else worst_surf, "materials_used": materials,
            "mb": mb, "nodes": nodes, "bad_names": [], "finite": True, "prims_without_uv": 0,
            "prims_without_normal": 0, "prims_without_material": 0, "sockets": []}


@_clean
def test_glb_summary_worst_mesh():
    """glb_summary counts the heaviest render mesh: collision skipped."""
    d = _mkdtemp("af_ships_worst_")
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=(0, 0, 0))
    cube = bpy.context.active_object
    cube.name = "rock_a"
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=3, radius=1.0, location=(3, 0, 0))
    ico = bpy.context.active_object
    ico.name = "rock_b"
    bpy.ops.mesh.primitive_ico_sphere_add(subdivisions=4, radius=1.0, location=(6, 0, 0))
    col = bpy.context.active_object
    col.name = "COL_SIMPLE-convcolonly"
    path = os.path.join(d, "bank.glb")
    af.export_glb(path, objects=[cube, ico, col], animations=False, skins=False)
    summ = sl.glb_summary(path)
    assert summ["worst_mesh_triangles"] == 320 and summ["triangles"] == 12 + 320, summ["triangles"]
    assert summ["worst_mesh_surfaces"] == 1 and summ["surfaces_lod0"] == 2, summ["surfaces_lod0"]


# ----------------------------------------------------------------------------- helpers
@_clean
def test_png_writer_roundtrip():
    tmp = _mkdtemp("af_ships_png_")
    rng = np.random.default_rng(0)
    img = (rng.random((33, 47, 3)) * 255).astype(np.uint8)
    img[5:20, 3:30] = 128
    path = os.path.join(tmp, "t.png")
    sl.write_png(path, img)
    back = sl.load_png_rgba(path)
    assert np.abs((back[..., :3] * 255 + 0.5).astype(int) - img).max() == 0


def test_manifest_validator_rejects_bad_manifest():
    schema = {"type": "object", "required": ["schema_version", "asset_id", "type"],
              "properties": {"schema_version": {"const": 1},
                             "asset_id": {"type": "string", "pattern": "^[a-z][a-z0-9_]*$"}}}
    errs = sl.validate_schema({"schema_version": 1, "asset_id": "Bad Id"}, schema)
    assert any("missing" in e for e in errs) and any("does not match" in e for e in errs)


def test_convex_collision_limit():
    bpy.ops.mesh.primitive_uv_sphere_add(segments=64, ring_count=32, radius=2.0)
    ob = bpy.context.active_object
    col, info = sl.convex_collision([ob])
    assert info["vertices"] <= 64 and info["coverage"] > 0.85, info


def test_silhouette_iou_sanity():
    V = np.array([[0, 0, 0], [1, 0, 0], [1, 0, 1], [0, 0, 1]], dtype=float)
    T = np.array([[0, 1, 2], [0, 2, 3]])
    same, _ = sl.silhouette_iou([(V, T), (V.copy(), T)], "top", 64)
    half = V.copy()
    half[:, 0] *= 0.5
    small, _ = sl.silhouette_iou([(V, T), (half, T)], "top", 64)
    assert same > 0.99 and 0.4 < small < 0.6, (same, small)


@_clean
def test_preview_leaves_no_temp_folders():
    before = set(glob.glob(os.path.join(tempfile.gettempdir(), "af_ship_prev_*")))
    res, out = _gen("fighter", 1, lods=False, preview=True, preview_size=120)
    assert os.path.isfile(res["outputs"]["preview"])
    after = set(glob.glob(os.path.join(tempfile.gettempdir(), "af_ship_prev_*")))
    assert not (after - before), sorted(after - before)


# ----------------------------------------------------------------------------- ship_from_mesh: palette, hull, paint
def _mat(name, rgb, emit=0.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*rgb, 1.0)
    if emit:
        b.inputs["Emission Color"].default_value = (*rgb, 1.0)
        b.inputs["Emission Strength"].default_value = emit
    m.diffuse_color = (*rgb, 1.0)
    return m


def _box(name, size, loc, mat=None):
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=loc)
    o = bpy.context.active_object
    o.scale = size
    o.name = name
    if mat is not None:
        o.data.materials.append(mat)
    return o


def _glow_ship(out, name="glow_ship.glb"):
    """Box hull (grey) + cone nose at +Y + two orange emissive engines aft + an amber status light on top."""
    af.reset_scene()
    grey, orange, amber = _mat("hull", (0.55, 0.52, 0.48)), _mat("engine", (1.0, 0.32, 0.05), 4.0), \
        _mat("light", (1.0, 0.45, 0.08), 6.0)
    objs = [_box("hull", (4.0, 16.0, 3.0), (0, 0, 0), grey)]
    bpy.ops.mesh.primitive_cone_add(vertices=16, radius1=1.8, depth=4.0, location=(0, 10.0, 0), rotation=(-math.pi / 2, 0, 0))
    objs.append(bpy.context.active_object)
    objs[-1].data.materials.append(grey)
    for x in (-1.0, 1.0):
        bpy.ops.mesh.primitive_cylinder_add(vertices=20, radius=0.7, depth=1.6, location=(x, -8.6, 0),
                                            rotation=(math.pi / 2, 0, 0))
        objs.append(bpy.context.active_object)
        objs[-1].data.materials.append(orange)
    objs.append(_box("light", (0.4, 0.4, 0.3), (0, 2.0, 1.6), amber))
    af.apply_transforms(objs)
    path = os.path.join(out, name)
    af.export_glb(path, objs, animations=False, skins=False)
    return path


@_clean
def test_ship_from_mesh_palette_input_keeps_glow_colours():
    """Review 10.10 (A2): classify replaced the input's orange glow by the faction's blue. palette 'input' (default)
    keeps drive and status colours from the input; 'faction' applies the faction palette."""
    tmp = _mkdtemp("af_ships_src_")
    src = _glow_ship(tmp)
    res, _ = _run(ship_from_mesh, "ship_from_mesh", {"class": "fighter", "nose": "+y", "preview": False,
                                                     "lods": False}, {"mesh": src})
    assert res["ok"], _failed(res)
    pal = res["stats"]["palette"]
    for k in ("drive", "status"):
        r, g, b = pal[k]
        assert r == 1.0 and g < 0.7 and b < 0.25 and r > g > b, (k, pal[k])      # orange, not blue
    assert res["stats"]["palette_source"]["drive"] == "input"
    assert res["stats"]["face_labels"].get("drive", 0) > 0 and res["stats"]["face_labels"].get("status", 0) > 0
    doc, _, _ = sl.read_glb(res["outputs"]["glb"])
    em = {m["name"]: m.get("emissiveFactor") for m in doc["materials"]}
    assert em["AF_EmissiveDrive"][0] > em["AF_EmissiveDrive"][2] * 3, em
    res2, _ = _run(ship_from_mesh, "ship_from_mesh", {"class": "fighter", "nose": "+y", "palette": "faction",
                                                      "faction": "military", "preview": False,
                                                      "lods": False}, {"mesh": src})
    p2 = res2["stats"]["palette"]["drive"]
    assert p2[2] > p2[0], p2                                                   # the military blue
    res3, _ = _run(ship_from_mesh, "ship_from_mesh", {"class": "fighter", "nose": "+y", "livery": {"drive": "#00ff00"},
                                                      "preview": False, "lods": False},
                   {"mesh": src})
    assert res3["stats"]["palette"]["drive"][1] > 0.99 and res3["stats"]["palette_source"]["drive"] == "livery"


def _barrel_ship(out):
    """Hull 6 m wide with two thin 4 m barrels per side (gun stubs) and two engines: nav lights must stay on the hull,
    turret_sockets='protrusions' puts turret sockets at the barrel bases."""
    af.reset_scene()
    objs = [_box("hull", (6.0, 24.0, 4.0), (0, 0, 0))]
    bpy.ops.mesh.primitive_cone_add(vertices=16, radius1=2.8, depth=5.0, location=(0, 14.5, 0), rotation=(-math.pi / 2, 0, 0))
    objs.append(bpy.context.active_object)
    for y in (4.0, -4.0):
        for x in (-5.0, 5.0):
            bpy.ops.mesh.primitive_cylinder_add(vertices=12, radius=0.22, depth=4.0, location=(x, y, -0.5),
                                                rotation=(0, math.pi / 2, 0))
            objs.append(bpy.context.active_object)
    for x in (-1.6, 1.6):
        bpy.ops.mesh.primitive_cylinder_add(vertices=20, radius=1.0, depth=2.0, location=(x, -12.8, 0),
                                            rotation=(math.pi / 2, 0, 0))
        objs.append(bpy.context.active_object)
    af.apply_transforms(objs)
    path = os.path.join(out, "barrel_ship.glb")
    af.export_glb(path, objs, animations=False, skins=False)
    return path


@_clean
def test_ship_from_mesh_nav_lights_on_hull_and_protrusion_turrets():
    """Review 10.10 (A3): nav lights sat on the vertex with the smallest X (a barrel tip). They now sit on the main
    hull body (top view opened: thin protrusions are not hull); turret_sockets='protrusions' finds the barrels."""
    tmp = _mkdtemp("af_ships_src_")
    src = _barrel_ship(tmp)
    res, _ = _run(ship_from_mesh, "ship_from_mesh", {"class": "corvette", "length_m": 34.0, "nose": "+y",
                                                     "turret_sockets": "protrusions", "turret_count": 4,
                                                     "preview": False, "lods": False},
                  {"mesh": src})
    assert res["ok"], _failed(res)
    k = 34.0 / 30.8                                   # input length 30.8 (engines -13.8 .. nose tip 17) -> 34 m
    summ = sl.glb_summary(res["outputs"]["glb"])
    socks = {s["game_socket"]: s for s in summ["sockets"]}
    for side, sgn in (("port", -1), ("starboard", 1)):
        nav = socks["socket_fx_nav_" + side]["position_gltf"]
        assert sgn * nav[0] > 0 and abs(nav[0]) <= 3.0 * k + 0.25, (side, nav)   # on the 6 m hull, not at |x| 7 m
    _nav(summ)
    tur = [s for s in summ["sockets"] if s["extras"].get("kind") == "turret"]
    assert len(tur) == 4, [t["game_socket"] for t in tur]
    for t in tur:
        x, _, z = t["position_gltf"]
        assert abs(abs(x) - 3.0 * k) < 0.6, t                 # at the hull side, the barrel base
        assert t["y_gltf"][0] * x > 0 and abs(t["y_gltf"][0]) > 0.99, t   # +Y glTF (= Blender +Z) points outward
        assert abs(t["extras"]["barrel_length_m"] - 4.0 * k) < 1.0, t["extras"]
    names = sorted(t["game_socket"] for t in tur)
    assert names == ["socket_turret_port_aft", "socket_turret_port_forward", "socket_turret_starboard_aft",
                     "socket_turret_starboard_forward"], names


def _concept_and_plain(out):
    """A coloured synthetic ship rendered as a 'concept' (Workbench flat colours, alpha) from a 3/4 camera, and the
    same geometry without any material (what a shape generator returns)."""
    af.reset_scene()
    light, dark, glow = _mat("armour", (0.62, 0.56, 0.5)), _mat("band", (0.008, 0.008, 0.009)), \
        _mat("glow", (1.0, 0.3, 0.04), 4.0)
    objs = [_box("hull", (4.0, 16.0, 3.0), (0, 0, 0), light), _box("band", (4.3, 3.0, 3.3), (0, 2.0, 0), dark)]
    bpy.ops.mesh.primitive_cone_add(vertices=24, radius1=1.9, depth=4.0, location=(0, 10.0, 0), rotation=(-math.pi / 2, 0, 0))
    objs.append(bpy.context.active_object)
    objs[-1].data.materials.append(light)
    for x in (-1.0, 1.0):
        bpy.ops.mesh.primitive_cylinder_add(vertices=24, radius=0.75, depth=1.6, location=(x, -8.6, 0),
                                            rotation=(math.pi / 2, 0, 0))
        objs.append(bpy.context.active_object)
        objs[-1].data.materials.append(glow)
    af.apply_transforms(objs)
    scene = bpy.context.scene
    cam = sl.cam_from_params([235.0, 28.0, 46.0, 0.0, 0.0, 0.0, 0.0, 50.0], (720, 480))
    cd = bpy.data.cameras.new("c")
    cd.lens, cd.sensor_width, cd.sensor_fit = 50.0, 36.0, "HORIZONTAL"
    co = bpy.data.objects.new("c", cd)
    scene.collection.objects.link(co)
    M = Matrix(cam["rotation_matrix"]).to_4x4()
    M.translation = cam["location"]
    co.matrix_world = M
    scene.camera = co
    r = scene.render
    r.engine = af.engine_id("WORKBENCH")
    r.resolution_x, r.resolution_y, r.resolution_percentage = 720, 480, 100
    r.film_transparent = True
    r.image_settings.file_format, r.image_settings.color_mode = "PNG", "RGBA"
    scene.view_settings.view_transform = "Standard"
    sh = scene.display.shading
    sh.light, sh.color_type = "FLAT", "MATERIAL"
    concept = os.path.join(out, "concept.png")
    r.filepath = concept
    bpy.ops.render.render(write_still=True)
    for o in objs:
        o.data.materials.clear()
    plain = os.path.join(out, "plain.glb")
    af.export_glb(plain, objs, animations=False, skins=False)
    return concept, plain


@_clean
def test_ship_from_mesh_paint_and_decals():
    """params.paint: camera fitted to the concept silhouette, labels projected (dark band -> graphite / panel, orange
    engines -> drive with the concept's colour), text decals on both sides."""
    tmp = _mkdtemp("af_ships_src_")
    concept, plain = _concept_and_plain(tmp)
    res, out = _run(ship_from_mesh, "ship_from_mesh", {
        "class": "fighter", "length_m": 24.0, "nose": "+y", "preview": False,
        "paint": {"image": concept, "camera": "fit", "mirror": "x"},
        "decals": [{"text": "T-01", "side": "both", "position": [-3.0, 0.0], "size": [4.0, 1.6]}]}, {"mesh": plain})
    assert res["ok"], _failed(res)
    st = res["stats"]
    assert st["material_mode"] == "paint" and st["paint"]["camera_from"] == "fit"
    assert st["paint"]["silhouette_iou"] >= 0.9, st["paint"]["silhouette_iou"]
    cam = json.load(open(res["outputs"]["paint_camera"]))
    assert abs(cam["params"]["az_deg"] % 360 - 235) < 15 and abs(cam["params"]["el_deg"] - 28) < 10, cam["params"]
    assert os.path.isfile(res["outputs"]["paint_overlay"])
    drive = st["palette"]["drive"]
    assert drive[0] == 1.0 and drive[2] < 0.25, drive
    assert st["face_labels"].get("drive", 0) > 0
    # the band (input y 0.5 .. 3.5; input length 21.4 from the engines at -9.4 to the nose tip at 12, centre 1.3,
    # scaled to 24 m) is dark
    af.reset_scene()
    objs = af.import_any(res["outputs"]["glb"])
    k = 24.0 / 21.4
    area = {}
    for o in objs:
        if o.type != "MESH" or o.name.startswith("COL_") or not o.data.materials:
            continue
        name = o.data.materials[0].name
        for p in o.data.polygons:
            c = o.matrix_world @ p.center
            yi = c.y / k + 1.3
            outer = abs(c.x / k) > 2.08 or c.z / k > 1.58     # band sides + top (seen or mirrored), not the hull inside
            key = "band" if (0.7 < yi < 3.3 and outer) else "rest"
            area.setdefault(key, {}).setdefault(name, 0.0)
            area[key][name] += p.area
    band = area["band"]
    dark = sum(v for n, v in band.items() if n in ("AF_Graphite", "AF_ServicePanel"))
    assert dark > 0.6 * sum(band.values()), band
    doc, _, _ = sl.read_glb(res["outputs"]["glb"])
    mats = {m["name"]: m for m in doc["materials"]}
    assert mats["AF_Marking"].get("alphaMode") == "BLEND"
    assert len(doc.get("images", [])) >= 1
    dec = st["decal_faces"]["lod0"]
    assert dec.get("0_port", 0) > 4 and dec.get("0_starboard", 0) > 4, dec
    assert all("AF_" in m and "." not in m for m in st["materials"]), st["materials"]
