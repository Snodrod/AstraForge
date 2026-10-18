"""turret_gen — turret asset with a yaw/pitch rig and short mechanical clips.

Rig:
    <asset_id> -> socket_mount -> pivot_yaw -> pivot_pitch -> socket_muzzle_01..N
Only socket_mount and socket_muzzle_* are sockets (extras.game_socket); pivot_yaw / pivot_pitch carry
extras kind "pivot", axis and range_degrees but no game_socket (contract §6).
Meshes per rig level, joined by material: M_root_<mat> (under socket_mount), M_yaw_<mat> (under pivot_yaw),
M_pitch_<mat> + M_barrel_steeltrim (under pivot_pitch; the barrel mesh carries the recoil clip).
Mount frame: Blender +Z up (outward), +Y forward -> glTF +Y up, -Z forward. Muzzles: identity frame of the
pitch pivot, i.e. glTF local -Z = fire direction. pivot_yaw: axis local Z (glTF Y), pivot_pitch: local X.
Attaching to a ship = making socket_mount coincide with the ship's socket_turret_* frame.

Request params:
  size        S | M | L (mount ring diameter 1.2 / 2.4 / 4.8 m; params.ring_m overrides within the class)
  barrels     1..4 (default 2)
  barrel_style plain (default) | rotary (each gun a 6-barrel rotary cluster, _ship_lib.rotary_barrels)
  faction     industrial | military | sleek | alien (default military)
  seed        int (default 1)
  clips       bool (default true): yaw_sweep-loop (4 s), pitch_cycle-loop (3 s), fire_recoil (0.4 s)
  lods        bool (default true): turret_lod1.glb, turret_lod2.glb (same rig, fewer triangles)
  livery, palette_variant, asset_id, tier, preview, preview_size — as in ship_gen.
Outputs: turret.glb, turret.blend, turret.json (manifest), turret_lod1/2.glb, preview.png.
"""
import math
import random
import zlib

import bpy
from mathutils import Matrix, Vector

import af_common as af
from steps import _ship_lib as sl

FPS = 30


def resolve(req):
    """Resolved parameters + warnings for every clamped or ignored value."""
    p = dict(req.get("params") or {})
    warn = []
    size = str(p.get("size", "M")).upper()
    if size not in sl.TURRET_RING:
        raise ValueError("size must be S, M or L")
    faction = str(p.get("faction", p.get("style", "military"))).lower()
    if faction not in sl.FACTIONS:
        raise ValueError("unknown faction %r" % faction)
    seed = int(p.get("seed", 1))
    b_in = int(p.get("barrels", 2))
    barrels = max(1, min(4, b_in))
    if barrels != b_in:
        warn.append("barrels %d clamped to %d (1..4)" % (b_in, barrels))
    lim = {"S": 1.5, "M": 3.0, "L": 6.0}[size]
    r_in = float(p.get("ring_m", sl.TURRET_RING[size]))
    ring = max(0.3, min(lim, r_in))
    if abs(ring - r_in) > 1e-9:
        warn.append("ring_m %.2f clamped to %.2f (size class %s: 0.3..%.1f m)" % (r_in, ring, size, lim))
    rng = random.Random(zlib.crc32(("%d|turret|%s" % (seed, size)).encode()))
    pal = int(p.get("palette_variant", rng.randint(0, 2)))
    if not 0 <= pal <= 2:
        warn.append("palette_variant %d wrapped to %d (0..2)" % (pal, pal % 3))
        pal %= 3
    psz = int(p.get("preview_size", 300))
    if not 120 <= psz <= 800:
        warn.append("preview_size %d clamped to 120..800" % psz)
        psz = max(120, min(800, psz))
    style = str(p.get("barrel_style", "plain")).lower()
    if style not in ("plain", "rotary"):
        warn.append("barrel_style %r unknown (plain | rotary); plain used" % style)
        style = "plain"
    livery = p.get("livery") or {}
    alias = {"accent": "graphite", "hull": "armor", "lights": "status", "engine": "drive"}
    for key in list(livery):
        if alias.get(key, key) not in sl.MATS:
            warn.append("livery key %r ignored (expected %s)" % (key, ", ".join(sl.MATS)))
    out = {
        "size": size, "barrels": barrels, "faction": faction, "seed": seed, "ring_m": ring,
        "clips": bool(p.get("clips", True)), "lods": bool(p.get("lods", True)),
        "livery": livery, "palette_variant": pal,
        "tier": int(p.get("tier", 1)), "preview": bool(p.get("preview", True)),
        "preview_size": psz,
        "budget_class": "turret", "barrel_style": style,
    }
    out["asset_id"] = str(p.get("asset_id") or "turret_%s_%s_b%d_%03d" % (size.lower(), faction, barrels, seed % 1000))
    return out, warn


