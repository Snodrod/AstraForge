"""kitbash — put detailed procedural parts onto an existing (AI) ship model at its sockets or at given points.

Input: inputs.blend (ship.blend of ship_from_mesh / a previous kitbash: LOD0 + LOD1/LOD2 roots, sockets, AF_*
materials and the forge metadata scene["af_ship"] with the manifest) or inputs.mesh (ship.glb; siblings
ship_lod1.glb / ship_lod2.glb and ship.json are used when present).

params.parts — list of part specs, applied in order:
  {kind: 'gatling', at: 'socket_turret_*' | [x, y, z] | [[x, y, z], ...], axis?, length (barrel, m), cluster_d,
   housing: [length, width, height], barrels: 6, seat: 'auto' | metres along the axis from the anchor,
   remove: {cylinder|sphere|box} | false | {radius}, muzzle_socket: true | name}
     rotary 6-barrel gun with receiver housing (_ship_lib.rotary_barrels), fire axis = the socket's +Z (outward);
     the AI stub inside a cylinder along the axis is removed first, the seat is found by rays onto the bay wall;
     adds socket_muzzle_<place> (glTF -Z = fire direction) and extras builtin on the turret socket
  {kind: 'nozzle', at: 'socket_thruster_*', diameter?, length?, throat_d?, collar_len?, ribs, bands, actuators,
   axis (exhaust, default aft -Y), remove?}
     ribbed dark bell, glowing inner surface (AF_EmissiveDrive) and throat, steel collar / flanges / rim,
     actuator pistons, mount plate; the socket keeps its frame, mount_d_m follows the new exit diameter
  {kind: 'piping', points: [[x, y, z], ...], radius, count, spread: [dx, dy, dz], mirror: 'x', material}
  {kind: 'antenna' | 'mast', at: [x, y] (snapped down onto the hull) | [x, y, z], height, style: whip | mast |
   rod | dome, light: true, radius?, mirror?, socket?: name, remove?: {radius}}
  {kind: 'nav_light', at: [[...], ...], snap: top | side | port | starboard | none, size, mirror, socket?: name
   or [port_name, starboard_name]}
  {kind: 'vent_grille', at: [y, z], side: port | starboard | both, size: [length, height], slats, depth}
  {kind: 'block', at: [x, y, z] (centre), size: [x, y, z], material, bevel, taper: [x, y], panel: material of
   an inset top panel, axis / up (frame), mirror} — a plain greeble block, e.g. to cover a cut
  {kind: 'remove', regions: [{cylinder|sphere|box}], cap: false}
Parts are built per LOD (LOD1 / LOD2 drop small details) and merged into the existing M_<part>_<material>
objects by material, so the surface / material count does not grow. Other params: lods (true), preview,
preview_size, drop_sockets [names], asset_id (default from the input), budget_class (default from
the input metadata or 'ship_m').
Outputs: ship.glb, ship_lod1.glb, ship_lod2.glb, ship.blend, ship.json (updated manifest), preview.png.
"""
import fnmatch
import json
import math
import os

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

import af_common as af
from steps import _kitbash_lib as kl
from steps import _ship_lib as sl
from steps import ship_from_mesh as sfm
from steps import ship_gen

GEN_KITBASH = "af_kitbash_v1"
KINDS = ("gatling", "nozzle", "piping", "antenna", "mast", "nav_light", "vent_grille", "block", "remove")


# ----------------------------------------------------------------------------- loading
def load_input(req, warnings):
    """Open the model; returns (meta dict, asset_id, {lod: (root, [mesh objects])}, collision object)."""
    inputs = req.get("inputs") or {}
    meta = {}
    if inputs.get("blend"):
        af.open_blend(inputs["blend"])
        raw = bpy.context.scene.get("af_ship")
        if raw:
            meta = json.loads(raw)
        else:
            warnings.append("input .blend has no forge metadata (scene['af_ship']); manifest not updated")
    elif inputs.get("mesh"):
        path = inputs["mesh"]
        af.reset_scene()
        af.import_any(path)
        stem, ext = os.path.splitext(path)
        for k in (1, 2):
            lp = "%s_lod%d%s" % (stem, k, ext)
            if os.path.isfile(lp):
                af.import_any(lp)
        # the importer makes 'AF_Armor.001' for every further file: map LOD materials back to the LOD0 ones
        for o in bpy.context.scene.objects:
            if o.type != "MESH":
                continue
            for i, m in enumerate(o.data.materials):
                if m is not None and "." in m.name and bpy.data.materials.get(m.name.rsplit(".", 1)[0]):
                    o.data.materials[i] = bpy.data.materials[m.name.rsplit(".", 1)[0]]
        mj = os.path.join(os.path.dirname(path), "ship.json")
        if os.path.isfile(mj):
            with open(mj, "r", encoding="utf-8") as fh:
                meta["manifest"] = json.load(fh)
        else:
            warnings.append("no ship.json next to the input GLB; manifest not updated")
    else:
        raise ValueError("kitbash needs inputs.blend (ship.blend) or inputs.mesh (ship.glb)")
    asset_id = req["params"].get("asset_id") or meta.get("asset_id")
    if not asset_id or asset_id not in bpy.data.objects:
        tops = [o for o in bpy.context.scene.objects if o.parent is None and o.type == "EMPTY"
                and any(c.type == "MESH" for c in o.children) and not o.name.endswith(("_lod1", "_lod2"))]
        if not tops:
            raise ValueError("no ship root (empty with mesh children) in the input")
        asset_id = tops[0].name
    root = bpy.data.objects[asset_id]
    sets = {0: (root, [o for o in root.children if o.type == "MESH" and not o.name.startswith("COL_")])}
    for k in (1, 2):
        r = bpy.data.objects.get("%s_lod%d" % (asset_id, k))
        if r is not None:
            sets[k] = (r, [o for o in r.children if o.type == "MESH"])
    col = next((o for o in root.children if o.type == "MESH" and o.name.startswith("COL_")), None)
    for k, (r, objs) in sets.items():
        for o in objs:
            o.hide_render = False
            o.hide_set(False)
    return meta, asset_id, sets, col


