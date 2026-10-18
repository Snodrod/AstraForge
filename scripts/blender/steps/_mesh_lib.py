"""Mesh-preparation helpers of the "core" module (cleanup / convert / separate).

Owned by the core module. Other modules may import it read-only; shared changes go through the
integrator (see docs/forge/core.md). Everything here works on plain Blender meshes with numpy
(Blender 5.0.1, bundled Python 3.11, no scipy / Pillow).
"""
import math
import os
import time

import bmesh
import bpy
import mathutils
import numpy as np

import af_common as af


# --------------------------------------------------------------------------- scene prep

def input_path(req):
    """The step's source file: inputs.mesh, else inputs.blend (contract §3 allows both; the
    pipeline passes only {blend} after a step that produced a .blend). Raises a clear error when
    neither is given or the file does not exist."""
    inputs = req.get("inputs") or {}
    key = "mesh" if inputs.get("mesh") else "blend"
    src = inputs.get(key)
    if not src:
        raise ValueError("inputs.mesh or inputs.blend is required")
    src = os.path.abspath(str(src))
    if not os.path.isfile(src):
        raise FileNotFoundError("inputs.%s not found: %s" % (key, src))
    return src


def purge_import_helpers(objects=None):
    """Delete objects that importers add but that are not part of the model: bone custom shapes
    and everything in the glTF importer's 'glTF_not_exported' collection (for a rigged GLB that
    is a 2 m 'Icosphere' bone shape, which otherwise lands in bboxes, joins and exports).

    Call right after af.import_any. Returns (objects without the helpers, removed names)."""
    helpers = helper_shapes()
    keep = [o for o in (objects or []) if o is not None and o not in helpers]
    names = sorted(o.name for o in helpers)
    for o in list(helpers):
        data = o.data
        bpy.data.objects.remove(o, do_unlink=True)
        if isinstance(data, bpy.types.Mesh) and data.users == 0:
            bpy.data.meshes.remove(data)
    coll = bpy.data.collections.get("glTF_not_exported")
    if coll is not None:
        bpy.data.collections.remove(coll)
    if names:
        af.log("removed importer helpers", objects=names)
    return keep, names


def purge_orphans():
    """Drop data blocks nothing uses any more (e.g. the original 4K maps after a remesh + bake)
    so the saved .blend does not carry them. Returns the number of removed blocks."""
    try:
        return int(bpy.data.orphans_purge(do_local_ids=True, do_linked_ids=True,
                                          do_recursive=True) or 0)
    except TypeError:  # pragma: no cover - older signature
        return int(bpy.data.orphans_purge() or 0)


def is_socket(o):
    """Contract §6 socket: an empty named socket_* or carrying the game_socket property."""
    if o is None or o.type != "EMPTY":
        return False
    return o.name.startswith("socket_") or "game_socket" in o.keys()


def _orthonormal(M):
    """4x4 world matrix -> the same position and rotation with unit scale (sockets carry no scale)."""
    loc, rot, _ = M.decompose()
    return mathutils.Matrix.Translation(loc) @ rot.to_matrix().to_4x4()


def socket_records(objects):
    """Socket empties among objects -> plain records (name, unit-scale world matrix, custom
    properties), so a step that flattens the hierarchy can put them back (contract §6: sockets such
    as socket_fx_eye_l/r of replace_part survive cleanup, separate and place_parts)."""
    out = []
    bpy.context.view_layer.update()
    for o in objects or []:
        if not is_socket(o):
            continue
        props = {}
        for k in o.keys():
            if k.startswith("_"):
                continue
            v = o[k]
            try:
                v = v.to_dict() if hasattr(v, "to_dict") else (list(v) if hasattr(v, "__len__") and
                                                               not isinstance(v, str) else v)
            except TypeError:
                pass
            props[k] = v
        name = str(props.get("game_socket") or o.name)
        name = name if name.startswith("socket_") else o.name
        props["game_socket"] = name
        out.append({"name": name, "matrix": _orthonormal(o.matrix_world.copy()),
                    "size": float(o.empty_display_size or 0.05), "props": props})
    return out


def restore_sockets(records, transform=None):
    """Recreate socket empties (unparented, world matrices; optional 4x4 transform applied first,
    scale removed). Returns the new objects in the order of the records."""
    out = []
    for r in records or []:
        props = dict(r.get("props") or {})
        props.pop("game_socket", None)
        kind = props.pop("kind", None)
        e = af.add_socket(r["name"], kind=kind, size=r.get("size", 0.05), **props)
        M = r["matrix"] if transform is None else transform @ r["matrix"]
        e.matrix_world = _orthonormal(M)
        out.append(e)
    bpy.context.view_layer.update()
    return out


def sockets_unit_scale(sockets):
    """Remove the scale a normalisation left on socket empties (position and rotation kept)."""
    bpy.context.view_layer.update()
    for e in sockets or []:
        if e is not None and e.parent is None:
            e.matrix_world = _orthonormal(e.matrix_world.copy())
    bpy.context.view_layer.update()


def prepare_meshes(objects, warnings=None, keep_armatures=False, sockets=None):
    """Flatten the imported hierarchy into world-space mesh objects with applied transforms.

    glTF/FBX imports often hang meshes under scaled empties. We keep each mesh's world matrix,
    unparent it, make its data single-user, delete the empties and apply transforms, so later
    steps see plain meshes in metres. Armatures are dropped unless keep_armatures is set.
    sockets (a list): socket empties (socket_* / game_socket) are recorded into it before the
    empties are deleted (socket_records); the step re-creates them with restore_sockets.
    """
    warnings = warnings if warnings is not None else []
    objs = [o for o in objects if o is not None]
    if sockets is not None:
        sockets.extend(socket_records(objs))
    mesh_objs = [o for o in objs if o.type == "MESH"]
    arms = [o for o in objs if o.type == "ARMATURE"]
    if arms and not keep_armatures:
        warnings.append("input has %d armature(s); this step works on static meshes, the rig and "
                        "skin weights were dropped" % len(arms))
    bpy.context.view_layer.update()
    for o in mesh_objs:
        mw = o.matrix_world.copy()
        if not keep_armatures:
            for m in list(o.modifiers):
                if m.type == "ARMATURE":
                    o.modifiers.remove(m)
            o.parent = None
            o.matrix_world = mw
        if o.data.users > 1:
            o.data = o.data.copy()
    if not keep_armatures:
        for o in objs:
            if o.type != "MESH":
                bpy.data.objects.remove(o, do_unlink=True)
        bpy.context.view_layer.update()
        if mesh_objs:
            af.apply_transforms(mesh_objs)
    return mesh_objs


def rotate_z(objects, deg):
    """Rotate root objects about world Z through matrix_world.

    af_common.normalize(rotate_z_deg=...) edits rotation_euler, which Blender ignores for objects
    in QUATERNION / AXIS_ANGLE mode — and the glTF importer creates exactly those. Steps of this
    module therefore rotate here and call normalize with rotate_z_deg=0."""
    if not deg:
        return
    rot = mathutils.Matrix.Rotation(math.radians(float(deg)), 4, "Z")
    bpy.context.view_layer.update()
    for o in objects:
        if o is not None and o.parent is None:
            o.matrix_world = rot @ o.matrix_world
    bpy.context.view_layer.update()


def helper_shapes():
    """Mesh objects that are not part of the model: bone custom shapes and the glTF importer's
    'glTF_not_exported' collection (its bone-shape Icosphere is 2 m wide and wrecks bboxes)."""
    out = set()
    for a in bpy.data.objects:
        if a.type == "ARMATURE" and a.pose is not None:
            for pb in a.pose.bones:
                if pb.custom_shape is not None:
                    out.add(pb.custom_shape)
    coll = bpy.data.collections.get("glTF_not_exported")
    if coll is not None:
        out.update(coll.all_objects)
    return out


def model_meshes(objs):
    skip = helper_shapes()
    return [o for o in objs if o is not None and o.type == "MESH" and o.users_scene
            and o not in skip]


def tris_of(ob):
    me = ob.data
    if not len(me.polygons):
        return 0
    lt = np.empty(len(me.polygons), dtype=np.int32)
    me.polygons.foreach_get("loop_total", lt)
    return int((lt - 2).sum())


def duplicate(ob, name=None):
    cp = ob.copy()
    cp.data = ob.data.copy()
    if name:
        cp.name = name
        cp.data.name = name
    for coll in ob.users_collection:
        coll.objects.link(cp)
    if not cp.users_collection:
        bpy.context.scene.collection.objects.link(cp)
    return cp


def remove_object(ob):
    me = ob.data if ob.type == "MESH" else None
    bpy.data.objects.remove(ob, do_unlink=True)
    if me is not None and me.users == 0:
        bpy.data.meshes.remove(me)
    # without this, view_layer.objects can still yield the removed base (as None) and the next
    # select_only() fails
    bpy.context.view_layer.update()


