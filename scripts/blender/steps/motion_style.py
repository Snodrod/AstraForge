"""motion_style - a character's own way of moving, and procedural humanoid deaths.

inputs.blend - the animated .blend of retarget (af_run opens it): a humanoid rig whose clips are actions
               written per frame for every bone (retarget's layered actions); clips.json next to it (if
               there) gives categories, loops and in-place locomotion, otherwise the clip names do.
params:
  seed                 any number or string (a file name): style knobs not given explicitly are drawn
                       from it deterministically (_style_lib.style_from_seed); no seed = neutral knobs
  build                optional body build biasing the seeded draw: heavy | armored | robed | light |
                       skeleton | zombie
  tempo 0.8..1.25, stride 0.7..1.35, arm_swing 0.2..2, lean -1..1, hunch 0..1, head_bob 0.3..2,
  sway 0.2..2.5, limp 0..1 (+ limp_side l|r), swagger 0..1, stiffness 0.5..1.8
                       explicit overrides of the global style (see _style_lib for what each does)
  clips {}             per clip (gltf name, name without -loop, or category): {knob: value} sets that
                       clip's knobs absolutely; false leaves the clip untouched
  apply [all]          categories that get the style (idle, walk, run, attack, cast, hit, other)
  death "library"      "library" (keep the retargeted death) | "auto" (seeded pick; crumble only when
                       undead) | fall_back | fall_forward | fall_left | fall_right | kneel_collapse |
                       spin_fall | slump_sit | crumble
  death_blend 1.0      1 = the procedural death replaces the library one; 0..1 mixes them (rotations
                       slerped, the body put back onto the floor afterwards)
  undead false         allows crumble in death "auto"
  hold_props true      upright held props (staff, banner, spear, bow) stay upright in idle/walk/hit, and
                       no prop tip goes below the floor in any clip
  floor true           leg IK targets never below the rest ankle height
  floor_tolerance_m 0.02, floor_samples 16, ground_samples 2000
  preview true, preview_cols 6, preview_tile 220
  robe_settle true     a robed body that lies after a procedural death: corrective shape key af_death_settle lowers
                       the robe/skirt cloth onto the legs and the floor at the end of the death (no stiff bell),
                       driven by a Hips custom property the death action ramps 0 -> 1 (other clips key 0)
  deform_qa true       measure torn/stretched surface on every final clip (every deform_every-th frame, default 2)
                       with the limits of rig_humanoid's pose test (_style_lib.ClipDeformQA): 1 % of the posed
                       surface with the upper arms within 90 deg of hanging, 8 % at 170 deg (arms up), linear in
                       between; 4 % of the robe cloth
  deform_gate "error"  severity of the per-clip checks clip_torn_<clip>: error (a character whose real clips
                       tear fails the step, with the numbers) | warning (measured and reported only)
  deform_limits {}     override CLIP_TORN_LIMITS {main, arms_up, robe}
outputs: styled.blend, styled.glb (one glTF animation per clip, same names, "-loop" kept), clips.json
         (resolved style, per clip: frames, tempo, foot_skate_cm vs the input, floor, seam, ground speed,
         the death variant and its impact frame), preview.png (row per clip).
See docs/forge/motion_style.md.
"""
import json
import os

import bpy
from mathutils import Vector

import af_common as af

try:
    from steps import _anim_lib as A
    from steps import _style_lib as SL
except ImportError:
    import _anim_lib as A
    import _style_lib as SL


def _alive(idb):
    try:
        idb.name
        return True
    except ReferenceError:
        return False


def _under(ob, rig):
    p = ob.parent
    while p is not None:
        if p == rig:
            return True
        p = p.parent
    return False


