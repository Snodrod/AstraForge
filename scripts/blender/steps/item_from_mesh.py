"""item_from_mesh — turn a generated (Hunyuan / Tripo / Meshy) item mesh into a contract item.

Request:
  inputs.mesh       .glb / .gltf / .fbx / .obj / .blend with the item (one or several mesh objects)
  params.kind       blade | axe | blunt | polearm | staff | wand | torch | shield | auto (default auto:
                    taken from params.type or guessed from proportions)
  params.type       optional kit type name (sword, battle_axe, staff, shield_kite, ...) -> kind, budget
  params.length_m   target length of the main axis in metres (default per kind)
  params.budget_tris  default = module budget: 2500 (one-handed) / 4000 (two-handed, staff, shield); a
                    larger value is clamped to it unless params.allow_over_budget (then a warning check)
  params.crop       optional: {"min": [x,y,z], "max": [x,y,z]} box or
                    {"segment": [[x,y,z],[x,y,z]], "radius": r} capsule (input coordinates) to cut a prop
                    out of a fused generation (e.g. a staff held by a generated character)
  params.min_island 0.005: loose parts with fewer vertices than this fraction are deleted
  params.grip       auto | {"point": [x,y,z]} (input coordinates) | {"t": 0.55} (fraction of the length
                    from the grip end)
  params.tip_end    auto | "min" | "max": which end of the principal axis is the business end
  params.edge       auto | [x,y,z]: cutting-edge / face direction in input coordinates
  params.texture_max  1024: larger textures are downscaled (Blender Image.scale, no PIL)
  params.origin     bbox | axis (contract §4)
Outputs: item.glb, item.blend, item.json, preview.png (3 views), profile.png (section profile).

Grip rules (length_m > 1.15 m makes blade / axe / blunt / polearm / staff two-handed and adds
socket_grip_off): blades — the round handle between guard and pommel is found from the section profile
(refined on the vertices); one-handed main = handle midpoint, two-handed main = 5 cm under the guard,
off = 11 cm lower (>= 3 cm above the pommel, never closer than 9 cm to main). Axe / blunt — one-handed:
thinnest round window near the butt; two-handed: main 0.41, off 0.115 of the length from the butt
(as item_kit). Polearm / staff / wand / torch — fraction of the length (GRIP_T), off hand 0.14 of the
length from the butt (polearm) or 0.14 below main (staff).

Honest scope: these are geometric guesses, reported in item.json with the profile (profile.png).
Guns, bows and other non-axial items need params.grip (point) and params.edge.
"""
import json
import math
import os

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector

import af_common as af
from steps import _items_lib as L

KIND_OF_TYPE = {"sword": "blade", "greatsword": "blade", "dagger": "blade", "axe": "axe", "battle_axe": "axe",
                "halberd": "polearm", "spear": "polearm", "mace": "blunt", "warhammer": "blunt", "staff": "staff",
                "wand": "wand", "torch": "torch", "shield_round": "shield", "shield_kite": "shield",
                "shield_tower": "shield", "buckler": "shield"}
DEFAULT_LENGTH = {"blade": 0.95, "axe": 0.7, "blunt": 0.62, "polearm": 2.0, "staff": 1.75, "wand": 0.36,
                  "torch": 0.55, "shield": 0.9}
GRIP_T = {"polearm": 0.42, "staff": 0.56, "wand": 0.17, "torch": 0.22}
TYPE_T = {"spear": (0.47, 0.23), "halberd": (0.42, 0.12)}  # (main, off) of known kit types, fraction from the butt


def join_all(objs):
    meshes = af.meshes(objs)
    for o in meshes:
        mw = o.matrix_world.copy()
        o.parent = None
        o.matrix_world = mw
    bpy.context.view_layer.update()
    af.apply_transforms(meshes)
    ob = af.join(meshes, name="item_source")
    for o in list(objs):
        try:
            if o is not ob and o.type != "MESH":
                bpy.data.objects.remove(o)
        except ReferenceError:
            pass
    # glTF / FBX split vertices at UV seams and hard edges: weld them so that connectivity (loose-part
    # cleanup) and decimation see one surface; UVs live on loops and survive the weld
    lo, hi = af.world_bbox([ob])
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    n0 = len(bm.verts)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=max(1e-6, 1e-5 * (hi - lo).length))
    welded = n0 - len(bm.verts)
    bm.to_mesh(ob.data)
    bm.free()
    ob.data.update()
    ob["af_welded_vertices"] = welded
    return ob


