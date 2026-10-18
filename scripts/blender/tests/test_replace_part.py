"""Self-tests of the replace_part step (region-of-interest part replacement) and _replace_lib.

Synthetic tests build their inputs in tempfile.mkdtemp(); test_real_* use assets on disk and
return "skip: ..." when a file is missing. Run:

    blender -b --factory-startup --python scripts/blender/tests/run_tests.py -- replace_part
"""
import importlib
import math
import os
import shutil
import tempfile

import bmesh
import bpy
import mathutils
import numpy as np

import af_common as af
from steps import _mesh_lib as ml
from steps import _replace_lib as rl

NECRO_HEAD = {"min": [-0.17, -0.22, 1.50], "max": [0.07, 0.10, 1.78]}


# --------------------------------------------------------------------------- helpers

def _tmp():
    return tempfile.mkdtemp(prefix="af_rp_")


def _run(inputs, params, out_dir=None):
    mod = importlib.import_module("steps.replace_part")
    req = {"step": "replace_part", "inputs": inputs, "params": params,
           "out_dir": out_dir or _tmp()}
    os.makedirs(req["out_dir"], exist_ok=True)
    return af.finish(req, mod.run(req))


def _check(res, name):
    for c in res["checks"]:
        if c["name"] == name:
            return c
    raise AssertionError("check %s missing: %s" % (name, [c["name"] for c in res["checks"]]))


def _add(bm, kind, **kw):
    """Add a closed primitive to bm (cylinder along Z, uv sphere or box), return its verts."""
    before = set(bm.verts)
    if kind == "cyl":
        bmesh.ops.create_cone(bm, cap_ends=True, cap_tris=False, segments=kw.get("seg", 24),
                              radius1=kw["r"], radius2=kw["r"], depth=kw["h"])
    elif kind == "sphere":
        bmesh.ops.create_uvsphere(bm, u_segments=kw.get("seg", 32), v_segments=kw.get("seg", 32) // 2,
                                  radius=kw["r"])
    elif kind == "box":
        bmesh.ops.create_cube(bm, size=1.0)
    verts = [v for v in bm.verts if v not in before]
    if kind == "box":
        bmesh.ops.scale(bm, vec=mathutils.Vector(kw["size"]), verts=verts)
    bmesh.ops.translate(bm, vec=mathutils.Vector(kw.get("at", (0, 0, 0))), verts=verts)
    return verts


def _save_mesh(path, build, name):
    af.reset_scene()
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    build(bm)
    bmesh.ops.triangulate(bm, faces=bm.faces)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    af.export_glb(path, objects=[ob], animations=False, skins=False)
    af.reset_scene()
    return path


def _sphere_man(bm):
    """Torso + neck + head sphere + an old 'jaw' block that dips 3 cm under the neck cut."""
    _add(bm, "cyl", r=0.20, h=1.20, at=(0, 0, 0.60))
    _add(bm, "cyl", r=0.06, h=0.30, at=(0, 0, 1.33))          # neck 1.18 .. 1.48
    _add(bm, "sphere", r=0.15, at=(0, 0, 1.60))               # head 1.45 .. 1.75
    _add(bm, "box", size=(0.10, 0.04, 0.10), at=(0, -0.12, 1.40))  # old jaw 1.35 .. 1.45


def _new_head(bm):
    """The replacement in its own frame, twice the size and shifted: head sphere, a neck stub
    (must go), a jaw block crossing the cut (must stay) and a floating base plate (must go)."""
    off = (0.5, -0.3, 2.0)

    def at(x, y, z):
        return (2 * x + off[0], 2 * y + off[1], 2 * z + off[2])
    _add(bm, "sphere", r=0.30, seg=40, at=at(0, 0, 0))
    _add(bm, "cyl", r=0.10, h=0.40, at=at(0, 0, -0.20))        # stub to -0.30 (world 1.30)
    _add(bm, "box", size=(0.16, 0.08, 0.16), at=at(0, -0.12, -0.20))  # jaw 1.36 .. 1.44
    _add(bm, "box", size=(0.50, 0.40, 0.01), at=at(0, 0, -0.33))     # plate at world 1.27


# --------------------------------------------------------------------------- unit tests

