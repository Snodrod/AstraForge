"""Self-tests of the items module (item_kit, item_from_mesh, attach_prop).

Zero-arg test_*() functions, temp files only under tempfile.mkdtemp(); tests on real assets are
test_real_* and return early when the file is missing (contract §10).
"""
import json
import math
import os
import random
import tempfile

import bpy
import numpy as np
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

import af_common as af
from steps import _items_lib as L
from steps import attach_prop as A
from steps import item_from_mesh as F
from steps import item_kit as K


def _tmp():
    return tempfile.mkdtemp(prefix="af_items_")


def _req(out, params, inputs=None):
    os.makedirs(out, exist_ok=True)
    return {"inputs": inputs or {}, "params": params, "out_dir": out}


def _errors(result):
    return [c for c in result.get("checks", []) if not c["ok"] and c.get("severity", "error") == "error"]


def _kit_glb(out, params):
    af.reset_scene()
    item, info = K.build_item(params)
    K.export_item(item, out, info, preview=False)
    return os.path.join(out, "item.glb"), info


# =============================================================================== item_kit

def test_kit_all_types_build_and_export():
    out = _tmp()
    for typ in K.TYPES:
        af.reset_scene()
        item, info = K.build_item({"type": typ, "ornament": 2, "seed": 3})
        errs = [c for c in K.item_checks(item, info) if not c["ok"] and c["severity"] == "error"]
        assert not errs, (typ, errs)
        assert info["tris"] <= info["budget_tris"], (typ, info["tris"])
        assert abs(info["bbox_min"][2]) < 1e-4, typ
        assert info["uv_layers"] == ["UVMap"], typ
        assert info["non_manifold_edges"] == 0, (typ, info["non_manifold_edges"])
        assert len(info["materials"]) >= 2, typ
        glb = af.export_glb(os.path.join(out, typ + ".glb"), objects=[item] + list(item.children),
                            animations=False, skins=False)
        summ = L.glb_summary(glb)
        assert "socket_grip_main" in summ["sockets"], (typ, summ["sockets"])
        assert summ["materials"], typ


def test_kit_grip_frame_and_axes():
    # sword: identity grip frame, blade along +Z, edges along Y (width) and flat along X (thickness)
    af.reset_scene()
    item, info = K.build_item({"type": "sword", "seed": 1})
    g = next(s for s in info["sockets"] if s["name"] == "socket_grip_main")
    assert np.allclose(g["z_axis"], [0, 0, 1], atol=1e-4) and np.allclose(g["y_axis"], [0, 1, 0], atol=1e-4)
    co, tri, _ = L.mesh_arrays(item)
    blade = co[co[:, 2] > g["location"][2] + 0.25]
    assert len(blade) and np.ptp(blade[:, 1]) > 2.5 * np.ptp(blade[:, 0]), "blade width must be along Y"
    assert co[:, 2].max() - g["location"][2] > 0.6, "blade must point +Z from the grip"
    # axe: the bit is on -Y (edge = -Y)
    af.reset_scene()
    item, info = K.build_item({"type": "axe", "ornament": 1})
    g = next(s for s in info["sockets"] if s["name"] == "socket_grip_main")["location"]
    co, _, _ = L.mesh_arrays(item)
    head = co[co[:, 2] > co[:, 2].max() - 0.15]
    assert head[:, 1].min() - g[1] < -0.1 and head[:, 1].max() - g[1] < 0.06, "axe bit must face -Y"
    # kite shield: convex painted face toward -Y, handle (grip) behind it
    af.reset_scene()
    item, info = K.build_item({"type": "shield_kite", "ornament": 1})
    g = next(s for s in info["sockets"] if s["name"] == "socket_grip_main")["location"]
    co, tri, _ = L.mesh_arrays(item)
    bvh = BVHTree.FromPolygons(co.tolist(), tri.tolist(), all_triangles=True)
    front = bvh.ray_cast(Vector((g[0], -2, g[2])), Vector((0, 1, 0)), 4)[0]
    assert front is not None and front.y < g[1] - 0.02, "shield face must be on -Y in front of the grip"
    # pistol: muzzle (fx tip) toward -Y, grip axis tilted toward the muzzle
    af.reset_scene()
    item, info = K.build_item({"type": "blaster_pistol"})
    fx = next(s for s in info["sockets"] if s["name"] == "socket_fx_tip")
    gm = next(s for s in info["sockets"] if s["name"] == "socket_grip_main")
    assert np.allclose(fx["z_axis"], [0, -1, 0], atol=1e-3)
    assert gm["z_axis"][1] < -0.1 and gm["z_axis"][2] > 0.9


