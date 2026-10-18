"""extract_props — cut held props (weapons, shields, banners, bows, staffs, books ...) out of a generated
humanoid so they stay rigid: the body is rigged alone, the items go back into the hands with attach_prop.

inputs.blend (the painted .blend of paint_from_concept; af_run opens it) or inputs.mesh.
params:
  height_m 1.8, front "auto", front_prior null ("-Y": the detector's back-to-front or uncertain answer
          loses to it - generated concepts face the camera), fix_yaw true, markers {}   (as rig_humanoid:
          the body is normalised the same way, so the rig frame barely moves)
  hints {} roles.json entry: hand_weapon ("sword", "banner + staff", "two swords"), hand (r | l | both),
          shield (bool), faction (order | undead -> kit style), build. Names the item types and lets two
          relaxed searches run (a pole the arm tracing missed, banner cloth); use_hints true
  replace "auto"   auto: an item failing the quality gate is rebuilt with item_kit (same type, measured
          length / width, colours from the extracted item); never: always the extracted mesh; always:
          always the kit item (for types without a kit equivalent - banner, skull, lantern - the extracted
          mesh stays)
  gate {}          override GATE limits (open_edge_frac 0.02, main_part_share 0.75, parts_over_1pct 6,
          min_tris 120)
  bridge true      a handle cylinder through the fist closes the item where the fist was cut away
  kit_style "auto" (undead -> dark, else medieval) | medieval | dark | elven; kit_ornament 1; seed (name)
  min_item_vertices 60   smaller detections are ignored
  preview true, preview_size 320
outputs: glb (body.glb: the body without the props + placement sockets socket_prop_<hand>_<n>), blend
(body.blend), props_json (props.json: every item with type, hand, source extracted|kit, sizes, tris, gate
numbers, placement matrix, files), items (item GLB paths), preview (exploded.png: assembled vs exploded,
textured), detect (detect.png: the labels on the original mesh).
Every item folder items/<id>/ holds item.glb, item.blend, item.json, preview.png: contract §6 sockets
socket_grip_main (grip frame), socket_grip_off (two-handed), socket_fx_tip (staff / wand / torch).
See docs/forge/extract_props.md.
"""
import json
import os
import time

import bpy
import numpy as np
from mathutils import Matrix, Vector

import af_common as af
from steps import _humanoid_lib as hl
from steps import _mesh_lib as ml
from steps import _props_lib as PL


def _seed_of(v):
    if v is None:
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    return int(sum((i + 1) * ord(ch) for i, ch in enumerate(str(v))) % 100000)


def _colour_labels(body, lab, n):
    """Vertex-colour attribute showing the labels (for detect.png)."""
    pal = [(0.95, 0.25, 0.2), (0.2, 0.55, 0.95), (0.95, 0.8, 0.15), (0.3, 0.85, 0.35), (0.8, 0.3, 0.9)]
    me = body.data
    ca = me.color_attributes.new("af_labels", "FLOAT_COLOR", "POINT")
    cols = np.tile(np.array([0.72, 0.72, 0.72, 1.0]), (len(me.vertices), 1))
    for i in range(n):
        cols[lab == i, :3] = pal[i % len(pal)]
    ca.data.foreach_set("color", cols.astype(np.float32).ravel())
    return ca


