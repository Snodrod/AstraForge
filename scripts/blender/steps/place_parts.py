"""place_parts - put copies of a part onto a target mesh surface (or at explicit poses).

Generic: clasps, buttons, studs, rivets, gems, badges, spikes on a garment, an armour or a hull, or
any part at a given pose (e.g. a procedural hand at a wrist).

inputs:
  mesh | blend    the target (.glb/.gltf/.fbx/.obj/.stl/.blend). All its meshes are joined into one
                  object, unless params.target_objects lists the ones to keep - the others are
                  dropped (e.g. keep only "body" of a separate.py parts file). A rig is dropped.
  part            the default part (.glb/.blend/...; or params.part): all its meshes form one
                  part, a rig is dropped, its sockets are kept (part_sockets). Part frame (bone_kit bone_toggle convention): origin at the
                  centre of the back face, local -Y = outward normal of the surface it sits on,
                  local +Z = the part's "up"
params:
  points          list of [x, z] (front-view metres), [x, y, z], or dicts:
                    at         [x, z] | [x, y, z]
                    part       path overriding inputs.part for this point
                    name       object name (join false) / report name (default "<part>_<i>")
                    scale      uniform scale (1.0)
                    roll_deg   turn about the surface normal, counter-clockwise seen from outside (0)
                    snap       "surface" (default: raycast) | "none" (no ray: the frame is rot_deg /
                               matrix and the origin goes to the 3D point `at`)
                    rot_deg    [x, y, z] Euler XYZ in degrees (snap none)
                    matrix     3x3 rotation, rows (snap none; overrides rot_deg)
                    standoff_m extra offset along the outward normal (0)
  ray             direction the rays travel: "+Y" (default: parts on surfaces facing -Y, the front
                  of a character, the rays come from in front) | "-Y" | "+X" | "-X" | "+Z" | "-Z".
                  An [x, z] point is shot from outside the target bbox along the ray; an [x, y, z]
                  point takes the hit nearest to it on the line through it.
  contact         "footprint" (default): rays across the part's back-face footprint, the part rests
                  on the outermost contacts (it bridges a gap or a fold instead of sinking into it)
                  | "center": the back-face centre goes to the centre hit
  footprint_grid  [nx, nz] rays over the footprint (default [5, 3]); footprint_fill (0.8 of the
                  part's X / Z extent); rest_percentile (100: rest on the outermost contact; lower
                  lets a lone bump poke into the part instead of lifting it; also per point)
  normal          "fit" (default: starts against the ray, then a robust plane through a square
                  patch of contacts as wide as the part, ignoring what lies deep behind it - a gap or
                  an opening; normal_patch 1.0 = patch side / longest part side, larger averages
                  over folds; max_tilt_deg 45 from the ray; both also per point) | "hit" (face
                  normal at the centre hit) | "axis" (exactly against the ray). With contact "center" the
                  normal is the ray axis unless "hit"
  up              world up that orients the part's +Z (default [0, 0, 1]; the ray axis falls back to
                  +Y)
  search_m        footprint / 3D-point ray search distance (default max(0.1, 3 x part size))
  join            true (default): target + parts become one mesh object; false: every copy is its
                  own object (origin at its back-face centre) next to the target
  name            name of the joined object (default: the target's first object name)
  height_m, rotate_z_deg, ground, center
                  optional normalisation of the target first (default off: the points refer to the
                  stored frame)
  preview         true: preview.png (front and 3/4 of the result + one close-up per point);
                  preview_size (512)
  part_sockets    true (default): socket empties of a part (socket_* / game_socket, e.g. the
                  socket_mount_hand_<side> of a bone_kit hand on its grip axis) are placed with every
                  copy (a repeated name gets a _2, _3 suffix); points[].sockets false skips one copy.
                  Sockets of the target are always kept (they follow its normalisation).
Part materials are kept (identical '.001' copies merged), the part's UV layer is renamed to the
target's, and colour attributes are completed with white on the meshes that lack them (glTF
multiplies COLOR_0 into the base colour of every primitive).
outputs: placed.glb, placed.blend, preview.png
stats: per point - position, outward normal, rotation rows, scale, contacts / support rays, rest
offset (outermost contact in front of the centre hit), gap under the part (median depth of the
other contacts), penetration (part vertices inside the target by a 3-ray parity vote: share and
max depth), tris.
checks: points_hit (error), no_nan_verts (error), part_frame (warning: the part has geometry behind
its back face), penetration (warning: a part vertex deeper than 30 % of the part thickness).
"""
import math
import os
import re
import time