def test_umeyama_recovers_similarity():
    rng = np.random.default_rng(4)
    P = rng.normal(size=(200, 3))
    ang = math.radians(25)
    R = np.array([[math.cos(ang), -math.sin(ang), 0], [math.sin(ang), math.cos(ang), 0], [0, 0, 1]])
    Q = 0.37 * P @ R.T + np.array([1.0, -2.0, 0.5])
    s, R2, t = rl.umeyama(P, Q)
    assert abs(s - 0.37) < 1e-9 and np.allclose(R2, R, atol=1e-9) and np.allclose(t, [1, -2, 0.5])
    s, R2, t = rl.umeyama(P, 0.5 * P + 3.0, with_rotation=False)
    assert abs(s - 0.5) < 1e-9 and np.allclose(R2, np.eye(3)) and np.allclose(t, 3.0)
    p, n = rl.plane_from_params({"axis": "z", "value": 1.5}, [0, 0, 1], [1, 1, 2])
    assert np.allclose(p, [0, 0, 1.5]) and np.allclose(n, [0, 0, 1])
    p, n = rl.plane_from_params(None, [0, 0, 1.2], [1, 1, 2])
    assert np.allclose(p, [0, 0, 1.2]) and np.allclose(n, [0, 0, 1])
    p, n = rl.plane_from_params({"point": [0, 0, 1], "normal": [0, 0, 2], "side": "-"}, None, None)
    assert np.allclose(n, [0, 0, -1])


def test_icp_recovers_offset_and_scale():
    """Sphere samples vs a scaled, shifted sphere: bbox init + ICP land on the truth."""
    af.reset_scene()
    me = bpy.data.meshes.new("s")
    bm = bmesh.new()
    _add(bm, "sphere", r=1.0, seg=48)
    _add(bm, "box", size=(0.6, 0.6, 0.6), at=(0, -0.9, 0.3))   # a "nose", breaks the symmetry
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new("s", me)
    bpy.context.scene.collection.objects.link(ob)
    src = rl.sample_surface(ob, 4000, seed=1)
    truth = (0.2, np.eye(3), np.array([0.3, 0.1, 1.6]))
    tgt = rl.apply_sim(rl.sample_surface(ob, 4000, seed=2), truth)
    init, _ = rl.bbox_init(src, tgt)
    sim, info = rl.icp_similarity(src, tgt, (init[0] * 1.08, init[1], init[2] + 0.02))
    assert abs(sim[0] - 0.2) < 0.004, sim[0]
    assert np.linalg.norm(sim[2] - truth[2]) < 0.006, sim[2]
    # point-to-sample RMS cannot beat the sampling floor: compare with the truth's own value
    floor = rl.pair_rms(src, tgt, truth)
    assert info["rms_m"] <= floor * 1.05 + 1e-4, (info["rms_m"], floor)