def test_kit_seed_determinism_and_variation():
    sigs = []
    for seed in range(1, 7):
        af.reset_scene()
        _, info = K.build_item({"type": "sword", "seed": seed})
        sigs.append((info["tris"], tuple(round(x, 4) for x in info["dimensions_m"]),
                     info["resolved"].get("guard"), info["resolved"].get("pommel")))
    assert len(set(sigs)) >= 5, sigs
    af.reset_scene()
    _, a = K.build_item({"type": "sword", "seed": 4})
    assert (a["tris"], tuple(round(x, 4) for x in a["dimensions_m"])) == sigs[3][:2], "same seed must rebuild identically"


def test_kit_batch_contact_sheet():
    out = _tmp()
    res = K.run(_req(out, {"type": "staff", "batch": [{"seed": 1}, {"seed": 2, "style": "dark", "head": "skull"},
                                                      {"seed": 3, "style": "elven"},
                                                      {"type": "potion", "name": "../../escape"}]}))
    assert not _errors(res), _errors(res)
    sheet = res["outputs"]["contact_sheet"]
    assert os.path.exists(sheet)
    batch = json.load(open(res["outputs"]["batch_json"], encoding="utf-8"))
    assert len(batch["variants"]) == 4
    real_out = os.path.realpath(out)
    for v in batch["variants"]:
        assert os.path.exists(v["glb"]) and os.path.exists(os.path.join(v["dir"], "item.json"))
        assert os.path.commonpath([real_out, os.path.realpath(v["dir"])]) == real_out, v["dir"]
    assert batch["variants"][3]["name"] == "escape", batch["variants"][3]["name"]
    # the pipeline needs a glb / blend / preview from a batch too (ForgePipeline.advanceState takes the first)
    o = res["outputs"]
    assert isinstance(o["glb"], list) and len(o["glb"]) == 4 and all(os.path.exists(p) for p in o["glb"])
    assert isinstance(o["blend"], list) and len(o["blend"]) == 4 and o["preview"] == sheet


def test_kit_single_request_outputs():
    out = _tmp()
    res = K.run(_req(out, {"type": "shield_round", "style": "dark", "ornament": 3, "preview_size": 160}))
    assert not _errors(res)
    for k in ("glb", "blend", "json", "preview"):
        assert os.path.exists(res["outputs"][k]), k
    # regression: the preview size is params.preview_size; params.size scales crossbows / blasters
    res = K.run(_req(os.path.join(out, "xb"), {"type": "crossbow", "preview_size": 128, "batch": [{}, {"type": "blaster_rifle"}]}))
    batch = json.load(open(res["outputs"]["batch_json"], encoding="utf-8"))
    assert all(max(v["dimensions_m"]) < 1.2 for v in batch["variants"]), [v["dimensions_m"] for v in batch["variants"]]
    assert not _errors(res), _errors(res)


def test_kit_materials_principled_check():
    af.reset_scene()
    item, info = K.build_item({"type": "sword", "seed": 2})
    assert all(m["principled_to_output"] for m in info["materials"])
    chk = next(c for c in K.item_checks(item, info) if c["name"] == "materials_principled")
    assert chk["ok"], chk
    # a slot whose Principled BSDF is not wired to the output must fail the check
    m = item.material_slots[0].material
    out_node = next(n for n in m.node_tree.nodes if n.type == "OUTPUT_MATERIAL")
    for lk in list(out_node.inputs["Surface"].links):
        m.node_tree.links.remove(lk)
    assert not L.principled_ok(m)
    info["materials"][0]["principled_to_output"] = L.principled_ok(m)
    chk = next(c for c in K.item_checks(item, info) if c["name"] == "materials_principled")
    assert not chk["ok"] and chk["severity"] == "error"


def test_kit_blaster_forward_plus_y():
    # props face +Y (contract §4): params.forward turns the item, sockets keep their frames
    af.reset_scene()
    item, info = K.build_item({"type": "blaster_pistol", "forward": "+Y"})
    fx = next(s for s in info["sockets"] if s["name"] == "socket_fx_tip")
    assert np.allclose(fx["z_axis"], [0, 1, 0], atol=1e-3) and fx["location"][1] > 0.1, fx
    gm = next(s for s in info["sockets"] if s["name"] == "socket_grip_main")
    assert gm["z_axis"][1] > 0.1 and info["axes"]["forward"] == "+Y"
    co, _, _ = L.mesh_arrays(item)
    assert abs(co[:, 2].min()) < 1e-4  # still on the ground