import bpy
import numpy as np
from mathutils import Matrix, Vector
from mathutils.bvhtree import BVHTree

import af_common as af
from steps import _mesh_lib as ml

RAYS = {"+Y": (0, 1, 0), "-Y": (0, -1, 0), "+X": (1, 0, 0), "-X": (-1, 0, 0),
        "+Z": (0, 0, 1), "-Z": (0, 0, -1)}


# --------------------------------------------------------------------------- loading

def _base(name):
    return re.sub(r"\.\d{3}$", "", name)


def _load_target(req, p, warnings, sock_recs=None):
    src = ml.input_path(req)
    af.reset_scene()
    objs, helpers = ml.purge_import_helpers(af.import_any(src))
    objs = ml.prepare_meshes(objs, warnings, sockets=sock_recs)
    names = p.get("target_objects")
    dropped = []
    if names:
        names = [str(n) for n in names]
        keep = [o for o in objs if o.name in names or _base(o.name) in names]
        missing = [n for n in names if not any(o.name == n or _base(o.name) == n for o in keep)]
        if missing:
            raise ValueError("params.target_objects not found in %s: %s (has %s)"
                             % (os.path.basename(src), missing, sorted(o.name for o in objs)))
        for o in objs:
            if o not in keep:
                dropped.append(o.name)
                ml.remove_object(o)
        objs = keep
    if not objs:
        raise ValueError("no mesh objects in %s" % src)
    first = objs[0].name
    ob = af.join(objs, name="__target__")
    ob.data.name = "__target__"
    return ob, first, src, helpers, dropped


class PartCache:
    """One template object per part file (meshes joined, transforms applied, hidden)."""

    def __init__(self, warnings):
        self.items = {}
        self.warnings = warnings

    def get(self, path):
        path = os.path.abspath(str(path))
        if path in self.items:
            return self.items[path]
        if not os.path.isfile(path):
            raise FileNotFoundError("part not found: %s" % path)
        before = set(bpy.data.objects)
        objs, _h = ml.purge_import_helpers(af.import_any(path))
        sock_recs = []
        objs = ml.prepare_meshes(objs, self.warnings, sockets=sock_recs)
        for o in [x for x in bpy.data.objects if x not in before and x.type != "MESH"]:
            bpy.data.objects.remove(o, do_unlink=True)
        if not objs:
            raise ValueError("no mesh objects in part %s" % path)
        tpl = af.join(objs, name="__part_%d__" % len(self.items))
        tpl.data.name = tpl.name
        tpl.hide_render = True
        tpl.hide_set(True)
        co = ml.mesh_arrays(tpl.data)["co"]
        lo, hi = co.min(0), co.max(0)
        thick = float(hi[1] - lo[1])
        item = {"obj": tpl, "path": path, "stem": ml.safe_name(os.path.splitext(os.path.basename(path))[0]),
                "lo": lo, "hi": hi, "size": float(np.max(hi - lo)), "thickness": thick,
                "tris": ml.tris_of(tpl), "sockets": sock_recs,
                "behind_back_m": float(hi[1])}   # geometry at local +Y > 0 lies behind the back face
        self.items[path] = item
        return item

    def remove(self):
        for it in self.items.values():
            ml.remove_object(it["obj"])


# --------------------------------------------------------------------------- frames

def frame_from_normal(n, up, roll_deg=0.0):
    """Rotation whose local -Y is the outward normal n and +Z the world up projected onto the
    surface plane (turned by roll_deg about n). Returns a 3x3 Matrix (columns = local axes)."""
    n = Vector(n).normalized()
    u = Vector(up).normalized()
    z = u - n * u.dot(n)
    if z.length < 1e-3:   # surface faces up/down: fall back to +Y (or +X) as "up"
        for alt in ((0, 1, 0), (1, 0, 0)):
            a = Vector(alt)
            z = a - n * a.dot(n)
            if z.length > 1e-3:
                break
    z.normalize()
    if roll_deg:
        z = Matrix.Rotation(math.radians(float(roll_deg)), 3, n) @ z
    y = -n
    x = y.cross(z).normalized()
    z = x.cross(y).normalized()
    return Matrix((x, y, z)).transposed()


