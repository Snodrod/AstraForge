"""Self-tests of the export module: render_sprites and godot_package (docs/forge/export.md).

Synthetic tests build their models in the test. Godot checks are skipped when Godot 4.5.1 is absent
(env ASTRA_GODOT).
Temporary files only under tempfile.mkdtemp().
"""
import atexit
import json
import math
import os
import shutil
import tempfile
import time

import bpy
import numpy as np
from mathutils import Matrix

import af_common as af
from steps import _export_lib as xl
from steps import godot_package
from steps import render_sprites

_CACHE = {}


def _tmp(prefix):
    """tempfile.mkdtemp(); passing tests delete it with _done() (the shared runner ends with
    os._exit, so atexit is only a fallback for other runners). AF_KEEP_TEST_FILES=1 keeps files."""
    d = tempfile.mkdtemp(prefix=prefix)
    if not os.environ.get("AF_KEEP_TEST_FILES"):
        atexit.register(shutil.rmtree, d, True)
    return d


def _done(*dirs):
    if not os.environ.get("AF_KEEP_TEST_FILES"):
        for d in dirs:
            shutil.rmtree(d, ignore_errors=True)


def _req(step, inputs, params, out_dir):
    return {"step": step, "inputs": inputs, "params": params, "out_dir": out_dir}


def _errors(res):
    return [c["name"] for c in res["checks"] if not c["ok"] and c.get("severity", "error") == "error"]


def _check(res, name):
    return next(c for c in res["checks"] if c["name"] == name)


def _material(name, color, emission=0.0):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    bsdf = m.node_tree.nodes.get("Principled BSDF")
    bsdf.inputs["Base Color"].default_value = (*color, 1.0)
    bsdf.inputs["Roughness"].default_value = 0.7
    if emission:
        bsdf.inputs["Emission Color"].default_value = (*color, 1.0)
        bsdf.inputs["Emission Strength"].default_value = emission
    return m


def _mesh_obj(name, op, mat, **kw):
    op(**kw)
    ob = bpy.context.active_object
    ob.name = name
    ob.data.materials.append(mat)
    return ob


# --------------------------------------------------------------------------- builders

def build_marker_figure(path, scale=1.0):
    """Static figure facing -Y (contract §4): grey column + head + red nose on the -Y side (1.7 m x scale)."""
    af.reset_scene()
    grey = _material("grey", (0.6, 0.6, 0.6))
    red = _material("red", (1.0, 0.02, 0.02), emission=3.0)
    body = _mesh_obj("body", bpy.ops.mesh.primitive_cylinder_add, grey, radius=0.2, depth=1.4,
                     location=(0, 0, 0.7), vertices=32)
    head = _mesh_obj("head", bpy.ops.mesh.primitive_uv_sphere_add, grey, radius=0.15, location=(0, 0, 1.55))
    nose = _mesh_obj("nose", bpy.ops.mesh.primitive_cube_add, red, size=0.1, location=(0, -0.17, 1.55))
    if scale != 1.0:
        for o in (body, head, nose):
            o.location = o.location * scale
            o.scale = (scale, scale, scale)
        af.apply_transforms([body, head, nose])
    af.export_glb(path, objects=[body, head, nose], animations=False, skins=False)
    return path


def build_ship(path, k=1.0, size_class="S", diameter=True, scaled_parent=False, tilt_deg=0.0,
               scaled_thruster=False):
    """Procedural ship by the ship contract: nose +Y, starboard +X, thrusters aft; k=1 is the 5.85 m
    test ship with 0.44 m nozzles. socket_thruster_main_l/r: local +Z = 'out' = aft (0,-1,0), local +Y = up
    - the legacy frame, so no extras.frame; they carry size_class and, with
    diameter=True, mount_d_m (nozzle diameter). tilt_deg cants both nozzles downwards by that angle;
    scaled_thruster (needs scaled_parent) adds socket_thruster_scaled under the 0.01 holder in the 'thrust' frame
    (extras.frame, identity rotation: exhaust along glTF local +Z)."""
    af.reset_scene()
    armor = _material("AF_Armor", (0.75, 0.7, 0.62))
    drive = _material("AF_EmissiveDrive", (0.3, 0.6, 1.0), emission=4.0)
    hull = _mesh_obj("M_hull_armor", bpy.ops.mesh.primitive_cube_add, armor, size=1.0 * k, location=(0, 0, 0))
    hull.scale = (1.2, 4.0, 0.7)
    nose = _mesh_obj("M_nose_armor", bpy.ops.mesh.primitive_cone_add, armor, radius1=0.6 * k, depth=1.4 * k,
                     location=(0, 2.7 * k, 0), rotation=(-math.pi / 2, 0, 0), vertices=16)
    parts = [hull, nose]
    for side, x in (("l", -0.4), ("r", 0.4)):
        noz = _mesh_obj("M_nozzle_%s" % side, bpy.ops.mesh.primitive_cylinder_add, drive, radius=0.22 * k,
                        depth=0.5 * k, location=(x * k, -2.2 * k, 0), rotation=(math.pi / 2, 0, 0), vertices=16)
        parts.append(noz)
    af.apply_transforms(parts)
    extra = {"mount_d_m": round(0.44 * k, 4)} if diameter else {}
    socks = [af.add_socket("socket_thruster_main_%s" % s, location=(x * k, -2.46 * k, 0.0),
                           rotation=(math.pi / 2 + math.radians(tilt_deg), 0, 0), kind="thruster",
                           size_class=size_class, **extra)
             for s, x in (("l", -0.4), ("r", 0.4))]
    socks.append(af.add_socket("socket_fx_nav_port", location=(-0.62 * k, 0.0, 0.2 * k),
                               rotation=(0, -math.pi / 2, 0), kind="fx", fx="nav_light"))
    if scaled_parent:
        # a socket under a 0.01-scaled node (cm FBX style): its FX must still come out in metres
        holder = bpy.data.objects.new("cm_holder", None)
        bpy.context.scene.collection.objects.link(holder)
        holder.location = (0.0, 1.0 * k, 0.35 * k)
        holder.scale = (0.01, 0.01, 0.01)
        socks += [holder, af.add_socket("socket_fx_scaled", parent=holder, kind="fx")]
        if scaled_thruster:
            socks.append(af.add_socket("socket_thruster_scaled", parent=holder, kind="thruster",
                                       size_class=size_class, frame="thrust", **extra))
    af.export_glb(path, objects=parts + socks, animations=False, skins=False)
    return path


def _appdata_godot_snapshot():
    """Files in the real %APPDATA%/Godot/app_userdata/af_godot_check (must not grow: contract §3)."""
    root = os.path.join(os.environ.get("APPDATA", ""), "Godot", "app_userdata", "af_godot_check")
    out = {}
    for dp, _dn, fn in os.walk(root):
        for f in fn:
            fp = os.path.join(dp, f)
            out[fp] = os.path.getmtime(fp)
    return out


# --------------------------------------------------------------------------- unit tests

def test_sample_times_and_clip_names():
    assert xl.sample_times(0, 12, 12, True) == [float(i) for i in range(12)]
    t = xl.sample_times(4, 84, 12, False)
    assert len(t) == 12 and t[0] == 4 and abs(t[-1] - 84) < 1e-9
    assert xl.clip_base("idle-loop") == ("idle", True)
    assert xl.clip_base("Walk_loop") == ("Walk", True)
    assert xl.clip_base("attack") == ("attack", False)
    assert dict(xl.ISO8)["S"] == 0.0 and dict(xl.ISO8)["E"] == 90.0