def normalize_tracked(objects, frame=None, **kw):
    """af_common.normalize plus the same transform applied to a (lo, hi) frame, so a
    before/after render pair stays aligned when re-centring moves the model (e.g. after small
    islands were removed). Returns (scale, mapped_frame)."""
    lo, hi = af.world_bbox(objects)
    s = af.normalize(objects, **kw)
    if frame is None:
        return s, None
    height = kw.get("height")
    size_z = float(hi.z - lo.z)
    k = float(height) / size_z if height and size_z > 1e-6 else 1.0
    pivot = mathutils.Vector(((lo.x + hi.x) / 2 if kw.get("center", True) else 0.0,
                              (lo.y + hi.y) / 2 if kw.get("center", True) else 0.0,
                              lo.z if kw.get("ground", True) else 0.0))
    return s, ((frame[0] - pivot) * k, (frame[1] - pivot) * k)


# --------------------------------------------------------------------------- arrays & metrics

def mesh_arrays(me):
    """Numpy views of the topology we need (copies, safe after the mesh changes)."""
    nv, ne, nf, nl = len(me.vertices), len(me.edges), len(me.polygons), len(me.loops)
    co = np.empty(nv * 3, dtype=np.float64)
    me.vertices.foreach_get("co", co)
    ev = np.empty(ne * 2, dtype=np.int64)
    me.edges.foreach_get("vertices", ev)
    lv = np.empty(nl, dtype=np.int64)
    me.loops.foreach_get("vertex_index", lv)
    le = np.empty(nl, dtype=np.int64)
    me.loops.foreach_get("edge_index", le)
    ls = np.empty(nf, dtype=np.int64)
    me.polygons.foreach_get("loop_start", ls)
    lt = np.empty(nf, dtype=np.int64)
    me.polygons.foreach_get("loop_total", lt)
    mi = np.zeros(nf, dtype=np.int64)
    if nf:
        me.polygons.foreach_get("material_index", mi)
    return {"co": co.reshape(nv, 3), "ev": ev.reshape(ne, 2), "lv": lv, "le": le, "ls": ls,
            "lt": lt, "mi": mi}


def components(n, edges):
    """Connected components of n vertices joined by edges (k,2). Returns a label per vertex.

    Vectorised hook-and-compress union-find (Shiloach-Vishkin style); converges in a few dozen
    numpy passes even for million-vertex meshes.
    """
    parent = np.arange(n, dtype=np.int64)
    if n == 0 or len(edges) == 0:
        return parent
    a0, b0 = edges[:, 0], edges[:, 1]
    for _ in range(200):
        pa, pb = parent[a0], parent[b0]
        diff = pa != pb
        if not diff.any():
            break
        lo = np.minimum(pa[diff], pb[diff])
        hi = np.maximum(pa[diff], pb[diff])
        np.minimum.at(parent, hi, lo)
        while True:
            pp = parent[parent]
            if np.array_equal(pp, parent):
                break
            parent = pp
    return parent


def island_labels(me, arrays=None):
    a = arrays or mesh_arrays(me)
    labels = components(len(a["co"]), a["ev"])
    uniq, inv, counts = np.unique(labels, return_inverse=True, return_counts=True)
    return inv, counts


def edge_face_counts(a):
    ne = len(a["ev"])
    return np.bincount(a["le"], minlength=ne) if ne else np.zeros(0, dtype=np.int64)


def mesh_metrics(objs):
    """Tris, verts, loose parts, boundary / non-manifold edges, NaN verts, bbox (world)."""
    objs = model_meshes(objs)
    out = {"objects": len(objs), "tris": 0, "verts": 0, "faces": 0, "loose_parts": 0,
           "boundary_edges": 0, "non_manifold_edges": 0, "wire_edges": 0, "nan_verts": 0,
           "materials": 0, "uv_layers": 0}
    mats = set()
    for o in objs:
        me = o.data
        a = mesh_arrays(me)
        out["tris"] += int((a["lt"] - 2).sum()) if len(a["lt"]) else 0
        out["verts"] += len(a["co"])
        out["faces"] += len(a["lt"])
        efc = edge_face_counts(a)
        out["boundary_edges"] += int((efc == 1).sum())
        out["non_manifold_edges"] += int((efc > 2).sum())
        out["wire_edges"] += int((efc == 0).sum())
        out["nan_verts"] += int((~np.isfinite(a["co"])).any(axis=1).sum())
        if len(a["co"]):
            _, counts = island_labels(me, a)
            out["loose_parts"] += len(counts)
        out["uv_layers"] = max(out["uv_layers"], len(me.uv_layers))
        for m in me.materials:
            if m is not None:
                mats.add(m.name)
    out["materials"] = len(mats)
    lo, hi = af.world_bbox(objs)
    out["bbox_min"] = [round(v, 5) for v in lo]
    out["bbox_max"] = [round(v, 5) for v in hi]
    out["size"] = [round(v, 5) for v in (hi - lo)]
    return out


# --------------------------------------------------------------------------- cleanup ops

def clear_custom_normals(ob):
    me = ob.data
    if getattr(me, "has_custom_normals", False):
        try:
            me.normals_split_custom_set([])  # empty list clears in 4.1+
        except Exception:
            pass
        attr = me.attributes.get("custom_normal")
        if attr is not None:
            me.attributes.remove(attr)
    return not getattr(me, "has_custom_normals", False)


def merge_by_distance(ob, dist):
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    n0 = len(bm.verts)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=float(dist))
    removed = n0 - len(bm.verts)
    # welding can leave zero-area faces / zero-length edges behind
    bmesh.ops.dissolve_degenerate(bm, dist=float(dist), edges=bm.edges)
    bm.to_mesh(ob.data)
    bm.free()
    ob.data.update()
    return removed


def delete_loose_geometry(ob):
    """Remove wire edges and isolated vertices (they break bakes and exporters)."""
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    wire = [e for e in bm.edges if not e.link_faces]
    if wire:
        bmesh.ops.delete(bm, geom=wire, context="EDGES")
    lone = [v for v in bm.verts if not v.link_faces]
    if lone:
        bmesh.ops.delete(bm, geom=lone, context="VERTS")
    bm.to_mesh(ob.data)
    bm.free()
    ob.data.update()
    return len(wire), len(lone)


def remove_small_islands(ob, fraction):
    """Delete connected pieces with fewer than fraction * total vertices. Returns (parts, verts)."""
    me = ob.data
    nv = len(me.vertices)
    if fraction <= 0 or nv == 0:
        return 0, 0
    inv, counts = island_labels(me)
    small = counts < fraction * nv
    if not small.any() or small.all():
        return 0, 0
    kill = small[inv]
    bm = bmesh.new()
    bm.from_mesh(me)
    bm.verts.ensure_lookup_table()
    doomed = [bm.verts[i] for i in np.nonzero(kill)[0]]
    bmesh.ops.delete(bm, geom=doomed, context="VERTS")
    bm.to_mesh(me)
    bm.free()
    me.update()
    return int(small.sum()), int(kill.sum())


def recalc_normals(ob):
    """Make face winding consistent and outward. Returns how many faces were flipped."""
    me = ob.data
    before = np.empty(len(me.polygons) * 3)
    me.polygons.foreach_get("normal", before)
    bm = bmesh.new()
    bm.from_mesh(me)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    bm.to_mesh(me)
    bm.free()
    me.update()
    after = np.empty(len(me.polygons) * 3)
    me.polygons.foreach_get("normal", after)
    if not len(before):
        return 0
    dots = (before.reshape(-1, 3) * after.reshape(-1, 3)).sum(axis=1)
    return int((dots < 0).sum())


def smooth_by_angle(ob, angle_deg):
    me = ob.data
    if hasattr(me, "set_sharp_from_angle"):
        me.set_sharp_from_angle(angle=math.radians(float(angle_deg)))
    else:  # pragma: no cover - older Blender
        af.select_only([ob])
        bpy.ops.object.shade_smooth_by_angle(angle=math.radians(float(angle_deg)))
    me.update()


def shade_smooth(ob):
    """Every face smooth, no sharp edges (what a baked tangent-space normal map expects)."""
    me = ob.data
    if hasattr(me, "shade_smooth"):
        me.shade_smooth()
    else:  # pragma: no cover - older Blender
        for poly in me.polygons:
            poly.use_smooth = True
    attr = me.attributes.get("sharp_edge")
    if attr is not None:
        me.attributes.remove(attr)
    me.update()


def apply_shading(ob, smooth_deg):
    """shade_auto_smooth_deg semantics: None = leave, <= 0 = flat, > 0 = smooth by angle."""
    if smooth_deg is None:
        return
    if float(smooth_deg) <= 0:
        for poly in ob.data.polygons:
            poly.use_smooth = False
        ob.data.update()
    else:
        smooth_by_angle(ob, float(smooth_deg))


def sharp_edge_count(ob):
    attr = ob.data.attributes.get("sharp_edge")
    if attr is None or attr.domain != "EDGE":
        return 0
    vals = np.zeros(len(ob.data.edges), dtype=bool)
    attr.data.foreach_get("value", vals)
    return int(vals.sum())