def test_kit_plate_fill_has_interior_vertices():
    # tapered axe cheeks get interior vertices (no long fan triangles -> no radial shading streaks)
    v_plain, f_plain = L.plate(K.axe_outline("medieval", 1.0), 0.02)
    v_fill, f_fill = L.plate(K.axe_outline("medieval", 1.0), lambda u, w: 0.03 + 0.1 * u, fill=0.02)
    assert len(v_fill) > len(v_plain) + 10
    tris = [f for f in f_fill if len(f) == 3]
    V = np.array(v_fill)
    longest = max(np.linalg.norm(V[a] - V[b]) for t in tris for a, b in ((t[0], t[1]), (t[1], t[2]), (t[2], t[0])))
    assert longest < 0.08, longest  # the straight eye side of the outline is 7.6 cm


def test_server_param_shapes():
    # server/forge-steps.mjs sends batch as a count, dims as an object, budget / grip [x, y, z]
    out = _tmp()
    res = K.run(_req(out, {"type": "sword", "seed": 5, "batch": 3, "dims": {"blade_length": 0.6}, "preview": False}))
    batch = json.load(open(res["outputs"]["batch_json"], encoding="utf-8"))
    assert [v["seed"] for v in batch["variants"]] == [5, 6, 7]
    assert all(abs(v["dimensions_m"][2] - 0.6) < 0.25 for v in batch["variants"])
    res = K.run(_req(os.path.join(out, "one"), {"type": "dagger", "batch": 1, "preview": False}))
    assert os.path.exists(res["outputs"]["glb"])
    src, _ = _mangled("sword", out, 3)
    res = F.run(_req(os.path.join(out, "ifm"), {"kind": "blade", "budget": 1500, "grip": [0.3, -0.2, 0.5]}, {"mesh": src}))
    assert res["stats"]["tris"] <= 1500 and res["stats"]["grip"]["method"] == "params.point"


# =============================================================================== item_from_mesh

def _mangled(typ, out, seed, extra=None):
    """A kit item made to look like a generator output: dense, rotated, scaled, no sockets."""
    af.reset_scene()
    item, info = K.build_item(dict({"type": typ, "ornament": 2}, **(extra or {})))
    for ch in list(item.children):
        bpy.data.objects.remove(ch)
    mod = item.modifiers.new("dense", "SUBSURF")
    mod.levels = 2
    L.apply_modifiers(item)
    rng = random.Random(seed)
    axis = Vector((rng.random(), rng.random(), rng.random())).normalized()
    item.matrix_world = Matrix.Translation((0.3, -0.2, 0.5)) @ Matrix.Rotation(rng.uniform(0, 6.28), 4, axis) @ Matrix.Scale(1.7, 4)
    path = os.path.join(out, "%s_generated.glb" % typ)
    af.export_glb(path, objects=[item], animations=False, skins=False)
    return path, info


def _ifm(src, out, params):
    res = F.run(_req(out, params, {"mesh": src}))
    return res, json.load(open(os.path.join(out, "item.json"), encoding="utf-8"))


def test_item_from_mesh_recovers_sword_axe_halberd():
    out = _tmp()
    for typ, kind, tol in (("sword", "blade", 0.02), ("greatsword", "blade", 0.03), ("axe", "axe", 0.03),
                           ("battle_axe", "axe", 0.03), ("halberd", "polearm", 0.05)):
        src, info0 = _mangled(typ, out, 7)
        k0 = {s["name"]: s["location"] for s in info0["sockets"]}
        g0 = k0["socket_grip_main"]
        res, info = _ifm(src, os.path.join(out, typ), {"kind": kind, "length_m": info0["dimensions_m"][2]})
        assert not _errors(res), (typ, _errors(res))
        assert res["stats"]["tris_source"] > 20000 and info["tris"] <= info["budget_tris"], typ
        k1 = {s["name"]: s["location"] for s in info["sockets"]}
        g1 = k1["socket_grip_main"]
        assert abs(g1[2] - g0[2]) < tol, (typ, g0, g1)
        assert abs(info["dimensions_m"][2] - info0["dimensions_m"][2]) < 0.01, typ
        if typ in ("greatsword", "battle_axe"):  # two-handed: main hand high, off hand one hand width lower
            assert "socket_grip_off" in k1, (typ, sorted(k1))
            assert g1[2] - k1["socket_grip_off"][2] >= 0.09 - 1e-4, (typ, g1, k1["socket_grip_off"])
            assert abs(k1["socket_grip_off"][2] - k0["socket_grip_off"][2]) < tol, (typ, k0, k1)
        if kind != "blade" and typ != "battle_axe":  # head mass (bit / blade) on -Y (a double bit is symmetric)
            ob = next(o for o in bpy.data.objects if o.type == "MESH")
            co, tri, _ = L.mesh_arrays(ob)
            c = co[tri].mean(1)
            ar = 0.5 * np.linalg.norm(np.cross(co[tri[:, 1]] - co[tri[:, 0]], co[tri[:, 2]] - co[tri[:, 0]]), axis=1)
            head = (c[:, 2] > c[:, 2].max() - 0.6) & (c[:, 2] < c[:, 2].max() - 0.1)  # below a spear tip
            assert np.average(c[head, 1], weights=ar[head]) < g1[1] - 0.005, typ


