"""_props_lib — held props cut out of a generated humanoid (step extract_props) and kept rigid.

A Hunyuan / Tripo humanoid holding a sword, a shield, a staff, a bow or a banner is ONE fused mesh:
skinned as a whole, the sword bends with the arm, the shield crumples on the forearm and a blade cuts
through the body. extract_props cuts every held prop out of the body and makes it a contract item
(docs/FORGE-CONTRACT.md §6), so the rig skins the body alone and attach_prop puts the items back
rigidly into the hands.

Detection is the humanoid rig's own (_humanoid_lib.Landmarks: find_props, find_blades, find_bows,
find_plates, banner plates on poles, bind_props, prop_vertex_sets) run in detect-only mode - the rig
verdict (lm.fail) does not stop it - plus roles.json hints (params.hints: hand_weapon, hand, shield)
that name the item types and relax two searches the rig keeps strict: a pole/staff the arm tracing
missed (banner pole under the cloth) and a shield fused with arm and pole.

Cut: a straight prop is cut by planes perpendicular to its axis at the edges of the fist (found from
the cross-section profile along the axis: the fist is the round wide part around the grip, a guard is
flat, a blade or shaft thin) - the fist stays with the body, the guard and blade go with the item;
shields, banners and bows by the voted vertex labels. Both sides of every cut are capped (holes_fill,
the cap takes the rim UV and material, so the painted texture stays on both parts). A handle through
the fist ("bridge", hidden by the fist) closes the item where the fist was.

Item frame (contract §6 grip frame): socket_grip_main at the grip, +Z along the blade / shaft from the
hand (the rig's prop_grip_dir rule), -Y the edge / face; shields: +Z up in the board, -Y the face, grip
3.5 cm behind the back centre (the frame of socket_mount_shield_*); socket_grip_off for two-handed
items, socket_fx_tip on staffs and wands. The placement (where the item sat on the body) is written
as a socket empty socket_prop_<hand>_<n> on the body (extras af_bone, af_hand, af_item): rig_humanoid
keeps input sockets (keep_input_sockets) as children of af_bone, attach_prop moves the hand / shield
mount onto it (refit) - the item comes back exactly where the concept had it.
"""
import json
import math
import os
import re
import time

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector
from mathutils.kdtree import KDTree

import af_common as af
from steps import _humanoid_lib as hl

# ----------------------------------------------------------------------------- hints / types

# words of roles.json hand_weapon -> item type (first match wins; order matters: "crossbow" before "bow")
TYPE_WORDS = [
    (r"great\s*sword|two.handed sword|greatsword|claymore|zweihander", "greatsword"),
    (r"crossbow", "crossbow"),
    (r"long\s*bow|war\s*bow|bow", "bow"),
    (r"banner|standard|flag", "banner"),
    (r"halberd|glaive|scythe|poleaxe|pole.axe", "halberd"),
    (r"spear|pike|lance|trident", "spear"),
    (r"staff|rod|scepter|sceptre|crozier|crook", "staff"),
    (r"wand", "wand"),
    (r"war\s*hammer|warhammer|maul", "warhammer"),
    (r"battle\s*axe|great\s*axe", "battle_axe"),
    (r"axe|hatchet", "axe"),
    (r"mace|morning\s*star|flail|club|hammer", "mace"),
    (r"dagger|knife|dirk", "dagger"),
    (r"sword|blade|sabre|saber|falchion|cutlass", "sword"),
    (r"book|tome|grimoire|codex", "book"),
    (r"skull", "skull"),
    (r"lantern|censer|bell", "lantern"),
    (r"torch", "torch"),
]
# plausible length of the main axis (m) per type, for the gate and for hint/geometry agreement
TYPE_LEN = {"dagger": (0.15, 0.6), "sword": (0.4, 1.45), "greatsword": (0.85, 2.1), "mace": (0.25, 1.35),
            "warhammer": (0.5, 1.9), "axe": (0.3, 1.35), "battle_axe": (0.7, 2.0), "spear": (1.0, 3.2),
            "halberd": (1.0, 3.2), "staff": (0.8, 3.0), "wand": (0.12, 0.75), "bow": (0.55, 2.3),
            "crossbow": (0.3, 1.3), "banner": (0.9, 3.4), "book": (0.1, 0.6), "skull": (0.08, 0.55),
            "lantern": (0.1, 0.9), "torch": (0.25, 1.1), "shield": (0.25, 1.7)}
LONG_TYPES = {"spear", "halberd", "staff", "banner"}
FX_TIP_TYPES = {"staff", "wand", "torch", "lantern"}
# extracted type -> item_kit type for the replacement (None: no kit equivalent, the extracted item stays)
KIT_TYPE = {"sword": "sword", "greatsword": "greatsword", "dagger": "dagger", "mace": "mace",
            "warhammer": "warhammer", "axe": "axe", "battle_axe": "battle_axe", "spear": "spear",
            "halberd": "halberd", "staff": "staff", "wand": "wand", "bow": "bow", "crossbow": "crossbow",
            "book": "book", "torch": "torch", "shield": "shield", "banner": None, "skull": None, "lantern": None}
SHIELD_BACK_GRIP = 0.035     # grip 3.5 cm behind the back centre of a shield (item_from_mesh rule)
ISLAND_MAX_FRAC = 0.015      # loose body islands up to this share of the vertices may join a nearby item
SHORT_HEADED_MIN = 0.17      # H: shortest haft of a headed weapon (mace, axe) find_blades takes in extract mode


def words_to_types(text):
    """'banner + sun scepter' -> ['banner', 'staff']; 'two swords' -> ['sword', 'sword']."""
    out = []
    if not text:
        return out
    for part in re.split(r"\+|,|/|\band\b|\bwith\b", str(text).lower()):
        part = part.strip()
        if not part or part in ("none", "unarmed"):
            continue
        for rx, typ in TYPE_WORDS:
            if re.search(rx, part):
                out.append(typ)
                if re.search(r"\btwo\b|\bdual\b|\bpair\b", part):
                    out.append(typ)
                break
    return out


def parse_hints(h):
    """params.hints (a roles.json entry) -> {"by_side": {"Left"|"Right": [types]}, "shield_side", "hand",
    "faction", "build", "text"}. hand r|l|both: the main weapon hand; a second weapon word goes to the
    other hand; a shield goes to the other hand (or the left one for 'both')."""
    h = dict(h or {})
    hand = str(h.get("hand") or "").lower()
    types = words_to_types(h.get("hand_weapon"))
    main = {"r": "Right", "l": "Left"}.get(hand)
    other = {"Right": "Left", "Left": "Right"}.get(main)
    by_side = {"Left": [], "Right": []}
    if hand == "both" and types:
        by_side["Right"].append(types[0])
        by_side["Left"].append(types[1] if len(types) > 1 else types[0])
    elif main:
        if types:
            by_side[main].append(types[0])
        for t in types[1:]:
            by_side[other].append(t)
    # notes like "heater shield and a banner pole in the left hand" name a second held item
    for m_ in re.finditer(r"([a-z][a-z \-]*?)\s+in (?:the |his |her )?(left|right) hand", str(h.get("notes") or "").lower()):
        side_n = "Left" if m_.group(2) == "left" else "Right"
        for t in words_to_types(m_.group(1).split(" and ")[-1]):
            if t not in by_side[side_n]:
                by_side[side_n].append(t)
    shield = bool(h.get("shield"))
    shield_side = None
    if shield:
        shield_side = other or "Left"
    return {"by_side": by_side, "shield_side": shield_side, "hand": hand or None, "types": types,
            "faction": h.get("faction"), "build": h.get("build"), "text": h.get("hand_weapon") or ""}


# ----------------------------------------------------------------------------- small geometry helpers

def unit(v):
    v = np.asarray(v, dtype=np.float64)
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-12 else v


def perp_frame(d):
    """Two unit rows perpendicular to d (the slicing frame of the rig)."""
    return hl._slice_frame(d)[:2]


def axis_coords(X, c, d):
    rel = X - c
    t = rel @ d
    perp = rel - np.outer(t, d)
    return t, perp, np.linalg.norm(perp, axis=1)


def kd_of(points):
    kd = KDTree(len(points))
    for i, p in enumerate(points.tolist()):
        kd.insert(p, i)
    kd.balance()
    return kd


# ----------------------------------------------------------------------------- detection

def _hint_held_poles(lm, hints, warnings):
    """A straight prop the rig left unheld (its arm chain merged with banner cloth / a cape, so grips()
    found no free arm chain ending at it) is taken as held when the hints say that side holds a long
    item (staff, spear, banner, halberd) or any weapon, the prop is on that side of the body and a fist of
    body mass sits on its axis line (within the shaft or just beyond its ends) in reach of the shoulder.
    Returns the props changed."""
    changed = []
    P, X0, H = lm.P, lm.X0, lm.H
    body = ~lm.prop_mask
    for p in lm.props:
        if p.get("held"):
            continue
        ex = p.get("extent", p["t"])
        mid = p["c"] + p["d"] * 0.5 * (ex[0] + ex[1])
        side = "Left" if mid[0] > X0 else "Right"
        want = hints["by_side"].get(side) or []
        if not want:
            continue
        rel = P - p["c"]
        t = rel @ p["d"]
        dist = np.linalg.norm(rel - np.outer(t, p["d"]), axis=1)
        lo, hi = min(ex[0], p["t"][0]) - 0.15, max(ex[1], p["t"][1]) + 0.15
        near = np.nonzero(body & (dist < 0.06) & (t > lo) & (t < hi) & (P[:, 2] > 0.3))[0]
        if len(near) < 30:
            continue
        tn = t[near]
        best_n, fist = 0, None
        for a0 in np.arange(float(tn.min()), float(tn.max()) + 0.005, 0.005):
            m = (tn >= a0) & (tn < a0 + 0.08)
            if int(m.sum()) > best_n:
                best_n, fist = int(m.sum()), near[m]
        if fist is None or best_n < 30:
            continue
        G = P[fist].mean(0)
        Sh = lm.shoulder_prior(side)
        reach = float(np.linalg.norm(G - Sh))
        if reach > 0.5 or abs(float(G[0]) - X0) < 0.06:
            continue
        tf = t[fist]
        tg = float(tf.mean())
        on_shaft = ex[0] + 0.03 < tg < ex[1] - 0.03
        p.update({"held": True, "side": side, "grip": G, "grip_t": (float(tf.min()), float(tf.max())),
                  "grip_mode": "shaft" if on_shaft else "end", "bind": side + "Hand",
                  "bind_mode": "hint_" + ("shaft" if on_shaft else "end"), "hinted": True,
                  "fist_samples": int(len(fist))})
        changed.append(p)
        warnings.append("prop %.2f m on the %s side taken as held by the %s hand (roles hint %s; fist %d samples, "
                        "%.2f H from the shoulder)" % ((ex[1] - ex[0]) * H, side.lower(), side.lower(),
                                                        "/".join(want), len(fist), reach))
    return changed


def _hint_pole_extension(lm, p, H):
    """A held long pole whose straight chain stops at the banner cloth: follow its axis line upward/downward
    through samples that lie on the line (radius r_cap) - the pole continues behind the cloth and below the
    fist. Updates p["extent"]. Returns the extension in H."""
    P = lm.P
    rel = P - p["c"]
    t = rel @ p["d"]
    dist = np.linalg.norm(rel - np.outer(t, p["d"]), axis=1)
    r = max(p.get("r_cap", 0.02), 0.02)
    ex = list(p.get("extent", p["t"]))
    grown = 0.0
    for k_, sgn in ((1, 1.0), (0, -1.0)):
        end = ex[k_]
        for _ in range(200):
            a0, a1 = (end, end + 0.01) if sgn > 0 else (end - 0.01, end)
            n_in = int(((dist < r) & (t >= a0) & (t < a1)).sum())
            if n_in < 3:
                break
            end += 0.01 * sgn
            if (p["c"] + p["d"] * end)[2] < 0.0 or (p["c"] + p["d"] * end)[2] > 1.4:
                break
        grown += abs(end - ex[k_])
        ex[k_] = end
    p["extent"] = ex
    return grown