def allocate_budget(counts, target, floor=64):
    """Split a triangle budget over several objects (join:false) in proportion to their current
    triangle counts. Each object keeps at least min(count, floor) triangles, so a low-poly piece
    (a 12-tri box, a 40-tri gem) is left whole instead of collapsing into a tetrahedron; the rest
    of the budget is re-split over the others. A budget equal to the count means "leave alone".
    If even the floors do not fit, falls back to a plain proportional split."""
    counts = [max(0, int(c)) for c in counts]
    target = int(target)
    if sum(counts) <= target:
        return counts[:]
    mins = [min(c, int(floor)) for c in counts]
    if sum(mins) >= target:
        tot = float(sum(counts)) or 1.0
        return [min(c, max(4, int(target * c / tot))) for c in counts]
    alloc = [None] * len(counts)
    free = list(range(len(counts)))
    budget = target
    while free:
        tot = float(sum(counts[i] for i in free)) or 1.0
        pinned = [i for i in free if budget * counts[i] / tot < mins[i]]
        if not pinned:
            break
        for i in pinned:
            alloc[i] = mins[i]
            budget -= mins[i]
            free.remove(i)
    tot = float(sum(counts[i] for i in free)) or 1.0
    for i in free:
        alloc[i] = min(counts[i], int(budget * counts[i] / tot))
    return alloc


def protect_seams_group(ob, name="af_protect", seam_weight=0.0):
    """Vertex group for the collapse decimator: weight 0 on UV seams, material borders and open
    boundaries, 1 elsewhere (Blender adds edge cost for low weights, so seams collapse last)."""
    me = ob.data
    a = mesh_arrays(me)
    nl, ne = len(a["lv"]), len(a["ev"])
    protected = np.zeros(len(a["co"]), dtype=bool)
    if nl == 0:
        return None, 0
    loop_face = np.repeat(np.arange(len(a["ls"])), a["lt"])
    nxt = np.arange(nl) + 1
    ends = a["ls"] + a["lt"] - 1
    nxt[ends] = a["ls"]
    efc = edge_face_counts(a)
    bad = efc != 2
    if me.uv_layers.active is not None:
        uv = np.empty(nl * 2, dtype=np.float64)
        me.uv_layers.active.data.foreach_get("uv", uv)
        uv = uv.reshape(nl, 2)
        swap = a["lv"] > a["lv"][nxt]
        uv_lo = np.where(swap[:, None], uv[nxt], uv)
        uv_hi = np.where(swap[:, None], uv, uv[nxt])
    else:
        uv_lo = uv_hi = None
    order = np.argsort(a["le"], kind="stable")
    starts = np.zeros(ne + 1, dtype=np.int64)
    starts[1:] = np.cumsum(efc)
    two = np.nonzero(efc == 2)[0]
    l1 = order[starts[two]]
    l2 = order[starts[two] + 1]
    seam = np.zeros(ne, dtype=bool)
    if uv_lo is not None:
        d = (np.abs(uv_lo[l1] - uv_lo[l2]).max(axis=1) > 1e-5) | \
            (np.abs(uv_hi[l1] - uv_hi[l2]).max(axis=1) > 1e-5)
        seam[two] |= d
    seam[two] |= a["mi"][loop_face[l1]] != a["mi"][loop_face[l2]]
    flag = seam | bad
    protected[a["ev"][flag].ravel()] = True
    grp = ob.vertex_groups.get(name) or ob.vertex_groups.new(name=name)
    free = np.nonzero(~protected)[0].tolist()
    keep = np.nonzero(protected)[0].tolist()
    if free:
        grp.add(free, 1.0, "REPLACE")
    if keep:
        grp.add(keep, float(seam_weight), "REPLACE")
    return grp, int(protected.sum())


def _apply_modifier(ob, mod):
    with bpy.context.temp_override(object=ob, active_object=ob, selected_objects=[ob],
                                   selected_editable_objects=[ob]):
        bpy.ops.object.modifier_apply(modifier=mod.name)


def planar_dissolve(ob, angle_deg):
    mod = ob.modifiers.new("af_planar", "DECIMATE")
    mod.decimate_type = "DISSOLVE"
    mod.angle_limit = math.radians(float(angle_deg))
    mod.delimit = {"SEAM", "MATERIAL", "UV", "SHARP"}
    mod.use_dissolve_boundaries = False
    _apply_modifier(ob, mod)
    # back to triangles so budgets stay comparable and exporters see what we counted
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    ngons = [f for f in bm.faces if len(f.verts) > 3]
    if ngons:
        bmesh.ops.triangulate(bm, faces=ngons, quad_method="BEAUTY", ngon_method="BEAUTY")
    bm.to_mesh(ob.data)
    bm.free()
    ob.data.update()


def decimate_to(ob, target, symmetry=None, planar_deg=0.0, seam_weight=None, protect_factor=1.0,
                stage_ratio=0.3, max_stages=10):
    """Planar pass (optional) + staged collapse decimation down to <= target triangles.

    Each stage keeps at least stage_ratio of the current triangles, so million-triangle inputs go
    through a few passes with fresh quadrics instead of one huge heap. Returns a stage log.

    UVs, seams and material borders: Blender's collapse decimator interpolates loop UVs and keeps
    face materials itself. seam_weight (0..1) additionally down-weights seam / border vertices
    through a vertex group; measured on a robed test character it made the textured result WORSE
    (render colour MAE 0.025 -> 0.035..0.038 at 30k tris), so it is off by default.
    """
    target = int(target)
    log = []
    t = tris_of(ob)
    if planar_deg and planar_deg > 0 and t > target:
        t0 = time.time()
        planar_dissolve(ob, planar_deg)
        t2 = tris_of(ob)
        log.append({"stage": "planar", "tris_in": t, "tris_out": t2,
                    "seconds": round(time.time() - t0, 2)})
        t = t2
    stage = 0
    while t > target and stage < max_stages:
        stage += 1
        t0 = time.time()
        want = target / float(t)
        ratio = want * 0.998 if want >= stage_ratio else stage_ratio
        protected = 0
        mod = ob.modifiers.new("af_collapse", "DECIMATE")
        mod.decimate_type = "COLLAPSE"
        mod.ratio = max(1e-4, min(1.0, ratio))
        mod.use_collapse_triangulate = True
        if symmetry:
            mod.use_symmetry = True
            mod.symmetry_axis = str(symmetry).upper()
        if seam_weight is not None:
            grp, protected = protect_seams_group(ob, seam_weight=seam_weight)
            if grp is not None and protected:
                mod.vertex_group = grp.name
                mod.vertex_group_factor = float(protect_factor)
        _apply_modifier(ob, mod)
        grp = ob.vertex_groups.get("af_protect")
        if grp is not None:
            ob.vertex_groups.remove(grp)
        t2 = tris_of(ob)
        log.append({"stage": "collapse_%d" % stage, "ratio": round(mod_ratio(ratio), 5),
                    "tris_in": t, "tris_out": t2, "protected_verts": protected,
                    "seconds": round(time.time() - t0, 2)})
        if t2 >= t:
            break
        t = t2
    return log


def mod_ratio(r):
    return max(1e-4, min(1.0, r))


def mirror_pair_fraction(ob, axis="X", tol=None):
    """Share of vertices that have a mirror twin across axis=0 (how symmetric the mesh is)."""
    co = mesh_arrays(ob.data)["co"]
    if not len(co):
        return 0.0
    k = "XYZ".index(axis.upper())
    size = co.max(axis=0) - co.min(axis=0)
    tol = tol or max(float(size.max()) * 1e-3, 1e-6)
    q = np.round(co / tol).astype(np.int64)
    m = co.copy()
    m[:, k] = -m[:, k]
    qm = np.round(m / tol).astype(np.int64)
    keys = set(map(tuple, q.tolist()))
    hits = sum(1 for t in map(tuple, qm.tolist()) if t in keys)
    return hits / float(len(co))


def voxel_remesh(ob, voxel_size, adaptivity=0.0):
    mod = ob.modifiers.new("af_voxel", "REMESH")
    mod.mode = "VOXEL"
    mod.voxel_size = float(voxel_size)
    mod.adaptivity = float(adaptivity)
    mod.use_smooth_shade = True
    _apply_modifier(ob, mod)
    # voxel output is quads; triangulate later through the decimator / exporter
    return tris_of(ob)


def smart_uv(ob, angle_deg=66.0, margin=0.004):
    me = ob.data
    while me.uv_layers:
        me.uv_layers.remove(me.uv_layers[0])
    me.uv_layers.new(name="UVMap")
    af.select_only([ob])
    with bpy.context.temp_override(object=ob, active_object=ob, selected_objects=[ob],
                                   selected_editable_objects=[ob]):
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.smart_project(angle_limit=math.radians(angle_deg), island_margin=margin,
                                 area_weight=0.0, correct_aspect=True, scale_to_bounds=False)
        bpy.ops.object.mode_set(mode="OBJECT")
    return me.uv_layers[0].name