def test_item_from_mesh_shield_face_and_grip():
    out = _tmp()
    src, info0 = _mangled("shield_kite", out, 11)
    res, info = _ifm(src, os.path.join(out, "kite"), {"kind": "shield", "length_m": info0["dimensions_m"][2]})
    assert not _errors(res)
    ob = next(o for o in bpy.data.objects if o.type == "MESH")
    co, tri, _ = L.mesh_arrays(ob)
    g = info["sockets"][0]["location"]
    bvh = BVHTree.FromPolygons(co.tolist(), tri.tolist(), all_triangles=True)
    front = bvh.ray_cast(Vector((g[0] + 0.1, -2, g[2])), Vector((0, 1, 0)), 4)[0]
    back = bvh.ray_cast(Vector((g[0] + 0.1, 2, g[2])), Vector((0, -1, 0)), 4)[0]
    assert front.y < back.y < g[1] + 0.01, (front, back, g)
    assert info["report"]["face"]["method"] == "curvature"


def test_item_from_mesh_blade_handles_across_seeds():
    # handle = longest constant-radius round run (spike / teardrop / disc / ball pommels, drooping guards)
    out = _tmp()
    errs = []
    for typ, seed, style in (("sword", 1, "medieval"), ("sword", 3, "dark"), ("dagger", 4, "elven"),
                             ("greatsword", 2, "medieval")):
        src, info0 = _mangled(typ, out, seed, {"seed": seed, "style": style})
        g0 = next(s for s in info0["sockets"] if s["name"] == "socket_grip_main")["location"]
        res, info = _ifm(src, os.path.join(out, "%s_%d" % (typ, seed)), {"kind": "blade",
                                                                          "length_m": info0["dimensions_m"][2]})
        errs.append(abs(info["sockets"][0]["location"][2] - g0[2]))
    assert max(errs) < 0.035, errs


def test_item_from_mesh_module_budget():
    out = _tmp()
    src, info0 = _mangled("sword", out, 5)
    res, info = _ifm(src, os.path.join(out, "clamp"), {"kind": "blade", "budget_tris": 5000})
    assert info["budget_tris"] == 2500 and info["tris"] <= 2500, (info["budget_tris"], info["tris"])
    assert any("clamped" in w for w in res["warnings"]), res["warnings"]
    chk = {c["name"]: c for c in res["checks"]}
    assert chk["within_module_budget"]["ok"] and chk["sockets_in_glb"]["ok"]
    res, info = _ifm(src, os.path.join(out, "over"), {"kind": "blade", "budget_tris": 5000, "allow_over_budget": True})
    chk = {c["name"]: c for c in res["checks"]}
    assert info["tris"] > 2500 and not chk["within_module_budget"]["ok"]
    assert chk["within_module_budget"]["severity"] == "warning" and not _errors(res)


# =============================================================================== attach_prop

def _synthetic_rig(path):
    """Minimal Mixamo-named rig in an A-pose with a boxy skinned body and one clip ('swing')."""
    af.reset_scene()
    ad = bpy.data.armatures.new("Rig")
    arm = bpy.data.objects.new("Armature", ad)
    bpy.context.scene.collection.objects.link(arm)
    af.select_only([arm])
    bpy.ops.object.mode_set(mode="EDIT")
    P = "mixamorig:"
    bones = {"Hips": ((0, 0, 0.95), (0, 0, 1.05), None), "Spine2": ((0, 0, 1.05), (0, 0, 1.38), "Hips")}
    for side, sx in (("Right", -1), ("Left", 1)):
        bones[side + "Arm"] = ((sx * 0.18, 0, 1.42), (sx * 0.40, 0, 1.20), "Spine2")
        bones[side + "ForeArm"] = ((sx * 0.40, 0, 1.20), (sx * 0.58, 0, 1.02), side + "Arm")
        bones[side + "Hand"] = ((sx * 0.58, 0, 1.02), (sx * 0.66, 0, 0.94), side + "ForeArm")
    for name, (h, t, par) in bones.items():
        eb = ad.edit_bones.new(P + name)
        eb.head, eb.tail = h, t
        if par:
            eb.parent = ad.edit_bones[P + par]
    bpy.ops.object.mode_set(mode="OBJECT")
    parts = []
    for name, (h, t, par) in bones.items():
        h, t = Vector(h), Vector(t)
        d = t - h
        if name.endswith("Hand"):
            w, th = 0.075, 0.03
        elif name == "Spine2" or name == "Hips":
            w, th = 0.3, 0.18
        else:
            w, th = 0.08, 0.08
        v, f = L.box(th, w, d.length, (0, 0, d.length / 2))
        q = Vector((0, 0, 1)).rotation_difference(d.normalized())
        M = Matrix.Translation(h) @ q.to_matrix().to_4x4()
        ob = L.part("p_" + name, (L.transform(v, M), f), None, smooth=False)
        vg = ob.vertex_groups.new(name=P + name)
        vg.add(list(range(len(ob.data.vertices))), 1.0, "REPLACE")
        parts.append(ob)
    body = af.join(parts, name="Body")
    body.parent = arm
    mod = body.modifiers.new("Armature", "ARMATURE")
    mod.object = arm
    arm.animation_data_create()
    pb = arm.pose.bones[P + "RightForeArm"]
    pb.rotation_mode = "QUATERNION"
    for fr, ang in ((1, 0.0), (10, 60.0), (20, 0.0)):
        pb.rotation_quaternion = Matrix.Rotation(math.radians(ang), 3, "X").to_quaternion()
        pb.keyframe_insert("rotation_quaternion", frame=fr)
    arm.animation_data.action.name = "swing"
    arm.animation_data.action.use_fake_user = True
    af.save_blend(path)
    return path


