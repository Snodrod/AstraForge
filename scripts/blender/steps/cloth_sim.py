"""cloth_sim - real cloth for the loose garments of a styled character (docs/forge/cloth_sim.md).

Runs after motion_style on its styled.blend. Robes, skirts, tabards, capes, hoods, wide sleeves and banner cloth
of generated characters are skinned like the body (they stretch, slide and swim in walk / attack / death); this
step segments them, splits them into cloth objects, simulates every clip with Blender's cloth against body
capsules, held poles and the floor, and bakes a per-clip cache that render_sprites switches together with the clip.
For the GLB (Godot, any engine) the same motion is carried by secondary cloth-bone chains.

inputs.blend    styled.blend of motion_style (af_run opens it); clips.json next to it gives loops / categories
params (all optional):
  cloth          'auto' (default) | false (pass-through) | [{"kind": robe|skirt|tabard|back_panel|cape|hood|sleeve|
                 banner|none, "min": [x,y,z], "max": [x,y,z], "only": bool}] region boxes in the bind pose (metres)
  build          roles.json build hint: robed (more eager robe/sleeve detection) | armored | heavy | ...
  leg_margin, hood, sleeves ('auto' = robed characters only), banners    segmentation switches (_cloth_lib.segment)
  presets        {group or region kind: preset name | {base: preset, mass: ..., bending: ...}}; groups: lower, hood,
                 sleeve_l, sleeve_r, banner_l, banner_r; presets: wool_robe, heavy_cape, tabard, light_silk
  clips          'all' (default) or [action names / names without -loop]: clips that get a simulation (others
                 stay skinned in the sprites and get spring chains in the GLB)
  remesh         'auto' (default: true when a garment has more than remesh_above = 12000 vertices) | false: simulate
                 the garment mesh itself (riding caps excluded) | true: a QuadriFlow proxy of proxy_edge_m (0.045)
                 carried onto the garment by embedded deformation (faster, but flaps and layers that the remesh smooths
                 over tear on the garment)
  quality        None: override the preset's solver steps per frame
  preroll        20 frames holding the first pose before each clip (the cloth starts settled)
  from_bind      0: frames easing from the bind pose into the first pose before the hold (Blender cloth takes its
                 spring rest lengths from the first simulated frame; 0 = start in the first pose)
  caps           'delete' (default): the disc / underside closing a robe's hem is removed from the garment (it
                 would stand up like a lid once the hem moves); 'ride': kept and carried by the simulated hem
  pelvis_follow  true: the free part of the lower garment hangs from the pelvis (leg skin weights moved to Hips in
                 proportion to its freedom): the legs push it by collision instead of tearing it apart
  inflate_m      0.006: collision capsules are this much bigger than the anthropometric body
  collider_friction 1.0: friction of the body capsules (low: legs push a robe, they do not drag it up)
  collide_props  true: poles held in a hand (staff, banner pole, spear) collide with the cloth
  shape_hold     None (preset: robe 0.45, cape 0.3, tabard 0.35, silk 0.1): minimum pin weight of the free cloth - a
                 soft pull toward its pelvis-carried shape that keeps the modelled silhouette; it fades out within
                 0.2 m of the floor and is off in deaths (the garment falls and settles freely)
  max_disp_m     1.2: a simulated point farther than this from the body capsules (or NaN) = exploded clip (one
                 retry with doubled steps and damping, then the clip falls back to skinning with a warning)
  qa_every       2: QA every n-th clip frame; qa_gate 'warning' (default) | 'error' for the cloth QA checks;
                 penetration_limit 0.03 (share of free cloth points > 1 cm inside the body, worst frame, and never
                 worse than the skinned cloth + 1 %), stretch_limit 1.6 (99th percentile edge stretch)
  cloth_bones    true: add cloth-bone chains to the GLB (bones cloth_<group>_<chain>_<joint>)
  bone_spacing_m 0.2, bone_joints 4, bone_chains_max 16: chain spacing along the attachment line, joints per
                 chain, chains per garment
  fit_weights    true: the GLB weights are fitted (least squares, 4 influences) to the simulated garment
  death_morph    true: a corrective morph af_cloth_settle (driven by a Hips property the death ramps 0 -> 1) puts the
                 GLB garment exactly onto the simulated one at the end of the death (bones alone swing a lying robe
                 like a fan)
  time_budget_s  300: warning when the step takes longer
  preview        true: preview.png (skinned vs cloth rows for walk / attack-cast / death) and segmentation.png
outputs: blend (cloth.blend: Body without the garments; Body_cloth_<group> garments with their Armature modifier and
per-clip shape-key caches (af_cloth_cache = {armature action: shape-key action}; render_sprites switches them with
the clip through _export_lib.bind_cloth_cache); hidden Body_glb = the whole body with cloth-bone weights), glb
(cloth.glb: rig + cloth bones, the same clip names), clips (clips.json of motion_style + per clip cloth metrics),
report (cloth.json: segmentation, groups, per clip and garment QA cloth vs original skinning, timing), preview,
segmentation.
"""
import json
import os
import time

import bpy
import numpy as np

import af_common as af

try:
    from steps import _anim_lib as A
    from steps import _cloth_lib as CL
    from steps import _export_lib as xl
except ImportError:  # pragma: no cover
    import _anim_lib as A
    import _cloth_lib as CL
    import _export_lib as xl