def _hint_banner_cloth(lm, p, H):
    """Cloth of a banner on a held pole: samples beside the pole on its far side from the body (beyond the
    pole's inner edge), within 0.45 H of the axis, along the pole but outside the fist window, that are
    connected - through 2D slice components linked between neighbouring heights - to a component touching
    the pole above the fist. Cloth hanging below the hand stays with it when it hangs from the pole.
    Returns a sample mask (H units of lm.P)."""
    P, X0 = lm.P, lm.X0
    rel = P - p["c"]
    t = rel @ p["d"]
    dist = np.linalg.norm(rel - np.outer(t, p["d"]), axis=1)
    ex = p.get("extent", p["t"])
    tg = float((np.asarray(p["grip"]) - p["c"]) @ p["d"])
    q = p["c"] + p["d"] * tg
    side = 1.0 if q[0] > X0 else -1.0
    zt = P[:, 2]
    ax_x = p["c"][0] + p["d"][0] * ((zt - p["c"][2]) / max(abs(p["d"][2]), 1e-6)) * np.sign(p["d"][2])
    r = max(p.get("r_cap", 0.02), 0.02)
    outer = (P[:, 0] - ax_x) * side > -r
    along = (t > ex[0] + 0.02) & (t < ex[1] + 0.05) & ~((np.abs(t - tg) < 0.05) & (dist < 0.045))
    m = outer & along & (dist < 0.45) & ~lm.prop_mask
    pole = dist < 0.03
    S = hl.Slices(P, axis=2, step=0.01, thick=0.012, cell=1.0 / 120, mask=m | pole)
    seeds = []
    for c in S.all:
        if len(c.idx) < 5:
            continue
        pts = P[c.idx]
        rr = pts - p["c"]
        tt = rr @ p["d"]
        d_ = np.linalg.norm(rr - np.outer(tt, p["d"]), axis=1)
        if d_.min() < 0.035 and float(np.median(tt)) > tg + 0.05 and m[c.idx].sum() >= 5:
            seeds.append(c.id)
    keep = np.zeros(len(P), dtype=bool)
    seen = set(seeds)
    stack = list(seeds)
    while stack:
        i = stack.pop()
        c = S.all[i]
        keep[c.idx] = True
        for j in S.up[i] + S.down[i]:
            jj = S.all[j].idx
            if j not in seen and len(jj) >= 3 and m[jj].sum() > 0.5 * max(1, int((~pole[jj]).sum())):
                seen.add(j)
                stack.append(j)
    keep &= m
    return keep


BOW_MIRROR_TUBE, BOW_MIRROR_BAND = 0.02, 0.012     # H: tube around the predicted limb, band around the bow plane


def _bow_side_chains(lm, bw):
    """Thin chains beside the body that run from the bow's fist to its far ends (a limb find_bows dropped
    because it left the bow plane - pressed against the leg): horizontal slices of the samples on the bow's
    side (|x - X0| > 0.12 H, not yet a prop), thin components (< BOW_THIN) chained across levels; a chain
    whose end is within 0.07 H of the grip and that runs >= 0.2 H away from it is a limb. Returns a mask."""
    P, X0 = lm.P, lm.X0
    G = np.asarray(bw["grip"], dtype=np.float64)
    s = 1.0 if bw["side"] == "Left" else -1.0
    m = ((P[:, 0] - X0) * s > hl.BOW_SIDE) & ~lm.prop_mask
    S = hl.Slices(P, axis=2, step=0.005, thick=0.006, cell=1.0 / 160, mask=m)
    thin = [c.id for c in S.all if c.n >= 3 and max(c.ext(0), c.ext(1)) < hl.BOW_THIN]
    tset = set(thin)
    links = {i: [j for j in S.up[i] if j in tset] for i in thin}
    out = np.zeros(len(P), dtype=bool)
    for g in hl._uf_groups(thin, links):
        cents = np.array([S.all[i].c for i in g])
        zs = cents[:, 2]
        if zs.max() - zs.min() < 0.2:
            continue
        ends = [cents[np.argmax(zs)], cents[np.argmin(zs)]]
        if min(np.linalg.norm(e - G) for e in ends) > 0.07:
            continue
        out[np.concatenate([S.all[i].idx for i in g])] = True
    return out


def _bow_missing_limb(lm, bw):
    """A bow limb fused with the leg (a longbow standing on the foot) breaks find_bows' chain of thin
    cross-sections: one limb is found, the other is only its string. The missing limb is predicted by
    mirroring the found limb's bulge across the plane through the grip perpendicular to the chord and
    fitting the mirrored tip onto the found tip of that side (the string end); samples within 0.02 H of
    the predicted curve and 0.012 H of the bow plane that are not yet the bow are added. Returns a sample
    mask (empty when both limbs were found)."""
    P = lm.P
    mc = np.asarray(bw["curve"], dtype=np.float64)
    G = np.asarray(bw["grip"], dtype=np.float64)
    tips = [np.asarray(x, dtype=np.float64) for x in bw["tips"]]
    d = unit(tips[1] - tips[0])
    n = unit(bw["normal"])
    # limb points: off the chord line (the string lies on it)
    rel = mc - tips[0]
    off = np.linalg.norm(rel - np.outer(rel @ d, d), axis=1)
    sag = max(float(bw["sagitta"]), 1e-3)
    limb = mc[off > 0.3 * sag]
    out = np.zeros(len(P), dtype=bool)
    if len(limb) < 4:
        return out
    tg = float((G - tips[0]) @ d)
    up = limb[(limb - tips[0]) @ d > tg + 0.03]
    lo = limb[(limb - tips[0]) @ d < tg - 0.03]
    for have, tip_have, tip_miss in ((up, tips[1], tips[0]), (lo, tips[0], tips[1])):
        other = lo if have is up else up
        if len(have) < 4 or len(other) >= 0.5 * len(have):
            continue
        if np.linalg.norm(tip_miss - G) < 0.08:
            # the chain stops at the fist: the missing tip is the far end of the string (a straight thin prop of
            # the same hand starting at the grip), else the found tip mirrored through the grip
            best = None
            for p in lm.props:
                ex = p.get("extent", p["t"])
                for e0, e1 in ((ex[0], ex[1]), (ex[1], ex[0])):
                    a0 = p["c"] + p["d"] * e0
                    a1 = p["c"] + p["d"] * e1
                    if np.linalg.norm(a0 - G) < 0.08 and float((a1 - G) @ (tip_have - G)) < 0:
                        L_ = float(np.linalg.norm(a1 - G))
                        if best is None or L_ > best[0]:
                            best = (L_, a1)
            tip_miss = best[1] if best is not None else G - (tip_have - G)
        # mirror across the plane through G with normal d, then rotate/scale (about G, in the bow plane) so
        # the mirrored far tip lands on the missing side's tip
        mir = have - 2.0 * np.outer((have - G) @ d, d)
        mt = tip_have - 2.0 * ((tip_have - G) @ d) * d
        a, b_ = mt - G, tip_miss - G
        a -= n * (a @ n)
        b_ -= n * (b_ @ n)
        if np.linalg.norm(a) < 1e-6 or np.linalg.norm(b_) < 1e-6:
            continue
        s_ = np.linalg.norm(b_) / np.linalg.norm(a)
        ang = math.atan2(float(np.cross(unit(a), unit(b_)) @ n), float(unit(a) @ unit(b_)))
        c_, sn = math.cos(ang), math.sin(ang)
        rel2 = (mir - G)
        rel2 = rel2 * c_ + np.cross(n, rel2) * sn + np.outer(rel2 @ n, n) * (1 - c_)
        pred = G + rel2 * s_
        pts = np.vstack([pred, tip_miss[None]])
        pts = pts[np.argsort((pts - G) @ (tip_miss - G))]
        dmin = np.full(len(P), np.inf)
        for p0, p1 in zip(pts[:-1], pts[1:]):
            dd, _ = hl._seg_dist(P, p0, p1)
            dmin = np.minimum(dmin, dd)
        dn = np.abs((P - G) @ n)
        far = np.linalg.norm(P - G, axis=1) > 0.04
        found = (dmin < BOW_MIRROR_TUBE) & (dn < BOW_MIRROR_BAND) & far & (P[:, 2] > 0.005)
        out |= found
        if found.sum() >= 10:
            out |= _track_limb(lm, found, tip_miss)
    return out