def test_attach_synthetic_rig_bone_child_and_skin_rigid():
    out = _tmp()
    rig = _synthetic_rig(os.path.join(out, "rig.blend"))
    sword, _ = _kit_glb(os.path.join(out, "sword"), {"type": "sword", "seed": 2})
    for mode in ("bone_child", "skin_rigid"):
        o = os.path.join(out, mode)
        res = A.run(_req(o, {"hand": "r", "mode": mode, "frames_per_clip": 6}, {"character": rig, "item": sword}))
        assert not _errors(res), (mode, _errors(res))
        for k in ("glb", "blend", "json", "preview", "grip_closeup"):
            assert os.path.exists(res["outputs"][k]), (mode, k)
        summ = L.glb_summary(res["outputs"]["glb"])
        assert "swing" in summ["animations"] and summ["skins"] >= 1, summ
        rep = json.load(open(res["outputs"]["json"], encoding="utf-8"))
        att = rep["attachments"][0]
        assert att["rest"]["axis_inside_arm"] == 0
        assert att["rest"]["grip_to_hand_m"] < 0.05
        hs = rep["hand_sockets"]["r"]
        assert hs["info"]["created"] and hs["parent_bone"] == "mixamorig:RightHand"
        # thumb (+Z) forward for an A-pose rig facing -Y
        assert hs["z_axis"][1] < -0.9, hs["z_axis"]
    # skin_rigid: the item vertices are part of the skinned body
    g = L.glb_json(os.path.join(out, "skin_rigid", "equipped.glb"))
    mesh_nodes = [n for n in g["nodes"] if "mesh" in n]
    assert len(mesh_nodes) == 1 and "skin" in mesh_nodes[0], mesh_nodes
    g = L.glb_json(os.path.join(out, "bone_child", "equipped.glb"))
    assert len([n for n in g["nodes"] if "mesh" in n]) == 2


def test_attach_offset_moves_item():
    out = _tmp()
    rig = _synthetic_rig(os.path.join(out, "rig.blend"))
    sword, _ = _kit_glb(os.path.join(out, "sword"), {"type": "dagger"})
    res = A.run(_req(os.path.join(out, "a"), {"hand": "l", "offset": {"loc": [0, 0, 0.02], "rot_deg": [0, 0, 90]},
                                               "clips_check": False}, {"character": rig, "item": sword}))
    rep = json.load(open(res["outputs"]["json"], encoding="utf-8"))
    M = Matrix(rep["attachments"][0]["socket_space_matrix"])
    assert abs(M.translation.z - 0.02) < 1e-4 and abs(M.to_euler().z - math.pi / 2) < 1e-3


def _world_of(name):
    ob = bpy.data.objects[name]
    M = ob.matrix_world.copy()
    R = M.to_3x3().normalized().to_4x4()
    R.translation = M.translation
    return R