# --------------------------------------------------------------------------- materials

def principled_of(mat):
    if mat is None or mat.node_tree is None:
        return None
    for n in mat.node_tree.nodes:
        if n.type == "BSDF_PRINCIPLED":
            return n
    return None


def image_into(mat, socket_name):
    """Image texture node that feeds the Principled input socket_name (through simple chains)."""
    bsdf = principled_of(mat)
    if bsdf is None or socket_name not in bsdf.inputs:
        return None
    stack = [l.from_node for l in bsdf.inputs[socket_name].links]
    seen = set()
    while stack:
        n = stack.pop()
        if n in seen:
            continue
        seen.add(n)
        if n.type == "TEX_IMAGE" and n.image is not None:
            return n
        for inp in n.inputs:
            stack.extend(l.from_node for l in inp.links)
    return None


def texture_report(objs):
    """{material: {base_color: [w,h], normal: [...], ...}} for the given mesh objects."""
    rep = {}
    for o in model_meshes(objs):
        for m in o.data.materials:
            if m is None or m.name in rep:
                continue
            item = {}
            for key, sock in (("base_color", "Base Color"), ("normal", "Normal"),
                              ("roughness", "Roughness"), ("metallic", "Metallic"),
                              ("emission", "Emission Color")):
                n = image_into(m, sock)
                if n is not None:
                    # [0, 0] = node present but the pixels are missing (unresolved file)
                    item[key] = [int(n.image.size[0]), int(n.image.size[1])]
            rep[m.name] = item
    return rep


def set_active_basecolor_nodes(objs):
    """Workbench TEXTURE mode shows the active image node: point it at the base colour map."""
    for o in af.meshes(objs):
        for m in o.data.materials:
            n = image_into(m, "Base Color")
            if n is not None:
                m.node_tree.nodes.active = n


def _base_color_key(mat):
    """Hashable description of a material's base colour (linked inputs are 'linked')."""
    bsdf = principled_of(mat)
    if bsdf is None or "Base Color" not in bsdf.inputs:
        return ("viewport",) + tuple(round(float(x), 4) for x in mat.diffuse_color)
    sock = bsdf.inputs["Base Color"]
    if sock.is_linked:
        return ("linked", mat.name)
    return ("value",) + tuple(round(float(x), 4) for x in sock.default_value)


def color_source(ob):
    """Why a remeshed copy of ob needs a colour bake, or None when nothing would be lost.

    'texture'   a Base Color image (the usual AI-mesh case)
    'linked'    Base Color fed by another node: colour attribute, ramp, mix... (the Remesh
                modifier drops colour attributes, so vertex colours vanish without a bake)
    'materials' faces use more than one distinct base colour (voxel remesh keeps one slot)
    'attribute' no material at all but colour attributes (STL/PLY/OBJ-style vertex colours)
    """
    me = ob.data
    mats = [m for m in me.materials if m is not None]
    for m in mats:
        if image_into(m, "Base Color") is not None:
            return "texture"
    for m in mats:
        bsdf = principled_of(m)
        if bsdf is not None and "Base Color" in bsdf.inputs and bsdf.inputs["Base Color"].is_linked:
            return "linked"
    if len(mats) > 1 and len(me.polygons):
        mi = np.zeros(len(me.polygons), dtype=np.int64)
        me.polygons.foreach_get("material_index", mi)
        keys = set()
        for i in np.unique(mi).tolist():
            if i < len(me.materials) and me.materials[i] is not None:
                keys.add(_base_color_key(me.materials[i]))
        if len(keys) > 1:
            return "materials"
    if not mats and len(getattr(me, "color_attributes", [])):
        return "attribute"
    return None


def attribute_color_material(ob, name="af_vertex_color"):
    """Give a material-less mesh a material that shows its active colour attribute, so a
    Cycles bake can read the colours. Returns the material."""
    me = ob.data
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    nt = mat.node_tree
    node = nt.nodes.new("ShaderNodeVertexColor")
    attrs = me.color_attributes
    active = attrs.active_color if attrs.active_color is not None else attrs[0]
    node.layer_name = active.name
    nt.links.new(node.outputs["Color"], principled_of(mat).inputs["Base Color"])
    me.materials.clear()
    me.materials.append(mat)
    return mat


def preview_color_type(objs):
    """Workbench colour mode that shows what the objects really look like: TEXTURE for a base
    colour map, VERTEX for colour attributes the materials use (or meshes without materials),
    MATERIAL otherwise (flat colours, synced from the Principled node by render_sheet)."""
    ms = af.meshes(objs)
    for o in ms:
        if any(image_into(m, "Base Color") is not None for m in o.data.materials if m is not None):
            return "TEXTURE"
    for o in ms:
        me = o.data
        if not len(getattr(me, "color_attributes", [])):
            continue
        mats = [m for m in me.materials if m is not None]
        if not mats:
            return "VERTEX"
        for m in mats:
            if m.node_tree is not None and any(n.type == "VERTEX_COLOR" or n.bl_idname in
                                               ("ShaderNodeVertexColor", "ShaderNodeAttribute")
                                               for n in m.node_tree.nodes):
                return "VERTEX"
    return "MATERIAL"


def _sync_viewport_colors(objs):
    """Workbench MATERIAL/TEXTURE modes draw material.diffuse_color; glTF imports leave it at
    the 0.8 grey default. Copy the unlinked Principled base colour there for the render and
    return what to restore."""
    saved = []
    seen = set()
    for o in af.meshes(objs):
        for m in o.data.materials:
            if m is None or m.name in seen:
                continue
            seen.add(m.name)
            bsdf = principled_of(m)
            if bsdf is None or "Base Color" not in bsdf.inputs or bsdf.inputs["Base Color"].is_linked:
                continue
            saved.append((m, tuple(m.diffuse_color)))
            m.diffuse_color = tuple(bsdf.inputs["Base Color"].default_value)
    return saved


def harmonise_attributes(target, copies):
    """Same UV layer name and the same colour attributes (white where missing) on all meshes, so a
    join keeps the part UVs and glTF does not tint the target with an absent COLOR_0."""
    uv_name = target.data.uv_layers.active.name if target.data.uv_layers.active else None
    allm = [target] + copies
    for ob in copies:
        if uv_name:
            if ob.data.uv_layers:
                ob.data.uv_layers.active.name = uv_name
            else:
                ob.data.uv_layers.new(name=uv_name)
    kinds = {}
    for ob in allm:
        for a in ob.data.color_attributes:
            kinds.setdefault(a.name, (a.domain, a.data_type))
    added = []
    for ob in allm:
        for name, (dom, typ) in kinds.items():
            if name in ob.data.color_attributes:
                continue
            a = ob.data.color_attributes.new(name, typ, dom)
            n = len(ob.data.vertices) if dom == "POINT" else len(ob.data.loops)
            a.data.foreach_set("color", [1.0] * (4 * n))
            added.append("%s:%s" % (ob.name, name))
    return added


def safe_name(name, fallback="part"):
    out = "".join(c if (c.isascii() and (c.isalnum() or c in "_-")) else "_" for c in str(name))
    out = out.strip("_")
    return out or fallback


# --------------------------------------------------------------------------- baking

def setup_cycles(device="CPU", samples=4):
    scene = bpy.context.scene
    scene.render.engine = "CYCLES"
    used = "CPU"
    if str(device).upper() == "GPU":
        try:
            prefs = bpy.context.preferences.addons["cycles"].preferences
            for kind in ("OPTIX", "CUDA", "HIP", "ONEAPI", "METAL"):
                try:
                    prefs.compute_device_type = kind
                except TypeError:
                    continue
                prefs.get_devices()
                devs = [d for d in prefs.devices if d.type == kind]
                if devs:
                    for d in prefs.devices:
                        d.use = d.type == kind
                    used = kind
                    break
        except Exception:
            used = "CPU"
    scene.cycles.device = "GPU" if used != "CPU" else "CPU"
    scene.cycles.samples = int(samples)
    scene.cycles.use_denoising = False
    try:
        scene.cycles.use_adaptive_sampling = False
    except Exception:
        pass
    return used