def socket_objects(root):
    return {o.get("game_socket", o.name): o for o in root.children if o.type == "EMPTY"
            and (o.get("game_socket") or o.name.startswith("socket_"))}


def material_table(sets):
    """Material per Kit key: the materials the LOD0 objects already use (textured AF_Armor included);
    missing keys get library materials (emission colours copied from an existing glow material)."""
    used = {}
    for o in sets[0][1]:
        for m in o.data.materials:
            if m is not None:
                used[m.name] = m
    out = {}
    missing = []
    for key in sl.MATS:
        name = sl.MAT_NAME[key]
        m = used.get(name) or bpy.data.materials.get(name)
        if m is None:
            missing.append(key)
        out[key] = m
    if missing:
        pal = sl.palette_for("military", 0)
        glow = out.get("drive") or out.get("status")
        if glow is not None and glow.node_tree:
            b = next((n for n in glow.node_tree.nodes if n.type == "BSDF_PRINCIPLED"), None)
            if b is not None:
                c = tuple(b.inputs["Emission Color"].default_value[:3])
                for k in ("drive", "status"):
                    pal[k]["rgb"] = pal[k]["emit"] = c
        lib = sl.make_materials(pal, suffix="")
        for key in missing:
            out[key] = lib[key]
    return out


# ----------------------------------------------------------------------------- geometry helpers
def lod0_bvh(objs):
    Vs, Fs, off = [], [], 0
    for o in objs:
        M = o.matrix_world
        Vs += [tuple(M @ v.co) for v in o.data.vertices]
        Fs += [tuple(i + off for i in p.vertices) for p in o.data.polygons]
        off += len(o.data.vertices)
    return BVHTree.FromPolygons(Vs, Fs) if Fs else None


def bounds(objs):
    pts = np.concatenate([np.array([o.matrix_world @ Vector(c) for c in o.bound_box]) for o in objs])
    return pts.min(0), pts.max(0)


def snap(bvh, origin, direction, max_d):
    hit = bvh.ray_cast(Vector(origin), Vector(direction).normalized(), max_d)
    return (hit[0], hit[1]) if hit[0] is not None else (None, None)


def merge_kit(kit, set_objs, parent, suffix, mats):
    """Kit -> one temporary object per material, UVs (metric box; tile from the material's af_tile_m), then
    joined into the LOD set object with the same material (new object only for a material the set lacks)."""
    if not kit.bm.faces:
        return set_objs, 0
    tris = kit.tri_count()
    tmp = sl.split_by_material(kit, mats, "kb", parent=None, angle=34.0, suffix=suffix)
    out = list(set_objs)
    for t in tmp:
        mat = t.data.materials[0]
        target = next((o for o in out if o.data.materials and o.data.materials[0] == mat), None)
        uvname = (target.data.uv_layers.active.name if target is not None and target.data.uv_layers.active
                  else "UVMap")
        if not t.data.uv_layers:
            t.data.uv_layers.new(name=uvname)
        sfm.box_uv_metres(t, float(mat.get("af_tile_m", 4.0)))
        if target is None:
            t.name = "M_hull_%s%s" % (sl.SHORT.get(next((k for k, m in mats.items() if m == mat), "armor"),
                                                  "part"), suffix)
            t.parent = parent
            out.append(t)
            continue
        t.data.transform(target.matrix_world.inverted() @ t.matrix_world)
        bm = bmesh.new()
        bm.from_mesh(target.data)
        bm.from_mesh(t.data)
        bm.to_mesh(target.data)
        bm.free()
        target.data.update()
        me = t.data
        bpy.data.objects.remove(t, do_unlink=True)
        bpy.data.meshes.remove(me)
    return out, tris


