"""Helpers of the export module (render_sprites, godot_package) — Blender 5.0.1, numpy, no Pillow.

Contents
- profile constants (iso8 directions, camera/light conventions);
- engine selection (EEVEE / Cycles GPU / Workbench) without af_common.engine_id, whose static enum
  lookup does not see CYCLES/WORKBENCH in background mode;
- clip discovery on armatures (Blender 5.0 slotted actions), evaluated vertex sampling;
- PNG I/O through bpy.data.images (lossless round trip, verified), sheet post-processing (grade,
  clean_alpha, clamp_shadow, fade_shadow_edges, pixel_support) in numpy;
- tiny 3x5 bitmap font, checker/anchor contact previews;
- Godot text resources (SpriteFrames .tres, FX .tscn) and the headless Godot 4.5.1 validation runner.
"""
import json
import math
import os
import re
import shutil
import subprocess
import time

import bpy
import numpy as np
from mathutils import Vector

HERE = os.path.dirname(os.path.abspath(__file__))
BLENDER_DIR = os.path.dirname(HERE)
GODOT_DIR = os.path.join(BLENDER_DIR, "godot")
FX_PRESETS_PATH = os.path.join(BLENDER_DIR, "data", "fx_presets.json")
# Godot console executable: params.godot, else env ASTRA_GODOT (no machine-specific default; None when unset)
GODOT_DEFAULT = os.environ.get("ASTRA_GODOT") or None
ASSET_ID_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")

# --------------------------------------------------------------------------- profiles

# iso8: model yaw (deg, CCW about +Z) applied to a character that faces -Y (contract §4) so that it
# faces the given screen direction; S = facing the camera. The step orbits the camera rig by -yaw
# instead of turning the model, which is equivalent and keeps root motion untouched.
ISO8 = [("S", 0.0), ("SW", -45.0), ("W", -90.0), ("NW", -135.0),
        ("N", 180.0), ("NE", 135.0), ("E", 90.0), ("SE", 45.0)]

LOOPING = {"idle", "walk", "run"}
# camera rig lights (build_camera_rig): warm sun from the upper-left-front of the screen, cool sky
SUN_COLOR = (1.0, 0.94, 0.84)
SUN_STRENGTH = 3.5
SUN_ELEVATION_DEG = 50.0
SKY_COLOR = (0.55, 0.62, 0.75)
SKY_STRENGTH = 0.7
# sheet colour grade (params.grade)
GRADE_SATURATION = 1.10
GRADE_CONTRAST = 1.05
ALPHA_FLOOR = 6

LOOP_SUFFIXES = ("-loop", "_loop", "-cycle", "_cycle")

# Asset kinds whose nose is Blender +Y / glTF -Z (contract §4); everything else faces -Y / +Z.
SHIP_KINDS = {"ship", "prop", "turret", "station", "station_module", "drone", "hull_module", "missile",
              "container", "asteroid", "debris"}
SHIP_SOCKET_RE = re.compile(r"(thruster|turret|launcher|muzzle)")
# Turret rig nodes (socket_mount -> pivot_yaw -> pivot_pitch -> socket_muzzle_*): pivots carry extras
# axis / range_degrees and are NOT sockets (contract §6) - socket lists and name checks skip them.
PIVOT_RE = re.compile(r"^pivot_[a-z0-9]+(_[a-z0-9]+)*$")


def is_rig_pivot(name, game_socket=None):
    """True for turret rig pivots (pivot_yaw, pivot_pitch, ...), also legacy ones that still carry a
    game_socket and Blender's '.001' duplicates."""
    for n in (game_socket, name):
        if n is not None and PIVOT_RE.match(str(n).split(".")[0]):
            return True
    return False

# Sprite sheet limits: sheets are kept <= SHEET_MAX_PX per side when possible; Godot cannot import a
# texture side above GODOT_TEXTURE_MAX.
SHEET_MAX_PX = 8192
GODOT_TEXTURE_MAX = 16384


def default_asset_id(name):
    """snake_case asset id from a file name: (id, warning or None). '01_pathfinder' -> 'a_01_pathfinder'."""
    s = re.sub(r"[^A-Za-z0-9]+", "_", str(name)).strip("_").lower()
    if ASSET_ID_RE.match(s):
        return s, None
    fixed = ("a_" + s) if s else "asset"
    if not ASSET_ID_RE.match(fixed):
        fixed = "asset"
    return fixed, "asset_id %r derived from the file name is not snake_case ASCII; using %r (pass params.asset_id)" \
        % (s or name, fixed)