def test_replace_synthetic_sphere_man():
    d = _tmp()
    try:
        body = _save_mesh(os.path.join(d, "body.glb"), _sphere_man, "body")
        part = _save_mesh(os.path.join(d, "part.glb"), _new_head, "part")
        res = _run({"mesh": body, "part": part},
                   {"region": {"min": [-0.2, -0.2, 1.38], "max": [0.2, 0.2, 1.80]},
                    "cut_plane": {"axis": "z", "value": 1.38}, "align": "icp",
                    "part_tris": 0, "preview": True, "preview_size": 128},
                   out_dir=os.path.join(d, "out"))
        assert res["ok"], (res.get("error"), res["checks"])
        s = res["stats"]
        a = s["alignment"]
        assert abs(a["scale"] - 0.5) < 0.02, a
        assert np.allclose(a["translation_m"], [-0.25, 0.15, 0.6], atol=0.015), a
        assert a["rms_rel"] < 0.01, a
        # old head and the old jaw's hanging bottom are gone; the neck below the cut stays
        reg = s["region"]
        assert len(reg["removed_hanging_pieces"]) == 1, reg
        assert reg["removed_bbox_min"][2] < 1.36 and reg["removed_bbox_max"][2] > 1.74, reg
        assert len(reg["body_open_loops"]) == 1, reg
        # part: stub (inside the neck loop) and the floating plate dropped, the jaw kept
        trim = s["part"]["trim"]
        assert len(trim["kept_hanging_pieces"]) == 1, trim
        assert any(p["stump"] for p in trim["removed_pieces"]), trim
        assert any(not p["touches_cut"] for p in trim["removed_pieces"]), trim
        seam = s["seam"]
        assert seam["bridged"] and seam["open_seam_edges"] == 0, seam
        assert seam["boundary_edges_after"] == 0, seam           # closed body stays closed
        assert _check(res, "seam_closed")["ok"]
        assert s["tris_before"] > 0 and s["tris_after"] > 0
        for k in ("glb", "blend", "part_glb", "compare"):
            assert os.path.isfile(res["outputs"][k]), k
        # the result re-imports as one mesh whose top is the new head (1.75 m)
        af.reset_scene()
        objs = [o for o in af.import_any(res["outputs"]["glb"]) if o.type == "MESH"]
        assert len(objs) == 1
        lo, hi = af.world_bbox(objs)
        assert abs(hi.z - 1.75) < 0.01 and lo.z < 0.01, (lo, hi)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_replace_bbox_mode_and_errors():
    d = _tmp()
    try:
        body = _save_mesh(os.path.join(d, "body.glb"), _sphere_man, "body")
        part = _save_mesh(os.path.join(d, "part.glb"), _new_head, "part")
        res = _run({"mesh": body, "part": part},
                   {"region": {"min": [-0.2, -0.2, 1.38], "max": [0.2, 0.2, 1.80]},
                    "cut_plane": {"axis": "z", "value": 1.38}, "align": "bbox",
                    "part_tris": 0, "preview": False}, out_dir=os.path.join(d, "out"))
        assert res["ok"], res["checks"]
        assert res["stats"]["alignment"]["mode"] == "bbox"
        assert abs(res["stats"]["alignment"]["scale"] - 0.5) < 0.03
        for bad in ({"min": [0, 0, 0], "max": [0, 1, 1]}, None):
            try:
                _run({"mesh": body, "part": part}, {"region": bad, "preview": False})
            except ValueError:
                pass
            else:
                raise AssertionError("bad region accepted: %s" % bad)
        try:
            _run({"mesh": body}, {"region": {"min": [-1, -1, 1], "max": [1, 1, 2]}})
        except ValueError:
            pass
        else:
            raise AssertionError("missing inputs.part accepted")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _holey_skull(yaw_deg=20.0):
    """Hollow sphere with two eye holes high on the front and a smaller nose hole lower."""
    af.reset_scene()
    me = bpy.data.meshes.new("skull")
    bm = bmesh.new()
    bmesh.ops.create_icosphere(bm, subdivisions=5, radius=0.1)
    th = math.radians(yaw_deg)
    rot = mathutils.Matrix.Rotation(th, 3, "Z")
    holes = []
    for x, z, r in ((0.40, 0.25, 0.028), (-0.40, 0.25, 0.028), (0.0, -0.25, 0.014)):
        dvec = mathutils.Vector((x, -1.0, z)).normalized() * 0.1
        holes.append((rot @ dvec, r))
    doomed = [f for f in bm.faces
              if any((f.calc_center_median() - c).length < r for c, r in holes)]
    bmesh.ops.delete(bm, geom=doomed, context="FACES")
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new("skull", me)
    bpy.context.scene.collection.objects.link(ob)
    return ob, [h[0] for h in holes]


def test_eye_detection_synthetic():
    ob, holes = _holey_skull(20.0)
    det = rl.detect_eye_sockets(ob)
    assert det["found"], det
    left, right = det["left"]["position"], det["right"]["position"]
    # character's left = +X side of a -Y facing head
    assert left[0] > right[0]
    th = math.radians(det["yaw_deg"])
    fwd = np.array([math.sin(th), math.cos(th), 0.0])            # the winning camera ray
    for e, h in ((left, holes[0]), (right, holes[1])):
        # seen along the camera ray the sphere sits on the hole, and it is inside the shell
        dv = np.asarray(e) - np.array(h)
        off = dv - fwd * float(dv @ fwd)
        assert np.linalg.norm(off) < 0.012, (e, h, det["yaw_deg"])
        assert np.linalg.norm(e) < 0.1, e
    assert abs(det["facing_yaw_deg"] - 20.0) < 6.0, det["facing_yaw_deg"]
    assert det["left"]["rim_to_bottom_m"] > 0.05            # through-holes: deep
    # the nose hole is not taken for an eye
    assert min(np.linalg.norm(np.asarray(left) - np.array(holes[2])),
               np.linalg.norm(np.asarray(right) - np.array(holes[2]))) > 0.03
    # socket depth at a known eye place (along the hole axis) vs the same rays on a ball
    c = np.array(holes[0])
    axis = c / np.linalg.norm(c)
    d_hole = rl.socket_depth_at(ob, c, 0.028, axis)
    af.reset_scene()
    me = bpy.data.meshes.new("ball")
    bm = bmesh.new()
    bmesh.ops.create_icosphere(bm, subdivisions=5, radius=0.1)
    bm.to_mesh(me)
    bm.free()
    ball = bpy.data.objects.new("ball", me)
    bpy.context.scene.collection.objects.link(ball)
    d_ball = rl.socket_depth_at(ball, c, 0.028, axis)
    assert d_hole > 0.05 and d_ball < 0.01, (d_hole, d_ball)


