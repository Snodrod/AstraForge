"""Helpers of the replace_part step: region removal along a cut plane, rigid+scale alignment
(bbox init + symmetric trimmed ICP), seam bridging, orthographic front projection of a reference
crop and eye-socket detection on a raycast depth grid.

Owned by the replace_part module (docs/forge/replace_part.md). Blender 5.0.1, bundled Python 3.11
+ numpy only (no scipy / Pillow); nearest neighbours via mathutils.kdtree, rays via
mathutils.bvhtree. _mesh_lib is used read-only.
"""
import math
import os

import bmesh
import bpy
import mathutils
import numpy as np
from mathutils.bvhtree import BVHTree
from mathutils.kdtree import KDTree

import af_common as af
from steps import _mesh_lib as ml


# --------------------------------------------------------------------------- small helpers

def vec3(v, name="value"):
    try:
        out = np.array([float(x) for x in v], dtype=np.float64)
    except Exception as exc:  # noqa: BLE001
        raise ValueError("%s must be a list of 3 numbers" % name) from exc
    if out.shape != (3,) or not np.isfinite(out).all():
        raise ValueError("%s must be a list of 3 finite numbers" % name)
    return out


def srgb_to_linear(c):
    c = np.asarray(c, dtype=np.float64)
    return np.where(c <= 0.04045, c / 12.92, ((c + 0.055) / 1.055) ** 2.4)


def plane_from_params(cut, region_lo, region_hi):
    """Cut plane as (point, unit normal). The part lies on the +normal side.

    cut: None -> horizontal plane through the region bottom (normal +Z);
         {"axis": "z", "value": 1.5, "side": "+"} ;  {"point": [...], "normal": [...]}.
    """
    if not cut:
        return np.array([0.0, 0.0, float(region_lo[2])]), np.array([0.0, 0.0, 1.0])
    if "normal" in cut:
        n = vec3(cut["normal"], "cut_plane.normal")
        if np.linalg.norm(n) < 1e-9:
            raise ValueError("cut_plane.normal must not be zero")
        n = n / np.linalg.norm(n)
        if "point" in cut:
            p = vec3(cut["point"], "cut_plane.point")
        else:
            p = n * float(cut.get("value", 0.0))
    else:
        axis = str(cut.get("axis", "z")).lower()
        if axis not in ("x", "y", "z"):
            raise ValueError("cut_plane.axis must be x, y or z")
        k = "xyz".index(axis)
        n = np.zeros(3)
        n[k] = 1.0
        p = np.zeros(3)
        p[k] = float(cut.get("value", region_lo[k]))
    if str(cut.get("side", "+")) in ("-", "below", "negative"):
        n = -n
    return p, n


def signed_dist(co, plane):
    p, n = plane
    return (np.asarray(co, dtype=np.float64) - p) @ n


def face_arrays(me):
    """Triangle-agnostic face data: centres, areas, normals (numpy)."""
    nf = len(me.polygons)
    cen = np.empty(nf * 3)
    me.polygons.foreach_get("center", cen)
    area = np.empty(nf)
    me.polygons.foreach_get("area", area)
    nor = np.empty(nf * 3)
    me.polygons.foreach_get("normal", nor)
    return cen.reshape(nf, 3), area, nor.reshape(nf, 3)


def tri_arrays(ob):
    """(co (n,3), tris (m,3)) of the object's mesh in world space (transforms applied)."""
    me = ob.data
    me.calc_loop_triangles()
    co = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    mw = np.array(ob.matrix_world)
    co = co @ mw[:3, :3].T + mw[:3, 3]
    tri = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("vertices", tri)
    return co, tri.reshape(-1, 3)


def sample_surface(ob, n, seed=0, mask_faces=None):
    """Area-weighted random points on the triangles of ob (world space)."""
    co, tri = tri_arrays(ob)
    if not len(tri):
        return np.zeros((0, 3))
    a, b, c = co[tri[:, 0]], co[tri[:, 1]], co[tri[:, 2]]
    area = 0.5 * np.linalg.norm(np.cross(b - a, c - a), axis=1)
    if mask_faces is not None:
        area = area * mask_faces
    tot = area.sum()
    if tot <= 0:
        return np.zeros((0, 3))
    rng = np.random.default_rng(seed)
    idx = rng.choice(len(tri), size=int(n), p=area / tot)
    r1 = np.sqrt(rng.random(int(n)))
    r2 = rng.random(int(n))
    w0, w1, w2 = 1 - r1, r1 * (1 - r2), r1 * r2
    return a[idx] * w0[:, None] + b[idx] * w1[:, None] + c[idx] * w2[:, None]


def bvh_of(ob):
    co, tri = tri_arrays(ob)
    return BVHTree.FromPolygons([tuple(v) for v in co.tolist()], [tuple(t) for t in tri.tolist()],
                                all_triangles=True), co, tri


def kdtree(points):
    kd = KDTree(len(points))
    for i, p in enumerate(points.tolist()):
        kd.insert(p, i)
    kd.balance()
    return kd


def nearest(kd, points):
    idx = np.empty(len(points), dtype=np.int64)
    dist = np.empty(len(points))
    for i, p in enumerate(points.tolist()):
        _co, j, d = kd.find(p)
        idx[i] = j
        dist[i] = d
    return idx, dist


# --------------------------------------------------------------------------- region removal

def _bisect_footprint(bm, plane, lo, hi, eps):
    """Bisect only the faces that straddle the plane inside the (expanded) region box."""
    p, n = plane
    pv = mathutils.Vector(p.tolist())
    nv = mathutils.Vector(n.tolist())
    strad = []
    for f in bm.faces:
        cos = [v.co for v in f.verts]
        if min(c.x for c in cos) > hi[0] or max(c.x for c in cos) < lo[0]:
            continue
        if min(c.y for c in cos) > hi[1] or max(c.y for c in cos) < lo[1]:
            continue
        if min(c.z for c in cos) > hi[2] or max(c.z for c in cos) < lo[2]:
            continue
        d = [(c - pv).dot(nv) for c in cos]
        if min(d) < -eps and max(d) > eps:
            strad.append(f)
    if not strad:
        return 0
    edges = {e for f in strad for e in f.edges}
    verts = {v for f in strad for v in f.verts}
    bmesh.ops.bisect_plane(bm, geom=list(verts) + list(edges) + strad, dist=eps, plane_co=pv,
                           plane_no=nv)
    return len(strad)


def _pieces(me, plane, side, eps):
    """Faces on one side of the plane (side=+1/-1, by face centre) grouped into pieces that are
    connected without crossing an edge lying on the plane. Returns (labels per face (-1 = other
    side), mesh arrays, face centres, face areas)."""
    a = ml.mesh_arrays(me)
    cen, area, _nor = face_arrays(me)
    sd = signed_dist(cen, plane)
    sel = sd * side > eps
    ev, co = a["ev"], a["co"]
    vd = signed_dist(co, plane)
    on_plane = (np.abs(vd[ev[:, 0]]) <= 4 * eps) & (np.abs(vd[ev[:, 1]]) <= 4 * eps)
    nf = len(a["ls"])
    loop_face = np.repeat(np.arange(nf), a["lt"])
    efc = ml.edge_face_counts(a)
    order = np.argsort(a["le"], kind="stable")
    starts = np.zeros(len(ev) + 1, dtype=np.int64)
    starts[1:] = np.cumsum(efc)
    two = np.nonzero((efc == 2) & ~on_plane)[0]
    f1 = loop_face[order[starts[two]]]
    f2 = loop_face[order[starts[two] + 1]]
    keep = sel[f1] & sel[f2]
    labels = ml.components(nf, np.stack([f1[keep], f2[keep]], axis=1))
    labels = np.where(sel, labels, -1)
    return labels, a, cen, area


def used_bbox(ob):
    """World bbox of the vertices that belong to faces (bound_box can be stale after bmesh)."""
    co, tri = tri_arrays(ob)
    if not len(tri):
        return np.zeros(3), np.zeros(3)
    used = co[np.unique(tri)]
    return used.min(axis=0), used.max(axis=0)


def _piece_hangs(labels, a, plane, side_sign=-1):
    """Per piece label: (max distance beyond the plane on its side, its vertex ids)."""
    vd = signed_dist(a["co"], plane)
    loop_face = np.repeat(np.arange(len(labels)), a["lt"])
    lab_loop = labels[loop_face]
    out = {}
    m = lab_loop >= 0
    order = np.argsort(lab_loop[m], kind="stable")
    labs = lab_loop[m][order]
    verts = a["lv"][m][order]
    if not len(labs):
        return out
    cuts = np.nonzero(np.diff(labs))[0] + 1
    for grp_l, grp_v in zip(np.split(labs, cuts), np.split(verts, cuts)):
        vs = np.unique(grp_v)
        out[int(grp_l[0])] = (float((side_sign * vd[vs]).max()), vs)
    return out