def explicit_rotation(pt):
    if pt.get("matrix") is not None:
        m = Matrix([[float(v) for v in row] for row in pt["matrix"]])
        if m.determinant() < 0:
            raise ValueError("points[].matrix must be a rotation (determinant > 0)")
        return m.normalized()
    r = [math.radians(float(v)) for v in (pt.get("rot_deg") or (0, 0, 0))]
    from mathutils import Euler
    return Euler(r, "XYZ").to_matrix()


def plane_normal(points, guess):
    P = np.asarray(points, float)
    c = P.mean(0)
    _u, s, vt = np.linalg.svd(P - c, full_matrices=False)
    if len(s) < 3 or s[1] < 1e-6 * max(s[0], 1e-12):   # collinear contacts: keep the guess
        return None
    n = Vector(vt[2].tolist())
    if n.dot(Vector(guess)) < 0:
        n = -n
    return n.normalized()


def robust_plane_normal(points, guess, keep=0.75, iters=4):
    """Plane through a contact patch that ignores what lies deep behind it (a gap, an opening,
    a fold): least squares, then repeatedly drop the deepest (1 - keep) of the points behind the
    current plane and refit. Returns the outward normal or None (too few / collinear points)."""
    P = np.asarray(points, float)
    if len(P) < 3:
        return None
    n = plane_normal(P, guess)
    if n is None:
        return None
    for _ in range(iters):
        r = (P - P.mean(0)) @ np.array(n)
        sel = P[r >= np.percentile(r, 100 * (1 - keep))]
        if len(sel) < 4:
            break
        n2 = plane_normal(sel, guess)
        if n2 is None:
            break
        if (n2 - n).length < 1e-5:
            n = n2
            break
        n = n2
        P = sel if len(sel) >= 6 else P
    return n


# --------------------------------------------------------------------------- placement

def _norm_point(i, raw):
    pt = dict(raw) if isinstance(raw, dict) else {"at": raw}
    at = pt.get("at")
    try:
        at = [float(v) for v in at]
    except Exception as exc:  # noqa: BLE001
        raise ValueError("points[%d].at must be [x, z] or [x, y, z]" % i) from exc
    if len(at) not in (2, 3) or not all(math.isfinite(v) for v in at):
        raise ValueError("points[%d].at must be [x, z] or [x, y, z] (finite numbers)" % i)
    pt["at"] = at
    snap = str(pt.get("snap", "surface")).lower()
    if snap not in ("surface", "none"):
        raise ValueError("points[%d].snap must be 'surface' or 'none'" % i)
    if snap == "none" and len(at) != 3:
        raise ValueError("points[%d]: snap 'none' needs a 3D point" % i)
    pt["snap"] = snap
    pt["scale"] = float(pt.get("scale", 1.0))
    if not pt["scale"] > 0:
        raise ValueError("points[%d].scale must be > 0" % i)
    return pt


def _cast_line(bvh, origin, d, dist):
    hit = bvh.ray_cast(Vector(origin), Vector(d), dist)
    return hit if hit[0] is not None else None