def test_godot_import_names_and_glb_rename():
    """Godot 4.5's scene importer strips loop tokens (loop_mode, loop, cycle) one stage at a time but renames
    from the ORIGINAL name, so a second matching token fails with 'Animation not found' (turret, smoke 10.10)."""
    assert xl.godot_import_name("idle-loop") == ("idle", True, False)
    assert xl.godot_import_name("fire_recoil") == ("fire_recoil", False, False)
    assert xl.godot_import_name("Walk_Cycle") == ("Walk", True, False)
    assert xl.godot_import_name("idle_loop-loop") == ("idle_loop", True, False)
    assert xl.godot_import_name("pitch_cycle-loop") == ("pitch_cycle", True, True)
    assert xl.godot_import_name("walk_cycle_loop") == ("walk_cycle", True, True)
    assert xl.godot_import_name("idle-loop.001") == ("idle.001", True, False)
    for name in ("idle-loop", "fire_recoil", "yaw_sweep-loop"):
        assert xl.godot_safe_clip_name(name) == name
    for name, want in (("pitch_cycle-loop", "pitch_cycle"), ("walk_cycle_loop", "walk_cycle")):
        safe = xl.godot_safe_clip_name(name)
        assert safe != name and xl.godot_import_name(safe) == (want, True, False), (name, safe)
        assert xl.clip_base(name)[0] == want
    d = _tmp("af_export_rename_")
    try:
        bpy.ops.mesh.primitive_cube_add()
        ob = bpy.context.active_object
        ob.animation_data_create()
        for nm in ("pitch_cycle-loop", "fire_recoil"):     # NLA tracks named like the clips (turret_gen)
            ob.animation_data.action = None
            ob.location.x = 0.0
            ob.keyframe_insert("location", index=0, frame=1)
            ob.location.x = 1.0
            ob.keyframe_insert("location", index=0, frame=10)
            act = ob.animation_data.action
            tr = ob.animation_data.nla_tracks.new()
            tr.name = nm
            tr.strips.new(nm, 1, act)
            ob.animation_data.action = None
        src = af.export_glb(os.path.join(d, "a.glb"), objects=[ob], animation_mode="NLA_TRACKS")
        dst = os.path.join(d, "b.glb")
        assert xl.rename_glb_animations(src, dst, {"pitch_cycle-loop": "pitch_cycle-cycle"}) == ["pitch_cycle-loop"]
        with open(dst, "rb") as fh:
            raw = fh.read()
        import struct
        n = struct.unpack_from("<I", raw, 12)[0]
        doc = json.loads(raw[20:20 + n].decode("utf-8"))
        assert sorted(a["name"] for a in doc["animations"]) == ["fire_recoil", "pitch_cycle-cycle"]
        assert struct.unpack_from("<I", raw, 8)[0] == len(raw) and n % 4 == 0
        af.reset_scene()
        assert af.import_any(dst), "the renamed GLB still imports"
        assert any(a.name.startswith("pitch_cycle-cycle") for a in bpy.data.actions), [a.name for a in bpy.data.actions]
    finally:
        _done(d)


def test_png_roundtrip_and_postprocess():
    d = _tmp("af_export_png_")
    rng = np.random.default_rng(1)
    img = rng.integers(0, 256, size=(37, 53, 4), dtype=np.uint8)
    p = xl.save_png(os.path.join(d, "a.png"), img)
    back = xl.load_png(p)
    assert back.shape == img.shape and np.array_equal(back, img), "PNG round trip must be lossless"
    a = np.zeros((20, 20, 4), np.uint8)
    a[5, 5] = (200, 200, 200, 3)          # below the alpha floor -> zeroed
    a[10, 10] = (200, 200, 200, 20)       # isolated weak speck -> zeroed
    a[2:6, 12:16] = (180, 90, 40, 255)    # solid body stays
    c = xl.clean_alpha(a)
    assert c[5, 5, 3] == 0 and c[10, 10, 3] == 0 and c[3, 13, 3] == 255
    g = xl.grade(a)
    assert np.array_equal(g[..., 3], a[..., 3]), "grade must not touch alpha"
    sup = xl.pixel_support(c)
    assert sup["body_bounds_px"] == [12, 2, 15, 5] and sup["body_border_2px_pixels"] == 0
    _done(d)


def test_spriteframes_and_fx_text():
    d = _tmp("af_export_tres_")
    path = xl.write_spriteframes(os.path.join(d, "x_frames.tres"), {"idle.png": "idle.png"},
                                 [{"name": "idle_S", "loop": True, "speed": 12,
                                   "frames": [("idle.png", 0, 0, 64, 64), ("idle.png", 64, 0, 64, 64)]}])
    txt = open(path, encoding="utf-8").read()
    assert txt.startswith('[gd_resource type="SpriteFrames"')
    assert 'region = Rect2(64, 0, 64, 64)' in txt and '"name": &"idle_S"' in txt and '"speed": 12.0' in txt
    presets = json.load(open(xl.FX_PRESETS_PATH, encoding="utf-8"))["presets"]
    need = {"engine_trail", "thruster_glow", "aura", "magic_cast", "fire", "smoke", "sparks", "ice", "poison",
            "heal", "muzzle_flash", "nav_light"}
    assert need <= set(presets), need - set(presets)
    for name, pr in presets.items():
        s = xl.fx_scene_text(name, xl.merge_fx(pr, {"scale": 2.0}), "textures/%s.png" % pr["texture"])
        assert 'type="GPUParticles3D"' in s and 'type="ParticleProcessMaterial"' in s and 'type="QuadMesh"' in s
        xl.make_fx_texture(pr["texture"], 16)
    m = xl.merge_fx(presets["engine_trail"], {"scale": 2.0, "color": [1, 0, 0]})
    assert abs(m["quad_size"] - 2.0 * presets["engine_trail"]["quad_size"]) < 1e-9
    assert m["color_ramp"][0][1][1] == 0 and m["velocity"][1] == 2.0 * presets["engine_trail"]["velocity"][1]
    _done(d)


def test_iso8_directions_scale_and_lighting():
    """Nose on the -Y face must appear centred in S, right in E, left in W and hidden in N;
    a rotationally symmetric body must be lit identically in S and N (camera-parented light rig)."""
    d = _tmp("af_export_iso8_")
    glb = build_marker_figure(os.path.join(d, "figure.glb"))
    af.reset_scene()
    res = render_sprites.run(_req("render_sprites", {"mesh": glb},
                                  {"profile": "iso8", "asset_id": "marker", "pixels_per_meter": 50,
                                   "validate_godot": False}, os.path.join(d, "out")))
    assert not _errors(res), _errors(res)
    doc = json.load(open(res["outputs"]["sprites_json"], encoding="utf-8"))
    cw, ch = doc["cell"]
    ax, ay = doc["anchor"]
    sheet = xl.load_png(res["outputs"]["sheets"]["static.png"])
    assert sheet.shape[0] == 8 * ch and sheet.shape[1] == cw
    views = doc["clips"]["static"]["views"]
    dx, lum = {}, {}
    for name in ("S", "SW", "W", "NW", "N", "NE", "E", "SE"):
        x, y, w, h = views[name]["rects"][0]
        cell = sheet[y:y + h, x:x + w]
        red = (cell[..., 0] > 150) & (cell[..., 1] < 90) & (cell[..., 2] < 90) & (cell[..., 3] > 200)
        dx[name] = float(np.nonzero(red)[1].mean() - ax) if red.sum() > 3 else None
        grey = (cell[..., 3] > 250) & (np.abs(cell[..., 0].astype(int) - cell[..., 2].astype(int)) < 30)
        lum[name] = float(cell[..., :3][grey].mean())
    assert dx["S"] is not None and abs(dx["S"]) <= 2.0, dx
    assert dx["E"] is not None and dx["E"] > 5 and dx["W"] is not None and dx["W"] < -5, dx
    assert dx["N"] is None, dx
    assert dx["SE"] > 2 and dx["SW"] < -2, dx
    assert abs(lum["S"] - lum["N"]) / lum["S"] < 0.05 and abs(lum["E"] - lum["W"]) / lum["E"] < 0.08, lum
    # fixed scale: 1.7 m figure at 50 px/m, 30 deg -> about 1.7*cos30*50 = 74 px plus depth
    body = xl.pixel_support(sheet[0:ch, 0:cw])["body_bounds_px"]
    height_px = body[3] - body[1]
    assert 70 <= height_px <= 84, height_px
    assert abs(body[3] - ay) <= 6, (body, ay)     # feet on the anchor
    _done(d)