def bake_maps(src_objs, dst, size=2048, device="CPU", maps=("base_color",), extrusion=0.01,
              ray_distance=0.03, samples=4, margin=None, out_dir=None, prefix="baked"):
    """Cycles selected-to-active bake from src_objs onto dst (needs UVs). Returns {map: image}.

    The bake target material on dst is rebuilt: Principled + baked base colour (and optional
    tangent-space normal / emission). Images are saved as PNG into out_dir when given.
    """
    used = setup_cycles(device, samples)
    scene = bpy.context.scene
    bk = scene.render.bake
    bk.use_selected_to_active = True
    bk.cage_extrusion = float(extrusion)
    bk.max_ray_distance = float(ray_distance)
    bk.margin = int(margin if margin is not None else max(4, size // 128))
    bk.margin_type = "EXTEND"
    bk.use_clear = True
    bk.target = "IMAGE_TEXTURES"
    mat = bpy.data.materials.new(prefix + "_" + safe_name(dst.name, "mesh"))
    mat.use_nodes = True
    nt = mat.node_tree
    bsdf = principled_of(mat)
    out_imgs = {}
    dst.data.materials.clear()
    dst.data.materials.append(mat)
    specs = {"base_color": ("DIFFUSE", {"COLOR"}, "sRGB", (0, 0, 0, 1)),
             "normal": ("NORMAL", None, "Non-Color", (0.5, 0.5, 1, 1)),
             "emission": ("EMIT", None, "sRGB", (0, 0, 0, 1))}
    timings = {}
    for key in maps:
        btype, pfilter, cs, fill = specs[key]
        img = bpy.data.images.new("%s_%s" % (prefix, key), width=int(size), height=int(size),
                                  alpha=False, float_buffer=False)
        img.colorspace_settings.name = cs
        img.generated_color = fill
        node = nt.nodes.new("ShaderNodeTexImage")
        node.image = img
        node.name = "bake_" + key
        nt.nodes.active = node
        for o in bpy.context.view_layer.objects:
            o.select_set(False)
        for o in src_objs:
            o.hide_render = False
            o.select_set(True)
        dst.select_set(True)
        bpy.context.view_layer.objects.active = dst
        t0 = time.time()
        kw = dict(type=btype, use_selected_to_active=True, cage_extrusion=float(extrusion),
                  max_ray_distance=float(ray_distance), margin=bk.margin, target="IMAGE_TEXTURES",
                  use_clear=True)
        if pfilter:
            kw["pass_filter"] = pfilter
        if btype == "NORMAL":
            kw["normal_space"] = "TANGENT"
        bpy.ops.object.bake(**kw)
        timings[key] = round(time.time() - t0, 2)
        if out_dir:
            path = os.path.join(out_dir, "%s_%s.png" % (prefix, key))
            img.filepath_raw = path
            img.file_format = "PNG"
            img.save()
        img.pack()
        out_imgs[key] = img
        if key == "base_color":
            nt.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])
        elif key == "normal":
            nm = nt.nodes.new("ShaderNodeNormalMap")
            nt.links.new(node.outputs["Color"], nm.inputs["Color"])
            nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
        elif key == "emission":
            nt.links.new(node.outputs["Color"], bsdf.inputs["Emission Color"])
            bsdf.inputs["Emission Strength"].default_value = 1.0
    if "base_color" in out_imgs:
        nt.nodes.active = nt.nodes["bake_base_color"]
    bsdf.inputs["Roughness"].default_value = 0.8
    return out_imgs, {"device": used, "seconds": timings, "margin_px": bk.margin}


def image_coverage(img, uv_obj=None):
    """Fraction of pure-black texels inside UV islands (bake misses) — rough estimate.

    Rasterising islands is expensive in Python, so we sample the image at every loop UV and at
    triangle centroids of uv_obj; a miss leaves the cleared colour (black)."""
    w, h = img.size
    px = np.array(img.pixels[:], dtype=np.float32).reshape(h, w, 4)
    if uv_obj is None:
        return None
    me = uv_obj.data
    me.calc_loop_triangles()
    nl = len(me.loops)
    uv = np.empty(nl * 2)
    me.uv_layers.active.data.foreach_get("uv", uv)
    uv = uv.reshape(nl, 2)
    tri = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("loops", tri)
    cen = uv[tri.reshape(-1, 3)].mean(axis=1)
    xs = np.clip((cen[:, 0] * w).astype(int), 0, w - 1)
    ys = np.clip((cen[:, 1] * h).astype(int), 0, h - 1)
    col = px[ys, xs, :3]
    black = (col.max(axis=1) < 1e-3)
    return float(black.mean()) if len(black) else 0.0


# --------------------------------------------------------------------------- previews