def snap_to_surface(bvh, lo, hi, pt, part, ray, cfg):
    """Find the placement of one part on the surface. Returns a dict or None (no hit)."""
    d = Vector(ray)
    n_guess = -d
    at = pt["at"]
    k_ax = [i for i in range(3) if abs(ray[i]) > 0.5][0]
    if len(at) == 2:   # view-plane coordinates: the two axes other than the ray, in xyz order
        others = [i for i in range(3) if i != k_ax]
        start = [0.0, 0.0, 0.0]
        start[others[0]], start[others[1]] = at[0], at[1]
        span = float(np.max(hi - lo)) + 1.0
        start[k_ax] = (lo[k_ax] - 1.0) if ray[k_ax] > 0 else (hi[k_ax] + 1.0)
        hit = _cast_line(bvh, start, d, span * 4)
    else:
        P = Vector(at)
        cands = []
        for sgn in (1, -1):   # nearest surface point on the line through P
            o = P - d * sgn * cfg["search"]
            h = _cast_line(bvh, o, d * sgn, 2 * cfg["search"])
            if h is not None:
                cands.append(h)
        hit = min(cands, key=lambda h: (h[0] - P).length) if cands else None
    if hit is None:
        return None
    h0 = hit[0].copy()
    fn = hit[1].normalized()
    if fn.dot(n_guess) < 0:
        fn = -fn
    # "hit" trusts the face normal at the centre hit; "fit" and "axis" start against the ray (a
    # single face normal of a noisy generated surface can be off by tens of degrees) and "fit"
    # refines it on the contacts the part actually rests on
    n = fn.copy() if cfg["normal"] == "hit" else n_guess.copy()
    s = pt["scale"]
    roll = float(pt.get("roll_deg", 0.0))
    R = frame_from_normal(n, cfg["up"], roll)
    contacts, support, rest = [], [], 0.0
    if cfg["contact"] == "footprint":
        nx, nz = cfg["grid"]
        f = cfg["fill"]
        xs = np.linspace(part["lo"][0], part["hi"][0], nx) * f * s if nx > 1 else np.zeros(1)
        zs = np.linspace(part["lo"][2], part["hi"][2], nz) * f * s if nz > 1 else np.zeros(1)
        tol0 = max(0.15 * float(part["hi"][2] - part["lo"][2]) * s, 0.002)
        tol_max = max(0.5 * float(np.max(part["hi"] - part["lo"])) * s, tol0)
        max_tilt = math.radians(cfg["max_tilt"])

        # the normal is fitted on a square patch as wide as the part's longest side: a thin
        # footprint (a toggle is 10 x 3 cm) cannot constrain the tilt about its long axis
        side = float(np.max(part["hi"] - part["lo"])) * f * s * cfg["patch"]
        ps = np.linspace(-side / 2, side / 2, max(nx, 7))
        patch = [(float(a), float(b)) for a in ps for b in ps]
        foot = [(float(a), float(b)) for a in xs for b in zs]

        def cast_footprint(R_, n_, grid_pts=None):
            out = []
            for gx, gz in (grid_pts or foot):
                q = h0 + R_ @ Vector((gx, 0.0, gz))
                h = _cast_line(bvh, q + n_ * cfg["search"], -n_, 2 * cfg["search"])
                if h is not None:
                    out.append(np.array(h[0]))
            return np.array(out).reshape(-1, 3)

        def supporting(C, n_):
            """Outermost contacts along n_: the band widens until it holds a plane (>= 3 points
            not on one line) or reaches half the part size."""
            depth = (C - np.array(h0)) @ np.array(n_)
            tol = tol0
            while True:
                sup = C[depth >= depth.max() - tol]
                if (len(sup) >= 3 and plane_normal(sup, n_guess) is not None) or tol >= tol_max:
                    return sup
                tol = min(tol * 1.6, tol_max)

        for _it in range(5 if cfg["normal"] == "fit" else 1):
            C = cast_footprint(R, n, patch if cfg["normal"] == "fit" else None)
            if not len(C):
                break
            support = supporting(C, n)
            if cfg["normal"] != "fit" or len(C) < 3:
                break
            n2 = robust_plane_normal(C, n_guess)
            if n2 is None:
                break
            ang = math.acos(max(-1.0, min(1.0, n2.dot(n_guess))))
            if ang > max_tilt:   # keep the normal within max_tilt_deg of the ray axis
                axis = n_guess.cross(n2)
                n2 = (Matrix.Rotation(max_tilt, 3, axis.normalized()) @ n_guess) if axis.length > 1e-9 \
                    else n_guess.copy()
            if (n2 - n).length < 1e-4:
                break
            n = n2.normalized()
            R = frame_from_normal(n, cfg["up"], roll)
        C = cast_footprint(R, n)   # contacts of the final frame
        contacts = list(C)
        if len(C):
            depth = (C - np.array(h0)) @ np.array(n)
            support = supporting(C, n)
            # 100 = on the outermost contact (never sinks); lower values let a lone bump (a fold,
            # a collar rim) poke into the part instead of lifting it off the surface
            rest = float(np.percentile(depth, cfg["rest_pct"]))
    origin = h0 + n * (rest + float(pt.get("standoff_m", 0.0)))
    info = {"hit": list(h0), "hit_normal": list(fn), "normal": list(n), "rest_offset_m": rest,
            "contacts": len(contacts), "support": len(support)}
    if len(contacts):
        C = np.array(contacts)
        dep = (C - np.array(origin)) @ np.array(n)
        info["gap_median_m"] = float(np.median(-dep))
        info["gap_max_m"] = float((-dep).max())
    return origin, R, info


