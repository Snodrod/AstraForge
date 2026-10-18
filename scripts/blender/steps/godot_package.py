"""godot_package — Godot 4.5 package of a GLB with FX presets at sockets, validated in headless Godot 4.5.1.

inputs.glb       the asset (sockets per contract §6, clips with '-loop' suffix per §7); turret rig pivots
                 (pivot_yaw / pivot_pitch: extras axis + range_degrees) are listed as 'pivots', never as
                 sockets, so the snake_case / socket checks and the Godot socket lookup skip them
inputs.manifest  optional asset.json (kind, fx, clips)
inputs.sprites   optional sprites.json from render_sprites: sheets + SpriteFrames are copied into
                 <asset_id>/sprites/ and validated too
params:
  asset_id       snake_case ASCII (required)
  profile        'godot' (default, the only profile)
  forward_axis   '+Z' keep | '-Z' face Godot look_at (-Z); default = the GLB's own forward
  source_forward '+Z' | '-Z' forward of the GLB as exported. Default from manifest/params kind (ships,
                 props, turrets... -Z; characters, creatures, items +Z — contract §4); without a kind it is
                 guessed (ship sockets thruster/turret/launcher/muzzle -> -Z, else +Z) and a warning says so
  kind           asset kind when there is no manifest (humanoid, creature, item, ship, prop, ...)
  fx             [{"preset": "engine_trail", "socket": "socket_thruster_main_l", "params": {...}}]
                 presets from scripts/blender/data/fx_presets.json (default: manifest.fx). Presets are
                 metric; without params.scale the step scales each binding to the asset (fx_auto_scale):
                 socket diameter (mount_d_m/diameter_m) or size_class S/M/L (seat 1/2.25/4.5 m, also
                 inherited from a parent socket) against the preset's reference_socket_m, else the asset's
                 longest axis against reference_size_m. The scale used is recorded in af_package.json/README.
                 FX nodes emit along their local +Y. On thruster sockets (kind thruster / socket_thruster_*) the socket's
                 exhaust axis is found first (extras.frame thrust|exhaust when it points aft in the imported
                 scene, else the socket-local axis most aligned with aft; a contradicting frame is a warning) and
                 written as binding.emit_axis; af_fx_mount.gd turns the FX +Y onto it. muzzle_flash (emits -Z) and
                 presets with another direction are left alone. Checks fx_thruster_axis (Python) and
                 godot_fx_thruster_aft (Godot) require the result to point aft.
  fx_auto_scale  default true; false keeps presets at their native size unless params.scale is given
  autoplay       animation played on _ready by the scene script ("" = none)
  validate       run headless Godot 4.5.1 (default true); godot: path override
  fx_preview     also render 3 frames with Godot Movie Maker into fx_preview.png (default false: it
                 opens a Godot game window for ~3 s); fx_preview_speed: asset sizes/s along -Z;
                 fx_preview_frames: frame numbers at 30 fps (default [14, 30, 46])
outputs: package (folder), glb, tscn, script, fx scenes, readme, manifest,
godot_check (json + log). checks include the validator's JSON ('godot_validation').
"""
import json
import os
import re
import shutil

import bpy
from mathutils import Vector

import af_common as af
from steps import _export_lib as xl

SOCKET_RE = re.compile(r"^socket_[a-z0-9]+(_[a-z0-9]+)*$")
SHIP_KINDS = xl.SHIP_KINDS
DIAMETER_KEYS = ("diameter_m", "mount_d_m", "nozzle_d_m")

# Thruster sockets (contract §6): where the exhaust goes in the socket's own glTF/Godot axes. The ship steps write
# one of two documented frames in extras.frame (_ship_lib.THRUSTER_FRAMES / THRUSTER_AXIS_TEXT); GLBs without
# extras.frame are resolved on the geometry. An FX node emits along its local +Y, so af_fx_mount.gd turns
# it onto this axis (binding "emit_axis").
THRUSTER_FRAME_AXES = {"thrust": (0.0, 0.0, 1.0), "exhaust": (0.0, 0.0, -1.0)}
# the six socket-local axes in glTF terms; order = preference on an exact tie (documented frames first)
LOCAL_AXES = (("+Z", (0.0, 0.0, 1.0)), ("-Z", (0.0, 0.0, -1.0)), ("+Y", (0.0, 1.0, 0.0)),
              ("-Y", (0.0, -1.0, 0.0)), ("+X", (1.0, 0.0, 0.0)), ("-X", (-1.0, 0.0, 0.0)))
AFT_MIN_DOT = 0.5          # the exhaust axis must lie within 60 deg of aft