# ----------------------------------------------------------------------------- part resolution
def _anchors(spec, sockets, warnings, idx):
    """[(position Vector, socket object or None)] for spec['at']."""
    at = spec.get("at")
    out = []
    if isinstance(at, str):
        names = sorted(n for n in sockets if fnmatch.fnmatchcase(n, at))
        if not names:
            warnings.append("parts[%d] (%s): no socket matches %r" % (idx, spec.get("kind"), at))
        for n in names:
            out.append((sockets[n].matrix_world.translation.copy(), sockets[n]))
    elif isinstance(at, (list, tuple)) and at and isinstance(at[0], (list, tuple)):
        out = [(Vector([float(c) for c in a] + [None] * 0), None) if len(a) == 3 else (list(a), None) for a in at]
    elif isinstance(at, (list, tuple)) and len(at) in (2, 3):
        out = [(Vector([float(c) for c in at]) if len(at) == 3 else list(at), None)]
    if spec.get("mirror") == "x":
        extra = []
        for p, s in out:
            if s is None:
                if isinstance(p, Vector) and abs(p.x) > 1e-6:
                    extra.append((Vector((-p.x, p.y, p.z)), None))
                elif isinstance(p, list) and len(p) == 2 and abs(p[0]) > 1e-6 and (spec.get("kind") in ("antenna", "mast") or str(spec.get("snap", "")) == "top"):
                    extra.append(([-p[0], p[1]], None))
        out += extra
    return out


def region_list(spec):
    r = spec.get("remove")
    if not r:
        return []
    if isinstance(r, dict) and any(k in r for k in ("cylinder", "sphere", "box")):
        return [r]
    if isinstance(r, list):
        return [x for x in r if isinstance(x, dict)]
    return []


def plan_parts(params, sockets, L, warnings):
    """Expand specs into placed instances (anchors resolved, default removal regions computed)."""
    plans = []
    for i, spec in enumerate(params.get("parts") or []):
        kind = str(spec.get("kind", "")).lower()
        if kind not in KINDS:
            warnings.append("parts[%d]: unknown kind %r (%s)" % (i, kind, ", ".join(KINDS)))
            continue
        if kind == "remove":
            plans.append({"i": i, "kind": kind, "spec": spec, "regions": [r for r in (spec.get("regions") or [])
                                                                           if isinstance(r, dict)],
                          "cap": bool(spec.get("cap", False))})
            continue
        if kind == "piping":
            pts = [Vector(p) for p in (spec.get("points") or [])]
            if len(pts) < 2:
                warnings.append("parts[%d] piping: needs >= 2 points" % i)
                continue
            groups = [pts]
            if spec.get("mirror") == "x":
                groups.append([Vector((-p.x, p.y, p.z)) for p in pts])
            for g in groups:
                plans.append({"i": i, "kind": kind, "spec": spec, "points": g, "regions": region_list(spec),
                              "cap": False})
            continue
        anchors = _anchors(spec, sockets, warnings, i)
        if not anchors:
            plans.append({"i": i, "kind": kind, "spec": spec, "failed": "no anchor"})
            continue
        for pos, sock in anchors:
            pl = {"i": i, "kind": kind, "spec": spec, "anchor": pos, "socket": sock, "regions": [], "cap": False}
            if kind == "gatling":
                axis = Vector(spec["axis"]) if spec.get("axis") else (
                    sock.matrix_world.to_3x3() @ Vector((0, 0, 1)) if sock is not None
                    else Vector((math.copysign(1.0, pos.x), 0, 0)))
                axis.normalize()
                blen = float(spec.get("length") or (sock.get("barrel_length_m") if sock is not None else 0) or 5.0)
                c = float(spec.get("cluster_d") or min(1.3, blen * 0.2))
                pl.update(axis=axis, length=blen, cluster_d=c)
                stub = float((sock.get("barrel_length_m") if sock is not None else 0) or blen)
                rr = spec.get("remove", {"radius": None})
                if rr is not False:
                    if isinstance(rr, dict) and any(k in rr for k in ("cylinder", "sphere", "box")):
                        pl["regions"] = [rr]
                    else:
                        rad = float((rr or {}).get("radius") or max(0.6, c * 0.75))
                        pl["regions"] = [{"cylinder": {"p0": list(pos - axis * float((rr or {}).get("inset", 0.2))),
                                                       "p1": list(pos + axis * (stub + 0.8)), "radius": rad}}]
                    pl["remove_radius"] = (pl["regions"][0].get("cylinder") or {}).get("radius", c)
            elif kind == "nozzle":
                axis = Vector(spec.get("axis") or (0, -1, 0)).normalized()
                D = float(spec.get("diameter") or (sock.get("mount_d_m") if sock is not None else 0) or 2.0)
                ln = float(spec.get("length") or D * 0.8)
                cl = float(spec.get("collar_len") or D * 0.28)
                pl.update(axis=axis, D=D, length=ln, collar=cl)
                rr = spec.get("remove", {})
                if rr is not False:
                    if isinstance(rr, dict) and any(k in rr for k in ("cylinder", "sphere", "box")):
                        pl["regions"] = [rr]
                    else:
                        rr = rr or {}
                        pl["regions"] = [{"cylinder": {"p0": list(pos + axis * 0.5),
                                                       "p1": list(pos - axis * float(rr.get("depth", ln + 0.25 * cl))),
                                                       "radius": float(rr.get("radius", 0.56 * D))}}]
                    pl["cap"] = bool((spec.get("remove") or {}).get("cap", False)) if isinstance(spec.get("remove"),
                                                                                                dict) else False
            elif kind in ("antenna", "mast"):
                pl.update(height=float(spec.get("height", 3.0)),
                          style=str(spec.get("style", "mast" if kind == "mast" else "whip")))
                rr = spec.get("remove")
                if isinstance(rr, dict):
                    pl["remove_spec"] = rr
            plans.append(pl)
    return plans