def render_sheet(path, objects=None, views=("front", "three_quarter"), size=512,
                 color_type="AUTO", margin=1.08, bbox=None, cavity=True):
    """Workbench orthographic sheet like af_common.render_views, plus a colour mode switch
    (AUTO picks TEXTURE / VERTEX / MATERIAL from what the objects use, see preview_color_type;
    RANDOM colours objects) and an optional fixed bbox so two renders share the same framing
    (before/after comparisons). Returns (path, tiles)."""
    scene = bpy.context.scene
    bpy.context.view_layer.update()
    if color_type == "AUTO":
        color_type = preview_color_type(objects if objects is not None else scene.objects)
    if color_type == "TEXTURE":
        set_active_basecolor_nodes(objects)
    synced = _sync_viewport_colors(objects if objects is not None else scene.objects) \
        if color_type in ("TEXTURE", "MATERIAL") else []
    lo, hi = bbox if bbox is not None else af.world_bbox(objects)
    centre = (lo + hi) / 2
    extent = max((hi - lo).length, 1e-3)
    cam_data = bpy.data.cameras.new("af_sheet_cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = extent * margin
    cam_data.clip_end = extent * 20
    cam = bpy.data.objects.new("af_sheet_cam", cam_data)
    scene.collection.objects.link(cam)
    prev_cam = scene.camera
    scene.camera = cam
    hidden = []
    if objects is not None:
        keep = set(objects)
        for o in scene.objects:
            if o.type == "MESH" and o not in keep and not o.hide_render:
                o.hide_render = True
                hidden.append(o)
    r = scene.render
    prev_engine = r.engine
    r.engine = af.engine_id("WORKBENCH")
    r.resolution_x = r.resolution_y = int(size)
    r.resolution_percentage = 100
    r.film_transparent = True
    r.image_settings.file_format = "PNG"
    r.image_settings.color_mode = "RGBA"
    sh = scene.display.shading
    sh.light = "STUDIO"
    sh.color_type = color_type
    sh.show_cavity = bool(cavity)
    sh.show_object_outline = False
    tmp_dir = os.path.join(os.path.dirname(os.path.abspath(path)), "_views")
    os.makedirs(tmp_dir, exist_ok=True)
    tiles = []
    stem = os.path.splitext(os.path.basename(path))[0]
    try:
        for name in views:
            direction, _up = af.VIEWS[name]
            cam.location = centre + direction * extent * 3
            if name == "top":
                cam.rotation_euler = (0.0, 0.0, 0.0)
            else:
                look = (centre - cam.location).normalized()
                cam.rotation_euler = look.to_track_quat("-Z", "Y").to_euler()
            one = os.path.join(tmp_dir, "%s_%s.png" % (stem, name))
            r.filepath = one
            bpy.ops.render.render(write_still=True)
            tiles.append(load_png(one))
            try:
                os.remove(one)
            except OSError:
                pass
    finally:
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass
        for o in hidden:
            o.hide_render = False
        for m, col in synced:
            m.diffuse_color = col
        scene.camera = prev_cam
        bpy.data.objects.remove(cam)
        bpy.data.cameras.remove(cam_data)
        r.engine = prev_engine
    save_png(path, np.concatenate(tiles, axis=1))
    return os.path.abspath(path), tiles


def load_png(path):
    img = bpy.data.images.load(path, check_existing=False)
    try:
        px = np.array(img.pixels[:], dtype=np.float32).reshape(img.size[1], img.size[0], 4)
    finally:
        bpy.data.images.remove(img)
    return px


def save_png(path, px):
    """px: (h, w, 4) float array, bottom row first (Blender order)."""
    h, w = px.shape[:2]
    out = bpy.data.images.new("af_png", width=w, height=h, alpha=True)
    out.pixels = np.ascontiguousarray(px, dtype=np.float32).ravel()
    out.filepath_raw = os.path.abspath(path)
    out.file_format = "PNG"
    out.save()
    bpy.data.images.remove(out)
    return os.path.abspath(path)


def flatten(px, bg=(0.82, 0.82, 0.84)):
    """Composite a straight-alpha RGBA array over an opaque background colour."""
    a = px[..., 3:4]
    out = np.empty_like(px)
    out[..., :3] = px[..., :3] * a + np.asarray(bg, dtype=np.float32)[None, None, :3] * (1 - a)
    out[..., 3] = 1.0
    return out


def side_by_side(path, sheets, gap=12, bg=(0.82, 0.82, 0.84)):
    """Concatenate sheets (h,w,4 arrays) left to right on an opaque background with a divider.
    bg=None keeps transparency."""
    h = max(s.shape[0] for s in sheets)
    parts = []
    for i, s in enumerate(sheets):
        if s.shape[0] < h:
            pad = np.zeros((h - s.shape[0], s.shape[1], 4), dtype=np.float32)
            s = np.concatenate([s, pad], axis=0)
        parts.append(flatten(s, bg) if bg is not None else s)
        if i < len(sheets) - 1:
            g = np.ones((h, gap, 4), dtype=np.float32)
            if bg is not None:
                g[..., :3] = bg
            else:
                g[..., 3] = 0.0
            g[:, gap // 2 - 1:gap // 2 + 1] = (0.25, 0.25, 0.25, 1.0)
            parts.append(g)
    return save_png(path, np.concatenate(parts, axis=1))


def downscale(px, factor):
    """Box-filter downscale of an (h, w, c) array by an integer factor."""
    f = int(factor)
    if f <= 1:
        return px
    h, w = (px.shape[0] // f) * f, (px.shape[1] // f) * f
    return px[:h, :w].reshape(h // f, f, w // f, f, -1).mean(axis=(1, 3))


def render_compare(tiles_a, tiles_b):
    """Image-space comparison of two renders with identical framing.

    silhouette_iou: alpha-mask IoU; color_mae: mean |RGB| difference (0..1) over pixels that are
    covered in both renders. Cheap stand-in for a texture/shape fidelity metric."""
    ious, maes = [], []
    for a, b in zip(tiles_a, tiles_b):
        ma, mb = a[..., 3] > 0.5, b[..., 3] > 0.5
        union = (ma | mb).sum()
        inter = (ma & mb)
        ious.append(float(inter.sum()) / float(union) if union else 1.0)
        if inter.any():
            maes.append(float(np.abs(a[..., :3][inter] - b[..., :3][inter]).mean()))
    return {"silhouette_iou": round(min(ious), 4) if ious else None,
            "color_mae": round(max(maes), 4) if maes else None}


# --------------------------------------------------------------------------- box cutting

def _face_bounds(f):
    xs = [v.co.x for v in f.verts]
    ys = [v.co.y for v in f.verts]
    zs = [v.co.z for v in f.verts]
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def _overlaps(fb, lo, hi):
    (a0, a1, a2), (b0, b1, b2) = fb
    return not (b0 < lo[0] or a0 > hi[0] or b1 < lo[1] or a1 > hi[1] or b2 < lo[2] or a2 > hi[2])


def _inside_any(c, boxes, eps):
    for lo, hi in boxes:
        if all(lo[k] - eps <= c[k] <= hi[k] + eps for k in range(3)):
            return True
    return False


def bisect_boxes(bm, boxes, eps):
    """Cut bm along the six planes of every box, only where faces touch that box."""
    cuts = 0
    for lo, hi in boxes:
        elo = [lo[k] - eps for k in range(3)]
        ehi = [hi[k] + eps for k in range(3)]
        cand = [f for f in bm.faces if _overlaps(_face_bounds(f), elo, ehi)]
        for axis in range(3):
            for value in (lo[axis], hi[axis]):
                faces = [f for f in cand if f.is_valid]
                strad = []
                for f in faces:
                    vals = [v.co[axis] for v in f.verts]
                    if min(vals) < value - eps and max(vals) > value + eps:
                        strad.append(f)
                if not strad:
                    continue
                edges = {e for f in strad for e in f.edges}
                verts = {v for f in strad for v in f.verts}
                no = mathutils.Vector((0, 0, 0))
                no[axis] = 1.0
                co = mathutils.Vector((0, 0, 0))
                co[axis] = value
                ret = bmesh.ops.bisect_plane(bm, geom=list(verts) + list(edges) + strad, dist=eps,
                                             plane_co=co, plane_no=no)
                cuts += 1
                new_faces = {g for g in ret["geom"] if isinstance(g, bmesh.types.BMFace)}
                cand = list({f for f in cand if f.is_valid} | new_faces)
    return cuts


def _fill_seams(bm, open_layer, boxes, eps, part_side, uv_layer):
    """Close boundary loops that the cut opened (edges not flagged as originally open).

    Returns (cap_faces, open_seam_edges). Caps get the UV of a neighbouring loop at the same
    vertex (the cut surface shows the colour of the cut rim), the neighbour's material, flat
    shading, and face away from the part (part_side=True) or into the box (rest side)."""
    seams = [e for e in bm.edges if e.is_boundary and not e[open_layer]]
    if not seams:
        return 0, 0
    res = bmesh.ops.holes_fill(bm, edges=seams, sides=0)
    caps = list(res.get("faces", []))
    if caps:
        tri = bmesh.ops.triangulate(bm, faces=caps, quad_method="BEAUTY", ngon_method="BEAUTY")
        caps = [f for f in tri.get("faces", caps) if f.is_valid]
    capset = set(caps)
    flip = []
    for f in caps:
        f.smooth = False
        neighbours = [l.face for e in f.edges for l in e.link_loops if l.face not in capset]
        if neighbours:
            f.material_index = neighbours[0].material_index
        if uv_layer is not None:
            for l in f.loops:
                src = [x for x in l.vert.link_loops if x.face not in capset]
                if src:
                    l[uv_layer].uv = src[0][uv_layer].uv
        c = f.calc_center_median()
        n = f.normal
        for lo, hi in boxes:
            hit = False
            for axis in range(3):
                for value, sign in ((lo[axis], -1.0), (hi[axis], 1.0)):
                    if abs(c[axis] - value) <= 10 * eps and abs(n[axis]) > 0.5:
                        want = sign if part_side else -sign
                        if n[axis] * want < 0:
                            flip.append(f)
                        hit = True
                        break
                if hit:
                    break
            if hit:
                break
    if flip:
        bmesh.ops.reverse_faces(bm, faces=flip)
    left = sum(1 for e in bm.edges if e.is_valid and e.is_boundary and not e[open_layer])
    return len(caps), left


def _open_seam_count(bm, open_layer):
    return sum(1 for e in bm.edges if e.is_valid and e.is_boundary and not e[open_layer])


def cut_by_boxes(ob, boxes, part_name, eps=None, caps=True, facing=None, facing_min=0.0,
                 make_part=True):
    """Cut the union of axis-aligned boxes out of ob (world == local, transforms applied).

    Returns (part_object_or_None, info). ob keeps everything outside the boxes.
    caps=True: both sides get their cut openings capped, so a fused prop comes out as its own
    closed piece. caps=False: the cut openings stay open on both sides (an open hem, a removed
    cap) - nothing re-joins the two sides.
    facing=[x, y, z] (with facing_min): only faces inside a box whose normal . facing >= facing_min
    count as inside (e.g. the down-facing bottom of a robe: facing [0, 0, -1], facing_min 0.5).
    make_part=False: the inside faces are deleted (no part object is made; info still counts them)."""
    me = ob.data
    lo, hi = af.world_bbox([ob])
    eps = eps or max((hi - lo).length * 1e-6, 1e-7)
    bm = bmesh.new()
    bm.from_mesh(me)
    open_layer = bm.edges.layers.int.new("af_open")
    for e in bm.edges:
        e[open_layer] = 1 if (e.is_boundary or len(e.link_faces) > 2) else 0
    t0 = time.time()
    cuts = bisect_boxes(bm, boxes, eps)
    inside = [f for f in bm.faces if _inside_any(f.calc_center_median(), boxes, eps)]
    if facing is not None:
        fd = mathutils.Vector([float(v) for v in facing])
        if fd.length < 1e-9:
            raise ValueError("facing must be a non-zero vector")
        fd.normalize()
        bm.normal_update()
        inside = [f for f in inside if f.normal.dot(fd) >= float(facing_min)]
    info = {"planes_cut": cuts, "faces_inside": len(inside), "cut_seconds": round(time.time() - t0, 2),
            "caps": bool(caps)}
    if not inside:
        bm.free()
        return None, info
    bm.faces.index_update()
    inside_idx = {f.index for f in inside}
    part_bm = bm.copy() if make_part else None  # same face order as bm
    info["area_removed_m2"] = round(sum(f.calc_area() for f in inside), 5)
    bmesh.ops.delete(bm, geom=inside, context="FACES")
    if part_bm is not None:
        part_bm.faces.ensure_lookup_table()
        outside = [f for i, f in enumerate(part_bm.faces) if i not in inside_idx]
        bmesh.ops.delete(part_bm, geom=outside, context="FACES")
    uv_rest = bm.loops.layers.uv.active
    rest_lay = bm.edges.layers.int.get("af_open")
    if caps:
        rest_caps, rest_open = _fill_seams(bm, rest_lay, boxes, eps, False, uv_rest)
    else:
        rest_caps, rest_open = 0, _open_seam_count(bm, rest_lay)
    part_caps = part_open = 0
    if part_bm is not None:
        uv_part = part_bm.loops.layers.uv.active
        part_lay = part_bm.edges.layers.int.get("af_open")
        if caps:
            part_caps, part_open = _fill_seams(part_bm, part_lay, boxes, eps, True, uv_part)
        else:
            part_open = _open_seam_count(part_bm, part_lay)
    for b in (bm, part_bm):
        if b is None:
            continue
        lay = b.edges.layers.int.get("af_open")
        if lay is not None:
            b.edges.layers.int.remove(lay)
    bm.to_mesh(me)
    bm.free()
    me.update()
    info.update(rest_cap_faces=rest_caps, rest_open_seam_edges=rest_open, cap_faces=part_caps,
                open_seam_edges=part_open)
    if part_bm is None:
        return None, info
    pme = bpy.data.meshes.new(part_name)
    part_bm.to_mesh(pme)
    part_bm.free()
    for m in me.materials:
        pme.materials.append(m)
    part = bpy.data.objects.new(part_name, pme)
    for coll in ob.users_collection:
        coll.objects.link(part)
    return part, info


class SlitSurface:
    """The cut surface of a slit: one plane, or a piecewise-linear curve value = f(along) extruded
    along the third axis (bands between the control points, one plane per band).

    spec: {"axis": "x", "value": v} | {"point": [x,y,z], "normal": [x,y,z]} (one plane) or
          {"axis": "x", "along": "z", "points": [[along_0, value_0], [along_1, value_1], ...]}."""

    def __init__(self, spec, boxes=None):
        self.curve = None
        if spec and "points" in spec:
            ax = str(spec.get("axis", "x")).lower()
            al = str(spec.get("along", "z")).lower()
            if len(ax) != 1 or len(al) != 1 or ax not in "xyz" or al not in "xyz" or ax == al:
                raise ValueError("slit curve: axis and along must be two different of x, y, z")
            pts = sorted((float(a), float(v)) for a, v in spec["points"])
            if len(pts) < 2:
                raise ValueError("slit curve needs at least two points [along, value]")
            if any(pts[i + 1][0] - pts[i][0] < 1e-9 for i in range(len(pts) - 1)):
                raise ValueError("slit curve points need distinct 'along' values")
            self.curve = ("xyz".index(ax), "xyz".index(al), pts)
            self.planes = []
            for (a0, v0), (a1, v1) in zip(pts[:-1], pts[1:]):
                t = mathutils.Vector((0.0, 0.0, 0.0))
                t[self.curve[1]] = a1 - a0
                t[self.curve[0]] = v1 - v0
                n = mathutils.Vector((0.0, 0.0, 0.0))
                n[self.curve[0]] = 1.0
                n = (n - t * (n.dot(t) / t.length_squared)).normalized()
                co = mathutils.Vector((0.0, 0.0, 0.0))
                co[self.curve[1]] = a0
                co[self.curve[0]] = v0
                self.planes.append((a0, a1, co, n))
        else:
            co, no = plane_from_spec(spec, boxes)
            self.planes = [(-math.inf, math.inf, co, no)]

    def band_of(self, p):
        if self.curve is None:
            return 0
        a = p[self.curve[1]]
        for k, (a0, a1, _, _) in enumerate(self.planes):
            if a <= a1:
                return k
        return len(self.planes) - 1

    def offset(self, p):
        """Signed offset of point p from the surface (along the plane normal of its band)."""
        _, _, co, no = self.planes[self.band_of(p)]
        return (mathutils.Vector(p) - co).dot(no)

    def normal(self, p):
        return self.planes[self.band_of(p)][3]

    def describe(self):
        if self.curve is None:
            _, _, co, no = self.planes[0]
            return {"point": [round(v, 5) for v in co], "normal": [round(v, 5) for v in no]}
        return {"axis": "xyz"[self.curve[0]], "along": "xyz"[self.curve[1]],
                "points": [[round(a, 5), round(v, 5)] for a, v in self.curve[2]]}


def plane_from_spec(spec, boxes=None):
    """{"axis": "x"|"y"|"z", "value": v} or {"point": [x, y, z], "normal": [x, y, z]} ->
    (point, unit normal). Without a spec the plane is x = centre of the first box (a slit between
    a sleeve and the body side runs along the body)."""
    if not spec:
        if not boxes:
            raise ValueError("a slit needs params plane or a box")
        lo, hi = boxes[0]
        c = mathutils.Vector([(lo[k] + hi[k]) * 0.5 for k in range(3)])
        return c, mathutils.Vector((1.0, 0.0, 0.0))
    if "axis" in spec and "value" in spec:
        ax = str(spec["axis"]).lower()
        if ax not in ("x", "y", "z"):
            raise ValueError("plane.axis must be x, y or z")
        k = "xyz".index(ax)
        n = mathutils.Vector((0.0, 0.0, 0.0))
        n[k] = 1.0
        co = mathutils.Vector((0.0, 0.0, 0.0))
        co[k] = float(spec["value"])
        return co, n
    if "point" in spec and "normal" in spec:
        n = mathutils.Vector([float(v) for v in spec["normal"]])
        if n.length < 1e-9:
            raise ValueError("plane.normal must not be zero")
        return mathutils.Vector([float(v) for v in spec["point"]]), n.normalized()
    raise ValueError("plane must be {axis, value}, {point, normal} or a curve {axis, along, points}")


def _bisect_faces(bm, faces, co, no, eps):
    faces = [f for f in faces if f.is_valid]
    strad = []
    for f in faces:
        d = [(v.co - co).dot(no) for v in f.verts]
        if min(d) < -eps and max(d) > eps:
            strad.append(f)
    if not strad:
        return set()
    edges = {e for f in strad for e in f.edges}
    verts = {v for f in strad for v in f.verts}
    ret = bmesh.ops.bisect_plane(bm, geom=list(verts) + list(edges) + strad, dist=eps,
                                 plane_co=co, plane_no=no)
    return {g for g in ret["geom"] if isinstance(g, bmesh.types.BMFace)}


def _chains(edges):
    """Group edges into vertex chains (open chains start at an end). Returns lists of BMVerts."""
    adj = {}
    for e in edges:
        a, b_ = e.verts
        adj.setdefault(a, []).append(b_)
        adj.setdefault(b_, []).append(a)
    seen, out = set(), []
    starts = [v for v, nb in adj.items() if len(nb) == 1] + list(adj)
    for s in starts:
        if s in seen:
            continue
        chain, prev, cur = [s], None, s
        seen.add(s)
        while True:
            nxt = [v for v in adj[cur] if v is not prev and v not in seen]
            if not nxt:
                break
            prev, cur = cur, nxt[0]
            chain.append(cur)
            seen.add(cur)
        out.append(chain)
    return out


def _cap_side(bm, chains, uv, surface, sgn):
    """Close the openings of one side of a slab with one cap: the rim chains of that side (e.g. the
    front and the back crease of a fused sleeve) are joined end to nearest end into one loop - the
    cross-section of that side's solid in the offset plane - and filled. The cap gets the rim UVs and
    material, flat shading, and faces into the slab (away from its own side). Returns the faces."""
    chains = [list(c) for c in chains if len(c) >= 2]
    if not chains:
        return []
    if len(chains) == 2:
        along = None
        if surface.curve is not None:
            along = mathutils.Vector((0.0, 0.0, 0.0))
            along[surface.curve[1]] = 1.0
        new = _zip_strip(bm, chains[0], chains[1], along)
        if new:
            return _finish_cap(bm, new, uv, surface, sgn)
    loop = chains.pop(0)
    while chains:
        end = loop[-1].co
        best, bi, rev = None, None, False
        for i, c in enumerate(chains):
            for r, v in ((False, c[0]), (True, c[-1])):
                d = (v.co - end).length
                if best is None or d < best:
                    best, bi, rev = d, i, r
        c = chains.pop(bi)
        loop.extend(reversed(c) if rev else c)
    seen, verts = set(), []
    for v in loop:
        if v not in seen:
            seen.add(v)
            verts.append(v)
    if len(verts) < 3:
        return []
    # a U-shaped rim (front and back crease rims joined at one end of the slit): split it at its
    # far end and zip the two halves by height - the same strip as for two separate rims
    if surface.curve is not None and len(verts) >= 6:
        along = mathutils.Vector((0.0, 0.0, 0.0))
        along[surface.curve[1]] = 1.0
        t = [v.co.dot(along) for v in verts]
        mid = 0.5 * (t[0] + t[-1])
        k = max(range(2, len(verts) - 2), key=lambda i: abs(t[i] - mid))
        span = max(t) - min(t)
        if span > 1e-6 and abs(t[k] - mid) > 0.5 * span and abs(t[0] - t[-1]) < 0.5 * span:
            new = _zip_strip(bm, verts[:k + 1], verts[k:], along)
            if new:
                return _finish_cap(bm, new, uv, surface, sgn)
    try:
        f = bm.faces.new(verts)
    except ValueError:
        return []
    new = [f]
    tri = bmesh.ops.triangulate(bm, faces=new, quad_method="BEAUTY", ngon_method="BEAUTY")
    new = [x for x in tri.get("faces", new) if x.is_valid]
    return _finish_cap(bm, new, uv, surface, sgn)


def _zip_strip(bm, A, B, along=None):
    """Triangle strip between two open rim chains (the front and the back crease rim of one side).
    Both chains are walked by their coordinate along the slit (along: unit vector; default the
    direction of chain A), so every triangle joins vertices at about the same height of the slit -
    no long diagonal slivers whose corners would follow different bones (elbow vs shoulder)."""
    if along is None:
        along = A[-1].co - A[0].co
        if along.length < 1e-9:
            along = mathutils.Vector((0.0, 0.0, 1.0))
    along = along.normalized()
    if (A[-1].co - A[0].co).dot(along) < 0:
        A = list(reversed(A))
    if (B[-1].co - B[0].co).dot(along) < 0:
        B = list(reversed(B))
    ta = [v.co.dot(along) for v in A]
    tb = [v.co.dot(along) for v in B]
    i = j = 0
    faces = []
    while i < len(A) - 1 or j < len(B) - 1:
        if j == len(B) - 1 or (i < len(A) - 1 and ta[i + 1] <= tb[j + 1]):
            tri = (A[i], A[i + 1], B[j])
            i += 1
        else:
            tri = (A[i], B[j + 1], B[j])
            j += 1
        if len(set(tri)) < 3:
            continue
        try:
            faces.append(bm.faces.new(tri))
        except ValueError:
            pass   # a face over these vertices exists already
    return faces


def _finish_cap(bm, new, uv, surface, sgn):
    new = [f for f in new if f.is_valid]
    nset = set(new)
    for f in new:
        f.smooth = False
        nb = [l.face for e in f.edges for l in e.link_loops if l.face not in nset]
        if nb:
            f.material_index = nb[0].material_index
        if uv is not None:
            for l in f.loops:
                src = [x for x in l.vert.link_loops if x.face not in nset]
                if src:
                    l[uv].uv = src[0][uv].uv
    bm.normal_update()
    flip = [f for f in new if f.normal.dot(surface.normal(f.calc_center_median())) * sgn > 0]
    if flip:
        bmesh.ops.reverse_faces(bm, faces=flip)
    return new


def slit(ob, boxes, surface, width=0.0, gap=0.0, caps=True, eps=None):
    """Open slit through ob (world == local), only inside the union of boxes; nothing re-joins the
    two sides. surface: SlitSurface (a plane or a curve value = f(along)).

    width == 0: rip. Faces that overlap a box and straddle the surface are bisected; the new edges
    on the surface inside a box that separate faces on its two sides are split (vertices
    duplicated): nothing is removed, the two sides keep coincident open rims. gap > 0 pulls each
    duplicated rim vertex back into its own faces by gap / 2, so welds of later steps (merge by
    distance, rig proxies) cannot sew the slit shut.
    width > 0: slab. The faces between the surfaces offset by +-width/2 are removed (clean cuts
    along both offsets). This also separates a fused web whose pinch lines do not lie exactly on the
    surface (a sleeve fused to the robe side along a crease that wanders a few mm). caps=True closes
    each side's opening with its own cap (the boundary chain on that side is closed by one edge
    between its ends and filled), so no hole shows and the two sides stay apart; caps=False leaves
    the openings open.
    Returns info: bisected faces, ripped edges / removed faces, caps per side, rim length."""
    me = ob.data
    lo, hi = af.world_bbox([ob])
    eps = eps or max((hi - lo).length * 1e-6, 1e-7)
    bm = bmesh.new()
    bm.from_mesh(me)
    open_layer = bm.edges.layers.int.new("af_open")
    for e in bm.edges:
        e[open_layer] = 1 if (e.is_boundary or len(e.link_faces) > 2) else 0
    t0 = time.time()
    cand = []
    for lo_b, hi_b in boxes:
        elo = [lo_b[k] - eps for k in range(3)]
        ehi = [hi_b[k] + eps for k in range(3)]
        cand.extend(f for f in bm.faces if _overlaps(_face_bounds(f), elo, ehi))
    cand = list(set(cand))
    n_bisect = 0
    if surface.curve is not None:
        # band borders first: afterwards every face lies in one band and meets only its own plane
        k_al = surface.curve[1]
        for a0, a1, _, _ in surface.planes[1:]:
            co = mathutils.Vector((0.0, 0.0, 0.0))
            co[k_al] = a0
            no = mathutils.Vector((0.0, 0.0, 0.0))
            no[k_al] = 1.0
            new = _bisect_faces(bm, cand, co, no, eps)
            cand = list({f for f in cand if f.is_valid} | new)
    offsets = (0.0,) if width <= 0 else (0.5 * width, -0.5 * width)
    for off in offsets:
        for k, (a0, a1, co, no) in enumerate(surface.planes):
            faces = [f for f in cand if f.is_valid and surface.band_of(f.calc_center_median()) == k]
            new = _bisect_faces(bm, faces, co + no * off, no, eps)
            n_bisect += len(new)
            cand = list({f for f in cand if f.is_valid} | new)
    bm.normal_update()
    info = {"faces_bisected": n_bisect, "width_m": float(width), "gap_m": float(gap),
            "surface": surface.describe()}

    def side(f):
        return surface.offset(f.calc_center_median())

    if width <= 0:
        rip = []
        for e in bm.edges:
            if len(e.link_faces) != 2:
                continue
            a, b_ = e.verts
            if not _inside_any((a.co + b_.co) * 0.5, boxes, eps):
                continue
            if abs(surface.offset(a.co)) > 2 * eps or abs(surface.offset(b_.co)) > 2 * eps:
                continue
            if side(e.link_faces[0]) * side(e.link_faces[1]) < 0:
                rip.append(e)
        info["ripped_edges"] = len(rip)
        info["rim_length_m"] = round(sum(e.calc_length() for e in rip), 4)
        moved = 0
        if rip:
            nv0 = len(bm.verts)
            bmesh.ops.split_edges(bm, edges=rip)
            bm.verts.ensure_lookup_table()
            info["ripped_verts"] = len(bm.verts) - nv0
            if gap > 0:
                rim = [v for v in bm.verts if v.is_boundary and abs(surface.offset(v.co)) <= 2 * eps and
                       _inside_any(v.co, boxes, eps)]
                shift = []
                for v in rim:
                    sides = [side(f) for f in v.link_faces]
                    if not sides or (min(sides) < -eps and max(sides) > eps):
                        continue   # the closed end of the slit keeps faces on both sides
                    c = mathutils.Vector((0.0, 0.0, 0.0))
                    for f in v.link_faces:
                        c += f.calc_center_median()
                    d = c / len(v.link_faces) - v.co
                    if d.length > 1e-12:
                        shift.append((v, d.normalized() * min(0.5 * gap, d.length)))
                for v, d in shift:
                    v.co += d
                    moved += 1
        info["rim_verts_moved"] = moved
        info["cut"] = len(rip)
    else:
        doomed = [f for f in bm.faces if _inside_any(f.calc_center_median(), boxes, eps) and
                  abs(side(f)) < 0.5 * width - eps]
        info["faces_removed"] = len(doomed)
        info["area_removed_m2"] = round(sum(f.calc_area() for f in doomed), 5)
        bmesh.ops.delete(bm, geom=doomed, context="FACES")
        loose = [v for v in bm.verts if not v.link_edges]
        if loose:
            bmesh.ops.delete(bm, geom=loose, context="VERTS")
        rims = [e for e in bm.edges if e.is_valid and e.is_boundary and not e[open_layer]]
        info["rim_length_m"] = round(sum(e.calc_length() for e in rims), 4)
        info["cut"] = len(doomed)
        cap_info = {"+": 0, "-": 0, "chains": 0, "open_rim_edges": 0}
        if caps and rims:
            uv = bm.loops.layers.uv.active
            for sgn, key in ((1.0, "+"), (-1.0, "-")):
                es = [e for e in rims if e.is_valid and e.is_boundary and
                      surface.offset((e.verts[0].co + e.verts[1].co) * 0.5) * sgn > 0]
                chs = _chains(es)
                new = _cap_side(bm, chs, uv, surface, sgn)
                cap_info[key] += len(new)
                cap_info["chains"] += len(chs)
                cap_info["chains_" + key] = [len(c) for c in chs]
        cap_info["open_rim_edges"] = sum(1 for e in bm.edges if e.is_valid and e.is_boundary and
                                         not e[open_layer])
        info["caps"] = cap_info
    lay = bm.edges.layers.int.get("af_open")
    if lay is not None:
        bm.edges.layers.int.remove(lay)
    bm.to_mesh(me)
    bm.free()
    me.update()
    info["seconds"] = round(time.time() - t0, 2)
    return info


def signed_volume(ob):
    me = ob.data
    me.calc_loop_triangles()
    if not len(me.loop_triangles):
        return 0.0
    co = mesh_arrays(me)["co"]
    tri = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("vertices", tri)
    t = co[tri.reshape(-1, 3)]
    return float(np.einsum("ij,ij->i", t[:, 0], np.cross(t[:, 1], t[:, 2])).sum() / 6.0)


def triangulate_object(ob):
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    ngons = [f for f in bm.faces if len(f.verts) > 3]
    if ngons:
        bmesh.ops.triangulate(bm, faces=ngons, quad_method="BEAUTY", ngon_method="BEAUTY")
    bm.to_mesh(ob.data)
    bm.free()
    ob.data.update()