def crop_mesh(ob, crop):
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    kill = []
    if "segment" in crop:
        a, b = Vector(crop["segment"][0]), Vector(crop["segment"][1])
        r = float(crop.get("radius", 0.1))
        ab = b - a
        for v in bm.verts:
            t = L.clamp((v.co - a).dot(ab) / ab.length_squared, 0.0, 1.0)
            if (v.co - (a + ab * t)).length > r:
                kill.append(v)
    else:
        lo, hi = Vector(crop["min"]), Vector(crop["max"])
        for v in bm.verts:
            if not all(lo[i] <= v.co[i] <= hi[i] for i in range(3)):
                kill.append(v)
    bmesh.ops.delete(bm, geom=kill, context="VERTS")
    bm.to_mesh(ob.data)
    bm.free()
    ob.data.update()
    return len(kill)


def remove_small_islands(ob, min_frac):
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    bm.verts.ensure_lookup_table()
    seen = set()
    islands = []
    for v in bm.verts:
        if v.index in seen:
            continue
        stack, comp = [v], []
        seen.add(v.index)
        while stack:
            x = stack.pop()
            comp.append(x)
            for e in x.link_edges:
                y = e.other_vert(x)
                if y.index not in seen:
                    seen.add(y.index)
                    stack.append(y)
        islands.append(comp)
    total = len(bm.verts)
    kill = [v for comp in islands if len(comp) < min_frac * total for v in comp]
    bmesh.ops.delete(bm, geom=kill, context="VERTS")
    bm.to_mesh(ob.data)
    bm.free()
    ob.data.update()
    return {"islands": len(islands), "removed_islands": sum(1 for c in islands if len(c) < min_frac * total),
            "removed_vertices": len(kill)}


def section_profile(P, c, axes, n_bins=60):
    a1, a2, a3 = axes[:, 0], axes[:, 1], axes[:, 2]
    Q = P - c
    t, u, w = Q @ a1, Q @ a2, Q @ a3
    t0, t1 = float(t.min()), float(t.max())
    edges = np.linspace(t0, t1, n_bins + 1)
    idx = np.clip(((t - t0) / (t1 - t0) * n_bins).astype(int), 0, n_bins - 1)
    prof = []
    for b in range(n_bins):
        m = idx == b
        if m.sum() < 3:
            prof.append(None)
            continue
        uu, ww = u[m], w[m]
        uc, wc = 0.5 * (uu.min() + uu.max()), 0.5 * (ww.min() + ww.max())
        eu, ew = uu.max() - uu.min(), ww.max() - ww.min()
        r = float(np.percentile(np.hypot(uu - uc, ww - wc), 98))
        prof.append({"t": float(0.5 * (edges[b] + edges[b + 1])), "r": r, "eu": float(eu), "ew": float(ew),
                     "aspect": float(max(eu, ew) / max(1e-6, min(eu, ew))), "uc": float(uc), "wc": float(wc),
                     "u_min": float(uu.min()), "u_max": float(uu.max()), "n": int(m.sum())})
    # fill gaps
    for i in range(n_bins):
        if prof[i] is None:
            j = next((k for k in range(1, n_bins) if 0 <= i - k < n_bins and prof[i - k] or
                      0 <= i + k < n_bins and prof[i + k]), None)
            src = prof[i - j] if (i - j >= 0 and prof[i - j]) else prof[min(n_bins - 1, i + j)]
            prof[i] = dict(src, t=float(0.5 * (edges[i] + edges[i + 1])), n=0)
    return prof, (t0, t1)


def guess_kind(prof, L_in):
    rs = np.array([p["r"] for p in prof])
    slender = L_in / max(1e-6, 2 * rs.max())
    flat = np.median([p["aspect"] for p in prof])
    if slender < 2.5:
        return "shield"
    if flat > 3.0:
        return "blade"
    if slender > 12:
        return "staff"
    return "axe"


# two-handed hand spacing (metres): main hand just below the guard / at ~0.41 of a long haft, off hand
# at least one hand width below it (item_kit: greatsword main 5.5 cm under the guard, off 11 cm lower)
HAND_W = 0.09
OFF_GAP = 0.11
TWO_HANDED_T = {"axe": (0.41, 0.115), "blunt": (0.41, 0.115)}   # (main, off) fraction of the length from the butt
OFF_T = {"polearm": 0.14}                                        # off hand, fraction from the butt
OFF_BELOW = {"staff": 0.14}                                      # off hand, fraction of the length below main


def grip_region(prof, c0, c1, r_ref, max_aspect=2.4, k_r=1.45):
    """Contiguous bins around [c0, c1] whose section stays round and close to r_ref (the handle
    between a wide guard / head and a pommel / butt)."""
    n = len(prof)
    thr = k_r * r_ref
    ok = lambda i: prof[i]["r"] <= thr and prof[i]["aspect"] < max_aspect
    lo, hi = max(0, c0), min(n - 1, c1)
    while lo - 1 >= 0 and ok(lo - 1):
        lo -= 1
    while hi + 1 < n and ok(hi + 1):
        hi += 1
    return lo, hi, thr