def place(pl, bvh, L, lo, hi, warnings):
    """Compute the part frame (pl['M']) on the hull after the removals."""
    kind, spec = pl["kind"], pl["spec"]
    big = 4.0 * L
    if kind == "gatling":
        pos, axis = pl["anchor"], pl["axis"]
        seat = spec.get("seat", "auto")
        if seat == "auto":
            # rays from outside towards the hull, around the removed stub: median hit = bay wall / hull side
            side = axis.cross(Vector((0, 0, 1)))
            if side.length < 1e-6:
                side = axis.cross(Vector((1, 0, 0)))
            side.normalize()
            up = side.cross(axis).normalized()
            r = float(pl.get("remove_radius", pl["cluster_d"])) * 1.25
            ts = []
            start = pos + axis * (pl["length"] + 3.0)
            for off in (side * r, -side * r, up * r, -up * r):
                hp, hn = snap(bvh, start + off, -axis, pl["length"] + 8.0)
                if hp is not None:
                    ts.append((hp - pos).dot(axis))
            t = float(np.median(ts)) if ts else 0.0
            t = max(-1.5, min(t, pl["length"] * 0.6))
            pl["seat_t"] = round(t, 3)
        else:
            t = float(seat)
            pl["seat_t"] = t
        pl["M"] = kl.frame_matrix(pos + axis * t, axis, (0, 0, 1))
        return True
    if kind == "nozzle":
        pl["M"] = kl.frame_matrix(pl["anchor"], pl["axis"], (0, 0, 1))
        return True
    if kind == "block":
        a = pl["anchor"]
        M = kl.frame_matrix(Vector(a) if not isinstance(a, list) else Vector(a + [0.0] * (3 - len(a))),
                            Vector(spec.get("axis") or (0, 1, 0)), Vector(spec.get("up") or (0, 0, 1)))
        pl["M"] = M
        return True
    if kind in ("antenna", "mast", "nav_light", "vent_grille"):
        a = pl["anchor"]
        snap_mode = str(spec.get("snap", "top" if kind in ("antenna", "mast") else "auto")).lower()
        if kind == "vent_grille" or (isinstance(a, list) and len(a) == 2 and kind == "nav_light"
                                     and snap_mode in ("side", "port", "starboard", "auto")):
            sides = {"port": [-1], "starboard": [1], "both": [-1, 1]}.get(
                snap_mode if snap_mode in ("port", "starboard") else str(spec.get("side", "port")), [-1])
            if pl.get("side_sign") is None and len(sides) > 1:
                # expand: one plan per side (handled by the caller through 'side_sign')
                pl["side_sign"] = sides[0]
                pl["_twin"] = sides[1]
            sgn = pl.get("side_sign", sides[0])
            y, z = float(a[0]), float(a[1])
            hp, hn = snap(bvh, (sgn * big, y, z), (-sgn, 0, 0), 2 * big)
            if hp is None:
                return False
            pl["M"] = kl.frame_z(hp, hn, (0, 1, 0))
            return True
        if isinstance(a, list) and len(a) == 2:
            x, y = float(a[0]), float(a[1])
            hp, hn = snap(bvh, (x, y, hi[2] + 5.0), (0, 0, -1), hi[2] - lo[2] + 10.0)
            if pl.get("roof_z") is not None and (hp is None or hp.z < pl["roof_z"] - 0.5):
                hp, hn = Vector((x, y, pl["roof_z"])), Vector((0, 0, 1))  # the stub cut left a hole: sit on the roof
            if hp is None:
                return False
            if kind in ("antenna", "mast"):
                pl["M"] = kl.frame_z(hp, (0, 0, 1), (0, 1, 0))
            else:
                pl["M"] = kl.frame_z(hp, hn, (0, 1, 0))
            pl["surface"] = hp
            return True
        p = Vector(a)
        if snap_mode == "none":
            n = Vector(spec.get("normal") or (0, 0, 1))
            pl["M"] = kl.frame_z(p, n, (0, 1, 0))
            return True
        # 3D point: nearest hull surface
        loc, nrm, _, d = bvh.find_nearest(p, 0.1 * L)
        if loc is None:
            return False
        pl["M"] = kl.frame_z(loc, nrm if kind != "antenna" else Vector((0, 0, 1)), (0, 1, 0))
        return True
    return True