def test_attach_shield_mount_swap_by_socket_matching():
    """A shield attached with a fitted mount can be swapped the way Godot does it: match the new
    shield's socket_grip_main to socket_mount_shield_<side> -> the same world matrix; another shield
    reuses the mount (upright, facing outward)."""
    out = _tmp()
    rig = _synthetic_rig(os.path.join(out, "rig.blend"))
    kite, _ = _kit_glb(os.path.join(out, "kite"), {"type": "shield_kite", "ornament": 1})
    rnd, _ = _kit_glb(os.path.join(out, "round"), {"type": "shield_round", "ornament": 1})
    res = A.run(_req(os.path.join(out, "a"), {"hand": "l", "frames_per_clip": 4}, {"character": rig, "item": kite}))
    assert not _errors(res), _errors(res)
    rep = json.load(open(res["outputs"]["json"], encoding="utf-8"))
    att = rep["attachments"][0]
    assert att["mount"] == "socket_mount_shield_l" and rep["mounts"]["socket_mount_shield_l"]["status"] == "created"
    assert att["grip_on_mount_error"]["pos_m"] < 1e-4 and att["swap_by_socket_matching"]
    # glTF: LeftHand joint -> socket_mount_shield_l -> shield mesh; hand socket carries the 4x4 as extras
    g = L.glb_json(res["outputs"]["glb"])
    nodes = g["nodes"]
    idx = {n.get("name"): i for i, n in enumerate(nodes)}
    mount_i = idx["socket_mount_shield_l"]
    parent = {c: i for i, n in enumerate(nodes) for c in n.get("children", [])}
    assert nodes[parent[mount_i]]["name"].endswith("LeftHand"), nodes[parent[mount_i]]["name"]
    assert any("mesh" in nodes[c] for c in nodes[mount_i].get("children", [])), nodes[mount_i]
    assert len(nodes[idx["socket_mount_hand_l"]]["extras"]["shield_mount_matrix"]) == 16
    assert not [n for n in L.glb_summary(res["outputs"]["glb"])["sockets"] if "." in n]
    # swap by socket matching in the saved scene: the same world matrix as the attached kite
    af.open_blend(res["outputs"]["blend"])
    arm = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    arm.data.pose_position = "REST"
    bpy.context.view_layer.update()
    attached = next(o for o in bpy.data.objects if o.get("af_attached_hand") == "l")
    M_attached = attached.matrix_world.copy()
    Mount = _world_of("socket_mount_shield_l")
    for path, name in ((kite, "kite"), (rnd, "round")):
        objs = af.import_any(path)
        root = next(o for o in objs if o.type == "MESH")
        grip = next(o for o in objs if o.get("game_socket") == "socket_grip_main")
        bpy.context.view_layer.update()
        G = grip.matrix_world.copy()
        root.matrix_world = Mount @ G.inverted() @ root.matrix_world
        bpy.context.view_layer.update()
        if name == "kite":
            D = root.matrix_world - M_attached
            assert max(abs(D[i][j]) for i in range(4) for j in range(4)) < 1e-4, (root.matrix_world, M_attached)
        R3 = root.matrix_world.to_3x3().normalized()
        up, face = R3 @ Vector((0, 0, 1)), R3 @ Vector((0, -1, 0))
        rest_up = att["rest"]["shield_up_dot"]
        assert abs(up.z - rest_up) < 0.02, (name, up.z, rest_up)  # same mount -> same attitude
        assert face.x > 0.3, (name, face)  # left hand of a -Y facing rig: outward is +X
    # a second attach_prop run on the output reuses the mount (replace removes the kite)
    res2 = A.run(_req(os.path.join(out, "b"), {"hand": "l", "replace": True, "clips_check": False},
                      {"character": res["outputs"]["blend"], "item": rnd}))
    rep2 = json.load(open(res2["outputs"]["json"], encoding="utf-8"))
    assert rep2["mounts"]["socket_mount_shield_l"]["status"] == "existing"
    assert rep2["replaced_items"], rep2["replaced_items"]
    M1 = Matrix(rep["mounts"]["socket_mount_shield_l"]["socket_space_matrix"])
    M2 = Matrix(rep2["mounts"]["socket_mount_shield_l"]["socket_space_matrix"])
    assert max(abs((M1 - M2)[i][j]) for i in range(4) for j in range(4)) < 1e-4
    assert len([o for o in bpy.data.objects if o.get("af_attached_hand") == "l"]) == 1