def test_eyes_and_spheres_in_step():
    """Holey skull as the part: spheres + sockets come out, sockets face outward."""
    d = _tmp()
    try:
        def body(bm):
            _add(bm, "cyl", r=0.2, h=1.2, at=(0, 0, 0.6))
            _add(bm, "cyl", r=0.04, h=0.25, at=(0, 0, 1.3))
            _add(bm, "sphere", r=0.1, at=(0, 0, 1.5))
        bpath = _save_mesh(os.path.join(d, "body.glb"), body, "body")
        ob, _ = _holey_skull(0.0)
        bm = bmesh.new()
        bm.from_mesh(ob.data)
        _add(bm, "cyl", r=0.03, h=0.12, at=(0, 0, -0.12))        # neck stub of the part
        bmesh.ops.triangulate(bm, faces=bm.faces)
        bm.to_mesh(ob.data)
        bm.free()
        ppath = af.export_glb(os.path.join(d, "part.glb"), objects=[ob], animations=False,
                              skins=False)
        res = _run({"mesh": bpath, "part": ppath},
                   {"region": {"min": [-0.15, -0.15, 1.36], "max": [0.15, 0.15, 1.62]},
                    "cut_plane": {"axis": "z", "value": 1.36}, "part_tris": 0,
                    "eyes": {"auto": True, "strength": 4}, "preview": False},
                   out_dir=os.path.join(d, "out"))
        assert res["ok"], res["checks"]
        e = res["stats"]["eyes"]
        assert _check(res, "eyes_found")["ok"] and e["l"]["position"][0] > e["r"]["position"][0]
        assert res["stats"]["sockets"] == ["socket_fx_eye_l", "socket_fx_eye_r"]
        assert "eye_glow" in res["stats"]["materials"]
        af.reset_scene()
        objs = af.import_any(res["outputs"]["glb"])
        socks = {o.name: o for o in objs if o.name.startswith("socket_")}
        assert set(socks) == {"socket_fx_eye_l", "socket_fx_eye_r"}
        for o in socks.values():
            assert o.get("game_socket") == o.name
            z = (o.matrix_world.to_3x3() @ mathutils.Vector((0, 0, 1))).normalized()
            assert z.y < -0.8, z                                 # +Z outward (towards -Y)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_front_projection_synthetic():
    """Left half red / right half blue disc projected on a sphere: colours land on the right
    side, hidden faces take the fill slot."""
    d = _tmp()
    try:
        size = 96
        px = np.zeros((size, size, 4), dtype=np.float32)
        yy, xx = np.mgrid[0:size, 0:size]
        disc = (xx - size / 2 + 0.5) ** 2 + (yy - size / 2 + 0.5) ** 2 < (0.42 * size) ** 2
        px[..., 3] = disc
        px[..., 0] = (xx < size / 2)
        px[..., 2] = (xx >= size / 2)
        img = bpy.data.images.new("halves", size, size, alpha=True)
        img.pixels = px[::-1].ravel()
        img.filepath_raw = os.path.join(d, "halves.png")
        img.file_format = "PNG"
        img.save()
        af.reset_scene()
        me = bpy.data.meshes.new("ball")
        bm = bmesh.new()
        bmesh.ops.create_uvsphere(bm, u_segments=48, v_segments=24, radius=0.5)
        bm.to_mesh(me)
        bm.free()
        ob = bpy.data.objects.new("ball", me)
        bpy.context.scene.collection.objects.link(ob)
        stats, mapping, _px, _mask = rl.project_texture(ob, os.path.join(d, "halves.png"),
                                                       fade_frac=0.0)
        assert stats["silhouette_iou"] > 0.9, stats
        assert stats["mask"] == "alpha"
        assert 0.3 < stats["front_fraction"] < 0.6, stats
        tex = bpy.data.images[stats["image"]]
        w, h = tex.size
        tp = np.array(tex.pixels[:], dtype=np.float32).reshape(h, w, 4)
        uv = np.empty(len(me.loops) * 2)
        me.uv_layers.active.data.foreach_get("uv", uv)
        uv = uv.reshape(-1, 2)
        cols = {"left": [], "right": []}
        for poly in me.polygons:
            if poly.material_index != 0:
                continue
            c = poly.center
            if abs(c.x) < 0.08 or c.y > -0.2:
                continue
            u, v = uv[list(poly.loop_indices)].mean(axis=0)
            pix = tp[min(h - 1, int(v * h)), min(w - 1, int(u * w)), :3]
            cols["left" if c.x < 0 else "right"].append(pix)
        L, R = np.mean(cols["left"], axis=0), np.mean(cols["right"], axis=0)
        assert L[0] > 0.8 and L[2] < 0.2, L
        assert R[2] > 0.8 and R[0] < 0.2, R
        back = sum(1 for poly in me.polygons if poly.material_index == 1)
        assert back > 0.3 * len(me.polygons)
        assert [m.name for m in me.materials] == ["part_front_projection", "part_back_fill"]
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- real assets