def _sliding_rig(blend_path):
    """Armature with a single 'root' bone sliding 2 m along +X over frames 1..25, a cube rides it."""
    af.reset_scene()
    arm_data = bpy.data.armatures.new("rig")
    arm = bpy.data.objects.new("rig", arm_data)
    bpy.context.scene.collection.objects.link(arm)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    b = arm_data.edit_bones.new("root")
    b.head, b.tail = (0, 0, 0), (0, 0, 1)
    bpy.ops.object.mode_set(mode="OBJECT")
    cube = _mesh_obj("block", bpy.ops.mesh.primitive_cube_add, _material("grey", (0.6, 0.6, 0.6)), size=0.5,
                     location=(0, 0, 0.25))
    mw = cube.matrix_world.copy()
    cube.parent = arm
    cube.parent_type = "BONE"
    cube.parent_bone = "root"
    cube.matrix_world = mw
    act = bpy.data.actions.new("slide-loop")
    xl.assign_action(arm, act)
    pb = arm.pose.bones["root"]
    for f, x in ((1, 0.0), (25, 2.0)):
        pb.location = (x, 0.0, 0.0)          # bone local X = world X for an upright bone
        pb.keyframe_insert("location", frame=f)
    if arm.animation_data.action_slot is None and len(act.slots):
        arm.animation_data.action_slot = act.slots[0]
    af.save_blend(blend_path)
    return blend_path


def test_in_place_lock_vs_none():
    d = _tmp("af_export_lock_")
    blend = _sliding_rig(os.path.join(d, "slide.blend"))
    roots = {}
    for mode in ("lock", "none"):
        af.open_blend(blend)
        res = render_sprites.run(_req("render_sprites", {"blend": blend},
                                      {"profile": "iso8", "asset_id": "slide", "directions": ["S"], "frames": 4,
                                       "pixels_per_meter": 40, "in_place": mode, "loop": {"slide": False},
                                       "center": "none", "validate_godot": False}, os.path.join(d, mode)))
        assert not _errors(res), (mode, _errors(res))
        doc = json.load(open(res["outputs"]["sprites_json"], encoding="utf-8"))
        assert doc["clips"]["slide"]["in_place"] == mode and doc["clips"]["slide"]["root_bone"] == "root"
        roots[mode] = [r[0] for r in doc["clips"]["slide"]["views"]["S"]["root_px"]]
    assert max(roots["lock"]) - min(roots["lock"]) < 0.6, roots       # hips fixed horizontally
    assert abs((roots["none"][-1] - roots["none"][0]) - 80.0) < 1.0, roots   # 2 m x 40 px/m
    _done(d)


def test_sheet_layout_limits():
    # 48 frames of 224x200 -> balanced 2 rows of 24, all 8 directions in one 5376x3200 sheet
    lay = xl.sheet_layout(48, 224, 200, 8)
    assert lay == {"cols": 24, "rows": 2, "layout": "stacked", "sheet": [5376, 3200], "over_soft": False}, lay
    # big creature: 800x700 cells -> 10 columns, per-direction sheets 8000x3500
    lay = xl.sheet_layout(48, 800, 700, 8)
    assert lay["layout"] == "per_view" and lay["sheet"] == [8000, 3500] and not lay["over_soft"], lay
    # per_view=True: always one sheet per view
    assert xl.sheet_layout(12, 320, 320, 3, cols=4, per_view=True)["sheet"] == [1280, 960]
    # over the soft limit but under Godot's: allowed with over_soft; beyond 16384 px: error
    lay = xl.sheet_layout(24, 2048, 2048, 8)
    assert lay["over_soft"] and lay["sheet"] == [8192, 12288] and lay["layout"] == "per_view", lay
    try:
        xl.sheet_layout(48, 2048, 2048, 8, cols=4)
    except ValueError as exc:
        assert "16384" in str(exc)
    else:
        raise AssertionError("a 8192x24576 sheet must be rejected")


def test_render_size_guards():
    """Wrong units fail fast (before any rendering) instead of a 1 px blob or a 17000 px frame; an odd but
    plausible size renders with a warning that suggests height_m."""
    d = _tmp("af_export_size_")
    tiny = build_marker_figure(os.path.join(d, "tiny.glb"), scale=0.01)        # 1.7 cm
    huge = build_marker_figure(os.path.join(d, "huge.glb"), scale=100.0)       # 170 m (cm FBX read as m)
    tall = build_marker_figure(os.path.join(d, "tall.glb"), scale=10.0)        # 17 m
    for glb, words in ((tiny, "min_body_px"), (huge, "max_cell_px")):
        af.reset_scene()
        t0 = time.time()
        try:
            render_sprites.run(_req("render_sprites", {"mesh": glb}, {"asset_id": "marker", "validate_godot": False},
                                    os.path.join(d, "o_" + words)))
        except ValueError as exc:
            assert words in str(exc) and "height_m" in str(exc), str(exc)
        else:
            raise AssertionError("%s must be rejected" % glb)
        assert time.time() - t0 < 20, "the guard must fire before rendering"
        assert not os.path.isdir(os.path.join(d, "o_" + words, "_frames")), "nothing may be rendered"
    af.reset_scene()
    res = render_sprites.run(_req("render_sprites", {"mesh": tall},
                                  {"asset_id": "marker", "directions": ["S"], "pixels_per_meter": 10,
                                   "validate_godot": False}, os.path.join(d, "o_tall")))
    assert not _errors(res), _errors(res)
    assert any("height_m" in w and "17" in w for w in res["warnings"]), res["warnings"]
    _done(d)


def test_forward_plus_y_and_default_asset_id():
    """forward '+Y' (ships/props) turns every view by 180 deg: the -Y nose of the marker is hidden in S and
    visible in N. A file name starting with a digit becomes a valid asset id with a warning."""
    d = _tmp("af_export_fwd_")
    glb = build_marker_figure(os.path.join(d, "01_marker.glb"))
    af.reset_scene()
    res = render_sprites.run(_req("render_sprites", {"mesh": glb},
                                  {"directions": ["S", "N"], "pixels_per_meter": 50, "forward": "+Y",
                                   "validate_godot": False}, os.path.join(d, "out")))
    assert not _errors(res), _errors(res)
    assert res["stats"]["asset_id"] == "a_01_marker", res["stats"]["asset_id"]
    assert any("a_01_marker" in w for w in res["warnings"]), res["warnings"]
    doc = json.load(open(res["outputs"]["sprites_json"], encoding="utf-8"))
    assert doc["forward"] == "+Y" and doc["views"] == [{"name": "S", "model_yaw_deg": 180.0},
                                                       {"name": "N", "model_yaw_deg": 0.0}], doc["views"]
    sheet = xl.load_png(res["outputs"]["sheets"]["static.png"])
    reds = {}
    for name in ("S", "N"):
        x, y, w, h = doc["clips"]["static"]["views"][name]["rects"][0]
        cell = sheet[y:y + h, x:x + w]
        reds[name] = int(((cell[..., 0] > 150) & (cell[..., 1] < 90) & (cell[..., 3] > 200)).sum())
    assert reds["S"] <= 3 and reds["N"] > 10, reds
    _done(d)