LOD_MAX = {"gatling": 2, "nozzle": 2, "piping": 1, "antenna": 0, "mast": 1, "nav_light": 0, "vent_grille": 1,
           "block": 2}


def build(kit, pl):
    kind, spec = pl["kind"], pl["spec"]
    if kit.lod > int(spec.get("lod_max", LOD_MAX.get(kind, 2))):
        return
    if kind == "gatling":
        hs = spec.get("housing")
        info = kl.gatling(kit, pl["M"], pl["length"], pl["cluster_d"], tuple(hs) if hs else None,
                          int(spec.get("barrels", 6)))
        pl["muzzle_world"] = pl["M"] @ Vector(info["muzzle"])
    elif kind == "nozzle":
        kl.nozzle(kit, pl["M"], pl["D"], pl["length"], spec.get("throat_d"), pl["collar"],
                  int(spec.get("ribs", 28)), int(spec.get("bands", 3)), int(spec.get("actuators", 4)),
                  plate_d=spec.get("plate_d"), inner_rings=int(spec.get("inner_rings", 2)))
    elif kind == "piping":
        n = int(spec.get("count", 1))
        spread = Vector(spec.get("spread") or (0, 0.6, 0))
        if pl["points"][0].x < 0 and spec.get("mirror") == "x" and spread.x:
            spread = Vector((-spread.x, spread.y, spread.z))
        for k in range(n):
            off = spread * (k - (n - 1) / 2.0)
            kl.tube(kit, [p + off for p in pl["points"]], float(spec.get("radius", 0.25)),
                    mat=str(spec.get("material", "trim")), flanges=bool(spec.get("flanges", True)))
    elif kind in ("antenna", "mast"):
        info = kl.antenna(kit, pl["M"], pl["height"], pl["style"], bool(spec.get("light", True)), spec.get("radius"))
        pl["tip_world"] = pl["M"] @ Vector(info["tip"])
    elif kind == "nav_light":
        kl.nav_light(kit, pl["M"], float(spec.get("size", 0.5)), str(spec.get("lens", "status")))
        pl["tip_world"] = pl["M"] @ Vector((0, 0, float(spec.get("size", 0.5)) * 0.5))
    elif kind == "vent_grille":
        kl.vent_grille(kit, pl["M"], tuple(spec.get("size", (4.0, 1.4))), int(spec.get("slats", 6)),
                       float(spec.get("depth", 0.12)))
    elif kind == "block":
        kl.block(kit, pl["M"], tuple(spec.get("size", (1.0, 1.0, 1.0))), str(spec.get("material", "graphite")),
                 float(spec.get("bevel", 0.05)), tuple(spec.get("taper", (1.0, 1.0))),
                 str(spec.get("panel", "") or ""))