def remove_region(ob, lo, hi, plane, min_inside=0.5, margin=0.5, max_hang=0.0):
    """Delete the part of ob that lies inside the region box on the +side of the cut plane.

    The mesh is bisected by the plane under the region footprint (box grown by `margin` of its
    size), faces on the +side are grouped into pieces that are connected without crossing the
    plane, and every piece with at least `min_inside` of its area inside the box is removed - so a
    slightly too tight box still takes the whole head, while a hood edge poking into the box (it
    continues below the plane outside the footprint) stays.

    max_hang > 0: pieces on the -side that touch the plane only where removed faces did and
    reach at most max_hang below it go too (a jaw that dips under the neck cut). The neck itself
    continues into the torso and stays. Returns (old_piece_object, info).
    """
    me = ob.data
    lo, hi = np.asarray(lo, float), np.asarray(hi, float)
    size = hi - lo
    glo, ghi = lo - size * margin, hi + size * margin
    blo, bhi = used_bbox(ob)
    eps = max(float(np.linalg.norm(bhi - blo)) * 1e-6, 1e-7)
    bm = bmesh.new()
    bm.from_mesh(me)
    n_bisect = _bisect_footprint(bm, plane, glo, ghi, eps)
    bm.to_mesh(me)
    bm.free()
    me.update()
    labels, a, cen, area = _pieces(me, plane, +1, eps)
    pos = labels >= 0
    nf = len(labels)
    inside = np.all((cen >= lo - eps) & (cen <= hi + eps), axis=1) & pos
    lab_safe = np.where(pos, labels, 0)
    tot_area = np.bincount(lab_safe, weights=area * pos, minlength=nf)
    in_area = np.bincount(lab_safe, weights=area * inside, minlength=nf)
    remove = np.zeros(nf, dtype=bool)
    pieces = []
    for lab in np.unique(labels[pos]).tolist():
        frac = in_area[lab] / tot_area[lab] if tot_area[lab] > 0 else 0.0
        if in_area[lab] > 0:
            pieces.append({"faces": int((labels == lab).sum()), "inside_fraction": round(frac, 4)})
        if frac >= min_inside and in_area[lab] > 0:
            remove |= labels == lab
    hanging = []
    if max_hang > 0 and remove.any():
        nlab, _a2, _c2, _ar2 = _pieces(me, plane, -1, eps)
        vd = signed_dist(a["co"], plane)
        loop_face = np.repeat(np.arange(nf), a["lt"])
        rem_verts = np.unique(a["lv"][remove[loop_face]])
        rem_plane = set(rem_verts[np.abs(vd[rem_verts]) <= 4 * eps].tolist())
        for lab, (hang, vs) in _piece_hangs(nlab, a, plane, -1).items():
            on = vs[np.abs(vd[vs]) <= 4 * eps]
            if hang <= max_hang and len(on) and all(int(v) in rem_plane for v in on):
                remove |= nlab == lab
                hanging.append({"faces": int((nlab == lab).sum()), "hang_m": round(hang, 5)})
    info = {"bisected_faces": n_bisect, "removed_faces": int(remove.sum()),
            "removed_hanging_pieces": hanging,
            "removed_area_m2": round(float(area[remove].sum()), 6),
            "pieces_touching_box": sorted(pieces, key=lambda d: -d["faces"])[:6]}
    if not remove.any():
        return None, info
    old = ml.duplicate(ob, "removed_region")
    _delete_faces(old, ~remove)
    _delete_faces(ob, remove)
    info["removed_tris"] = ml.tris_of(old)
    lo_o, hi_o = used_bbox(old)
    info["removed_bbox_min"] = [round(float(v), 5) for v in lo_o]
    info["removed_bbox_max"] = [round(float(v), 5) for v in hi_o]
    return old, info


def _delete_faces(ob, mask):
    idx = np.nonzero(mask)[0]
    if not len(idx):
        return
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    bm.faces.ensure_lookup_table()
    bmesh.ops.delete(bm, geom=[bm.faces[i] for i in idx.tolist()], context="FACES")
    bm.to_mesh(ob.data)
    bm.free()
    ob.data.update()