def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _inspect(glb):
    """Import the GLB into an empty scene and list sockets, clips, size."""
    af.reset_scene()
    objs = af.import_any(glb)
    sockets, pivots = [], []
    for o in objs:
        gs = o.get("game_socket")
        if xl.is_rig_pivot(o.name, gs):
            # turret rig pivot (contract §6): extras axis + range_degrees, not a socket
            rd = o.get("range_degrees")
            pivots.append({"node": o.name, "axis": o.get("axis"),
                           "range_degrees": [float(x) for x in rd] if rd is not None else None,
                           "parent": o.parent.name if o.parent else None, "has_game_socket": gs is not None})
            continue
        if gs is None and not o.name.startswith("socket_"):
            continue
        sc = o.get("size_class")
        sc = str(sc) if sc is not None else None
        sc_from = None
        diam = next((_num(o.get(k)) for k in DIAMETER_KEYS if _num(o.get(k))), None)
        par = o.parent
        while par is not None and (sc is None or diam is None):
            # turret rigs: socket_mount (size_class, mount_d_m) -> pivot_yaw -> pivot_pitch -> socket_muzzle_*
            if sc is None and par.get("size_class") is not None:
                sc, sc_from = str(par.get("size_class")), str(par.get("game_socket") or par.name)
            if diam is None:
                diam = next((_num(par.get(k)) for k in DIAMETER_KEYS if _num(par.get(k))), None)
            par = par.parent
        frame = o.get("frame")
        sockets.append({"name": str(gs or o.name), "node": o.name, "kind": o.get("kind"),
                        "size_class": sc, "size_class_from": sc_from, "diameter_m": diam, "type": o.type,
                        "parent": o.parent.name if o.parent else None,
                        "parent_bone": o.parent_bone if o.parent_type == "BONE" else None,
                        "has_game_socket": gs is not None,
                        "frame": str(frame) if frame is not None else None})
    clips = []
    for a in bpy.data.actions:
        if not xl.fcurves_of(a):
            continue
        base, loop = xl.clip_base(a.name)
        # name in the packaged GLB: Godot 4.5 renames 'x_cycle-loop' twice on import (error in its log);
        # the package carries a name it imports cleanly as the same 'base' (godot_safe_clip_name)
        clips.append({"name": a.name, "base": base, "loop": loop, "package_name": xl.godot_safe_clip_name(a.name),
                      "frames": [round(a.frame_range[0], 3), round(a.frame_range[1], 3)]})
    shown = xl.render_meshes(objs)          # excludes the importer's bone-shape Icosphere
    stats = af.scene_stats(shown)
    # size in the rest pose (a hunched idle frame would under-measure a character), from deformed
    # vertices rather than object bound boxes
    arms = [o for o in objs if o.type == "ARMATURE"]
    saved = [(a, a.data.pose_position) for a in arms]
    for a in arms:
        a.data.pose_position = "REST"
    bpy.context.view_layer.update()
    pts = xl.world_points(shown)
    # world rotation of the thruster sockets (rest pose) for the exhaust axis (resolve_thruster_axes)
    world3 = {s["node"]: bpy.data.objects[s["node"]].matrix_world.to_3x3().copy()
              for s in sockets if is_thruster_socket(s)}
    for a, pos in saved:
        a.data.pose_position = pos
    bpy.context.view_layer.update()
    size = (pts.max(axis=0) - pts.min(axis=0)).tolist() if len(pts) else [0.0, 0.0, 0.0]
    return {"sockets": sockets, "pivots": pivots, "socket_world3": world3, "clips": clips,
            "armatures": af.scene_stats(objs)["armatures"],
            "tris": stats["tris"], "size_blender_m": [round(v, 4) for v in size],
            "size_gltf_m": [round(size[0], 4), round(size[2], 4), round(size[1], 4)],
            "longest_m": round(max(size), 4) if len(pts) else 0.0, "meshes": stats["meshes"]}


def is_thruster_socket(s):
    """Sockets whose FX must go out of the nozzle: kind 'thruster' or the name socket_thruster_*."""
    return s.get("kind") == "thruster" or str(s.get("name", "")).startswith("socket_thruster_")


def _axis_world(m3, v):
    """Blender-world unit direction of the socket-local glTF vector v. The importer maps a glTF vector (x, y, z)
    to the Blender one (x, -z, y), so glTF +X = Blender +X, +Y = Blender +Z, +Z = Blender -Y of the same node."""
    w = m3 @ Vector((v[0], -v[2], v[1]))
    return w.normalized() if w.length > 1e-9 else w


def _axis_txt(v):
    n = {(1, 0, 0): "+X", (-1, 0, 0): "-X", (0, 1, 0): "+Y", (0, -1, 0): "-Y", (0, 0, 1): "+Z", (0, 0, -1): "-Z"}
    return n.get(tuple(int(round(c)) for c in v), str(list(v)))