def handle_run(vt, vu, vw, t_from, t_to, s, uc, wc, bin_m=0.006, near_m=0.045, max_aspect=1.8, tol=0.15):
    """Longest run of round fine bins (bin_m) whose radius stays within tol of the handle radius (the
    most common round radius) between t_from (grip end) and t_to, using only vertices within near_m of
    the handle axis (uc, wc). Empty bins (coarse source meshes) do not break a run. Returns
    (t_start, t_end, r, bins) in source units, t_start on the t_from side, or None."""
    nb = max(4, int(abs(t_to - t_from) * s / bin_m))
    rad = np.hypot(vu - uc, vw - wc)
    lo_t, hi_t = min(t_from, t_to), max(t_from, t_to)
    sel = (rad < near_m / s) & (vt >= lo_t) & (vt <= hi_t)
    if sel.sum() < 12:
        return None
    tt, rr, uu, ww = vt[sel], rad[sel], vu[sel], vw[sel]
    k = np.clip(((tt - t_from) / (t_to - t_from) * nb).astype(int), 0, nb - 1)
    r = np.full(nb, np.nan)
    a = np.full(nb, np.nan)
    for b in range(nb):
        mb = k == b
        if mb.sum() >= 4:
            r[b] = float(np.percentile(rr[mb], 95))
            eu, ew = float(np.ptp(uu[mb])), float(np.ptp(ww[mb]))
            a[b] = max(eu, ew) / max(1e-9, min(eu, ew))
    rnd = np.isfinite(r) & (a < max_aspect)
    if rnd.sum() < 3:
        return None
    vals = r[rnd]
    # handle radius = the most common round radius (a running median would drift along a tapering pommel)
    counts = [int(np.sum(np.abs(vals - v) <= 0.08 * v)) for v in vals]
    r_h = float(np.median(vals[np.abs(vals - vals[int(np.argmax(counts))]) <= 0.08 * vals[int(np.argmax(counts))]]))
    ok = rnd & (np.abs(r - r_h) <= tol * r_h)
    best = None
    b = 0
    while b < nb:
        if not ok[b]:
            b += 1
            continue
        start = last = b
        j = b + 1
        while j < nb and (ok[j] or not np.isfinite(r[j])):
            if ok[j]:
                last = j
            j += 1
        if best is None or last - start > best[1] - best[0]:
            best = (start, last)
        b = last + 1
    if best is None or best[1] - best[0] < 2:
        return None
    step = (t_to - t_from) / nb
    return t_from + step * best[0], t_from + step * (best[1] + 1), r_h, best[1] - best[0] + 1