def _empty(name, parent, loc, display="ARROWS", size=0.2, **props):
    e = bpy.data.objects.new(name, None)
    e.empty_display_type = display
    e.empty_display_size = size
    bpy.context.scene.collection.objects.link(e)
    if parent is not None:
        e.parent = parent
    e.location = loc
    for k, v in props.items():
        e[k] = v
    return e


def assemble(P, lod, mats, suffix=""):
    """Build the rig + meshes in the current scene. Returns dict with all objects."""
    kits, rig = sl.turret_geometry(P["size"], P["barrels"], P["faction"], P["seed"], lod=lod, ring=P["ring_m"],
                                   rotary=P["barrel_style"] == "rotary")
    D = rig["ring_d"]
    root = _empty(P["asset_id"] + suffix, None, (0, 0, 0), display="PLAIN_AXES", size=D * 0.5)
    mount = _empty("socket_mount", root, (0, 0, 0), size=D * 0.3, game_socket="socket_mount", kind="mount",
                   size_class=P["size"], mount_d_m=round(D, 3))
    yaw_pos = Vector((0.0, 0.0, rig["yaw_z"]))
    # rig pivots are not sockets (contract 6): extras axis + range_degrees, no game_socket
    yaw = _empty("pivot_yaw", mount, yaw_pos, size=D * 0.25, kind="pivot", axis="local Z",
                 range_degrees=[-180.0, 180.0])
    pitch_w = Vector(rig["pitch"])
    pr = [-10.0, 60.0] if P["size"] != "L" else [-5.0, 45.0]
    pitch = _empty("pivot_pitch", yaw, pitch_w - yaw_pos, size=D * 0.2, kind="pivot", axis="local X",
                   range_degrees=pr)
    muzzles = []
    for i, m in enumerate(rig["muzzles"]):
        nm = "socket_muzzle_%02d" % (i + 1)
        muzzles.append(_empty(nm, pitch, Vector(m) - pitch_w, size=D * 0.1, game_socket=nm, kind="muzzle"))
    meshes = []
    for key, parent, origin in (("root", mount, Vector()), ("yaw", yaw, yaw_pos), ("pitch", pitch, pitch_w),
                                ("barrel", pitch, pitch_w)):
        kt = kits[key]
        kt.transform_all(Matrix.Translation(-origin))
        objs = sl.split_by_material(kt, mats, key, parent=parent, angle=34.0, suffix=suffix)
        kt.bm.free()
        meshes += objs
    bpy.context.view_layer.update()
    return {"root": root, "mount": mount, "yaw": yaw, "pitch": pitch, "muzzles": muzzles, "meshes": meshes,
            "rig": rig, "barrel": [o for o in meshes if o.name.startswith("M_barrel_")]}


def _clip(obj, name, path, idx, keys, rest):
    obj.animation_data_create()
    obj.animation_data.action = None
    for f, v in keys:
        getattr(obj, path)[idx] = v
        obj.keyframe_insert(path, index=idx, frame=f)
    act = obj.animation_data.action
    act.name = "%s__%s" % (name, obj.name)
    tr = obj.animation_data.nla_tracks.new()
    tr.name = name
    tr.strips.new(name, int(keys[0][0]), act)
    obj.animation_data.action = None
    getattr(obj, path)[idx] = rest
    return {"name": name, "seconds": round((keys[-1][0] - keys[0][0]) / float(FPS), 3),
            "loop": name.endswith("-loop")}


def add_clips(A, P):
    scene = bpy.context.scene
    scene.render.fps = FPS
    scene.frame_start, scene.frame_end = 1, 121
    r = math.radians
    clips = []
    clips.append(_clip(A["yaw"], "yaw_sweep-loop", "rotation_euler", 2,
                       [(1, 0.0), (31, r(60)), (61, 0.0), (91, r(-60)), (121, 0.0)], 0.0))
    hi = 40.0 if P["size"] != "L" else 32.0
    clips.append(_clip(A["pitch"], "pitch_cycle-loop", "rotation_euler", 0,
                       [(1, 0.0), (31, r(hi)), (61, r(-6)), (91, 0.0)], 0.0))
    rec = A["rig"]["barrel_len"] * 0.12
    for b in A["barrel"]:
        c = _clip(b, "fire_recoil", "location", 1, [(1, 0.0), (3, -rec), (13, 0.0)], 0.0)
    clips.append(c)
    return clips