def resolve_thruster_axes(info, src_fwd, warnings):
    """Exhaust axis of every thruster socket in its own glTF/Godot axes (socket keys exhaust_axis,
    exhaust_axis_from, exhaust_aft_dot). The documented frame (extras.frame -> THRUSTER_FRAME_AXES) is used when
    its axis really points aft in the imported scene (aft = Blender -Y for a -Z-forward GLB, +Y for +Z); with no
    frame, an unknown one or a contradicting one, the socket-local axis (+-X, +-Y, +-Z) most aligned with aft is
    taken, and a documented frame that lost to the geometry is reported as a warning."""
    aft = Vector((0.0, -1.0, 0.0)) if src_fwd == "-Z" else Vector((0.0, 1.0, 0.0))
    overridden = []
    for s in info["sockets"]:
        if not is_thruster_socket(s):
            continue
        m3 = info["socket_world3"].get(s["node"])
        if m3 is None:
            continue
        frame = s.get("frame")
        axis, src, dot = None, None, None
        if frame in THRUSTER_FRAME_AXES:
            cand = THRUSTER_FRAME_AXES[frame]
            d = _axis_world(m3, cand).dot(aft)
            if d >= AFT_MIN_DOT:
                axis, src, dot = cand, "extras.frame %s" % frame, d
        if axis is None:
            ranked = [(_axis_world(m3, v).dot(aft), v) for _n, v in LOCAL_AXES]
            best = max(d for d, _v in ranked)
            dot, axis = next(r for r in ranked if r[0] >= best - 1e-6)         # first in LOCAL_AXES order on a tie
            src = "inferred: most aft-pointing local axis %s" % _axis_txt(axis)
            if frame in THRUSTER_FRAME_AXES:
                src = "extras.frame %s contradicts geometry; %s" % (frame, src)
                overridden.append("%s (%s -> %s)" % (s["name"], frame, _axis_txt(axis)))
            elif frame is not None:
                src = "extras.frame %r unknown; %s" % (frame, src)
        s["exhaust_axis"] = [float(c) + 0.0 for c in axis]
        s["exhaust_axis_from"] = src
        s["exhaust_aft_dot"] = round(float(dot), 4)
    if overridden:
        warnings.append("thruster sockets whose documented extras.frame contradicts the geometry (its exhaust axis "
                        "does not point aft); the most aft-pointing local axis is used for the FX instead: %s"
                        % "; ".join(overridden[:6]))


def _fx_spec(b, aft_root):
    """Entry of af_check.json fx[]: socket, node name and, for thruster FX turned by emit_axis, the expected
    +Y axis of the FX node in the package root's space (aft) with the dot-product floor of validate_package.gd."""
    spec = {"socket": b["socket"], "name": b["node_name"]}
    if b.get("emit_axis"):
        spec["expect_axis"] = list(aft_root)
        spec["expect_dot"] = round(min(0.9, max(AFT_MIN_DOT, float(b["emit_aft_dot"]) - 0.05)), 3)
    return spec


def _emits_up(direction):
    """True when a preset emits along its local +Y (direction missing or ~(0, 1, 0)) - the presets whose FX
    node must be turned onto the socket's exhaust axis. muzzle_flash emits along -Z and is left alone."""
    v = Vector(direction if direction is not None else (0.0, 1.0, 0.0))
    return v.length > 1e-9 and v.normalized().dot(Vector((0.0, 1.0, 0.0))) > 0.99


def _source_forward(p, manifest, profile, info):
    """GLB forward (+Z characters / -Z ships, contract §4) and where it came from; guessed=True when
    neither params nor the manifest said."""
    if p.get("source_forward"):
        return p["source_forward"], "params.source_forward", False
    if manifest.get("kind"):
        return ("-Z" if manifest["kind"] in SHIP_KINDS else "+Z"), "manifest kind %s" % manifest["kind"], False
    if p.get("kind"):
        return ("-Z" if p["kind"] in SHIP_KINDS else "+Z"), "params.kind %s" % p["kind"], False
    ship = [s["name"] for s in info["sockets"]
            if xl.SHIP_SOCKET_RE.search(str(s.get("kind") or "")) or xl.SHIP_SOCKET_RE.search(s["name"])]
    if ship:
        return "-Z", "guessed from ship sockets (%s)" % ", ".join(ship[:3]), True
    if info["armatures"]:
        return "+Z", "guessed: rigged character", True
    return "+Z", "guessed: default for characters/items", True


def _tscn(asset_id, glb_name, bindings, fx_files, rotate, autoplay, meta):
    ext = ['[ext_resource type="PackedScene" path="%s" id="1_model"]' % glb_name,
           '[ext_resource type="Script" path="af_fx_mount.gd" id="2_mount"]']
    fx_ids = {}
    for i, f in enumerate(fx_files):
        fx_ids[f] = "%d_fx" % (i + 3)
        ext.append('[ext_resource type="PackedScene" path="fx/%s" id="%s"]' % (f, fx_ids[f]))
    items = []
    for b in bindings:
        # emit_axis (thruster sockets): the socket-local axis the FX node's +Y is turned onto by af_fx_mount.gd
        axis = ',\n"emit_axis": %s' % xl.gd_vec3(b["emit_axis"]) if b.get("emit_axis") else ""
        items.append('{\n"name": "%s",\n"scene": ExtResource("%s"),\n"socket": "%s"%s\n}'
                     % (b["node_name"], fx_ids[b["file"]], b["socket"], axis))
    root = ['[node name="%s" type="Node3D"]' % asset_id,
            'script = ExtResource("2_mount")',
            "fx_bindings = [%s]" % ", ".join(items),
            'autoplay_animation = "%s"' % autoplay,
            "metadata/astraforge = %s" % json.dumps(json.dumps(meta, ensure_ascii=True))]
    model = ['[node name="model" parent="." instance=ExtResource("1_model")]']
    if rotate:
        model.append("transform = Transform3D(-1, 0, 0, 0, 1, 0, 0, 0, -1, 0, 0, 0)")
    return ("[gd_scene load_steps=%d format=3]\n\n%s\n\n%s\n\n%s\n"
            % (len(ext) + 1, "\n".join(ext), "\n".join(root), "\n".join(model)))