def plane_basis(n):
    n = np.asarray(n, float)
    t = np.array([1.0, 0.0, 0.0]) if abs(n[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    e1 = np.cross(n, t)
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(n, e1)
    return e1, e2


def plane_loops(ob, plane, tol=None):
    """Open boundary loops of ob lying on the plane, as 2D segment arrays in the plane basis:
    [{"segments": (k,2,2), "centroid": (2,), "radius": float, "edges": k}]."""
    me = ob.data
    a = ml.mesh_arrays(me)
    lo, hi = used_bbox(ob)
    tol = tol or max(float(np.linalg.norm(hi - lo)) * 2e-5, 1e-6)
    efc = ml.edge_face_counts(a)
    ev, co = a["ev"], a["co"]
    vd = signed_dist(co, plane)
    sel = (efc == 1) & (np.abs(vd[ev[:, 0]]) <= tol) & (np.abs(vd[ev[:, 1]]) <= tol)
    if not sel.any():
        return []
    e = ev[sel]
    lab = ml.components(len(co), e)
    e1, e2 = plane_basis(plane[1])
    p2 = np.stack([(co - plane[0]) @ e1, (co - plane[0]) @ e2], axis=1)
    out = []
    for l in np.unique(lab[e[:, 0]]).tolist():
        ee = e[lab[e[:, 0]] == l]
        seg = np.stack([p2[ee[:, 0]], p2[ee[:, 1]]], axis=1)
        mids = seg.mean(axis=1)
        length = np.linalg.norm(seg[:, 1] - seg[:, 0], axis=1)
        c = (mids * length[:, None]).sum(axis=0) / max(length.sum(), 1e-12)
        out.append({"segments": seg, "centroid": c, "radius": float(length.sum() / (2 * math.pi)),
                    "edges": int(len(ee))})
    return out


def _inside_or_near(pt, loop, tol_frac=0.3):
    seg = loop["segments"]
    x, y = pt
    a, b = seg[:, 0], seg[:, 1]
    cond = (a[:, 1] > y) != (b[:, 1] > y)
    with np.errstate(divide="ignore", invalid="ignore"):
        xc = a[:, 0] + (y - a[:, 1]) * (b[:, 0] - a[:, 0]) / (b[:, 1] - a[:, 1])
    inside = int((cond & (xc > x)).sum()) % 2 == 1
    if inside:
        return True
    ab = b - a
    t = np.clip(((pt - a) * ab).sum(axis=1) / np.maximum((ab ** 2).sum(axis=1), 1e-18), 0, 1)
    d = np.linalg.norm(a + ab * t[:, None] - pt, axis=1).min()
    return d <= tol_frac * loop["radius"]


def trim_by_plane(ob, plane, offset=0.0, keep_hang=0.0, body_loops=None):
    """Cut ob by the plane moved `offset` along its normal and drop what hangs on the -side.

    A -side piece whose cut lies inside (or near) one of body_loops - the body's open loops on the
    cut plane, e.g. the neck - is the part's own copy of that stump (a neck stub, with whatever
    base plate the generator glued to it) and is removed. Other -side pieces are kept when they
    reach at most keep_hang below the plane (a chin dipping under a neck cut stays whole) and
    removed otherwise, and so is every piece that does not touch the cut at all. Returns info."""
    p, n = plane
    p2 = p + n * float(offset)
    plane2 = (p2, n)
    me = ob.data
    bm = bmesh.new()
    bm.from_mesh(me)
    before = len(bm.faces)
    bmesh.ops.bisect_plane(bm, geom=list(bm.verts) + list(bm.edges) + list(bm.faces),
                           dist=1e-7, plane_co=mathutils.Vector(p2.tolist()),
                           plane_no=mathutils.Vector(n.tolist()))
    bm.to_mesh(me)
    bm.free()
    me.update()
    lo, hi = used_bbox(ob)
    eps = max(float(np.linalg.norm(hi - lo)) * 1e-6, 1e-7)
    labels, a, cen, area = _pieces(me, plane2, -1, eps)
    remove = np.zeros(len(labels), dtype=bool)
    kept, dropped = [], []
    vd = signed_dist(a["co"], plane2)
    e1, e2 = plane_basis(n)
    for lab, (hang, vs) in _piece_hangs(labels, a, plane2, -1).items():
        fm = labels == lab
        on = vs[np.abs(vd[vs]) <= 4 * eps]
        stump = False
        if body_loops and len(on):
            q = a["co"][on] - p
            c2 = np.array([(q @ e1).mean(), (q @ e2).mean()])
            stump = any(_inside_or_near(c2, L) for L in body_loops)
        item = {"faces": int(fm.sum()), "hang_m": round(hang, 5), "stump": bool(stump),
                "touches_cut": bool(len(on))}
        # a piece that does not reach the cut floats below it (a base plate fragment)
        if len(on) and not stump and keep_hang > 0 and hang <= keep_hang:
            kept.append(item)
        else:
            remove |= fm
            dropped.append(item)
    _delete_faces(ob, remove)
    return {"faces_before": before, "faces_after": len(me.polygons), "plane_point": p2.tolist(),
            "removed_faces": int(remove.sum()), "kept_hanging_pieces": kept[:8],
            "removed_pieces": sorted(dropped, key=lambda d: -d["faces"])[:8]}


# --------------------------------------------------------------------------- alignment

def umeyama(P, Q, w=None, with_scale=True, with_rotation=True):
    """Least-squares similarity Q ~ s R P + t (Umeyama 1991). Returns (s, R, t)."""
    P = np.asarray(P, float)
    Q = np.asarray(Q, float)
    w = np.ones(len(P)) if w is None else np.asarray(w, float)
    w = w / w.sum()
    mp = (P * w[:, None]).sum(axis=0)
    mq = (Q * w[:, None]).sum(axis=0)
    Pc, Qc = P - mp, Q - mq
    var_p = float((w * (Pc ** 2).sum(axis=1)).sum())
    if with_rotation:
        S = (Qc * w[:, None]).T @ Pc
        U, D, Vt = np.linalg.svd(S)
        E = np.eye(3)
        if np.linalg.det(U) * np.linalg.det(Vt) < 0:
            E[2, 2] = -1.0
        R = U @ E @ Vt
        s = float(np.trace(np.diag(D) @ E) / var_p) if with_scale and var_p > 0 else 1.0
    else:
        R = np.eye(3)
        s = float((w * (Qc * Pc).sum(axis=1)).sum() / var_p) if with_scale and var_p > 0 else 1.0
    t = mq - s * (R @ mp)
    return s, R, t


def apply_sim(points, sim):
    s, R, t = sim
    return s * (np.asarray(points) @ R.T) + t


def sim_matrix(sim):
    s, R, t = sim
    M = np.eye(4)
    M[:3, :3] = s * R
    M[:3, 3] = t
    return mathutils.Matrix(M.tolist())


def compose(a, b):
    """a after b (both (s, R, t))."""
    sa, Ra, ta = a
    sb, Rb, tb = b
    return sa * sb, Ra @ Rb, sa * (Ra @ tb) + ta


def rotation_deg(R):
    c = (np.trace(R) - 1.0) / 2.0
    return float(math.degrees(math.acos(max(-1.0, min(1.0, c)))))


def _upper_extent(points, frac=0.6, lo_q=2.0, hi_q=98.0):
    """Robust extents of the top `frac` of a point cloud (stubs, plates and necks below do not
    count): (x_extent, y_extent, centre_x, centre_y, top_z)."""
    z = points[:, 2]
    top = np.percentile(z, 99.7)
    bot = np.percentile(z, 0.3)
    sel = points[z >= top - frac * (top - bot)]
    if len(sel) < 10:
        sel = points
    qlo = np.percentile(sel[:, :2], lo_q, axis=0)
    qhi = np.percentile(sel[:, :2], hi_q, axis=0)
    ext = qhi - qlo
    return ext[0], ext[1], (qlo + qhi) / 2.0, top


def bbox_init(src, tgt, frac=0.6):
    """Initial similarity (no rotation): match the robust width/depth of the top `frac` of both
    clouds and their top points. Robust to a neck stub or a flat artefact under the part."""
    sx, sy, sc, stop = _upper_extent(src, frac)
    tx, ty, tc, ttop = _upper_extent(tgt, frac)
    ratios = [tx / sx if sx > 1e-9 else 1.0, ty / sy if sy > 1e-9 else 1.0]
    s = float(np.mean(ratios))
    t = np.array([tc[0] - s * sc[0], tc[1] - s * sc[1], ttop - s * stop])
    return (s, np.eye(3), t), {"scale_x": round(ratios[0], 5), "scale_y": round(ratios[1], 5)}


def _rms(d):
    return float(np.sqrt(np.mean(d ** 2))) if len(d) else None


def icp_similarity(src, tgt, init, plane=None, plane_margin=0.0, iters=60, trim=0.85,
                   with_rotation=True, with_scale=True, tol=1e-7):
    """Symmetric trimmed ICP with a uniform scale.

    src: part surface samples (part coordinates), tgt: removed-region surface samples (world).
    Each iteration pairs every transformed source point with its nearest target point and every
    target point with its nearest transformed source point, drops the worst (1 - trim) of each
    direction and solves the similarity in closed form (Umeyama). Source points that fall on the
    -side of the cut plane (the part's neck stub) are ignored. Returns (sim, info).
    """
    kd_t = kdtree(tgt)
    sim = init
    hist = []
    prev = None
    for it in range(int(iters)):
        S = apply_sim(src, sim)
        m = np.ones(len(src), dtype=bool)
        if plane is not None:
            m = signed_dist(S, plane) > -plane_margin
        if m.sum() < 20:
            m[:] = True
        src_m, S_m = src[m], S[m]
        i_f, d_f = nearest(kd_t, S_m)
        kd_s = kdtree(S_m)
        i_b, d_b = nearest(kd_s, tgt)
        cf = d_f <= np.percentile(d_f, 100 * trim)
        cb = d_b <= np.percentile(d_b, 100 * trim)
        P = np.concatenate([src_m[cf], src_m[i_b[cb]]])
        Q = np.concatenate([tgt[i_f[cf]], tgt[cb]])
        sim = umeyama(P, Q, with_scale=with_scale, with_rotation=with_rotation)
        rms_all = _rms(np.concatenate([d_f, d_b]))
        hist.append(rms_all)
        if prev is not None and abs(prev - rms_all) < tol:
            break
        prev = rms_all
    S = apply_sim(src, sim)
    m = signed_dist(S, plane) > -plane_margin if plane is not None else np.ones(len(src), bool)
    _, d_f = nearest(kd_t, S[m])
    _, d_b = nearest(kdtree(S[m]), tgt)
    d = np.concatenate([d_f, d_b])
    info = {"iterations": len(hist), "rms_m": _rms(d),
            "rms_trimmed_m": _rms(np.sort(d)[: max(1, int(len(d) * trim))]),
            "rms_forward_m": _rms(d_f), "rms_backward_m": _rms(d_b),
            "rms_history_m": [round(x, 6) for x in hist[:: max(1, len(hist) // 12)]]}
    return sim, info


def pair_rms(src, tgt, sim, plane=None):
    S = apply_sim(src, sim)
    if plane is not None:
        m = signed_dist(S, plane) > 0
        if m.sum() >= 20:
            S = S[m]
    _, d_f = nearest(kdtree(tgt), S)
    _, d_b = nearest(kdtree(S), tgt)
    return _rms(np.concatenate([d_f, d_b]))


def surface_rms(part_ob, old_ob, src, tgt, sim, plane=None):
    """Symmetric point-to-SURFACE RMS (no sample-spacing floor): transformed part samples to the
    old surface and old samples to the transformed part surface (BVH nearest points). Part
    samples on the -side of the cut plane (its neck stub) are left out, like in the ICP.
    Returns (rms, rms_forward, rms_backward) in metres."""
    co, tri = tri_arrays(part_ob)
    co_w = apply_sim(co, sim)
    bvh_p = BVHTree.FromPolygons([tuple(v) for v in co_w.tolist()],
                                 [tuple(t) for t in tri.tolist()], all_triangles=True)
    bvh_o, _c, _t = bvh_of(old_ob)
    S = apply_sim(src, sim)
    if plane is not None:
        m = signed_dist(S, plane) > 0
        if m.sum() >= 20:
            S = S[m]

    def dists(bvh, pts):
        out = np.empty(len(pts))
        for i, q in enumerate(pts.tolist()):
            hit = bvh.find_nearest(mathutils.Vector(q))
            out[i] = hit[3] if hit[0] is not None else np.nan
        return out[np.isfinite(out)]

    d_f = dists(bvh_o, S)
    d_b = dists(bvh_p, tgt)
    return _rms(np.concatenate([d_f, d_b])), _rms(d_f), _rms(d_b)


# --------------------------------------------------------------------------- seam

def _loops_of(edges):
    """Group boundary edges into connected chains (lists of BMEdge)."""
    adj = {}
    for e in edges:
        for v in e.verts:
            adj.setdefault(v, []).append(e)
    seen = set()
    groups = []
    for e in edges:
        if e in seen:
            continue
        stack, grp = [e], []
        seen.add(e)
        while stack:
            x = stack.pop()
            grp.append(x)
            for v in x.verts:
                for y in adj[v]:
                    if y not in seen:
                        seen.add(y)
                        stack.append(y)
        groups.append(grp)
    groups.sort(key=lambda g: -sum(x.calc_length() for x in g))
    return groups


def bridge_seam(ob, plane, gap, body_mat_index=None, tol=None):
    """Connect the body's open loop on the cut plane with the part's open loop on the plane moved
    by `gap`: the biggest loop of each side is bridged (bmesh bridge_loops, unequal counts allowed),
    any other open loop on either plane is capped (holes_fill). Bridge faces take the body
    material and the UVs of the nearest body-rim vertex. Returns info."""
    me = ob.data
    lo, hi = af.world_bbox([ob])
    tol = tol or max((hi - lo).length * 2e-5, 1e-6)
    bm = bmesh.new()
    bm.from_mesh(me)
    p, n = plane
    pv, nv = mathutils.Vector(p.tolist()), mathutils.Vector(n.tolist())

    def sd(v):
        return (v.co - pv).dot(nv)

    bnd = [e for e in bm.edges if e.is_boundary]
    body_e = [e for e in bnd if all(abs(sd(v)) <= tol for v in e.verts)]
    part_e = [e for e in bnd if all(abs(sd(v) - gap) <= tol for v in e.verts)]
    body_loops = _loops_of(body_e)
    part_loops = _loops_of(part_e)
    info = {"body_loops": len(body_loops), "part_loops": len(part_loops), "bridged": False,
            "bridge_faces": 0, "cap_faces": 0}
    uv = bm.loops.layers.uv.active
    new_faces = []
    caps_src = []
    if body_loops and part_loops:
        a, b = body_loops[0], part_loops[0]
        rim = {v for e in a for v in e.verts}
        if body_mat_index is None:
            body_mat_index = a[0].link_faces[0].material_index if a[0].link_faces else 0
        rim_uv = {}
        if uv is not None:
            for v in rim:
                for l in v.link_loops:
                    rim_uv[v] = l[uv].uv.copy()
                    break
        try:
            res = bmesh.ops.bridge_loops(bm, edges=list(a) + list(b), use_pairs=False,
                                         use_cyclic=False, use_merge=False)
            faces = [f for f in res.get("faces", []) if f.is_valid]
        except Exception as exc:  # noqa: BLE001
            faces = []
            info["bridge_error"] = str(exc)
        if faces:
            info["bridged"] = True
            info["bridge_faces"] = len(faces)
            info["body_loop_edges"] = len(a)
            info["part_loop_edges"] = len(b)
            rim_list = list(rim)
            kd = KDTree(len(rim_list))
            for i, v in enumerate(rim_list):
                kd.insert(v.co, i)
            kd.balance()
            for f in faces:
                f.material_index = body_mat_index
                f.smooth = True
                if uv is not None:
                    for l in f.loops:
                        v = l.vert
                        if v not in rim_uv:
                            _c, j, _d = kd.find(v.co)
                            src = rim_list[j]
                        else:
                            src = v
                        if src in rim_uv:
                            l[uv].uv = rim_uv[src]
            new_faces.extend(faces)
            caps_src = body_loops[1:] + part_loops[1:]
        else:
            caps_src = body_loops + part_loops
    else:
        caps_src = body_loops + part_loops
    cap_faces = []
    for grp in caps_src:
        grp = [e for e in grp if e.is_valid and e.is_boundary]
        if not grp:
            continue
        res = bmesh.ops.holes_fill(bm, edges=grp, sides=0)
        fs = [f for f in res.get("faces", []) if f.is_valid]
        if fs:
            tri = bmesh.ops.triangulate(bm, faces=fs, quad_method="BEAUTY", ngon_method="BEAUTY")
            fs = [f for f in tri.get("faces", fs) if f.is_valid]
        for f in fs:
            f.smooth = False
            nb = [l2.face for e in f.edges for l2 in e.link_loops if l2.face not in fs]
            if nb:
                f.material_index = nb[0].material_index
            if uv is not None:
                for l in f.loops:
                    srcs = [x for x in l.vert.link_loops if x.face not in fs]
                    if srcs:
                        l[uv].uv = srcs[0][uv].uv
        cap_faces.extend(fs)
    new_faces.extend(cap_faces)
    info["cap_faces"] = len(cap_faces)
    # consistent winding with the neighbours
    newset = set(new_faces)
    flip = []
    for f in new_faces:
        for l in f.loops:
            others = [x for x in l.edge.link_loops if x.face not in newset]
            if others:
                if others[0].vert == l.vert:
                    flip.append(f)
                break
    if flip:
        bmesh.ops.reverse_faces(bm, faces=flip)
    info["flipped"] = len(flip)
    tri_faces = [f for f in new_faces if f.is_valid and len(f.verts) > 3]
    if tri_faces:
        bmesh.ops.triangulate(bm, faces=tri_faces, quad_method="BEAUTY", ngon_method="BEAUTY")
    left = [e for e in bm.edges if e.is_boundary and
            (all(abs(sd(v)) <= tol for v in e.verts) or all(abs(sd(v) - gap) <= tol for v in e.verts))]
    info["open_seam_edges"] = len(left)
    bm.to_mesh(me)
    bm.free()
    me.update()
    return info


def boundary_edge_count(ob):
    a = ml.mesh_arrays(ob.data)
    return int((ml.edge_face_counts(a) == 1).sum())


# --------------------------------------------------------------------------- 2D grid tools

def _filt1d(a, k, axis, fn):
    r = k // 2
    pad = [(0, 0), (0, 0)]
    pad[axis] = (r, r)
    ap = np.pad(a, pad, mode="edge")
    win = np.lib.stride_tricks.sliding_window_view(ap, k, axis=axis)
    return fn(win, axis=-1)


def max_filter(a, k):
    k = max(1, int(k) | 1)
    return _filt1d(_filt1d(a, k, 0, np.max), k, 1, np.max)


def min_filter(a, k):
    k = max(1, int(k) | 1)
    return _filt1d(_filt1d(a, k, 0, np.min), k, 1, np.min)


def label2d(mask):
    """4-connected components of a boolean grid. Returns (labels (-1 = background), count)."""
    h, w = mask.shape
    idx = np.arange(h * w).reshape(h, w)
    e = []
    m = mask
    hz = m[:, :-1] & m[:, 1:]
    e.append(np.stack([idx[:, :-1][hz], idx[:, 1:][hz]], axis=1))
    vt = m[:-1, :] & m[1:, :]
    e.append(np.stack([idx[:-1, :][vt], idx[1:, :][vt]], axis=1))
    edges = np.concatenate(e) if e else np.zeros((0, 2), dtype=np.int64)
    lab = ml.components(h * w, edges.astype(np.int64)).reshape(h, w)
    out = np.full((h, w), -1, dtype=np.int64)
    if m.any():
        u, inv = np.unique(lab[m], return_inverse=True)
        out[m] = inv
        return out, len(u)
    return out, 0


def fill_nearest(vals, valid, iters=None):
    """Extend valid grid values into invalid cells (mean of valid 4-neighbours, repeated)."""
    v = np.where(valid, vals, 0.0).astype(np.float64)
    ok = valid.copy()
    iters = iters or max(vals.shape)
    for _ in range(iters):
        if ok.all():
            break
        s = np.zeros_like(v)
        c = np.zeros_like(v)
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            sv = np.roll(v, (dy, dx), axis=(0, 1))
            so = np.roll(ok, (dy, dx), axis=(0, 1))
            if dy == 1:
                so[0, :] = False
            if dy == -1:
                so[-1, :] = False
            if dx == 1:
                so[:, 0] = False
            if dx == -1:
                so[:, -1] = False
            s += np.where(so, sv, 0.0)
            c += so
        new = (~ok) & (c > 0)
        v[new] = s[new] / c[new]
        ok = ok | new
    if not ok.all() and ok.any():
        v[~ok] = v[ok].min()
    return v


# --------------------------------------------------------------------------- eye sockets

def depth_grid(bvh, co, yaw_deg, n=128, margin=0.04):
    """Orthographic depth map of a mesh seen from the front, the camera turned by yaw about Z
    (yaw 0 = camera at -Y looking +Y). Returns dict with D (distance from the ray start plane,
    nan = miss), the grid geometry and the hit mask."""
    th = math.radians(yaw_deg)
    fwd = np.array([math.sin(th), math.cos(th), 0.0])
    right = np.array([math.cos(th), -math.sin(th), 0.0])
    up = np.array([0.0, 0.0, 1.0])
    u = co @ right
    v = co @ up
    f = co @ fwd
    u0, u1, v0, v1 = u.min(), u.max(), v.min(), v.max()
    du, dv = (u1 - u0) * margin, (v1 - v0) * margin
    u0, u1, v0, v1 = u0 - du, u1 + du, v0 - dv, v1 + dv
    px = max(u1 - u0, v1 - v0) / n
    nu = max(8, int(math.ceil((u1 - u0) / px)))
    nv = max(8, int(math.ceil((v1 - v0) / px)))
    start = f.min() - 0.05 * (f.max() - f.min()) - 1e-4
    far = (f.max() - start) * 1.5 + 1e-3
    D = np.full((nv, nu), np.nan)
    fv = mathutils.Vector(fwd.tolist())
    for j in range(nv):
        vv = v0 + (j + 0.5) * px
        for i in range(nu):
            uu = u0 + (i + 0.5) * px
            o = right * uu + up * vv + fwd * start
            hit = bvh.ray_cast(mathutils.Vector(o.tolist()), fv, far)
            if hit[0] is not None:
                D[j, i] = hit[3]
    return {"D": D, "hit": ~np.isnan(D), "u0": u0, "v0": v0, "px": px, "fwd": fwd,
            "right": right, "up": up, "start": start, "yaw": yaw_deg}


def _socket_candidates(g, band=(0.35, 0.92), close_frac=0.34, thr_frac=0.3):
    D, hit = g["D"], g["hit"]
    nv, nu = D.shape
    if hit.sum() < 50:
        return [], None
    # misses enclosed by the silhouette (through-holes) count as very deep
    miss_lab, nm = label2d(~hit)
    exterior = set()
    for edge in (miss_lab[0, :], miss_lab[-1, :], miss_lab[:, 0], miss_lab[:, -1]):
        exterior.update(int(x) for x in np.unique(edge) if x >= 0)
    interior_miss = (~hit) & ~np.isin(miss_lab, list(exterior))
    Dv = D.copy()
    dmax = np.nanmax(D)
    Dv[interior_miss] = dmax * 1.05
    valid = hit | interior_miss
    h = fill_nearest(-np.nan_to_num(Dv, nan=0.0), valid)
    rows = np.nonzero(valid.any(axis=1))[0]
    cols = np.nonzero(valid.any(axis=0))[0]
    width_px = cols.max() - cols.min() + 1
    k = max(5, int(width_px * close_frac))
    clo = min_filter(max_filter(h, k), k)
    C = np.where(valid, clo - h, 0.0)
    r0, r1 = rows.min(), rows.max()
    z_lo = r0 + band[0] * (r1 - r0)
    z_hi = r0 + band[1] * (r1 - r0)
    rr = np.arange(nv)[:, None] * np.ones((1, nu))
    inband = (rr >= z_lo) & (rr <= z_hi) & valid
    Cb = np.where(inband, C, 0.0)
    cmax = Cb.max()
    if cmax <= 1e-9:
        return [], {"C": C, "clo": clo, "h": h}
    mask = Cb > max(thr_frac * cmax, 1e-6)
    lab, nl = label2d(mask)
    out = []
    for i in range(nl):
        m = lab == i
        area = int(m.sum())
        if area < 3:
            continue
        w = Cb[m]
        jj, ii = np.nonzero(m)
        cj = float((jj * w).sum() / w.sum())
        ci = float((ii * w).sum() / w.sum())
        out.append({"area_px": area, "volume": float(w.sum()) * g["px"] ** 2,
                    "depth_max_m": float(w.max()), "cj": cj, "ci": ci,
                    "u": g["u0"] + (ci + 0.5) * g["px"], "v": g["v0"] + (cj + 0.5) * g["px"],
                    "mask": m})
    out.sort(key=lambda d: -d["volume"])
    return out[:8], {"C": C, "clo": clo, "h": h, "width_px": int(width_px),
                     "height_px": int(r1 - r0 + 1)}


def _best_pair(cands, extra):
    if extra is None or len(cands) < 2:
        return None, 0.0
    W = extra["width_px"]
    H = extra["height_px"]
    best, score = None, 0.0
    for i in range(len(cands)):
        for j in range(i + 1, len(cands)):
            a, b = cands[i], cands[j]
            dx = abs(a["ci"] - b["ci"])
            dz = abs(a["cj"] - b["cj"])
            if dx < 0.15 * W or dx > 0.75 * W or dz > 0.15 * H:
                continue
            sym = min(a["volume"], b["volume"]) / max(a["volume"], b["volume"])
            s = min(a["volume"], b["volume"]) * (1.0 - dz / (0.15 * H)) * (0.5 + 0.5 * sym)
            if s > score:
                best, score = (a, b), s
    return best, score


def detect_eye_sockets(ob, yaws=(-45, -30, -15, 0, 15, 30, 45), n=112, radius_factor=0.22):
    """Find the two eye sockets of a skull-like mesh: the deepest pair of side-by-side front
    concavities in its upper part. For every camera yaw an orthographic depth grid is raycast,
    holes are measured as morphological closing(height) - height, and the best-scoring pair of
    blobs wins (deep, level, apart, similar). Returns dict or None."""
    bvh, co, _tri = bvh_of(ob)
    best = None
    tried = []
    for yaw in yaws:
        g = depth_grid(bvh, co, yaw, n=n)
        cands, extra = _socket_candidates(g)
        pair, score = _best_pair(cands, extra)
        tried.append({"yaw": yaw, "score": score, "candidates": len(cands)})
        if pair is not None and (best is None or score > best[0]):
            best = (score, g, pair, extra)
    if best is None:
        return {"found": False, "tried": tried}
    score, g, pair, extra = best
    eyes = []
    for c in pair:
        m = c["mask"]
        D = g["D"]
        area_m2 = c["area_px"] * g["px"] ** 2
        r_socket = math.sqrt(area_m2 / math.pi)
        r = radius_factor * r_socket
        jj, ii = int(round(c["cj"])), int(round(c["ci"]))
        d_rim = -float(extra["clo"][jj, ii])
        d_bottom = float(np.nanmax(np.where(m, D, np.nan))) if np.isfinite(D[m]).any() else d_rim
        d_hit = float(D[jj, ii]) if np.isfinite(D[jj, ii]) else d_bottom
        depth = d_rim + max(r, 0.5 * (d_bottom - d_rim))
        depth = min(depth, d_hit - 0.3 * r)
        depth = max(depth, d_rim + 0.3 * r)
        pos = g["right"] * c["u"] + g["up"] * c["v"] + g["fwd"] * (g["start"] + depth)
        eyes.append({"position": pos, "radius": r, "socket_radius_m": r_socket,
                     "depth_m": c["depth_max_m"], "rim_to_bottom_m": d_bottom - d_rim,
                     "area_m2": area_m2})
    # character faces the camera: its left eye is on the camera's right (+right)
    eyes.sort(key=lambda e: float(e["position"] @ g["right"]))
    # facing from the eye line (finer than the yaw grid): outward is perpendicular to the line
    # from the right eye to the left eye, horizontal, on the camera side
    line = eyes[1]["position"] - eyes[0]["position"]
    line[2] = 0.0
    out = np.array([line[1], -line[0], 0.0])
    if np.linalg.norm(out) < 1e-9:
        out = -g["fwd"]
    out = out / np.linalg.norm(out)
    if out @ (-g["fwd"]) < 0:
        out = -out
    facing_yaw = math.degrees(math.atan2(out[0], -out[1]))
    return {"found": True, "yaw_deg": g["yaw"], "score": score, "tried": tried,
            "outward": out.tolist(), "facing_yaw_deg": facing_yaw,
            "right": eyes[0], "left": eyes[1]}


def remove_inner_islands(ob, det, max_fraction=0.05, radius_mult=1.5):
    """Delete small loose islands that sit inside the detected eye sockets (a generator often
    models a glowing eye as a ball); the socket then shows its own dark interior. Returns the
    number of removed islands."""
    if not det or not det.get("found"):
        return 0
    me = ob.data
    a = ml.mesh_arrays(me)
    inv, counts = ml.island_labels(me, a)
    nv = len(a["co"])
    kill = np.zeros(nv, dtype=bool)
    removed = 0
    for k in np.nonzero(counts < max_fraction * nv)[0].tolist():
        c = a["co"][inv == k].mean(axis=0)
        for e in (det["left"], det["right"]):
            if np.linalg.norm(c - e["position"]) <= radius_mult * e["socket_radius_m"]:
                kill |= inv == k
                removed += 1
                break
    if removed:
        bm = bmesh.new()
        bm.from_mesh(me)
        bm.verts.ensure_lookup_table()
        bmesh.ops.delete(bm, geom=[bm.verts[i] for i in np.nonzero(kill)[0].tolist()],
                         context="VERTS")
        bm.to_mesh(me)
        bm.free()
        me.update()
    return removed


def socket_depth_at(ob, center, radius, outward, ring=(1.6, 2.4), n_ring=24):
    """Depth (m) of a socket at a known place: ray hit at the centre minus the median hit of a ring
    of rays around it (radius x ring), all cast along -outward. None when the centre ray misses."""
    bvh, co, _tri = bvh_of(ob)
    out = np.asarray(outward, float)
    out = out / np.linalg.norm(out)
    fwd = -out
    e1 = np.cross(fwd, [0.0, 0.0, 1.0])
    if np.linalg.norm(e1) < 1e-6:
        e1 = np.array([1.0, 0.0, 0.0])
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(e1, fwd)
    c = np.asarray(center, float)
    start = c + out * 0.5
    fv = mathutils.Vector(fwd.tolist())

    def hit(o):
        h = bvh.ray_cast(mathutils.Vector(o.tolist()), fv, 2.0)
        return h[3] if h[0] is not None else None

    d0 = hit(start)
    if d0 is None:
        return None
    ds = []
    for k in range(n_ring):
        th = 2 * math.pi * k / n_ring
        for rr in ring:
            o = start + (e1 * math.cos(th) + e2 * math.sin(th)) * radius * rr
            d = hit(o)
            if d is not None:
                ds.append(d)
    if not ds:
        return None
    return float(d0 - np.median(ds))


def socket_rotation(outward):
    """Euler that turns local +Z to `outward` (contract §6: +Z of an fx socket = outward)."""
    q = mathutils.Vector((0, 0, 1)).rotation_difference(mathutils.Vector(list(outward)))
    return q.to_euler()


def emissive_material(name, color, strength):
    mat = bpy.data.materials.new(name)
    mat.use_nodes = True
    bsdf = ml.principled_of(mat)
    col = tuple(float(c) for c in color) + (1.0,)
    bsdf.inputs["Base Color"].default_value = col
    if "Emission Color" in bsdf.inputs:
        bsdf.inputs["Emission Color"].default_value = col
    bsdf.inputs["Emission Strength"].default_value = float(strength)
    bsdf.inputs["Roughness"].default_value = 0.3
    mat.diffuse_color = col
    return mat


def make_sphere(name, center, radius, mat, uv_name="UVMap", segments=16):
    me = bpy.data.meshes.new(name)
    bm = bmesh.new()
    bmesh.ops.create_uvsphere(bm, u_segments=segments, v_segments=max(6, segments // 2),
                              radius=float(radius), calc_uvs=True)
    bmesh.ops.translate(bm, verts=bm.verts, vec=mathutils.Vector([float(x) for x in center]))
    bm.to_mesh(me)
    bm.free()
    if me.uv_layers:
        me.uv_layers[0].name = uv_name
    for poly in me.polygons:
        poly.use_smooth = True
    me.materials.append(mat)
    ob = bpy.data.objects.new(name, me)
    bpy.context.scene.collection.objects.link(ob)
    return ob


# --------------------------------------------------------------------------- front projection

def load_image_px(path):
    """(h, w, 4) float array, TOP row first, values as stored (sRGB-encoded for 8-bit files)."""
    img = bpy.data.images.load(os.path.abspath(path), check_existing=False)
    try:
        w, h = img.size
        px = np.array(img.pixels[:], dtype=np.float32).reshape(h, w, 4)[::-1].copy()
        has_alpha = img.depth in (32, 64, 128) or img.alpha_mode != "NONE"
    finally:
        bpy.data.images.remove(img)
    return px, has_alpha


def subject_mask(px):
    """Alpha mask if the image has real transparency, else key out the border colour."""
    a = px[..., 3]
    if a.min() < 0.98:
        return a > 0.5, "alpha"
    border = np.concatenate([px[0, :, :3], px[-1, :, :3], px[:, 0, :3], px[:, -1, :3]])
    bg = np.median(border, axis=0)
    d = np.abs(px[..., :3] - bg).max(axis=2)
    spread = np.percentile(np.abs(border - bg).max(axis=1), 95)
    return d > max(0.06, 2.5 * spread), "border_key"


def median_subject_color(px, mask):
    """Median colour of the lit subject (dark sockets and saturated glow are skipped); sRGB."""
    rgb = px[..., :3][mask]
    if not len(rgb):
        return np.array([0.7, 0.66, 0.58])
    lum = rgb @ np.array([0.2126, 0.7152, 0.0722])
    sat = rgb.max(axis=1) - rgb.min(axis=1)
    sel = (lum >= np.percentile(lum, 35)) & (sat <= np.percentile(sat, 90))
    return np.median(rgb[sel] if sel.sum() > 20 else rgb, axis=0)


def edge_distance(mask, k):
    """Per pixel: how many 3x3 erosions it survives (0 outside the subject, capped at k)."""
    cur = mask.copy()
    dist = np.zeros(mask.shape, dtype=np.float32)
    for _ in range(int(k)):
        if not cur.any():
            break
        dist += cur
        pad = np.pad(cur, 1, constant_values=False)
        er = cur.copy()
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                er &= pad[1 + dy: 1 + dy + cur.shape[0], 1 + dx: 1 + dx + cur.shape[1]]
        cur = er
    return dist


def edge_band_color(px, mask, dist, k):
    """Median colour of the subject's outer band (dark holes skipped): what the photo shows where
    the projection fades out, so the fill of the hidden faces meets it without a step."""
    band = mask & (dist <= k)
    rgb = px[..., :3][band]
    if len(rgb) < 20:
        return median_subject_color(px, mask)
    lum = rgb @ np.array([0.2126, 0.7152, 0.0722])
    sel = lum >= np.percentile(lum, 25)
    return np.median(rgb[sel], axis=0)


def dilate_colors(px, mask, passes=24):
    """Pad subject colours outward so UVs near the silhouette never sample the background."""
    out = px[..., :3].copy()
    ok = mask.copy()
    for _ in range(passes):
        if ok.all():
            break
        s = np.zeros_like(out)
        c = np.zeros(ok.shape, dtype=np.float32)
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (-1, -1), (1, -1), (-1, 1)):
            so = np.roll(ok, (dy, dx), axis=(0, 1))
            s += np.where(so[..., None], np.roll(out, (dy, dx), axis=(0, 1)), 0.0)
            c += so
        new = (~ok) & (c > 0)
        out[new] = s[new] / c[new][:, None]
        ok |= new
    return out, ok


def find_glow_blobs(px, mask, min_px=6, max_blobs=2):
    """Bright saturated green spots of the subject (glowing eyes in the reference). Returns
    [(col, row, pixels)] of the biggest blobs, greenness-weighted centres, in crop pixels."""
    r, g, b = px[..., 0], px[..., 1], px[..., 2]
    green = (g - np.maximum(r, b))
    m = mask & (green > 0.12) & (g > 0.3)
    if m.sum() < min_px:
        return []
    lab, n = label2d(m)
    out = []
    for i in range(n):
        sel = lab == i
        cnt = int(sel.sum())
        if cnt < min_px:
            continue
        w = green[sel]
        rr, cc = np.nonzero(sel)
        out.append((float((cc * w).sum() / w.sum()), float((rr * w).sum() / w.sum()), cnt))
    out.sort(key=lambda t: -t[2])
    return out[:max_blobs]


def splat_silhouette(xz, mapping, shape):
    """Rasterise projected sample points (x, z) through mapping (a, bx, bz) -> pixel (col, row)
    into a boolean grid, closed with a 3x3 dilate/erode."""
    a, bx, bz = mapping
    col = np.floor(a * xz[:, 0] + bx).astype(np.int64)
    row = np.floor(-a * xz[:, 1] + bz).astype(np.int64)
    h, w = shape
    ok = (col >= 0) & (col < w) & (row >= 0) & (row < h)
    g = np.zeros(h * w, dtype=bool)
    g[row[ok] * w + col[ok]] = True
    g = g.reshape(h, w)
    g = min_filter(max_filter(g.astype(np.float32), 3), 3) > 0.5
    return g


def iou(a, b):
    u = (a | b).sum()
    return float((a & b).sum()) / float(u) if u else 0.0


def fit_projection(xz, mask, iters=40):
    """World (x, z) -> crop pixel similarity (uniform scale, no rotation) that maps the part's
    front silhouette onto the crop's subject mask: start from the top edge + upper width, then a
    pattern search on IoU. mask is the (downsampled) subject mask. Returns ((a, bx, bz), iou)."""
    h, w = mask.shape
    rows = np.nonzero(mask.any(axis=1))[0]
    top_row = rows.min()
    bot_row = rows.max()
    up_rows = mask[top_row: top_row + int(0.6 * (bot_row - top_row + 1))]
    cols = np.nonzero(up_rows.any(axis=0))[0]
    c0, c1 = np.percentile(cols, 1), np.percentile(cols, 99)
    z = xz[:, 1]
    ztop = np.percentile(z, 99.8)
    zbot = np.percentile(z, 0.2)
    up = xz[z >= ztop - 0.6 * (ztop - zbot)]
    x0, x1 = np.percentile(up[:, 0], 1), np.percentile(up[:, 0], 99)
    a = (c1 - c0) / max(x1 - x0, 1e-9)
    bx = (c0 + c1) / 2.0 - a * (x0 + x1) / 2.0
    bz = top_row + a * ztop
    best = (a, bx, bz)
    best_iou = iou(splat_silhouette(xz, best, (h, w)), mask)
    step_s, step_t = 0.04, max(2.0, 0.03 * w)
    for _ in range(iters):
        improved = False
        for ds, dx, dz in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1)):
            a2 = best[0] * (1 + ds * step_s)
            # scale about the current silhouette centre so translation and scale decouple
            cx = (best[0] * np.median(xz[:, 0]) + best[1])
            cz = (-best[0] * np.median(xz[:, 1]) + best[2])
            bx2 = cx - a2 * np.median(xz[:, 0]) + dx * step_t
            bz2 = cz + a2 * np.median(xz[:, 1]) + dz * step_t
            cand = (a2, bx2, bz2)
            v = iou(splat_silhouette(xz, cand, (h, w)), mask)
            if v > best_iou + 1e-6:
                best, best_iou, improved = cand, v, True
        if not improved:
            step_s *= 0.5
            step_t *= 0.5
            if step_t < 0.25 and step_s < 0.002:
                break
    return best, best_iou


def visible_vertices(ob, view_dir=(0.0, 1.0, 0.0), facing_min=0.12):
    """Vertices whose normal faces the camera (camera looks along view_dir) and that are not
    occluded on the way to it. Returns (visible bool array, facing values)."""
    me = ob.data
    co = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    nor = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("normal", nor)
    nor = nor.reshape(-1, 3)
    d = -np.asarray(view_dir, float)
    d = d / np.linalg.norm(d)
    facing = nor @ d
    bvh, _co, _tri = bvh_of(ob)
    lo, hi = co.min(axis=0), co.max(axis=0)
    eps = float(np.linalg.norm(hi - lo)) * 2e-4
    far = float(np.linalg.norm(hi - lo)) * 2
    dv = mathutils.Vector(d.tolist())
    vis = np.zeros(len(co), dtype=bool)
    for i in np.nonzero(facing > facing_min)[0].tolist():
        o = mathutils.Vector(co[i].tolist()) + dv * eps
        hit = bvh.ray_cast(o, dv, far)
        vis[i] = hit[0] is None
    return vis, facing


def project_texture(ob, image_path, crop_px=None, out_png=None, uv_name="UVMap",
                    facing_min=0.12, fit_res=320, name="part", fade_frac=0.05, occlusion=False):
    """Front (orthographic, camera at -Y) projection of a reference crop onto ob.

    Faces whose three vertices face the camera (occlusion=True: and see it unobstructed) get UVs
    into the crop and the material
    "<name>_front_projection"; all other faces get "<name>_back_fill" with the median subject colour.
    The crop is registered to the mesh by its subject silhouette (fit_projection). The fill is
    the median colour of the subject's outer band and the photo fades into it over the outer
    fade_frac of the subject width (those pixels land on grazing faces), so the edge between the
    two slots does not show as a step."""
    px, _alpha = load_image_px(image_path)
    if crop_px:
        l, t, r, b = [int(round(v)) for v in crop_px]
        l, t = max(0, l), max(0, t)
        r, b = min(px.shape[1], r), min(px.shape[0], b)
        if r - l < 8 or b - t < 8:
            raise ValueError("texture.crop_px is empty")
        px = px[t:b, l:r].copy()
    H, W = px.shape[:2]
    mask, mask_mode = subject_mask(px)
    if mask.sum() < 50:
        raise ValueError("texture image has no detectable subject")
    sub_w = float(np.ptp(np.nonzero(mask.any(axis=0))[0]) + 1)
    k_band = max(2, int(round(fade_frac * sub_w)))
    dist = edge_distance(mask, k_band + 1)
    med = edge_band_color(px, mask, dist, k_band) if fade_frac > 0 else \
        median_subject_color(px, mask)
    # registration on a downsampled mask
    f = max(1, int(math.ceil(max(H, W) / float(fit_res))))
    small = ml.downscale(mask[..., None].astype(np.float32), f)[..., 0] > 0.5
    co, _tri = tri_arrays(ob)
    # dense area-weighted samples: big cranium triangles must not leave holes in the silhouette
    n_pix = int(small.sum())
    pts = np.concatenate([co, sample_surface(ob, max(200000, 12 * n_pix), seed=3)])
    xz = pts[:, [0, 2]]
    m_small, fit_iou = fit_projection(xz, small)
    a, bx, bz = m_small[0] * f, m_small[1] * f, m_small[2] * f
    # texture: subject colours padded outward, background never sampled
    rgb, _ = dilate_colors(px, mask, passes=40)
    if fade_frac > 0:
        # grazing faces map to the outer band of the subject: fade the photo there to the fill
        w = np.clip(dist / float(k_band), 0.0, 1.0)
        w = (w * w * (3 - 2 * w))[..., None]
        rgb = rgb * w + np.asarray(med, dtype=np.float32)[None, None, :] * (1 - w)
    tex = np.ones((H, W, 4), dtype=np.float32)
    tex[..., :3] = rgb
    img = bpy.data.images.new(name + "_projection", width=W, height=H, alpha=False)
    img.pixels = np.ascontiguousarray(tex[::-1]).ravel()
    if out_png:
        img.filepath_raw = os.path.abspath(out_png)
        img.file_format = "PNG"
        img.save()
    img.pack()
    me = ob.data
    while me.uv_layers:
        me.uv_layers.remove(me.uv_layers[0])
    uvl = me.uv_layers.new(name=uv_name)
    if occlusion:
        vis, facing = visible_vertices(ob, facing_min=facing_min)
    else:  # front-facing is enough: an occluded fold takes the occluder's colour, not the fill
        nor = np.empty(len(me.vertices) * 3)
        me.vertices.foreach_get("normal", nor)
        facing = -nor.reshape(-1, 3)[:, 1]
        vis = facing > facing_min
    vco = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", vco)
    vco = vco.reshape(-1, 3)
    pcol = a * vco[:, 0] + bx
    prow = -a * vco[:, 2] + bz
    vu = pcol / W
    vv = 1.0 - prow / H
    nl = len(me.loops)
    lv = np.empty(nl, dtype=np.int64)
    me.loops.foreach_get("vertex_index", lv)
    nf = len(me.polygons)
    ls = np.empty(nf, dtype=np.int64)
    lt = np.empty(nf, dtype=np.int64)
    me.polygons.foreach_get("loop_start", ls)
    me.polygons.foreach_get("loop_total", lt)
    loop_face = np.repeat(np.arange(nf), lt)
    face_vis = np.ones(nf, dtype=bool)
    np.logical_and.at(face_vis, loop_face, vis[lv])
    # back faces: a UV inside the padded subject colour (any point works, colour is flat anyway)
    uv = np.stack([vu[lv], vv[lv]], axis=1)
    uvl.data.foreach_set("uv", uv.ravel())
    front = bpy.data.materials.new(name + "_front_projection")
    front.use_nodes = True
    nt = front.node_tree
    bsdf = ml.principled_of(front)
    node = nt.nodes.new("ShaderNodeTexImage")
    node.image = img
    node.extension = "EXTEND"
    nt.links.new(node.outputs["Color"], bsdf.inputs["Base Color"])
    nt.nodes.active = node
    bsdf.inputs["Roughness"].default_value = 0.75
    med_lin = srgb_to_linear(med)
    back = bpy.data.materials.new(name + "_back_fill")
    back.use_nodes = True
    bb = ml.principled_of(back)
    bb.inputs["Base Color"].default_value = tuple(med_lin.tolist()) + (1.0,)
    bb.inputs["Roughness"].default_value = 0.75
    back.diffuse_color = tuple(med_lin.tolist()) + (1.0,)
    me.materials.clear()
    me.materials.append(front)
    me.materials.append(back)
    mi = np.where(face_vis, 0, 1).astype(np.int64)
    me.polygons.foreach_set("material_index", mi)
    me.update()
    return {"mapping_px": [round(float(a), 4), round(float(bx), 3), round(float(bz), 3)],
            "silhouette_iou": round(fit_iou, 4), "mask": mask_mode, "image_size": [W, H],
            "front_faces": int(face_vis.sum()), "back_faces": int((~face_vis).sum()),
            "front_fraction": round(float(face_vis.mean()), 4),
            "visible_vertices": int(vis.sum()),
            "fill_color_srgb": [round(float(c), 4) for c in med], "fade_px": k_band,
            "image": img.name, "occlusion_test": bool(occlusion),
            "glow_blobs_px": [[round(c, 1), round(r, 1), n] for c, r, n in
                              find_glow_blobs(px, mask)]}, (a, bx, bz), px, mask


def reference_in_frame(px, mask, mapping, lo, hi, size, margin):
    """The crop resampled into the render_sheet front frame of bbox (lo, hi): (size, size, 4)
    straight-alpha array, bottom row first (like render tiles)."""
    a, bx, bz = mapping
    centre = (np.asarray(lo) + np.asarray(hi)) / 2.0
    extent = max(float(np.linalg.norm(np.asarray(hi) - np.asarray(lo))), 1e-3)
    os_ = extent * margin
    j = np.arange(size)
    xs = centre[0] - os_ / 2 + (j + 0.5) / size * os_
    zs = centre[2] - os_ / 2 + (j + 0.5) / size * os_   # bottom row first
    X, Z = np.meshgrid(xs, zs)
    col = np.floor(a * X + bx).astype(np.int64)
    row = np.floor(-a * Z + bz).astype(np.int64)
    H, W = mask.shape
    ok = (col >= 0) & (col < W) & (row >= 0) & (row < H)
    out = np.zeros((size, size, 4), dtype=np.float32)
    rc, cc = row[ok], col[ok]
    out[ok, :3] = px[rc, cc, :3]
    out[ok, 3] = mask[rc, cc].astype(np.float32)
    return out


def frame_rows_above(lo, hi, size, margin, plane_z):
    """Boolean (size,) rows (bottom first) of the front frame whose world z is above plane_z."""
    centre = (np.asarray(lo) + np.asarray(hi)) / 2.0
    extent = max(float(np.linalg.norm(np.asarray(hi) - np.asarray(lo))), 1e-3)
    os_ = extent * margin
    zs = centre[2] - os_ / 2 + (np.arange(size) + 0.5) / size * os_
    return zs > plane_z


def tile_compare(ref, tile, rows):
    """Silhouette IoU and colour MAE of a render tile vs the reference, rows above the cut only.
    Colour is compared in display space (render tiles are display-referred PNG values)."""
    a = ref[rows]
    b = tile[rows]
    ma, mb = a[..., 3] > 0.5, b[..., 3] > 0.5
    inter = ma & mb
    out = {"iou": round(iou(ma, mb), 4)}
    out["color_mae"] = round(float(np.abs(a[..., :3][inter] - b[..., :3][inter]).mean()), 4) \
        if inter.any() else None
    return out


# --------------------------------------------------------------------------- renders

def render_dirs(path, objects, dirs, size=384, color_type="TEXTURE", bbox=None, margin=1.0,
                cavity=True, clip=None):
    """Workbench orthographic tiles of `objects` from arbitrary camera directions (vector from
    the target to the camera, Z up), framed on bbox exactly like _mesh_lib.render_sheet (same
    centre / ortho scale), so tiles of different objects line up. clip (m): only geometry within
    this distance of the bbox centre along the view is drawn (a staff between the camera and
    the head is cut away). Returns the tiles
    ((size, size, 4), bottom row first)."""
    scene = bpy.context.scene
    bpy.context.view_layer.update()
    if color_type == "TEXTURE":
        ml.set_active_basecolor_nodes(objects)
    synced = ml._sync_viewport_colors(objects) if color_type in ("TEXTURE", "MATERIAL") else []
    lo, hi = bbox if bbox is not None else af.world_bbox(objects)
    lo, hi = mathutils.Vector(list(lo)), mathutils.Vector(list(hi))
    centre = (lo + hi) / 2
    extent = max((hi - lo).length, 1e-3)
    cam_data = bpy.data.cameras.new("af_rp_cam")
    cam_data.type = "ORTHO"
    cam_data.ortho_scale = extent * margin
    cam_data.clip_end = extent * 40
    cam = bpy.data.objects.new("af_rp_cam", cam_data)
    scene.collection.objects.link(cam)
    prev_cam = scene.camera
    scene.camera = cam
    keep = set(objects)
    hidden = []
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
    sh.single_color = (0.8, 0.8, 0.8)
    sh.show_cavity = bool(cavity)
    sh.show_object_outline = False
    tmp_dir = os.path.join(os.path.dirname(os.path.abspath(path)), "_rp_views")
    os.makedirs(tmp_dir, exist_ok=True)
    tiles = []
    try:
        for k, d in enumerate(dirs):
            dv = mathutils.Vector([float(x) for x in d]).normalized()
            cam.location = centre + dv * extent * 6
            if clip:
                cam_data.clip_start = max(1e-3, extent * 6 - float(clip))
                cam_data.clip_end = extent * 6 + float(clip)
            look = (centre - cam.location).normalized()
            cam.rotation_euler = look.to_track_quat("-Z", "Y").to_euler()
            one = os.path.join(tmp_dir, "v%02d.png" % k)
            r.filepath = one
            bpy.ops.render.render(write_still=True)
            tiles.append(ml.load_png(one))
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
    return tiles


def rot_z(v, deg):
    th = math.radians(deg)
    c, s = math.cos(th), math.sin(th)
    v = np.asarray(v, float)
    return np.array([c * v[0] - s * v[1], s * v[0] + c * v[1], v[2]])


def grid_sheet(rows, gap=8, bg=(0.82, 0.82, 0.84)):
    """Rows of tiles (lists of (s, s, 4) arrays, bottom row first) -> one flattened image."""
    out_rows = []
    for row in rows:
        parts = []
        for i, t in enumerate(row):
            parts.append(ml.flatten(t, bg))
            if i < len(row) - 1:
                g = np.ones((t.shape[0], gap, 4), dtype=np.float32)
                g[..., :3] = bg
                parts.append(g)
        out_rows.append(np.concatenate(parts, axis=1))
    w = max(r.shape[1] for r in out_rows)
    stack = []
    for i, r in enumerate(out_rows):
        if r.shape[1] < w:
            pad = np.ones((r.shape[0], w - r.shape[1], 4), dtype=np.float32)
            pad[..., :3] = bg
            r = np.concatenate([r, pad], axis=1)
        stack.append(r)
        if i < len(out_rows) - 1:
            g = np.ones((gap, w, 4), dtype=np.float32)
            g[..., :3] = bg
            stack.append(g)
    return np.concatenate(stack[::-1], axis=0)  # bottom row first: last row at the bottom


def snap_boundary_to_plane(ob, plane, offset, max_dist):
    """Put open-boundary vertices that drifted off the cut plane (decimation) back onto it."""
    p, n = plane
    p2 = p + n * float(offset)
    me = ob.data
    a = ml.mesh_arrays(me)
    efc = ml.edge_face_counts(a)
    vs = np.unique(a["ev"][efc == 1].ravel())
    if not len(vs):
        return 0
    co = a["co"]
    d = (co[vs] - p2) @ n
    m = np.abs(d) <= max_dist
    if not m.any():
        return 0
    co[vs[m]] -= d[m][:, None] * n[None, :]
    me.vertices.foreach_set("co", co.ravel())
    me.update()
    return int(m.sum())


def transform_mesh(ob, sim, inverse=False):
    """Apply a similarity to the mesh data (object transforms stay identity)."""
    M = sim_matrix(sim)
    if inverse:
        M = M.inverted()
    ob.data.transform(M)
    ob.data.update()