def run(req):
    P, warnings = resolve(req)
    bud = sl.BUDGETS["turret"]
    outputs, lod_tris = {}, {}
    pal = sl.palette_for(P["faction"], P["palette_variant"], P["livery"])
    # LOD0 triangle count first (targets for the LOD files)
    kits, _ = sl.turret_geometry(P["size"], P["barrels"], P["faction"], P["seed"], lod=0, ring=P["ring_m"],
                                 rotary=P["barrel_style"] == "rotary")
    tris0 = sum(k.tri_count() for k in kits.values())
    for k in kits.values():
        k.bm.free()
    # ---- LOD files (built in clean scenes so the rig node names stay canonical)
    if P["lods"]:
        for lod in (1, 2):
            af.reset_scene()
            mats = sl.make_materials(pal)
            A = assemble(P, lod, mats, suffix="_lod%d" % lod)
            frac = 0.35 if lod == 1 else 0.12
            target = int(min(bud["lod%d" % lod], frac * tris0) * 0.95)
            sl.decimate_to(A["meshes"], target)
            sl.uv_unwrap(A["meshes"], "box")
            path = af.out_path(req, "turret_lod%d.glb" % lod)
            af.export_glb(path, [A["root"], A["mount"], A["yaw"], A["pitch"]] + A["muzzles"] + A["meshes"],
                          animations=False, skins=False, export_tangents=False, export_image_format="AUTO",
                          export_draco_mesh_compression_enable=False)
            lod_tris["lod%d" % lod] = sl.glb_summary(path)["triangles"]
            outputs["lod%d" % lod] = path
    # ---- LOD0 with clips
    af.reset_scene()
    mats = sl.make_materials(pal)
    A = assemble(P, 0, mats)
    sl.uv_unwrap(A["meshes"], "smart")
    # rest-pose data first: the glTF exporter samples the clips and leaves the scene on another frame
    rest_rows = sl.socket_manifest_rows([A["mount"], A["yaw"], A["pitch"]] + A["muzzles"])
    if P["preview"]:
        outputs["preview"] = sl.render_grid(af.out_path(req, "preview.png"), A["meshes"], tile=P["preview_size"],
                                            views=("three_quarter", "rear_quarter", "side", "top", "front"))
    clips = add_clips(A, P) if P["clips"] else []
    objs = [A["root"], A["mount"], A["yaw"], A["pitch"]] + A["muzzles"] + A["meshes"]
    out_glb = af.out_path(req, "turret.glb")
    af.export_glb(out_glb, objs, animations=bool(clips), skins=False, animation_mode="NLA_TRACKS",
                  export_tangents=False, export_image_format="AUTO", export_draco_mesh_compression_enable=False)
    bpy.context.scene.frame_set(1)
    outputs["glb"] = out_glb
    summ = sl.glb_summary(out_glb)
    checks = sl.budget_checks(summ, "turret", lod_tris if P["lods"] else None)
    names = summ["node_names"]
    need = ["socket_mount", "pivot_yaw", "pivot_pitch"] + ["socket_muzzle_%02d" % (i + 1) for i in range(P["barrels"])]
    missing = [n for n in need if n not in names]
    checks.append(af.check("rig_nodes", not missing, missing, need))
    # hierarchy check straight from the GLB: mount -> yaw -> pitch -> muzzles
    doc, _, _ = sl.read_glb(out_glb)
    nodes = doc["nodes"]
    idx = {n.get("name"): i for i, n in enumerate(nodes)}
    parent = {c: i for i, n in enumerate(nodes) for c in n.get("children", [])}
    chain_ok = all(k in idx for k in need) and \
        parent.get(idx.get("pivot_yaw")) == idx.get("socket_mount") and \
        parent.get(idx.get("pivot_pitch")) == idx.get("pivot_yaw") and \
        all(parent.get(idx.get(m)) == idx.get("pivot_pitch") for m in need[3:])
    checks.append(af.check("rig_hierarchy", chain_ok, chain_ok, "socket_mount>pivot_yaw>pivot_pitch>socket_muzzle_*"))
    piv = {n.get("name"): (n.get("extras") or {}) for n in nodes if str(n.get("name", "")).startswith("pivot_")}
    piv_bad = sorted(k for k, ex in piv.items()
                     if "game_socket" in ex or not ex.get("axis") or len(ex.get("range_degrees") or []) != 2)
    checks.append(af.check("pivots_not_sockets", len(piv) == 2 and not piv_bad,
                           {k: sorted(ex) for k, ex in piv.items()}, "pivot_yaw/pitch: axis + range_degrees, no game_socket",
                           note="contract: only socket_mount and socket_muzzle_* are sockets"))
    only = sorted(s_["game_socket"] for s_ in summ["sockets"])
    checks.append(af.check("turret_sockets_only_mount_muzzles",
                           only == sorted(["socket_mount"] + need[3:]), only, sorted(["socket_mount"] + need[3:])))
    mz = [s for s in summ["sockets"] if s["extras"].get("kind") == "muzzle"]
    fwd = all(s["neg_z_gltf"][2] < -0.99 for s in mz)
    checks.append(af.check("muzzle_frame_minus_z_forward", fwd, [s["neg_z_gltf"] for s in mz][:2], "(0,0,-1)"))
    anims = [a["name"] for a in summ["animations"]]
    if P["clips"]:
        checks.append(af.check("clips", sorted(anims) == sorted(c["name"] for c in clips), anims,
                               [c["name"] for c in clips]))
        checks.append(af.check("clips_max_4", len(anims) <= 4, len(anims), 4))
    D = A["rig"]["ring_d"]
    checks.append(af.check("mount_ring_size_class", D <= {"S": 1.5, "M": 3.0, "L": 6.0}[P["size"]], round(D, 3),
                           P["size"]))
    outputs["blend"] = sl.save_blend_clean(af.out_path(req, "turret.blend"))
    outputs["definition"] = sl.write_json(af.out_path(req, "turret.definition.json"),
                                          dict(P, generator_version=sl.GEN_TURRET))
    manifest = build_manifest(P, summ, lod_tris, outputs, rest_rows, clips, req)
    outputs["manifest"] = sl.write_json(af.out_path(req, "turret.json"), manifest)
    stats = {"asset_id": P["asset_id"], "size": P["size"], "barrels": P["barrels"], "faction": P["faction"],
             "ring_d_m": round(D, 3), "height_m": round(A["rig"]["height"], 3), "tris": summ["triangles"],
             "tris_lod1": lod_tris.get("lod1"), "tris_lod2": lod_tris.get("lod2"), "surfaces": summ["surfaces_lod0"],
             "materials": summ["material_names_used"], "nodes": summ["nodes"], "glb_mb": summ["mb"],
             "clips": clips, "node_names": names, "geometry_hash": summ["geometry_hash"], "budget": bud}
    return {"outputs": outputs, "stats": stats, "checks": checks, "warnings": warnings}


