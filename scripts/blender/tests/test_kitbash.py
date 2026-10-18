"""Self-tests of the kitbash step (scripts/blender/steps/kitbash.py, _kitbash_lib.py) and the rotary turret style.

Zero-argument test_* functions; temp files only under tempfile.mkdtemp() (removed when a test passes).
Run: blender -b --factory-startup --python scripts/blender/tests/run_tests.py -- kitbash
"""
import functools
import json
import math
import os
import shutil
import tempfile

import bpy
import numpy as np
from mathutils import Matrix, Vector

import af_common as af
from steps import _kitbash_lib as kl
from steps import _ship_lib as sl
from steps import kitbash, ship_gen, turret_gen

_TMP = []


def _mkdtemp(prefix):
    d = tempfile.mkdtemp(prefix=prefix)
    _TMP.append(d)
    return d


def _clean(fn):
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
    out = _mkdtemp("af_kb_test_")
    req = {"step": step, "inputs": inputs or {}, "params": params, "out_dir": out}
    res = af.finish(req, module.run(req))
    return res, out


def _errors(res):
    return [c["name"] for c in res["checks"] if not c["ok"] and c.get("severity", "error") == "error"]


@_clean
def test_regions_frames_and_part_builders():
    P = np.array([[0, 0.5, 0], [0, 2.5, 0], [0.9, 1.0, 0], [0.2, 1.0, 0.2]], float)
    cyl = kl.in_region(P, {"cylinder": {"p0": [0, 0, 0], "p1": [0, 2, 0], "radius": 0.5}})
    assert cyl.tolist() == [True, False, False, True], cyl
    sph = kl.in_region(P, {"sphere": {"center": [0, 1, 0], "radius": 0.6}})
    assert sph.tolist() == [True, False, False, True], sph
    box = kl.in_region(P, {"box": {"min": [-1, 0, -1], "max": [1, 1.2, 1]}})
    assert box.tolist() == [True, False, True, True], box
    M = kl.frame_matrix((1, 2, 3), (1, 0, 0), (0, 0, 1))
    R = np.array(M.to_3x3())
    assert np.allclose(R.T @ R, np.eye(3), atol=1e-6) and np.allclose(R[:, 1], [1, 0, 0]), R
    assert np.allclose(R[:, 2], [0, 0, 1]) and np.allclose(np.array(M.translation), [1, 2, 3])
    counts = {}
    for lod in (0, 1, 2):
        kit = sl.Kit(lod=lod, size_ref=60.0)
        info = kl.gatling(kit, kl.frame_matrix((0, 0, 0), (1, 0, 0)), barrel_len=5.0, cluster_d=1.0)
        g = kit.tri_count()
        kl.nozzle(kit, kl.frame_matrix((0, -30, 0), (0, -1, 0)), D=5.0)
        counts[lod] = (g, kit.tri_count() - g)
        mats = {sl.MATS[f.material_index] for f in kit.bm.faces}
        assert {"graphite", "trim", "drive"} <= mats, mats
        if lod == 0:
            # the nozzle's glowing interior faces point inwards (towards the axis), the outer shell outwards
            kit.bm.normal_update()
            inner = [f for f in kit.bm.faces if sl.MATS[f.material_index] == "drive"
                     and -29.9 < f.calc_center_median().y < -26.5]  # exit at y = -30, bell runs forward
            assert inner, "no drive faces inside the bell"
            radial = [Vector((f.calc_center_median().x, 0, f.calc_center_median().z)) for f in inner]
            dots = [f.normal.dot(r.normalized()) for f, r in zip(inner, radial) if r.length > 0.2]
            assert np.mean(dots) < -0.3, np.mean(dots)
            assert abs(info["muzzle"][1] - (0.26 + 5.0 * 0.36 + 5.0)) < 1e-6, info
        kit.bm.free()
    assert counts[0][0] > counts[1][0] > counts[2][0] > 0, counts
    assert counts[0][1] > counts[1][1] > counts[2][1] > 0, counts
    kit = sl.Kit(lod=0, size_ref=60.0)
    kl.tube(kit, [(0, 0, 0), (1, 1, 2), (3, 1, 2)], 0.2)
    kl.antenna(kit, kl.frame_z((0, 0, 0), (0, 0, 1)), 3.0, "whip")
    kl.antenna(kit, kl.frame_z((2, 0, 0), (0, 0, 1)), 1.0, "dome", radius=0.5)
    kl.nav_light(kit, kl.frame_z((4, 0, 0), (1, 0, 0)), 0.5)
    kl.vent_grille(kit, kl.frame_z((6, 0, 0), (1, 0, 0)), (4.0, 1.2), 6)
    kl.block(kit, kl.frame_matrix((8, 0, 0), (0, 1, 0)), (1, 2, 1), "graphite", panel="panel")
    mats = {sl.MATS[f.material_index] for f in kit.bm.faces}
    assert {"trim", "graphite", "status", "panel"} <= mats, mats
    kit.bm.free()