def test_attach_clip_names_and_clean_sockets():
    """-loop suffix for looping clips (contract §7); two kit items in one call: dot-free, unique socket
    names and no duplicated '.001' materials; height measured in the rest pose."""
    out = _tmp()
    rig = _synthetic_rig(os.path.join(out, "rig.blend"))
    # a clip file named idle (armature-only FBX of the rig swing action, like a Mixamo clip) and a posed rig
    af.open_blend(rig)
    arm = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    L.assign_action(arm, bpy.data.actions["swing"])
    clip = af.export_fbx(os.path.join(out, "clips", "idle.fbx"), objects=[arm], object_types={"ARMATURE"},
                         bake_anim=True, bake_anim_use_all_actions=False, bake_anim_use_nla_strips=False)
    arm.data.pose_position = "REST"
    bpy.context.view_layer.update()
    body = next(o for o in bpy.data.objects if o.type == "MESH")
    rest_h = float(np.ptp(L.mesh_arrays(body)[0][:, 2]))
    arm.data.pose_position = "POSE"
    pb = arm.pose.bones["mixamorig:Spine2"]
    pb.rotation_mode = "XYZ"
    pb.rotation_euler = (math.radians(85), 0, 0)
    posed = af.save_blend(os.path.join(out, "posed.blend"))
    staff, _ = _kit_glb(os.path.join(out, "staff"), {"type": "staff", "head": "orb", "seed": 1})
    wand, _ = _kit_glb(os.path.join(out, "wand"), {"type": "wand", "seed": 1})
    res = A.run(_req(os.path.join(out, "a"), {"attachments": [{"item": staff, "hand": "r"}, {"item": wand, "hand": "l"}],
                                              "frames_per_clip": 4},
                     {"character": posed, "clips": [clip, {"file": clip, "name": "strike"}]}))
    assert not _errors(res), _errors(res)
    rep = json.load(open(res["outputs"]["json"], encoding="utf-8"))
    assert abs(rep["height_m"] - rest_h) < 0.01, (rep["height_m"], rest_h)
    summ = L.glb_summary(res["outputs"]["glb"])
    assert "idle-loop" in summ["animations"] and "strike" in summ["animations"], summ["animations"]
    socks = set(summ["sockets"])
    assert {"socket_fx_tip_r", "socket_fx_tip_l", "socket_grip_off_r"} <= socks, sorted(socks)
    assert "socket_grip_main" not in socks and not [n for n in socks if "." in n], sorted(socks)
    assert not [m for m in summ["materials"] if m and "." in m], summ["materials"]


# =============================================================================== evidence (not a test)

def _png_rgb(src, dst, max_w=1280):
    """Re-save a preview as an opaque, maximally compressed RGB PNG (docs evidence <= 400 KB)."""
    arr = L.load_png(src)
    if arr.shape[1] > max_w:
        k = max_w / float(arr.shape[1])
        arr = L.resize_nearest(arr, max_w, int(arr.shape[0] * k))
    arr = L.over_bg(arr)
    h, w = arr.shape[:2]
    im = bpy.data.images.new("af_evidence", width=w, height=h, alpha=False)
    im.pixels = np.ascontiguousarray(arr[::-1]).ravel()
    st = bpy.context.scene.render.image_settings
    st.file_format, st.color_mode, st.compression = "PNG", "RGB", 100
    im.save_render(os.path.abspath(dst), scene=bpy.context.scene)
    bpy.data.images.remove(im)
    return {"path": dst, "kb": round(os.path.getsize(dst) / 1024.0, 1), "px": [w, h]}


def _summarise_attach(res):
    rep = json.load(open(res["outputs"]["json"], encoding="utf-8"))
    out = {"height_m": rep["height_m"], "actions": rep["actions"],
           "mounts": {k: {"status": v["status"], "standoff_m": v["fit_info"].get("standoff_m"),
                          "rotation_euler_deg": v["fit_info"].get("rotation_euler_deg")}
                      for k, v in rep["mounts"].items()},
           "glb_mb": round(rep["glb"]["bytes"] / 1e6, 2), "glb_animations": rep["glb"]["animations"],
           "glb_sockets": sorted(rep["glb"]["sockets"]), "glb_materials": rep["glb"]["materials"], "attachments": []}
    for a in rep["attachments"]:
        e = {"kind": a["kind"], "hand": a["hand"], "mode": a["mode"], "mount": a["mount"], "fit": a["fit"],
             "grip_to_hand_cm": round(100 * a["rest"].get("grip_to_hand_m", 0), 1),
             "enclosure": a["rest"].get("grip_enclosure"),
             "rest_overlap_arm_body": a["rest"]["overlap_arm_tris"] + a["rest"]["overlap_body_tris"],
             "rest_overlap_hand": a["rest"]["overlap_hand_tris"], "frames": a["frames"]["count"],
             "frames_pen": a["frames"]["frames_with_penetration"], "max_body_tris": a["frames"]["max_overlap_body_tris"],
             "max_arm_tris": a["frames"]["max_overlap_arm_tris"], "frames_axis_in_arm": a["frames"]["frames_axis_inside_arm"],
             "grip_on_mount": a["grip_on_mount_error"]}
        if a["kind"].startswith("shield"):
            e.update({"rest_up": a["rest"]["shield_up_dot"], "rest_out": a["rest"]["shield_face_out_dot"],
                      "upright_outward_ratio": a["frames"].get("upright_outward_ratio")})
        out["attachments"].append(e)
    return out