def _crossings(bvh, origin, d, limit=64):
    n, o = 0, Vector(origin)
    for _ in range(limit):
        h = bvh.ray_cast(o, d, 1e4)
        if h[0] is None:
            break
        n += 1
        o = h[0] + d * 1e-5
    return n


def penetration(bvh, co, thickness):
    """Part vertices inside the target: majority vote of three ray-parity tests (+X, -Y, -Z), so a
    single open seam or an inside-out patch of a generated surface does not flip the answer; the
    depth of an inside vertex is its distance to the nearest target surface. Returns the share of
    inside vertices and the max depth."""
    dirs = (Vector((1, 0, 0)), Vector((0, -1, 0)), Vector((0, 0, -1)))
    depth = []
    for v in co.tolist():
        if sum(_crossings(bvh, v, d) % 2 for d in dirs) >= 2:
            loc, _nor, _i, dist = bvh.find_nearest(Vector(v))
            depth.append(dist if loc is not None else 0.0)
    return {"inside_share": round(len(depth) / max(len(co), 1), 4),
            "max_depth_m": round(max(depth), 5) if depth else 0.0}


def _instance(part, M, name):
    me = part["obj"].data.copy()
    me.name = name
    me.transform(M)
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    return ob


def _harmonise(target, copies):
    """See _mesh_lib.harmonise_attributes (shared with replace_part keep_materials)."""
    return ml.harmonise_attributes(target, copies)


def _material_signature(mat):
    """Node types, unlinked input values, links and image names: two materials with the same
    signature render the same (Principled values, vertex-colour nodes, textures)."""
    if mat is None or not mat.use_nodes or mat.node_tree is None:
        return ("flat", tuple(round(float(c), 4) for c in mat.diffuse_color)) if mat else None
    nodes = []
    for nd in sorted(mat.node_tree.nodes, key=lambda n: (n.bl_idname, n.name.split(".")[0])):
        vals = []
        for s in nd.inputs:
            if s.is_linked or not hasattr(s, "default_value"):
                continue
            v = s.default_value
            try:
                vals.append((s.identifier, tuple(round(float(x), 4) for x in v)))
            except TypeError:
                vals.append((s.identifier, round(float(v), 4) if isinstance(v, (int, float)) else str(v)))
        extra = []
        if getattr(nd, "image", None) is not None:
            extra.append(nd.image.name.split(".")[0])
        if hasattr(nd, "layer_name"):
            extra.append(nd.layer_name)
        if hasattr(nd, "attribute_name"):
            extra.append(nd.attribute_name)
        nodes.append((nd.bl_idname, tuple(vals), tuple(extra)))
    links = sorted((l.from_node.bl_idname, l.from_socket.identifier, l.to_node.bl_idname,
                    l.to_socket.identifier) for l in mat.node_tree.links)
    return (tuple(nodes), tuple(links), mat.blend_method if hasattr(mat, "blend_method") else None)


def dedupe_materials():
    """Two copies of one part file re-import their materials as 'Bone' and 'Bone.001': remap a
    '.NNN' copy onto its base material when both render the same. Returns the merged names."""
    merged = []
    for m in list(bpy.data.materials):
        mt = re.match(r"^(.*)\.\d{3}$", m.name)
        if not mt:
            continue
        base = bpy.data.materials.get(mt.group(1))
        if base is None or base == m or _material_signature(base) != _material_signature(m):
            continue
        m.user_remap(base)
        merged.append(m.name)
        bpy.data.materials.remove(m)
    return merged