def test_shadow_footprint_in_cell():
    """iso8/custom with the Cycles shadow catcher: the auto cell includes the projected shadow, so no shadow
    pixel is cut by the cell border."""
    d = _tmp("af_export_shadow_")
    glb = build_marker_figure(os.path.join(d, "figure.glb"))
    af.reset_scene()
    res = render_sprites.run(_req("render_sprites", {"mesh": glb},
                                  {"profile": "custom", "asset_id": "marker", "directions": [["front", 0], ["side", -60]],
                                   "engine": "CYCLES", "samples": 8, "shadow": True, "pixels_per_meter": 40,
                                   "validate_godot": False}, os.path.join(d, "out")))
    assert not _errors(res), _errors(res)
    doc = json.load(open(res["outputs"]["sprites_json"], encoding="utf-8"))
    assert doc["shadow"] is True
    sheet = xl.load_png(res["outputs"]["sheets"]["static.png"])
    shadow_px = 0
    for v in doc["clips"]["static"]["views"].values():
        x, y, w, h = v["rects"][0]
        cell = sheet[y:y + h, x:x + w]
        a = cell[..., 3]
        border = np.concatenate([a[:2].ravel(), a[-2:].ravel(), a[:, :2].ravel(), a[:, -2:].ravel()])
        assert border.max() == 0, ("shadow cut by the cell border", int(border.max()))
        shadow_px += int(((a > 0) & (cell[..., :3].max(axis=2) < 40)).sum())
    assert shadow_px > 200, shadow_px          # there is a shadow to protect
    _done(d)


def test_package_rejects_unknown_preset_and_missing_socket():
    d = _tmp("af_export_bad_")
    glb = build_ship(os.path.join(d, "ship.glb"))
    af.reset_scene()
    res = godot_package.run(_req("godot_package", {"glb": glb},
                                 {"asset_id": "ship_bad_001", "profile": "godot", "validate": False,
                                  "fx": [{"preset": "laser_disco", "socket": "socket_thruster_main_l"},
                                         {"preset": "smoke", "socket": "socket_does_not_exist"}]},
                                 os.path.join(d, "out")))
    errs = _errors(res)
    assert "fx_presets_known" in errs and "fx_sockets_exist" in errs, errs
    try:
        godot_package.run(_req("godot_package", {"glb": glb}, {"asset_id": "Bad-Id"}, os.path.join(d, "o2")))
    except ValueError:
        pass
    else:
        raise AssertionError("non snake_case asset_id must be rejected")
    _done(d)


def test_ship_package_engine_trail():
    d = _tmp("af_export_ship_")
    glb = build_ship(os.path.join(d, "ship.glb"), scaled_parent=True)
    af.reset_scene()
    fx = [{"preset": "engine_trail", "socket": "socket_thruster_main_l"},
          {"preset": "engine_trail", "socket": "socket_thruster_main_r"},
          {"preset": "nav_light", "socket": "socket_fx_nav_port", "params": {"color": [0.15, 6, 1.5]}},
          {"preset": "smoke", "socket": "socket_fx_scaled"}]
    before = _appdata_godot_snapshot()
    res = godot_package.run(_req("godot_package", {"glb": glb},
                                 {"asset_id": "ship_af_test_001", "profile": "godot", "kind": "ship",
                                  "fx": fx}, os.path.join(d, "out")))
    assert not _errors(res), (_errors(res), res["checks"])
    assert not any("source_forward" in w for w in res["warnings"]), res["warnings"]   # kind given, not guessed
    pkg = res["outputs"]["package"]
    for f in ("ship_af_test_001.glb", "ship_af_test_001.tscn", "af_fx_mount.gd", "README.md",
              "fx/engine_trail.tscn", "fx/nav_light.tscn",
              "fx/textures/soft.png"):
        assert os.path.isfile(os.path.join(pkg, f)), f
    man = json.load(open(os.path.join(pkg, "af_package.json"), encoding="utf-8"))
    k = {b["node_name"] + "@" + b["socket"]: (b["scale"], b["scale_source"]) for b in man["fx"]}
    # 0.44 m nozzles (mount_d_m) on the 5.85 m test ship: presets stay at their native size
    assert k["fx_engine_trail@socket_thruster_main_l"][0] == 0.978, k
    assert k["fx_nav_light@socket_fx_nav_port"][0] == 0.975 and "longest" in k["fx_nav_light@socket_fx_nav_port"][1], k
    assert _check(res, "fx_scale_vs_asset")["ok"], _check(res, "fx_scale_vs_asset")
    if xl.find_godot() is None:
        _done(d)
        return
    g = _check(res, "godot_validation")["value"]
    sc = g["scenes"][0]
    assert sc["loaded"] and sc["instanced"], sc
    assert all(f["ok"] for f in sc["fx"]) and len(sc["fx"]) == 4, sc["fx"]
    # FX in metres of the asset, also under the 0.01-scaled holder (af_fx_mount undoes the socket's scale)
    assert all(abs(f["scale_rel"] - 1.0) < 0.01 for f in sc["fx"]), sc["fx"]
    tl = sc["sockets"]["socket_thruster_main_l"]
    assert tl["found"] and tl["origin"][0] < 0, tl          # port = -X
    assert tl["axis_y"][2] > 0.99, tl                       # exhaust (+Y local) points aft = +Z in Godot
    assert all(r["ok"] for r in g["fx_scenes"]), g["fx_scenes"]
    # Godot ran with APPDATA/LOCALAPPDATA inside out_dir: nothing new in the real user dir (contract §3)
    assert _appdata_godot_snapshot() == before, "Godot wrote into %APPDATA%/Godot/app_userdata/af_godot_check"
    home = os.path.join(d, "out", "_godot_check", xl.GODOT_HOME)
    assert os.path.isdir(os.path.join(home, "Roaming", "Godot")), os.listdir(os.path.join(home, "Roaming"))
    logs = [f for dp, _dn, fn in os.walk(home) for f in fn if f.endswith(".log")]
    assert not logs, logs                                   # file logging disabled in project.godot
    _done(d)