def _gunship(out):
    req = {"step": "ship_gen", "inputs": {}, "out_dir": out,
           "params": {"class": "gunship", "seed": 1, "faction": "industrial", "preview": False}}
    res = af.finish(req, ship_gen.run(req))
    assert res["ok"], _errors(res)
    return res


@_clean
def test_kitbash_on_procedural_gunship_glb():
    """ship_gen gunship (GLB + LOD siblings + ship.json) -> every part kind; budgets, sockets, LODs, manifest."""
    src = _mkdtemp("af_kb_src_")
    g = _gunship(src)
    summ0 = sl.glb_summary(g["outputs"]["glb"])
    turrets = [s for s in summ0["sockets"] if s["extras"].get("kind") == "turret"]
    thr = [s for s in summ0["sockets"] if s["extras"].get("kind") == "thruster"]
    assert turrets and thr
    L = 42.0
    params = {"parts": [
        {"kind": "gatling", "at": "socket_turret_*", "length": 2.5, "cluster_d": 0.5, "seat": 0.0},
        {"kind": "nozzle", "at": "socket_thruster_*"},
        {"kind": "piping", "points": [[1.0, -14.0, 3.0], [2.5, -16.0, 3.5], [3.5, -18.0, 2.0]], "radius": 0.15,
         "count": 2, "mirror": "x"},
        {"kind": "antenna", "at": [0.0, 2.0], "height": 2.5, "socket": "socket_utility_mast_kb"},
        {"kind": "nav_light", "at": [3.0, 0.5], "snap": "side", "side": "both", "size": 0.4},
        {"kind": "vent_grille", "at": [-5.0, 0.0], "side": "both", "size": [3.0, 1.0], "slats": 5},
        {"kind": "block", "at": [0.0, -8.0, 4.0], "size": [1.0, 2.0, 0.6]},
        {"kind": "remove", "regions": [{"sphere": {"center": [0.0, 30.0, 0.0], "radius": 0.5}}]},
        {"kind": "antenna", "at": "socket_does_not_exist"}],
        "preview": False}
    res, out = _run(kitbash, "kitbash", params, {"mesh": g["outputs"]["glb"]})
    errs = _errors(res)
    assert errs == ["kitbash_parts_placed"], (errs, res["warnings"])   # the bogus socket only
    st = res["stats"]
    b = sl.BUDGETS["ship_m"]
    assert st["tris"] <= b["tris"] and st["tris_lod1"] <= b["lod1"] and st["tris_lod2"] <= b["lod2"], st
    assert st["tris"] > summ0["triangles"]
    for k in ("gatling", "nozzle", "piping", "antenna", "nav_light", "vent_grille", "block"):
        assert st["part_tris"]["lod0"].get(k, 0) > 0, (k, st["part_tris"])
    assert st["part_tris"]["lod2"].get("piping", 0) == 0 and st["part_tris"]["lod2"]["nozzle"] > 0
    summ = sl.glb_summary(res["outputs"]["glb"])
    assert summ["surfaces_lod0"] <= max(summ0["surfaces_lod0"], 9), (summ0["surfaces_lod0"], summ["surfaces_lod0"])
    assert set(summ["material_names_used"]) <= set(summ0["material_names_used"]) | {"AF_EmissiveDrive",
                                                                                    "AF_EmissiveStatus"}
    socks = {s["game_socket"]: s for s in summ["sockets"]}
    mz = [s for s in summ["sockets"] if s["extras"].get("kind") == "muzzle"]
    assert len(mz) == len(turrets), (len(mz), len(turrets))
    for t in turrets:
        m = socks["socket_muzzle_" + t["game_socket"][len("socket_turret_"):]]
        assert m["extras"]["mount"] == t["game_socket"] and abs(m["extras"]["diameter_m"] - 1.25) < 1e-6
        assert socks[t["game_socket"]]["extras"].get("builtin") == "gatling_6barrel"
        # fire axis = glTF -Z of the muzzle = away from the mount (the gun's axis is the turret socket's +Z)
        d = np.array(m["position_gltf"]) - np.array(t["position_gltf"])
        assert np.dot(d / np.linalg.norm(d), m["neg_z_gltf"]) > 0.95, (t["game_socket"], d, m["neg_z_gltf"])
        assert abs(np.dot(m["y_gltf"], m["neg_z_gltf"])) < 1e-3
    assert "socket_utility_mast_kb" in socks
    for t in thr:
        assert socks[t["game_socket"]]["extras"]["builtin"] == "nozzle_ribbed_bell"
    for k in ("glb", "lod1", "lod2", "blend", "manifest"):
        assert os.path.isfile(res["outputs"][k]), k
    man = json.load(open(res["outputs"]["manifest"], encoding="utf-8"))
    assert "af_kitbash_v1" in man["generator_version"] and man["triangles"]["lod0"] == st["tris"]
    sil = st["lod_silhouette_iou"]["lod2"]
    assert min(sil["top"], sil["side"]) >= 0.85, sil