def run(req):
    t0 = time.time()
    os.makedirs(req["out_dir"], exist_ok=True)
    P = dict(req.get("params") or {})
    H = float(P.get("height_m", 1.8))
    warnings, checks = [], []
    hints = PL.parse_hints(P.get("hints"))
    replace = str(P.get("replace", "auto")).lower()
    if replace not in ("auto", "never", "always"):
        raise ValueError("params.replace must be auto, never or always")
    in_socks = []
    body, Ps, prep = hl.prepare_body(req, warnings, sockets=in_socks)
    af.log("prepared", facing=prep["facing"]["detected"], scale=prep["scale"])
    lm, Ps, items, report = PL.detect(body, Ps, prep, H, P, hints, warnings)
    af.log("detected", items=[(it["hand"], it["type"]) for it in items], rig_verdict=report.get("rig_verdict"))
    min_v = int(P.get("min_item_vertices", 60))
    lab = PL.label_vertices(body, Ps, items, H, report, lm) if items else np.full(len(body.data.vertices), -1)
    keep = [i for i, it in enumerate(items) if it["vertices"] >= min_v]
    if len(keep) != len(items):
        for i, it in enumerate(items):
            if i not in keep:
                warnings.append("detected %s in the %s hand has only %d vertices; left on the body" % (
                    it["type"], it["side"].lower(), it["vertices"]))
        remap = {old: new for new, old in enumerate(keep)}
        lab = np.array([remap.get(int(x), -1) for x in lab])
        items = [items[i] for i in keep]
    outputs = {}
    # detect.png: the labels on the normalised original
    orig = ml.duplicate(body, "original")
    if P.get("preview", True) and items:
        ca = _colour_labels(orig, lab, len(items))
        orig.data.color_attributes.active_color = ca
        try:
            orig.data.color_attributes.render_color_index = orig.data.color_attributes.active_color_index
        except Exception:
            pass
        outputs["detect"] = PL.render_only(af.out_path(req, "detect.png"), [orig], views=("front", "left", "three_quarter"),
                                            size=int(P.get("preview_size", 320)), color="VERTEX")
        orig.data.color_attributes.remove(ca)
    objs, cut_info = PL.cut(body, lab, items, H) if items else ([], {})
    af.log("cut", **{k: v for k, v in cut_info.items() if k != "uv"})
    style = P.get("kit_style", "auto")
    if style == "auto":
        style = "dark" if str(hints.get("faction") or "").lower() == "undead" else "medieval"
    seed = _seed_of(P.get("seed"))
    records, finals = [], []
    item_dir = os.path.join(req["out_dir"], "items")
    for k, (it, (ob, cinfo)) in enumerate(zip(items, objs)):
        rec = {"id": it["id"], "type": it["type"], "type_from": it.get("type_from"), "kind": it["kind"],
               "hand": it["hand"], "side": it["side"], "grip_mode": it.get("grip_mode"), "cut": cinfo,
               "vertices_on_body": it.get("vertices")}
        if ob is None:
            rec.update({"source": "none", "reason": "no face could be cut"})
            records.append(rec)
            warnings.append("%s (%s hand): no face could be cut" % (it["type"], it["side"].lower()))
            continue
        G, R = PL.item_frame(it, ob, lm, H)
        if it.get("axis") is not None and it.get("fist"):
            rec["fist"] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in it["fist"].items()}
            rec["fist"]["edge"] = {str(kk): round(vv, 4) for kk, vv in it["fist"]["edge"].items()}
            # blade width for the kit: the major spread of the item beyond the fist
            V = PL.ob_verts(ob)
            rel = V - G
            tt = rel @ R[:, 2]
            sel = rel[(tt > 0.15) & (tt < 0.8 * it["dims"]["length_m"])]
            if len(sel) > 20:
                pp = sel - np.outer(sel @ R[:, 2], R[:, 2])
                it["blade_width_m"] = float(np.percentile(np.abs(pp @ R[:, 1]), 95) * 2)
        ok, nums, reasons = PL.gate(ob, it, H, P.get("gate"))
        rec.update({"gate": {"ok": ok, "numbers": nums, "reasons": reasons}, "dims": it["dims"]})
        bridge = PL.add_bridge(ob, it, G, R, H) if P.get("bridge", True) else None
        rec["bridge"] = bridge
        cols = PL.dominant_colors(ob)
        palette = PL.palette_from_colors(cols, it["kind"])
        rec["colours"] = [{"srgb": PL.to_hex(c), "share": round(s, 3)} for c, s in cols]
        want_kit = replace == "always" or (replace == "auto" and not ok)
        kparams = PL.kit_params(it, style, P.get("kit_ornament", 1), seed + k, palette) if want_kit else None
        if want_kit and kparams is None:
            warnings.append("%s (%s hand) %s, but item_kit has no %s: the extracted mesh is kept" % (
                it["type"], it["side"].lower(), "failed the gate (" + "; ".join(reasons) + ")" if not ok else "replace=always",
                it["type"]))
        M_place = PL.matrix_from(G, R)
        sockets = []
        if kparams is not None:
            kob, kinfo = PL.build_kit_item(kparams)
            kob.name = "item_%s" % it["id"]
            sockets = list(kob.children)
            src_ob = ob
            final = kob
            rec.update({"source": "kit", "kit": {"params": kparams, "tris": kinfo["tris"],
                                                  "dimensions_m": kinfo["dimensions_m"]},
                        "replaced_because": reasons or ["replace=always"]})
            bpy.data.objects.remove(src_ob, do_unlink=True)
            ob = None
            gm = next((s for s in sockets if s.get("game_socket") == "socket_grip_main"), None)
            G_loc = Matrix(gm.matrix_local) if gm is not None else Matrix.Identity(4)
            item_info = dict(kinfo, source="kit", extracted_from=it["id"], placement_matrix=[list(r) for r in M_place])
        else:
            # extracted mesh into the item frame: grip frame = item axes, the item standing on z = 0
            Rm = Matrix([[float(R[i, j]) for j in range(3)] for i in range(3)]).to_4x4()
            T = Rm.transposed() @ Matrix.Translation(Vector((-G).tolist()))
            ob.data.transform(T)
            ob.data.update()
            V = PL.ob_verts(ob)
            lo, hi = V.min(0), V.max(0)
            shift = Vector((-(lo[0] + hi[0]) / 2, -(lo[1] + hi[1]) / 2, -lo[2]))
            ob.data.transform(Matrix.Translation(shift))
            ob.data.update()
            ob.name = "item_%s" % it["id"]
            ob["af_item_type"] = it["type"]
            ob["af_item_source"] = "extracted"
            ob["af_forward"] = "-Y"
            sockets.append(PL_socket(ob, "socket_grip_main", shift, kind="grip",
                                     frame="grip: +Z thumb side / along shaft, -Y edge-face"))
            off = PL.two_handed_off(it, lm, G, R, H)
            if off is not None:
                sockets.append(PL_socket(ob, "socket_grip_off", shift + Vector((0, 0, off[0])), kind="grip",
                                         rule=off[1]))
            if it["type"] in PL.FX_TIP_TYPES:
                top = float(hi[2] - lo[2])
                sockets.append(PL_socket(ob, "socket_fx_tip", Vector((0.0, 0.0, top)), kind="fx", forward="+Z"))
            G_loc = Matrix.Translation(shift)
            final = ob
            Vf = PL.ob_verts(ob)
            item_info = {"type": it["type"], "source": "extracted", "id": it["id"], "hand": it["hand"],
                         "tris": hl.tri_count(ob), "dimensions_m": [round(float(x), 4) for x in (Vf.max(0) - Vf.min(0))],
                         "gate": rec["gate"], "colours": rec["colours"], "placement_matrix": [list(r) for r in M_place],
                         "sockets": [s.name for s in sockets], "axes": {"up": "+Z", "main_axis": "+Z", "edge_or_face": "-Y"}}
            rec["source"] = "extracted"
        bpy.context.view_layer.update()
        outs = PL.write_item(final, sockets, it, os.path.join(item_dir, it["id"]), item_info,
                             preview=bool(P.get("preview", True)), size=int(P.get("preview_size", 320)))
        rec.update({"files": outs, "tris": hl.tri_count(final), "placement_matrix": [[round(x, 6) for x in r] for r in M_place],
                    "grip_m": hl.rnd(G), "z_axis": hl.rnd(R[:, 2], 4), "y_axis": hl.rnd(R[:, 1], 4)})
        # the placement socket on the body: rig_humanoid keeps it (child of af_bone), attach_prop refits onto it
        n_same = sum(1 for r in records if r.get("hand") == it["hand"] and r.get("placement_socket")) + 1
        sname = "socket_prop_%s_%d" % (it["hand"], n_same)
        ps = af.add_socket(sname, kind="prop", size=0.05, af_bone=it.get("bone") or it["side"] + "Hand", af_hand=it["hand"],
                           af_item=it["id"], af_item_kind=it["kind"], af_item_type=it["type"],
                           af_grip_mode=str(it.get("grip_mode") or "shaft"))
        ps.rotation_mode = "QUATERNION"
        ps.matrix_world = M_place
        rec["placement_socket"] = sname
        # for the previews: the item back at its place on the body (socket_grip_main on the placement)
        final.matrix_world = M_place @ G_loc.inverted()
        finals.append((final, sockets, rec))
        records.append(rec)
        af.log("item", id=it["id"], source=rec["source"], tris=rec["tris"], gate=rec["gate"]["ok"])
    bpy.context.view_layer.update()
    # input sockets of earlier steps (eyes of replace_part ...) follow the body
    if in_socks:
        ml.restore_sockets(in_socks, transform=prep.get("matrix"))
    # previews: original | assembled | exploded
    if P.get("preview", True):
        size = int(P.get("preview_size", 320))
        rows, labels = [], []
        tmp = os.path.join(req["out_dir"], "_preview")
        os.makedirs(tmp, exist_ok=True)
        rows.append(PL.render_only(os.path.join(tmp, "orig.png"), [orig], views=("front", "three_quarter"), size=size,
                                    color="TEXTURE"))
        labels.append("ORIGINAL (FUSED)")
        orig.hide_render = True
        allobjs = [body] + [f for f, _, _ in finals]
        rows.append(PL.render_only(os.path.join(tmp, "assembled.png"), allobjs, views=("front", "three_quarter"),
                                    size=size, color="TEXTURE"))
        labels.append("BODY + %d ITEMS" % len(finals))
        bc = np.array(hl.mesh_verts(body).mean(0))
        moved = []
        for f, _, rec in finals:
            V = PL.ob_verts(f)
            d = V.mean(0) - bc
            d[2] = 0.0
            d = PL.unit(d) if np.linalg.norm(d) > 1e-3 else np.array([1.0, 0.0, 0.0])
            off = Matrix.Translation(Vector((d * 0.45 * H / 1.8).tolist()))
            moved.append((f, f.matrix_world.copy()))
            f.matrix_world = off @ f.matrix_world
        rows.append(PL.render_only(os.path.join(tmp, "exploded.png"), allobjs, views=("front", "three_quarter"),
                                    size=size, color="TEXTURE"))
        labels.append("EXPLODED: " + ", ".join("%s %s" % (r["id"], r["source"]) for _, _, r in finals)[:60])
        for f, mw in moved:
            f.matrix_world = mw
        from steps import _items_lib as IL
        outputs["preview"] = IL.stack_rows(rows, af.out_path(req, "exploded.png"), labels=labels)
        # grips.png: every item's grip close-up (body grey, item coloured) - the cut at the fist
        crow, clab = [], []
        for k, (f, _, rec) in enumerate(finals):
            g = np.array(rec["grip_m"])
            r_ = 0.2 * H / 1.8 if rec["kind"] != "shield" else 0.4 * H / 1.8
            crow.append(PL.closeup(os.path.join(tmp, "grip_%d.png" % k), allobjs, {f: k}, g - r_, g + r_, size=size))
            clab.append("%s (%s) GRIP" % (rec["id"].upper(), rec["source"].upper()))
        if crow:
            outputs["grips"] = IL.stack_rows(crow, af.out_path(req, "grips.png"), labels=clab)
    # the body file: body + placement sockets (+ kept input sockets); items live in their own files
    for f, socks, _ in finals:
        for s in socks:
            bpy.data.objects.remove(s, do_unlink=True)
        bpy.data.objects.remove(f, do_unlink=True)
    bpy.data.objects.remove(orig, do_unlink=True)
    bpy.context.view_layer.update()
    sock_objs = [o for o in bpy.context.scene.objects if ml.is_socket(o)]
    outputs["glb"] = af.export_glb(af.out_path(req, "body.glb"), objects=[body] + sock_objs, animations=False, skins=False)
    outputs["blend"] = af.save_blend(af.out_path(req, "body.blend"))
    doc = {"items": records, "hints": hints, "detector": {k: v for k, v in report.items() if k != "hints"},
           "cut": cut_info, "body": {"glb": outputs["glb"], "blend": outputs["blend"], "tris": hl.tri_count(body)},
           "prepare": {"facing": prep["facing"], "scale": prep["scale"], "turn_deg": prep.get("turn_deg")},
           "replace": replace, "kit_style": style}
    outputs["props_json"] = af.out_path(req, "props.json")
    with open(outputs["props_json"], "w", encoding="utf-8") as fh:
        json.dump(af._jsonable(doc), fh, ensure_ascii=False, indent=2)
    outputs["items"] = [r["files"]["glb"] for r in records if r.get("files")]
    checks.append(af.check("body_faces", len(body.data.polygons) > 100, len(body.data.polygons), ">100"))
    expected = sum(len(v) for v in hints["by_side"].values()) + (1 if hints.get("shield_side") else 0)
    written = len(outputs["items"])
    checks.append(af.check("items_vs_hints", written >= expected, written, expected, severity="warning",
                           note="items written vs held items named by params.hints (books at the belt, sheathed "
                                "swords are not held)"))
    for r in records:
        if r.get("gate"):
            checks.append(af.check("gate_%s" % r["id"], r["gate"]["ok"] or r["source"] == "kit",
                                   {"source": r["source"], "reasons": r["gate"]["reasons"]}, "extracted passes or kit",
                                   severity="warning"))
    stats = {"items": [{"id": r["id"], "type": r["type"], "hand": r["hand"], "source": r.get("source"),
                        "tris": r.get("tris"), "dims": {k: round(v, 3) for k, v in (r.get("dims") or {}).items()},
                        "gate_ok": (r.get("gate") or {}).get("ok"), "gate_reasons": (r.get("gate") or {}).get("reasons")}
                       for r in records],
             "rig_verdict_on_fused_mesh": report.get("rig_verdict"), "body_tris": hl.tri_count(body),
             "detector": {k: report.get(k) for k in ("props", "bows", "plates", "not_held", "hint_pole_extension_h")},
             "facing": prep["facing"]["detected"], "seconds": round(time.time() - t0, 1)}
    return {"outputs": outputs, "stats": stats, "checks": checks, "warnings": warnings}


def PL_socket(ob, name, loc, kind=None, **extras):
    from steps import _items_lib as IL
    return IL.add_item_socket(ob, name, loc, None, kind=kind, size=0.04, **extras)