def test_big_ship_fx_auto_scale():
    """A 60 m ship with size_class L thrusters and no explicit FX scale: engine_trail scales by the seat
    (L 4.5 m / 0.45 m = 10), nav_light by the hull (60 m / 6 m = 10), visibility AABB with them; an explicit
    scale 1 on the same ship is flagged by fx_scale_vs_asset."""
    d = _tmp("af_export_bigship_")
    k_hull = 60.0 / 5.85
    glb = build_ship(os.path.join(d, "big.glb"), k=k_hull, size_class="L", diameter=False)
    af.reset_scene()
    fx = [{"preset": "engine_trail", "socket": "socket_thruster_main_l"},
          {"preset": "engine_trail", "socket": "socket_thruster_main_r"},
          {"preset": "nav_light", "socket": "socket_fx_nav_port"}]
    res = godot_package.run(_req("godot_package", {"glb": glb},
                                 {"asset_id": "ship_big_test", "profile": "godot", "fx": fx},
                                 os.path.join(d, "out")))
    assert not _errors(res), (_errors(res), res["checks"])
    # profile godot without kind/manifest: ship sockets make the GLB forward -Z, with a warning
    assert res["stats"]["glb_forward"] == "-Z", res["stats"]
    assert any("source_forward -Z guessed from ship sockets" in w for w in res["warnings"]), res["warnings"]
    pkg = res["outputs"]["package"]
    man = json.load(open(os.path.join(pkg, "af_package.json"), encoding="utf-8"))
    assert abs(man["longest_m"] - 60.0) < 0.05, man["longest_m"]
    by = {b["node_name"] + "@" + b["socket"]: b for b in man["fx"]}
    et = by["fx_engine_trail@socket_thruster_main_l"]
    assert et["scale"] == 10.0 and et["scale_source"].startswith("size_class L"), et
    assert abs(et["quad_size_m"] - 9.5) < 1e-6 and et["visibility_aabb"] == [-80, -20, -80, 160, 160, 160], et
    nav = by["fx_nav_light@socket_fx_nav_port"]
    assert nav["scale"] == 10.0 and "longest" in nav["scale_source"], nav
    txt = open(os.path.join(pkg, "fx", "engine_trail.tscn"), encoding="utf-8").read()
    assert "visibility_aabb = AABB(-80, -20, -80, 160, 160, 160)" in txt and "size = Vector2(9.5, 9.5)" in txt
    assert "damping_min = 5" in txt, "damping must scale with the effect"
    assert _check(res, "fx_scale_vs_asset")["ok"], _check(res, "fx_scale_vs_asset")
    readme = open(os.path.join(pkg, "README.md"), encoding="utf-8").read()
    assert "k = 10" in readme and "size_class L" in readme
    if xl.find_godot() is not None:
        sc = _check(res, "godot_validation")["value"]["scenes"][0]
        fxr = {f["name"] + "@" + f["socket"]: f for f in sc["fx"]}
        g_et = fxr["fx_engine_trail@socket_thruster_main_l"]
        assert g_et["ok"] and abs(g_et["scale_rel"] - 1.0) < 0.01, g_et
        assert [round(v) for v in g_et["visibility_aabb"]] == [-80, -20, -80, 160, 160, 160], g_et
        assert abs(g_et["quad_size"] - 9.5) < 1e-3, g_et
    # explicit scale 1 on the 60 m hull: the 0.95 m trail is flagged (warning only)
    af.reset_scene()
    res1 = godot_package.run(_req("godot_package", {"glb": glb},
                                  {"asset_id": "ship_big_test", "profile": "godot", "kind": "ship", "validate": False,
                                   "fx": [{"preset": "engine_trail", "socket": "socket_thruster_main_l",
                                           "params": {"scale": 1}}]}, os.path.join(d, "out1")))
    c = _check(res1, "fx_scale_vs_asset")
    assert not c["ok"] and c["severity"] == "warning" and not _errors(res1), (c, _errors(res1))
    _done(d)


def _glb_with_pivot_sockets(src, dst):
    """Copy of a turret GLB whose pivots still carry extras.game_socket (rigs from before 10.10)."""
    import struct
    raw = open(src, "rb").read()
    n = struct.unpack_from("<I", raw, 12)[0]
    doc = json.loads(raw[20:20 + n])
    for node in doc["nodes"]:
        if node.get("name", "").startswith("pivot_"):
            node.setdefault("extras", {})["game_socket"] = node["name"]
    js = json.dumps(doc, separators=(",", ":")).encode()
    js += b" " * ((4 - len(js) % 4) % 4)
    rest = raw[20 + n:]
    with open(dst, "wb") as fh:
        fh.write(struct.pack("<4sII", b"glTF", 2, 20 + len(js) + len(rest)) + struct.pack("<I4s", len(js), b"JSON")
                 + js + rest)
    return dst


def test_turret_package_pivots_are_not_sockets():
    """turret_gen -> godot_package: pivot_yaw / pivot_pitch are listed as rig pivots (axis,
    range_degrees), never as sockets - the snake_case socket check passes and Godot finds exactly
    socket_mount + socket_muzzle_*; a legacy GLB with game_socket on the pivots passes the same way with a
    warning. A ship_gen thruster's mount_d_m drives the engine_trail scale (contract 6)."""
    from steps import ship_gen, turret_gen
    d = _tmp("af_export_turret_")
    req = _req("turret_gen", {}, {"size": "M", "barrels": 2, "seed": 3, "preview": False,
                                  "lods": False}, os.path.join(d, "turret"))
    tres = af.finish(req, turret_gen.run(req))
    assert tres["ok"], _errors(tres)
    glb = tres["outputs"]["glb"]
    af.reset_scene()
    res = godot_package.run(_req("godot_package", {"glb": glb},
                                 {"asset_id": "turret_af_test_001", "profile": "godot", "kind": "turret",
                                  "fx": [{"preset": "muzzle_flash", "socket": "socket_muzzle_01"}]},
                                 os.path.join(d, "out")))
    assert not _errors(res), (_errors(res), res["checks"])
    assert _check(res, "socket_names_snake_ascii")["ok"] and _check(res, "rig_pivots")["ok"]
    assert res["stats"]["sockets"] == 3 and res["stats"]["pivots"] == 2, res["stats"]
    man = json.load(open(res["outputs"]["manifest"], encoding="utf-8"))
    assert sorted(s["name"] for s in man["sockets"]) == ["socket_mount", "socket_muzzle_01", "socket_muzzle_02"]
    piv = {p["node"]: p for p in man["pivots"]}
    assert piv["pivot_yaw"]["axis"] == "local Z" and piv["pivot_pitch"]["axis"] == "local X", piv
    assert not any(p["has_game_socket"] for p in piv.values())
    if xl.find_godot() is not None:
        sc = _check(res, "godot_validation")["value"]["scenes"][0]
        assert sorted(sc["sockets"]) == ["socket_mount", "socket_muzzle_01", "socket_muzzle_02"], sc["sockets"]
        assert all(v["found"] for v in sc["sockets"].values()) and sc["loaded"] and sc["instanced"]
        assert _check(res, "godot_animation_loops")["ok"]
        # smoke 10.10: 'pitch_cycle-loop' made Godot's importer rename twice ('Animation not found');
        # the package carries 'pitch_cycle-cycle', imported as the looping 'pitch_cycle' without errors
        assert res["stats"]["clip_renames"] == {"pitch_cycle-loop": "pitch_cycle-cycle"}, res["stats"]["clip_renames"]
        found = {a["expected"]: (a["found"], a["loop_mode"]) for a in sc["animation_checks"]}
        assert found["pitch_cycle-cycle"] == ("pitch_cycle", 1) and found["yaw_sweep-loop"] == ("yaw_sweep", 1), found
        assert _check(res, "godot_engine_errors")["ok"], _check(res, "godot_engine_errors")
    # legacy turret (pivots with game_socket): still not sockets, a warning says so
    legacy = _glb_with_pivot_sockets(glb, os.path.join(d, "legacy.glb"))
    af.reset_scene()
    res2 = godot_package.run(_req("godot_package", {"glb": legacy},
                                  {"asset_id": "turret_af_test_002", "profile": "godot", "kind": "turret",
                                   "validate": False}, os.path.join(d, "out2")))
    assert not _errors(res2), _errors(res2)
    assert res2["stats"]["sockets"] == 3 and not _check(res2, "rig_pivots")["ok"]
    # ship_gen thrusters carry mount_d_m: engine_trail is scaled by the nozzle diameter
    req = _req("ship_gen", {}, {"class": "fighter", "seed": 4, "preview": False, "lods": False},
               os.path.join(d, "ship"))
    sres = af.finish(req, ship_gen.run(req))
    assert sres["ok"] and _check(sres, "thruster_mount_d_m")["ok"], _errors(sres)
    thr = [s for s in sres["stats"]["sockets"] if s.startswith("socket_thruster_")]
    af.reset_scene()
    res3 = godot_package.run(_req("godot_package", {"glb": sres["outputs"]["glb"]},
                                  {"asset_id": "ship_af_test_004", "profile": "godot", "kind": "ship",
                                   "validate": False, "fx": [{"preset": "engine_trail", "socket": thr[0]}]},
                                  os.path.join(d, "out3")))
    assert not _errors(res3), _errors(res3)
    b = json.load(open(res3["outputs"]["manifest"], encoding="utf-8"))["fx"][0]
    assert b["scale_source"].startswith("socket diameter"), b["scale_source"]
    _done(d)