def _track_limb(lm, start, tip):
    """Follow a bow limb from the part already found (start, a sample mask) toward its tip, slice by slice: in
    each horizontal slice the thin component (< 1.3 x BOW_THIN) nearest to the position expected from the last
    one and the direction to the tip (within 0.035 H) is the limb; three empty slices end it. The mirrored
    prediction catches the limb near the grip; a longbow's limb bends differently further out."""
    P = lm.P
    pts = P[start]
    up = tip[2] > float(pts[:, 2].mean())
    S = hl.Slices(P, axis=2, step=0.005, thick=0.006, cell=1.0 / 160, mask=~lm.prop_mask)
    edge = pts[np.argsort(pts[:, 2])[-max(3, len(pts) // 10):]] if up else pts[np.argsort(pts[:, 2])[:max(3, len(pts) // 10)]]
    cur = edge.mean(0)
    out = np.zeros(len(P), dtype=bool)
    k = S.k_of(float(cur[2]))
    k_end = S.k_of(float(tip[2]))
    step = 1 if up else -1
    misses = 0
    while (k - k_end) * step < 0 and 0 <= k + step < len(S.comps):
        k += step
        z = S.z_of(k)
        frac = 0.0 if abs(tip[2] - cur[2]) < 1e-6 else (z - cur[2]) / (tip[2] - cur[2])
        want = cur + (tip - cur) * float(np.clip(frac, 0.0, 1.0))
        best = None
        for c in S.comps[k]:
            if max(c.ext(0), c.ext(1)) > 1.3 * hl.BOW_THIN:
                continue
            dxy = float(np.linalg.norm((c.c - want)[:2]))
            if dxy < 0.035 and (best is None or dxy < best[0]):
                best = (dxy, c)
        if best is None:
            misses += 1
            if misses >= 3:
                break
            continue
        misses = 0
        out[best[1].idx] = True
        cur = best[1].c
    return out


def _hint_shield(lm, side, H, rng_seed=5):
    """A shield the rig's free-chain plate search missed (fused with the arm, a pole and a cape into one
    chain): RANSAC planes over the samples on the shield side - beside or in front of the trunk, between
    0.25 H and the shoulders, not behind the back, not yet a prop. The best plane's inliers (0.012 H) are
    split into in-plane components; the largest is a shield when it is disc-like (spread ratio < 2.5),
    bigger than a buckler (R95 >= 0.11 H) and not the trunk's own surface (its centre clear of the trunk
    core, or in front of it). Returns a plate record like find_plates' or None."""
    P, X0 = lm.P, lm.X0
    s = lm.side_sign(side)
    yb = np.array([lm.trunk_at(float(z))["yback"] for z in np.clip(P[:, 2], 0.3, lm.top)])
    reg = ((P[:, 0] - X0) * s > 0.02) & (P[:, 2] > 0.25) & (P[:, 2] < lm.z_st + 0.05) & (P[:, 1] < yb - 0.02) &         ~lm.prop_mask
    idx = np.nonzero(reg)[0]
    if len(idx) < 300:
        return None
    rng = np.random.default_rng(rng_seed)
    Q = P[idx]
    sub = Q[rng.integers(0, len(Q), size=min(len(Q), 4000))]
    best = None
    for _ in range(600):
        a, b_, c = Q[rng.integers(0, len(Q), size=3)]
        nrm = np.cross(b_ - a, c - a)
        ln = float(np.linalg.norm(nrm))
        if ln < 1e-6:
            continue
        nrm /= ln
        cnt = int((np.abs((sub - a) @ nrm) < 0.012).sum())
        if best is None or cnt > best[0]:
            best = (cnt, a, nrm)
    if best is None:
        return None
    _, a, nrm = best
    inl = idx[np.abs((Q - a) @ nrm) < 0.012]
    if len(inl) < 200:
        return None
    R_ = hl._slice_frame(nrm)
    q2 = P[inl] @ R_.T
    comps = hl.components_2d(q2, np.arange(len(inl)), [0, 1], 0.008, 0)
    if not comps:
        return None
    cp = max(comps, key=lambda c: c.n)
    pts = P[inl[cp.idx]]
    cen = pts.mean(0)
    rel = pts - cen
    rel -= np.outer(rel @ nrm, nrm)
    R95 = float(np.percentile(np.linalg.norm(rel, axis=1), 95))
    sv = np.linalg.svd(rel, full_matrices=False)[1] / math.sqrt(len(rel))
    tp = lm.trunk_at(float(cen[2]))
    rec = {"side": side, "centre": cen, "normal": nrm, "radius": R95, "points": int(len(pts)), "hinted": True,
           "spread_ratio": round(float(sv[0] / max(sv[1], 1e-9)), 3)}
    if sv[0] / max(sv[1], 1e-9) > 2.5 or R95 < 0.11:
        return None
    if abs(float(cen[0]) - X0) < 0.5 * tp["W"] and float(cen[1]) > tp["yfront"] - 0.03:
        return None      # the trunk's own front / side
    m = np.zeros(len(P), dtype=bool)
    relq = P - cen
    dn = np.abs(relq @ nrm)
    inp = np.linalg.norm(relq - np.outer(relq @ nrm, nrm), axis=1)
    m[(dn < 0.06) & (inp < R95 * 1.08) & reg] = True
    lm.prop_mask |= m
    rec["points"] = int(m.sum())
    return rec


def detect(body, P, prep, H, params, hints, warnings):
    """Run the rig's landmarks in detect-only mode and turn every held prop into an item candidate.
    Returns (lm, P, items, report). items: [{id, side, hand, kind, type, sources, sample_mask, vmask,
    axis (metres) | None, grip (m), grip_mode, zdir}]."""
    lp = {k: params[k] for k in ("markers", "robe", "min_confidence") if params.get(k) is not None}
    # a short headed weapon (a mace or an axe held at the hip, 0.17-0.25 H of haft) is a held prop too here: left
    # fused, the rig traces the arm down the haft into the head ("arm 0.52 H")
    if params.get("short_weapons", True):
        lp["short_headed_min_len"] = SHORT_HEADED_MIN
        lp["belt_items"] = True
    report = {"rig_verdict": None, "props": [], "bows": [], "plates": [], "not_held": [], "hints": hints}
    try:
        lm, P = hl.run_landmarks(body, P, H, lp, warnings, prep)
    except Exception as exc:     # no trunk etc.: nothing to extract
        report["error"] = "%s: %s" % (type(exc).__name__, exc)
        return None, P, [], report
    report["rig_verdict"] = lm.fail
    # roles hints: a pole / staff left unheld because the arm tracing failed
    hinted = _hint_held_poles(lm, hints, warnings) if params.get("use_hints", True) else []
    extra_banner = {}
    for p in hinted:
        side_types = hints["by_side"].get(p["side"]) or []
        if any(t in ("banner", "staff", "spear", "halberd") for t in side_types):
            g = _hint_pole_extension(lm, p, H)
            report.setdefault("hint_pole_extension_h", []).append(round(g, 3))
            ex = p["extent"]
            tg = float((np.asarray(p["grip"]) - p["c"]) @ p["d"])
            if ex[0] + 0.03 < tg < ex[1] - 0.03:      # the pole now runs through the fist
                p["grip_mode"], p["bind_mode"] = "shaft", "hint_shaft"
            if "banner" in side_types:
                cloth = _hint_banner_cloth(lm, p, H)
                if cloth.sum() > 200:
                    extra_banner[id(p)] = cloth
                    lm.prop_mask |= cloth
    for bw in lm.bow_props:
        miss = (_bow_missing_limb(lm, bw) | _bow_side_chains(lm, bw)) & ~lm.prop_mask
        if miss.sum() >= 30:
            bw["idx"] = np.unique(np.concatenate([bw["idx"], np.nonzero(miss)[0]]))
            lm.prop_mask |= miss
            report.setdefault("bow_mirrored_limb_samples", []).append(int(miss.sum()))
    # roles hint: a shield the free-chain plate search missed
    if params.get("use_hints", True) and hints.get("shield_side"):
        sd = hints["shield_side"]
        have = [pl for pl in getattr(lm, "plate_props", []) if pl["side"] == sd and not pl.get("on_prop")]
        if not have:
            pl = _hint_shield(lm, sd, H)
            if pl is not None:
                lm.plate_props = list(getattr(lm, "plate_props", [])) + [pl]
                report["hint_shield"] = {"side": sd, "radius_h": round(pl["radius"], 3), "points": pl["points"],
                                         "spread_ratio": pl["spread_ratio"]}
                warnings.append("shield in the %s hand found by the roles hint (plane fit, R95 %.2f H)" % (
                    sd.lower(), pl["radius"]))
    if hinted:
        # the plates the rig rejected for being far from a (mis-traced) forearm: a plate on a now-held pole
        for pl in getattr(lm, "plate_props", []):
            pp = lm._plate_prop(pl["centre"])
            if pp is not None and pp.get("held"):
                pl["side"], pl["on_prop"] = pp["side"], True
    verts = hl.mesh_verts(body)
    sets = hl.prop_vertex_sets(lm, verts, P, H)
    srcs = [("bow", bw) for bw in lm.bow_props] + [("prop", p) for p in lm.props] + \
        [("plate", pl) for pl in getattr(lm, "plate_props", [])]
    sample_masks = _sample_masks(lm, srcs)
    for (kind, src), (bone, vm, rec), sm in zip(srcs, sets, sample_masks):
        rec = dict(rec)
        bone_s = hl.short(bone)
        entry = {"kind": kind, "bone": bone_s, "vertices": int(vm.sum())}
        entry.update({k: rec[k] for k in ("length_m", "radius_m", "centre", "bind_mode") if k in rec})
        report[{"bow": "bows", "prop": "props", "plate": "plates"}[kind]].append(entry)
        src["_vm"], src["_sm"] = vm, sm
        if kind == "prop" and id(src) in extra_banner:
            src["_cloth_sm"] = extra_banner[id(src)]
        if not bone_s.endswith("Hand"):
            report["not_held"].append(entry)
    # per hand: weapons (props, bows, banner plates) merged into one item; shields one item each
    items = []
    # a short headed weapon hanging at the belt: its own item on the Hips
    for p in lm.props:
        if p.get("belt") and p.get("_vm") is not None and p["_vm"].any():
            it = _weapon_item(lm, p["side"], "hip", [("prop", p)], {"by_side": {"Left": ["mace"], "Right": ["mace"]}}, H)
            it.update({"kind": "belt", "hand": "hip", "bone": "Hips"})
            items.append(it)
    for side in hl.SIDES:
        hand = "l" if side == "Left" else "r"
        weap = [(k, s) for k, s in srcs if str(s.get("bind") or (s.get("side", "") + "Hand")).endswith("Hand")
                and _side_of(k, s) == side and (k != "plate" or s.get("on_prop")) and not s.get("belt")]
        weap = [(k, s) for k, s in weap if s["_vm"].any() or k == "prop"]
        if weap:
            items.append(_weapon_item(lm, side, hand, weap, hints, H))
        shields = [(k, s) for k, s in srcs if k == "plate" and not s.get("on_prop") and _side_of(k, s) == side
                   and s["_vm"].any()]
        if shields:
            items.append(_shield_item(lm, side, hand, shields, H))
    n = {}
    for it in items:
        n[it["hand"]] = n.get(it["hand"], 0) + 1
        it["id"] = "%s_%s%s" % (it["hand"], it["type"], "" if n[it["hand"]] == 1 else "_%d" % n[it["hand"]])
    return lm, P, items, report


def _side_of(kind, s):
    if kind == "plate":
        return s.get("side")
    b_ = str(s.get("bind") or "")
    if b_.startswith("Left"):
        return "Left"
    if b_.startswith("Right"):
        return "Right"
    return s.get("side")


def _sample_masks(lm, srcs):
    """Sample-level region of every source (the same windows as prop_vertex_sets)."""
    P = lm.P
    out = []
    for kind, s in srcs:
        if kind == "bow":
            m = np.zeros(len(P), dtype=bool)
            m[s["idx"]] = True
        elif kind == "prop":
            rel = P - s["c"]
            t = rel @ s["d"]
            dist = np.linalg.norm(rel - np.outer(t, s["d"]), axis=1)
            ex = s.get("extent", s["t"])
            m = lm.prop_mask & (dist < 0.12) & (t > min(ex[0], s["t"][0]) - 0.2) & (t < max(ex[1], s["t"][1]) + 0.2)
        else:
            rel = P - s["centre"]
            dn = np.abs(rel @ s["normal"])
            inp = np.linalg.norm(rel - np.outer(rel @ s["normal"], s["normal"]), axis=1)
            m = lm.prop_mask & (dn < 0.07) & (inp < s["radius"] * 1.15)
        out.append(m)
    return out


def _guess_type(length_m, flat, has_head, hint_types):
    """Item type of a straight prop from its length and cross-section; a matching hint wins."""
    geo = []
    if length_m >= 1.25:
        geo = ["staff", "spear", "halberd", "banner", "greatsword"]
    elif has_head:
        geo = ["mace", "axe", "warhammer", "battle_axe", "staff", "wand"]
    elif flat:
        geo = ["sword", "greatsword", "dagger", "axe"]
    else:
        geo = ["sword", "mace", "staff", "wand", "dagger", "torch", "spear"]
    for t in hint_types or []:
        lo, hi = TYPE_LEN.get(t, (0, 99))
        if lo * 0.8 <= length_m <= hi * 1.2:
            return t, "hint"
    if length_m < 0.42:
        return ("dagger" if flat else "wand"), "geometry"
    return geo[0], "geometry"


def _weapon_item(lm, side, hand, srcs, hints, H):
    """One item per hand: every held straight prop, bow and banner plate of that hand together."""
    P = lm.P
    hint_types = hints["by_side"].get(side) or []
    vm = np.zeros_like(srcs[0][1]["_vm"])
    sm = np.zeros(len(P), dtype=bool)
    for k, s in srcs:
        vm |= s["_vm"]
        sm |= s["_sm"]
        if s.get("_cloth_sm") is not None:
            sm |= s["_cloth_sm"]
    bows = [s for k, s in srcs if k == "bow"]
    props = [s for k, s in srcs if k == "prop"]
    plates = [s for k, s in srcs if k == "plate"]
    it = {"side": side, "hand": hand, "sources": [(k, s) for k, s in srcs], "vmask": vm, "sample_mask": sm,
          "axis": None, "planes": [], "hint_types": hint_types}
    if bows:
        bw = max(bows, key=lambda b_: b_["length"])
        it.update({"kind": "bow", "grip": np.asarray(bw["grip"]) * H, "grip_mode": "bow",
                   "zdir": unit(bw["grip_dir"]), "bow": bw,
                   "type": "crossbow" if "crossbow" in hint_types else "bow", "type_from": "bow_detector"})
        if bw["kind"] == "curved_blade":
            it["type"], it["kind"] = ("sword", "weapon")
        it["length_m"] = float(bw["length"]) * H
        return it
    main = max(props, key=lambda p: (bool(p.get("held")), float(np.ptp(p.get("extent", p["t"])))))
    ex = main.get("extent", main["t"])
    L = float(ex[1] - ex[0]) * H
    rad = float(main["rad"]) * H
    # flat cross-section: the samples of the prop away from the grip spread much more one way
    rel = P[main["_sm"]] - main["c"]
    t_ = rel @ main["d"]
    perp = rel - np.outer(t_, main["d"])
    flat = False
    if len(perp) > 30:
        ev = np.sort(np.linalg.eigvalsh(np.cov((perp @ perp_frame(main["d"]).T).T)))
        flat = bool(ev[1] > 6.0 * max(ev[0], 1e-12))
    has_head = bool(any(r.get("head_samples") for r in lm.info.get("blades", []) if r.get("accepted"))) or \
        bool(main.get("extent") and (main["extent"][1] - main["t"][1] > 0.05 or main["t"][0] - main["extent"][0] > 0.05))
    if plates or "banner" in hint_types and L >= 0.9:
        typ, src = ("banner", "plate on pole" if plates else "hint")
    else:
        typ, src = _guess_type(L, flat, has_head, hint_types)
    d, rule, ref, frac = hl.prop_grip_dir(main, H)
    it.update({"kind": "banner" if typ == "banner" else "weapon", "type": typ, "type_from": src,
               "grip": np.asarray(main["grip"]) * H, "grip_mode": main.get("grip_mode", "shaft"),
               "zdir": unit(d), "grip_rule": rule, "main": main, "length_m": L, "flat": flat, "has_head": has_head,
               "axis": {"c": np.asarray(main["c"]) * H, "d": unit(main["d"]), "ext": [ex[0] * H, ex[1] * H],
                        "t": [main["t"][0] * H, main["t"][1] * H], "rad": rad,
                        "r_cap": float(main.get("r_cap", max(0.012, 1.8 * main["rad"]))) * H}})
    return it


def _shield_item(lm, side, hand, srcs, H):
    vm = np.zeros_like(srcs[0][1]["_vm"])
    sm = np.zeros(len(lm.P), dtype=bool)
    for k, s in srcs:
        vm |= s["_vm"]
        sm |= s["_sm"]
    pl = max((s for _, s in srcs), key=lambda s: s["points"])
    return {"side": side, "hand": hand, "kind": "shield", "type": "shield", "type_from": "plate",
            "sources": list(srcs), "vmask": vm, "sample_mask": sm, "axis": None, "planes": [], "plate": pl,
            "grip_mode": "plate", "centre": np.asarray(pl["centre"]) * H, "normal": unit(pl["normal"]),
            "radius_m": float(pl["radius"]) * H}


# ----------------------------------------------------------------------------- fist window and labels

FIST_EDGE_MIN, FIST_EDGE_MAX = 0.015, 0.03     # fist half-length clamp along the handle (H)


def fist_window(P_m, it, H):
    """Edges of the fist along a straight prop's axis (metres from the grip centre, per side) from the
    cross-section profile: per 0.01 H slab of the samples within 0.05 H of the axis, the spread across
    the minor in-plane direction. The fist is round and wide (~0.045 H both ways), a guard flat (minor
    = its thickness), a blade or shaft thin. Walking out of the fist centre, the first two slabs below
    max(0.02 H, 1.7 x shaft + 0.004 H) end it. End grips (sword at the hilt) only need the tip side.
    Returns {"centre_t", "tip_sign", "edge": {+1: e, -1: e}, "profile": [...]}."""
    ax = it["axis"]
    c, d = ax["c"], ax["d"]
    t, perp, dist = axis_coords(P_m, c, d)
    q2 = perp @ perp_frame(d).T
    tg = float((it["grip"] - c) @ d)
    step = 0.01 * H
    ks = np.arange(-0.15 * H, 0.15 * H + 1e-9, step)
    near = dist < 0.04 * H
    minor, cnt = [], []
    for k in ks:
        sel = near & (t >= tg + k - step / 2) & (t < tg + k + step / 2)
        n = int(sel.sum())
        cnt.append(n)
        if n < 8:
            minor.append(0.0)
            continue
        Q = q2[sel] - q2[sel].mean(0)
        w, V = np.linalg.eigh(np.cov(Q.T))
        pr = Q @ V[:, 0]
        minor.append(float(np.percentile(pr, 95) - np.percentile(pr, 5)))
    minor = np.array(minor)
    cnt = np.array(cnt)
    ex = ax["ext"]
    # the tip: the far end of the extent from the grip
    tip_sign = 1.0 if (ex[1] - tg) >= (tg - ex[0]) else -1.0
    shaft = minor[(np.abs(ks) >= 0.09 * H) & (cnt >= 8) & (np.sign(ks) == tip_sign)]
    shaft_minor = float(np.median(shaft)) if len(shaft) else 0.012 * H
    thr = max(0.02 * H, 1.7 * shaft_minor + 0.004 * H)
    cen_sel = np.abs(ks) <= 0.03 * H
    ic = int(np.nonzero(cen_sel)[0][np.argmax(minor[cen_sel])])
    centre_t = tg + float(ks[ic])
    edge = {}
    for sgn in (1, -1):
        e = None
        i = ic
        while 0 <= i + sgn < len(ks) and 0 <= i + 2 * sgn < len(ks):
            i += sgn
            if minor[i] < thr and minor[i + sgn] < thr:
                e = abs(float(ks[i]) - float(ks[ic])) - step / 2
                break
        if e is None:
            e = FIST_EDGE_MAX * H
        # a forearm or gauntlet beside the fist and a curved guard read as "fist" too: never further than
        # ~a hand breadth / 2 from the centre - a finger sliver on the item or a guard tip on the body both
        # ride the hand bone; a blade stub left on the body would bend
        edge[sgn] = float(np.clip(e, FIST_EDGE_MIN * H, FIST_EDGE_MAX * H))
    return {"grip_t": tg, "centre_t": centre_t, "tip_sign": tip_sign, "edge": edge, "threshold": thr,
            "shaft_minor": shaft_minor,
            "profile": [[round(float(k), 4), round(float(m), 4), int(n)] for k, m, n in zip(ks, minor, cnt)]}


def guard_extension(P_m, it, fw, H):
    """Samples of a guard / ornament right past the tip-side fist edge: per 0.01 H slab over 0.06 H, the 2D
    component (in the plane across the axis) that holds the axis point, when it is no wider than 0.16 H
    (a guard fused with a thigh or a tabard makes the component huge: then nothing is added)."""
    ax = it["axis"]
    c, d = ax["c"], ax["d"]
    t, perp, dist = axis_coords(P_m, c, d)
    R = perp_frame(d)
    q2 = perp @ R.T
    s = fw["tip_sign"]
    e0 = fw["centre_t"] + s * fw["edge"][s]
    out = np.zeros(len(P_m), dtype=bool)
    added = 0
    for k in range(6):
        a0 = e0 + s * k * 0.01 * H
        a1 = a0 + s * 0.01 * H
        lo, hi = min(a0, a1), max(a0, a1)
        idx = np.nonzero((t >= lo) & (t < hi) & (dist < 0.12 * H))[0]
        if len(idx) < 5:
            continue
        comps = hl.components_2d(q2, idx, [0, 1], 0.006 * H, 0)
        best = None
        for cp in comps:
            dmin = float(np.min(np.linalg.norm(q2[cp.idx], axis=1)))
            if dmin < max(ax["r_cap"], 0.012 * H) and (best is None or dmin < best[0]):
                best = (dmin, cp)
        if best is None:
            continue
        cp = best[1]
        if max(cp.ext(0), cp.ext(1)) > 0.16 * H:
            continue
        out[cp.idx] = True
        added += len(cp.idx)
    return out, added


def vote_vertices(verts, P_m, sample_masks, H, k=6, need=4):
    """Vertex labels from sample masks: the 6 nearest samples (within 0.006 H) vote; a vertex takes item i
    when >= need of them are in its mask (the largest count wins). -1 = body."""
    kd = kd_of(P_m)
    lab = np.full(len(verts), -1, dtype=np.int64)
    if not sample_masks:
        return lab
    M = np.stack(sample_masks, 1)
    lim = 0.006 * H
    for vi, v in enumerate(verts.tolist()):
        near = kd.find_n(v, k)
        if not near or near[0][2] > lim:
            continue
        ids = [i for _, i, _ in near]
        cnt = M[ids].sum(0)
        j = int(np.argmax(cnt))
        if cnt[j] >= need:
            lab[vi] = j
    return lab


def shield_face_normal(it, lm, H):
    """The plate normal oriented away from the body (the shield's face)."""
    n = unit(it["normal"])
    cen = np.asarray(it["centre"], dtype=np.float64)
    if lm is not None:
        try:
            ym = lm.trunk_at(float(cen[2]) / H)["ymid"] * H
        except Exception:
            ym = 0.0
        body_pt = np.array([lm.X0 * H, ym, cen[2]])
    else:
        body_pt = np.array([0.0, 0.0, cen[2]])
    return n if float((cen - body_pt) @ n) >= 0 else -n


def shield_axes(verts, n):
    """In-plane principal axes of a board's vertices: (centre, long axis (up-ish), short axis, half extents)."""
    c = verts.mean(0)
    rel = verts - c
    inp = rel - np.outer(rel @ n, n)
    w, V = np.linalg.eigh(np.cov(inp.T))
    a1 = unit(V[:, 2])
    if a1[2] < 0:
        a1 = -a1
    a2 = unit(np.cross(n, a1))
    e1 = float(np.percentile(np.abs(inp @ a1), 98))
    e2 = float(np.percentile(np.abs(inp @ a2), 98))
    return c, a1, a2, e1, e2, float(w[2] / max(w[1], 1e-12))


def grow_shield(body, verts, lab, i, it, H, lm=None, rings=60):
    """The plate mask of the rig (a slab +-0.07 H around the fitted plane, R95 radius) misses the rim and
    the tips of a bowed or kite-shaped board and can hold bits of the forearm behind it. The board is a
    thin smooth surface: a quadratic height field w = f(u, v) over the board plane is fitted to the voted
    vertices (trimmed twice, so forearm bits do not pull it), and the board = vertices within 0.025 H of
    that surface (both faces and the rim of a 2-4 cm board), within 1.6 x R95 of the centre in the plane,
    never in the holding forearm / hand tube (0.03 H around the bones when the rig's arms are valid).
    Voted vertices off the surface are dropped (the hand and forearm behind the board), then the board
    grows over mesh edges."""
    seed = np.nonzero(lab == i)[0]
    if len(seed) < 30:
        return 0
    # the board's own plane from the voted vertices (the detector's RANSAC plane can lean on a cape or arm)
    w_, V_ = np.linalg.eigh(np.cov((verts[seed] - verts[seed].mean(0)).T))
    if w_[0] < 0.25 * w_[1]:
        it["normal"] = unit(V_[:, 0])
    nf = shield_face_normal(it, lm, H)
    c, a1, a2, e1, e2, _ = shield_axes(verts[seed], nf)

    def uvw(X):
        rel = X - c
        return rel @ a1, rel @ a2, rel @ nf
    u, v, w = uvw(verts[seed])
    sc = max(e1, e2, 1e-3)

    def design(u_, v_):
        uu, vv = u_ / sc, v_ / sc
        return np.stack([np.ones_like(uu), uu, vv, uu * uu, uu * vv, vv * vv], 1)
    # the FRONT surface (the outermost layer along the face normal): refit on what lies in front of or on
    # the previous fit - the back face, the handle, hand and forearm behind fall away
    keep = np.ones(len(seed), dtype=bool)
    coef = None
    for _ in range(5):
        coef, *_ = np.linalg.lstsq(design(u[keep], v[keep]), w[keep], rcond=None)
        r = w - design(u, v) @ coef
        nk = r > -0.004 * H
        if nk.sum() < 20:
            break
        keep = nk
    U, Vv, W = uvw(verts)
    res = W - design(U, Vv) @ coef          # > 0: in front of the face, < 0: behind it
    rad = np.sqrt(U ** 2 + Vv ** 2)
    R = it["radius_m"]
    # behind the face: a thin board in the centre (the hand grips there), a thick / bowed rim outside
    depth = np.where(rad < 0.6 * R, 0.03 * H, 0.075 * H)
    ok = (res < 0.02 * H) & (res > -depth) & (rad < 1.6 * R) & ((lab < 0) | (lab == i))
    j = getattr(lm, "j", None) if lm is not None and lm.fail is None else None
    if j is not None:
        sd = it["side"]
        for a_, b_ in ((sd + "ForeArm", sd + "Hand"), (sd + "Arm", sd + "ForeArm")):
            dd, _ = hl._seg_dist(verts / H, np.asarray(j[a_]), np.asarray(j[b_]))
            ok &= dd > 0.03
    dropped = int(((lab == i) & ~ok).sum())
    lab[(lab == i) & ~ok] = -1
    ed = hl.mesh_edges(body)
    nb = [[] for _ in range(len(verts))]
    for a, b_ in ed.tolist():
        nb[a].append(b_)
        nb[b_].append(a)
    front = set(np.nonzero(lab == i)[0].tolist())
    added = 0
    for _ in range(rings):
        nxt = set()
        for vv_ in front:
            for uu_ in nb[vv_]:
                if lab[uu_] < 0 and ok[uu_]:
                    lab[uu_] = i
                    nxt.add(uu_)
        if not nxt:
            break
        added += len(nxt)
        front = nxt
    it["shield_seed_dropped"] = dropped
    it["shield_surface_fit"] = {"coef": [round(float(x), 5) for x in coef], "scale_m": round(sc, 4)}
    return added


def label_vertices(body, P_m, items, H, report, lm=None):
    """Final per-vertex item label (-1 = body) and the cut planes of every straight item."""
    verts = hl.mesh_verts(body)
    masks = []
    for i, it in enumerate(items):
        sm = it["sample_mask"].copy()
        if it.get("axis") is not None:
            fw = fist_window(P_m, it, H)
            it["fist"] = fw
            if it["grip_mode"] == "end":
                gx, n_added = guard_extension(P_m, it, fw, H)
                sm |= gx
                it["guard_samples"] = n_added
        masks.append(sm)
    lab = vote_vertices(verts, P_m, masks, H)
    # the rig's own vertex sets (bows, plates: exact) where the vote left a vertex unlabelled
    for i, it in enumerate(items):
        lab[(lab < 0) & it["vmask"]] = i
    for i, it in enumerate(items):
        ax = it.get("axis")
        if ax is None and it["kind"] == "shield":
            it["shield_grown"] = grow_shield(body, verts, lab, i, it, H, lm)
            continue
        if ax is None:
            if it["kind"] == "bow":
                # the fist on the riser stays body: near the grip only what the bow's own samples voted
                g = it["grip"]
                own = np.zeros(len(verts), dtype=bool)
                for k_, s_ in it["sources"]:
                    if k_ == "bow":
                        own |= s_["_vm"]
                close = np.linalg.norm(verts - g, axis=1) < 0.032 * H
                lab[(lab == i) & close & ~own] = -1
            continue
        fw = it["fist"]
        t, perp, dist = axis_coords(verts, ax["c"], ax["d"])
        c0 = fw["centre_t"]
        s = fw["tip_sign"]
        lo_e, hi_e = ax["ext"][0] - 0.02 * H, ax["ext"][1] + 0.02 * H
        caps = dist < ax["r_cap"] + 0.004 * H
        if it["grip_mode"] == "end":
            e = fw["edge"][s]
            item_side = (t - c0) * s > e
            planes = [(ax["c"] + ax["d"] * (c0 + s * e), ax["d"] * s)]
            # the butt side of an end grip (pommel in the wrist) stays with the body
            lab[(lab == i) & ~item_side & (dist < 0.15 * H)] = -1
        else:
            ep, em = fw["edge"][1], fw["edge"][-1]
            item_side = ((t - c0) > ep) | ((t - c0) < -em)
            planes = [(ax["c"] + ax["d"] * (c0 + ep), ax["d"].copy()), (ax["c"] + ax["d"] * (c0 - em), -ax["d"])]
            # a shaft grip: the fist (and the forearm leaving it sideways) is close to the shaft; banner cloth
            # beside the pole at the fist height stays with the item
            lab[(lab == i) & ~item_side & (dist < 0.06 * H)] = -1
        fill = caps & item_side & (t > lo_e) & (t < hi_e) & (lab < 0)
        lab[fill] = i
        it["planes"] = planes
    lab = smooth_labels(body, lab, len(items))
    lab, moved = absorb_fragments(body, lab, len(items), H, items=items)
    report["fragments_absorbed"] = moved
    for i, it in enumerate(items):
        it["vertices"] = int((lab == i).sum())
    return lab


def _main_body_kd(adj, ul, U):
    """KD-tree of the largest connected component of the unlabelled (body) vertices of the welded graph."""
    n = len(ul)
    seen = np.zeros(n, dtype=bool)
    best = []
    for st in range(n):
        if seen[st] or ul[st] >= 0:
            continue
        comp, stack = [], [st]
        seen[st] = True
        while stack:
            u = stack.pop()
            comp.append(u)
            for v, _ in adj[u]:
                if ul[v] < 0 and not seen[v]:
                    seen[v] = True
                    stack.append(v)
        if len(comp) > len(best):
            best = comp
    return kd_of(U[best]) if best else None


def absorb_fragments(body, lab, n_items, H, max_frac=0.004, near=0.12, items=None):
    """Body fragments that belong to an item: a small body-labelled patch whose every labelled neighbour is
    item i (a hole in a shield or a banner) and a small loose island next to item i (a finial, a pommel bead,
    a tassel) go to that item. Works on the position-welded vertex graph (glTF splits along UV seams)."""
    if n_items == 0:
        return lab, 0
    inv, U, adj = hl.welded_graph(body, H)
    nU = len(U)
    ul = np.full(nU, -1, dtype=np.int64)
    for v, u in enumerate(inv.tolist()):
        if lab[v] >= 0:
            ul[u] = lab[v]
    seen = np.zeros(nU, dtype=bool)
    moved = 0
    item_pts = {i: U[ul == i] for i in range(n_items)}
    kds = {i: kd_of(p) for i, p in item_pts.items() if len(p)}
    lim = max(20, int(max_frac * nU))
    body_kd = None
    for start in range(nU):
        if seen[start] or ul[start] >= 0:
            continue
        comp, stack, nbl = [], [start], set()
        seen[start] = True
        big = False
        while stack:
            u = stack.pop()
            comp.append(u)
            if len(comp) > lim:
                big = True
            for v, _ in adj[u]:
                if ul[v] >= 0:
                    nbl.add(int(ul[v]))
                elif not seen[v]:
                    seen[v] = True
                    stack.append(v)
        # every unlabelled neighbour was explored, so a component touching only item i is cut off from the rest of
        # the body by that item (a patch in a shield, the unlabelled rest of a finial island): up to 1.5 % of the
        # vertices; touching two items - only small patches
        if len(comp) > ISLAND_MAX_FRAC * nU or (big and len(nbl) > 1):
            continue
        target = None
        if len(nbl) == 1:
            target = next(iter(nbl))
            if big:
                # a bigger cut-off piece only at the far end of a pole / staff (a finial): a separate gauntlet or
                # pauldron shell next to the fist or behind a shield stays with the body
                it_t = (items or [None] * n_items)[target]
                far = False
                if it_t is not None and it_t.get("grip") is not None and (
                        it_t.get("kind") == "banner" or it_t.get("type") in LONG_TYPES):
                    far = float(np.linalg.norm(U[comp].mean(0) - np.asarray(it_t["grip"]))) > 0.3 * H
                if not far:
                    target = None
        elif not nbl and kds:
            # a loose island: to the nearest item when it is closer to it (within near x H) than to the body
            c = U[comp].mean(0)
            best = min(((kds[i].find(c.tolist())[2], i) for i in kds), default=None)
            if best is not None and best[0] < near * H:
                if body_kd is None:
                    body_kd = _main_body_kd(adj, ul, U)
                d_body = min((body_kd.find(q.tolist())[2] for q in U[comp][:: max(1, len(comp) // 20)]),
                             default=np.inf) if body_kd is not None else np.inf
                # a separate gauntlet / pauldron shell touches the body: it stays; a finial floats off it
                if best[0] < d_body:
                    target = best[1]
        if target is not None:
            ul[comp] = target
            moved += len(comp)
    if moved:
        lab = ul[inv]
    return lab, moved


def smooth_labels(body, lab, n_items, passes=2):
    """Drop isolated labelled vertices (fewer than 2 same-label neighbours) and fill unlabelled vertices
    surrounded by one label."""
    ed = hl.mesh_edges(body)
    nb = [[] for _ in range(len(lab))]
    for a, b_ in ed.tolist():
        nb[a].append(b_)
        nb[b_].append(a)
    lab = lab.copy()
    for _ in range(passes):
        new = lab.copy()
        for v in range(len(lab)):
            ls = [lab[u] for u in nb[v]]
            if not ls:
                continue
            if lab[v] >= 0:
                if sum(1 for x in ls if x == lab[v]) < 2:
                    new[v] = -1
            else:
                vals = set(ls)
                if len(vals) == 1 and ls[0] >= 0 and len(ls) >= 3:
                    new[v] = ls[0]
        lab = new
    return lab


# ----------------------------------------------------------------------------- the cut

def _cap_open_loops(bm, open_layer, uv_layer):
    """Close the boundary loops the separation opened (edges not flagged as originally open). Caps take the
    UV of a neighbouring loop at the same vertex (the rim colour), the neighbour's material, flat shading
    and the winding that agrees with the neighbour across the shared edge. Returns (cap faces, open
    seam edges left)."""
    seams = [e for e in bm.edges if e.is_boundary and not e[open_layer]]
    if not seams:
        return 0, 0
    res = bmesh.ops.holes_fill(bm, edges=seams, sides=0)
    caps = [f for f in res.get("faces", []) if f.is_valid]
    if caps:
        tri = bmesh.ops.triangulate(bm, faces=caps, quad_method="BEAUTY", ngon_method="BEAUTY")
        caps = [f for f in tri.get("faces", caps) if f.is_valid]
    capset = set(caps)
    flip = []
    for f in caps:
        f.smooth = False
        nbs = [l_.face for e in f.edges for l_ in e.link_loops if l_.face not in capset]
        if nbs:
            f.material_index = nbs[0].material_index
        if uv_layer is not None:
            for l_ in f.loops:
                src = [x for x in l_.vert.link_loops if x.face not in capset]
                if src:
                    l_[uv_layer].uv = src[0][uv_layer].uv
    # winding: across an edge shared with a non-cap face the two faces must run the edge in opposite
    # directions; caps of one loop are flipped together (decided by majority over their rim edges)
    done = set()
    for f in caps:
        if f in done:
            continue
        group, stack = [], [f]
        done.add(f)
        while stack:
            g = stack.pop()
            group.append(g)
            for e in g.edges:
                for l_ in e.link_loops:
                    h = l_.face
                    if h in capset and h not in done:
                        done.add(h)
                        stack.append(h)
        agree = disagree = 0
        for g in group:
            for l_ in g.loops:
                e = l_.edge
                for l2 in e.link_loops:
                    if l2.face is g or l2.face in capset:
                        continue
                    same = l2.vert == l_.vert       # same start vertex -> same direction -> inconsistent
                    if same:
                        disagree += 1
                    else:
                        agree += 1
        if disagree > agree:
            flip.extend(group)
    if flip:
        bmesh.ops.reverse_faces(bm, faces=flip)
    left = sum(1 for e in bm.edges if e.is_valid and e.is_boundary and not e[open_layer])
    return len(caps), left


def cut(body, lab, items, H):
    """Bisect the body along every item's cut planes (only faces near its axis), assign faces to items
    (all vertices labelled with the item or lying on one of its planes) and split: one new object per
    item, the body keeps the rest; both sides capped. Returns per-item objects (None when no face)."""
    me = body.data
    bm = bmesh.new()
    bm.from_mesh(me)
    lay = bm.verts.layers.int.new("af_lab")
    for v, l_ in zip(bm.verts, lab.tolist()):
        v[lay] = int(l_) + 1
    eps = 1e-5 * H
    bm.verts.ensure_lookup_table()
    t0 = time.time()
    for i, it in enumerate(items):
        ax = it.get("axis")
        for pc, pn in it.get("planes", []):
            pcv, pnv = Vector(pc.tolist()), Vector(unit(pn).tolist())
            faces = []
            for f in bm.faces:
                ds = [(v.co - pcv).dot(pnv) for v in f.verts]
                if min(ds) < -eps and max(ds) > eps:
                    cen = f.calc_center_median()
                    rel = np.array(cen) - ax["c"]
                    tt = rel @ ax["d"]
                    if np.linalg.norm(rel - tt * ax["d"]) < 0.15 * H:
                        faces.append(f)
            if not faces:
                continue
            geom = list({v for f in faces for v in f.verts}) + list({e for f in faces for e in f.edges}) + faces
            bmesh.ops.bisect_plane(bm, geom=geom, dist=eps, plane_co=pcv, plane_no=pnv)
    bm.verts.index_update()
    bm.faces.index_update()
    # on-plane vertices count for both sides of their item's planes
    on = {}
    for i, it in enumerate(items):
        if not it.get("planes"):
            continue
        ax = it["axis"]
        for v in bm.verts:
            rel = np.array(v.co) - ax["c"]
            tt = rel @ ax["d"]
            if np.linalg.norm(rel - tt * ax["d"]) > 0.16 * H:
                continue
            for pc, pn in it["planes"]:
                if abs(float((np.array(v.co) - pc) @ unit(pn))) <= 4 * eps:
                    on.setdefault(v.index, set()).add(i)
    face_lab = np.full(len(bm.faces), -1, dtype=np.int64)
    for f in bm.faces:
        ls = [v[lay] - 1 for v in f.verts]
        cand = {x for x in ls if x >= 0}
        if len(cand) != 1:
            continue
        i = cand.pop()
        ok = all((v[lay] - 1) == i or i in on.get(v.index, ()) for v in f.verts)
        if ok:
            face_lab[f.index] = i
    open_layer = bm.edges.layers.int.new("af_open")
    for e in bm.edges:
        e[open_layer] = 1 if (e.is_boundary or len(e.link_faces) > 2) else 0
    out = []
    uv_name = me.uv_layers.active.name if me.uv_layers.active else None
    for i, it in enumerate(items):
        sel = np.nonzero(face_lab == i)[0]
        if not len(sel):
            out.append((None, {"faces": 0}))
            continue
        b2 = bm.copy()
        b2.faces.ensure_lookup_table()
        keep = set(sel.tolist())
        bmesh.ops.delete(b2, geom=[f for j, f in enumerate(b2.faces) if j not in keep], context="FACES")
        bmesh.ops.delete(b2, geom=[v for v in b2.verts if not v.link_faces], context="VERTS")
        uvl = b2.loops.layers.uv.active
        ol = b2.edges.layers.int.get("af_open")
        caps, left = _cap_open_loops(b2, ol, uvl)
        for ly in (b2.edges.layers.int.get("af_open"), ):
            if ly is not None:
                b2.edges.layers.int.remove(ly)
        if b2.verts.layers.int.get("af_lab") is not None:
            b2.verts.layers.int.remove(b2.verts.layers.int.get("af_lab"))
        pme = bpy.data.meshes.new("prop_" + it["type"])
        b2.to_mesh(pme)
        b2.free()
        for m in me.materials:
            pme.materials.append(m)
        ob = bpy.data.objects.new("prop_%s_%s" % (it["hand"], it["type"]), pme)
        bpy.context.scene.collection.objects.link(ob)
        out.append((ob, {"faces": int(len(sel)), "cap_faces": caps, "open_seam_edges": left}))
    # the body: every item face removed, the cut openings capped
    item_faces = [f for f in bm.faces if face_lab[f.index] >= 0]
    bmesh.ops.delete(bm, geom=item_faces, context="FACES")
    bmesh.ops.delete(bm, geom=[v for v in bm.verts if not v.link_faces], context="VERTS")
    body_caps, body_left = _cap_open_loops(bm, open_layer, bm.loops.layers.uv.active)
    bm.edges.layers.int.remove(open_layer)
    bm.verts.layers.int.remove(bm.verts.layers.int.get("af_lab"))
    bm.to_mesh(me)
    bm.free()
    me.update()
    info = {"body_cap_faces": body_caps, "body_open_seam_edges": body_left, "seconds": round(time.time() - t0, 2),
            "uv": uv_name}
    return out, info


# ----------------------------------------------------------------------------- item frame, bridge, sockets

def ob_verts(ob):
    return hl.mesh_verts(ob)


def item_frame(it, ob, lm, H):
    """World (body-frame) grip frame of an item: (G, R 3x3 columns X, Y, Z). Also fills it["dims"]."""
    V = ob_verts(ob)
    if it["kind"] == "shield":
        w_, V_ = np.linalg.eigh(np.cov((V - V.mean(0)).T))
        if w_[0] < 0.25 * w_[1]:
            it["normal"] = unit(V_[:, 0])     # the extracted board's own plane
        n = shield_face_normal(it, lm, H)
        cen = V.mean(0)
        # +Z: the board's long axis (a kite / tower shield held tilted), upward; a round board: world up
        _, a1, _, _, _, aspect = shield_axes(V, n)
        up = np.array([0.0, 0.0, 1.0]) - n * n[2]
        if aspect > 1.3:
            up = a1
        elif np.linalg.norm(up) < 0.3:   # a board lying flat: "up" from the character front
            up = np.array([0.0, -1.0, 0.0]) - n * (-n[1])
        Z = unit(up)
        Y = -n
        X = unit(np.cross(Y, Z))
        Z = unit(np.cross(X, Y))
        R = np.stack([X, Y, Z], 1)
        loc = (V - cen) @ R
        lo, hi = loc.min(0), loc.max(0)
        mid = (lo + hi) * 0.5
        back = hi[1]             # +Y side of the board = the back (toward the wearer)
        G = cen + R @ np.array([mid[0], back + SHIELD_BACK_GRIP, mid[2]])
        it["dims"] = {"width_m": float(hi[0] - lo[0]), "thickness_m": float(hi[1] - lo[1]),
                      "height_m": float(hi[2] - lo[2])}
        return G, R
    Z = unit(it["zdir"])
    G = np.asarray(it["grip"], dtype=np.float64)
    if it.get("axis") is not None:
        ax = it["axis"]
        tg = it["fist"]["centre_t"] if it.get("fist") else float((G - ax["c"]) @ ax["d"])
        G = ax["c"] + ax["d"] * tg
    if it["kind"] == "bow":
        bw = it["bow"]
        tips = [np.asarray(x) * H for x in bw["tips"]]
        apex = np.asarray(it["grip"])
        yv = (0.5 * (tips[0] + tips[1])) - apex     # toward the string (kit bow: string on +Y)
        Y = unit(yv - Z * (yv @ Z))
        if np.linalg.norm(yv - Z * (yv @ Z)) < 1e-4:
            Y = unit(np.array([0.0, 1.0, 0.0]) - Z * Z[1])
    else:
        rel = V - G
        t = rel @ Z
        far = rel[t > 0.06 * H] if (t > 0.06 * H).sum() > 20 else rel
        perp = far - np.outer(far @ Z, Z)
        e = None
        if len(perp) > 20:
            w, Vv = np.linalg.eigh(np.cov(perp.T))
            if w[2] > 3.0 * max(w[1], 1e-12):
                e = unit(Vv[:, 2])
        if e is None:
            f = np.array([0.0, -1.0, 0.0])
            e = unit(f - Z * (f @ Z)) if np.linalg.norm(f - Z * (f @ Z)) > 0.2 else unit(np.cross(Z, [1.0, 0.0, 0.0]))
        if e @ np.array([0.0, -1.0, 0.0]) < 0:
            e = -e
        Y = -e                    # -Y = the edge
        Y = unit(Y - Z * (Y @ Z))
    X = unit(np.cross(Y, Z))
    Y = unit(np.cross(Z, X))
    R = np.stack([X, Y, Z], 1)
    loc = (V - G) @ R
    lo, hi = loc.min(0), loc.max(0)
    it["dims"] = {"length_m": float(hi[2] - lo[2]), "width_m": float(max(hi[0] - lo[0], hi[1] - lo[1])),
                  "thickness_m": float(min(hi[0] - lo[0], hi[1] - lo[1])),
                  "grip_to_far_end_m": float(hi[2]), "grip_to_near_end_m": float(-lo[2])}
    return G, R


def add_bridge(ob, it, G, R, H):
    """A handle through the fist (where the fist was cut away): a capped cylinder along the item's axis,
    hidden by the fist on the body, so the item is whole when it is swapped or seen alone. Its UV is the
    UV of the nearest item vertex (the cut colour)."""
    if it["kind"] == "shield":
        return None
    Z = R[:, 2]
    if it.get("axis") is not None and it.get("fist"):
        fw, ax = it["fist"], it["axis"]
        s = fw["tip_sign"]
        a_t = fw["centre_t"] + s * fw["edge"][s] + s * 0.005 * H
        if it["grip_mode"] == "end":
            b_t = fw["centre_t"] - s * 0.02 * H
        else:
            b_t = fw["centre_t"] - s * fw["edge"][-s] - s * 0.005 * H
        A = ax["c"] + ax["d"] * a_t
        B = ax["c"] + ax["d"] * b_t
        r = float(np.clip(0.0085 * H, 0.009, 0.022))
        if it["grip_mode"] == "shaft":
            r = float(np.clip(0.55 * max(fw["shaft_minor"], 0.012 * H), 0.009, 0.024))
    else:
        A, B = G - Z * 0.055, G + Z * 0.055
        r = 0.016
    seg = 10
    me = ob.data
    bm = bmesh.new()
    bm.from_mesh(me)
    axis = unit(B - A)
    u = unit(np.cross(axis, [0.0, 0.0, 1.0]) if abs(axis[2]) < 0.9 else np.cross(axis, [1.0, 0.0, 0.0]))
    w = np.cross(axis, u)
    ring_a, ring_b = [], []
    for k in range(seg):
        ang = 2 * math.pi * k / seg
        off = (u * math.cos(ang) + w * math.sin(ang)) * r
        ring_a.append(bm.verts.new((A + off).tolist()))
        ring_b.append(bm.verts.new((B + off).tolist()))
    faces = []
    for k in range(seg):
        k2 = (k + 1) % seg
        faces.append(bm.faces.new((ring_a[k], ring_a[k2], ring_b[k2], ring_b[k])))
    faces.append(bm.faces.new(list(reversed(ring_a))))
    faces.append(bm.faces.new(ring_b))
    bmesh.ops.recalc_face_normals(bm, faces=faces)
    uvl = bm.loops.layers.uv.active
    if uvl is not None:
        # UV of the nearest existing vertex (the cap at the cut: the rim colour)
        old = [v for v in bm.verts if v not in set(ring_a + ring_b)]
        if old:
            co = np.array([tuple(v.co) for v in old])
            j = int(np.argmin(np.linalg.norm(co - A, axis=1)))
            src = old[j].link_loops
            uv = tuple(src[0][uvl].uv) if src else (0.5, 0.5)
            for f in faces:
                for l_ in f.loops:
                    l_[uvl].uv = uv
    for f in faces:
        f.smooth = False
        f.material_index = 0
    bm.to_mesh(me)
    bm.free()
    me.update()
    return {"from": hl.rnd(A), "to": hl.rnd(B), "radius_m": round(r, 4)}


def two_handed_off(it, lm, G, R, H):
    """socket_grip_off of a two-handed item: the other hand's wrist when it lies on the item's axis
    (within 0.08 H), else the item_from_mesh rule for long items (staff / spear / banner > 1.15 m: 0.14 of
    the length below the main grip; greatsword: 11 cm below). Returns the local point or None."""
    Z = R[:, 2]
    other = "Right" if it["side"] == "Left" else "Left"
    j = getattr(lm, "j", None)
    L = it.get("dims", {}).get("length_m", 0.0)
    if j is not None and not lm.fail:
        w = np.asarray(j[other + "Hand"]) * H
        rel = w - G
        tt = float(rel @ Z)
        if np.linalg.norm(rel - tt * Z) < 0.08 * H and abs(tt) > 0.12:
            return tt, "other_wrist_on_axis"
    if it["type"] in LONG_TYPES and L > 1.15:
        return -0.14 * L, "long_item_rule"
    if it["type"] == "greatsword":
        return -0.11, "greatsword_rule"
    return None


# ----------------------------------------------------------------------------- colours

def base_color_image(ob):
    for slot in ob.material_slots:
        m = slot.material
        if m is None or not m.use_nodes:
            continue
        for n in m.node_tree.nodes:
            if n.type == "BSDF_PRINCIPLED":
                inp = n.inputs["Base Color"]
                if inp.is_linked and inp.links[0].from_node.type == "TEX_IMAGE":
                    img = inp.links[0].from_node.image
                    if img is not None:
                        return img
    return None


def face_colors(ob):
    """Per-face sRGB colour (texture at the face's mean UV, else the material colour) and face areas."""
    me = ob.data
    img = base_color_image(ob)
    nf = len(me.polygons)
    area = np.array([p.area for p in me.polygons], dtype=np.float64)
    col = np.full((nf, 3), 0.6)
    if img is not None and me.uv_layers.active is not None and img.size[0] > 0:
        w, h = img.size
        px = np.array(img.pixels[:], dtype=np.float32).reshape(h, w, 4)
        uv = np.empty(len(me.loops) * 2, dtype=np.float64)
        me.uv_layers.active.data.foreach_get("uv", uv)
        uv = uv.reshape(-1, 2)
        start = np.array([p.loop_start for p in me.polygons])
        total = np.array([p.loop_total for p in me.polygons])
        acc = np.zeros((nf, 2))
        for k in range(int(total.max()) if nf else 0):
            m = total > k
            acc[m] += uv[start[m] + k]
        acc /= np.maximum(total, 1)[:, None]
        xi = np.clip((acc[:, 0] % 1.0) * w, 0, w - 1).astype(int)
        yi = np.clip((acc[:, 1] % 1.0) * h, 0, h - 1).astype(int)
        col = px[yi, xi, :3].astype(np.float64)
        if img.colorspace_settings.name.lower() in ("linear", "linear rec.709", "non-color"):
            col = np.where(col <= 0.0031308, col * 12.92, 1.055 * np.power(np.clip(col, 0, 1), 1 / 2.4) - 0.055)
    else:
        for i, p in enumerate(me.polygons):
            m = ob.material_slots[p.material_index].material if ob.material_slots else None
            if m is not None:
                col[i] = list(m.diffuse_color[:3])
    return np.clip(col, 0, 1), area


def dominant_colors(ob, k=4, seed=1):
    """Area-weighted k-means of the face colours: [(sRGB, share)] largest first."""
    col, area = face_colors(ob)
    if len(col) == 0:
        return []
    rng = np.random.default_rng(seed)
    wts = area / max(area.sum(), 1e-12)
    k = min(k, len(col))
    cen = col[rng.choice(len(col), size=k, replace=False, p=wts if wts.sum() > 0 else None)]
    for _ in range(25):
        dd = np.linalg.norm(col[:, None, :] - cen[None], axis=2)
        a = np.argmin(dd, 1)
        for j in range(k):
            m = a == j
            if m.any():
                cen[j] = (col[m] * wts[m, None]).sum(0) / max(wts[m].sum(), 1e-12)
    a = np.argmin(np.linalg.norm(col[:, None, :] - cen[None], axis=2), 1)
    out = [(cen[j], float(wts[a == j].sum())) for j in range(k)]
    return sorted(out, key=lambda x: -x[1])


def _hsv(c):
    r, g, b_ = [float(x) for x in c]
    mx, mn = max(r, g, b_), min(r, g, b_)
    v = mx
    s = 0.0 if mx <= 1e-6 else (mx - mn) / mx
    if mx - mn < 1e-6:
        h = 0.0
    elif mx == r:
        h = ((g - b_) / (mx - mn)) % 6
    elif mx == g:
        h = (b_ - r) / (mx - mn) + 2
    else:
        h = (r - g) / (mx - mn) + 4
    return h * 60.0, s, v


def to_hex(c):
    return "#%02x%02x%02x" % tuple(int(round(float(np.clip(x, 0, 1)) * 255)) for x in c)


def palette_from_colors(cols, kind):
    """item_kit palette roles from the dominant colours of the extracted item: metal = the brightest
    low-saturation colour, metal_dark = a darker version, grip / wood = the darkest warm colour, trim =
    a gold/yellow one, accent = the most saturated."""
    if not cols:
        return {}
    info = [(np.asarray(c), sh) + _hsv(c) for c, sh in cols]
    pal = {}
    greys = [x for x in info if x[3] < 0.32]
    metal = max(greys, key=lambda x: x[4]) if greys else max(info, key=lambda x: x[4])
    pal["metal"] = to_hex(metal[0])
    pal["metal_dark"] = to_hex(metal[0] * 0.62)
    warm_dark = [x for x in info if (x[2] < 50 or x[2] > 330) and x[4] < 0.55]
    darkest = min(info, key=lambda x: x[4])
    g = min(warm_dark, key=lambda x: x[4]) if warm_dark else darkest
    pal["grip"] = to_hex(g[0])
    pal["wood"] = to_hex(g[0] if g[4] > 0.12 else g[0] * 1.6)
    gold = [x for x in info if 30 <= x[2] <= 65 and x[3] > 0.3 and x[4] > 0.35]
    if gold:
        pal["trim"] = to_hex(max(gold, key=lambda x: x[1])[0])
    sat = max(info, key=lambda x: x[3] * x[4])
    if sat[3] > 0.3:
        pal["accent"] = to_hex(sat[0])
    if kind == "shield":
        # the board face is the largest colour (kit shields paint the board with wood / accent)
        face = max(info, key=lambda x: x[1])
        pal["accent"] = to_hex(face[0])
        pal["wood"] = to_hex(face[0])
    return pal


# ----------------------------------------------------------------------------- gate

def mesh_quality(ob, H):
    """Open boundary edges, loose parts (welded) and their area shares."""
    bm = bmesh.new()
    bm.from_mesh(ob.data)
    bmesh.ops.remove_doubles(bm, verts=bm.verts, dist=1e-5 * H)
    edges = len(bm.edges)
    boundary = sum(1 for e in bm.edges if e.is_boundary)
    nonman = sum(1 for e in bm.edges if len(e.link_faces) > 2)
    bm.faces.ensure_lookup_table()
    seen = set()
    parts = []
    for f in bm.faces:
        if f.index in seen:
            continue
        stack, area = [f], 0.0
        seen.add(f.index)
        while stack:
            g = stack.pop()
            area += g.calc_area()
            for e in g.edges:
                for h in e.link_faces:
                    if h.index not in seen:
                        seen.add(h.index)
                        stack.append(h)
        parts.append(area)
    bm.free()
    tot = max(sum(parts), 1e-12)
    parts = sorted(parts, reverse=True)
    return {"edges": edges, "open_edges": boundary, "open_edge_frac": round(boundary / max(edges, 1), 4),
            "non_manifold_edges": nonman, "parts": len(parts),
            "parts_over_1pct": sum(1 for a in parts if a / tot > 0.01),
            "main_part_share": round(parts[0] / tot, 3) if parts else 0.0,
            "main2_part_share": round(sum(parts[:2]) / tot, 3) if parts else 0.0, "area_m2": round(tot, 4)}


GATE = {"open_edge_frac": 0.02, "main_part_share": 0.75, "parts_over_1pct": 6, "min_tris": 120}


def gate(ob, it, H, limits=None):
    """Quality gate of an extracted item: closed enough, not shredded, plausible size/shape for its type.
    Returns (ok, numbers, reasons)."""
    lim = dict(GATE, **(limits or {}))
    q = mesh_quality(ob, H)
    tris = hl.tri_count(ob)
    reasons = []
    if tris < lim["min_tris"]:
        reasons.append("only %d triangles" % tris)
    # banner cloth is an open sheet; a staff held mid-shaft or a bow held at the riser is two pieces by
    # construction (above and below the fist; the bridge handle is not counted)
    # (a bow string of a generation is a thin tube often open at the nocks: 4 %)
    open_lim = {"banner": 0.15, "bow": 0.04}.get(it["type"], lim["open_edge_frac"])
    if q["open_edge_frac"] > open_lim:
        reasons.append("open edges %.1f %% (limit %.0f %%)" % (100 * q["open_edge_frac"], 100 * open_lim))
    two = it.get("grip_mode") in ("shaft", "bow")
    share = q["main2_part_share"] if two else q["main_part_share"]
    q["pieces_expected"] = 2 if two else 1
    if share < lim["main_part_share"]:
        reasons.append("shredded: largest piece%s %.0f %% of the area (limit %.0f %%)" % (
            "s" if two else "", 100 * share, 100 * lim["main_part_share"]))
    if q["parts_over_1pct"] > lim["parts_over_1pct"]:
        reasons.append("%d pieces over 1 %% of the area (limit %d)" % (q["parts_over_1pct"], lim["parts_over_1pct"]))
    dims = it.get("dims", {})
    typ = it["type"]
    if typ == "shield":
        big = max(dims.get("width_m", 0), dims.get("height_m", 0))
        lo, hi = TYPE_LEN["shield"]
        if not lo <= big <= hi:
            reasons.append("shield %.2f m outside %.2f..%.2f m" % (big, lo, hi))
        if dims.get("thickness_m", 0) > 0.3 * max(big, 1e-6):
            reasons.append("shield %.2f m thick for %.2f m (not a board)" % (dims.get("thickness_m", 0), big))
        if min(dims.get("width_m", 0), dims.get("height_m", 0)) < 0.18:
            reasons.append("shield narrower than 0.18 m")
    else:
        L = dims.get("length_m", 0)
        lo, hi = TYPE_LEN.get(typ, (0.05, 3.5))
        if not lo <= L <= hi:
            reasons.append("%s %.2f m long outside %.2f..%.2f m" % (typ, L, lo, hi))
        wmax = {"sword": 0.42, "greatsword": 0.5, "dagger": 0.2, "staff": 0.55, "spear": 0.45, "wand": 0.2,
                "bow": 0.6, "mace": 0.45, "axe": 0.6, "battle_axe": 0.8, "halberd": 0.7, "warhammer": 0.5,
                "banner": 1.4, "crossbow": 1.0}.get(typ, 1.0)
        if dims.get("width_m", 0) > wmax:
            reasons.append("%s %.2f m wide (limit %.2f m: fused body parts?)" % (typ, dims.get("width_m", 0), wmax))
        if dims.get("grip_to_far_end_m", 1) < 0.05:
            reasons.append("nothing beyond the grip")
    nums = dict(q, tris=tris, dims={k: round(v, 3) for k, v in dims.items()})
    return not reasons, nums, reasons


# ----------------------------------------------------------------------------- kit replacement

def kit_params(it, style, ornament, seed, palette):
    """item_kit params for the extracted item's type with its measured sizes."""
    kt = KIT_TYPE.get(it["type"])
    if kt is None:
        return None
    d = it.get("dims", {})
    L = d.get("length_m", 0.0)
    p = {"type": kt, "style": style, "ornament": int(ornament), "seed": int(seed), "palette": palette,
         "preview": False}
    if kt in ("sword", "greatsword", "dagger"):
        if kt == "sword" and L > 1.3:
            kt = p["type"] = "greatsword"
        blade = max(0.12, d.get("grip_to_far_end_m", L) - 0.03)
        p["blade_length"] = round(float(np.clip(blade, 0.15, 1.5)), 3)
        w = it.get("blade_width_m")
        if w:
            p["blade_width"] = round(float(np.clip(w, 0.025, 0.1)), 3)
    elif kt in ("mace", "axe", "battle_axe", "warhammer"):
        p["haft_length"] = round(float(np.clip(L, 0.35, 1.6)), 3)
    elif kt in ("spear", "halberd", "staff"):
        p["shaft_length"] = round(float(np.clip(L, 0.9, 2.8)), 3)
    elif kt == "wand":
        p["length"] = round(float(np.clip(L, 0.2, 0.7)), 3)
    elif kt == "bow":
        p["length"] = round(float(np.clip(L, 0.7, 2.0)), 3)
    elif kt == "crossbow":
        p["size"] = round(float(np.clip(L / 0.84, 0.5, 1.6)), 3)
    elif kt == "shield":
        w, h = d.get("width_m", 0.6), d.get("height_m", 0.8)
        big, small = max(w, h), min(w, h)
        if big < 0.42:
            p["type"] = "buckler"
            p["radius"] = round(big / 2, 3)
        elif big / max(small, 1e-6) < 1.25:
            p["type"] = "shield_round"
            p["radius"] = round(big / 2, 3)
        elif big > 1.12:
            p["type"] = "shield_tower"
            p["width"], p["height"] = round(small, 3), round(big, 3)
        else:
            p["type"] = "shield_kite"
            p["width"], p["height"] = round(small, 3), round(big, 3)
    elif kt == "book":
        p.update({"height": round(float(np.clip(L, 0.12, 0.5)), 3)})
    elif kt == "torch":
        pass
    return p


def build_kit_item(params):
    """Build an item_kit item in the current scene: (object, info)."""
    from steps import item_kit as K
    ob, info = K.build_item(params)
    return ob, info


# ----------------------------------------------------------------------------- export helpers

def write_item(ob, sockets, it, out_dir, info, preview=True, size=320):
    """item.glb / item.blend / item.json (+ preview.png) of one item object with its socket children."""
    from steps import _items_lib as IL
    os.makedirs(out_dir, exist_ok=True)
    objs = [ob] + list(sockets)
    glb = af.export_glb(os.path.join(out_dir, "item.glb"), objects=objs, animations=False, skins=False)
    summ = IL.glb_summary(glb)
    info["glb"] = {"sockets": sorted(summ["sockets"]), "materials": summ["materials"], "bytes": summ["bytes"]}
    blend = os.path.join(out_dir, "item.blend")
    ids = set(objs) | {ob.data} | {m for m in ob.data.materials if m is not None}
    bpy.data.libraries.write(blend, ids, fake_user=True, compress=True)
    out = {"glb": glb, "blend": blend}
    if preview:
        views = ("front", "left", "three_quarter")
        out["preview"] = render_only(os.path.join(out_dir, "preview.png"), [ob], views=views, size=size,
                                     color="TEXTURE")
    with open(os.path.join(out_dir, "item.json"), "w", encoding="utf-8") as fh:
        json.dump(af._jsonable(info), fh, ensure_ascii=False, indent=2)
    out["json"] = os.path.join(out_dir, "item.json")
    return out


def render_only(path, objs, color="TEXTURE", **kw):
    """af.render_views with every other mesh hidden for the render (items and the body overlap in place)."""
    hidden = []
    for o in bpy.context.scene.objects:
        if o.type == "MESH" and o not in objs and not o.hide_render:
            o.hide_render = True
            hidden.append(o)
    try:
        return af.render_views(path, objs, color=color, **kw)
    finally:
        for o in hidden:
            o.hide_render = False


ITEM_COLOURS = [(0.9, 0.22, 0.18, 1.0), (0.2, 0.5, 0.95, 1.0), (0.95, 0.75, 0.1, 1.0), (0.25, 0.8, 0.3, 1.0)]


def closeup(path, objs, coloured, lo, hi, size=320, views=("front", "left", "three_quarter")):
    """Region render (box lo..hi) of objs with flat object colours: coloured {object: index} get
    ITEM_COLOURS, the rest light grey; other meshes hidden."""
    from steps import _items_lib as IL
    keep = {}
    for o in objs:
        keep[o] = tuple(o.color)
        o.color = ITEM_COLOURS[coloured[o] % len(ITEM_COLOURS)] if o in coloured else (0.78, 0.78, 0.76, 1.0)
    v, f = IL.box(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2], tuple(((np.asarray(lo) + np.asarray(hi)) / 2).tolist()))
    me = bpy.data.meshes.new("af_frame_box")
    me.from_pydata(v, [], f)
    fr = bpy.data.objects.new("af_frame_box", me)
    bpy.context.scene.collection.objects.link(fr)
    fr.hide_render = True
    hidden = []
    for o in bpy.context.scene.objects:
        if o.type == "MESH" and o not in objs and o is not fr and not o.hide_render:
            o.hide_render = True
            hidden.append(o)
    try:
        af.render_views(path, [fr], views=tuple(views), size=size, margin=1.0, color="OBJECT")
    finally:
        bpy.data.objects.remove(fr)
        bpy.data.meshes.remove(me)
        for o in hidden:
            o.hide_render = False
        for o, c in keep.items():
            o.color = c
    return path


def matrix_from(G, R):
    M = Matrix.Identity(4)
    for i in range(3):
        for j in range(3):
            M[i][j] = float(R[i, j])
        M[i][3] = float(G[i])
    return M


# ----------------------------------------------------------------------------- clip QA (after motion_style)


def _eval_mesh(ob):
    """Evaluated world-space vertices and triangles of an object (armature + modifiers applied)."""
    e = ob.evaluated_get(bpy.context.evaluated_depsgraph_get())
    me = e.to_mesh()
    M = np.array(e.matrix_world, dtype=np.float64)
    co = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3) @ M[:3, :3].T + M[:3, 3]
    me.calc_loop_triangles()
    tri = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("vertices", tri)
    e.to_mesh_clear()
    return co, tri.reshape(-1, 3)


_ENCLOSE_DIRS = [Vector(d) for d in ((1, 0, 0), (-1, 0, 0), (0, 1, 0), (0, -1, 0), (0, 0, 1), (0, 0, -1),
                                     (0.577, 0.577, 0.577), (-0.577, -0.577, 0.577))]


def _depth_inside(bvh, pts, max_d, enclose_m=0.6, min_hits=7):
    """Per point: distance to the nearest body face when the point is inside the body, else 0. Inside = behind the
    nearest face AND enclosed: rays in 8 directions hit the body within enclose_m in at least min_hits of them. The
    enclosure test keeps a sword behind an open cape or skirt (behind a thin sheet, free on the other side) from
    counting as buried in the body; one miss is tolerated for the holes of an AI mesh."""
    out = np.zeros(len(pts))
    for k, p in enumerate(pts.tolist()):
        hit = bvh.find_nearest(p, max_d)
        if hit[0] is None:
            continue
        loc, nrm, _, dist = hit
        v = Vector(p)
        if (v - loc).dot(nrm) >= 0:
            continue
        hits = 0
        for j, d in enumerate(_ENCLOSE_DIRS):
            if bvh.ray_cast(v, d, enclose_m)[0] is not None:
                hits += 1
            if hits + (len(_ENCLOSE_DIRS) - 1 - j) < min_hits:
                break
        if hits >= min_hits:
            out[k] = dist
    return out


def clip_props_qa(frames_per_clip=12, pen_tol_m=0.02, grip_tol_m=0.01, max_depth_m=0.1, samples=600):
    """Items in the hands over the final clips of the open scene (styled.blend). Items: meshes skinned 100 % to
    one hand bone with extras af_prop_item (attach_prop skin_separate), or objects under a mount socket
    (bone_child, extras af_attached_hand). Per clip, at sampled frames:
      grip drift - how far any item vertex is from where the hand bone carries its rest position (m): a rigid
                   item in the hand stays at 0;
      penetration - item vertices behind the nearest skinned-body face (inside), depth up to max_depth_m;
                    reported raw and as the increase over the rest pose at the same vertex (the item was put
                    back where the concept had it, touching the fused fist / forearm: only what the animation
                    adds counts against pen_tol_m; thin open cloth makes the raw "inside" test noisy).
    Returns {items, clips: {clip: {frames, max_grip_drift_m, max_penetration_m, max_added_penetration_m,
    frames_over_tolerance}}, tolerance}."""
    from mathutils.bvhtree import BVHTree
    from steps import _anim_lib as A
    scene = bpy.context.scene
    rigs = [o for o in scene.objects if o.type == "ARMATURE"]
    if not rigs:
        return {"error": "no armature"}
    rig = rigs[0]
    meshes = A.skinned_meshes(rig)
    items, bodies = [], []
    for ob in meshes:
        hand_vg = [g for g in ob.vertex_groups if g.name.endswith("Hand") and g.name.startswith("mixamorig:")]
        if ob.get("af_prop_item") and len(hand_vg) == 1:
            items.append((ob, hand_vg[0].name))
        else:
            bodies.append(ob)
    for ob in scene.objects:
        if ob.type != "MESH" or ob in meshes:
            continue
        p = ob
        while p is not None and p is not rig:
            if p.get("af_attached_hand"):
                items.append((ob, "mixamorig:%sHand" % ("Right" if str(p["af_attached_hand"]) == "r" else "Left")))
                break
            p = p.parent
    if not items or not bodies:
        return {"items": [o.name for o, _ in items], "clips": {}, "note": "no items or no body"}
    acts = A.rig_actions(rig, meshes)

    def body_bvh():
        bco, btri, off = [], [], 0
        for ob in bodies:
            co, tri = _eval_mesh(ob)
            bco.append(co)
            btri.append(tri + off)
            off += len(co)
        bco, btri = np.concatenate(bco), np.concatenate(btri)
        return BVHTree.FromPolygons(bco.tolist(), btri.tolist(), all_triangles=True)

    saved_act = rig.animation_data.action if rig.animation_data else None
    rig.data.pose_position = "REST"
    bpy.context.view_layer.update()
    bvh0 = body_bvh()
    rest = {}
    for ob, hand in items:
        bn = rig.data.bones.get(hand)
        if bn is None:
            continue
        co, _ = _eval_mesh(ob)
        pick = np.arange(0, len(co), max(1, len(co) // samples))
        Minv = np.array((rig.matrix_world @ bn.matrix_local).inverted(), dtype=np.float64)
        loc = co @ Minv[:3, :3].T + Minv[:3, 3]
        rest[ob.name] = (hand, loc, pick, _depth_inside(bvh0, co[pick], max_depth_m))
    rig.data.pose_position = "POSE"
    if rig.animation_data is None:
        rig.animation_data_create()
    report = {"items": sorted(rest), "clips": {},
              "rest_penetration_m": {k: round(float(v[3].max()), 4) if len(v[3]) else 0.0 for k, v in rest.items()}}
    for act in acts:
        rig.animation_data.action = act
        a0, a1 = [int(round(x)) for x in act.frame_range]
        frames = sorted({int(round(a0 + (a1 - a0) * k / max(frames_per_clip - 1, 1))) for k in range(frames_per_clip)})
        w_pen, w_add, w_grip, bad = 0.0, 0.0, 0.0, 0
        for f in frames:
            scene.frame_set(f)
            bpy.context.view_layer.update()
            bvh = body_bvh()
            frame_bad = False
            for ob, _ in items:
                if ob.name not in rest:
                    continue
                hand, loc, pick, pen0 = rest[ob.name]
                co, _ = _eval_mesh(ob)
                Mp = np.array(rig.matrix_world @ rig.pose.bones[hand].matrix, dtype=np.float64)
                carried = loc @ Mp[:3, :3].T + Mp[:3, 3]
                drift = float(np.max(np.linalg.norm(carried - co, axis=1)))
                pen = _depth_inside(bvh, co[pick], max_depth_m)
                add = float(np.max(pen - pen0)) if len(pen) else 0.0
                w_grip = max(w_grip, drift)
                w_pen = max(w_pen, float(pen.max()) if len(pen) else 0.0)
                w_add = max(w_add, add)
                if add > pen_tol_m or drift > grip_tol_m:
                    frame_bad = True
            bad += int(frame_bad)
        report["clips"][act.name] = {"frames": len(frames), "max_grip_drift_m": round(w_grip, 5),
                                     "max_penetration_m": round(w_pen, 4), "max_added_penetration_m": round(w_add, 4),
                                     "frames_over_tolerance": bad}
    rig.animation_data.action = saved_act
    report["tolerance"] = {"added_penetration_m": pen_tol_m, "grip_drift_m": grip_tol_m, "max_depth_m": max_depth_m}
    return report


def plate_rubber_qa(mask=None, frames_per_clip=12, limit=0.25):
    """Armour "rubber" over the final clips of the open scene: for the triangles whose vertices are all in mask
    (default: vertices skinned 100 % to one _humanoid_lib.HARD_BONES bone - the rigid plates of hard_surface), the
    rest-area share of triangles whose area changed by more than `limit` (25 %) at sampled frames of every clip.
    Returns ({clip: worst share}, mask) - pass the mask of a hard_surface build to measure the same plates of a
    smooth-weight build of the same body."""
    from steps import _anim_lib as A
    scene = bpy.context.scene
    rig = next(o for o in scene.objects if o.type == "ARMATURE")
    body = max((m for m in A.skinned_meshes(rig) if not m.get("af_prop_item")), key=lambda m: len(m.data.vertices))
    if mask is None:
        hard = {hl.b(n) for n in hl.HARD_BONES if "Hand" not in n}
        groups = {g.index: g.name for g in body.vertex_groups}
        mask = np.zeros(len(body.data.vertices), dtype=bool)
        for v in body.data.vertices:
            ws = [(g.weight, groups.get(g.group)) for g in v.groups if g.weight > 1e-4]
            if len(ws) == 1 and ws[0][0] > 0.999 and ws[0][1] in hard:
                mask[v.index] = True
    acts = A.rig_actions(rig, A.skinned_meshes(rig))
    rig.data.pose_position = "REST"
    bpy.context.view_layer.update()
    co0, tri = _eval_mesh(body)
    tin = mask[tri].all(1)
    T0 = co0[tri[tin]]
    A0 = np.linalg.norm(np.cross(T0[:, 1] - T0[:, 0], T0[:, 2] - T0[:, 0]), axis=1) * 0.5
    ok = A0 > 1e-9
    rig.data.pose_position = "POSE"
    out = {}
    for act in acts:
        rig.animation_data.action = act
        a0, a1 = [int(round(x)) for x in act.frame_range]
        worst = 0.0
        for f in sorted({int(round(a0 + (a1 - a0) * k / max(frames_per_clip - 1, 1))) for k in range(frames_per_clip)}):
            scene.frame_set(f)
            bpy.context.view_layer.update()
            co, _ = _eval_mesh(body)
            T1 = co[tri[tin]]
            A1 = np.linalg.norm(np.cross(T1[:, 1] - T1[:, 0], T1[:, 2] - T1[:, 0]), axis=1) * 0.5
            r = A1[ok] / A0[ok]
            bad = (r > 1 + limit) | (r < 1 / (1 + limit))
            worst = max(worst, float(A0[ok][bad].sum() / max(A0[ok].sum(), 1e-12)))
        out[act.name] = round(100 * worst, 2)
    return out, mask