def find_grip(kind, prof, hand_bins, params, t_range, s, two_handed, verts=None):
    """Main (and off) hand positions along the principal axis, in source units.
    verts: optional (t, u, w) arrays of the vertices (PCA coordinates) for sub-bin refinement of the
    handle ends. Returns (grip_end, t_main, t_off or None, info)."""
    n = len(prof)
    t0, t1 = t_range
    bw = (t1 - t0) / n
    tb = lambda b: t0 + bw * b
    rs = np.array([p["r"] for p in prof])
    asp = np.array([p["aspect"] for p in prof])
    info = {}
    tip_end = params.get("tip_end", "auto")
    win = max(2, int(round(hand_bins)))
    m = 1.0 / s  # one metre in source units
    t_off = None

    def window_scores(lo, hi, max_aspect=None):
        """Hand-wide windows scored by mean radius (flat sections penalised). max_aspect: skip windows
        that contain a flat bin (a blade tip is thin but flat, a handle is round)."""
        out = []
        for st in range(max(0, lo), min(n - win, hi) + 1):
            if max_aspect is not None and asp[st:st + win].max() > max_aspect:
                continue
            r = rs[st:st + win].mean()
            a = asp[st:st + win].mean()
            out.append((r * (1 + 0.6 * max(0.0, a - 1.8)), st + win / 2.0, st))
        return out
    g_t = params.get("grip", {}).get("t") if isinstance(params.get("grip"), dict) else None
    if kind == "blade":
        lim = int(0.42 * n)
        cand_lo = window_scores(0, lim - win, 2.6)
        cand_hi = window_scores(n - lim, n - win, 2.6)
        if not cand_lo and not cand_hi:  # no round section anywhere (odd mesh): fall back to all windows
            cand_lo, cand_hi = window_scores(0, lim - win), window_scores(n - lim, n - win)
        best_lo = min(cand_lo) if cand_lo else (1e9, 0, 0)
        best_hi = min(cand_hi) if cand_hi else (1e9, n, n - win)
        if tip_end == "max" or (tip_end == "auto" and best_lo[0] <= best_hi[0]):
            grip_end, best = "min", best_lo
        else:
            grip_end, best = "max", best_hi
        info["scores"] = {"min_end": round(best_lo[0], 5), "max_end": round(best_hi[0], 5)}
        d = 1.0 if grip_end == "min" else -1.0          # +t direction toward the business end
        # handle = the longest round run of near-constant radius in the grip half, measured on the
        # vertices near the axis (fine 6 mm bins; far quillon tips ignored). A spike / teardrop pommel,
        # a peen or a ricasso collar is round too, but short or of another radius
        t_end = t0 if d > 0 else t1
        t_mid = 0.5 * (t0 + t1)
        grip_half = [i for i in range(n) if (prof[i]["t"] - t_mid) * d < 0 and prof[i]["aspect"] < 2.4]
        run = None
        if verts is not None and grip_half:
            vt, vu, vw = verts
            uc = float(np.median([prof[i]["uc"] for i in grip_half]))
            wc = float(np.median([prof[i]["wc"] for i in grip_half]))
            run = handle_run(vt, vu, vw, t_end, t_mid, s, uc, wc)
        if run is not None:
            t_lo, t_hi = sorted((run[0], run[1]))
            info["handle_rule"] = "longest_constant_round_run"
            info["handle_r_m"] = round(run[2] * s, 4)
        else:
            st = int(best[2])
            r_ref = float(np.median(rs[st:st + win]))
            lo_b, hi_b, _ = grip_region(prof, st, st + win - 1, r_ref)
            t_lo, t_hi = tb(lo_b), tb(hi_b + 1)
            info["handle_rule"] = "thinnest_window_region"
        t_guard, t_pommel = (t_hi, t_lo) if d > 0 else (t_lo, t_hi)
        length_m = abs(t_hi - t_lo) * s
        info["handle_m"] = round(length_m, 4)
        if g_t is not None:
            t_main = (t0 if d > 0 else t1) + d * g_t * (t1 - t0)
            info["rule"] = "params.grip.t"
        elif two_handed and length_m >= HAND_W + 0.04:
            t_main = t_guard - d * 0.05 * m
            info["rule"] = "two_handed: 5 cm under the guard"
        else:
            t_main = 0.5 * (t_lo + t_hi)
            info["rule"] = "handle_midpoint"
        if two_handed:
            t_off = t_main - d * OFF_GAP * m
            if d * (t_off - t_pommel) < 0.03 * m:          # stay on the handle (3 cm above the pommel)
                t_off = t_pommel + d * 0.03 * m
            if d * (t_main - t_off) < HAND_W * m:          # never closer than one hand width
                t_off = t_main - d * HAND_W * m
                info["off_hand_beyond_handle"] = True
        info["handle_t"] = [round(t_lo, 5), round(t_hi, 5)]
    else:
        k = max(2, int(0.3 * n))
        head_lo, head_hi = rs[:k].max(), rs[-k:].max()
        if tip_end == "min":
            grip_end = "max"
        elif tip_end == "max":
            grip_end = "min"
        else:
            grip_end = "min" if head_hi >= head_lo else "max"
        d = 1.0 if grip_end == "min" else -1.0
        t_butt = t0 if d > 0 else t1
        Lt = t1 - t0
        info["head_r"] = {"min_end": round(float(head_lo), 5), "max_end": round(float(head_hi), 5)}
        if g_t is not None:
            t_main = t_butt + d * g_t * Lt
            info["rule"] = "params.grip.t"
        elif params.get("type") in TYPE_T:  # item_kit proportions of a known type
            t_main = t_butt + d * TYPE_T[params["type"]][0] * Lt
            info["rule"] = "type_fraction %.2f (%s)" % (TYPE_T[params["type"]][0], params["type"])
        elif kind in GRIP_T:
            t_main = t_butt + d * GRIP_T[kind] * Lt
            info["rule"] = "length_fraction %.2f" % GRIP_T[kind]
        elif two_handed and kind in TWO_HANDED_T:
            t_main = t_butt + d * TWO_HANDED_T[kind][0] * Lt
            info["rule"] = "two_handed: %.2f of the length from the butt" % TWO_HANDED_T[kind][0]
        else:  # axe / blunt: thinnest round window in the outer 4..26 % at the grip end (hand near the butt)
            if grip_end == "min":
                cand = window_scores(int(0.04 * n), int(0.26 * n) - win + 1)
            else:
                cand = window_scores(int(0.74 * n), int(0.96 * n) - win)
            centre = min(cand)[1] if cand else (0.12 * n if grip_end == "min" else 0.88 * n)
            t_main = tb(centre)
            info["rule"] = "thinnest_round_window"
        if two_handed:
            if params.get("type") in TYPE_T:
                t_off = t_butt + d * TYPE_T[params["type"]][1] * Lt
            elif kind in TWO_HANDED_T:
                t_off = t_butt + d * TWO_HANDED_T[kind][1] * Lt
            elif kind in OFF_T:
                t_off = t_butt + d * OFF_T[kind] * Lt
            else:
                t_off = t_main - d * OFF_BELOW.get(kind, 0.14) * Lt
            if d * (t_main - t_off) < 0.10 * m:
                t_off = t_main - d * 0.10 * m
            if d * (t_off - t_butt) < 0.03 * m:
                t_off = t_butt + d * 0.03 * m
    info["grip_end"] = grip_end
    info["grip_bin"] = round(float((t_main - t0) / bw), 2)
    if t_off is not None:
        info["off_bin"] = round(float((t_off - t0) / bw), 2)
    return grip_end, t_main, t_off, info