def _stub_ship(path):
    """Box hull 20 x 6 x 4 m (nose +Y) with a 3 m stub barrel sticking out of the port side and a turret socket at
    its base pointing outward (-X), as ship_from_mesh(turret_sockets='protrusions') writes it."""
    af.reset_scene()
    root = bpy.data.objects.new("ship_stub_test", None)
    bpy.context.scene.collection.objects.link(root)
    bpy.ops.mesh.primitive_cube_add(size=1.0)
    hull = bpy.context.active_object
    hull.scale = (6.0, 20.0, 4.0)
    bpy.ops.mesh.primitive_cylinder_add(radius=0.25, depth=3.0, location=(-4.5, 2.0, 0.0), rotation=(0, math.pi / 2, 0),
                                        vertices=16)
    stub = bpy.context.active_object
    af.apply_transforms([hull, stub])
    ob = af.join([hull, stub], "M_hull_armor")
    mats = sl.make_materials(sl.palette_for("military", 0))
    ob.data.materials.clear()
    ob.data.materials.append(mats["armor"])
    ob.parent = root
    sock = sl.add_socket("socket_turret_port_forward", "turret", (-3.0, 2.0, 0.0), z_axis=(-1, 0, 0), parent=root,
                         size_class="M", mount_d_m=1.2, barrel_length_m=3.0)
    af.export_glb(path, [root, ob, sock], animations=False, skins=False)
    return path


@_clean
def test_kitbash_removes_ai_stub_and_seats_on_the_wall():
    d = _mkdtemp("af_kb_stub_")
    glb = _stub_ship(os.path.join(d, "ship.glb"))
    res, out = _run(kitbash, "kitbash", {"parts": [{"kind": "gatling", "at": "socket_turret_*", "length": 2.0,
                                                    "cluster_d": 0.5, "remove": {"radius": 0.4}}],
                                         "budget_class": "ship_s", "preview": False},
                    {"mesh": glb})
    assert not _errors(res), _errors(res)
    st = res["stats"]
    assert st["removed_faces"]["0"] >= 16, st["removed_faces"]          # the stub's side faces are gone
    part = st["parts"][0]
    assert abs(part["seat_t"]) < 0.15 and abs(part["at"][0] + 3.0) < 0.15, part   # seated on the hull wall x = -3
    # no hull geometry of the old stub remains outside the wall beyond the new gun's housing
    af.reset_scene()
    objs = af.import_any(res["outputs"]["glb"])
    socks = {o.get("game_socket"): o for o in objs if o.get("game_socket")}
    assert "socket_muzzle_port_forward" in socks
    mz = socks["socket_muzzle_port_forward"].matrix_world
    assert mz.translation.x < -3.0 - 2.0, mz.translation           # muzzle out along -X
    fire = mz.to_3x3() @ Vector((0, 1, 0))                         # Blender +Y = glTF -Z = fire direction
    assert fire.x < -0.99, fire


@_clean
def test_turret_gen_rotary_barrels():
    res, out = _run(turret_gen, "turret_gen", {"size": "M", "barrels": 2, "barrel_style": "rotary", "preview": False})
    assert res["ok"], _errors(res)
    st = res["stats"]
    assert st["tris"] <= sl.BUDGETS["turret"]["tris"], st["tris"]
    res2, _ = _run(turret_gen, "turret_gen", {"size": "M", "barrels": 2, "preview": False})
    assert st["tris"] > res2["stats"]["tris"] * 1.3, (st["tris"], res2["stats"]["tris"])
    names = st["node_names"]
    assert "socket_muzzle_01" in names and "socket_muzzle_02" in names and "pivot_pitch" in names
    res3, _ = _run(turret_gen, "turret_gen", {"size": "M", "barrel_style": "laser", "preview": False,
                                              "lods": False})
    assert any("barrel_style" in w for w in res3["warnings"]), res3["warnings"]
