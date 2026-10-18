"""Self-tests of the core module: steps cleanup / convert / separate and _mesh_lib.

Synthetic tests build their inputs in tempfile.mkdtemp(); test_real_* use assets on disk and
return "skip: ..." when the file is missing. Run:

    blender -b --factory-startup --python scripts/blender/tests/run_tests.py -- core
"""
import importlib
import math
import os
import tempfile
import time

import bmesh
import bpy
import mathutils
import numpy as np

import af_common as af
from steps import _mesh_lib as ml


# --------------------------------------------------------------------------- helpers

def _purge_stale_sessions(max_age_s=2 * 3600):
    """Old af_core_* session folders of earlier runs (only ours, only stale ones)."""
    import shutil
    root = tempfile.gettempdir()
    now = time.time()
    for name in os.listdir(root):
        path = os.path.join(root, name)
        if name.startswith("af_core_") and os.path.isdir(path):
            try:
                if now - os.path.getmtime(path) > max_age_s:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                pass


_purge_stale_sessions()
_SESSION = tempfile.mkdtemp(prefix="af_core_")  # every test writes below this folder


def _tmp():
    return tempfile.mkdtemp(dir=_SESSION)


def _run(step, inputs, params, out_dir=None):
    mod = importlib.import_module("steps." + step)
    req = {"step": step, "inputs": inputs, "params": params, "out_dir": out_dir or _tmp()}
    os.makedirs(req["out_dir"], exist_ok=True)
    res = mod.run(req)
    return af.finish(req, res)


def _check(res, name):
    for c in res["checks"]:
        if c["name"] == name:
            return c
    raise AssertionError("check %s missing: %s" % (name, [c["name"] for c in res["checks"]]))