def _clip_meta(blend_path):
    """clips.json written by retarget next to its animated.blend: {gltf_name: record}."""
    if not blend_path:
        return {}, []
    cj = os.path.join(os.path.dirname(os.path.abspath(blend_path)), "clips.json")
    if not os.path.exists(cj):
        return {}, []
    try:
        with open(cj, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except Exception:
        return {}, []
    recs = doc.get("clips") or []
    return {r.get("gltf_name"): r for r in recs if r.get("gltf_name")}, [r.get("gltf_name") for r in recs]


def _cloth_masks(R, meshes):
    """Per mesh a vertex mask of robe/skirt cloth (same rule as _style_lib.cloth_sample_mask, on the bind
    pose): below the hip joints and carried by the Hips with weight >= 0.25."""
    import numpy as np
    thr = SL.cloth_threshold(R)
    out = {}
    hn = R.tr.hips
    for ob in meshes:
        vg = ob.vertex_groups.get(hn)
        nv = len(ob.data.vertices)
        m = np.zeros(nv, dtype=bool)
        if vg is not None and nv:
            co = np.empty(nv * 3)
            ob.data.vertices.foreach_get("co", co)
            co = co.reshape(-1, 3)
            Mw = np.array(ob.matrix_world)
            z = co @ Mw[2, :3] + Mw[2, 3]
            gi = vg.index
            for v in ob.data.vertices:
                if z[v.index] >= thr:
                    continue
                for g in v.groups:
                    if g.group == gi and g.weight >= 0.25:
                        m[v.index] = True
                        break
        out[ob.name] = m
    return out


def _min_z(ob, dg, mask=None, invert=False):
    """Lowest evaluated world z of a mesh, optionally over (not) masked vertices only."""
    import numpy as np
    src = ob.evaluated_get(dg)
    me = src.to_mesh()
    try:
        nv = len(me.vertices)
        if not nv:
            return None
        co = np.empty(nv * 3)
        me.vertices.foreach_get("co", co)
        co = co.reshape(-1, 3)
        Mw = np.array(ob.matrix_world)
        z = co @ Mw[2, :3] + Mw[2, 3]
        if mask is not None and len(mask) == nv:
            sel = mask if invert else ~mask
            if not sel.any():
                return None
            z = z[sel]
        return float(z.min())
    finally:
        src.to_mesh_clear()


def _true_floor_correction(rig, meshes, act, n, impact, target=0.003, tol=0.005, cloth=None):
    """Per-frame vertical shifts from the real mesh (the 2000-sample estimate misses thin parts): from the
    impact on, a frame whose lowest vertex is under the floor is lifted; once the body has settled (the
    last third after the impact, ramped in) the lowest vertex is put exactly at `target` above the floor.
    Frames before the impact are not touched. None without meshes."""
    if not meshes:
        return None
    scene = bpy.context.scene
    A.assign_action(rig, act)
    lows = [None] * n
    for f in range(max(impact - 2, 0), n):
        scene.frame_set(f)
        dg = bpy.context.evaluated_depsgraph_get()
        zs = [_min_z(o, dg, (cloth or {}).get(o.name)) for o in meshes]
        zs = [z for z in zs if z is not None]
        lows[f] = min(zs) if zs else None
    settle = impact + max(1, (n - impact) * 2 // 3)
    corr = [0.0] * n
    for f in range(n):
        z = lows[f]
        if z is None:
            continue
        lift = max(0.0, -z - tol)
        if f >= settle - 4:
            w = min(1.0, (f - (settle - 4)) / 4.0)
            corr[f] = (1.0 - w) * lift + w * (target - z)
        else:
            corr[f] = lift
    # smooth the lift so it does not pop (never below what a frame needs)
    sm = [max(corr[max(0, k - 2): k + 3], key=abs) if any(corr[max(0, k - 2): k + 3]) else 0.0 for k in range(n)]
    out = []
    for k in range(n):
        win = sm[max(0, k - 2): k + 3]
        v = sum(win) / len(win)
        out.append(v if k >= settle else max(v, corr[k]))
    return out


SETTLE_KEY = "af_death_settle"


def _set_prop_curve(rig, act, path, frames_values):
    """(Re)write a linear F-curve of a rig custom property in a layered action."""
    bag = A.channelbag(act)
    for fc in list(bag.fcurves):
        if fc.data_path == path:
            bag.fcurves.remove(fc)
    fc = bag.fcurves.new(path, index=0)
    fc.keyframe_points.add(len(frames_values))
    co = []
    for f, v in frames_values:
        co += [float(f), float(v)]
    fc.keyframe_points.foreach_set("co", co)
    for kp in fc.keyframe_points:
        kp.interpolation = "LINEAR"
    fc.update()


def _robe_settle(R, rig, meshes, cloth, act, n, impact, all_actions, keep=0.2, min_bell=0.10, clearance=0.01):
    """Robe / skirt cloth of a lying body under linear skinning is a stiff cone carried by the pelvis and the
    thighs: after the fall it sticks up like a bell (pilot: the lich). Corrective shape key: on the last frame
    every cloth vertex (cloth mask: carried by the Hips below the hip joints) higher than what lies under it -
    the non-cloth body within 4 cm horizontally, else the floor - is lowered onto that support keeping `keep`
    of its height above it (the robe drapes over the legs and onto the floor). The world offsets are turned
    into rest-space shape-key offsets through the skinning Jacobian of that frame (measured numerically: three
    unit offsets through the armature modifier), so the key gives exactly that shape on the lying pose. The
    key "af_death_settle" is driven by a custom property of the Hips pose bone that the death action ramps
    0 -> 1 between the impact and the settle (every other clip keys it 0, so switching clips never inherits
    it); the glTF exporter samples armature-driven shape keys into morph-weight animation. Returns info or
    None when there is no bell (no robe, or it already lies low)."""
    import numpy as np
    hips = R.tr.hips
    scene = bpy.context.scene
    A.assign_action(rig, act)
    scene.frame_set(n - 1)
    dg = bpy.context.evaluated_depsgraph_get()
    plans = []
    for ob in meshes:
        m = cloth.get(ob.name)
        nv = len(ob.data.vertices)
        if m is None or len(m) != nv or m.sum() < max(50, 0.03 * nv):
            continue
        co = SL._mesh_posed(ob, dg)
        cell = 0.04
        grid = {}
        for (i, j), z in zip(map(tuple, np.floor(co[~m, :2] / cell).astype(np.int64).tolist()), co[~m, 2].tolist()):
            if z > grid.get((i, j), -1e9):
                grid[(i, j)] = z
        idx = np.nonzero(m)[0]
        sup = np.zeros(len(idx))
        for k, (i, j) in enumerate(np.floor(co[idx, :2] / cell).astype(np.int64).tolist()):
            best = 0.0
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    z = grid.get((i + di, j + dj))
                    if z is not None and z > best:
                        best = z
            sup[k] = best
        zs = np.maximum(sup, 0.0) + clearance
        excess = co[idx, 2] - zs
        bell = float(excess.max()) if len(excess) else 0.0
        plans.append((ob, idx, co, zs, excess, bell))
    if not plans or max(p[5] for p in plans) < min_bell:
        return {"applied": False, "bell_height_cm": round(100 * max([p[5] for p in plans] or [0.0]), 1),
                "reason": "no robe" if not plans else "robe already low"}
    info = {"applied": True, "vertices": 0, "bell_height_cm": round(100 * max(p[5] for p in plans), 1)}
    eps = 0.01
    for ob, idx, co, zs, excess, bell in plans:
        dz = np.where(excess > 0, -(1.0 - keep) * excess, 0.0)
        if ob.data.shape_keys is None:
            ob.shape_key_add(name="Basis", from_mix=False)
        kb_basis = ob.data.shape_keys.reference_key
        nv = len(ob.data.vertices)
        base = np.empty(nv * 3)
        kb_basis.data.foreach_get("co", base)
        base = base.reshape(-1, 3)
        tmp = ob.shape_key_add(name="af_tmp_jacobian", from_mix=False)
        tmp.value = 1.0
        J = np.zeros((len(idx), 3, 3))
        for k in range(3):
            off = base.copy()
            off[idx, k] += eps
            tmp.data.foreach_set("co", off.ravel())
            ob.data.update()
            bpy.context.view_layer.update()
            pk = SL._mesh_posed(ob, bpy.context.evaluated_depsgraph_get())
            J[:, :, k] = (pk[idx] - co[idx]) / eps
        ob.shape_key_remove(tmp)
        D = np.zeros((len(idx), 3))
        D[:, 2] = dz
        ok = np.abs(np.linalg.det(J)) > 1e-8
        delta = np.zeros((len(idx), 3))
        delta[ok] = np.linalg.solve(J[ok], D[ok][..., None])[..., 0]
        kb = ob.data.shape_keys.key_blocks.get(SETTLE_KEY) or ob.shape_key_add(name=SETTLE_KEY, from_mix=False)
        new = base.copy()
        new[idx] += delta
        kb.data.foreach_set("co", new.ravel())
        kb.value = 0.0
        kb.slider_min, kb.slider_max = 0.0, 1.0
        ad = ob.data.shape_keys.animation_data or ob.data.shape_keys.animation_data_create()
        for fc in list(ad.drivers):
            if SETTLE_KEY in fc.data_path:
                ad.drivers.remove(fc)
        fc = kb.driver_add("value")
        drv = fc.driver
        drv.type = "AVERAGE"
        var = drv.variables.new()
        var.name = "settle"
        var.type = "SINGLE_PROP"
        var.targets[0].id_type = "OBJECT"
        var.targets[0].id = rig
        var.targets[0].data_path = 'pose.bones["%s"]["%s"]' % (hips, SETTLE_KEY)
        info["vertices"] += int((dz < 0).sum())
        info.setdefault("meshes", []).append(ob.name)
        info["max_drop_cm"] = round(100 * float(-dz.min()), 1)
    pb = rig.pose.bones[hips]
    pb[SETTLE_KEY] = 0.0
    try:
        pb.id_properties_ui(SETTLE_KEY).update(min=0.0, max=1.0)
    except Exception:
        pass
    path = 'pose.bones["%s"]["%s"]' % (hips, SETTLE_KEY)
    # ramp in after the impact: from a third of the way to the end until two thirds (the body has landed)
    f0 = int(impact + max(1, (n - impact) // 4))
    f1 = int(min(n - 1, impact + max(2, (n - impact) * 2 // 3)))
    if f1 <= f0:
        f0, f1 = max(0, n - 3), n - 1
    _set_prop_curve(rig, act, path, [(0, 0.0), (f0, 0.0), (f1, 1.0), (n - 1, 1.0)])
    for a in all_actions:
        if a is not act:
            _set_prop_curve(rig, a, path, [(0, 0.0)])
    info["ramp_frames"] = [f0, f1]
    # what is left of the bell on the last frame
    A.assign_action(rig, act)
    scene.frame_set(n - 1)
    dg = bpy.context.evaluated_depsgraph_get()
    after = 0.0
    for ob, idx, co, zs, excess, bell in plans:
        pk = SL._mesh_posed(ob, dg)
        after = max(after, float((pk[idx, 2] - zs).max()))
    info["bell_height_after_cm"] = round(100 * after, 1)
    return info


def _override_for(clips_param, gltf_name, category):
    if not clips_param:
        return None
    base = gltf_name[:-5] if gltf_name.endswith("-loop") else gltf_name
    for key in (gltf_name, base, category):
        if key in clips_param:
            return clips_param[key]
    return None


def run(req):
    P, I = req["params"], req.setdefault("inputs", {})
    os.makedirs(req["out_dir"], exist_ok=True)
    warnings, checks = [], []
    blend = I.get("blend")
    if blend:
        want = os.path.normcase(os.path.abspath(blend))
        if os.path.normcase(os.path.abspath(bpy.data.filepath or "")) != want:
            af.open_blend(blend)
    elif I.get("mesh"):
        af.reset_scene()
        af.import_any(I["mesh"])
    else:
        raise ValueError("inputs.blend (the animated .blend of retarget) is required")
    rig, scheme, tmap = A.find_target_rig(list(bpy.context.scene.objects))
    R = SL.Rig(rig)
    tr = R.tr
    meshes = A.skinned_meshes(rig)
    if rig.animation_data:
        rig.animation_data.action = None
    meta, order = _clip_meta(blend)
    acts = A.rig_actions(rig, meshes)
    if not acts:
        raise ValueError("the rig %s has no clips (run retarget first)" % rig.name)
    by_name = {a.name: a for a in acts}
    names = [n for n in order if n in by_name] + sorted(n for n in by_name if n not in order)

    style, src = SL.resolve_style(P)
    seed = P.get("seed")
    apply = set(P.get("apply") or SL.CATEGORIES)
    clips_param = P.get("clips") or {}
    undead = bool(P.get("undead", False))
    death = SL.death_choice(P, seed, undead)
    death_blend = float(P.get("death_blend", 1.0))
    if not 0.0 <= death_blend <= 1.0:
        raise ValueError("death_blend must be within 0..1")
    opts = {"hold_props": bool(P.get("hold_props", True)), "floor": bool(P.get("floor", True))}
    fps = float(bpy.context.scene.render.fps) / float(bpy.context.scene.render.fps_base or 1.0)
    props = SL.held_props(R, meshes) if opts["hold_props"] else {}
    gs = A.skin_samples(rig, meshes, n=int(P.get("ground_samples", 2000))) if meshes else None
    cloth = _cloth_masks(R, meshes) if meshes else {}
    deform_qa = bool(P.get("deform_qa", True)) and bool(meshes)
    gate = str(P.get("deform_gate", "error")).lower()
    if gate not in ("error", "warning"):
        raise ValueError("deform_gate must be error or warning")
    af.log("motion_style", rig=rig.name, clips=len(names), death=death, style={k: style[k] for k in SL.STYLE_KEYS},
           props={s: {"len": round(v["len"], 2), "upright": v["upright"]} for s, v in props.items()})

    report, out_actions = [], []
    death_done = False
    settle_job = None
    for gname in names:
        act = by_name[gname]
        n = int(round(act.frame_range[1] - act.frame_range[0])) + 1
        m = meta.get(gname) or {}
        cat = SL.category_of(gname, m)
        loop = bool(m.get("loop", gname.endswith("-loop") or act.use_cyclic))
        locomotion = bool(m.get("locomotion", cat in ("walk", "run")))
        rec = {"name": gname[:-5] if gname.endswith("-loop") else gname, "gltf_name": gname, "category": cat,
               "loop": loop, "locomotion": locomotion, "frames_in": n, "library_id": m.get("library_id")}
        over = _override_for(clips_param, gname, cat)
        frames = SL.read_action(R, act, n)
        if cat == "death":
            if death == "library" or over is False:
                rec.update({"mode": "untouched", "death": "library", "frames": n})
                out_actions.append(act)
                report.append(rec)
                continue
            start = SL.pose_from_basis(R, frames[0])
            poses, dinfo = SL.make_death(R, start, death, seed if seed is not None else 0, fps, props, gs,
                                         height=max(tr.heads["HeadTop_End"].z if "HeadTop_End" in tr.heads else 1.8, 0.5))
            if death_blend < 1.0:
                lib_poses = [SL.pose_from_basis(R, b) for b in frames]
                poses = SL.blend_poses(R, lib_poses, poses, death_blend)
                lo, lift = SL.floor_fix(R, poses, gs, contact_from=dinfo["impact_frame"])
                dinfo["blend_floor_lift_cm"] = round(100 * lift, 1)
            new = SL.write_poses(R, gname, poses, False)
            # the sample-based floor estimate misses thin parts: measure the real mesh after the impact
            # and move the lying body exactly onto the floor (ramped in over the impact)
            corr = _true_floor_correction(rig, meshes, new, len(poses), dinfo.get("impact_frame", 0), cloth=cloth)
            if corr is not None and any(abs(c) > 0.003 for c in corr):
                for i, pz in enumerate(poses):
                    if abs(corr[i]) > 1e-6:
                        pz.hips = pz.hips + Vector((0.0, 0.0, corr[i]))
                new = SL.write_poses(R, gname, poses, False)
                dinfo["mesh_floor_correction_cm"] = [round(100 * min(corr), 1), round(100 * max(corr), 1)]
            out_actions.append(new)
            settle_job = (new, len(poses), int(dinfo.get("impact_frame", 0)), dinfo)
            rec.update({"mode": "procedural_death", "death": death, "death_blend": death_blend,
                        "frames": len(poses), "death_info": dinfo})
            report.append(rec)
            death_done = True
            continue
        if over is False or cat not in apply:
            rec.update({"mode": "untouched", "frames": n})
            out_actions.append(act)
            report.append(rec)
            continue
        st = SL.clip_style(style, cat, over if isinstance(over, dict) else None)
        poses, info = SL.style_clip(R, frames, cat, st, loop, locomotion, props, opts)
        new = SL.write_poses(R, gname, poses, loop)
        out_actions.append(new)
        rec.update({"mode": "styled", "style": {k: round(float(v), 3) if not isinstance(v, str) else v
                                                 for k, v in st.items()},
                    "frames": info["frames"], "tempo_real": info["tempo_real"],
                    "hips_drop_max_cm": info["hips_drop_max_cm"], "ik_unreached_frames": info["ik_unreached_frames"],
                    "clip_trunk_lean_deg": info["clip_trunk_lean_deg"], "forward_knobs_scale": info["forward_knobs_scale"],
                    "prop_fixes": info["prop_fixes"], "_src_feet": info["src_feet"]})
        report.append(rec)
    if death != "library" and not death_done:
        # no death clip in the input: build one from the first frame of the first clip
        first = by_name[names[0]]
        frames = SL.read_action(R, first, 1)
        start = SL.pose_from_basis(R, frames[0])
        poses, dinfo = SL.make_death(R, start, death, seed if seed is not None else 0, fps, props, gs)
        new = SL.write_poses(R, "death", poses, False)
        out_actions.append(new)
        report.append({"name": "death", "gltf_name": "death", "category": "death", "loop": False,
                       "locomotion": False, "frames_in": 0, "mode": "procedural_death", "death": death,
                       "death_blend": 1.0, "frames": len(poses), "death_info": dinfo})
        warnings.append("the input has no death clip: a procedural %s death was added" % death)

    if settle_job is not None and meshes and bool(P.get("robe_settle", True)):
        act_d, n_d, imp_d, dinfo_d = settle_job
        st_info = _robe_settle(R, rig, meshes, cloth, act_d, n_d, imp_d, out_actions)
        dinfo_d["robe_settle"] = st_info
        if st_info and st_info.get("applied"):
            af.log("robe settle", **{k: v for k, v in st_info.items() if k != "meshes"})

    # ---------------------------------------------------------------- measurements
    scene = bpy.context.scene
    scene.frame_start = 0
    fl_lim = float(P.get("floor_tolerance_m", 0.02))
    descend = [o for o in scene.objects if o.type == "MESH" and o not in meshes and _under(o, rig)]
    for act, rec in zip(out_actions, report):
        n = rec["frames"]
        tag = rec["gltf_name"]
        ev = A.evaluate_action(tr, act, n, root_travel=False, hips_travel=False)
        rec["nan"] = ev["nan"]
        checks.append(af.check("no_nan_" + tag, ev["nan"] == 0, ev["nan"], 0))
        if rec["loop"]:
            rec["seam_rot_deg"], rec["seam_pos_mm"] = ev["seam_rot_deg"], ev["seam_pos_mm"]
            checks.append(af.check("loop_seam_" + tag, ev["seam_rot_deg"] <= 0.5 and ev["seam_pos_mm"] <= 1.0,
                                   [ev["seam_rot_deg"], ev["seam_pos_mm"]], [0.5, 1.0],
                                   note="deg, mm between the first and the last pose"))
        src_feet = rec.pop("_src_feet", None)
        if src_feet and tr.legs:
            sf = {s: {"ankle": [f[s]["ankle"] for f in src_feet], "toe": [f[s]["toe"] for f in src_feet]}
                  for s in tr.legs}
            out = ev["feet_tracks"]
            v_in = A.source_ground_velocity(sf)
            contacts = A.source_contacts(sf, 30.0 if not scene.render.fps else scene.render.fps, v_in)
            if rec["locomotion"]:
                v_out = A.source_ground_velocity(out)
                sk = A.contact_skate(out, contacts, offset=[-v_out * i for i in range(n)])
                sk_in = A.contact_skate(sf, contacts, offset=[-v_in * i for i in range(n)])
                rec["ground_speed_mps"] = round(v_out.length * scene.render.fps, 3)
                rec["ground_speed_in_mps"] = round(v_in.length * scene.render.fps, 3)
            else:
                sk = A.contact_skate(out, contacts)
                sk_in = A.contact_skate(sf, contacts)
            rec["foot_skate_cm"], rec["foot_skate_in_cm"] = sk["skate_cm"], sk_in["skate_cm"]
            rec["foot_plant_drift_cm"], rec["foot_plant_drift_in_cm"] = sk["drift_cm"], sk_in["drift_cm"]
            lim = round(max(2.0, 1.3 * sk_in["skate_cm"]), 1)
            checks.append(af.check("foot_skate_" + tag, sk["skate_cm"] <= lim, sk["skate_cm"], lim, severity="warning",
                                   note="cm the contact point moves on the input's contact frames (game frame for "
                                        "in-place locomotion), vs max(2, 1.3 x input %.1f)" % sk_in["skate_cm"]))
        fr = A.floor_report(rig, meshes, descend, act, n, samples=int(P.get("floor_samples", 16)))
        rec["floor"] = fr
        if fr.get("body_min_z_m") is not None:
            checks.append(af.check("floor_body_" + tag, fr["body_min_z_m"] >= -fl_lim, fr["body_min_z_m"], -fl_lim,
                                   severity="warning", note="lowest skinned point over the sampled frames, m"))
        if rec.get("mode") == "procedural_death" and gs is not None:
            A.assign_action(rig, act)
            scene.frame_set(n - 1)
            dg = bpy.context.evaluated_depsgraph_get()
            lows = [z for z in (_min_z(o, dg, cloth.get(o.name)) for o in meshes) if z is not None]
            low = min(lows) if lows else None
            rec["death_final_lowest_m"] = None if low is None else round(low, 4)
            cl = [z for z in (_min_z(o, dg, cloth.get(o.name), invert=True) for o in meshes) if z is not None]
            rec["death_cloth_lowest_m"] = round(min(cl), 4) if cl else None
            rec["cloth_vertices"] = int(sum(int(m.sum()) for m in cloth.values()))
            checks.append(af.check("death_on_floor_" + tag, low is not None and abs(low) <= fl_lim,
                                   rec["death_final_lowest_m"], fl_lim,
                                   note="the dead body ends ON the floor: lowest point of the last frame within "
                                        "+-tolerance of z = 0 (robe/skirt cloth carried by the Hips is a stiff shell "
                                        "under linear skinning and may fold through the floor: death_cloth_lowest_m)"))
    # ---------------------------------------------------------------- deformation on the real clips
    deform = {}
    if deform_qa:
        qa = SL.ClipDeformQA(R, meshes, cloth)
        every = max(1, int(P.get("deform_every", 2)))
        lims = P.get("deform_limits") or None
        for act, rec in zip(out_actions, report):
            tag = rec["gltf_name"]
            d = qa.measure(act, rec["frames"], every=every, limits=lims)
            rec["deform"] = d
            deform[tag] = {k: d.get(k) for k in ("worst_torn_frac", "worst_frame", "worst_class", "worst_limit",
                                                 "worst_robe_frac", "arms_up_frames", "over_frames", "frames_measured",
                                                 "torn_by_bone_m2", "ok")}
            note = ("%s: worst frame %s (%s class) torn %.2f %% of the posed surface (limit %.1f %%)%s%s" % (
                tag, d["worst_frame"], d["worst_class"], 100 * d["worst_torn_frac"], 100 * (d.get("worst_limit") or 0),
                ("; robe %.2f %% (limit %.0f %%)" % (100 * d["worst_robe_frac"], 100 * SL.CLIP_TORN_LIMITS["robe"])
                 if d.get("worst_robe_frac") is not None else ""),
                ("; mostly %s" % ", ".join(d["torn_by_bone_m2"])) if d.get("torn_by_bone_m2") else ""))
            checks.append(af.check("clip_torn_" + tag, d["ok"], d["worst_torn_frac"], d.get("worst_limit"),
                                   severity=gate, note=note + " (frames over a limit: %d of %d measured)" % (
                                       d["over_frames"], d["frames_measured"])))
        af.log("clip deformation", **{k: v["worst_torn_frac"] for k, v in deform.items()})
    A.assign_action(rig, out_actions[0])
    scene.frame_set(0)

    # ---------------------------------------------------------------- outputs
    outputs = {}
    keep = [a for a in out_actions if _alive(a)]
    exp_objs, notes = A.export_objects(rig, meshes)
    warnings += notes
    sockets_in = sorted({o["game_socket"] for o in exp_objs if "game_socket" in o.keys()})
    # actions of the rig that were replaced are gone; anything else of the rig is not exported
    with A.gltf_only_actions(keep, exp_objs, rig) as only:
        has_keys = any(o.type == "MESH" and o.data.shape_keys is not None for o in exp_objs)
        glb = af.export_glb(af.out_path(req, "styled.glb"), objects=exp_objs, animations=True, skins=True,
                            animation_mode="ACTIONS", morph=has_keys)
    warnings += only.notes
    outputs["glb"] = glb
    summ = A.glb_summary(glb)
    got = [a["name"] for a in summ["animations"]]
    want = [r["gltf_name"] for r in report]
    checks.append(af.check("gltf_animations", sorted(got) == sorted(want), got, want))
    checks.append(af.check("gltf_sockets", set(sockets_in) <= set(summ["sockets"]), summ["sockets"], sockets_in))
    checks.append(af.check("gltf_no_nan", all(a["nan"] == 0 for a in summ["animations"]),
                           sum(a["nan"] for a in summ["animations"]), 0))
    if meshes:
        checks.append(af.check("gltf_skin", bool(summ["skins"]) and summ["skinned_primitives"] > 0,
                               {"skins": summ["skins"], "skinned_primitives": summ["skinned_primitives"]}, ">=1"))
    for rec in report:
        a = next((x for x in summ["animations"] if x["name"] == rec["gltf_name"]), None)
        rec["gltf_duration"] = a["duration"] if a else None
    outputs["blend"] = af.save_blend(af.out_path(req, "styled.blend"))
    if P.get("preview", True) and meshes:
        outputs["preview"] = A.render_clip_grid(
            af.out_path(req, "preview.png"), tr, meshes,
            [(a, r["loop"], r["frames"]) for a, r in zip(out_actions, report)],
            cols=int(P.get("preview_cols", 6)), tile=int(P.get("preview_tile", 220)))
    doc = {"rig": rig.name, "fps": fps, "seed": seed, "build": P.get("build"),
           "style": {k: (round(float(style[k]), 3) if k != "limp_side" else style[k]) for k in list(SL.STYLE_KEYS) + ["limp_side"]},
           "style_source": src, "death": death, "death_blend": death_blend, "undead": undead,
           "held_props": {s: {"length_m": round(v["len"], 3), "upright": v["upright"], "samples": v["samples"]}
                          for s, v in props.items()},
           "glb": glb, "sockets": summ["sockets"], "clips": report, "deform_qa": deform or None,
           "deform_gate": gate if deform_qa else None}
    cj = af.out_path(req, "clips.json")
    with open(cj, "w", encoding="utf-8") as fh:
        json.dump(af._jsonable(doc), fh, ensure_ascii=False, indent=1)
    outputs["clips"] = cj
    stats = {"rig": rig.name, "clips": len(report), "death": death, "style": doc["style"],
             "gltf_animations": got, "held_props": doc["held_props"], "deform_qa": deform or None,
             "deform_gate": gate if deform_qa else None,
             "per_clip": {r["gltf_name"]: {k: r.get(k) for k in (
                 "mode", "frames_in", "frames", "foot_skate_cm", "foot_skate_in_cm", "ground_speed_mps",
                 "seam_rot_deg", "death")} for r in report}}
    return {"outputs": outputs, "stats": stats, "checks": checks, "warnings": warnings}