def run(req):
    inp, prm = req["inputs"], dict(req["params"])
    if "budget" in prm and "budget_tris" not in prm:  # server shape
        prm["budget_tris"] = prm.pop("budget")
    if isinstance(prm.get("grip"), (list, tuple)) and len(prm["grip"]) == 3:
        prm["grip"] = {"point": [float(x) for x in prm["grip"]]}
    af.reset_scene()
    objs = af.import_any(inp["mesh"])
    if not af.meshes(objs):
        raise ValueError("no mesh in %s" % inp["mesh"])
    ob = join_all(objs)
    tris_in = af.tri_count([ob])
    warnings, checks = [], []
    report = {"source": inp["mesh"], "tris_source": tris_in, "welded_vertices": int(ob.get("af_welded_vertices", 0))}
    if prm.get("crop"):
        report["crop_removed_vertices"] = crop_mesh(ob, prm["crop"])
    report["cleanup"] = remove_small_islands(ob, float(prm.get("min_island", 0.005)))
    if len(ob.data.vertices) < 20:
        raise ValueError("nothing left after crop/cleanup")
    co = np.array([v.co[:] for v in ob.data.vertices])
    # area-weighted triangle centroids for PCA
    co_w, tri, _ = L.mesh_arrays(ob, world=False, evaluated=False)
    A, B, C = co_w[tri[:, 0]], co_w[tri[:, 1]], co_w[tri[:, 2]]
    area = 0.5 * np.linalg.norm(np.cross(B - A, C - A), axis=1)
    axes, centre, mn, mx = L.pca_axes((A + B + C) / 3.0, area + 1e-12)
    prof, (t0, t1) = section_profile(co, centre, axes)
    L_in = t1 - t0
    kind = prm.get("kind", "auto")
    if kind == "auto":
        kind = KIND_OF_TYPE.get(prm.get("type", ""), None) or guess_kind(prof, L_in)
    report["kind"] = kind
    L_out = float(prm.get("length_m", DEFAULT_LENGTH.get(kind, 1.0)))
    s = L_out / L_in
    two = L_out > 1.15 and kind in ("blade", "axe", "blunt", "polearm", "staff")
    a1, a2, a3 = Vector(axes[:, 0]), Vector(axes[:, 1]), Vector(axes[:, 2])
    grip_auto = True
    off_dz = None  # socket_grip_off along +Z from socket_grip_main (metres), two-handed items
    if kind == "shield":
        # face = thinnest axis toward the convex side (rim behind the centre); up = wider end
        # convex side: quadratic fit w = c0 + c1 t + c2 u + c3 t^2 + c4 u^2 over the area-weighted
        # triangle centroids; the face bulges toward the side where w decreases with r^2. Flat boards
        # fall back to the side facing the source camera (-Y in Blender, like the generators' output).
        Qc = (A + B + C) / 3.0 - centre
        tq, uq, wq = Qc @ np.array(a1), Qc @ np.array(a2), Qc @ np.array(a3)
        Xd = np.stack([np.ones_like(tq), tq, uq, tq * tq, uq * uq], axis=1)
        sw = np.sqrt(area + 1e-12)
        coef = np.linalg.lstsq(Xd * sw[:, None], wq * sw, rcond=None)[0]
        curv = float(coef[3] + coef[4])
        sag = abs(curv) * (0.5 * L_in) ** 2
        if sag > 0.01 * L_in:
            face = -a3 if curv > 0 else a3
            face_method = "curvature"
        else:
            face = a3 if a3.y < 0 else -a3
            face_method = "flat_board_faces_source_camera"
        rs = [p["r"] for p in prof]
        k = max(2, len(rs) // 3)
        up = a1 if max(rs[-k:]) >= max(rs[:k]) else -a1
        if abs(max(rs[-k:]) - max(rs[:k])) / max(rs) < 0.05:  # symmetric: keep the source's up
            up = a1 if a1.z >= 0 else -a1
        Z, Y = up, -face
        # board back plane: rays from behind toward the face at 8 points on a ring around the centre
        # (the exact centre often carries a handle), median first hit; handle axis 3.5 cm behind it
        from mathutils.bvhtree import BVHTree
        bvh = BVHTree.FromPolygons(co_w.tolist(), tri.tolist(), all_triangles=True)
        hits = []
        for k in range(8):
            ang = 2 * math.pi * k / 8
            off = (a1 * math.cos(ang) + a2 * math.sin(ang)) * (0.12 * L_in)
            o = Vector(centre) + off - face * L_in
            h = bvh.ray_cast(o, face, 2 * L_in)
            if h[0] is not None:
                hits.append((h[0] - Vector(centre)).dot(-face))
        back = float(np.median(hits)) if hits else float(np.percentile((co - centre) @ np.array(-face), 70))
        grip_pt = Vector(centre) + (-face) * (back + 0.035 / s)
        report["face"] = {"method": face_method, "sag_m": round(sag * s, 4)}
        report["grip"] = {"method": "shield_back_centre", "standoff_m": 0.035, "back_hits": len(hits),
                          "back_offsets_m": [round(h * s, 4) for h in hits], "back_m": round(back * s, 4)}
    else:
        hand_bins = 0.09 / s / (L_in / len(prof))
        Qv = co - centre
        grip_end, t_g, t_off, ginfo = find_grip(kind, prof, hand_bins, prm, (t0, t1), s, two,
                                                verts=(Qv @ axes[:, 0], Qv @ axes[:, 1], Qv @ axes[:, 2]))
        report["grip"] = dict(ginfo, method="section_profile", hand_window_bins=round(hand_bins, 2))
        if t_off is not None:
            off_dz = (t_off - t_g) * s * (1.0 if grip_end == "min" else -1.0)
        # shaft centre-line: weighted line fit through the section centres on the grip side (the PCA
        # axis is tilted by an off-axis head, the haft is not)
        n = len(prof)
        b0, b1 = (int(0.03 * n), int(0.6 * n)) if grip_end == "min" else (int(0.4 * n), int(0.97 * n))
        tt = np.array([prof[i]["t"] for i in range(b0, b1)])
        ww_ = np.array([1.0 / max(1e-6, prof[i]["r"]) for i in range(b0, b1)])
        bu, au = np.polyfit(tt, [prof[i]["uc"] for i in range(b0, b1)], 1, w=ww_)
        bw, aw = np.polyfit(tt, [prof[i]["wc"] for i in range(b0, b1)], 1, w=ww_)
        d = (a1 + a2 * float(bu) + a3 * float(bw)).normalized()
        Z = d if grip_end == "min" else -d
        grip_pt = Vector(centre) + a1 * t_g + a2 * float(au + bu * t_g) + a3 * float(aw + bw * t_g)
        report["shaft_tilt_vs_pca_deg"] = round(math.degrees(d.angle(a1)), 2)
        # edge: the side where the business end carries more surface (axe bit vs poll, halberd blade vs
        # hook), measured from the shaft line with area-weighted triangle centroids
        cen = (A + B + C) / 3.0 - np.array(grip_pt)
        along = cen @ np.array(Z)
        perp = cen - np.outer(along, np.array(Z))
        a2p = (a2 - Z * a2.dot(Z)).normalized()
        head_m = along > along.max() - (along.max() - along.min()) / 3
        side = perp[head_m] @ np.array(a2p)
        off = float(np.average(side, weights=area[head_m] + 1e-12)) if head_m.any() else 0.0
        edge = a2p if off >= 0 else -a2p
        report["edge"] = {"method": "head_area_side", "mean_offset_m": round(off * s, 4)}
        Y = -edge
    g = prm.get("grip", "auto")
    if isinstance(g, dict) and "point" in g:
        grip_pt = Vector(g["point"])
        report["grip"]["method"] = "params.point"
        grip_auto = False
    if prm.get("edge") not in (None, "auto"):
        e = Vector(prm["edge"]).normalized()
        Y = -(e - Z * e.dot(Z)).normalized()
        report["edge"] = {"method": "params.edge"}
    R = L.frame_matrix(z=Z, y=Y)          # item axes in input coordinates
    M = Matrix.Scale(s, 4) @ R.transposed().to_4x4() @ Matrix.Translation(-grip_pt)
    ob.data.transform(M)
    ob.data.update()
    bpy.context.view_layer.update()
    ob.name = "item_%s" % (prm.get("type") or kind)
    # module budgets (items brief): one-handed 2500, two-handed / staff / shield 4000. A larger requested
    # budget (server default, user) is clamped unless params.allow_over_budget
    class_budget = 2500 if (kind in ("blade", "axe", "blunt", "wand", "torch") and L_out < 1.15) else 4000
    allow_over = bool(prm.get("allow_over_budget", False))
    budget = int(prm.get("budget_tris") or class_budget)
    if budget > class_budget and not allow_over:
        warnings.append("budget_tris %d exceeds the module budget %d for this class; clamped "
                        "(params.allow_over_budget keeps it)" % (budget, class_budget))
        budget = class_budget
    before, after = L.decimate_to(ob, budget)
    me = ob.data
    me.shade_smooth()
    me.set_sharp_from_angle(angle=math.radians(float(prm.get("sharp_deg", 50))))
    if not me.uv_layers:
        af.select_only([ob])
        bpy.ops.object.mode_set(mode="EDIT")
        bpy.ops.mesh.select_all(action="SELECT")
        bpy.ops.uv.smart_project(angle_limit=math.radians(60), island_margin=0.01)
        bpy.ops.object.mode_set(mode="OBJECT")
        warnings.append("source had no UVs: smart-projected (no texture to carry)")
    if not me.materials:
        me.materials.append(L.Materials(prm.get("style", "medieval"))("metal"))
    shrunk = L.downscale_images([ob], int(prm.get("texture_max", 1024)))
    shift = L.place_on_ground(ob, prm.get("origin", "bbox"))
    ob["af_item_type"] = prm.get("type") or kind
    ob["af_source"] = os.path.basename(inp["mesh"])
    socks = [L.add_item_socket(ob, "socket_grip_main", shift, kind="grip", size=0.04,
                               frame="grip: +Z thumb side / along shaft, -Y edge-face")]
    bpy.context.view_layer.update()
    lo, hi = af.world_bbox([ob])
    if two:
        if off_dz is None or not grip_auto:  # manual grip point: keep the rule's hand spacing below it
            off_dz = -(OFF_GAP if kind == "blade" else max(0.10, 0.14 * L_out))
        off = Vector(shift) + Vector((0, 0, off_dz))
        off.z = max(off.z, lo.z + 0.03)
        socks.append(L.add_item_socket(ob, "socket_grip_off", off, kind="grip", size=0.04))
        report["grip"]["hands_gap_m"] = round(float(shift[2] - off.z), 4)
    # holdability of the main grip: section of the output mesh within +-1.5 cm of socket_grip_main
    vco = np.array([v.co[:] for v in ob.data.vertices])
    band = np.abs(vco[:, 2] - shift[2]) < 0.015
    grip_sec = None
    if kind != "shield" and band.sum() >= 6:
        q = vco[band, :2] - np.array([shift[0], shift[1]])
        ex, ey = float(np.ptp(q[:, 0])), float(np.ptp(q[:, 1]))
        grip_sec = {"radius_m": round(float(np.percentile(np.hypot(q[:, 0], q[:, 1]), 98)), 4),
                    "aspect": round(max(ex, ey) / max(1e-6, min(ex, ey)), 3)}
    if kind in ("staff", "wand", "polearm", "torch"):
        top = [v.co for v in ob.data.vertices if v.co.z > hi.z - 0.04 * L_out]
        tip = sum(top, Vector()) / len(top) if top else Vector((0, 0, hi.z))
        socks.append(L.add_item_socket(ob, "socket_fx_tip", (tip.x, tip.y, hi.z - 0.02 * L_out), kind="fx", size=0.04))
    bpy.context.view_layer.update()
    tris = af.tri_count([ob])
    # profile chart (numpy image): radius along the main axis, grip marked
    prof_img = np.zeros((120, 480, 4), np.float32)
    prof_img[..., :3] = (0.16, 0.17, 0.19)
    prof_img[..., 3] = 1
    rmax = max(p["r"] for p in prof)
    for i, p in enumerate(prof):
        x0 = int(i * 480 / len(prof))
        x1 = int((i + 1) * 480 / len(prof))
        h = int(100 * p["r"] / rmax)
        col = (0.85, 0.65, 0.3, 1) if p["aspect"] > 2.5 else (0.5, 0.75, 0.95, 1)
        prof_img[118 - h:118, x0:max(x0 + 1, x1 - 1)] = col
    if kind != "shield":
        gx = int(report["grip"]["grip_bin"] * 480 / len(prof))
        prof_img[:, max(0, gx - 1):gx + 1] = (1, 0.3, 0.3, 1)
        if "off_bin" in report["grip"]:
            ox = int(report["grip"]["off_bin"] * 480 / len(prof))
            prof_img[:, max(0, ox - 1):ox + 1] = (0.3, 1, 0.4, 1)
    L.stamp_text(prof_img, "SECTION R ALONG AXIS  BLUE ROUND  ORANGE FLAT  RED GRIP  GREEN OFF", 4, 4, 1,
                 (0.9, 0.9, 0.9, 1))
    outputs = {"profile": L.save_png(prof_img, af.out_path(req, "profile.png"))}
    out_dir = req["out_dir"]
    outputs["glb"] = af.export_glb(af.out_path(req, "item.glb"), objects=[ob] + socks, animations=False, skins=False)
    if prm.get("preview", True):
        outputs["preview"] = af.render_views(af.out_path(req, "preview.png"), [ob],
                                             views=("left", "front", "three_quarter"), size=int(prm.get("size", 384)))
    outputs["blend"] = af.save_blend(af.out_path(req, "item.blend"))
    info = {"type": prm.get("type") or kind, "kind": kind, "source": inp["mesh"], "tris_source": tris_in,
            "tris": tris, "budget_tris": budget, "module_budget_tris": class_budget, "grip_section": grip_sec,
            "decimate": [before, after], "scale_applied": round(s, 5),
            "length_source_units": round(L_in, 5), "length_m": L_out,
            "dimensions_m": [round(x, 4) for x in (hi - lo)], "bbox_min": list(lo), "bbox_max": list(hi),
            "sockets": [L.socket_record(e) for e in socks], "textures_downscaled": shrunk,
            "pca_axes_source": [[round(float(x), 4) for x in axes[:, k]] for k in range(3)],
            "grip_point_source": [round(x, 5) for x in grip_pt], "report": report,
            "profile": [{"t": round(p["t"] * s, 4), "r": round(p["r"] * s, 4), "aspect": round(p["aspect"], 2)}
                        for p in prof],
            "axes": {"up": "+Z", "main_axis": "+Z", "edge_or_face": "-Y", "origin": prm.get("origin", "bbox")}}
    with open(af.out_path(req, "item.json"), "w", encoding="utf-8") as fh:
        json.dump(af._jsonable(info), fh, ensure_ascii=False, indent=2)
    outputs["json"] = af.out_path(req, "item.json")
    checks.append(af.check("tris_within_budget", tris <= budget, tris, budget))
    checks.append(af.check("within_module_budget", tris <= class_budget, tris, class_budget,
                           severity="warning" if allow_over else "error",
                           note="items brief: one-handed 2500, two-handed / staff / shield 4000"))
    summ = L.glb_summary(outputs["glb"])
    need = {"socket_grip_main"} | ({"socket_grip_off"} if two else set())
    checks.append(af.check("sockets_in_glb", need <= set(summ["sockets"]), sorted(summ["sockets"]), sorted(need)))
    if grip_sec is not None:
        checks.append(af.check("grip_section_holdable", grip_sec["aspect"] < 2.2 and 0.005 < grip_sec["radius_m"] < 0.045,
                               grip_sec, {"aspect": "< 2.2", "radius_m": "0.005..0.045"}, severity="warning",
                               note="section at socket_grip_main (+-1.5 cm); a flat or very thick section means the "
                                    "grip guess landed on the blade / head: set params.grip; see profile.png"))
    bad_mats = [m.name for m in me.materials if not L.principled_ok(m)]
    checks.append(af.check("materials_principled", not bad_mats, {"slots": len(me.materials), "not_principled": bad_mats},
                           "every slot: Principled BSDF -> Material Output", severity="warning"))
    checks.append(af.check("length_m", abs((hi.z - lo.z) - L_out) / L_out < 0.15 or kind == "shield",
                           round(hi.z - lo.z, 4), L_out, severity="warning"))
    if prm.get("crop"):
        warnings.append("cropped from a fused mesh: remnants of the holder (hand, sleeve) may stay at the grip")
    return {"outputs": outputs, "stats": {"kind": kind, "tris_source": tris_in, "tris": tris, "budget_tris": budget,
                                          "length_m": round(hi.z - lo.z, 4), "grip": report["grip"],
                                          "dimensions_m": info["dimensions_m"]},
            "checks": checks, "warnings": warnings}