def _clip_meta(blend_path):
    if not blend_path:
        return {}, None
    cj = os.path.join(os.path.dirname(os.path.abspath(blend_path)), "clips.json")
    if not os.path.exists(cj):
        return {}, None
    try:
        with open(cj, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception:
        return {}, None
    return {r.get("gltf_name"): r for r in (doc.get("clips") or []) if r.get("gltf_name")}, doc


def _clip_info(act, meta):
    n = int(round(act.frame_range[1] - act.frame_range[0])) + 1
    m = meta.get(act.name) or {}
    loop = bool(m.get("loop", act.name.endswith("-loop") or act.use_cyclic))
    cat = m.get("category") or A.guess_category(act.name[:-5] if act.name.endswith("-loop") else act.name)
    return n, loop, cat


def _seg_png(path, body, seg):
    """Workbench views of the bind pose: garments coloured by region, pinned parts paler."""
    pal = {"robe": (0.85, 0.25, 0.2), "skirt": (0.9, 0.5, 0.15), "tabard": (0.2, 0.45, 0.95),
           "back_panel": (0.1, 0.7, 0.85), "cape": (0.6, 0.25, 0.9), "hood": (0.2, 0.75, 0.3),
           "sleeve": (0.9, 0.85, 0.2), "banner": (0.9, 0.3, 0.7)}
    me = body.data
    attr = me.color_attributes.new("af_cloth_seg", "FLOAT_COLOR", "POINT")
    c = np.full((len(me.vertices), 4), 0.72)
    c[:, 3] = 1.0
    for j, k in enumerate(seg["kinds"]):
        m = seg["kind"] == j
        if m.any():
            base = np.array(pal[k])
            p = seg["pin"][m][:, None]
            c[m, :3] = base * (1 - 0.55 * p) + 0.55 * 0.92 * p
    c[seg["cap"], :3] = (0.35, 0.1, 0.1)
    attr.data.foreach_set("color", c.ravel())
    old = me.color_attributes.active_color
    me.color_attributes.active_color = attr
    shown = [(m, m.show_render) for m in body.modifiers]
    arm = next((m.object for m in body.modifiers if m.type == "ARMATURE" and m.object), None)
    pos = arm.data.pose_position if arm else None
    if arm:
        arm.data.pose_position = "REST"
    try:
        af.render_views(path, objects=[body], views=("front", "left", "back", "three_quarter"), size=360,
                        color="VERTEX")
    finally:
        if arm:
            arm.data.pose_position = pos
        if old is not None:
            me.color_attributes.active_color = old
        me.color_attributes.remove(me.color_attributes["af_cloth_seg"])
        for m, s in shown:
            m.show_render = s
    return path


def _export_glb(req, rig, body_glb, body_render, keep_actions):
    """GLB of the rig (with cloth bones), Body_glb (named Body inside the file) and the sockets."""
    exp, notes = A.export_objects(rig, [body_glb])
    exp = [o for o in exp if o.get(CL.CLOTH_ROLE) in (None, "glb") and o is not body_render]
    old_name = body_render.name
    body_render.name = "Body_render_tmp"
    glb_name = body_glb.name
    body_glb.name = "Body"
    body_glb.hide_viewport = False
    body_glb.hide_set(False)
    try:
        with A.gltf_only_actions(keep_actions, exp, rig) as only:
            path = af.export_glb(af.out_path(req, "cloth.glb"), objects=exp, animations=True, skins=True,
                                 animation_mode="ACTIONS", morph=body_glb.data.shape_keys is not None)
        notes += only.notes
    finally:
        body_glb.name = glb_name
        body_render.name = old_name
        body_glb.hide_render = True
        body_glb.hide_set(True)
    return path, notes


def run(req):
    T0 = time.time()
    P, I = req["params"], req.setdefault("inputs", {})
    warnings, checks, timing = [], [], {}
    blend = I.get("blend")
    if blend:
        if os.path.normcase(os.path.abspath(bpy.data.filepath or "")) != os.path.normcase(os.path.abspath(blend)):
            af.open_blend(blend)
    else:
        raise ValueError("inputs.blend (styled.blend of motion_style) is required")
    gate = str(P.get("qa_gate", "warning")).lower()
    if gate not in ("warning", "error"):
        raise ValueError("qa_gate must be warning or error")
    scene = bpy.context.scene
    fps = float(scene.render.fps) / float(scene.render.fps_base or 1.0)
    rig, scheme, tmap = A.find_target_rig(list(scene.objects))
    meshes = [o for o in A.skinned_meshes(rig) if o.get(CL.CLOTH_ROLE) is None]
    if not meshes:
        raise ValueError("no skinned mesh on %s" % rig.name)
    body = max(meshes, key=lambda o: len(o.data.vertices))
    if rig.animation_data:
        rig.animation_data.action = None
    meta, clips_doc = _clip_meta(blend)
    acts = [a for a in A.rig_actions(rig, meshes) if not a.name.startswith("AF_")]
    order = [r.get("gltf_name") for r in (clips_doc or {}).get("clips", [])]
    acts.sort(key=lambda a: (order.index(a.name) if a.name in order else 999, a.name))
    if not acts:
        raise ValueError("the rig %s has no clips (run retarget / motion_style first)" % rig.name)
    want = P.get("clips", "all")
    if isinstance(want, str) and want != "all":
        want = [want]

    def wanted(a):
        if want == "all":
            return True
        base = a.name[:-5] if a.name.endswith("-loop") else a.name
        return a.name in want or base in want

    # ------------------------------------------------------------ segmentation
    t = time.time()
    sk = CL.Skeleton(rig)
    seg_p = {k: P[k] for k in ("cloth", "build", "leg_margin", "hood", "sleeves", "banners") if k in P}
    seg = CL.segment(body, sk, seg_p)
    names, groups = CL.garment_groups(seg, sk, P.get("presets"))
    timing["segment_s"] = round(time.time() - t, 2)
    outputs = {}
    if P.get("preview", True):
        try:
            outputs["segmentation"] = _seg_png(af.out_path(req, "segmentation.png"), body, seg)
        except Exception as exc:
            warnings.append("segmentation preview failed: %s" % exc)
    report = {"segmentation": seg["stats"], "groups": {g: {k: v for k, v in d.items() if k != "settings"}
                                                       for g, d in groups.items()},
              "presets": {g: d["settings"] for g, d in groups.items()}, "clips": {}}
    checks.append(af.check("cloth_regions", bool(groups), {g: d["vertices"] for g, d in groups.items()}, ">=1 garment",
                           severity="warning", note="loose garments found (none = the step passes the scene through)"))
    clip_recs = []
    if not groups:
        warnings.append("no loose garment found: scene passed through unchanged (skinned clothing)")
        exp, notes = A.export_objects(rig, meshes)
        with A.gltf_only_actions(acts, exp, rig) as only:
            outputs["glb"] = af.export_glb(af.out_path(req, "cloth.glb"), objects=exp, animations=True, skins=True,
                                           animation_mode="ACTIONS",
                                           morph=any(o.type == "MESH" and o.data.shape_keys is not None for o in exp))
        warnings += notes + only.notes
        outputs["blend"] = af.save_blend(af.out_path(req, "cloth.blend"))
        return _finish(req, outputs, report, clips_doc, clip_recs, checks, warnings, timing, T0, P)

    # ------------------------------------------------------------ split, proxies, colliders
    t = time.time()
    body_glb = None
    if P.get("cloth_bones", True):
        body_glb = body.copy()
        body_glb.data = body.data.copy()
        body_glb.name = "Body_glb"
        body_glb.data.name = "Body_glb"
        for c in body.users_collection:
            c.objects.link(body_glb)
        body_glb[CL.CLOTH_ROLE] = "glb"
        if body_glb.data.shape_keys is not None:
            body_glb.shape_key_clear()      # the death-settle corrective is replaced by the cloth bones
        body_glb.hide_render = True
    renders = CL.split_garments(body, seg, names, groups, caps=str(P.get("caps", "delete")))
    # untouched copies for the QA baseline: the garment exactly as the original skinning moved it
    qa_orig = {}
    for g, r in renders.items():
        o = r.copy()
        o.data = r.data.copy()
        o.name = "AF_qa_orig_" + g
        for c in r.users_collection:
            c.objects.link(o)
        o[CL.CLOTH_ROLE] = "qa"
        o.hide_render = True
        qa_orig[g] = o
    deform_names = [b.name for b in rig.data.bones if b.use_deform]
    for g, r in renders.items():
        if groups[g].get("hand"):
            CL.set_rigid_weights(r, groups[g]["hand"], deform_names)
    edge = float(P.get("proxy_edge_m", 0.045))
    proxies, pins, pins_hold = {}, {}, {}
    for g, r in renders.items():
        remesh = P.get("remesh", "auto")
        if remesh == "auto":
            remesh = len(r.data.vertices) > int(P.get("remesh_above", 12000))
        px = CL.build_proxy(r, edge_m=edge, hand=groups[g].get("hand"), remesh=bool(remesh))
        groups[g]["proxy"] = px.get("af_proxy_method")
        if g == "lower" and P.get("pelvis_follow", True):
            # a robe / skirt / tabard / cape tail hangs from the pelvis: the thighs push it by collision instead of
            # tearing it through their skin weights (render cloth keeps the original weights on its pinned seam)
            legs = [sk.name(s_ + c_) for s_ in ("Left", "Right") for c_ in ("UpLeg", "Leg", "Foot", "ToeBase", "Toe_End")
                    if sk.name(s_ + c_)]
            # the welded band keeps the body weights (proxy and garment move exactly like the body there), the free
            # part hangs from the pelvis
            groups[g]["pelvis_follow_vertices"] = CL.blend_merge_weights(px, sk.name("Hips"), legs)
            # the render garment blends into the same pelvis-carried skinning away from its seam (pin x original
            # + (1 - pin) x pelvis), so skin and simulation agree where they are mixed
            CL.blend_merge_weights(r, sk.name("Hips"), legs)
        st = groups[g]["settings"]
        hold = P.get("shape_hold", st.get("shape_hold", 0.0))
        pg = px.vertex_groups[CL.PIN_GROUP]
        pin = np.zeros(len(px.data.vertices))
        for v in px.data.vertices:
            for gg in v.groups:
                if gg.group == pg.index:
                    pin[v.index] = gg.weight
        pin, n_isl, n_isl_v = CL.anchor_islands(px, pin)
        if n_isl:
            groups[g]["anchored_islands"] = [n_isl, n_isl_v]
        # shape hold: a soft pull of the free cloth toward its pelvis-carried shape (keeps the garment's silhouette);
        # it fades out near the floor (a pooled hem is not pulled through the floor) and deaths run without it
        held = pin
        if hold:
            zr = CL._proxy_rest(px)[:, 2]
            fade = CL.smoothstep((zr - 0.02) / 0.2)
            held = np.maximum(pin, float(hold) * fade)
        proxies[g], pins[g], pins_hold[g] = px, pin, held
        groups[g]["shape_hold"] = float(hold or 0.0)
        groups[g]["proxy_vertices"] = len(px.data.vertices)
        groups[g]["render_vertices"] = len(r.data.vertices)
    timing["split_proxy_s"] = round(time.time() - t, 2)
    leg_k = seg["stats"]["leg_bulk"]["k"]
    poles = []
    if P.get("collide_props", True):
        for side, pole in seg["poles"].items():
            c, d = pole["c"], pole["d"]
            t0, t1 = pole["t_range"]
            poles.append({"hand": sk.name(side + "Hand"), "a": (c + d * t0).tolist(), "b": (c + d * t1).tolist(),
                          "r": pole["r"], "side": side})
    inflate = float(P.get("inflate_m", 0.006))
    free_rest = seg["P"][seg["mask"] & (seg["pin"] < 0.5) & ~seg["cap"]]
    caps, shrunk = CL.fit_capsules(CL.capsules_rest(sk, leg_k), free_rest, margin=inflate + 0.012)
    report["capsules_shrunk_m"] = shrunk
    cols = CL.build_colliders(sk, rig, inflate=inflate, poles=poles, caps=caps,
                              friction=float(P.get("collider_friction", 1.0)))
    body_cols = [o for o in cols.values()]
    colls = {}
    for g, px in proxies.items():
        use = body_cols
        if g.startswith("banner"):
            side = "Left" if g.endswith("l") else "Right"
            use = [o for o in body_cols if o.get("af_pole_side") != side]
        colls[g] = CL.collider_collection("AF_COLL_" + g, use)
        st = dict(groups[g]["settings"])
        if P.get("quality"):
            st["quality"] = int(P["quality"])
        CL.setup_cloth(px, rig, st, colls[g], fps=fps, internal=g.startswith("banner"))

    # ------------------------------------------------------------ simulate every clip
    every = max(1, int(P.get("qa_every", 2)))
    max_disp = float(P.get("max_disp_m", 1.2))
    pen_lim = float(P.get("penetration_limit", 0.03))
    str_lim = float(P.get("stretch_limit", 1.6))
    preroll = int(P.get("preroll", 20))
    cache = {g: [] for g in proxies}
    sim_pos = {}
    t_sim = time.time()
    for act in acts:
        n, loop, cat = _clip_info(act, meta)
        rec = {"name": act.name, "frames": n, "loop": loop, "category": cat}
        if not wanted(act) or n < 2:
            rec["mode"] = "skinned"
            clip_recs.append(rec)
            continue
        tl = CL.sim_timeline(n, loop, preroll=preroll, from_bind=int(P.get("from_bind", 0)))
        qa_frames = sorted(set(list(range(0, n, every)) + [n - 1]))
        for g, px in proxies.items():
            CL.set_pins(px, pins[g] if cat == "death" else pins_hold[g])
        attempt = 0
        while True:
            S, secs = CL.simulate(rig, list(proxies.values()), act, tl, tag=act.name)
            win = {g: CL.clip_window(S[px.name], tl, n) for g, px in proxies.items()}
            skin = CL.skinned(rig, list(proxies.values()), act, qa_frames)
            bad = []
            far = {}
            for g, px in proxies.items():
                w = win[g][qa_frames]
                far[g] = CL.farthest_from_body(rig, caps, px, w, qa_frames) if not np.isnan(w).any() else float("inf")
                if far[g] > max_disp:
                    bad.append(g)
            if not bad or attempt >= 1:
                break
            attempt += 1
            warnings.append("clip %s: cloth of %s exploded, re-simulated with doubled steps and damping" % (act.name, bad))
            for g in bad:
                s = proxies[g].modifiers["AF_Cloth"].settings
                s.quality = min(20, s.quality * 2)
                s.air_damping *= 2.0
        rec["qa_frames"] = qa_frames
        rec.update({"mode": "cloth", "sim_frames": len(tl["times"]), "sim_s": round(secs, 2),
                    "s_per_frame": round(secs / max(len(tl["times"]), 1), 4), "retries": attempt,
                    "timeline": {k: tl[k] for k in ("preroll", "warm", "blend", "period")}, "groups": {}})
        for g, px in proxies.items():
            ok = g not in bad
            q = CL.clip_qa(rig, caps, px, win[g][qa_frames], skin[px.name], qa_frames, pins[g])
            q["simulated"] = ok
            if cat == "death":
                free = pins[g] < 0.5
                Mw = np.array(px.matrix_world)
                last_sim = win[g][n - 1].astype(np.float64) @ Mw[:3, :3].T + Mw[:3, 3]
                last_skin = skin[px.name][-1].astype(np.float64) @ Mw[:3, :3].T + Mw[:3, 3]
                q["death_lying_height_cm"] = round(100 * CL.lying_height(last_sim, free), 1)
                q["death_lying_height_skinned_cm"] = round(100 * CL.lying_height(last_skin, free), 1)
                q["death_floor_min_cm"] = round(100 * float(last_sim[:, 2].min()), 1)
            rec["groups"][g] = {"proxy": q}
            tag = "%s_%s" % (act.name, g)
            if ok:
                cache[g].append((act.name, win[g]))
                sim_pos[(act.name, g)] = win[g]
            else:
                warnings.append("clip %s: cloth of %s still unstable - this clip stays skinned" % (act.name, g))
            q["far_from_body_m"] = round(far[g], 3)
            checks.append(af.check("cloth_stable_" + tag, ok, round(far[g], 3), max_disp, severity="warning",
                                   note="farthest simulated point from the body capsules, m (NaN = inf); an exploded "
                                        "clip is re-simulated once with doubled steps and damping, then stays skinned"))
        clip_recs.append(rec)
        af.log("cloth clip", clip=act.name, frames=len(tl["times"]), seconds=round(secs, 1))
    timing["simulate_s"] = round(time.time() - t_sim, 2)

    # ------------------------------------------------------------ bake caches, bind render cloth
    t = time.time()
    for o in list(cols.values()):
        bpy.data.objects.remove(o, do_unlink=True)
    for nm in ["AF_CLOTH_COLLIDERS"] + ["AF_COLL_" + g for g in proxies]:
        c = bpy.data.collections.get(nm)
        if c is not None:
            bpy.data.collections.remove(c)
    for px in proxies.values():
        px.modifiers["AF_Cloth"].show_viewport = False
        px.modifiers["AF_Cloth"].show_render = False
    binds = {g: CL.bind_embedded(renders[g], px) for g, px in proxies.items()}
    maps = {}
    for g, px in proxies.items():
        if "af_render_index" in px.data.attributes:
            m_ = np.empty(len(px.data.vertices), dtype=np.int32)
            px.data.attributes["af_render_index"].data.foreach_get("value", m_)
            maps[g] = m_
    fws = {g: CL.free_weights(renders[g]) for g in renders}
    bind_err = {}
    for g in renders:
        # the rest pose rebuilt through the binding (embedded deformation is exact at rest)
        rest_r = CL._object_rest(renders[g])
        back = CL.transfer_embedded(binds[g], CL._object_rest(proxies[g]))
        bind_err[g] = round(100 * float(np.percentile(np.linalg.norm(back - rest_r, axis=1), 99)), 3)
    report["bind_rest_error_p99_cm"] = bind_err
    render_cache = {g: [] for g in renders}
    for act in acts:
        have = [g for g in renders if (act.name, g) in sim_pos]
        if not have:
            continue
        n = _clip_info(act, meta)[0]
        A.assign_action(rig, act)
        skin_pos = {g: np.empty((n, len(renders[g].data.vertices), 3), dtype=np.float32) for g in have}
        for f in range(n):
            scene.frame_set(f)
            dg = bpy.context.evaluated_depsgraph_get()
            for g in have:
                skin_pos[g][f] = CL.read_points(renders[g], dg)
        for g in have:
            sim = sim_pos[(act.name, g)]
            # the simulated garment everywhere but its welded band: a short blend over the last 15 % of the pin ramp
            w = np.clip((fws[g] / 0.15), 0.0, 1.0)[None, :, None]
            moved = np.stack([CL.transfer_embedded(binds[g], sim[f]) for f in range(n)])
            rmap = maps.get(g)
            if rmap is not None:
                moved[:, rmap] = sim                   # the garment's own vertices take the simulation exactly
            render_cache[g].append((act.name, ((1.0 - w) * skin_pos[g] + w * moved).astype(np.float32)))
    for g, r in renders.items():
        CL.bake_render_cache(r, render_cache[g])
    timing["bake_s"] = round(time.time() - t, 2)

    # ------------------------------------------------------------ QA on the visible garment: cloth vs original skin
    t = time.time()
    for rec in clip_recs:
        if rec.get("mode") != "cloth":
            continue
        act = bpy.data.actions[rec["name"]]
        frames = rec.pop("qa_frames")
        xl.assign_action(rig, act)
        pos = {}
        for side in ("after", "before"):
            xl.bind_cloth_cache([act] if side == "after" else None)
            arrs = {g: np.empty((len(frames), len(r.data.vertices), 3), dtype=np.float32) for g, r in renders.items()}
            for i, f in enumerate(frames):
                scene.frame_set(int(f))
                dg = bpy.context.evaluated_depsgraph_get()
                for g, r in renders.items():
                    arrs[g][i] = CL.read_points(r if side == "after" else qa_orig[g], dg)
            pos[side] = arrs
        xl.bind_cloth_cache([act])
        for g, r in renders.items():
            if g not in rec["groups"]:
                continue
            q = CL.render_qa(rig, caps, r, frames, pos["after"][g], pos["before"][g])
            rec["groups"][g].update(q)
            a, b = q.get("after"), q.get("before")
            if not a:
                continue
            tag = "%s_%s" % (act.name, g)
            if rec["category"] == "death":
                Mw = np.array(r.matrix_world)
                last_a = pos["after"][g][-1].astype(np.float64) @ Mw[:3, :3].T + Mw[:3, 3]
                last_b = pos["before"][g][-1].astype(np.float64) @ Mw[:3, :3].T + Mw[:3, 3]
                allv = np.ones(len(last_a), dtype=bool)
                rg = rec["groups"][g]
                rg["death_lying_height_cm"] = round(100 * CL.lying_height(last_a, allv), 1)
                rg["death_lying_height_skinned_cm"] = round(100 * CL.lying_height(last_b, allv), 1)
                rg["death_floor_min_cm"] = round(100 * float(last_a[:, 2].min()), 1)
                checks.append(af.check("cloth_death_floor_" + tag, rg["death_floor_min_cm"] >= -2.0,
                                       rg["death_floor_min_cm"], -2.0, severity=gate,
                                       note="lowest point of the garment on the last death frame, cm (on the floor, not "
                                            "through it); lying height %.1f cm, skinned %.1f cm" % (
                                                rg["death_lying_height_cm"], rg["death_lying_height_skinned_cm"])))
            pen_ok = a["penetration_frac"] <= max(pen_lim, b["penetration_frac"] + 0.01)
            checks.append(af.check("cloth_penetration_" + tag, pen_ok, [a["penetration_frac"], b["penetration_frac"]],
                                   pen_lim, severity=gate,
                                   note="share of free garment points > 1 cm inside the body capsules, worst sampled "
                                        "frame [cloth, original skinning]; deepest %.1f / %.1f cm" % (
                                            a["depth_max_cm"], b["depth_max_cm"])))
            st_ok = a["stretch_p99"] is None or a["stretch_p99"] <= max(str_lim, (b["stretch_p99"] or 0) + 0.05)
            checks.append(af.check("cloth_stretch_" + tag, st_ok, [a["stretch_p99"], b["stretch_p99"]], str_lim,
                                   severity=gate, note="99th percentile edge length / rest length of the free garment "
                                                       "[cloth, original skinning]; torn share %.2f / %.2f %%" % (
                                                           100 * a["torn_frac"], 100 * b["torn_frac"])))
    for o in list(qa_orig.values()):
        me = o.data
        bpy.data.objects.remove(o, do_unlink=True)
        if me.users == 0:
            bpy.data.meshes.remove(me)
    timing["qa_s"] = round(time.time() - t, 2)

    # ------------------------------------------------------------ cloth bones for the GLB
    keep_actions = list(acts)
    bones_info = None
    if body_glb is not None:
        t = time.time()
        bones_info = _cloth_bones(rig, sk, proxies, pins, renders, body_glb, seg, acts, meta, sim_pos, caps, P, fps,
                                  render_cache)
        report["cloth_bones"] = bones_info
        timing["cloth_bones_s"] = round(time.time() - t, 2)
    for px in list(proxies.values()):           # the caches live on the garments; proxies are rebuilt on a re-run
        me = px.data
        bpy.data.objects.remove(px, do_unlink=True)
        if me.users == 0:
            bpy.data.meshes.remove(me)
    for a in keep_actions:
        a.use_fake_user = True
    A.assign_action(rig, acts[0])
    scene.frame_set(0)
    xl.bind_cloth_cache([acts[0]])
    outputs["blend"] = af.save_blend(af.out_path(req, "cloth.blend"))

    # ------------------------------------------------------------ GLB from the saved scene
    # (exported after a reload: drivers created in this session - the death morph - are only baked into morph
    # weights by the glTF exporter once the file has been re-read; measured: 0 -> 1 after reload, constant 0 before)
    t = time.time()
    names = {"rig": rig.name, "body": body.name, "glb": body_glb.name if body_glb is not None else None,
             "renders": {g: r.name for g, r in renders.items()}, "acts": [a.name for a in acts]}
    af.open_blend(outputs["blend"])
    scene = bpy.context.scene
    rig = bpy.data.objects[names["rig"]]
    body = bpy.data.objects[names["body"]]
    body_glb = bpy.data.objects.get(names["glb"]) if names["glb"] else None
    renders = {g: bpy.data.objects[n] for g, n in names["renders"].items()}
    acts = [bpy.data.actions[n] for n in names["acts"]]
    keep_actions = list(acts)
    if body_glb is not None:
        A.assign_action(rig, acts[0])
        outputs["glb"], notes = _export_glb(req, rig, body_glb, body, keep_actions)
        warnings += notes
        summ = A.glb_summary(outputs["glb"])
        got = sorted(a["name"] for a in summ["animations"])
        checks.append(af.check("gltf_animations", got == sorted(a.name for a in acts), got, sorted(a.name for a in acts)))
        checks.append(af.check("gltf_skin", bool(summ["skins"]) and summ["skinned_primitives"] > 0,
                               {"skins": summ["skins"], "skinned_primitives": summ["skinned_primitives"]}, ">=1"))
        checks.append(af.check("gltf_no_nan", all(a["nan"] == 0 for a in summ["animations"]),
                               sum(a["nan"] for a in summ["animations"]), 0))
        checks.append(af.check("cloth_bones", bones_info["bones"] > 0, bones_info["bones"], ">0", severity="warning",
                               note="secondary cloth-bone chains in the GLB, weighted to the garments"))
        if bones_info.get("death_morph"):
            dm = _glb_morph_peak(outputs["glb"])
            bones_info["death_morph"]["glb_weight_peak"] = dm
            checks.append(af.check("gltf_death_morph", dm.get("death", 0.0) >= 0.99, dm, 1.0, severity="warning",
                                   note="peak weight of the af_cloth_settle morph per animation in the GLB (death 1, "
                                        "others 0)"))
    else:
        exp, notes = A.export_objects(rig, [body] + list(renders.values()))
        with A.gltf_only_actions(keep_actions, exp, rig) as only:
            outputs["glb"] = af.export_glb(af.out_path(req, "cloth.glb"), objects=exp, animations=True, skins=True,
                                           animation_mode="ACTIONS", morph=False)
        warnings += notes + only.notes
    timing["glb_s"] = round(time.time() - t, 2)
    A.assign_action(rig, acts[0])
    scene.frame_set(0)
    xl.bind_cloth_cache([acts[0]])

    # ------------------------------------------------------------ preview
    if P.get("preview", True):
        t = time.time()
        try:
            outputs["preview"] = _preview(req, rig, body, renders, acts, meta)
        except Exception as exc:
            warnings.append("preview failed: %s" % exc)
        timing["preview_s"] = round(time.time() - t, 2)
    report["groups"] = {g: {k: v for k, v in d.items() if k != "settings"} for g, d in groups.items()}
    return _finish(req, outputs, report, clips_doc, clip_recs, checks, warnings, timing, T0, P)


def _glb_morph_peak(path):
    """{animation: peak morph weight} of a GLB (weights channels)."""
    js, binc = A.read_glb(path)
    out = {}
    for an in js.get("animations", []):
        for ch in an["channels"]:
            if ch["target"].get("path") != "weights":
                continue
            acc = js["accessors"][an["samplers"][ch["sampler"]]["output"]]
            bv = js["bufferViews"][acc["bufferView"]]
            off = bv.get("byteOffset", 0) + acc.get("byteOffset", 0)
            vals = np.frombuffer(binc, dtype=np.float32, count=acc["count"], offset=off)
            out[an["name"]] = round(float(vals.max()) if len(vals) else 0.0, 3)
    return out


def _cloth_bones(rig, sk, proxies, pins, renders, body_glb, seg, acts, meta, sim_pos, caps, P, fps, render_cache=None):
    """Chains per garment group, their keys in every clip (fitted to the simulation, spring chains where a clip
    has none) and the weights of Body_glb: first pin x original skin + (1 - pin) x chain bones, then refined by a
    least-squares fit to the simulated garment over the simulated clips (CL.fit_weights, 4 influences)."""
    deform = [b.name for b in rig.data.bones if b.use_deform]
    spacing = float(P.get("bone_spacing_m", 0.2))
    J = int(P.get("bone_joints", 4))
    info = {"bones": 0, "groups": {}, "clips": {}}
    me = body_glb.data
    Pg = CL.rest_coords(body_glb)
    src = np.arange(len(me.vertices))
    pin_v = seg["pin"][src]
    kind_v = seg["kind"][src]
    group_v = np.array([None] * len(src), dtype=object)
    for g, r in renders.items():
        s_idx = np.empty(len(r.data.vertices), dtype=np.int32)
        r.data.attributes["af_src_index"].data.foreach_get("value", s_idx)
        group_v[s_idx] = g
    all_rows = {}
    for g, px in proxies.items():
        lay = CL.chain_layout(px, pins[g], spacing=spacing, joints=J, max_chains=int(P.get("bone_chains_max", 16)))
        if not lay["chains"]:
            continue
        attach = CL.chain_attach_bones(px, lay, deform)
        rows = CL.add_chain_bones(rig, g, lay, attach)
        all_rows[g] = (lay, rows, attach)
        info["groups"][g] = {"chains": len(rows), "bones": sum(len(r) for r in rows), "attach": sorted(set(attach))}
        info["bones"] += sum(len(r) for r in rows)
        # weights of the GLB body: garment vertices of this group (pin blend), nearest proxy point
        vm = group_v == g
        if not vm.any():
            continue
        Wc = CL.chain_vertex_weights(lay, Pg[vm], CL._proxy_rest(px), pins[g])
        flat = [nm for row in rows for nm in row]
        idx = np.nonzero(vm)[0]
        free = 1.0 - pin_v[idx]
        hand = None
        for gg in (g,):
            if gg.startswith("banner"):
                hand = next((a for a in attach if a), None)
        if hand:
            # a banner rides its hand (generated banners are skinned to the head or a foot as well)
            hv = body_glb.vertex_groups.get(hand) or body_glb.vertex_groups.new(name=hand)
            for v_i in idx.tolist():
                for ge in me.vertices[v_i].groups:
                    if body_glb.vertex_groups[ge.group].name in deform:
                        ge.weight = 0.0
                hv.add([v_i], 1.0, "REPLACE")
        # scale the original deform weights by the pin weight, add the chain weights x (1 - pin)
        for v_i, w_free in zip(idx.tolist(), free.tolist()):
            v = me.vertices[v_i]
            for ge in v.groups:
                vg = body_glb.vertex_groups[ge.group]
                if vg.name in deform:
                    ge.weight = ge.weight * (1.0 - w_free)
        for j, nm in enumerate(flat):
            vg = body_glb.vertex_groups.get(nm) or body_glb.vertex_groups.new(name=nm)
            col = Wc[:, j] * free
            for v_i, w in zip(idx.tolist(), col.tolist()):
                if w > 1e-4:
                    vg.add([v_i], float(w), "REPLACE")
    # keys in every clip
    scene = bpy.context.scene
    for act in acts:
        n = int(round(act.frame_range[1] - act.frame_range[0])) + 1
        loop = bool((meta.get(act.name) or {}).get("loop", act.name.endswith("-loop") or act.use_cyclic))
        src_used = {}
        for g, (lay, rows, attach) in all_rows.items():
            px = proxies[g]
            Mw = np.array(px.matrix_world)
            pos = sim_pos.get((act.name, g))
            if pos is not None:
                seq = np.stack([CL.joints_from_points(lay, pos[i].astype(np.float64) @ Mw[:3, :3].T + Mw[:3, 3])
                                for i in range(n)])
                src_used[g] = "sim"
            else:
                seq = CL.spring_joints(rig, lay, attach, caps, act, n, loop, fps=fps)
                src_used[g] = "spring"
            A.assign_action(rig, act)

            def pose(i):
                scene.frame_set(int(i))
            rots = CL.chain_rotations(rig, rows, seq, pose)
            CL.write_bone_keys(act, rots, n)
        info["clips"][act.name] = src_used
    rig.data.pose_position = "POSE"
    # refine the GLB weights against the simulated garment (every simulated clip, ~24 frames each)
    if render_cache and P.get("fit_weights", True):
        Winv = np.array(rig.matrix_world.inverted())
        for g in all_rows:
            r = renders.get(g)
            if r is None or not render_cache.get(g):
                continue
            s_idx = np.empty(len(r.data.vertices), dtype=np.int32)
            r.data.attributes["af_src_index"].data.foreach_get("value", s_idx)
            M = Winv @ np.array(r.matrix_world)
            samples = []
            for aname, arr in render_cache[g]:
                act = bpy.data.actions.get(aname)
                if act is None:
                    continue
                n = len(arr)
                for fr in range(0, n, max(1, n // 24)):
                    tgt = arr[fr].astype(np.float64) @ M[:3, :3].T + M[:3, 3]

                    def setf(act=act, fr=fr):
                        A.assign_action(rig, act)
                        scene.frame_set(int(fr))
                    samples.append((setf, tgt))
            if samples:
                info["groups"][g]["fit"] = CL.fit_weights(rig, body_glb, s_idx, samples)
    # the end of a death: a corrective morph puts the lying garment exactly where the simulation left it
    if render_cache and P.get("death_morph", True):
        idx_all, tgt_all, death = [], [], None
        Winv = np.array(rig.matrix_world.inverted())
        for act in acts:
            m = meta.get(act.name) or {}
            if (m.get("category") or A.guess_category(act.name)) != "death":
                continue
            for g in all_rows:
                arr = dict(render_cache.get(g) or []).get(act.name)
                r = renders.get(g)
                if arr is None or r is None:
                    continue
                s_idx = np.empty(len(r.data.vertices), dtype=np.int32)
                r.data.attributes["af_src_index"].data.foreach_get("value", s_idx)
                M = Winv @ np.array(r.matrix_world)
                idx_all.append(s_idx)
                tgt_all.append(arr[-1].astype(np.float64) @ M[:3, :3].T + M[:3, 3])
                death = (act, len(arr), m)
            break
        if death is not None and idx_all:
            act, n, m = death
            impact = int(((m.get("death_info") or {}).get("impact_frame")) or int(0.45 * n))
            f0 = int(min(n - 3, impact + max(1, (n - impact) // 4)))
            f1 = int(min(n - 1, max(f0 + 1, impact + max(2, (n - impact) * 2 // 3))))
            A.assign_action(rig, act)
            scene.frame_set(n - 1)
            vidx = np.concatenate(idx_all)
            tgt = np.concatenate(tgt_all)
            uniq, first = np.unique(vidx, return_index=True)
            info["death_morph"] = CL.death_settle_morph(rig, body_glb, uniq, tgt[first], sk.name("Hips"), act,
                                                       (f0, f1, n - 1), acts)
    return info


def _preview(req, rig, body, renders, acts, meta):
    """Rows: skinned / cloth for walk, attack-or-cast and death (5 frames each), one fixed camera."""
    pick = []
    for cat in ("walk", ("attack", "cast"), "death"):
        cats = cat if isinstance(cat, tuple) else (cat,)
        a = next((a for a in acts if _clip_info(a, meta)[2] in cats), None)
        if a is not None:
            pick.append(a)
    if not pick:
        pick = acts[:3]
    objs = [body] + list(renders.values())
    shots, labels = [], []
    for a in pick:
        n = _clip_info(a, meta)[0]
        frames = [int(round(x)) for x in np.linspace(0, n - 1, 5)]
        for mode in ("skin", "cloth"):
            def setup(a=a, mode=mode):
                xl.assign_action(rig, a)
                xl.bind_cloth_cache([a] if mode == "cloth" else None)
            for f in frames:
                shots.append((setup, f))
                labels.append("%s %s %d" % (a.name.replace("-loop", ""), mode, f))
    path = CL.render_frames(af.out_path(req, "preview.png"), objs, shots, size=220, labels=labels, cols=5,
                            title="cloth_sim  rows: skinned / cloth")
    xl.bind_cloth_cache([acts[0]])
    return path


def _finish(req, outputs, report, clips_doc, clip_recs, checks, warnings, timing, T0, P):
    total = round(time.time() - T0, 2)
    timing["total_s"] = total
    budget = float(P.get("time_budget_s", 300))
    checks.append(af.check("cloth_time_budget", total <= budget, total, budget, severity="warning",
                           note="seconds for segmentation, simulation of every clip, bake, cloth bones and GLB"))
    report["clips"] = {r["name"]: r for r in clip_recs}
    report["timing"] = timing
    rp = af.out_path(req, "cloth.json")
    with open(rp, "w", encoding="utf-8") as fh:
        json.dump(af._jsonable(report), fh, ensure_ascii=False, indent=1)
    outputs["report"] = rp
    doc = dict(clips_doc or {"clips": []})
    by = {r["name"]: r for r in clip_recs}
    for r in doc.get("clips", []):
        c = by.get(r.get("gltf_name"))
        if c is not None:
            r["cloth"] = {k: c.get(k) for k in ("mode", "sim_s", "groups")}
    doc["cloth"] = {"groups": report.get("groups"), "glb": outputs.get("glb"), "timing": timing}
    if outputs.get("glb"):
        doc["glb"] = outputs["glb"]
    cj = af.out_path(req, "clips.json")
    with open(cj, "w", encoding="utf-8") as fh:
        json.dump(af._jsonable(doc), fh, ensure_ascii=False, indent=1)
    outputs["clips"] = cj
    stats = {"groups": report.get("groups"), "timing": timing,
             "clips": {r["name"]: {"mode": r.get("mode"), "sim_s": r.get("sim_s"),
                                   "penetration_cloth_skin": {g: [(q.get("after") or {}).get("penetration_frac"),
                                                                  (q.get("before") or {}).get("penetration_frac")]
                                                              for g, q in (r.get("groups") or {}).items()},
                                   "stretch_p99_cloth_skin": {g: [(q.get("after") or {}).get("stretch_p99"),
                                                                  (q.get("before") or {}).get("stretch_p99")]
                                                              for g, q in (r.get("groups") or {}).items()},
                                   "torn_cloth_skin": {g: [(q.get("after") or {}).get("torn_frac"),
                                                           (q.get("before") or {}).get("torn_frac")]
                                                       for g, q in (r.get("groups") or {}).items()}}
                       for r in clip_recs},
             "cloth_bones": (report.get("cloth_bones") or {}).get("bones")}
    return {"outputs": outputs, "stats": stats, "checks": checks, "warnings": warnings}