# --------------------------------------------------------------------------- previews

def _closeup(path, objs, centre, n, extent, size):
    scene = bpy.context.scene
    cd = bpy.data.cameras.new("af_pp_cam")
    cd.type = "ORTHO"
    cd.ortho_scale = extent
    cd.clip_start = 0.001
    cd.clip_end = 50.0
    cam = bpy.data.objects.new("af_pp_cam", cd)
    scene.collection.objects.link(cam)
    prev = scene.camera
    scene.camera = cam
    r = scene.render
    r.engine = af.engine_id("WORKBENCH")
    r.resolution_x = r.resolution_y = int(size)
    r.film_transparent = True
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGBA"
    sh = scene.display.shading
    sh.light = "STUDIO"
    sh.color_type = "TEXTURE"
    sh.show_cavity = True
    nn = Vector(n).normalized()
    cam.location = Vector(centre) + nn * 2.0
    up = Vector((0, 0, 1)) if abs(nn.z) < 0.9 else Vector((0, 1, 0))
    cam.rotation_euler = (-nn).to_track_quat("-Z", "Y").to_euler()
    zc = -nn
    xc = zc.cross(up).normalized()
    yc = xc.cross(zc).normalized()
    cam.matrix_world = Matrix.Translation(cam.location) @ Matrix((xc, yc, -zc)).transposed().to_4x4()
    r.filepath = path
    try:
        bpy.ops.render.render(write_still=True)
        px = ml.load_png(path)
    finally:
        scene.camera = prev
        bpy.data.objects.remove(cam)
        bpy.data.cameras.remove(cd)
        try:
            os.remove(path)
        except OSError:
            pass
    return px