def _glb_with_socket_frame(src, dst, frame):
    """Copy of a GLB whose socket_thruster_* nodes carry extras.frame = frame (the JSON chunk is patched)."""
    import struct
    raw = open(src, "rb").read()
    n = struct.unpack_from("<I", raw, 12)[0]
    doc = json.loads(raw[20:20 + n])
    for node in doc["nodes"]:
        if node.get("name", "").startswith("socket_thruster_"):
            node.setdefault("extras", {})["frame"] = frame
    js = json.dumps(doc, separators=(",", ":")).encode()
    js += b" " * ((4 - len(js) % 4) % 4)
    rest = raw[20 + n:]
    with open(dst, "wb") as fh:
        fh.write(struct.pack("<4sII", b"glTF", 2, 20 + len(js) + len(rest)) + struct.pack("<I4s", len(js), b"JSON")
                 + js + rest)
    return dst


def _thruster_fx(names, presets=("engine_trail", "thruster_glow")):
    return [{"preset": pr, "socket": n} for n in names for pr in presets]


def _godot_thruster_fx(res):
    """(scene report, FX entries that carry an axis expectation) from the Godot validation of a package."""
    sc = _check(res, "godot_validation")["value"]["scenes"][0]
    return sc, [f for f in sc["fx"] if "axis_ok" in f]


def test_thruster_frames_fx_aft_godot():
    """The thruster frames of ship_gen (thrust: exhaust along glTF local +Z; exhaust: -Z) both get thruster FX
    that fly aft: emit_axis is the frame's local axis, and in Godot 4.5.1 the FX +Y axis in the package root is
    +Z (aft of a -Z-forward ship). thrust and exhaust sockets have local +Y = up, so FX that ignored the frame
    pointed up."""
    from steps import ship_gen
    expect = {"thrust": [0.0, 0.0, 1.0], "exhaust": [0.0, 0.0, -1.0]}
    d = _tmp("af_export_frames_")
    godot = xl.find_godot() is not None
    for frame, axis in expect.items():
        af.reset_scene()
        req = _req("ship_gen", {}, {"class": "fighter", "seed": 1, "thruster_frame": frame, "preview": False,
                                    "lods": False}, os.path.join(d, "gen_" + frame))
        sres = af.finish(req, ship_gen.run(req))
        assert sres["ok"], (frame, _errors(sres))
        thr = [n for n in sres["stats"]["sockets"] if n.startswith("socket_thruster_")]
        assert thr, sres["stats"]["sockets"]
        af.reset_scene()
        res = godot_package.run(_req("godot_package", {"glb": sres["outputs"]["glb"]},
                                     {"asset_id": "ship_af_frame_" + frame, "profile": "godot", "kind": "ship",
                                      "fx": _thruster_fx(thr), "validate": True},
                                     os.path.join(d, "pkg_" + frame)))
        assert not _errors(res), (frame, _errors(res), res["checks"])
        assert not any("contradicts" in w for w in res["warnings"]), res["warnings"]
        assert _check(res, "fx_thruster_axis")["ok"]
        man = json.load(open(res["outputs"]["manifest"], encoding="utf-8"))
        assert {s["frame"] for s in man["sockets"] if s["name"] in thr} == {frame}, man["sockets"]
        assert len(man["fx"]) == 2 * len(thr)
        for b in man["fx"]:
            assert b["emit_axis"] == axis and b["emit_axis_from"] == "extras.frame " + frame, b
            assert abs(b["emit_aft_dot"] - 1.0) < 1e-3, b
        assert {v["from"] for v in res["stats"]["thruster_axes"].values()} == {"extras.frame " + frame}
        tscn = open(res["outputs"]["tscn"], encoding="utf-8").read()
        assert tscn.count('"emit_axis": Vector3(%s)' % ", ".join("%d" % int(c) for c in axis)) == 2 * len(thr), tscn
        readme = open(res["outputs"]["readme"], encoding="utf-8").read()
        assert "emission axis" in readme and "extras.frame " + frame in readme
        if not godot:
            continue
        sc, fxs = _godot_thruster_fx(res)
        assert len(fxs) == 2 * len(thr) and all(f["ok"] and f["axis_ok"] for f in fxs), sc["fx"]
        assert all(f["axis_y_root"][2] > 0.9 for f in fxs), [f["axis_y_root"] for f in fxs]   # aft = +Z in Godot
        assert all(abs(f["scale_rel"] - 1.0) < 0.01 for f in fxs), sc["fx"]
        assert _check(res, "godot_fx_thruster_aft")["ok"]
        # the situation the fix is for: the socket's own +Y is up in thrust / exhaust
        ay = sc["sockets"][thr[0]]["axis_y"]
        assert ay[1] > 0.99, (frame, ay)
    _done(d)