def _readme(asset_id, profile, info, src_fwd, fwd, rotate, bindings, clips, sprites, gres,
            fwd_from=""):
    lines = ["# %s — Godot 4.5 package (AstraForge Forge)" % asset_id, "",
             "Profile: `%s`. Created by the `godot_package` step; nothing was written to the target project." % profile, "",
             "## Contents", "",
             "| File | What |", "|---|---|",
             "| `%s.glb` | model (byte-for-byte copy of the input GLB) |" % asset_id,
             "| `%s.tscn` | scene: `Node3D` + GLB instance (`model`) + `af_fx_mount.gd` script |" % asset_id,
             "| `af_fx_mount.gd` | on `_ready` finds sockets by `extras.game_socket` or by name and mounts the FX |",
             "| `fx/*.tscn` | effects: `GPUParticles3D` + `ParticleProcessMaterial` + `QuadMesh` |",
             "| `fx/textures/*.png` | procedural particle textures (white, shape in alpha) |"]
    if sprites:
        lines.append("| `sprites/` | sprite sheets and `SpriteFrames` from `render_sprites` |")
    lines += ["", "## Axes", "",
              "- The GLB faces `%s` (glTF/Godot; source: %s). By the AstraForge contract characters face glTF +Z "
              "and ships face −Z." % (src_fwd, fwd_from),
              "- Scene `%s.tscn` faces `%s`%s." % (asset_id, fwd,
                                                     " (the `model` node is rotated 180° about Y, the GLB is untouched)"
                                                     if rotate else ""),
              "- FX emit along the socket's local +Y (= socket +Z in Blender: outward, exhaust, blade); "
              "`muzzle_flash` emits along −Z (muzzle). Exception: thruster sockets (`socket_thruster_*`): "
              "the nozzle frame differs between ships (`extras.frame` = `thrust`, `exhaust`, or absent), so "
              "`af_fx_mount.gd` rotates the FX node so its +Y lies along the socket's exhaust axis (`emit_axis`, "
              "in socket axes: taken from `extras.frame` if it agrees with the geometry, otherwise the local axis pointing "
              "away from the nose).", "",
              "## Sockets", ""]
    if info["sockets"]:
        for s in info["sockets"]:
            lines.append("- `%s`%s%s%s" % (s["name"], " (bone `%s`)" % s["parent_bone"] if s["parent_bone"] else "",
                                           ", kind `%s`" % s["kind"] if s["kind"] else "",
                                           "; exhaust axis %s in socket axes (%s)" % (_axis_txt(s["exhaust_axis"]),
                                                                                    s["exhaust_axis_from"])
                                           if s.get("exhaust_axis") else ""))
    else:
        lines.append("- none")
    if info.get("pivots"):
        lines += ["", "Turret rig nodes (not sockets, contract section 6):"]
        for pv in info["pivots"]:
            lines.append("- `%s`: axis %s, range %s°" % (pv["node"], pv["axis"], pv["range_degrees"]))
    lines += ["", "## Effects", "",
              "Presets are metric; scaled to the asset (long axis %s m): k and where it comes from." % xl._f(
                  info["longest_m"]), ""]
    if bindings:
        for b in bindings:
            lines.append("- `%s` → `%s` (node `%s`, scene `fx/%s`; k = %s — %s; particle %s m%s)"
                         % (b["preset"], b["socket"], b["node_name"], b["file"], xl._f(b["scale"]),
                            b["scale_source"], xl._f(b["quad_size_m"]),
                            "; emission axis %s in socket axes — %s" % (_axis_txt(b["emit_axis"]), b["emit_axis_from"])
                            if b.get("emit_axis") else ""))
    else:
        lines.append("- none")
    lines += ["", "## Animations", ""]
    if clips:
        for c in clips:
            lines.append("- `%s` → in Godot `%s`, %s" % (c["name"], c["base"] if c["loop"] else c["name"],
                                                        "looped (`-loop` suffix)" if c["loop"] else "one-shot")
                         + ("; in the package GLB as `%s` (otherwise the Godot 4.5 import strips the suffix twice and reports "
                            "`Animation not found`)" % c["package_name"] if c.get("package_name", c["name"]) != c["name"]
                            else ""))
    else:
        lines.append("- none")
    lines += ["", "## How to use", "",
              "1. Copy the `%s/` folder into any directory of the project (paths inside are relative)." % asset_id,
              "2. Instantiate `%s.tscn`; fire one-shot FX (`muzzle_flash`) with `trigger(\"<socket>\")`." % asset_id,
              "3. Godot 4.5 import: the `-loop` suffix is stripped and the loop mode is set automatically.", ""]
    if gres:
        r = gres.get("result") or {}
        sc = (r.get("scenes") or [{}])[0]
        lines += ["## Godot %s check (headless)" % (gres.get("godot_version") or ""), "",
                  "- scene loaded: %s, instanced: %s" % (sc.get("loaded"), sc.get("instanced")),
                  "- sockets found: %d of %d" % (sum(1 for v in (sc.get("sockets") or {}).values() if v.get("found")),
                                                   len(sc.get("sockets") or {})),
                  "- FX mounted: %d of %d" % (sum(1 for v in (sc.get("fx") or []) if v.get("ok")),
                                                   len(sc.get("fx") or [])),
                  "- animations: %s" % ", ".join("%s loop_mode=%s" % (k, v.get("loop_mode"))
                                               for k, v in (sc.get("animations") or {}).items()),
                  "- engine errors: %d" % len(gres.get("engine_errors") or []), ""]
        thr = [v for v in (sc.get("fx") or []) if "axis_ok" in v]
        if thr:
            lines[-1:-1] = ["- thruster FX face backward: %d of %d (FX node +Y in package root axes: %s)"
                            % (sum(1 for v in thr if v["axis_ok"]), len(thr),
                               "; ".join("%s %s" % (v["socket"], v.get("axis_y_root")) for v in thr[:4]))]
    return "\n".join(lines)