def _grip_world(blend):
    """World matrix (rest pose, unit scale) of the attached item's grip frame and of the left hand socket
    in a saved equipped.blend."""
    af.open_blend(blend)
    arm = next(o for o in bpy.data.objects if o.type == "ARMATURE")
    arm.data.pose_position = "REST"
    bpy.context.view_layer.update()
    return _world_of("socket_mount_hand_l"), next(o for o in bpy.data.objects if o.get("af_attached_hand") == "l")


def test_attach_refit_socket_bakes_offset_for_swaps():
    """refit_socket: the hand socket moves onto the item's grip (the offset is baked into the socket), the
    item hangs under it with an identity offset, the world placement equals the plain offset run, and a
    swap by matching socket_grip_main to the socket reproduces it (what Godot does)."""
    out = _tmp()
    rig = _synthetic_rig(os.path.join(out, "rig.blend"))
    staff, _ = _kit_glb(os.path.join(out, "staff"), {"type": "staff", "seed": 1})
    off = {"loc": [0.01, -0.02, 0.03], "rot_deg": [10, 70, 20]}
    plain = A.run(_req(os.path.join(out, "plain"), {"hand": "l", "offset": off, "clips_check": False},
                       {"character": rig, "item": staff}))
    refit = A.run(_req(os.path.join(out, "refit"), {"hand": "l", "offset": off, "refit_socket": True,
                                                    "clips_check": False}, {"character": rig, "item": staff}))
    assert not _errors(plain) and not _errors(refit), (_errors(plain), _errors(refit))
    rp = json.load(open(plain["outputs"]["json"], encoding="utf-8"))
    rr = json.load(open(refit["outputs"]["json"], encoding="utf-8"))
    assert not rp["attachments"][0]["swap_by_socket_matching"]
    att = rr["attachments"][0]
    assert att["swap_by_socket_matching"] and att["grip_on_mount_error"]["pos_m"] < 1e-5, att
    sr = rr["socket_refit"]["l"]
    assert abs(sr["delta_pos_m"] - math.sqrt(0.01 ** 2 + 0.02 ** 2 + 0.03 ** 2)) < 1e-5, sr
    assert sr["delta_angle_deg"] > 30, sr
    H_plain, item_plain = _grip_world(plain["outputs"]["blend"])
    M_plain = item_plain.matrix_world.copy()
    H_refit, item_refit = _grip_world(refit["outputs"]["blend"])
    D = item_refit.matrix_world - M_plain
    assert max(abs(D[i][j]) for i in range(4) for j in range(4)) < 1e-4    # same placement in the world
    O = L.offset_matrix(off)
    E = H_refit - H_plain @ O
    assert max(abs(E[i][j]) for i in range(4) for j in range(4)) < 1e-4    # socket = old socket x offset
    assert "refit_offset_matrix" in bpy.data.objects["socket_mount_hand_l"].keys()
    # swap by socket matching in the refit scene: the same world matrix as the attached staff
    objs = af.import_any(staff)
    root = next(o for o in objs if o.type == "MESH")
    grip = next(o for o in objs if o.get("game_socket") == "socket_grip_main")
    bpy.context.view_layer.update()
    root.matrix_world = H_refit @ grip.matrix_world.inverted() @ root.matrix_world
    bpy.context.view_layer.update()
    D = root.matrix_world - item_refit.matrix_world
    assert max(abs(D[i][j]) for i in range(4) for j in range(4)) < 1e-4


def test_grip_dir_names_and_created_socket():
    assert (L.grip_dir_vector("out", "l") - Vector((1, 0, 0))).length < 1e-9
    assert (L.grip_dir_vector("out", "r") - Vector((-1, 0, 0))).length < 1e-9
    assert (L.grip_dir_vector("forward", "r") - Vector((0, -1, 0))).length < 1e-9
    assert (L.grip_dir_vector([0, 0, 2], "l") - Vector((0, 0, 1))).length < 1e-9
    for bad in ("sideways", [0, 0, 0]):
        try:
            L.grip_dir_vector(bad, "l")
        except ValueError:
            pass
        else:
            raise AssertionError("no error for %r" % (bad,))
    out = _tmp()
    rig = _synthetic_rig(os.path.join(out, "rig.blend"))
    staff, _ = _kit_glb(os.path.join(out, "staff"), {"type": "staff", "seed": 1})
    res = A.run(_req(os.path.join(out, "a"), {"hand": "l", "grip_dir": "up", "clips_check": False},
                     {"character": rig, "item": staff}))
    rep = json.load(open(res["outputs"]["json"], encoding="utf-8"))
    hs = rep["hand_sockets"]["l"]
    assert hs["info"]["created"] and hs["info"]["thumb_source"] == "grip_dir", hs["info"]
    assert hs["z_axis"][2] > 0.999, hs["z_axis"]