def test_thruster_axis_legacy_inferred_and_rotated_model():
    """An older GLB without extras.frame (local +Y = exhaust, build_ship) gets the +Y axis by geometry
    ('inferred'), also under a 0.01-scaled node (the thrust-frame socket there needs a rotation that survives
    af_fx_mount's scale fix); muzzle_flash (emits -Z) and non-thruster sockets are untouched; a model turned 180
    degrees about Y (forward_axis +Z) expects aft = -Z in the package root."""
    d = _tmp("af_export_axis_legacy_")
    glb = build_ship(os.path.join(d, "ship.glb"), scaled_parent=True, scaled_thruster=True)
    af.reset_scene()
    fx = _thruster_fx(["socket_thruster_main_l"], ("engine_trail", "muzzle_flash")) \
        + _thruster_fx(["socket_thruster_main_r"], ("thruster_glow",)) \
        + _thruster_fx(["socket_thruster_scaled"], ("engine_trail",)) \
        + [{"preset": "smoke", "socket": "socket_fx_scaled"}]
    res = godot_package.run(_req("godot_package", {"glb": glb},
                                 {"asset_id": "ship_af_axis_001", "profile": "godot", "kind": "ship",
                                  "fx": fx}, os.path.join(d, "out")))
    assert not _errors(res), (_errors(res), res["checks"])
    assert not any("contradicts" in w for w in res["warnings"]), res["warnings"]
    man = json.load(open(res["outputs"]["manifest"], encoding="utf-8"))
    by = {b["node_name"] + "@" + b["socket"]: b for b in man["fx"]}
    for key in ("fx_engine_trail@socket_thruster_main_l", "fx_thruster_glow@socket_thruster_main_r"):
        assert by[key]["emit_axis"] == [0.0, 1.0, 0.0], by[key]
        assert by[key]["emit_axis_from"] == "inferred: most aft-pointing local axis +Y", by[key]
    sc_b = by["fx_engine_trail@socket_thruster_scaled"]
    assert sc_b["emit_axis"] == [0.0, 0.0, 1.0] and sc_b["emit_axis_from"] == "extras.frame thrust", sc_b
    assert "emit_axis" not in by["fx_muzzle_flash@socket_thruster_main_l"], "muzzle_flash emits along -Z"
    assert "emit_axis" not in by["fx_smoke@socket_fx_scaled"], "only thruster sockets get an exhaust axis"
    assert {s["name"]: s["frame"] for s in man["sockets"] if s["kind"] == "thruster"} == {
        "socket_thruster_main_l": None, "socket_thruster_main_r": None, "socket_thruster_scaled": "thrust"}
    tscn = open(res["outputs"]["tscn"], encoding="utf-8").read()
    assert tscn.count('"emit_axis"') == 3, tscn
    if xl.find_godot() is not None:
        sc, fxs = _godot_thruster_fx(res)
        assert len(fxs) == 3 and all(f["axis_ok"] and f["axis_y_root"][2] > 0.9 for f in fxs), sc["fx"]
        # also under the 0.01 holder: scale undone and the +Z rotation kept (scale after rotation in af_fx_mount)
        assert all(abs(f["scale_rel"] - 1.0) < 0.01 for f in sc["fx"]), sc["fx"]
        mf = next(f for f in sc["fx"] if f["name"] == "fx_muzzle_flash")
        assert mf["ok"] and "axis_ok" not in mf, mf
        assert _check(res, "godot_fx_thruster_aft")["ok"]
    # the same ship with its model turned to face Godot's +Z: the exhaust must then go to -Z of the package root
    af.reset_scene()
    res2 = godot_package.run(_req("godot_package", {"glb": glb},
                                  {"asset_id": "ship_af_axis_002", "profile": "godot", "kind": "ship",
                                   "forward_axis": "+Z",
                                   "fx": _thruster_fx(["socket_thruster_main_l", "socket_thruster_scaled"],
                                                      ("engine_trail",))}, os.path.join(d, "out2")))
    assert not _errors(res2), (_errors(res2), res2["checks"])
    assert res2["stats"]["scene_forward"] == "+Z" and res2["stats"]["glb_forward"] == "-Z"
    if xl.find_godot() is not None:
        sc, fxs = _godot_thruster_fx(res2)
        assert sc["model_basis_z"][2] < -0.99, sc["model_basis_z"]
        assert len(fxs) == 2 and all(f["axis_ok"] and f["axis_y_root"][2] < -0.9 for f in fxs), sc["fx"]
        assert _check(res2, "godot_fx_thruster_aft")["ok"]
    _done(d)


def test_thruster_axis_contradicting_frame_and_canted_nozzle():
    """extras.frame that disagrees with the geometry loses to it, with one warning naming the sockets: legacy
    sockets claiming 'thrust' get +Y (Godot confirms the FX fly aft); the sockets that tell the truth keep their frame. An unknown frame name is ignored
    with a note; nozzles canted 30 degrees keep their axis and Godot accepts the FX 30 degrees off aft."""
    d = _tmp("af_export_axis_bad_")
    glb = build_ship(os.path.join(d, "ship.glb"), scaled_parent=True, scaled_thruster=True)
    names = ["socket_thruster_main_l", "socket_thruster_main_r", "socket_thruster_scaled"]
    fx = _thruster_fx(names, ("engine_trail",))
    # main_l / main_r: legacy geometry (aft = local +Y); socket_thruster_scaled: thrust geometry (aft = local +Z)
    for frame, liars, truthful, wrong_axis in (("thrust", names[:2], names[2:], "+Y"),):
        af.reset_scene()
        patched = _glb_with_socket_frame(glb, os.path.join(d, "liar_%s.glb" % frame), frame)
        res = godot_package.run(_req("godot_package", {"glb": patched},
                                     {"asset_id": "ship_af_axis_lie_" + frame, "profile": "godot",
                                      "kind": "ship", "fx": fx}, os.path.join(d, "out_" + frame)))
        assert not _errors(res), (frame, _errors(res), res["checks"])
        warn = [w for w in res["warnings"] if "contradicts the geometry" in w]
        assert len(warn) == 1, res["warnings"]
        assert all("%s (%s -> %s)" % (n, frame, wrong_axis) in warn[0] for n in liars), warn
        assert not any(n in warn[0] for n in truthful), warn
        man = json.load(open(res["outputs"]["manifest"], encoding="utf-8"))
        by = {b["socket"]: b for b in man["fx"]}
        for n in liars:         # the geometry wins: the axis that really points aft
            assert by[n]["emit_axis"] == {"+Y": [0.0, 1.0, 0.0], "+Z": [0.0, 0.0, 1.0]}[wrong_axis], by[n]
            assert by[n]["emit_axis_from"] == ("extras.frame %s contradicts geometry; inferred: most aft-pointing "
                                               "local axis %s" % (frame, wrong_axis)), by[n]
        for n in truthful:      # the documented frame holds
            assert by[n]["emit_axis_from"] == "extras.frame " + frame, by[n]
        assert all(b["emit_aft_dot"] > 0.99 for b in by.values()), by
        assert {s["frame"] for s in man["sockets"] if s["kind"] == "thruster"} == {frame}
        if xl.find_godot() is not None:
            sc, fxs = _godot_thruster_fx(res)
            assert len(fxs) == 3 and all(f["axis_ok"] and f["axis_y_root"][2] > 0.9 for f in fxs), sc["fx"]
            assert all(abs(f["scale_rel"] - 1.0) < 0.01 for f in sc["fx"]), sc["fx"]
    fx = fx[:2]
    glb = build_ship(os.path.join(d, "plain.glb"))          # legacy ship without extras.frame
    # unknown frame name: geometry decides, noted in emit_axis_from, no contradiction warning
    af.reset_scene()
    odd = _glb_with_socket_frame(glb, os.path.join(d, "odd.glb"), "sideways")
    res_o = godot_package.run(_req("godot_package", {"glb": odd},
                                   {"asset_id": "ship_af_axis_004", "profile": "godot", "kind": "ship",
                                    "validate": False, "fx": fx}, os.path.join(d, "out_o")))
    assert not _errors(res_o) and not any("contradicts" in w for w in res_o["warnings"]), res_o["warnings"]
    b = json.load(open(res_o["outputs"]["manifest"], encoding="utf-8"))["fx"][0]
    assert b["emit_axis"] == [0.0, 1.0, 0.0] and "extras.frame 'sideways' unknown" in b["emit_axis_from"], b
    # canted nozzles
    tilt = build_ship(os.path.join(d, "tilt.glb"), tilt_deg=30.0)
    af.reset_scene()
    res_t = godot_package.run(_req("godot_package", {"glb": tilt},
                                   {"asset_id": "ship_af_axis_005", "profile": "godot", "kind": "ship",
                                    "fx": fx}, os.path.join(d, "out_t")))
    assert not _errors(res_t), (_errors(res_t), res_t["checks"])
    b = json.load(open(res_t["outputs"]["manifest"], encoding="utf-8"))["fx"][0]
    assert b["emit_axis"] == [0.0, 1.0, 0.0] and abs(b["emit_aft_dot"] - math.cos(math.radians(30))) < 1e-3, b
    if xl.find_godot() is not None:
        sc, fxs = _godot_thruster_fx(res_t)
        assert len(fxs) == 2 and all(f["axis_ok"] and 0.8 < f["axis_y_root"][2] < 0.9 for f in fxs), sc["fx"]
        assert all(abs(f["axis_dot"] - math.cos(math.radians(30))) < 0.01 for f in fxs), sc["fx"]
    _done(d)