def _half_texture(name="halves", size=64):
    """Image: left half (u < 0.5) red, right half blue."""
    img = bpy.data.images.new(name, size, size, alpha=False)
    px = np.zeros((size, size, 4), dtype=np.float32)
    px[..., 3] = 1.0
    px[:, : size // 2, 0] = 1.0
    px[:, size // 2:, 2] = 1.0
    img.pixels = px.ravel()
    img.pack()
    return img


def _textured_material(img):
    mat = bpy.data.materials.new("mat_" + img.name)
    mat.use_nodes = True
    nt = mat.node_tree
    tex = nt.nodes.new("ShaderNodeTexImage")
    tex.image = img
    nt.links.new(tex.outputs["Color"], ml.principled_of(mat).inputs["Base Color"])
    return mat


def _sphere_glb(path, with_island=True, segments=48, rings=24):
    """UV sphere r=1 with the half texture, plus a tiny floating cube (an island)."""
    af.reset_scene()
    me = bpy.data.meshes.new("sphere")
    bm = bmesh.new()
    bm.loops.layers.uv.new("UVMap")  # calc_uvs writes into the active UV layer
    bmesh.ops.create_uvsphere(bm, u_segments=segments, v_segments=rings, radius=1.0,
                              calc_uvs=True)
    if with_island:
        ret = bmesh.ops.create_cube(bm, size=0.05)
        bmesh.ops.translate(bm, verts=ret["verts"], vec=(1.6, 0.0, 0.2))
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new("sphere", me)
    bpy.context.scene.collection.objects.link(ob)
    me.materials.append(_textured_material(_half_texture()))
    for poly in me.polygons:
        poly.use_smooth = True
    af.export_glb(path, objects=[ob], animations=False, skins=False)
    return path


def _cylinder(name, radius=0.05, height=2.0, segments=24, rings=40, z0=0.0):
    """Closed cylinder along Z with cylindrical UVs (a stand-in for a staff)."""
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    verts_rings = []
    for r in range(rings + 1):
        z = z0 + height * r / rings
        ring = [bm.verts.new((radius * math.cos(2 * math.pi * s / segments),
                              radius * math.sin(2 * math.pi * s / segments), z))
                for s in range(segments)]
        verts_rings.append(ring)
    for r in range(rings):
        a, b = verts_rings[r], verts_rings[r + 1]
        for s in range(segments):
            bm.faces.new((a[s], a[(s + 1) % segments], b[(s + 1) % segments], b[s]))
    bm.faces.new(list(reversed(verts_rings[0])))
    bm.faces.new(verts_rings[-1])
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    uv = bm.loops.layers.uv.new("UVMap")
    for f in bm.faces:
        for l in f.loops:
            l[uv].uv = (math.atan2(l.vert.co.y, l.vert.co.x) / (2 * math.pi) + 0.5,
                        (l.vert.co.z - z0) / height)
    bm.to_mesh(me)
    bm.free()
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    return ob


def _sample(img, uv):
    w, h = img.size
    px = np.array(img.pixels[:], dtype=np.float32).reshape(h, w, 4)
    x = np.clip((uv[:, 0] % 1.0 * w).astype(int), 0, w - 1)
    y = np.clip((uv[:, 1] % 1.0 * h).astype(int), 0, h - 1)
    return px[y, x, :3]


def _loop_colours(ob, img):
    me = ob.data
    nl = len(me.loops)
    uv = np.empty(nl * 2)
    me.uv_layers.active.data.foreach_get("uv", uv)
    lv = np.empty(nl, dtype=np.int64)
    me.loops.foreach_get("vertex_index", lv)
    co = ml.mesh_arrays(me)["co"][lv]
    return co, _sample(img, uv.reshape(nl, 2))


def _imported_base_color(path):
    af.reset_scene()
    objs = af.import_any(path)
    rep = ml.texture_report(objs)
    sizes = [v["base_color"] for v in rep.values() if "base_color" in v]
    return objs, sizes


def _rigged_glb(path, height=1.7):
    """Skinned cylinder (2 bones) exported as GLB: re-importing it makes the glTF importer add
    its 2 m bone-shape 'Icosphere' in the collection glTF_not_exported."""
    af.reset_scene()
    body = _cylinder("Body", radius=0.15, height=height, segments=16, rings=12)
    arm_data = bpy.data.armatures.new("Armature")
    arm = bpy.data.objects.new("Armature", arm_data)
    bpy.context.scene.collection.objects.link(arm)
    af.select_only([arm])
    bpy.ops.object.mode_set(mode="EDIT")
    hips = arm_data.edit_bones.new("hips")
    hips.head, hips.tail = (0, 0, 0), (0, 0, height / 2)
    chest = arm_data.edit_bones.new("chest")
    chest.head, chest.tail = (0, 0, height / 2), (0, 0, height)
    chest.parent = hips
    bpy.ops.object.mode_set(mode="OBJECT")
    g_hips, g_chest = body.vertex_groups.new(name="hips"), body.vertex_groups.new(name="chest")
    for v in body.data.vertices:
        w = min(1.0, max(0.0, v.co.z / height))
        g_hips.add([v.index], 1.0 - w, "REPLACE")
        g_chest.add([v.index], w, "REPLACE")
    body.parent = arm
    body.modifiers.new("Armature", "ARMATURE").object = arm
    af.export_glb(path, objects=None, animations=False, skins=True)
    return path


def _assert_rigged_inputs_clean(glb, height, d):
    """Rigged GLB through convert (FBX, GLB + height), cleanup join:false and separate loose:
    one mesh each, the right height, feet on z=0 and no bone-shape helper anywhere."""
    af.reset_scene()
    af.import_any(glb, purge_helpers=False)  # precondition: the raw importer still adds the helper
    assert bpy.data.collections.get("glTF_not_exported") is not None, \
        "test input does not trigger the importer helper any more"
    # (a) FBX without params: one mesh, stored height, nothing extra in the file
    res = _run("convert", {"mesh": glb}, {"format": "fbx"}, os.path.join(d, "fbx"))
    assert res["ok"], (res.get("error"), res["checks"])
    rt = res["stats"]["roundtrip"]
    # af.import_any now purges the helper itself, so the step may find nothing left to remove
    assert res["stats"]["import_helpers_removed"] in ([], ["Icosphere"]), res["stats"]
    assert rt["meshes"] == 1 and rt["armatures"] == 1, rt
    assert abs(rt["size"][2] - height) < 2e-3 * height and abs(rt["bbox_min"][2]) < 1e-3, rt
    af.reset_scene()
    names = [o.name for o in af.meshes(af.import_any(res["outputs"]["fbx"]))]
    assert len(names) == 1 and not any("Icosphere" in n for n in names), names
    # (b) GLB with height_m=1.8: the model, not the helper, is 1.8 m and grounded
    res = _run("convert", {"mesh": glb}, {"format": "glb", "height_m": 1.8},
               os.path.join(d, "glb"))
    assert res["ok"], (res.get("error"), res["checks"])
    rt = res["stats"]["roundtrip"]
    assert _check(res, "height_matches")["ok"] and _check(res, "grounded")["ok"], res["checks"]
    assert rt["meshes"] == 1 and abs(rt["size"][2] - 1.8) < 2e-3 and abs(rt["bbox_min"][2]) < 1e-3, rt
    # (c) cleanup join:false keeps the stored height, feet on z=0, one object
    res = _run("cleanup", {"mesh": glb}, {"join": False, "preview": False},
               os.path.join(d, "clean"))
    assert res["ok"], (res.get("error"), res["checks"])
    after = res["stats"]["after"]
    assert after["objects"] == 1 and abs(after["size"][2] - height) < 2e-3 * height, after
    assert abs(after["bbox_min"][2]) < 1e-4, after
    af.reset_scene()  # clean.blend is the current file after the step saved it
    with bpy.data.libraries.load(res["outputs"]["blend"]) as (src, _dst):
        assert not any("Icosphere" in n for n in src.objects), list(src.objects)
    # (d) separate loose: one real part, no fake 'small'
    res = _run("separate", {"mesh": glb}, {"mode": "loose", "preview": False},
               os.path.join(d, "sep"))
    assert res["ok"], (res.get("error"), res["checks"])
    assert res["stats"]["part_count"] == 1, res["stats"]["parts"]
    part = res["stats"]["parts"][0]
    assert part["name"] != "small" and abs(part["size"][2] - height) < 2e-3 * height, part
    assert abs(part["bbox_min"][2]) < 1e-4, part


def _vertex_colour_sphere(name="vc_sphere", with_material=True):
    """UV sphere r=1 with a colour attribute: red where x > 0, blue where x < 0 (corner domain),
    optionally a material that feeds the attribute into Base Color."""
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=48, v_segments=24, radius=1.0)
    bm.to_mesh(me)
    bm.free()
    attr = me.color_attributes.new("Col", "BYTE_COLOR", "CORNER")
    nl = len(me.loops)
    lv = np.empty(nl, dtype=np.int64)
    me.loops.foreach_get("vertex_index", lv)
    x = ml.mesh_arrays(me)["co"][lv, 0]
    col = np.zeros((nl, 4), dtype=np.float32)
    col[:, 3] = 1.0
    col[x >= 0, 0] = 1.0
    col[x < 0, 2] = 1.0
    attr.data.foreach_set("color", col.ravel())
    me.color_attributes.active_color = attr
    for poly in me.polygons:
        poly.use_smooth = True
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    if with_material:
        mat = bpy.data.materials.new("vc_mat")
        mat.use_nodes = True
        node = mat.node_tree.nodes.new("ShaderNodeVertexColor")
        node.layer_name = "Col"
        mat.node_tree.links.new(node.outputs["Color"], ml.principled_of(mat).inputs["Base Color"])
        me.materials.append(mat)
    return ob


def _red_blue_agreement(glb):
    """Share of loops whose baked colour says red on the +X side and blue on the -X side."""
    af.reset_scene()
    objs = ml.prepare_meshes(af.import_any(glb))
    low = objs[0]
    img = ml.image_into(low.data.materials[0], "Base Color").image
    co, col = _loop_colours(low, img)
    side = np.abs(co[:, 0]) > 0.15 * float(np.abs(co[:, 0]).max())
    good = np.where(co[side, 0] > 0, col[side, 0] > col[side, 2], col[side, 2] > col[side, 0])
    return float(good.mean())


# --------------------------------------------------------------------------- _mesh_lib

def test_components_matches_bfs():
    rng = np.random.default_rng(7)
    n = 3000
    edges = rng.integers(0, n, size=(1800, 2))
    labels = ml.components(n, edges)
    adj = [[] for _ in range(n)]
    for a, b in edges.tolist():
        adj[a].append(b)
        adj[b].append(a)
    seen = [-1] * n
    comp = 0
    for s in range(n):
        if seen[s] >= 0:
            continue
        stack = [s]
        seen[s] = comp
        while stack:
            v = stack.pop()
            for w in adj[v]:
                if seen[w] < 0:
                    seen[w] = comp
                    stack.append(w)
        comp += 1
    assert len(np.unique(labels)) == comp, (len(np.unique(labels)), comp)
    for a, b in edges.tolist():
        assert labels[a] == labels[b]


def test_metrics_and_islands():
    a = _cylinder("a")
    b = _cylinder("b", z0=3.0)
    ob = af.join([a, b], name="ab")
    m = ml.mesh_metrics([ob])
    assert m["loose_parts"] == 2, m
    assert m["boundary_edges"] == 0 and m["non_manifold_edges"] == 0, m
    assert m["nan_verts"] == 0
    # a tiny third piece is removed by the island filter, the two big ones stay
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    bmesh.ops.create_cube(bm, size=0.01)
    bm.to_mesh(ob.data)
    bm.free()
    parts, verts = ml.remove_small_islands(ob, 0.05)
    assert (parts, verts) == (1, 8), (parts, verts)
    assert ml.mesh_metrics([ob])["loose_parts"] == 2


def test_rotate_z_quaternion_mode():
    """glTF imports use quaternion rotation mode; rotate_z must still turn them."""
    ob = _cylinder("q", height=1.0)
    ob.rotation_mode = "QUATERNION"
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.active_object
    cube.rotation_mode = "QUATERNION"
    cube.scale = (2.0, 0.5, 0.5)
    bpy.context.view_layer.update()
    ml.rotate_z([cube], 90)
    lo, hi = af.world_bbox([cube])
    assert abs((hi - lo).x - 1.0) < 1e-4 and abs((hi - lo).y - 4.0) < 1e-4, (lo, hi)


def test_allocate_budget_protects_low_poly():
    # a 12-tri box next to big meshes stays whole; the rest is split in proportion
    b = ml.allocate_budget([12, 100000, 20000], 30000, 64)
    assert b[0] == 12 and sum(b) <= 30000 and b[1] > 4 * b[2] > 0, b
    # a mid object whose share would fall under the floor gets the floor
    b = ml.allocate_budget([500, 100000], 10000, 64)
    assert b[0] == 64 and b[1] == 9936, b
    # everything fits: nothing changes
    assert ml.allocate_budget([10, 20], 100) == [10, 20]
    # floors do not fit: plain proportional split, still >= 4 each
    b = ml.allocate_budget([1000, 1000, 1000], 90, 64)
    assert b == [30, 30, 30], b


def test_cut_by_boxes_closes_both_sides():
    ob = _cylinder("staff", radius=0.05, height=2.0)
    vol0 = ml.signed_volume(ob)
    part, info = ml.cut_by_boxes(ob, [([-0.2, -0.2, 1.2], [0.2, 0.2, 2.1])], "top")
    assert part is not None
    assert info["open_seam_edges"] == 0 and info["rest_open_seam_edges"] == 0, info
    for o in (part, ob):
        m = ml.mesh_metrics([o])
        assert m["boundary_edges"] == 0 and m["non_manifold_edges"] == 0, (o.name, m)
    v_top, v_rest = ml.signed_volume(part), ml.signed_volume(ob)
    assert v_top > 0 and v_rest > 0, (v_top, v_rest)
    assert abs(v_top + v_rest - vol0) < 0.01 * vol0, (v_top, v_rest, vol0)
    assert abs(v_top / vol0 - 0.4) < 0.02, v_top / vol0
    lo, hi = af.world_bbox([part])
    assert abs(lo.z - 1.2) < 1e-4 and abs(hi.z - 2.0) < 1e-4, (lo, hi)


# --------------------------------------------------------------------------- cleanup

def test_cleanup_synthetic_decimate_keeps_texture():
    d = _tmp()
    src = _sphere_glb(os.path.join(d, "sphere.glb"))
    res = _run("cleanup", {"mesh": src},
               {"height_m": 2.0, "target_tris": 1000, "remove_islands_below": 0.01,
                "preview_size": 192}, os.path.join(d, "out"))
    assert res["ok"], res.get("error")
    s = res["stats"]
    assert s["before"]["boundary_edges"] > 0, s["before"]  # glTF split the UV seam open
    assert s["welded"]["boundary_edges"] == 0, s["welded"]  # ...and the weld closed it again
    assert s["welded"]["loose_parts"] == 1, s["welded"]     # tiny cube removed as an island
    assert s["islands_removed"] == 1 and s["island_verts_removed"] == 8, s
    assert s["after"]["tris"] <= 1000
    assert abs(s["after"]["size"][2] - 2.0) < 2e-3
    assert _check(res, "base_color_texture_kept")["ok"]
    # removing the island re-centres the sphere; the comparison frame must follow it
    # (it did not before the fix round: IoU 0.668 from a pure 0.31 m shift)
    assert s["render_compare"]["silhouette_iou"] >= 0.97, s["render_compare"]
    assert os.path.isfile(res["outputs"]["preview"]) and os.path.isfile(res["outputs"]["blend"])
    objs, sizes = _imported_base_color(res["outputs"]["glb"])
    assert sizes == [[64, 64]], sizes
    assert ml.mesh_metrics(objs)["tris"] <= 1000


def test_cleanup_rotate_and_keep_scale():
    d = _tmp()
    af.reset_scene()
    bpy.ops.mesh.primitive_cube_add()
    cube = bpy.context.active_object
    cube.scale = (2.0, 0.5, 0.25)
    src = af.export_glb(os.path.join(d, "box.glb"), objects=[cube], animations=False,
                        skins=False)
    res = _run("cleanup", {"mesh": src}, {"rotate_z_deg": 90, "preview": False},
               os.path.join(d, "out"))
    assert res["ok"], res.get("error")
    size = res["stats"]["after"]["size"]
    assert abs(size[0] - 1.0) < 1e-3 and abs(size[1] - 4.0) < 1e-3 and abs(size[2] - 0.5) < 1e-3, size
    assert abs(res["stats"]["after"]["bbox_min"][2]) < 1e-5


def test_cleanup_without_join_splits_budget():
    d = _tmp()
    af.reset_scene()
    a = _cylinder("shaft", radius=0.05, segments=32, rings=60)
    b = _cylinder("guard", radius=0.3, height=0.1, segments=64, rings=6, z0=1.0)
    src = af.export_glb(os.path.join(d, "two.glb"), objects=[a, b], animations=False,
                        skins=False)
    t_in = ml.tris_of(a) + ml.tris_of(b)
    res = _run("cleanup", {"mesh": src}, {"join": False, "target_tris": t_in // 3,
                                          "preview": False}, os.path.join(d, "out"))
    assert res["ok"], (res.get("error"), res["checks"])
    assert res["stats"]["after"]["objects"] == 2, res["stats"]["after"]
    assert res["stats"]["after"]["tris"] <= t_in // 3
    af.reset_scene()
    names = sorted(o.name for o in af.meshes(af.import_any(res["outputs"]["glb"])))
    assert names == ["guard", "shaft"], names


def test_cleanup_voxel_remesh_bakes_colour():
    d = _tmp()
    src = _sphere_glb(os.path.join(d, "sphere.glb"), with_island=False)
    t0 = time.time()
    res = _run("cleanup", {"mesh": src},
               {"height_m": 2.0, "voxel_remesh_m": 0.05, "target_tris": 2500, "bake_size": 128,
                "bake_samples": 2, "preview_size": 192}, os.path.join(d, "out"))
    assert res["ok"], res.get("error")
    assert time.time() - t0 < 120
    s = res["stats"]
    assert s["after"]["tris"] <= 2500 and s["bake"]["miss_fraction"] <= 0.02, s["bake"]
    assert os.path.isfile(res["outputs"]["baked_base_color"])
    # compare colours: original texture at original UVs vs baked texture at new UVs, matched by
    # nearest position — red/blue sides must agree
    af.reset_scene()
    orig = ml.prepare_meshes(af.import_any(src))[0]
    af.normalize([orig], height=2.0)
    co_a, col_a = _loop_colours(orig, ml.image_into(orig.data.materials[0], "Base Color").image)
    objs = ml.prepare_meshes(af.import_any(res["outputs"]["glb"]))
    low = objs[0]
    img = ml.image_into(low.data.materials[0], "Base Color").image
    co_b, col_b = _loop_colours(low, img)
    idx = np.random.default_rng(1).choice(len(co_b), size=min(1500, len(co_b)), replace=False)
    agree = 0
    for i in idx:
        j = int(np.argmin(((co_a - co_b[i]) ** 2).sum(axis=1)))
        agree += int((col_a[j, 0] > col_a[j, 2]) == (col_b[i, 0] > col_b[i, 2]))
    share = agree / float(len(idx))
    assert share > 0.9, share


def test_cleanup_remesh_bake_normal_shading_and_orphans():
    """bake_normal: normals fixed and shading set BEFORE the bake (fully smooth, no sharp
    edges), and clean.blend no longer carries the original map."""
    d = _tmp()
    src = _sphere_glb(os.path.join(d, "sphere.glb"), with_island=False)
    res = _run("cleanup", {"mesh": src},
               {"height_m": 2.0, "voxel_remesh_m": 0.05, "target_tris": 2000, "bake_size": 128,
                "bake_samples": 2, "bake_normal": True, "preview": False}, os.path.join(d, "out"))
    assert res["ok"], (res.get("error"), res["checks"])
    s = res["stats"]
    assert s["shading"] == "smooth (bake_normal)" and s["sharp_edges"] == 0, s["shading"]
    assert s["orphans_purged"] > 0, s["orphans_purged"]
    af.reset_scene()
    objs = af.import_any(res["outputs"]["blend"])
    body = af.meshes(objs)[0]
    smooth = np.zeros(len(body.data.polygons), dtype=bool)
    body.data.polygons.foreach_get("use_smooth", smooth)
    assert smooth.all() and ml.sharp_edge_count(body) == 0
    images = sorted(i.name for i in bpy.data.images)
    assert images and all(n.startswith("baked_") for n in images), images  # "halves" is gone


def test_cleanup_join_false_budgets():
    """join:false: a 12-tri box is left whole when decimating, and after a voxel remesh the
    budget is split over the remeshed counts (the box used to collapse into a tetrahedron)."""
    d = _tmp()
    af.reset_scene()
    ball = _cylinder("ball", radius=0.4, height=1.0, segments=96, rings=80)
    ball.data.materials.append(_textured_material(_half_texture()))
    bpy.ops.mesh.primitive_cube_add(size=0.5, location=(1.2, 0.0, 0.25))
    box = bpy.context.active_object
    box.name = "box"
    shaft = _cylinder("shaft", radius=0.1, height=1.0, segments=48, rings=60)
    shaft.location = (-1.2, 0.0, 0.0)
    src = af.export_glb(os.path.join(d, "three.glb"), objects=[ball, box, shaft],
                        animations=False, skins=False)
    res = _run("cleanup", {"mesh": src}, {"join": False, "target_tris": 3000,
                                          "preview_size": 192}, os.path.join(d, "dec"))
    assert res["ok"], (res.get("error"), res["checks"])
    af.reset_scene()
    tris = {o.name: ml.tris_of(o) for o in af.meshes(af.import_any(res["outputs"]["glb"]))}
    assert tris["box"] == 12 and sum(tris.values()) <= 3000, tris
    res = _run("cleanup", {"mesh": src}, {"join": False, "voxel_remesh_m": 0.04,
                                          "target_tris": 3000, "bake_size": 128,
                                          "bake_samples": 2, "preview_size": 192},
               os.path.join(d, "vox"))
    assert res["ok"], (res.get("error"), res["checks"])
    s = res["stats"]
    assert s["after"]["tris"] <= 3000 and min(s["budgets"]) > 100, s["budgets"]
    assert s["render_compare"]["silhouette_iou"] >= 0.95, s["render_compare"]
    af.reset_scene()
    objs = {o.name: o for o in af.meshes(af.import_any(res["outputs"]["blend"]))}
    vol = ml.signed_volume(objs["box"])
    assert abs(vol - 0.125) < 0.15 * 0.125, vol  # a tetrahedron would keep a third of it


def test_cleanup_vertex_colours():
    """Vertex colours survive: decimation keeps the attribute, a remesh bakes it (through the
    material's Color Attribute node, or a temporary one when the mesh has no material), and the
    previews draw them (VERTEX mode) so render_color_mae can see a loss."""
    d = _tmp()
    af.reset_scene()
    ob = _vertex_colour_sphere()
    src = af.export_glb(os.path.join(d, "vc.glb"), objects=[ob], animations=False, skins=False)
    af.reset_scene()
    objs = af.import_any(src)
    assert ml.preview_color_type(objs) == "VERTEX"
    assert ml.color_source(af.meshes(objs)[0]) == "linked"
    res = _run("cleanup", {"mesh": src}, {"height_m": 2.0, "target_tris": 600,
                                          "preview_size": 160}, os.path.join(d, "dec"))
    assert res["ok"], (res.get("error"), res["checks"])
    assert res["stats"]["render_compare"]["color_mae"] <= 0.05, res["stats"]["render_compare"]
    res = _run("cleanup", {"mesh": src}, {"height_m": 2.0, "voxel_remesh_m": 0.05,
                                          "target_tris": 2000, "bake_size": 128,
                                          "bake_samples": 2, "preview_size": 160},
               os.path.join(d, "vox"))
    assert res["ok"], (res.get("error"), res["checks"])
    assert res["stats"]["bake"]["colour_source"] == "linked", res["stats"]["bake"]
    assert res["stats"]["render_compare"]["color_mae"] <= 0.08, res["stats"]["render_compare"]
    share = _red_blue_agreement(res["outputs"]["glb"])
    assert share > 0.95, share
    # no material at all, colours only in the attribute (.blend input)
    af.reset_scene()
    _vertex_colour_sphere("bare", with_material=False)
    blend = os.path.join(d, "bare.blend")
    bpy.ops.wm.save_as_mainfile(filepath=blend, copy=True)
    res = _run("cleanup", {"blend": blend}, {"height_m": 2.0, "voxel_remesh_m": 0.05,
                                             "target_tris": 2000, "bake_size": 128,
                                             "bake_samples": 2, "preview": False},
               os.path.join(d, "bare"))
    assert res["ok"], (res.get("error"), res["checks"])
    assert res["stats"]["bake"]["colour_source"] == "attribute", res["stats"]["bake"]
    share = _red_blue_agreement(res["outputs"]["glb"])
    assert share > 0.95, share


def test_inputs_blend_only():
    """The pipeline passes only {blend} after a step that wrote a .blend (cleanup -> separate)."""
    d = _tmp()
    src = _sphere_glb(os.path.join(d, "sphere.glb"), with_island=False)
    res = _run("cleanup", {"mesh": src}, {"height_m": 2.0, "target_tris": 1500,
                                          "preview": False}, os.path.join(d, "clean"))
    assert res["ok"], res.get("error")
    blend = res["outputs"]["blend"]
    res = _run("separate", {"blend": blend},
               {"mode": "regions", "regions": [{"name": "top", "min": [-2, -2, 1.2],
                                                "max": [2, 2, 2.1]}], "preview": False},
               os.path.join(d, "sep"))
    assert res["ok"], (res.get("error"), res["checks"])
    assert sorted(p["name"] for p in res["stats"]["parts"]) == ["body", "top"]
    assert res["stats"]["input"] == os.path.abspath(blend)
    res = _run("convert", {"blend": blend}, {"format": "fbx"}, os.path.join(d, "fbx"))
    assert res["ok"], (res.get("error"), res["checks"])
    assert _check(res, "roundtrip_base_color")["ok"] and res["stats"]["roundtrip"]["meshes"] == 1
    res = _run("cleanup", {"blend": blend}, {"target_tris": 800, "preview": False},
               os.path.join(d, "again"))
    assert res["ok"], (res.get("error"), res["checks"])
    assert res["stats"]["after"]["tris"] <= 800
    assert abs(res["stats"]["after"]["size"][2] - 2.0) < 2e-3, res["stats"]["after"]


def test_missing_input_is_reported():
    d = _tmp()
    missing = os.path.join(d, "no_such_model.glb")
    for step in ("cleanup", "convert", "separate"):
        try:
            _run(step, {"mesh": missing}, {}, os.path.join(d, step))
        except FileNotFoundError as exc:
            assert "inputs.mesh not found" in str(exc) and "no_such_model.glb" in str(exc), exc
        else:
            raise AssertionError("%s accepted a missing file" % step)
        try:
            _run(step, {}, {}, os.path.join(d, step + "_none"))
        except ValueError as exc:
            assert "inputs.mesh or inputs.blend" in str(exc), exc
        else:
            raise AssertionError("%s ran without inputs" % step)


def test_rigged_glb_helper_purged():
    d = _tmp()
    glb = _rigged_glb(os.path.join(d, "rigged.glb"), height=1.7)
    _assert_rigged_inputs_clean(glb, 1.7, d)


# --------------------------------------------------------------------------- convert

def test_convert_formats_roundtrip():
    d = _tmp()
    src = _sphere_glb(os.path.join(d, "sphere.glb"), with_island=False)
    for fmt in ("glb", "fbx", "obj"):
        res = _run("convert", {"mesh": src}, {"format": fmt, "height_m": 1.5},
                   os.path.join(d, "out_" + fmt))
        assert res["ok"], (fmt, res.get("error"), res["checks"])
        assert os.path.isfile(res["outputs"][fmt])
        assert _check(res, "roundtrip_base_color")["ok"], fmt
        assert abs(res["stats"]["roundtrip"]["size"][2] - 1.5) < 2e-3, res["stats"]["roundtrip"]


def test_convert_mixamo_fbx():
    d = _tmp()
    src = _sphere_glb(os.path.join(d, "sphere.glb"), segments=96, rings=48)
    res = _run("convert", {"mesh": src}, {"mixamo": True, "mixamo_max_tris": 3000},
               os.path.join(d, "out"))
    assert res["ok"], (res.get("error"), res["checks"])
    rt = res["stats"]["roundtrip"]
    assert rt["tris"] <= 3000 and abs(rt["size"][2] - 1.8) < 2e-3, rt
    assert _check(res, "mixamo_single_mesh")["ok"]
    assert res["stats"]["welded_verts"] > 0


# --------------------------------------------------------------------------- separate

def test_separate_regions_union_and_rest():
    d = _tmp()
    af.reset_scene()
    ob = _cylinder("staff", radius=0.05, height=2.0)
    src = af.export_glb(os.path.join(d, "staff.glb"), objects=[ob], animations=False, skins=False)
    regions = [{"name": "ends", "min": [-0.2, -0.2, -0.1], "max": [0.2, 0.2, 0.6]},
               {"name": "ends", "min": [-0.2, -0.2, 1.4], "max": [0.2, 0.2, 2.1]}]
    res = _run("separate", {"mesh": src}, {"mode": "regions", "regions": regions,
                                           "preview_size": 160}, os.path.join(d, "out"))
    assert res["ok"], (res.get("error"), res["checks"])
    parts = {p["name"]: p for p in res["stats"]["parts"]}
    assert set(parts) == {"ends", "body"}, parts.keys()
    assert parts["ends"]["loose_parts"] == 2 and parts["body"]["loose_parts"] == 1
    assert parts["ends"]["closed"] and parts["body"]["closed"], parts
    total = parts["ends"]["volume_m3"] + parts["body"]["volume_m3"]
    assert abs(parts["body"]["volume_m3"] / total - 0.4) < 0.03, parts
    af.reset_scene()
    objs = af.import_any(res["outputs"]["glb"])
    names = sorted(o.name for o in af.meshes(objs))
    assert names == ["body", "ends"], names


def test_separate_loose_and_material():
    d = _tmp()
    af.reset_scene()
    a = _cylinder("a")
    b = _cylinder("b", z0=3.0, radius=0.2, segments=48)  # more vertices -> listed first
    m1, m2 = bpy.data.materials.new("Wood"), bpy.data.materials.new("Iron")
    a.data.materials.append(m1)
    b.data.materials.append(m2)
    ob = af.join([a, b], name="ab")
    src = af.export_glb(os.path.join(d, "ab.glb"), objects=[ob], animations=False, skins=False)
    res = _run("separate", {"mesh": src}, {"mode": "loose", "names": ["big", "thin"],
                                           "preview": False}, os.path.join(d, "loose"))
    assert res["ok"] and res["stats"]["part_count"] == 2, res["stats"]
    by_name = {p["name"]: p for p in res["stats"]["parts"]}
    assert by_name["big"]["tris"] > by_name["thin"]["tris"]
    assert by_name["big"]["size"][0] > 3 * by_name["thin"]["size"][0]
    res = _run("separate", {"mesh": src}, {"mode": "material", "preview": False},
               os.path.join(d, "mat"))
    names = sorted(p["name"] for p in res["stats"]["parts"])
    assert res["ok"] and names == ["Iron", "Wood"], names


def test_runner_argument_parsing():
    import run_tests
    assert run_tests.parse_args(["blender", "--", "core,items", "-k", "real"]) == \
        (["core", "items"], "real", False)
    assert run_tests.parse_args(["blender", "--", "test_core", "--list"]) == (["core"], None, True)
    assert run_tests.parse_args(["blender"]) == ([], None, False)


# --------------------------------------------------------------------------- real assets


# --------------------------------------------------------------------------- slits, delete, sockets (10.10, v2)

def _dumbbell_glb(path, neck=0.02):
    """Two cubes joined by a thin neck (one watertight mesh by a voxel remesh): a sleeve fused to a
    robe side in miniature. Left cube x -0.5..0, neck x 0..0.06 (+-neck in y/z), right cube 0.06..0.56."""
    af.reset_scene()
    objs = []
    for lo, hi in (((-0.5, -0.25, -0.25), (0.0, 0.25, 0.25)), ((-0.01, -neck, -neck), (0.07, neck, neck)),
                   ((0.06, -0.25, -0.25), (0.56, 0.25, 0.25))):
        me = bpy.data.meshes.new("c")
        bm = bmesh.new()
        bmesh.ops.create_cube(bm, size=1.0)
        bmesh.ops.scale(bm, vec=[hi[k] - lo[k] for k in range(3)], verts=bm.verts)
        bmesh.ops.translate(bm, vec=[(hi[k] + lo[k]) / 2 for k in range(3)], verts=bm.verts)
        bm.to_mesh(me)
        bm.free()
        ob = bpy.data.objects.new("c", me)
        bpy.context.scene.collection.objects.link(ob)
        objs.append(ob)
    ob = af.join(objs, name="db")
    mod = ob.modifiers.new("r", "REMESH")
    mod.mode = "VOXEL"
    mod.voxel_size = 0.008
    af.select_only([ob])
    bpy.ops.object.modifier_apply(modifier=mod.name)
    return af.export_glb(path, objects=[ob], animations=False, skins=False)


def _islands_after_weld(ob, dist=1e-6):
    me = ob.data.copy()
    tmp = bpy.data.objects.new("tmp", me)
    bpy.context.scene.collection.objects.link(tmp)
    ml.merge_by_distance(tmp, dist)
    _, counts = ml.island_labels(tmp.data)
    m = ml.mesh_metrics([tmp])
    bpy.data.objects.remove(tmp, do_unlink=True)
    return len(counts), m


def test_slit_rip_separates_without_caps():
    d = _tmp()
    src = _dumbbell_glb(os.path.join(d, "db.glb"))
    af.reset_scene()
    ob = af.join([o for o in af.import_any(src) if o.type == "MESH"], name="db")
    ml.merge_by_distance(ob, 1e-6)
    n0, m0 = _islands_after_weld(ob)
    assert n0 == 1 and m0["boundary_edges"] == 0, (n0, m0)
    faces0 = len(ob.data.polygons)
    box = ([0.01, -0.1, -0.1], [0.05, 0.1, 0.1])
    info = ml.slit(ob, [box], ml.SlitSurface({"axis": "x", "value": 0.03}), width=0.0, gap=0.004)
    assert info["ripped_edges"] > 8 and info["rim_verts_moved"] > 0, info
    n1, m1 = _islands_after_weld(ob, 1e-4)        # the gap keeps a weld from sewing it back
    assert n1 == 2, n1
    assert m1["boundary_edges"] >= 2 * info["ripped_edges"] - 4, (m1, info)   # open rims, no caps
    assert len(ob.data.polygons) >= faces0      # nothing removed (bisect only adds faces)


def test_slit_slab_caps_each_side_and_curve():
    d = _tmp()
    src = _dumbbell_glb(os.path.join(d, "db.glb"))
    res = _run("separate", {"mesh": src}, {"mode": "slit", "ground": False, "center": False, "preview": False,
                                           "regions": [{"name": "neck", "min": [0.01, -0.1, -0.1],
                                                        "max": [0.05, 0.1, 0.1], "width_m": 0.012,
                                                        "plane": {"axis": "x", "value": 0.03}}]},
               os.path.join(d, "out"))
    assert res["ok"], (res.get("error"), res["checks"])
    reg = res["stats"]["regions"]["slit_neck"]
    s0 = reg["slits"][0]
    assert s0["faces_removed"] > 0 and s0["caps"]["+"] > 0 and s0["caps"]["-"] > 0, s0
    assert _check(res, "slit_neck_cut")["ok"] and _check(res, "slit_neck_capped")["ok"]
    af.reset_scene()
    ob = af.join([o for o in af.import_any(res["outputs"]["glb"]) if o.type == "MESH"], name="db")
    assert [o.name for o in bpy.context.scene.objects if o.type == "MESH"] == ["db"]
    n, m = _islands_after_weld(ob, 1e-5)
    assert n == 2 and m["boundary_edges"] == 0, (n, m)      # two closed pieces, each with its own cap
    # curve: value = f(along) piecewise linear, one plane per band, continuous at the borders
    sf = ml.SlitSurface({"axis": "x", "along": "z", "points": [[0.0, 0.0], [1.0, 0.1], [2.0, 0.1]]})
    assert len(sf.planes) == 2
    assert abs(sf.offset((0.05, 3.0, 0.5))) < 1e-9 and abs(sf.offset((0.1, -1.0, 1.0))) < 1e-9
    assert abs(sf.offset((0.1, 0.0, 1.5))) < 1e-9 and sf.offset((0.2, 0.0, 1.5)) > 0.09


def test_separate_delete_kind_open_or_capped_and_facing():
    d = _tmp()
    af.reset_scene()
    ob = _cylinder("robe", radius=0.3, height=1.5)
    src = af.export_glb(os.path.join(d, "robe.glb"), objects=[ob], animations=False, skins=False)
    region = {"name": "bottom", "kind": "delete", "min": [-0.5, -0.5, -0.01], "max": [0.5, 0.5, 0.04],
              "facing": [0, 0, -1], "facing_min": 0.5}
    res = _run("separate", {"mesh": src}, {"mode": "regions", "preview": False, "ground": False, "center": False,
                                           "regions": [dict(region, caps=False)]}, os.path.join(d, "open"))
    assert res["ok"], res["checks"]
    parts = res["stats"]["parts"]
    assert [p["name"] for p in parts] == ["body"], parts               # deleted, not a part
    info = res["stats"]["regions"]["delete_bottom"]
    assert info["faces_inside"] > 0 and info["rest_cap_faces"] == 0 and info["rest_open_seam_edges"] > 0, info
    assert parts[0]["boundary_edges"] > 0 and abs(parts[0]["bbox_min"][2]) < 1e-4  # the side wall stays
    res = _run("separate", {"mesh": src}, {"mode": "regions", "preview": False, "ground": False, "center": False,
                                           "regions": [dict(region, caps=True)]}, os.path.join(d, "capped"))
    info = res["stats"]["regions"]["delete_bottom"]
    assert info["rest_cap_faces"] > 0 and res["stats"]["parts"][0]["closed"], info


def _socket_scene_glb(path, height=1.0):
    af.reset_scene()
    ob = _cylinder("body", radius=0.2, height=height)
    e = af.add_socket("socket_fx_test", location=(0.1, -0.2, 0.8 * height), rotation=(0.3, 0.0, 0.5), kind="fx",
                      size_class="S")
    return af.export_glb(path, objects=[ob, e], animations=False, skins=False), e.matrix_world.copy()


def _socket_in(glb, name):
    af.reset_scene()
    objs = af.import_any(glb)
    s = [o for o in objs if o.type == "EMPTY" and (o.get("game_socket") == name or o.name == name)]
    return s[0] if s else None


def test_cleanup_and_separate_keep_sockets():
    d = _tmp()
    src, M0 = _socket_scene_glb(os.path.join(d, "s.glb"))
    res = _run("cleanup", {"mesh": src}, {"height_m": 2.0, "preview": False}, os.path.join(d, "clean"))
    assert res["ok"] and res["stats"]["sockets_kept"] == ["socket_fx_test"], res["stats"].get("sockets_kept")
    s = _socket_in(res["outputs"]["glb"], "socket_fx_test")
    assert s is not None and s.get("kind") == "fx" and s.get("size_class") == "S"
    # height 1 -> 2 (x2 about the base centre), feet on z = 0: the socket follows, unit scale
    loc = s.matrix_world.translation.copy()   # a copy: the scene is reset below
    assert (loc - mathutils.Vector((0.2, -0.4, 1.6))).length < 2e-3, loc
    assert abs(s.matrix_world.to_scale().x - 1.0) < 1e-4
    ang = (s.matrix_world.to_quaternion().rotation_difference(M0.to_quaternion())).angle
    assert ang < 1e-3, ang
    res = _run("separate", {"mesh": res["outputs"]["glb"]},
               {"mode": "regions", "preview": False, "regions": [{"name": "top", "min": [-1, -1, 1.5],
                                                                   "max": [1, 1, 2.1]}]}, os.path.join(d, "sep"))
    assert res["ok"] and res["stats"]["sockets_kept"] == ["socket_fx_test"]
    s = _socket_in(res["outputs"]["glb"], "socket_fx_test")
    assert s is not None and (s.matrix_world.translation - loc).length < 2e-3