# ----------------------------------------------------------------------------- run
def run(req):
    p = req.get("params") or {}
    warnings = []
    meta, asset_id, sets, col = load_input(req, warnings)
    root = sets[0][0]
    cls = str(p.get("budget_class") or meta.get("budget_class") or "ship_m")
    if cls not in sl.BUDGETS:
        raise ValueError("budget_class %r unknown" % cls)
    bud = sl.BUDGETS[cls]
    lo, hi = bounds(sets[0][1])
    L = float(meta.get("length_m") or (hi[1] - lo[1]))
    sockets = socket_objects(root)
    mats = material_table(sets)
    tris_before = {k: sl.mesh_tris(v[1]) for k, v in sets.items()}
    plans = plan_parts(p, sockets, L, warnings)
    # twins for 'both' sided vents / side-snapped lights
    expanded = []
    for pl in plans:
        expanded.append(pl)
        sp = pl["spec"]
        if pl["kind"] in ("vent_grille",) and str(sp.get("side", "port")) == "both":
            pl["side_sign"] = -1
            expanded.append(dict(pl, side_sign=1, regions=[]))
        if pl["kind"] == "nav_light" and isinstance(pl.get("anchor"), list) and len(pl["anchor"]) == 2 and \
                str(sp.get("snap", "side")) in ("side", "auto") and str(sp.get("side", "both")) == "both":
            pl["side_sign"] = -1
            expanded.append(dict(pl, side_sign=1, regions=[]))
    plans = expanded
    # ---- 1. removals on every LOD set
    removed = {}
    for pl in plans:
        regs = list(pl.get("regions") or [])
        if pl.get("remove_spec") and pl.get("anchor") is not None:
            pl["_late_remove"] = pl["remove_spec"]
        if not regs:
            continue
        for k, (r, objs) in sets.items():
            for o in objs:
                removed[k] = removed.get(k, 0) + kl.remove_faces(o, regs, cap=pl.get("cap", False))
    # antenna-style removals need the surface point: snap first on the untouched hull
    bvh = lod0_bvh(sets[0][1])
    late = [pl for pl in plans if pl.get("_late_remove")]
    for pl in late:
        a = pl["anchor"]
        rs = pl["_late_remove"]
        rad = float(rs.get("radius", 0.5))
        if isinstance(a, list) and len(a) == 2:
            # the roof under an AI antenna stub: median of rays around it (a ray on the stub hits its top)
            zs = []
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (0.7, 0.7), (-0.7, -0.7)):
                q, _ = snap(bvh, (float(a[0]) + dx * rad * 1.4, float(a[1]) + dy * rad * 1.4, hi[2] + 5.0),
                            (0, 0, -1), hi[2] - lo[2] + 10.0)
                if q is not None:
                    zs.append(q.z)
            if not zs:
                continue
            # the highest roof hit around the stub: never cut into a raised roof under it
            hp = Vector((float(a[0]), float(a[1]), float(np.max(zs))))
            pl["roof_z"] = float(np.max(zs))
        else:
            hp = Vector(a)
        reg = {"cylinder": {"p0": [hp.x, hp.y, hp.z + float(rs.get("above", 0.25))],
                            "p1": [hp.x, hp.y, hi[2] + 1.0], "radius": rad}}
        pl["regions"] = [reg]
        for k, (r, objs) in sets.items():
            for o in objs:
                removed[k] = removed.get(k, 0) + kl.remove_faces(o, [reg], cap=bool(rs.get("cap", True)))
    bvh = lod0_bvh(sets[0][1])
    # ---- 2. placement on the trimmed LOD0 hull
    failed = []
    for pl in plans:
        if pl["kind"] in ("remove", "piping") or pl.get("failed"):
            if pl.get("failed"):
                failed.append("parts[%d] %s: %s" % (pl["i"], pl["kind"], pl["failed"]))
            continue
        if not place(pl, bvh, L, lo, hi, warnings):
            pl["failed"] = "no hull surface at %s" % (pl.get("anchor"),)
            failed.append("parts[%d] %s: %s" % (pl["i"], pl["kind"], pl["failed"]))
    live = [pl for pl in plans if not pl.get("failed") and pl["kind"] != "remove"]
    # ---- 3. build per LOD and merge by material
    part_tris = {}
    for k, (r, objs) in list(sets.items()):
        suffix = "" if k == 0 else "_lod%d" % k
        kit = sl.Kit(lod=k, size_ref=L)
        per_kind = {}
        for pl in live:
            before = kit.tri_count()
            build(kit, pl)
            per_kind[pl["kind"]] = per_kind.get(pl["kind"], 0) + kit.tri_count() - before
        new_objs, added = merge_kit(kit, objs, r, suffix, mats)
        kit.bm.free()
        sets[k] = (r, new_objs)
        part_tris["lod%d" % k] = per_kind
    # ---- 4. sockets
    for pl in live:
        sp = pl["spec"]
        if pl["kind"] == "gatling":
            sock = pl.get("socket")
            if sock is not None:
                sock["builtin"] = "gatling_%dbarrel" % int(sp.get("barrels", 6))
                sock["seat_offset_m"] = pl.get("seat_t", 0.0)
            ms = sp.get("muzzle_socket", True)
            if ms and pl.get("muzzle_world") is not None:
                if isinstance(ms, str):
                    name = ms
                elif sock is not None and sock.name.startswith("socket_turret_"):
                    name = "socket_muzzle_" + sock.name[len("socket_turret_"):]
                else:
                    name = "socket_muzzle_%02d" % (len([n for n in sockets if n.startswith("socket_muzzle_")]) + 1)
                old = sockets.get(name)
                if old is not None:
                    bpy.data.objects.remove(old, do_unlink=True)
                # muzzle: glTF -Z (Blender +Y) = fire direction, glTF +Y (Blender +Z) = up; a gun firing up or
                # down takes the ship's forward as its 'up'
                zax = (0, 0, 1) if abs(pl["axis"].z) < 0.9 else (0, 1, 0)
                e = sl.add_socket(name, "muzzle", tuple(pl["muzzle_world"]), z_axis=zax, y_hint=tuple(pl["axis"]),
                                  parent=root, size=L * 0.012, mount=sock.name if sock is not None else "",
                                  diameter_m=round(pl["cluster_d"] * 2.5, 3),  # flash envelope: muzzle_flash scales by it
                                  confidence=0.95, heuristic="kitbash gatling muzzle")
                sockets[name] = e
        elif pl["kind"] == "nozzle" and pl.get("socket") is not None:
            s = pl["socket"]
            s["mount_d_m"] = round(pl["D"], 3)
            s["size_class"] = sl.size_class_for(pl["D"])
            s["builtin"] = "nozzle_ribbed_bell"
        elif pl["kind"] in ("antenna", "mast", "nav_light") and sp.get("socket"):
            names = sp["socket"] if isinstance(sp["socket"], list) else [sp["socket"]]
            idx = [q for q in live if q["spec"] is sp].index(pl)
            if idx >= len(names):
                continue
            name = names[idx]
            tip = pl.get("tip_world") or pl["M"].translation
            old = sockets.get(name)
            kind_s = name.split("_")[1] if name.startswith("socket_") else "utility"
            extras = {}
            if old is not None:
                extras = {k_: old[k_] for k_ in old.keys() if k_ not in ("game_socket", "kind", "_RNA_UI")}
                bpy.data.objects.remove(old, do_unlink=True)
            if pl["kind"] == "nav_light":
                n = pl["M"].to_3x3() @ Vector((0, 0, 1))
                zax = (math.copysign(1.0, n.x), 0, 0) if abs(n.x) > 0.3 else tuple(n)
            else:
                zax = (0, 0, 1)
            extras.update(confidence=0.95, heuristic="kitbash %s" % pl["kind"])
            sockets[name] = sl.add_socket(name, kind_s, tuple(tip), z_axis=zax, parent=root, size=L * 0.012,
                                          **extras)
    for name in p.get("drop_sockets") or []:
        if name in sockets:
            bpy.data.objects.remove(sockets.pop(name), do_unlink=True)
    # ---- 5. collision, export, checks
    objs0 = sets[0][1]
    if col is not None:
        cme = col.data
        bpy.data.objects.remove(col, do_unlink=True)
        if cme.users == 0:
            bpy.data.meshes.remove(cme)
    col, col_info = sl.convex_collision(objs0, parent=root)
    empties = [o for o in root.children if o.type == "EMPTY"]
    bpy.context.view_layer.update()
    out_glb = af.out_path(req, "ship.glb")
    af.export_glb(out_glb, [root] + objs0 + [col] + empties, animations=False, skins=False, export_tangents=False,
                  export_image_format="AUTO", export_draco_mesh_compression_enable=False)
    summ = sl.glb_summary(out_glb)
    outputs = {"glb": out_glb}
    lod_tris, lod_paths = {}, {}
    if p.get("lods", True):
        for k in (1, 2):
            if k not in sets:
                continue
            r, objs = sets[k]
            path = af.out_path(req, "ship_lod%d.glb" % k)
            af.export_glb(path, [r] + objs, animations=False, skins=False, export_tangents=False,
                          export_image_format="AUTO", export_draco_mesh_compression_enable=False)
            lod_tris["lod%d" % k] = sl.glb_summary(path)["triangles"]
            outputs["lod%d" % k] = lod_paths["lod%d" % k] = path
    checks = sl.budget_checks(summ, cls, lod_tris or None)
    checks.append(af.check("collision_vertices", col_info["vertices"] <= 64, col_info["vertices"], 64))
    checks.append(af.check("kitbash_parts_placed", not failed, failed, [],
                           note="every part spec resolved its anchor and found the hull"))
    thr = [x for x in summ["sockets"] if x["extras"].get("kind") == "thruster"]
    frame = meta.get("thruster_frame") or (thr[0]["extras"].get("frame") if thr else "thrust") or "thrust"
    if thr:
        checks.append(af.check("thruster_frame", all(sl.thruster_frame_ok(x, frame) for x in thr), len(thr),
                               sl.THRUSTER_AXIS_TEXT.get(frame, frame)))
        checks.append(sl.thruster_diameter_check(thr))
    if any(x["game_socket"].startswith("socket_fx_nav_") for x in summ["sockets"]):
        checks.append(ship_gen.nav_checks(summ))
    mz = [x for x in summ["sockets"] if x["extras"].get("kind") == "muzzle"]
    if mz:
        pos = {x["game_socket"]: np.array(x["position_gltf"]) for x in summ["sockets"]}
        bad = []
        for x in mz:
            m = x["extras"].get("mount")
            if m in pos:
                d = np.array(x["position_gltf"]) - pos[m]
                if np.linalg.norm(d) > 1e-6 and float(np.dot(d / np.linalg.norm(d), x["neg_z_gltf"])) < 0.9:
                    bad.append(x["game_socket"])
        checks.append(af.check("muzzle_frame_fire_axis", not bad, bad, [],
                               note="glTF -Z of every muzzle points away from its mount (fire direction)"))
    lod_sil = sl.lod_silhouettes(out_glb, lod_paths) if lod_paths else {}
    for key, v in lod_sil.items():
        checks.append(af.check("silhouette_iou_%s" % key, min(v["top"], v["side"]) >= 0.85, v, 0.85,
                               note="gate 10: top/side silhouette IoU vs LOD0 (same window)"))
    if p.get("preview", True):
        col.hide_render = True
        for k, (r, objs) in sets.items():
            for o in objs:
                o.hide_render = k != 0
        outputs["preview"] = sl.render_grid(af.out_path(req, "preview.png"), objs0, tile=int(p.get("preview_size", 300)),
                                            views=("three_quarter_long", "rear_quarter_long", "side", "top", "front"),
                                            color_type="TEXTURE")
        if len(sets) > 1:
            outputs["lod_strip"] = sl.lod_strip(af.out_path(req, "lod_strip.png"), [sets[k][1] for k in sorted(sets)],
                                                tile=int(p.get("preview_size", 300)), color_type="TEXTURE")
    man = meta.get("manifest")
    if man:
        man = dict(man)
        man["triangles"] = {"lod0": summ["triangles"]}
        man["triangles"].update({k: int(v) for k, v in lod_tris.items()})
        man["surfaces_lod0"] = summ["surfaces_lod0"]
        man["materials"] = sorted(set(summ["material_names_used"]))
        man["sockets"] = sl.socket_manifest_rows(empties)
        man["glb_bytes"] = int(summ["bytes"])
        man["geometry_hash"] = summ["geometry_hash"]
        sx, sy, sz = summ["size_gltf"]
        man["dimensions_m"] = {"length": round(sz, 3), "width": round(sx, 3), "height": round(sy, 3)}
        if isinstance(man.get("collision"), dict) and isinstance(man["collision"].get("simple"), dict):
            man["collision"]["simple"]["vertices"] = col_info["vertices"]
        files = dict(man.get("files") or {})
        files.update({"game_glb": "ship.glb", "source_blend": "ship.blend"})
        if lod_paths:
            files["lod_glb"] = [os.path.basename(lod_paths[k]) for k in sorted(lod_paths)]
        if outputs.get("preview"):
            files["preview"] = ["preview.png"]
        man["files"] = files
        gv = str(man.get("generator_version", ""))
        if GEN_KITBASH not in gv:
            man["generator_version"] = (gv + " + " if gv else "") + GEN_KITBASH
        val = dict(man.get("validation") or {})
        val["date"] = sl.today()
        val.pop("inspector", None)
        man["validation"] = val
        man["notes"] = (str(man.get("notes", "")) + "; kitbash: %s" % ", ".join(
            "%s x%d" % (k, sum(1 for q in live if q["kind"] == k)) for k in KINDS
            if any(q["kind"] == k for q in live)))[:1800]
        outputs["manifest"] = sl.write_json(af.out_path(req, "ship.json"), man)
    meta2 = dict(meta, step="kitbash", asset_id=asset_id, budget_class=cls, manifest=man or meta.get("manifest"),
                 kitbash=[{k_: v for k_, v in pl["spec"].items()} for pl in plans if pl.get("spec")][:64])
    bpy.context.scene["af_ship"] = json.dumps(af._jsonable(meta2), ensure_ascii=False)
    outputs["blend"] = sl.save_blend_clean(af.out_path(req, "ship.blend"))
    stats = {"asset_id": asset_id, "budget_class": cls, "tris": summ["triangles"], "tris_lod1": lod_tris.get("lod1"),
             "tris_lod2": lod_tris.get("lod2"), "tris_before": tris_before, "removed_faces": removed,
             "part_tris": part_tris, "surfaces": summ["surfaces_lod0"], "materials": summ["material_names_used"],
             "nodes": summ["nodes"], "glb_mb": summ["mb"], "lod_silhouette_iou": lod_sil, "collision": col_info,
             "parts": [{"i": pl["i"], "kind": pl["kind"], "at": [round(c, 3) for c in pl["M"].translation]
                        if pl.get("M") else None, "seat_t": pl.get("seat_t"),
                        "socket": pl["socket"].name if pl.get("socket") is not None else None}
                       for pl in live if pl["kind"] != "piping"],
             "sockets": [{"name": x["game_socket"], "position_gltf": x["position_gltf"],
                          "kind": x["extras"].get("kind")} for x in summ["sockets"]],
             "dimensions_m": summ["size_gltf"]}
    return {"outputs": outputs, "stats": stats, "checks": checks, "warnings": warnings}