def run(req):
    p = req["params"]
    inputs = req["inputs"]
    warnings, checks = [], []
    asset_id = p.get("asset_id")
    if not asset_id or not xl.ASSET_ID_RE.match(asset_id):
        raise ValueError("params.asset_id must be snake_case ASCII (^[a-z][a-z0-9]*(_[a-z0-9]+)*$): %r" % asset_id)
    profile = p.get("profile", "godot")
    if profile != "godot":
        raise ValueError("params.profile must be godot: %r" % profile)
    glb = inputs.get("glb") or inputs.get("mesh")
    if not glb or not os.path.isfile(glb) or not glb.lower().endswith(".glb"):
        raise ValueError("inputs.glb must be an existing .glb file: %r" % glb)
    manifest = {}
    if inputs.get("manifest"):
        with open(inputs["manifest"], "r", encoding="utf-8-sig") as fh:
            manifest = json.load(fh)
    info = _inspect(glb)
    src_fwd, fwd_from, guessed = _source_forward(p, manifest, profile, info)
    if guessed:
        warnings.append("source_forward %s %s; pass params.source_forward, params.kind or inputs.manifest"
                        % (src_fwd, fwd_from))
    fwd = p.get("forward_axis", src_fwd)
    if fwd not in ("+Z", "-Z") or src_fwd not in ("+Z", "-Z"):
        raise ValueError("forward_axis/source_forward must be '+Z' or '-Z'")
    rotate = fwd != src_fwd
    resolve_thruster_axes(info, src_fwd, warnings)      # exhaust axis of the thruster sockets (FX emit_axis)

    # ---- sockets
    socket_names = [s["name"] for s in info["sockets"]]
    bad = [s for s in socket_names if not SOCKET_RE.match(s)]
    checks.append(af.check("socket_names_snake_ascii", not bad, bad, [], severity="warning" if profile == "godot"
                           else "error", note="contract §6: socket_<kind>_<place>, ASCII snake_case, no dots"))
    no_extras = [s["node"] for s in info["sockets"] if not s["has_game_socket"]]
    if no_extras:
        warnings.append("sockets without extras.game_socket (found by name only): %s" % no_extras)
    if info["pivots"]:
        legacy = [pv["node"] for pv in info["pivots"] if pv["has_game_socket"]]
        no_axis = [pv["node"] for pv in info["pivots"] if not pv["axis"] or not pv["range_degrees"]]
        checks.append(af.check("rig_pivots", not legacy and not no_axis,
                               {pv["node"]: [pv["axis"], pv["range_degrees"]] for pv in info["pivots"]},
                               "axis + range_degrees, no game_socket", severity="warning",
                               note="turret rig pivots are not sockets (contract §6); listed under 'pivots'"
                                    + ("; legacy game_socket on %s" % legacy if legacy else "")
                                    + ("; missing axis/range on %s" % no_axis if no_axis else "")))

    # ---- FX bindings
    with open(xl.FX_PRESETS_PATH, "r", encoding="utf-8") as fh:
        presets = json.load(fh)["presets"]
    fx = p.get("fx", manifest.get("fx", [])) or []
    pkg = af.out_path(req, asset_id)
    if os.path.isdir(pkg):
        shutil.rmtree(pkg)
    os.makedirs(os.path.join(pkg, "fx", "textures"))
    bindings, fx_files, variants, textures = [], [], {}, set()
    thruster_fx = []        # bindings on thruster sockets whose preset emits along +Y (they get emit_axis)
    unknown, missing = [], []
    per_socket = {}
    variant_count = {}
    sock_info = {s["name"]: s for s in info["sockets"]}
    auto = bool(p.get("fx_auto_scale", True))
    scale_problems = []
    for b in fx:
        preset, sock = b.get("preset"), b.get("socket")
        if preset not in presets:
            unknown.append(preset)
            continue
        if sock not in socket_names:
            missing.append(sock)
        bparams = b.get("params") or {}
        if "scale" in bparams:
            k, k_src = float(bparams["scale"]), "params.scale"
        elif auto:
            k, k_src = xl.fx_auto_scale(presets[preset], sock_info.get(sock), info["longest_m"])
        else:
            k, k_src = 1.0, "preset native size (fx_auto_scale false)"
        merged = xl.merge_fx(presets[preset], bparams, scale=k)
        merged["_preset"] = preset
        prob = xl.fx_scale_problem(presets[preset], k, info["longest_m"], quad_m=merged.get("quad_size"),
                                   mount_scaled=k_src.startswith(("socket diameter", "size_class")))
        if prob:
            scale_problems.append({"preset": preset, "socket": sock, "scale": k, "problem": prob})
        key = json.dumps(merged, sort_keys=True)
        if key not in variants:
            n = variant_count.get(preset, 0)
            variant_count[preset] = n + 1
            fname = "%s.tscn" % preset if n == 0 else "%s_%d.tscn" % (preset, n + 1)
            variants[key] = fname
            tex = merged.get("texture", "soft")
            textures.add(tex)
            with open(os.path.join(pkg, "fx", fname), "w", encoding="utf-8", newline="\n") as fh:
                fh.write(xl.fx_scene_text(preset, merged, "textures/%s.png" % tex))
            fx_files.append(fname)
        cnt = per_socket.setdefault((sock, preset), 0) + 1
        per_socket[(sock, preset)] = cnt
        node_name = "fx_%s" % preset if cnt == 1 else "fx_%s_%d" % (preset, cnt)
        binding = {"preset": preset, "socket": sock, "file": variants[key], "node_name": node_name,
                   "params": bparams, "scale": k, "scale_source": k_src,
                   "quad_size_m": round(float(merged.get("quad_size", 0.2)), 4),
                   "visibility_aabb": merged.get("visibility_aabb", xl.DEFAULT_VISIBILITY_AABB)}
        si = sock_info.get(sock)
        if si and is_thruster_socket(si) and _emits_up(merged.get("direction")):
            # the FX emits along its local +Y: af_fx_mount.gd turns +Y onto the socket's exhaust axis
            thruster_fx.append(binding)
            if si.get("exhaust_axis"):
                binding["emit_axis"] = list(si["exhaust_axis"])
                binding["emit_axis_from"] = si["exhaust_axis_from"]
                binding["emit_aft_dot"] = si["exhaust_aft_dot"]
        bindings.append(binding)
    for tex in sorted(textures):
        xl.save_png(os.path.join(pkg, "fx", "textures", "%s.png" % tex), xl.make_fx_texture(tex))
    checks.append(af.check("fx_presets_known", not unknown, unknown, [], note="presets in data/fx_presets.json"))
    checks.append(af.check("fx_sockets_exist", not missing, missing, [], note="FX sockets present in the GLB"))
    if scale_problems:
        warnings.append("FX look out of scale next to the %.3g m asset: %s; pass params.scale (fx_auto_scale picks "
                        "it from size_class / the asset size)" % (info["longest_m"], "; ".join(
                            "%s@%s k=%s: %s" % (x["preset"], x["socket"], xl._f(x["scale"]), x["problem"])
                            for x in scale_problems[:4])))
    if thruster_fx:
        rows = {"%s@%s" % (b["node_name"], b["socket"]): {"emit_axis": b.get("emit_axis"),
                                                          "from": b.get("emit_axis_from"),
                                                          "aft_dot": b.get("emit_aft_dot")} for b in thruster_fx}
        checks.append(af.check("fx_thruster_axis",
                               all(b.get("emit_axis") and b["emit_aft_dot"] >= AFT_MIN_DOT for b in thruster_fx),
                               rows, "emit_axis present and aft_dot >= %s" % AFT_MIN_DOT,
                               note="every thruster FX (preset emitting along +Y) has an exhaust axis in the socket's "
                                    "own axes whose world direction points aft (>= 60 deg from the nose; aft = "
                                    "opposite of the GLB's forward %s)" % src_fwd))
    if bindings:
        checks.append(af.check("fx_scale_vs_asset", not scale_problems, scale_problems, [], severity="warning",
                               note="particle quad >= 1 %% of the asset's longest axis (%.3g m) and within 4x of "
                                    "the preset's design ratio" % info["longest_m"]))

    # ---- files
    glb_out = os.path.join(pkg, "%s.glb" % asset_id)
    renames = {c["name"]: c["package_name"] for c in info["clips"] if c["package_name"] != c["name"]}
    if renames:   # recorded in stats.clip_renames, af_package.json (clips[].package_name) and the README
        xl.rename_glb_animations(glb, glb_out, renames)
    else:
        shutil.copy2(glb, glb_out)
    shutil.copy2(os.path.join(xl.GODOT_DIR, "af_fx_mount.gd"), os.path.join(pkg, "af_fx_mount.gd"))
    meta = {"asset_id": asset_id, "profile": profile, "glb_forward": src_fwd, "scene_forward": fwd,
            "glb_forward_from": fwd_from, "generator": "astraforge-forge/godot_package"}
    tscn_path = os.path.join(pkg, "%s.tscn" % asset_id)
    with open(tscn_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_tscn(asset_id, "%s.glb" % asset_id, bindings, fx_files, rotate, p.get("autoplay", ""), meta))

    # ---- optional sprites
    sprites_info = None
    if inputs.get("sprites"):
        with open(inputs["sprites"], "r", encoding="utf-8") as fh:
            sprites_info = json.load(fh)
        sdir = os.path.dirname(os.path.abspath(inputs["sprites"]))
        dst = os.path.join(pkg, "sprites")
        os.makedirs(dst, exist_ok=True)
        for name in sprites_info.get("sheets", []):
            shutil.copy2(os.path.join(sdir, name), os.path.join(dst, name))
        tres = "%s_frames.tres" % sprites_info.get("asset_id", asset_id)
        if os.path.isfile(os.path.join(sdir, tres)):
            shutil.copy2(os.path.join(sdir, tres), os.path.join(dst, tres))
        shutil.copy2(inputs["sprites"], os.path.join(dst, "sprites.json"))

    # ---- Godot validation
    gres = None
    exe = xl.find_godot(p)
    if p.get("validate", True):
        if exe is None:
            warnings.append("Godot 4.5.1 not found: package not validated")
            checks.append(af.check("godot_validation", False, "godot missing", severity="warning"))
        else:
            proj = xl.make_check_project(af.out_path(req, "_godot_check"))
            dst = os.path.join(proj, "packages", asset_id)
            shutil.copytree(pkg, dst)
            res_root = "res://packages/%s/" % asset_id
            # aft in the package root's space: -Z-forward GLB -> glTF +Z; a model turned 180 deg about Y -> x, z negated
            aft_glb = (0.0, 0.0, 1.0) if src_fwd == "-Z" else (0.0, 0.0, -1.0)
            aft_root = [-aft_glb[0] + 0.0, 0.0, -aft_glb[2] + 0.0] if rotate else list(aft_glb)
            config = {"scenes": [{"path": res_root + "%s.tscn" % asset_id, "sockets": socket_names,
                                  "fx": [_fx_spec(b, aft_root) for b in bindings],
                                  "animations": [{"name": c["package_name"], "loop": c["loop"]}
                                                 for c in info["clips"]]}],
                      "fx_scenes": [res_root + "fx/" + f for f in fx_files]}
            if sprites_info:
                tres = "%s_frames.tres" % sprites_info.get("asset_id", asset_id)
                expect = {}
                for cname, c in sprites_info.get("clips", {}).items():
                    for v in c.get("views", {}).values():
                        expect[v["animation"]] = {"frames": c["frames"], "loop": c["loop"], "speed": c["fps"]}
                config["sprite_frames"] = [{"path": res_root + "sprites/" + tres, "expect": expect,
                                            "cell": sprites_info.get("cell")}]
            gres = xl.godot_validate(exe, proj, config)
            res = gres.get("result")
            ok = bool(res) and bool(res.get("ok"))
            checks.append(af.check("godot_validation", ok, res, True,
                                   note="validate_package.gd JSON (Godot %s headless): scene loads, instance, "
                                        "animations + loop modes, sockets, FX attached" % gres.get("godot_version")))
            sc = ((res or {}).get("scenes") or [{}])[0]
            checks.append(af.check("godot_scene_loads", bool(sc.get("loaded") and sc.get("instanced")),
                                   [sc.get("loaded"), sc.get("instanced")], [True, True]))
            anim_checks = sc.get("animation_checks") or []
            checks.append(af.check("godot_animation_loops", all(a.get("ok") for a in anim_checks),
                                   [(a.get("expected"), a.get("found"), a.get("loop_mode")) for a in anim_checks],
                                   note="'-loop' clips import with loop_mode != 0, others with 0"))
            socks = sc.get("sockets") or {}
            checks.append(af.check("godot_sockets_found", all(v.get("found") for v in socks.values()),
                                   sum(1 for v in socks.values() if v.get("found")), len(socks)))
            fxr = sc.get("fx") or []
            checks.append(af.check("godot_fx_attached", all(f.get("ok") for f in fxr),
                                   sum(1 for f in fxr if f.get("ok")), len(bindings)))
            want_axis = sum(1 for b in bindings if b.get("emit_axis"))
            if want_axis:
                got = [f for f in fxr if "axis_ok" in f]
                checks.append(af.check("godot_fx_thruster_aft", len(got) == want_axis and all(f["axis_ok"] for f in got),
                                       {"%s@%s" % (f["name"], f["socket"]): {"axis_y_root": f.get("axis_y_root"),
                                                                              "axis_dot": f.get("axis_dot")}
                                        for f in got}, "FX +Y within the allowed angle of aft %s (package root)" % aft_root,
                                       note="validate_package.gd: thruster FX +Y axis in the package root's space vs "
                                            "aft (dot >= 0.9, lower only for nozzles canted by the model itself)"))
            checks.append(af.check("godot_engine_errors", not gres["engine_errors"], gres["engine_errors"][:10], [],
                                   severity="warning"))
            if p.get("fx_preview", False):
                speed = float(p.get("fx_preview_speed",
                                    1.0 if src_fwd == "-Z" and not info["armatures"] else 0.0))
                frames = tuple(int(f) for f in p.get("fx_preview_frames", (14, 30, 46)))
                prev = xl.godot_fx_preview(exe, proj, res_root + "%s.tscn" % asset_id,
                                           af.out_path(req, "fx_preview.png"), speed=speed, frames=frames,
                                           quit_after=max(frames) + 4)
                gres["fx_preview"] = prev
                checks.append(af.check("godot_fx_preview", bool(prev.get("ok")),
                                       prev.get("error") or prev.get("path"), severity="warning",
                                       note="Godot Movie Maker frames %s (short windowed run)" % prev.get("frames")))
            gpath = af.out_path(req, "godot_check.json")
            with open(gpath, "w", encoding="utf-8") as fh:
                json.dump(gres, fh, ensure_ascii=False, indent=1)

    # ---- readme + manifest
    with open(os.path.join(pkg, "README.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(_readme(asset_id, profile, info, src_fwd, fwd, rotate, bindings, info["clips"],
                         sprites_info is not None, gres, fwd_from))
    pkg_manifest = {"format": "astraforge-godot-package/1", "asset_id": asset_id, "profile": profile,
                    "godot": "4.5", "glb": os.path.basename(glb_out), "glb_sha256": xl.file_sha256(glb_out),
                    "source_glb": os.path.abspath(glb), "glb_forward": src_fwd, "glb_forward_from": fwd_from,
                    "glb_forward_guessed": guessed, "scene_forward": fwd,
                    "model_rotated_180": rotate, "sockets": info["sockets"], "pivots": info["pivots"],
                    "clips": info["clips"],
                    "fx": bindings, "fx_auto_scale": auto, "size_gltf_m": info["size_gltf_m"],
                    "longest_m": info["longest_m"], "tris": info["tris"],
                    "sprites": sprites_info is not None}
    with open(os.path.join(pkg, "af_package.json"), "w", encoding="utf-8") as fh:
        json.dump(af._jsonable(pkg_manifest), fh, ensure_ascii=False, indent=2)

    outputs = {"package": pkg, "glb": glb_out, "tscn": tscn_path, "script": os.path.join(pkg, "af_fx_mount.gd"),
               "fx": [os.path.join(pkg, "fx", f) for f in fx_files], "readme": os.path.join(pkg, "README.md"),
               "manifest": os.path.join(pkg, "af_package.json")}
    if gres:
        outputs["godot_check"] = af.out_path(req, "godot_check.json")
        outputs["godot_log"] = gres["log"]
        if (gres.get("fx_preview") or {}).get("ok"):
            outputs["fx_preview"] = gres["fx_preview"]["path"]
    stats = {"asset_id": asset_id, "profile": profile, "sockets": len(socket_names), "pivots": len(info["pivots"]),
             "clips": len(info["clips"]), "clip_renames": renames, "sprites": sprites_info is not None,
             "fx": len(bindings), "fx_scenes": len(fx_files), "tris": info["tris"],
             "size_gltf_m": info["size_gltf_m"], "glb_forward": src_fwd, "glb_forward_from": fwd_from,
             "scene_forward": fwd,
             "fx_scales": {"%s@%s" % (b["node_name"], b["socket"]): b["scale"] for b in bindings},
             "thruster_axes": {s["name"]: {"axis": s["exhaust_axis"], "from": s["exhaust_axis_from"],
                                           "frame": s["frame"]}
                               for s in info["sockets"] if s.get("exhaust_axis")},
             "godot_seconds": (gres or {}).get("seconds"), "godot_version": (gres or {}).get("godot_version")}
    return {"outputs": outputs, "stats": stats, "checks": checks, "warnings": warnings}