def build_manifest(P, summ, lod_tris, outputs, socks, clips, req):
    sx, sy, sz = summ["size_gltf"]
    tri = {"lod0": int(summ["triangles"])}
    tri.update({k: int(v) for k, v in lod_tris.items()})
    files = {"game_glb": "turret.glb", "source_blend": "turret.blend"}
    if lod_tris:
        files["lod_glb"] = ["turret_lod1.glb", "turret_lod2.glb"]
    if outputs.get("preview"):
        files["preview"] = ["preview.png"]
    return {
        "schema_version": 1, "asset_id": P["asset_id"], "type": "turret", "class": "turret",
        "faction": P["faction"], "tier": P["tier"], "source": "procedural", "generator_version": sl.GEN_TURRET,
        "seed": P["seed"], "definition_file": "turret.definition.json",
        "dimensions_m": {"length": round(sz, 3), "width": round(sx, 3), "height": round(sy, 3)},
        "axes": {"blender_forward": "+Y", "blender_up": "+Z", "godot_forward": "-Z", "godot_up": "+Y"},
        "triangles": tri, "surfaces_lod0": int(summ["surfaces_lod0"]),
        "materials": sorted(set(summ["material_names_used"])), "textures": [],
        "collision": {"simple": {"node": "none (turret uses the hull COL_SIMPLE)", "vertices": 0}, "complex": None},
        "sockets": socks, "animations": clips, "files": files, "glb_bytes": int(summ["bytes"]),
        "geometry_hash": summ["geometry_hash"],
        "validation": {"date": sl.today(),
                       "gltf_validator": {"errors": -1, "warnings": -1, "version": "not_run"},
                       "godot_import": "SKIPPED",
                       "notes": "gltf_validator / godot_import not run by the forge step (-1 = not run)"},
        "status": "prototype", "created": sl.today(),
        "notes": "AstraForge turret_gen; aiming is code-driven, clips are demo/mechanics only",
    }