def test_fx_scale_rules():
    presets = json.load(open(xl.FX_PRESETS_PATH, encoding="utf-8"))["presets"]
    et, nav, aura = presets["engine_trail"], presets["nav_light"], presets["aura"]
    assert xl.fx_auto_scale(et, {"size_class": "L"}, 64.8)[0] == 10.0
    assert xl.fx_auto_scale(et, {"size_class": "M"}, 30)[0] == 5.0
    assert xl.fx_auto_scale(et, {"size_class": "S"}, 10)[0] == 2.22
    assert xl.fx_auto_scale(et, {"size_class": "L", "diameter_m": 6.4}, 64.8)[0] == 14.2   # diameter wins
    assert xl.fx_auto_scale(et, {}, 64.8)[0] == 10.8                                        # hull fallback
    assert xl.fx_auto_scale(nav, {"size_class": "L"}, 64.8)[0] == 10.8                     # no socket reference
    assert xl.fx_auto_scale(aura, {}, 1.8)[0] == 1.0
    k, src = xl.fx_auto_scale(presets["muzzle_flash"], {"size_class": "M", "size_class_from": "socket_mount_top"}, 40)
    assert k == 2.25 and "socket_mount_top" in src, (k, src)
    m = xl.merge_fx(et, {}, scale=10.0)
    assert m["visibility_aabb"] == [-80, -20, -80, 160, 160, 160] and m["damping"] == [5.0, 10.0], m
    assert xl.merge_fx(et, {"scale": 2.0}, scale=10.0)["quad_size"] == 1.9      # params.scale beats auto
    assert xl.merge_fx(presets["sparks"], {}, scale=3.0)["visibility_aabb"] == [-12, -12, -12, 24, 24, 24]
    assert xl.fx_scale_problem(et, 1.0, 64.8) is not None and xl.fx_scale_problem(et, 10.0, 64.8) is None
    assert xl.fx_scale_problem(nav, 1.0, 64.8) is not None                       # 0.5 m light on 64.8 m
    assert xl.fx_scale_problem(aura, 1.0, 1.8) is None
    mf = presets["muzzle_flash"]                     # gatling flash sized by its mount: only the 1 % floor applies
    assert xl.fx_scale_problem(mf, 2.6, 64.8) is not None
    assert xl.fx_scale_problem(mf, 2.6, 64.8, mount_scaled=True) is None
    assert xl.fx_scale_problem(mf, 1.5, 64.8, mount_scaled=True) is not None


# --------------------------------------------------------------------------- real assets


def _wide_figure(path, bar_x=0.0, disc_r=0.0):
    """The marker figure with a horizontal bar held across it (bar_x m long, at 1.0 m) and/or a thin disc of radius
    disc_r at 0.25 m around it (above the lowest 12 % used for the feet centre, it reaches below the ground line on
    screen toward the camera)."""
    build_marker_figure(path)
    objs = [o for o in bpy.context.scene.objects if o.type == "MESH"]
    grey = objs[0].data.materials[0]
    if bar_x:
        objs.append(_mesh_obj("bar", bpy.ops.mesh.primitive_cube_add, grey, size=1.0, location=(0, -0.25, 1.0),
                              scale=(bar_x, 0.04, 0.04)))
    if disc_r:
        objs.append(_mesh_obj("disc", bpy.ops.mesh.primitive_cylinder_add, grey, radius=disc_r, depth=0.02,
                              location=(0, 0, 0.25), vertices=48))
    af.apply_transforms(objs)
    af.export_glb(path, objects=objs, animations=False, skins=False)
    return path


def _cloth_cache_scene(blend_path):
    """Two clips of a one-bone rig (the block rides the bone, standing still) and a 'garment' plane carrying a
    cloth_sim cache: in clip 'left' it sits 0.6 m to -X, in 'right' 0.6 m to +X (and skinned at the origin)."""
    from steps import _cloth_lib as CL
    af.reset_scene()
    arm_data = bpy.data.armatures.new("rig")
    arm = bpy.data.objects.new("rig", arm_data)
    bpy.context.scene.collection.objects.link(arm)
    bpy.context.view_layer.objects.active = arm
    bpy.ops.object.mode_set(mode="EDIT")
    b = arm_data.edit_bones.new("root")
    b.head, b.tail = (0, 0, 0), (0, 0, 1)
    bpy.ops.object.mode_set(mode="OBJECT")
    grey = _material("grey", (0.6, 0.6, 0.6))
    cube = _mesh_obj("block", bpy.ops.mesh.primitive_cube_add, grey, size=0.4, location=(0, 0, 0.2))
    mw = cube.matrix_world.copy()
    cube.parent, cube.parent_type, cube.parent_bone = arm, "BONE", "root"
    cube.matrix_world = mw
    red = _material("red", (0.8, 0.1, 0.1))
    red.diffuse_color = (0.8, 0.1, 0.1, 1.0)          # Workbench shows the viewport colour
    garment = _mesh_obj("Body_cloth_test", bpy.ops.mesh.primitive_plane_add, red,
                        size=0.3, location=(0, -0.3, 0.6), rotation=(math.pi / 2, 0, 0))
    bpy.ops.object.transform_apply(location=True, rotation=True, scale=True)
    vg = garment.vertex_groups.new(name="root")
    vg.add(list(range(len(garment.data.vertices))), 1.0, "REPLACE")
    garment.parent = arm
    m = garment.modifiers.new("Armature", "ARMATURE")
    m.object = arm
    for name in ("left", "right"):
        act = bpy.data.actions.new(name)
        act.use_fake_user = True
        xl.assign_action(arm, act)
        pb = arm.pose.bones["root"]
        for f in (0, 6):
            pb.location = (0.0, 0.0, 0.0)
            pb.keyframe_insert("location", frame=f)
        if arm.animation_data.action_slot is None and len(act.slots):
            arm.animation_data.action_slot = act.slots[0]
    rest = CL._object_rest(garment)
    CL.bake_render_cache(garment, [("left", np.stack([rest + np.array([-0.6, 0, 0])] * 7)),
                                   ("right", np.stack([rest + np.array([0.6, 0, 0])] * 7))])
    af.save_blend(blend_path)
    return blend_path


def test_render_sprites_plays_cloth_cache():
    """render_sprites switches a cloth_sim cache together with each clip (_export_lib.bind_cloth_cache): the
    garment's simulated place of that clip is in its sheet, the rest pose (normalisation) stays the skinned one."""
    d = _tmp("af_export_cloth_")
    blend = _cloth_cache_scene(os.path.join(d, "cloth.blend"))
    af.open_blend(blend)
    res = render_sprites.run(_req("render_sprites", {"blend": blend},
                                  {"profile": "custom", "directions": [["S", 0.0]], "asset_id": "cloth_cache",
                                   "frames": 2, "pixels_per_meter": 60, "engine": "WORKBENCH", "center": "none",
                                   "in_place": "none", "validate_godot": False}, os.path.join(d, "sprites")))
    assert not _errors(res), _errors(res)
    doc = json.load(open(res["outputs"]["sprites_json"], encoding="utf-8"))
    xs = {}
    for clip in ("left", "right"):
        sheet = xl.load_png(res["outputs"]["sheets"]["%s.png" % clip])
        red = (sheet[..., 0].astype(int) > 120) & (sheet[..., 1] < 90) & (sheet[..., 3] > 0)
        assert red.any(), clip
        cw = doc["cell"][0]
        xs[clip] = float((np.nonzero(red)[1] % cw).mean())
    ax = doc["anchor"][0]
    # 0.6 m x 60 px/m = 36 px to each side of the anchor
    assert xs["left"] < ax - 25 and xs["right"] > ax + 25, (xs, ax)
    # the cell was measured on the cached garment of every clip (both sides), not on the skinned one
    assert doc["cell"][0] >= 2 * (0.6 + 0.15) * 60 - 2, doc["cell"]
    _done(d)