def _preview(req, objs, placed, size):
    ml.set_active_basecolor_nodes(objs)
    synced = ml._sync_viewport_colors(objs)
    try:
        _, tiles = ml.render_sheet(af.out_path(req, "_pp_full.png"), objs,
                                   views=("front", "three_quarter"), size=size, color_type="TEXTURE")
        top = ml.flatten(np.concatenate(tiles, axis=1))
        cs = max(96, size // 2)
        row = []
        for rec in placed:
            ext = max(0.04, 3.2 * rec["size_m"])
            row.append(ml.flatten(_closeup(af.out_path(req, "_pp_c.png"), objs, rec["position"],
                                           rec["view_dir"], ext, cs)))
        if row:
            strip = np.concatenate(row, axis=1)
            w = max(strip.shape[1], top.shape[1])
            pad = lambda a: np.concatenate(  # noqa: E731
                [a, np.tile(np.array([0.82, 0.82, 0.84, 1.0], np.float32), (a.shape[0], w - a.shape[1], 1))],
                axis=1) if a.shape[1] < w else a
            sheet = np.concatenate([pad(strip), pad(top)], axis=0)   # Blender order: bottom row first
        else:
            sheet = top
        try:
            os.remove(af.out_path(req, "_pp_full.png"))
        except OSError:
            pass
        return ml.save_png(af.out_path(req, "preview.png"), sheet)
    finally:
        for m, col in synced:
            m.diffuse_color = col


# --------------------------------------------------------------------------- step

def run(req):
    p = req["params"]
    warnings, checks = [], []
    t0 = time.time()
    raw_points = p.get("points")
    if not raw_points or not isinstance(raw_points, (list, tuple)):
        raise ValueError("params.points must be a non-empty list of [x, z] / [x, y, z] / {at: ...}")
    points = [_norm_point(i, r) for i, r in enumerate(raw_points)]
    ray_key = str(p.get("ray", "+Y")).upper()
    if ray_key not in RAYS:
        raise ValueError("params.ray must be one of %s" % sorted(RAYS))
    ray = RAYS[ray_key]
    contact = str(p.get("contact", "footprint")).lower()
    if contact not in ("footprint", "center"):
        raise ValueError("params.contact must be 'footprint' or 'center'")
    normal_mode = str(p.get("normal", "fit")).lower()
    if normal_mode not in ("fit", "hit", "axis"):
        raise ValueError("params.normal must be 'fit', 'hit' or 'axis'")
    grid = [int(v) for v in (p.get("footprint_grid") or (5, 3))]
    if len(grid) != 2 or min(grid) < 1:
        raise ValueError("params.footprint_grid must be [nx, nz] with nx, nz >= 1")
    up = Vector([float(v) for v in (p.get("up") or (0, 0, 1))])
    if up.length < 1e-9:
        raise ValueError("params.up must not be zero")
    inputs = req.get("inputs") or {}
    default_part = inputs.get("part") or p.get("part")
    for i, pt in enumerate(points):
        if not (pt.get("part") or default_part):
            raise ValueError("points[%d]: no part (inputs.part or points[].part)" % i)

    tgt_socks = []
    target, first_name, src, helpers, dropped = _load_target(req, p, warnings, tgt_socks)
    sockets = ml.restore_sockets(tgt_socks)   # sockets of the target (e.g. socket_fx_eye_l/r) survive
    ml.rotate_z([target] + sockets, float(p.get("rotate_z_deg") or 0.0))
    if p.get("height_m") or p.get("ground") or p.get("center"):
        af.normalize([target] + sockets, height=p.get("height_m"), ground=bool(p.get("ground", False)),
                     center=bool(p.get("center", False)))
        ml.sockets_unit_scale(sockets)
    part_sockets = bool(p.get("part_sockets", True))
    socket_names = {e.name for e in sockets}
    socket_log = []
    tgt_metrics = ml.mesh_metrics([target])
    co = ml.mesh_arrays(target.data)["co"]
    lo, hi = co.min(0), co.max(0)
    me = target.data
    me.calc_loop_triangles()
    tri = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("vertices", tri)
    bvh = BVHTree.FromPolygons(co.tolist(), tri.reshape(-1, 3).tolist(), all_triangles=True)

    cache = PartCache(warnings)
    placed, copies = [], []
    frame_issues = []
    missed = []
    for i, pt in enumerate(points):
        part = cache.get(pt.get("part") or default_part)
        if part["behind_back_m"] > max(0.05 * part["thickness"], 1e-4):
            frame_issues.append({"part": os.path.basename(part["path"]),
                                 "behind_back_m": round(part["behind_back_m"], 5)})
        name = ml.safe_name(pt.get("name") or "%s_%d" % (part["stem"], i + 1))
        s = pt["scale"]
        cfg = {"normal": normal_mode, "contact": contact, "grid": grid,
               "fill": float(p.get("footprint_fill", 0.8)), "up": up,
               "max_tilt": float(pt.get("max_tilt_deg", p.get("max_tilt_deg", 45.0))),
               "patch": float(pt.get("normal_patch", p.get("normal_patch", 1.0))),
               "rest_pct": float(pt.get("rest_percentile", p.get("rest_percentile", 100.0))),
               "search": float(p.get("search_m") or max(0.1, 3.0 * part["size"] * s))}
        if pt["snap"] == "none":
            R = explicit_rotation(pt)
            origin = Vector(pt["at"])
            n = R @ Vector((0, -1, 0))
            info = {"snap": "none"}
        else:
            res = snap_to_surface(bvh, lo, hi, pt, part, ray, cfg)
            if res is None:
                missed.append(name)
                warnings.append("point %s at %s: the ray missed the target" % (name, pt["at"]))
                continue
            origin, R, info = res
            n = Vector(info["normal"])
        M = Matrix.Translation(origin) @ R.to_4x4() @ Matrix.Scale(s, 4)
        ob = _instance(part, M, name)
        if part_sockets and part["sockets"] and pt.get("sockets", True):
            # the part's own sockets (e.g. socket_mount_hand_l of a bone_kit hand, on its grip axis)
            # go with the copy; a second copy of the same socket name gets a numeric suffix
            recs = []
            for r in part["sockets"]:
                nm, k = r["name"], 2
                while nm in socket_names:
                    nm = "%s_%d" % (r["name"], k)
                    k += 1
                socket_names.add(nm)
                recs.append(dict(r, name=nm, props=dict(r["props"], game_socket=nm)))
            made = ml.restore_sockets(recs, transform=M)
            sockets.extend(made)
            socket_log.append({"point": name, "sockets": [e.name for e in made]})
        pco = ml.mesh_arrays(ob.data)["co"]
        pen = penetration(bvh, pco, part["thickness"] * s)
        rec = {"name": name, "part": part["path"], "at": pt["at"], "snap": pt["snap"],
               "position": [round(float(v), 5) for v in origin],
               "normal": [round(float(v), 5) for v in n],
               "rotation_rows": [[round(float(v), 6) for v in row] for row in R],
               "scale": s, "roll_deg": float(pt.get("roll_deg", 0.0)), "tris": ml.tris_of(ob),
               "size_m": round(part["size"] * s, 5), "thickness_m": round(part["thickness"] * s, 5),
               "penetration": pen,
               # close-up camera: from outside along the normal; explicit poses are seen against the ray
               "view_dir": [round(float(v), 5) for v in (n if pt["snap"] == "surface" else -Vector(ray))]}
        for k, v in info.items():
            rec[k] = [round(float(x), 5) for x in v] if isinstance(v, list) else (
                round(v, 5) if isinstance(v, float) else v)
        placed.append(rec)
        copies.append(ob)
    cache.remove()

    checks.append(af.check("points_hit", not missed, {"placed": len(placed), "missed": missed},
                           len(points), note="every surface point needs a ray hit on the target"))
    checks.append(af.check("part_frame", not frame_issues, frame_issues or 0, 0, severity="warning",
                           note="geometry behind the back face (local +Y > 0): the frame convention is "
                                "origin at the back-face centre, -Y outward"))
    worst = [r["name"] for r in placed
             if r["penetration"]["max_depth_m"] > 0.3 * max(r["thickness_m"], 1e-4)]
    checks.append(af.check("penetration", not worst,
                           {r["name"]: r["penetration"]["max_depth_m"] for r in placed},
                           "0.3 x part thickness", severity="warning",
                           note="part vertices inside the target surface (nearest-normal test)"))

    added_cols = _harmonise(target, copies)
    merged_mats = dedupe_materials()
    if p.get("join", True):
        name = ml.safe_name(p.get("name") or first_name, "placed")
        result = af.join([target] + copies, name=name)
        result.data.name = name
        objs = [result]
    else:
        target.name = ml.safe_name(p.get("name") or first_name, "target")
        target.data.name = target.name
        for ob, rec in zip(copies, placed):   # origin at the back-face centre
            c = Vector(rec["position"])
            ob.data.transform(Matrix.Translation(-c))
            ob.location = c
        objs = [target] + copies
    bpy.context.view_layer.update()
    out_metrics = ml.mesh_metrics(objs)
    checks.append(af.check("no_nan_verts", out_metrics["nan_verts"] == 0, out_metrics["nan_verts"], 0))

    outputs = {"glb": af.export_glb(af.out_path(req, "placed.glb"), objects=objs + sockets,
                                    animations=False, skins=False)}
    if p.get("preview", True) and placed:
        outputs["preview"] = _preview(req, objs, placed, int(p.get("preview_size", 512)))
    ml.purge_orphans()
    outputs["blend"] = af.save_blend(af.out_path(req, "placed.blend"))
    stats = {"input": src, "target_objects_dropped": dropped, "import_helpers_removed": helpers,
             "target": {k: tgt_metrics[k] for k in ("tris", "verts", "loose_parts", "bbox_min", "bbox_max")},
             "result": {k: out_metrics[k] for k in ("objects", "tris", "verts", "materials")},
             "points": placed, "placed": len(placed), "missed": missed, "ray": ray_key,
             "contact": contact, "normal_mode": normal_mode, "join": bool(p.get("join", True)),
             "colour_attributes_completed": added_cols, "materials_merged": merged_mats,
             "sockets": sorted(e.name for e in sockets), "part_sockets": socket_log,
             "materials": sorted({m.name for o in objs for m in o.data.materials if m}),
             "seconds": round(time.time() - t0, 2)}
    return {"outputs": outputs, "stats": stats, "checks": checks, "warnings": warnings}