def sheet_layout(n, cw, ch, n_views, cols=None, per_view=False, max_side=SHEET_MAX_PX):
    """Sheet geometry for n frames per view.

    per_view=True gives one sheet per view; otherwise all views are stacked in one sheet
    per clip and the step switches to per-view sheets when the stack would exceed max_side. Columns:
    params.cols, else all frames in one row when they fit max_side, else balanced rows. Returns
    {cols, rows, layout: 'stacked'|'per_view', sheet: [w, h], over_soft: bool}; raises ValueError when a
    side would exceed Godot's 16384 px texture limit.
    """
    n = max(1, int(n))
    if cols:
        c = max(1, min(int(cols), n))
    elif n * cw <= max_side:
        c = n
    else:
        rows_needed = int(math.ceil(n * cw / float(max_side)))
        c = int(math.ceil(n / float(rows_needed)))
        c = max(1, min(c, max(1, max_side // cw)))
    rows = int(math.ceil(n / float(c)))
    layout = "per_view" if per_view else "stacked"
    if layout == "stacked" and n_views * rows * ch > max_side:
        layout = "per_view"
    w = c * cw
    h = rows * ch * (n_views if layout == "stacked" else 1)
    if max(w, h) > GODOT_TEXTURE_MAX:
        raise ValueError("sprite sheet would be %dx%d px (cell %dx%d, %d frames, %d views): over Godot's %d px "
                         "texture limit; lower pixels_per_meter or frames, or fix the model scale (height_m)"
                         % (w, h, cw, ch, n, n_views, GODOT_TEXTURE_MAX))
    return {"cols": c, "rows": rows, "layout": layout, "sheet": [w, h], "over_soft": max(w, h) > max_side}


def clip_base(name):
    """('idle-loop' -> ('idle', True)); Godot strips the same suffixes on import."""
    low = name.lower()
    for suf in LOOP_SUFFIXES:
        if low.endswith(suf):
            return name[:-len(suf)], True
    return name, False


GODOT_LOOP_TOKENS = ("loop_mode", "loop", "cycle")   # resource_importer_scene.cpp _pre_fix_node, Godot 4.5


def _godot_trailing(name):
    what = name
    while what and (what[-1].isdigit() or ord(what[-1]) <= 32 or what[-1] == "."):
        what = what[:-1]
    return what, name[len(what):]


def _godot_teststr(name, tok):
    what, _ = _godot_trailing(name)
    low = what.lower()
    return ("$" + tok) in low or low.endswith("-" + tok) or low.endswith("_" + tok)


def _godot_fixstr(name, tok):
    what, end = _godot_trailing(name)
    low = what.lower()
    i = low.find("$" + tok)
    if i >= 0:
        return what[:i] + what[i + len(tok) + 1:] + end
    if low.endswith("-" + tok) or low.endswith("_" + tok):
        return what[:-(len(tok) + 1)] + end
    return what


def godot_import_name(name):
    """What Godot 4.5's scene importer makes of a glTF animation name: (imported_name, loops, error).

    For each token loop_mode / loop / cycle that the CURRENT name ends with ('-tok' / '_tok', or contains
    '$tok') the importer sets a linear loop, strips it and calls rename_animation(ORIGINAL, stripped). A second
    matching token therefore renames an animation that no longer exists: 'pitch_cycle-loop' imports as
    'pitch_cycle' (looping) with 'ERROR: Animation not found: pitch_cycle-loop' (smoke 10.10, turret)."""
    cur, imported, loops, error = name, None, False, False
    for tok in GODOT_LOOP_TOKENS:
        if _godot_teststr(cur, tok):
            loops = True
            cur = _godot_fixstr(cur, tok)
            if imported is None:
                imported = cur          # the first rename succeeds
            else:
                error = True            # rename_animation(original, ...) fails
    return (name if imported is None else imported), loops, error


def godot_safe_clip_name(name):
    """A glTF name that Godot 4.5 imports as the same animation (clip_base name, same loop flag) without
    the double-rename error, or `name` itself when it is already clean. 'pitch_cycle-loop' ->
    'pitch_cycle-cycle' (imported 'pitch_cycle', looping): the only stripped token is the last one the
    importer tests, so nothing is renamed twice."""
    imported, loops, error = godot_import_name(name)
    if not error:
        return name
    for cand in (imported + "-cycle", imported + "_cycle", imported + "$cycle"):
        got, l2, e2 = godot_import_name(cand)
        if got == imported and l2 == loops and not e2:
            return cand
    return name


def rename_glb_animations(src, dst, mapping):
    """Copy a .glb to dst with glTF animation names renamed by `mapping` {old: new} (JSON chunk only, the
    binary chunk is copied byte for byte). Returns the list of renamed names."""
    import struct
    with open(src, "rb") as fh:
        data = fh.read()
    magic, version, _ = struct.unpack_from("<III", data, 0)
    if magic != 0x46546C67:
        raise ValueError("not a GLB: %s" % src)
    jlen, jtype = struct.unpack_from("<II", data, 12)
    doc = json.loads(data[20:20 + jlen].decode("utf-8"))
    rest = data[20 + jlen:]
    done = []
    for a in doc.get("animations", []):
        if a.get("name") in mapping:
            done.append(a["name"])
            a["name"] = mapping[a["name"]]
    js = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    js += b" " * (-len(js) % 4)
    out = struct.pack("<III", magic, version, 12 + 8 + len(js) + len(rest)) + struct.pack("<II", len(js), jtype)
    with open(dst, "wb") as fh:
        fh.write(out + js + rest)
    return done


def safe_name(name):
    s = re.sub(r"[^A-Za-z0-9]+", "_", str(name)).strip("_").lower()
    return s or "clip"


# --------------------------------------------------------------------------- engine

def set_engine(scene, name="EEVEE", samples=None):
    """Select a render engine; returns (engine_id, device_note)."""
    name = (name or "EEVEE").upper()
    cands = {"EEVEE": ["BLENDER_EEVEE", "BLENDER_EEVEE_NEXT"], "CYCLES": ["CYCLES"],
             "WORKBENCH": ["BLENDER_WORKBENCH"]}.get(name)
    if cands is None:
        raise ValueError("engine must be EEVEE, CYCLES or WORKBENCH: %r" % name)
    for cand in cands:
        try:
            scene.render.engine = cand
            break
        except TypeError:
            continue
    device = "cpu"
    eng = scene.render.engine
    if eng == "CYCLES":
        prefs = bpy.context.preferences.addons["cycles"].preferences
        scene.cycles.device = "CPU"
        for kind in ("OPTIX", "CUDA"):
            try:
                prefs.compute_device_type = kind
                prefs.get_devices()
                use = [d for d in prefs.devices if d.type == kind]
                if not use:
                    continue
                for d in prefs.devices:
                    d.use = d.type == kind
                scene.cycles.device = "GPU"
                device = kind.lower()
                break
            except Exception:
                continue
        scene.cycles.samples = int(samples or 64)
        scene.cycles.use_denoising = True
        scene.cycles.max_bounces = 6
        scene.cycles.transparent_max_bounces = 16
    elif eng.startswith("BLENDER_EEVEE"):
        scene.eevee.taa_render_samples = int(samples or 32)
        device = "gpu"
    else:
        sh = scene.display.shading
        sh.light = "STUDIO"
        sh.color_type = "TEXTURE"
        sh.show_cavity = False
        device = "gpu"
    return eng, device


def setup_output(scene, width, height, fps=12):
    r = scene.render
    r.resolution_x = int(width)
    r.resolution_y = int(height)
    r.resolution_percentage = 100
    r.pixel_aspect_x = r.pixel_aspect_y = 1.0
    r.film_transparent = True
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGBA"
    r.image_settings.color_depth = "8"
    r.use_motion_blur = False
    try:
        scene.view_settings.view_transform = "Standard"
        scene.view_settings.look = "None"
        scene.view_settings.exposure = 0.0
        scene.view_settings.gamma = 1.0
    except TypeError:
        pass
    r.fps = int(fps)
    r.fps_base = 1.0


# --------------------------------------------------------------------------- actions / clips

def fcurves_of(action):
    """F-curves of a Blender 4.4+/5.0 slotted action (action.fcurves is gone)."""
    out = []
    for layer in action.layers:
        for strip in layer.strips:
            for bag in strip.channelbags:
                out.extend(bag.fcurves)
    return out


def action_bones(action):
    names = set()
    for fc in fcurves_of(action):
        m = re.match(r'pose\.bones\["(.+?)"\]', fc.data_path)
        if m:
            names.add(m.group(1))
    return names


def assign_action(obj, action):
    """Assign an action with a matching slot (in 5.0 curves do nothing without a slot)."""
    if obj.animation_data is None:
        obj.animation_data_create()
    ad = obj.animation_data
    # NLA strips would add on top of the active action — mute them while sampling
    for tr in ad.nla_tracks:
        tr.mute = True
    ad.action = action
    if action is not None and len(action.slots):
        slot = None
        for s in action.slots:
            if s.target_id_type == "OBJECT" and (s.identifier[2:] == obj.name or s.name_display == obj.name):
                slot = s
                break
        if slot is None:
            slot = next((s for s in action.slots if s.target_id_type == "OBJECT"), action.slots[0])
        try:
            ad.action_slot = slot
        except Exception:
            pass


def find_clips(arms):
    """Group armature actions into clips. Returns [{name, actions: {arm_name: action}, range}].

    An action fits an armature when >= 50 % of its pose-bone channels name bones of it.
    Multi-armature clips (rider + mount) use the naming '<clip>|<armature>'.
    """
    clips = {}
    for action in bpy.data.actions:
        bones = action_bones(action)
        if not bones:
            continue
        for arm in arms:
            have = set(arm.data.bones.keys())
            if len(bones & have) < 0.5 * len(bones):
                continue
            name = action.name
            if "|" in name and name.rsplit("|", 1)[1] == arm.name:
                name = name.rsplit("|", 1)[0]
            entry = clips.setdefault(name, {"name": name, "actions": {}, "range": None})
            entry["actions"][arm.name] = action
            lo, hi = float(action.frame_range[0]), float(action.frame_range[1])
            rng = entry["range"]
            entry["range"] = [lo, hi] if rng is None else [min(rng[0], lo), max(rng[1], hi)]
    return list(clips.values())


CLOTH_CACHE_PROP = "af_cloth_cache"     # set by cloth_sim (_cloth_lib.bake_render_cache) on its garment objects


def cloth_proxies():
    """Garment objects baked by cloth_sim (Body_cloth_<group>): meshes whose af_cloth_cache maps armature actions to
    shape-key actions holding the simulated cloth of that clip (absolute positions, one key per clip frame)."""
    return [o for o in bpy.data.objects if o.type == "MESH" and CLOTH_CACHE_PROP in o.keys()]


def bind_cloth_cache(actions):
    """Switch every baked cloth cache to the clip of these armature actions (actions or names; None / empty = no
    clip): the garment plays that clip's shape-key action with its Armature modifier off (the keys hold the final
    positions); a clip without a cache falls back to plain skinning (keys at 0, Armature on).
    Returns {object name: shape-key action name or None}."""
    names = set()
    for a in (actions.values() if isinstance(actions, dict) else (actions or ())):
        if a is not None:
            names.add(a if isinstance(a, str) else a.name)
    out = {}
    for px in cloth_proxies():
        try:
            mapping = json.loads(px[CLOTH_CACHE_PROP])
        except (TypeError, ValueError):
            continue
        act_name = next((mapping[n] for n in sorted(names) if n in mapping), None)
        act = bpy.data.actions.get(act_name) if act_name else None
        key = px.data.shape_keys
        if key is not None:
            # keys animated by the previous clip keep their last value when its action is unassigned
            for kb in key.key_blocks:
                kb.value = 0.0
            if key.animation_data is None:
                key.animation_data_create()
            key.animation_data.action = act
            if act is not None and len(act.slots):
                try:
                    key.animation_data.action_slot = act.slots[0]
                except Exception:
                    pass
        for m in px.modifiers:
            if m.type == "ARMATURE":
                m.show_viewport = m.show_render = act is None
        for ob in bpy.data.objects:
            if ob.type != "MESH":
                continue
            for m in ob.modifiers:
                if m.type == "SURFACE_DEFORM" and m.target == px:      # older caches on a proxy
                    m.show_viewport = m.show_render = act is not None
        out[px.name] = act.name if act is not None else None
    return out


def root_bone(arm):
    names = arm.data.bones.keys()
    for cand in ("mixamorig:Hips", "Hips", "hips", "mixamorig_Hips", "root", "Root", "pelvis", "Pelvis"):
        if cand in names:
            return cand
    for b in arm.data.bones:
        if b.parent is None:
            return b.name
    return None


def sample_times(start, end, count, loop):
    """Evenly spaced; a loop does not repeat its first frame."""
    span = end - start
    if count <= 1:
        return [start]
    step = span / count if loop else span / (count - 1)
    return [start + step * i for i in range(count)]


def set_time(value):
    whole = math.floor(value)
    bpy.context.scene.frame_set(int(whole), subframe=float(value - whole))
    bpy.context.view_layer.update()


def world_points(objs):
    """Evaluated (deformed) vertices of the given meshes in world space, (N, 3) float64."""
    dg = bpy.context.evaluated_depsgraph_get()
    rows = []
    for o in objs:
        ev = o.evaluated_get(dg)
        me = ev.to_mesh()
        try:
            n = len(me.vertices)
            if not n:
                continue
            co = np.empty(n * 3, dtype=np.float32)
            me.vertices.foreach_get("co", co)
            m = np.array(ev.matrix_world, dtype=np.float64)
            rows.append(co.reshape(-1, 3).astype(np.float64) @ m[:3, :3].T + m[:3, 3])
        finally:
            ev.to_mesh_clear()
    return np.concatenate(rows) if rows else np.zeros((0, 3))


def render_meshes(objs=None):
    """Meshes that actually render: visible, not hidden for render, not bone custom shapes."""
    scene_objs = objs if objs is not None else list(bpy.context.scene.objects)
    shapes = set()
    for a in bpy.data.objects:
        if a.type == "ARMATURE" and a.pose:
            for pb in a.pose.bones:
                if pb.custom_shape is not None:
                    shapes.add(pb.custom_shape.name)
    out = []
    for o in scene_objs:
        if o.type != "MESH" or o.hide_render or o.name in shapes:
            continue
        try:
            if not o.visible_get():
                continue
        except Exception:
            pass
        out.append(o)
    return out


def grade_materials(meshes, saturation=1.1, rough_min=0.55, metal_max=0.6, hue=0.5, value=1.0):
    """Removes the plastic shine of
    AI textures (roughness floor, metallic cap, specular 0.35) and lifts saturation. Returns the
    number of graded materials. Changes only the in-memory scene."""
    seen = set()
    for o in meshes:
        for slot in o.material_slots:
            m = slot.material
            if m is None or m.name in seen or not m.use_nodes:
                continue
            seen.add(m.name)
            nt = m.node_tree
            for n in list(nt.nodes):
                if n.type != "BSDF_PRINCIPLED":
                    continue
                _clamp_input(nt, n, "Roughness", "MAXIMUM", rough_min)
                _clamp_input(nt, n, "Metallic", "MINIMUM", metal_max)
                if "Specular IOR Level" in n.inputs:
                    n.inputs["Specular IOR Level"].default_value = 0.35
                base = n.inputs["Base Color"]
                if base.is_linked and (saturation != 1.0 or hue != 0.5 or value != 1.0):
                    src = base.links[0].from_socket
                    hsv = nt.nodes.new("ShaderNodeHueSaturation")
                    hsv.inputs["Saturation"].default_value = saturation
                    hsv.inputs["Hue"].default_value = hue
                    hsv.inputs["Value"].default_value = value
                    nt.links.new(src, hsv.inputs["Color"])
                    nt.links.new(hsv.outputs["Color"], base)
    return len(seen)


def _clamp_input(nt, bsdf, name, op, value):
    sock = bsdf.inputs[name]
    if sock.is_linked:
        src = sock.links[0].from_socket
        math_node = nt.nodes.new("ShaderNodeMath")
        math_node.operation = op
        math_node.inputs[1].default_value = value
        nt.links.new(src, math_node.inputs[0])
        nt.links.new(math_node.outputs[0], sock)
    else:
        v = sock.default_value
        sock.default_value = max(v, value) if op == "MAXIMUM" else min(v, value)


# --------------------------------------------------------------------------- camera / lights

def dir_from(elev_deg, az_deg):
    """Unit vector from the origin toward a source: azimuth from +X counter-clockwise."""
    e = math.radians(elev_deg)
    a = math.radians(az_deg)
    return Vector((math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e)))


def build_camera_rig(scene, width, height, anchor, ppm, elevation_deg, extent_m=2.0,
                     light_scale=1.0, lights=True):
    """Camera + light rig parented to an empty at the world origin (orbit it with rotation_euler.z).

    In rig space the camera looks along +Y, tilted down by elevation; the world origin projects
    exactly to `anchor` (pixels from the top-left), 1 m = `ppm` px on the horizontal axis.
    Lights: sun from upper-left-front of the screen, cool
    rim behind-right, sky ambient; they rotate with the camera, so every direction is lit the same.
    """
    w, h = int(width), int(height)
    ax, ay = float(anchor[0]), float(anchor[1])
    rig = bpy.data.objects.new("AF_CAM_RIG", None)
    scene.collection.objects.link(rig)
    cam_data = bpy.data.cameras.new("AF_SPRITE_CAM")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = max(w, h) / float(ppm)
    cam_data.sensor_fit = "AUTO"
    dist = max(50.0, extent_m * 6.0)
    cam_data.clip_start = 0.05
    cam_data.clip_end = dist * 2.0 + extent_m * 4.0
    cam = bpy.data.objects.new("AF_SPRITE_CAM", cam_data)
    scene.collection.objects.link(cam)
    cam.parent = rig
    tilt = math.radians(90.0 - elevation_deg)
    cam.rotation_euler = (tilt, 0.0, 0.0)
    rot = cam.rotation_euler.to_matrix()
    right = rot @ Vector((1, 0, 0))
    up = rot @ Vector((0, 1, 0))
    view = rot @ Vector((0, 0, -1))
    dx = (ax - w / 2.0) / ppm
    dy = (ay - h / 2.0) / ppm
    look = -right * dx + up * dy
    cam.location = look - view * dist
    scene.camera = cam
    made = [rig, cam]
    rim = None
    if lights:
        world = scene.world or bpy.data.worlds.new("AF_World")
        scene.world = world
        world.use_nodes = True
        bg = world.node_tree.nodes.get("Background")
        if bg is not None:
            bg.inputs["Color"].default_value = (*SKY_COLOR, 1.0)
            bg.inputs["Strength"].default_value = SKY_STRENGTH
        sun_data = bpy.data.lights.new("AF_Sun", "SUN")
        sun_data.energy = SUN_STRENGTH
        sun_data.color = SUN_COLOR
        sun_data.angle = math.radians(4.0)
        sun = bpy.data.objects.new("AF_Sun", sun_data)
        scene.collection.objects.link(sun)
        sun.parent = rig
        to_light = dir_from(SUN_ELEVATION_DEG, 225.0)
        sun.rotation_euler = (-to_light).to_track_quat("-Z", "Y").to_euler()
        k = max(1.0, float(light_scale))
        rim_data = bpy.data.lights.new("AF_Rim", "AREA")
        rim_data.energy = 60.0 * k * k
        rim_data.size = 3.0 * k
        rim_data.color = (0.75, 0.85, 1.0)
        rim = bpy.data.objects.new("AF_Rim", rim_data)
        scene.collection.objects.link(rim)
        rim.parent = rig
        target = Vector((0, 0, 1.0 * k))
        rim.location = dir_from(35.0, 45.0) * 5.0 * k + target
        rim.rotation_euler = (target - rim.location).to_track_quat("-Z", "Y").to_euler()
        made += [sun, rim]
    return rig, cam, made, rim


def view_basis(elevation_deg, rig_yaw_rad):
    """World right/up vectors of the sprite camera for a rig yaw (radians)."""
    e = math.radians(elevation_deg)
    c, s = math.cos(rig_yaw_rad), math.sin(rig_yaw_rad)
    right = np.array([c, s, 0.0])
    up0 = np.array([0.0, math.sin(e), math.cos(e)])
    up = np.array([c * up0[0] - s * up0[1], s * up0[0] + c * up0[1], up0[2]])
    return right, up


def sun_to_light(rig_yaw_rad):
    """World direction toward the rig's sun (dir_from(SUN_ELEVATION_DEG, 225) turned with the rig)."""
    d = dir_from(SUN_ELEVATION_DEG, 225.0)
    c, s = math.cos(rig_yaw_rad), math.sin(rig_yaw_rad)
    return np.array([c * d.x - s * d.y, s * d.x + c * d.y, d.z])


def shadow_ground(points, to_light):
    """Cast points along the sun onto z=0 (the shadow catcher plane); points below z=0 stay put."""
    z = np.clip(points[:, 2:3], 0.0, None)
    out = points - z * (to_light / to_light[2])
    out[:, 2] = 0.0
    return out


def project(points, anchor, ppm, right, up):
    """World points -> cell pixel coordinates (x right, y down) for the anchored ortho camera."""
    x = anchor[0] + points @ right * ppm
    y = anchor[1] - points @ up * ppm
    return x, y


def shadow_catcher(scene, rim, rig, size=8.0):
    """Cycles shadow catcher on z=0 with the rim light's own shadow removed."""
    me = bpy.data.meshes.new("AF_ShadowCatcher")
    s = size / 2.0
    me.from_pydata([(-s, -s, 0), (s, -s, 0), (s, s, 0), (-s, s, 0)], [], [(0, 1, 2, 3)])
    plane = bpy.data.objects.new("AF_ShadowCatcher", me)
    scene.collection.objects.link(plane)
    plane.is_shadow_catcher = True
    made = [plane]
    if rim is not None:
        _link_light(rim, [plane], "EXCLUDE", "AF_RimExclude")
        twin = bpy.data.objects.new("AF_RimCatcher", rim.data.copy())
        twin.data.use_shadow = False
        scene.collection.objects.link(twin)
        twin.parent = rig
        twin.matrix_basis = rim.matrix_basis.copy()
        _link_light(twin, [plane], "INCLUDE", "AF_RimCatcherOnly")
        made.append(twin)
    return made


def _link_light(light_obj, objs, state, name):
    coll = bpy.data.collections.get(name) or bpy.data.collections.new(name)
    for o in objs:
        if o.name not in coll.objects:
            coll.objects.link(o)
    light_obj.light_linking.receiver_collection = coll
    for co in coll.collection_objects:
        co.light_linking.link_state = state
    return coll


# --------------------------------------------------------------------------- images

def load_png(path):
    """PNG -> uint8 (H, W, 4), rows top-down (Blender stores bottom-up)."""
    img = bpy.data.images.load(os.path.abspath(path), check_existing=False)
    try:
        w, h = img.size
        buf = np.empty(w * h * 4, dtype=np.float32)
        img.pixels.foreach_get(buf)
    finally:
        bpy.data.images.remove(img)
    arr = np.rint(buf.reshape(h, w, 4) * 255.0).astype(np.uint8)
    return arr[::-1].copy()


def save_png(path, arr):
    """uint8 (H, W, 4) top-down -> PNG (straight alpha, sRGB bytes as given)."""
    arr = np.ascontiguousarray(arr)
    h, w = arr.shape[:2]
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img = bpy.data.images.new("af_png_out", width=w, height=h, alpha=True)
    try:
        img.pixels.foreach_set((arr[::-1].astype(np.float32) / 255.0).ravel())
        img.filepath_raw = path
        img.file_format = "PNG"
        img.save()
    finally:
        bpy.data.images.remove(img)
    return path


def _l_channel(rgb):
    """PIL 'L' conversion (ITU-R 601-2 with PIL's fixed-point rounding)."""
    r, g, b = (rgb[..., i].astype(np.int64) for i in range(3))
    return (r * 19595 + g * 38470 + b * 7471 + 0x8000) >> 16


def grade(arr, saturation=GRADE_SATURATION, contrast=GRADE_CONTRAST):
    """Colour grade: PIL-style ImageEnhance.Color then Contrast on RGB only; alpha untouched."""
    rgb = arr[..., :3].astype(np.float64)
    lum = _l_channel(arr[..., :3]).astype(np.float64)[..., None]
    rgb = np.clip(np.rint(lum + (rgb - lum) * saturation), 0, 255)
    mean = int(_l_channel(rgb.astype(np.uint8)).mean() + 0.5)
    rgb = np.clip(np.rint(mean + (rgb - mean) * contrast), 0, 255)
    out = arr.copy()
    out[..., :3] = rgb.astype(np.uint8)
    return out


def clean_alpha(arr, floor=ALPHA_FLOOR, speck=24):
    """Zero alpha < floor (with colour) and isolated weak specks."""
    if floor <= 0 or arr[..., 3].min() >= floor:
        return arr
    a = arr.copy()
    a[a[..., 3] < floor] = 0
    if speck > floor:
        on = np.pad(a[..., 3] >= floor, 1)
        h, w = a.shape[:2]
        nb = np.zeros((h, w), dtype=np.int32)
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                if dy or dx:
                    nb += on[1 + dy:1 + dy + h, 1 + dx:1 + dx + w]
        a[(a[..., 3] < speck) & (nb == 0)] = 0
    return a


def _shadow_mask(a):
    return 1.0 - np.clip((a[:, :, :3].max(axis=2) - 4.0) / 12.0, 0.0, 1.0)


def clamp_shadow(arr, max_alpha):
    """Cap the alpha of translucent black shadow pixels."""
    if not max_alpha or max_alpha <= 0:
        return arr
    a = arr.astype(np.float32)
    cap = 255.0 * float(max_alpha)
    shadow = _shadow_mask(a) * (a[:, :, 3] < 250.0)
    a[:, :, 3] = a[:, :, 3] * (1.0 - shadow) + np.minimum(a[:, :, 3], cap) * shadow
    return np.clip(a + 0.5, 0, 255).astype(np.uint8)


def fade_shadow_edges(arr, px):
    """Shadow pixels fade out toward the cell border."""
    if px <= 0:
        return arr
    a = arr.astype(np.float32)
    h, w = a.shape[:2]
    yy, xx = np.mgrid[0:h, 0:w]
    dist = np.minimum(np.minimum(xx, w - 1 - xx), np.minimum(yy, h - 1 - yy)).astype(np.float32)
    ramp = np.clip(dist / float(px), 0.0, 1.0)
    ramp = ramp * ramp * (3 - 2 * ramp)
    a[:, :, 3] *= 1.0 - _shadow_mask(a) * (1.0 - ramp)
    return np.clip(a + 0.5, 0, 255).astype(np.uint8)


def body_mask(arr):
    """Visible body pixels excluding black shadow."""
    return (arr[..., 3] >= 24) & ((arr[..., :3].max(axis=2) > 16) | (arr[..., 3] >= 250))


def pixel_support(arr):
    mask = body_mask(arr)
    yy, xx = np.nonzero(mask)
    if not len(xx):
        return {"body_bounds_px": None, "body_border_2px_pixels": 0, "body_pixels": 0}
    border = mask.copy()
    border[2:-2, 2:-2] = False
    return {"body_bounds_px": [int(xx.min()), int(yy.min()), int(xx.max()), int(yy.max())],
            "body_border_2px_pixels": int(border.sum()), "body_pixels": int(mask.sum())}


# --------------------------------------------------------------------------- preview drawing

_FONT = {
    "A": "010101111101101", "B": "110101110101110", "C": "011100100100011", "D": "110101101101110",
    "E": "111100110100111", "F": "111100110100100", "G": "011100101101011", "H": "101101111101101",
    "I": "111010010010111", "J": "001001001101010", "K": "101101110101101", "L": "100100100100111",
    "M": "101111111101101", "N": "110101101101101", "O": "010101101101010", "P": "110101110100100",
    "Q": "010101101110011", "R": "110101110101101", "S": "011100010001110", "T": "111010010010010",
    "U": "101101101101111", "V": "101101101101010", "W": "101101111111101", "X": "101101010101101",
    "Y": "101101010010010", "Z": "111001010100111", "0": "111101101101111", "1": "010110010010111",
    "2": "110001010100111", "3": "110001010001110", "4": "101101111001001", "5": "111100110001110",
    "6": "011100111101111", "7": "111001010010010", "8": "111101111101111", "9": "111101111001110",
    "_": "000000000000111", "-": "000000111000000", ".": "000000000000010", ":": "000010000010000",
    "/": "001001010100100", "(": "010100100100010", ")": "010001001001010", " ": "000000000000000",
    "=": "000111000111000", "+": "000010111010000",
}


def draw_text(img, x, y, text, scale=2, color=(20, 20, 20, 255)):
    """Draw upper-case text with the 3x5 font into an RGBA uint8 image (in place)."""
    h, w = img.shape[:2]
    cx = int(x)
    for ch in str(text).upper():
        glyph = _FONT.get(ch, _FONT[" "])
        for i, bit in enumerate(glyph):
            if bit != "1":
                continue
            gx, gy = cx + (i % 3) * scale, int(y) + (i // 3) * scale
            x0, x1 = max(gx, 0), min(gx + scale, w)
            y0, y1 = max(gy, 0), min(gy + scale, h)
            if x0 < x1 and y0 < y1:
                img[y0:y1, x0:x1] = color
        cx += 4 * scale
    return img


def checker(w, h, s=16, c0=200, c1=160):
    yy, xx = np.mgrid[0:h, 0:w]
    v = np.where(((xx // s) + (yy // s)) % 2 == 1, c1, c0).astype(np.uint8)
    out = np.empty((h, w, 4), dtype=np.uint8)
    out[..., 0] = out[..., 1] = out[..., 2] = v
    out[..., 3] = 255
    return out


def alpha_over(bg, fg):
    """Composite straight-alpha fg over opaque bg (same size), returns opaque uint8."""
    a = fg[..., 3:4].astype(np.float32) / 255.0
    rgb = fg[..., :3].astype(np.float32) * a + bg[..., :3].astype(np.float32) * (1.0 - a)
    out = bg.copy()
    out[..., :3] = np.clip(np.rint(rgb), 0, 255).astype(np.uint8)
    return out


def draw_cross(img, x, y, r=8, color=(255, 0, 0, 255)):
    h, w = img.shape[:2]
    x, y = int(round(x)), int(round(y))
    if 0 <= y < h:
        img[y, max(x - r, 0):min(x + r + 1, w)] = color
    if 0 <= x < w:
        img[max(y - r, 0):min(y + r + 1, h), x] = color
    return img


def draw_rect(img, x0, y0, x1, y1, color=(90, 90, 255, 255)):
    h, w = img.shape[:2]
    x0, y0 = max(int(x0), 0), max(int(y0), 0)
    x1, y1 = min(int(x1), w - 1), min(int(y1), h - 1)
    if x0 > x1 or y0 > y1:
        return img
    img[y0, x0:x1 + 1] = color
    img[y1, x0:x1 + 1] = color
    img[y0:y1 + 1, x0] = color
    img[y0:y1 + 1, x1] = color
    return img


def downscale(img, max_w):
    """Box-filter downscale by an integer factor so the width fits max_w."""
    h, w = img.shape[:2]
    k = max(1, int(math.ceil(w / float(max_w))))
    if k == 1:
        return img
    ph, pw = (-h) % k, (-w) % k
    pad = np.pad(img, ((0, ph), (0, pw), (0, 0)), mode="edge").astype(np.float32)
    H, W = pad.shape[0] // k, pad.shape[1] // k
    out = pad.reshape(H, k, W, k, 4).mean(axis=(1, 3))
    return np.clip(np.rint(out), 0, 255).astype(np.uint8)


def tile_preview(cells, cols, cell_w, cell_h, labels=None, anchor=None, title=None, label_h=14):
    """Grid of RGBA cells on checker background with optional anchor cross and labels."""
    rows = max(1, int(math.ceil(len(cells) / float(cols))))
    top = 18 if title else 0
    out = np.full((top + rows * (cell_h + label_h), cols * cell_w, 4), 255, dtype=np.uint8)
    if title:
        draw_text(out, 4, 3, title, scale=2)
    for i, cell in enumerate(cells):
        r, c = divmod(i, cols)
        x0, y0 = c * cell_w, top + r * (cell_h + label_h) + label_h
        tile = checker(cell_w, cell_h)
        if cell is not None:
            tile = alpha_over(tile, cell)
        if anchor is not None:
            draw_cross(tile, anchor[0], anchor[1])
        draw_rect(tile, 0, 0, cell_w - 1, cell_h - 1, (120, 120, 120, 255))
        out[y0:y0 + cell_h, x0:x0 + cell_w] = tile
        if labels and i < len(labels):
            draw_text(out, x0 + 3, y0 - label_h + 3, labels[i], scale=2)
    return out


# --------------------------------------------------------------------------- Godot resources

def _f(v):
    v = float(v)
    if abs(v - round(v)) < 1e-9:
        return "%d" % int(round(v)) if abs(v) < 1e15 else repr(v)
    return ("%.6f" % v).rstrip("0").rstrip(".")


def gd_vec3(v):
    return "Vector3(%s, %s, %s)" % tuple(_f(x) for x in v)


def gd_vec2(v):
    return "Vector2(%s, %s)" % tuple(_f(x) for x in v)


def gd_color(c):
    c = list(c) + [1.0] * (4 - len(c))
    return "Color(%s, %s, %s, %s)" % tuple(_f(x) for x in c[:4])


def write_spriteframes(path, textures, animations):
    """Godot 4 SpriteFrames .tres.

    textures: {key: relative path from the .tres}; animations: [{name, loop, speed,
    frames: [(texture_key, x, y, w, h), ...]}]. Paths are relative so the folder can be moved.
    """
    keys = list(textures)
    ext_id = {k: "%d_%s" % (i + 1, safe_name(k)[:24]) for i, k in enumerate(keys)}
    subs = []
    anim_txt = []
    n = 0
    for anim in animations:
        frames_txt = []
        for (key, x, y, w, h) in anim["frames"]:
            sid = "AtlasTexture_%d" % n
            n += 1
            subs.append('[sub_resource type="AtlasTexture" id="%s"]\natlas = ExtResource("%s")\n'
                        'region = Rect2(%d, %d, %d, %d)\n' % (sid, ext_id[key], x, y, w, h))
            frames_txt.append('{\n"duration": 1.0,\n"texture": SubResource("%s")\n}' % sid)
        anim_txt.append('{\n"frames": [%s],\n"loop": %s,\n"name": &"%s",\n"speed": %s\n}'
                        % (", ".join(frames_txt), "true" if anim["loop"] else "false",
                           anim["name"], repr(float(anim["speed"]))))
    lines = ['[gd_resource type="SpriteFrames" load_steps=%d format=3]\n' % (len(keys) + len(subs) + 1)]
    for k in keys:
        lines.append('[ext_resource type="Texture2D" path="%s" id="%s"]\n' % (textures[k], ext_id[k]))
    lines.extend(subs)
    lines.append("[resource]\nanimations = [%s]\n" % ", ".join(anim_txt))
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines))
    return path


def make_fx_texture(kind, size=64):
    """Procedural white RGBA particle textures (alpha carries the shape)."""
    yy, xx = np.mgrid[0:size, 0:size].astype(np.float32)
    c = (size - 1) / 2.0
    dx, dy = (xx - c) / c, (yy - c) / c
    r = np.sqrt(dx * dx + dy * dy)
    if kind == "soft":
        a = np.clip(1.0 - r, 0, 1) ** 2
    elif kind == "spark":
        a = np.clip(1.0 - r, 0, 1) ** 4 + 0.6 * np.exp(-(r * 6.0) ** 2)
    elif kind == "star":
        cross = np.exp(-(dx * 9.0) ** 2) * np.clip(1 - np.abs(dy), 0, 1) ** 2 \
            + np.exp(-(dy * 9.0) ** 2) * np.clip(1 - np.abs(dx), 0, 1) ** 2
        a = np.clip(cross + np.clip(1.0 - r * 1.6, 0, 1) ** 2, 0, 1)
    elif kind == "smoke":
        rng = np.random.default_rng(7)
        noise = np.zeros((size, size), np.float32)
        for octave, amp in ((4, 0.5), (8, 0.3), (16, 0.2)):
            g = rng.random((octave + 1, octave + 1)).astype(np.float32)
            gy = np.linspace(0, octave, size)
            gx = np.linspace(0, octave, size)
            i0 = np.floor(gy).astype(int).clip(0, octave - 1)
            j0 = np.floor(gx).astype(int).clip(0, octave - 1)
            fy = (gy - i0)[:, None]
            fx = (gx - j0)[None, :]
            v = (g[i0][:, j0] * (1 - fy) * (1 - fx) + g[i0 + 1][:, j0] * fy * (1 - fx)
                 + g[i0][:, j0 + 1] * (1 - fy) * fx + g[i0 + 1][:, j0 + 1] * fy * fx)
            noise += amp * v
        a = np.clip(1.0 - r, 0, 1) ** 1.5 * np.clip(0.35 + noise, 0, 1)
    elif kind == "ring":
        a = np.exp(-((r - 0.75) * 7.0) ** 2) * (r < 1.0)
    else:
        raise ValueError("unknown fx texture kind %r" % kind)
    a = np.clip(a, 0, 1)
    out = np.empty((size, size, 4), dtype=np.uint8)
    out[..., :3] = 255
    out[..., 3] = np.rint(a * 255).astype(np.uint8)
    return out


EMISSION_SHAPES = {"point": 0, "sphere": 1, "sphere_surface": 2, "box": 3, "ring": 6}
BLEND_MODES = {"mix": 0, "add": 1, "sub": 2, "mul": 3, "premult": 4}
DEFAULT_VISIBILITY_AABB = [-4, -4, -4, 8, 8, 8]
# Representative seat diameter (m) of a mount size class: middle of the class range
# (S <= 1.5, M <= 3, L <= 6 m); used to scale socket-sized FX presets.
SIZE_CLASS_SEAT_M = {"S": 1.0, "M": 2.25, "L": 4.5}


def _sig(v, digits=3):
    return float("%.*g" % (digits, v))


def fx_auto_scale(preset, socket=None, asset_longest_m=None):
    """Scale factor of a preset for one binding when params.scale is not given: (k, source).

    Presets are metric and tuned for a reference: `reference_socket_m` (seat/nozzle diameter, used when
    the socket or a parent socket carries mount_d_m / diameter_m or a size_class) and `reference_size_m`
    (the asset's longest axis). Without either, k = 1.
    """
    socket = socket or {}
    ref_sock = preset.get("reference_socket_m")
    if ref_sock:
        d = socket.get("diameter_m")
        if d:
            return _sig(float(d) / float(ref_sock)), "socket diameter %.3g m / preset %.3g m" % (float(d), ref_sock)
        sc = socket.get("size_class")
        if sc in SIZE_CLASS_SEAT_M:
            seat = SIZE_CLASS_SEAT_M[sc]
            return _sig(seat / float(ref_sock)), "size_class %s (seat %.3g m) / preset %.3g m%s" % (
                sc, seat, ref_sock, " (from %s)" % socket["size_class_from"] if socket.get("size_class_from") else "")
    ref_size = preset.get("reference_size_m")
    if ref_size and asset_longest_m and asset_longest_m > 1e-6:
        return _sig(float(asset_longest_m) / float(ref_size)), "asset longest axis %.3g m / preset %.3g m" % (
            asset_longest_m, ref_size)
    return 1.0, "preset native size"


def fx_scale_problem(preset, k, asset_longest_m, quad_m=None, mount_scaled=False):
    """Why a scaled preset looks wrong next to the asset (None when fine): particle quad under 1 % of the
    asset's longest axis, or quad/asset ratio 4x away from the preset's design ratio. quad_m = the final
    quad size when the binding overrides it. mount_scaled = k came from the socket's own diameter /
    size_class: the effect is sized to its mount (a gun, a nozzle), so only the 1 % visibility floor applies."""
    if not asset_longest_m or asset_longest_m <= 1e-6:
        return None
    quad = float(quad_m) if quad_m is not None else float(preset.get("quad_size", 0.2)) * k
    ratio = quad / asset_longest_m
    if ratio < 0.01:
        return "particle quad %.3g m is %.2g %% of the asset (%.3g m)" % (quad, ratio * 100, asset_longest_m)
    ref = preset.get("reference_size_m")
    if ref and not mount_scaled:
        design = float(preset.get("quad_size", 0.2)) / float(ref)
        rel = ratio / design
        if rel < 0.25 or rel > 4.0:
            return "particle quad %.3g m vs asset %.3g m is %.2gx the preset's design ratio" % (
                quad, asset_longest_m, rel)
    return None


def merge_fx(preset, overrides, scale=None):
    """Preset dict + binding params. Supports 'scale' (every length: quad, velocities, accelerations,
    damping, gravity, emission shape, light range, visibility AABB), 'amount', 'lifetime', 'color' (RGB
    tint of the ramp), 'speed_scale', and any raw preset key. `scale` is used when the binding params
    have no 'scale' (godot_package passes the auto scale here)."""
    p = json.loads(json.dumps(preset))
    o = dict(overrides or {})
    k = float(o.pop("scale", scale if scale is not None else 1.0))
    if k <= 0:
        raise ValueError("fx scale must be > 0: %r" % k)
    if k != 1.0:
        p["quad_size"] = float(p.get("quad_size", 0.2)) * k
        for key in ("velocity", "damping", "tangential_accel", "radial_accel"):
            if key in p:
                p[key] = [float(v) * k for v in p[key]]
        p["gravity"] = [float(v) * k for v in p.get("gravity", [0, 0, 0])]
        em = p.setdefault("emission", {"shape": "point"})
        for key in ("radius", "inner_radius", "height"):
            if key in em:
                em[key] = float(em[key]) * k
        if "extents" in em:
            em["extents"] = [float(v) * k for v in em["extents"]]
        if p.get("light"):
            p["light"]["range"] = float(p["light"].get("range", 1.0)) * k
        p["visibility_aabb"] = [float(v) * k for v in p.get("visibility_aabb", DEFAULT_VISIBILITY_AABB)]
    p["_scale"] = k
    tint = o.pop("color", None)
    if tint is not None:
        ramp = []
        for t, col in p.get("color_ramp", []):
            col = list(col) + [1.0] * (4 - len(col))
            ramp.append([t, [col[0] * tint[0], col[1] * tint[1], col[2] * tint[2], col[3]]])
        p["color_ramp"] = ramp
        if p.get("light"):
            lc = p["light"].get("color", [1, 1, 1])
            p["light"]["color"] = [lc[0] * tint[0], lc[1] * tint[1], lc[2] * tint[2]]
    p.update(o)
    return p


def fx_scene_text(node_name, p, texture_rel):
    """GPUParticles3D + ParticleProcessMaterial + QuadMesh/StandardMaterial3D (+ optional OmniLight3D).

    The FX emits along its local +Y: a socket's local +Y in Godot is the Blender socket's +Z,
    i.e. 'out' (exhaust, outward normal, blade direction) in the AstraForge convention.
    """
    subs = []
    ramp = p.get("color_ramp") or [[0, [1, 1, 1, 1]], [1, [1, 1, 1, 0]]]
    offs = ", ".join(_f(t) for t, _ in ramp)
    cols = ", ".join(", ".join(_f(x) for x in (list(c) + [1.0] * (4 - len(c)))[:4]) for _, c in ramp)
    subs.append('[sub_resource type="Gradient" id="Gradient_ramp"]\noffsets = PackedFloat32Array(%s)\n'
                'colors = PackedColorArray(%s)\n' % (offs, cols))
    subs.append('[sub_resource type="GradientTexture1D" id="GradientTexture1D_ramp"]\n'
                'gradient = SubResource("Gradient_ramp")\n')
    curve = p.get("scale_curve") or [[0, 1], [1, 1]]
    pts = ", ".join("Vector2(%s, %s), 0.0, 0.0, 0, 0" % (_f(x), _f(y)) for x, y in curve)
    cmax = max(1.0, max(float(y) for _, y in curve))
    subs.append('[sub_resource type="Curve" id="Curve_scale"]\nmax_value = %s\n_data = [%s]\npoint_count = %d\n'
                % (_f(cmax), pts, len(curve)))
    subs.append('[sub_resource type="CurveTexture" id="CurveTexture_scale"]\ncurve = SubResource("Curve_scale")\n')
    em = p.get("emission", {"shape": "point"})
    shape = EMISSION_SHAPES[em.get("shape", "point")]
    m = ['[sub_resource type="ParticleProcessMaterial" id="ParticleProcessMaterial_fx"]']
    m.append("particle_flag_disable_z = false")
    m.append("emission_shape = %d" % shape)
    if shape in (1, 2):
        m.append("emission_sphere_radius = %s" % _f(em.get("radius", 0.1)))
    elif shape == 3:
        m.append("emission_box_extents = %s" % gd_vec3(em.get("extents", [0.1, 0.1, 0.1])))
    elif shape == 6:
        m.append("emission_ring_axis = %s" % gd_vec3(em.get("axis", [0, 1, 0])))
        m.append("emission_ring_height = %s" % _f(em.get("height", 0.0)))
        m.append("emission_ring_radius = %s" % _f(em.get("radius", 0.5)))
        m.append("emission_ring_inner_radius = %s" % _f(em.get("inner_radius", 0.0)))
    m.append("direction = %s" % gd_vec3(p.get("direction", [0, 1, 0])))
    m.append("spread = %s" % _f(p.get("spread", 15)))
    vel = p.get("velocity", [0, 0])
    m.append("initial_velocity_min = %s" % _f(vel[0]))
    m.append("initial_velocity_max = %s" % _f(vel[1]))
    m.append("gravity = %s" % gd_vec3(p.get("gravity", [0, 0, 0])))
    damp = p.get("damping", [0, 0])
    m.append("damping_min = %s" % _f(damp[0]))
    m.append("damping_max = %s" % _f(damp[1]))
    tan = p.get("tangential_accel", [0, 0])
    m.append("tangential_accel_min = %s" % _f(tan[0]))
    m.append("tangential_accel_max = %s" % _f(tan[1]))
    rad = p.get("radial_accel", [0, 0])
    m.append("radial_accel_min = %s" % _f(rad[0]))
    m.append("radial_accel_max = %s" % _f(rad[1]))
    ang = p.get("angle", [0, 0])
    m.append("angle_min = %s" % _f(ang[0]))
    m.append("angle_max = %s" % _f(ang[1]))
    angv = p.get("angular_velocity", [0, 0])
    m.append("angular_velocity_min = %s" % _f(angv[0]))
    m.append("angular_velocity_max = %s" % _f(angv[1]))
    sc = p.get("scale", [1, 1])
    m.append("scale_min = %s" % _f(sc[0]))
    m.append("scale_max = %s" % _f(sc[1]))
    m.append('scale_curve = SubResource("CurveTexture_scale")')
    m.append('color_ramp = SubResource("GradientTexture1D_ramp")')
    hue = p.get("hue_variation", [0, 0])
    m.append("hue_variation_min = %s" % _f(hue[0]))
    m.append("hue_variation_max = %s" % _f(hue[1]))
    if p.get("turbulence"):
        m.append("turbulence_enabled = true")
        m.append("turbulence_noise_strength = %s" % _f(p["turbulence"]))
    subs.append("\n".join(m) + "\n")
    mat = ['[sub_resource type="StandardMaterial3D" id="StandardMaterial3D_fx"]',
           "transparency = 1",
           "blend_mode = %d" % BLEND_MODES[p.get("blend", "add")],
           "cull_mode = 2",
           "shading_mode = 0",
           "vertex_color_use_as_albedo = true",
           "albedo_color = %s" % gd_color(p.get("albedo", [1, 1, 1, 1])),
           'albedo_texture = ExtResource("1_tex")',
           "billboard_mode = 3",
           "billboard_keep_scale = true"]
    subs.append("\n".join(mat) + "\n")
    q = float(p.get("quad_size", 0.2))
    subs.append('[sub_resource type="QuadMesh" id="QuadMesh_fx"]\nmaterial = SubResource("StandardMaterial3D_fx")\n'
                'size = Vector2(%s, %s)\n' % (_f(q), _f(q)))
    node = ['[node name="%s" type="GPUParticles3D"]' % node_name,
            "emitting = %s" % ("true" if p.get("autostart", True) else "false"),
            "amount = %d" % int(p.get("amount", 16)),
            "lifetime = %s" % _f(p.get("lifetime", 1.0)),
            "one_shot = %s" % ("true" if p.get("one_shot") else "false"),
            "explosiveness = %s" % _f(p.get("explosiveness", 0.0)),
            "randomness = %s" % _f(p.get("randomness", 0.0)),
            "speed_scale = %s" % _f(p.get("speed_scale", 1.0)),
            "local_coords = %s" % ("true" if p.get("local_coords", False) else "false"),
            "fixed_fps = %d" % int(p.get("fixed_fps", 30)),
            "visibility_aabb = AABB(%s)" % ", ".join(_f(x) for x in p.get("visibility_aabb", DEFAULT_VISIBILITY_AABB)),
            'process_material = SubResource("ParticleProcessMaterial_fx")',
            'draw_pass_1 = SubResource("QuadMesh_fx")']
    meta = {"preset": p.get("_preset", node_name), "astraforge_fx": True}
    node.append("metadata/astraforge_fx = %s" % json.dumps(meta["preset"]))
    body = "\n".join(node) + "\n"
    light = p.get("light")
    if light:
        body += ('\n[node name="light" type="OmniLight3D" parent="."]\nlight_color = %s\nlight_energy = %s\n'
                 'omni_range = %s\nshadow_enabled = false\n'
                 % (gd_color(light.get("color", [1, 1, 1])), _f(light.get("energy", 1.0)),
                    _f(light.get("range", 1.0))))
    head = '[gd_scene load_steps=%d format=3]\n\n[ext_resource type="Texture2D" path="%s" id="1_tex"]\n' \
        % (len(subs) + 2, texture_rel)
    return head + "\n" + "\n".join(subs) + "\n" + body


# --------------------------------------------------------------------------- Godot runner

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def find_godot(params=None):
    params = params or {}
    for cand in (params.get("godot"), os.environ.get("ASTRA_GODOT"), GODOT_DEFAULT):
        if cand and os.path.isfile(cand):
            return cand
    return None


GODOT_HOME = "_home"


def make_check_project(root, name="af_godot_check"):
    """Throwaway Godot 4.5 project (GL Compatibility) with the validator script.

    Nothing may land outside out_dir (contract §3): file logging and the shader cache are off, and
    run_godot points APPDATA/LOCALAPPDATA of the Godot process at <root>/_home (ignored by the import
    via .gdignore), so editor settings and user:// stay inside the throwaway project.
    """
    if os.path.isdir(root):
        shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root, exist_ok=True)
    with open(os.path.join(root, "project.godot"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write('; Engine configuration file.\nconfig_version=5\n\n[application]\n\n'
                 'config/name="%s"\nconfig/features=PackedStringArray("4.5", "GL Compatibility")\n\n'
                 '[debug]\n\nfile_logging/enable_file_logging=false\n'
                 'file_logging/enable_file_logging.pc=false\n\n'
                 '[rendering]\n\nrenderer/rendering_method="gl_compatibility"\n'
                 'renderer/rendering_method.mobile="gl_compatibility"\n'
                 'shader_compiler/shader_cache/enabled=false\n' % name)
    shutil.copy2(os.path.join(GODOT_DIR, "validate_package.gd"), os.path.join(root, "validate_package.gd"))
    home = os.path.join(root, GODOT_HOME)
    for sub in ("Roaming", "Local"):
        os.makedirs(os.path.join(home, sub), exist_ok=True)
    open(os.path.join(home, ".gdignore"), "w").close()
    return root


def godot_env(proj):
    """Environment for a Godot child process with its config/data/cache dirs inside the project."""
    env = dict(os.environ)
    home = os.path.join(proj, GODOT_HOME)
    if os.path.isdir(home):
        env["APPDATA"] = os.path.join(home, "Roaming")
        env["LOCALAPPDATA"] = os.path.join(home, "Local")
    return env


def run_godot(exe, proj, args, timeout=300):
    cmd = [exe, "--headless", "--path", proj] + list(args)
    t0 = time.time()
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=proj, env=godot_env(proj))
        out = (cp.stdout or b"").decode("utf-8", "replace") + "\n" + (cp.stderr or b"").decode("utf-8", "replace")
        code = cp.returncode
    except subprocess.TimeoutExpired as exc:
        out = ((exc.stdout or b"").decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else "") \
            + "\nTIMEOUT after %ss" % timeout
        code = -1
    return code, ANSI.sub("", out), round(time.time() - t0, 2)


def godot_validate(exe, proj, config, timeout=300):
    """Import the throwaway project, run validate_package.gd, parse its AFGODOT line."""
    with open(os.path.join(proj, "af_check.json"), "w", encoding="utf-8") as fh:
        json.dump(config, fh, ensure_ascii=False, indent=2)
    code_i, out_i, sec_i = run_godot(exe, proj, ["--import"], timeout)
    code_v, out_v, sec_v = run_godot(exe, proj, ["--script", "res://validate_package.gd"], timeout)
    result = None
    for line in out_v.splitlines():
        if line.startswith("AFGODOT "):
            try:
                result = json.loads(line[len("AFGODOT "):])
            except ValueError:
                result = None
    errs = [ln.strip() for ln in (out_i + "\n" + out_v).splitlines()
            if re.match(r"\s*(ERROR|SCRIPT ERROR|USER ERROR|Parse Error)", ln)]
    log_path = os.path.join(proj, "godot_log.txt")
    with open(log_path, "w", encoding="utf-8") as fh:
        fh.write("== --import (exit %s, %ss)\n%s\n== --script (exit %s, %ss)\n%s\n"
                 % (code_i, sec_i, out_i, code_v, sec_v, out_v))
    version = None
    m = re.search(r"Godot Engine v([0-9][^ ]*)", out_v)
    if m:
        version = m.group(1)
    return {"result": result, "engine_errors": errs, "exit_import": code_i, "exit_script": code_v,
            "seconds": round(sec_i + sec_v, 2), "log": log_path, "godot_version": version}


def file_sha256(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def godot_fx_preview(exe, proj, scene_res, out_png, speed=0.0, frames=(14, 30, 46), quit_after=50,
                     resolution=(640, 360), elevation_deg=32.0, timeout=180):
    """Render a few frames of the packaged scene with Godot's Movie Maker (GL Compatibility; a short
    windowed game run, headless has no renderer) and tile them into one PNG. speed > 0 flies the
    asset along its -Z (asset sizes per second) so world-space trails stretch behind it."""
    shutil.copy2(os.path.join(GODOT_DIR, "af_preview.gd"), os.path.join(proj, "af_preview.gd"))
    scene = ('[gd_scene load_steps=4 format=3]\n\n'
             '[ext_resource type="PackedScene" path="%s" id="1_asset"]\n'
             '[ext_resource type="Script" path="res://af_preview.gd" id="2_script"]\n\n'
             '[sub_resource type="Environment" id="Environment_1"]\nbackground_mode = 1\n'
             'background_color = Color(0.05, 0.06, 0.09, 1)\nambient_light_source = 2\n'
             'ambient_light_color = Color(0.35, 0.38, 0.45, 1)\n\n'
             '[node name="preview" type="Node3D"]\nscript = ExtResource("2_script")\n'
             'speed = %s\nelevation_deg = %s\n\n'
             '[node name="asset" parent="." instance=ExtResource("1_asset")]\n\n'
             '[node name="cam" type="Camera3D" parent="."]\n\n'
             '[node name="sun" type="DirectionalLight3D" parent="."]\n'
             'transform = Transform3D(0.707, -0.5, 0.5, 0, 0.707, 0.707, -0.707, -0.5, 0.5, 0, 5, 0)\n\n'
             '[node name="env" type="WorldEnvironment" parent="."]\nenvironment = SubResource("Environment_1")\n'
             % (scene_res, _f(speed), _f(elevation_deg)))
    with open(os.path.join(proj, "af_preview.tscn"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(scene)
    movie = os.path.join(proj, "_movie")
    shutil.rmtree(movie, ignore_errors=True)
    os.makedirs(movie)
    cmd = [exe, "--path", proj, "--resolution", "%dx%d" % tuple(resolution), "--fixed-fps", "30",
           "--write-movie", os.path.join(movie, "f.png"), "--quit-after", str(int(quit_after)),
           "res://af_preview.tscn"]
    t0 = time.time()
    try:
        cp = subprocess.run(cmd, capture_output=True, timeout=timeout, cwd=proj, env=godot_env(proj))
        out = ANSI.sub("", (cp.stdout or b"").decode("utf-8", "replace")
                       + (cp.stderr or b"").decode("utf-8", "replace"))
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "timeout", "seconds": timeout}
    tiles = []
    for f in frames:
        path = os.path.join(movie, "f%08d.png" % f)
        if os.path.isfile(path):
            tiles.append(load_png(path))
    if not tiles:
        return {"ok": False, "error": "no frames written", "log": out[-2000:],
                "seconds": round(time.time() - t0, 2)}
    save_png(out_png, downscale(np.concatenate(tiles, axis=1), 1280))
    errs = [ln.strip() for ln in out.splitlines() if re.match(r"\s*(ERROR|SCRIPT ERROR)", ln)]
    return {"ok": True, "path": out_png, "frames": list(frames), "seconds": round(time.time() - t0, 2),
            "engine_errors": errs}