def test_replace_align_none_keeps_part_pose_materials_and_sockets():
    """align 'none' + part_transform places a procedural part by its pose (no fitting to the removed
    region), keep_materials keeps its own materials, cut_part false keeps it whole and caps the body
    opening; the part's sockets ride with it, the body's sockets outside the region survive and the ones
    inside the region go with it."""
    d = _tmp()
    try:
        af.reset_scene()
        me = bpy.data.meshes.new("body")
        bm = bmesh.new()
        _sphere_man(bm)
        bmesh.ops.triangulate(bm, faces=bm.faces)
        bm.to_mesh(me)
        bm.free()
        body_ob = bpy.data.objects.new("body", me)
        bpy.context.scene.collection.objects.link(body_ob)
        s_keep = af.add_socket("socket_fx_chest", location=(0, -0.2, 1.0), kind="fx")
        s_gone = af.add_socket("socket_fx_eye_l", location=(0.05, -0.12, 1.62), kind="fx")
        body = af.export_glb(os.path.join(d, "body.glb"), objects=[body_ob, s_keep, s_gone], animations=False,
                             skins=False)
        # the part: a small cube (10 cm) in its own frame at the origin, gold material, one socket
        af.reset_scene()
        me = bpy.data.meshes.new("part")
        bm = bmesh.new()
        bmesh.ops.create_cube(bm, size=0.1)
        bm.to_mesh(me)
        bm.free()
        part_ob = bpy.data.objects.new("part", me)
        bpy.context.scene.collection.objects.link(part_ob)
        gold = bpy.data.materials.new("Gold")
        gold.use_nodes = True
        part_ob.data.materials.append(gold)
        ps = af.add_socket("socket_fx_tip", location=(0, 0, 0.05), kind="fx")
        part = af.export_glb(os.path.join(d, "part.glb"), objects=[part_ob, ps], animations=False, skins=False)
        res = _run({"mesh": body, "part": part},
                   {"region": {"min": [-0.2, -0.2, 1.38], "max": [0.2, 0.2, 1.80]},
                    "cut_plane": {"axis": "z", "value": 1.38}, "align": "none", "keep_materials": True,
                    "cut_part": False, "part_transform": {"location": [0, 0, 1.55], "rot_deg": [0, 0, 45]},
                    "part_tris": 0, "preview": False}, out_dir=os.path.join(d, "out"))
        assert res["ok"], (res.get("error"), res["checks"])
        s = res["stats"]
        assert s["alignment"]["mode"] == "none" and abs(s["alignment"]["scale"] - 1.0) < 1e-9, s["alignment"]
        assert "Gold" in s["materials"] and "part_fill" not in s["materials"], s["materials"]
        assert s["part"]["trim"]["cut"] is False and s["part"]["trim"]["tris_after_cut"] == 12, s["part"]["trim"]
        assert s["seam"]["cap_faces"] > 0 and not s["seam"]["bridged"], s["seam"]
        assert s["region"]["sockets_dropped"] == ["socket_fx_eye_l"], s["region"]
        assert sorted(s["sockets"]) == ["socket_fx_chest", "socket_fx_tip"], s["sockets"]
        af.reset_scene()
        objs = af.import_any(res["outputs"]["glb"])
        meshes = [o for o in objs if o.type == "MESH"]
        assert len(meshes) == 1
        lo, hi = af.world_bbox(meshes)
        assert abs(hi.z - 1.6) < 1e-3, hi            # the cube's top (1.55 + 0.05), no scaling to the old head
        socks = {o.get("game_socket"): o for o in objs if o.get("game_socket")}
        bpy.context.view_layer.update()
        assert (socks["socket_fx_tip"].matrix_world.translation - mathutils.Vector((0, 0, 1.6))).length < 1e-4
        assert (socks["socket_fx_chest"].matrix_world.translation - mathutils.Vector((0, -0.2, 1.0))).length < 1e-4
        try:
            _run({"mesh": body, "part": part}, {"region": {"min": [-0.2, -0.2, 1.38], "max": [0.2, 0.2, 1.8]},
                                                "align": "none", "part_transform": {"scale": 0}, "preview": False})
        except ValueError as exc:
            assert "scale" in str(exc)
        else:
            raise AssertionError("no error for part_transform.scale 0")
    finally:
        shutil.rmtree(d, ignore_errors=True)
