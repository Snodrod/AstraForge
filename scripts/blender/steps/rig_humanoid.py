"""rig_humanoid - automatic Mixamo-compatible rig for any humanoid mesh (a local auto-rigger).

inputs.mesh (.glb/.gltf/.fbx/.obj/.stl/.blend; params.objects picks objects of a .blend) or
inputs.blend (opened by af_run; its meshes are used).
params:
  height_m 1.8            body height (head top; props above the head do not count, height_ref)
  height_ref "head"       "head" | "bbox"
  front "auto"            auto | -Y | +Y | +X | -X  (where the character looks in the input)
  markers {}              Mixamo-style overrides in the normalised frame, metres:
                          chin, groin, l_wrist, r_wrist, l_elbow, r_elbow, l_knee, r_knee as [x, z]
                          or [x, y, z]; plus joints: {BoneName: [x, y, z]} for any bone head
  rigid []                manual rigid rules {"bone", "axis"|"x_min".., "merge"}
  auto_rigid true         held staff/spear/sword (and shields) become rigid to that hand bone
  robe "auto"             auto | true | false: cloth below the crotch follows Hips + both thighs
                          with a smooth left/right blend (no tearing between the legs)
  robe_feet false         true: toes/shoes poking out under a robe keep their foot weights
  max_influences 4
  proxy_tris 60000        bone heat on a welded/decimated proxy (retries at 1/2, 1/4, then a voxel
                          remesh), Data Transfer to the full mesh
  prune_limbs true        arm weights fade outside the arm tube; a vertex never follows arm + leg
  smooth_iterations 0     Laplacian smoothing of the proxy weights
  objects []              names of the objects to rig when inputs.mesh is a .blend
  sockets true            contract 6 human sockets; socket_mount_hand_* use _items_lib.hand_socket_frame
                          (the frame attach_prop computes itself, so it reuses them untouched)
  shield_mounts true      also socket_mount_shield_l/r: attach_prop's canonical shield fit on the rest pose
  grip_dirs {}            {"l"|"r": [x, y, z] | "up" | "down" | "forward" | "back" | "out" | "in"}: +Z of
                          socket_mount_hand_<side> exactly along this direction (rig frame: the character
                          faces -Y, "out" = away from the body). A prop swapped in later by matching its
                          socket_grip_main then points the right way (a staff held upright: "up")
  keep_input_sockets true socket empties of the input (socket_* / game_socket, e.g. socket_fx_eye_l/r of
                          replace_part, socket_mount_hand_l of a placed bone_kit hand) follow the body into the
                          rig frame and are kept: a name the rig builds itself (hand, hip, back, head, chest
                          sockets) takes the input frame and extras; any other socket becomes a child of the
                          bone whose skin weight dominates its nearest vertices (params.socket_bones
                          {name: Bone} overrides); replace_sockets [names] rebuilds those instead
  island_max_frac 0.01    loose pieces with fewer vertices (bone_kit finger bones, clasps, eyes) stay out of
                          the bone-heat proxy (they made Blender's solver fail on the whole mesh) and ride
                          rigidly on the bone nearest to their cluster (pieces closer than 1 cm = one cluster)
  surface_aware true      the arm-tube prune and the arm/leg separation also look at surface (geodesic)
                          distances: a sleeve slit off the robe keeps its arm weights, robe skin next to a
                          hidden arm loses them (surface_margin_h 0.05)
  arm_bleed [0.10, 0.35]  residual arm weight a on torso-dominated vertices x smoothstep(lo, hi, a); null = off
  fingers false           try 3+1 bones per finger (only when fingers are separable)
  deform_test true        pose test (arms forward, arms up, squat, stride) with metrics
  deform_gate "error"     severity of the pose-test checks (deform_torn_area*, robe_stride_tear): error | warning.
                          Production batches may pass warning: the abstract extreme poses
                          do not occur in the game clips, motion_style measures the real clips instead
  intersections true      self-intersection proxy in the pose test (BVH overlap)
  min_confidence 0.35     below this the rig is not built (markers are still written)
  fix_yaw true            an input turned about Z (not axis-aligned) is rotated to face -Y when the
  max_yaw_deg 5           leftover yaw exceeds this (shoulder band PCA + feet line)
The rig is not built (ok:false, markers still written) on gross landmark failures: upper/forearm
outside 0.6..2.0, forearm < 0.09 H, leg < 0.33 H, mirrored ForeArm/Hand joints > 0.10 H apart while no
hand holds a detected prop, bow or shield, no ground support, non-humanoid proportions.
Bows: a thin curved band in a roughly vertical plane beside the body (bow limb + string, or a curved
blade) touched by a fist is a held curved prop (_humanoid_lib.Landmarks.find_bows): it stays out of
the landmarks (the wrist is not put on the limb), is rigid to that hand bone, the hand socket +Z runs
up the riser, and the arm-symmetry check knows the hand holds a prop. A quiver behind the back is
never taken.
outputs: rigged.glb, rigged.blend, markers.json (with a "projection" block: front tile of markers.png,
metres <-> PNG pixels, for the marker editor), markers.png, poses.png
See docs/forge/humanoid.md.
"""
import os
import time

import bpy
import numpy as np

from mathutils import Matrix

import af_common as af
from steps import _humanoid_lib as hl
from steps import _items_lib as IL


def run(req):
    os.makedirs(req["out_dir"], exist_ok=True)
    t0 = time.time()
    params = req["params"]
    H = float(params.get("height_m", 1.8))
    warnings = []
    outputs, checks, stats = {}, [], {}

    input_socks = []
    shield_kin = []
    body, P, prep = hl.prepare_body(req, warnings, sockets=input_socks)
    # placement sockets of extract_props (socket_prop_<hand>_<n>, extras af_bone / af_hand): those hands held a
    # prop that now lives in its own item file - their asymmetric pose is a held pose for the arm check
    held = sorted({"Left" if str((r.get("props") or {}).get("af_hand")) == "l" else "Right"
                   for r in input_socks if (r.get("props") or {}).get("af_hand") in ("l", "r")})
    if held:
        params = dict(params, held_hands=sorted(set(params.get("held_hands") or []) | set(held)))
        # the fist of a cut-out weapon / staff / bow / banner / shield handle: the arm is solved on its grip
        # (shield_grips false: the hand behind a shield is traced as before)
        kin = [{"side": "Left" if (r.get("props") or {}).get("af_hand") == "l" else "Right", "matrix": r["matrix"],
                "grip_mode": str((r.get("props") or {}).get("af_grip_mode") or "shaft")}
               for r in input_socks if (r.get("props") or {}).get("af_hand") in ("l", "r")]
        kin_w = [k for k, r in zip(kin, [r for r in input_socks if (r.get("props") or {}).get("af_hand") in ("l", "r")])
                 if (r.get("props") or {}).get("af_item_kind") != "shield"]
        shield_kin = [k for k in kin if k not in kin_w]
        if str(params.get("shield_grips", "auto")).lower() == "always":
            kin_w, shield_kin = kin_w + shield_kin, []
        if kin_w and params.get("use_placement_grips", True):
            params = dict(params, known_grips_in=kin_w)
            stats["known_grips_from_sockets"] = [k["side"] for k in kin_w]
        req = dict(req, params=params)
        stats["held_hands_from_sockets"] = held
    af.log("prepared", facing=prep["facing"]["detected"], scale=prep["scale"],
           yaw=prep["yaw"]["residual_deg"])
    lm, P = hl.run_landmarks(body, P, H, params, warnings, prep)
    if held and lm.fail is not None and shield_kin and params.get("use_placement_grips", True):
        # the hand behind a cut-out shield traced into the cape / the board's cap: solve it on the shield handle
        params = dict(params, known_grips_in=list(params.get("known_grips_in") or []) + shield_kin)
        req = dict(req, params=params)
        w2 = []
        lm2, P2 = hl.run_landmarks(body, P, H, params, w2, prep)
        stats["shield_grip_retry"] = {"first_verdict": lm.fail, "retry_verdict": lm2.fail}
        lm, P = lm2, P2
        warnings.extend(w2)
    af.log("landmarks", pose=lm.pose, robe=lm.robe, confidence=lm.overall, props=len(lm.props))
    J = lm.joints_m()
    stats.update({"pose_class": lm.pose, "robe": bool(lm.robe), "legs": lm.leg_mode,
                  "landmark_confidence": lm.overall, "facing": prep["facing"]["detected"],
                  "scale_applied": prep["scale"], "source_height": prep["source_height"],
                  "props": len(lm.props), "plates": len(getattr(lm, "plate_props", [])),
                  "bows": [{k: bw[k] for k in ("kind", "side", "grip_frac", "fist_samples")} for bw in lm.bow_props],
                  "yaw": prep["yaw"], "recentred_m": prep.get("recentred_m"),
                  "arm_symmetry_max_h": lm.arm_sym_max, "shoulders": lm.info.get("shoulders")})
    min_c = float(params.get("min_confidence", 0.35))
    checks.append(af.check("landmark_confidence", lm.fail is None and lm.overall >= min_c,
                           lm.overall, min_c, note=lm.fail))
    checks.append(af.check("ground_support", lm.ground_ok or "groin" in lm.ov, lm.info["ground"],
                           None, note=None if lm.ground_ok else "floating/legless figure"))
    checks.append(af.check("landmark_proportions", not lm.gross, lm.ratios,
                           "upper/fore %.1f..%.1f, forearm >= %.2f H, leg >= %.2f H" % (
                               hl.GROSS["upper_fore"][0], hl.GROSS["upper_fore"][1],
                               hl.GROSS["forearm_min"], hl.GROSS["leg_min"]),
                           note="; ".join(lm.gross) or None))
    checks.append(af.check("arm_symmetry", lm.arm_sym_exempt or lm.arm_sym_max <= hl.ARM_SYM_MAX,
                           lm.arm_sym_max, hl.ARM_SYM_MAX,
                           note="max distance (H) of mirrored ForeArm/Hand joints" +
                                ("; exempt: a hand holds a detected prop/shield or markers were "
                                 "given" if lm.arm_sym_exempt else "")))

    fingers, finger_info = None, None
    if params.get("fingers"):
        fingers, finger_info = hl.detect_fingers(body, lm, J, H, warnings)
        for side, fs in (fingers or {}).items():
            for f, pts in fs.items():
                for i, p in enumerate(pts):
                    J[side + "Hand" + f + str(i + 1)] = np.asarray(p)

    projection = {}
    outputs["markers_png"] = hl.render_markers(af.out_path(req, "markers.png"), body, J, lm.conf, H,
                                               projection=projection)
    mk = hl.markers_json(lm, prep, H, warnings, {"fingers": finger_info, "projection": projection})
    outputs["markers_json"] = hl.write_json(af.out_path(req, "markers.json"), mk)

    if lm.fail is not None:
        stats["tris"] = hl.tri_count(body)
        return {"ok": False, "error": "rig not built: " + lm.fail, "outputs": outputs,
                "stats": stats, "checks": checks, "warnings": warnings}

    # ---- armature and weights
    roll_hints = hl.grip_roll_hints(lm) if params.get("auto_rigid", True) else {}
    rig = hl.build_armature(J, H, fingers=fingers, roll_hints=roll_hints)
    names, W, skin_info = hl.skin(body, rig, H, params, warnings, log=af.log, radii=hl.arm_radii(lm))
    verts = hl.mesh_verts(body)
    rigid_sets, rigid_mask = [], np.zeros(len(verts), dtype=bool)
    if params.get("auto_rigid", True):
        rigid_sets += hl.prop_vertex_sets(lm, verts, P, H)
    for rule, mask in hl.rigid_rule_sets(verts, params.get("rigid")):
        bone = hl.b(hl.short(rule["bone"]))
        if bone not in names:
            raise ValueError("params.rigid: unknown bone %s" % rule["bone"])
        rigid_sets.append((bone, mask, dict(rule, kind="rule")))
    robe_info = None
    if lm.robe:
        robe_info = hl.robe_blend(W, names, verts, lm, rig, H, feet=bool(params.get("robe_feet", False)))
    for _, mask, _ in rigid_sets:
        rigid_mask |= mask
    # rigid plate armour (painted metal regions -> one bone each, smooth weights only in the joint gaps)
    W_soft = W.copy()
    hard_info = hl.hard_surface(body, W, names, H, params, exclude=rigid_mask, log=af.log)
    metal_region = hard_info.pop("_metal", None)
    rigid_counts = hl.apply_rigid(W, names, rigid_sets)
    maxinf = int(params.get("max_influences", 4))
    W = hl.limit_normalize(W, maxinf)
    unweighted = int((W.sum(1) < 1e-6).sum())
    hl.write_weights(body, names, W)
    body.parent = rig
    body.matrix_parent_inverse.identity()
    mod = body.modifiers.new("Armature", "ARMATURE")
    mod.object = rig
    mod.use_vertex_groups = True
    bpy.context.view_layer.update()

    sockets = {}
    grip_align, grip_rules = {}, {}
    # sockets of the input (socket_fx_eye_l/r of replace_part, the socket_mount_hand_* of a placed
    # bone_kit hand ...) follow the body into the rig frame and survive (contract 6)
    M_in = prep["matrix"] if prep.get("matrix") is not None else Matrix.Identity(4)
    keep_input = bool(params.get("keep_input_sockets", True))
    replace_names = set(params.get("replace_sockets") or [])
    in_socks = {}
    for r in input_socks:
        if not keep_input or r["name"] in replace_names:
            continue
        loc, rot, _ = (M_in @ r["matrix"]).decompose()
        in_socks[r["name"]] = (Matrix.Translation(loc) @ rot.to_matrix().to_4x4(), dict(r.get("props") or {}))
    if params.get("sockets", True):
        grip_dirs = {("Left" if k.startswith("Left") else "Right"): v for k, v in roll_hints.items()}
        for tag, spec in (params.get("grip_dirs") or {}).items():
            side = {"l": "Left", "left": "Left", "r": "Right", "right": "Right"}.get(str(tag).lower())
            if side is None:
                raise ValueError("params.grip_dirs keys must be l / r")
            grip_dirs[side] = IL.grip_dir_vector(spec, "l" if side == "Left" else "r")
        sock_info = {}
        sockets = hl.add_human_sockets(rig, body, names, W, H, rigid_mask=rigid_mask, grip_dirs=grip_dirs,
                                       shields=bool(params.get("shield_mounts", True)), info=sock_info,
                                       existing=in_socks)
        stats["hand_socket_frame"] = {k: {kk: sock_info[k].get(kk) for kk in (
            "source", "thumb_source", "grip_dir", "hand_vertices", "excluded_vertices", "palm_gap", "knuckles_from")
            if sock_info[k].get(kk) is not None} for k in ("socket_mount_hand_l", "socket_mount_hand_r")}
        stats["shield_mounts"] = {k: {"standoff_m": v["fit"].get("standoff_m"),
                                      "rotation_euler_deg": v["fit"].get("rotation_euler_deg"),
                                      "socket_space_matrix": v["socket_space_matrix"]}
                                  for k, v in sock_info.items() if k.startswith("socket_mount_shield_")}
        stats["shield_mounts_seconds"] = sock_info.get("shield_mounts_seconds")
        if params.get("shield_mounts", True):
            missing_sm = [n for n in ("socket_mount_shield_l", "socket_mount_shield_r") if n not in sockets]
            if missing_sm:
                warnings.append("shield mounts not built (%s): %s" % (
                    ", ".join(missing_sm), sock_info.get("shield_mounts_error", "unknown reason")))
        for p in list(lm.props) + list(lm.bow_props):
            if p.get("grip_ref") is None:
                continue
            s = sockets.get("socket_mount_hand_" + ("l" if p["side"] == "Left" else "r"))
            if s is not None:
                # measured against geometry: grip -> far end of the prop (blade rule) or the upward
                # shaft axis (staff/spear rule), not against the roll hint itself
                z = s.matrix_world.col[2].to_3d().normalized()
                ref = hl.vec(p["grip_ref"]).normalized()
                ang = float(np.degrees(np.arccos(np.clip(float(z.dot(ref)), -1, 1))))
                grip_align[s.name] = round(ang, 1)
                grip_rules[s.name] = {"rule": p["grip_rule"], "grip_fraction": round(float(p["grip_frac"]), 3),
                                      "socket_z": hl.rnd(z, 3), "reference": hl.rnd(ref, 3)}
        stats["grip_socket_vs_prop_axis_deg"] = grip_align
        stats["grip_socket_rule"] = grip_rules
    # input sockets the rig does not build itself: child of the bone that drives the vertices around
    # them (dominant skin weight of the nearest vertices; params.socket_bones overrides)
    extra_in = {n: v for n, v in in_socks.items() if n not in sockets}
    if extra_in:
        sock_bones = hl.bones_for_points(body, names, W, [v[0].translation for v in extra_in.values()])
        over = params.get("socket_bones") or {}
        kept = {}
        for (n, (Mw, props)), (bone, how) in zip(extra_in.items(), sock_bones):
            if n in over:
                bone, how = hl.b(hl.short(over[n])), "params.socket_bones"
                if bone not in rig.data.bones:
                    raise ValueError("params.socket_bones: unknown bone %s" % over[n])
            elif props.get("af_bone") and hl.b(hl.short(str(props["af_bone"]))) in rig.data.bones:
                # an extract_props placement socket names the hand that held the item
                bone, how = hl.b(hl.short(str(props["af_bone"]))), "socket extras af_bone"
            e = af.add_socket(n, parent=rig, bone=bone, kind=props.get("kind"),
                              size=0.03, **{k: v for k, v in props.items() if k not in ("kind", "game_socket")})
            e.matrix_world = Mw
            sockets[n] = e
            kept[n] = {"bone": hl.short(bone), "from": how}
        bpy.context.view_layer.update()
        stats["sockets_from_input"] = kept
    stats["input_sockets"] = sorted(in_socks)

    # ---- QA
    nb = len(rig.data.bones)
    checks.append(af.check("unweighted_vertices", unweighted == 0, unweighted, 0))
    checks.append(af.check("bones", nb in (25, 65), nb, "25|65"))
    infl = int((W > 1e-4).sum(1).max())
    checks.append(af.check("max_influences", infl <= maxinf, infl, maxinf))
    X0m = lm.X0 * H
    sym, sym_per = hl.symmetry_error(J, X0m, H, ["Shoulder", "Arm", "UpLeg", "Leg", "Foot", "ToeBase"])
    checks.append(af.check("joint_symmetry", sym <= 0.03, sym, 0.03, severity="warning",
                           note="max mirror error / height over shoulders and legs"))
    arm_sym, arm_per = hl.symmetry_error(J, X0m, H, ["ForeArm", "Hand"])
    hips_x = float(J["Hips"][0])
    checks.append(af.check("centred", abs(hips_x) < 0.01, round(hips_x, 4), 0.01,
                           note="Hips x (m): contract 4, x = 0 is the trunk symmetry axis"))
    ratio_issues = []
    for side, r in lm.ratios.items():
        if not (0.85 <= r["upper_fore"] <= 1.6):
            ratio_issues.append("%s upper/forearm %.2f" % (side, r["upper_fore"]))
        if not (0.8 <= r["thigh_shin"] <= 1.35):
            ratio_issues.append("%s thigh/shin %.2f" % (side, r["thigh_shin"]))
        if not (0.24 <= r["arm"] <= 0.40):
            ratio_issues.append("%s arm %.2f H" % (side, r["arm"]))
        if not (0.38 <= r["leg"] <= 0.56):
            ratio_issues.append("%s leg %.2f H" % (side, r["leg"]))
    checks.append(af.check("limb_ratios", not ratio_issues, lm.ratios,
                           "upper/fore 0.85-1.6, thigh/shin 0.8-1.35, arm 0.24-0.40 H, leg 0.38-0.56 H",
                           severity="warning", note="; ".join(ratio_issues) or None))
    for bone, mask, rec in rigid_sets:
        col = names.index(bone)
        if mask.any():
            leak = float(1.0 - W[mask, col].min())
            checks.append(af.check("rigid_%s_%s" % (rec.get("kind", "rule"), hl.short(bone)), leak < 1e-3,
                                   round(leak, 5), 0.001, note="%d vertices rigid to %s" % (mask.sum(), bone)))
    auto_sets = [rec for _, _, rec in rigid_sets if rec.get("kind") in ("prop", "plate", "bow")]
    if lm.props or getattr(lm, "plate_props", None) or lm.bow_props:
        loose = [rec for rec in auto_sets if rec["vertices"] == 0]
        expected = (len(lm.props) + len(lm.plate_props) + len(lm.bow_props)) if params.get("auto_rigid", True) else 0
        checks.append(af.check("props_rigid", not loose and len(auto_sets) >= expected,
                               len(auto_sets) - len(loose), expected,
                               note="every detected prop/shield is rigid to one bone (never left to "
                                    "bone heat)" + ("; without vertices: %s" % loose if loose else "")))
    deform = None
    if hard_info.get("enabled") and params.get("deform_test", True) and params.get("hard_surface_compare", True):
        # the pose test once more with the smooth weights (hard surface off): before / after in the report
        hl.apply_rigid(W_soft, names, rigid_sets)
        W_soft = hl.limit_normalize(W_soft, maxinf)
        hl.write_weights(body, names, W_soft)
        d0 = hl.deformation_test(body, rig, names, W_soft, H, intersections=False, region=metal_region)
        hl.write_weights(body, names, W)
        hl.reset_pose(rig)
        hard_info["pose_test_soft"] = {p: {"torn_pct": round(100 * d0[p]["torn_area_frac"], 2),
                                           "stretched_pct": round(100 * d0[p]["stretched_area_frac"], 2),
                                           "plate_rubber_pct": round(100 * d0[p].get("region_rubber_frac", 0.0), 2)}
                                       for p in hl.TEST_POSES}
    gate = str(params.get("deform_gate", "error")).lower()
    if gate not in ("error", "warning"):
        raise ValueError("params.deform_gate must be error or warning")
    if params.get("deform_test", True):
        robe_zone = None
        if lm.robe:
            robe_zone = verts[:, 2] < (lm.crotch + 0.07) * H
        deform = hl.deformation_test(body, rig, names, W, H, robe_zone=robe_zone,
                                     intersections=bool(params.get("intersections", True)), log=af.log,
                                     region=metal_region if hard_info.get("enabled") else None)
        if hard_info.get("enabled"):
            hard_info["pose_test_hard"] = {p: {"torn_pct": round(100 * deform[p]["torn_area_frac"], 2),
                                               "stretched_pct": round(100 * deform[p]["stretched_area_frac"], 2),
                                               "plate_rubber_pct": round(100 * deform[p].get("region_rubber_frac", 0.0), 2)}
                                           for p in hl.TEST_POSES}
        lim = hl.TORN_LIMITS
        worst_pose = max(("arms_forward", "squat", "stride"), key=lambda p: deform[p]["torn_area_frac"])
        torn = deform[worst_pose]["torn_area_frac"]
        checks.append(af.check("deform_torn_area", torn <= lim["main"], torn, lim["main"], severity=gate,
                               note="largest share of the POSED surface made of triangles that grew > 3x "
                                    "(arms forward / squat / stride; worst: %s, %.3f m2, mostly %s); "
                                    "floor caps excluded" % (
                                        worst_pose, deform[worst_pose]["torn_area_m2"],
                                        ", ".join(deform[worst_pose].get("torn_by_bone_m2") or {}) or "-")))
        tu = deform["arms_up"]["torn_area_frac"]
        checks.append(af.check("deform_torn_area_arms_up", tu <= lim["arms_up"], tu, lim["arms_up"], severity=gate,
                               note="arms straight up (170 deg): 2-5 %% of armpit stretch is normal for linear "
                                    "blend skinning; above %.0f %% a sleeve/robe web becomes a sheet" % (
                                        lim["arms_up"] * 100)))
        worst = max(deform[p]["max_stretch"] for p in hl.TEST_POSES)
        checks.append(af.check("deform_max_edge_stretch", worst <= 6.0, worst, 6.0, severity="warning",
                               note="worst edge length ratio, sliver edges excluded"))
        p999 = max(deform[p]["p999_stretch"] for p in hl.TEST_POSES)
        checks.append(af.check("deform_p999_edge_stretch", p999 <= 3.5, p999, 3.5, severity="warning"))
        if lm.robe:
            rs = deform["stride"].get("robe_torn_area_frac")
            checks.append(af.check("robe_stride_tear", rs is not None and rs <= lim["robe_stride"], rs,
                                   lim["robe_stride"], severity=gate,
                                   note="share of the posed robe surface torn (> 3x area) in the stride pose"))
        if params.get("intersections", True):
            nsi = max(deform[p]["new_self_intersection_frac"] for p in hl.TEST_POSES)
            checks.append(af.check("deform_new_self_intersections", nsi <= 0.02, nsi, 0.02,
                                   severity="warning", note="fraction of triangles newly intersecting"))
        outputs["poses_png"] = hl.render_poses(af.out_path(req, "poses.png"), body, rig)

    # ---- exports
    export_objs = [rig, body] + list(sockets.values())
    outputs["glb"] = af.export_glb(af.out_path(req, "rigged.glb"), objects=export_objs, animations=False)
    outputs["blend"] = af.save_blend(af.out_path(req, "rigged.blend"))
    stats.update({
        "tris": hl.tri_count(body), "verts": len(body.data.vertices), "bones": nb,
        "deform_bones": len(names), "skin": skin_info, "rigid": rigid_counts, "robe_blend": robe_info,
        "hard_surface": hard_info,
        "sockets": sorted(sockets), "joint_symmetry": sym_per, "arm_symmetry": arm_per,
        "deformation": deform, "deform_gate": gate if deform else None, "fingers": finger_info, "bbox": prep["bbox"],
        "roll_convention": "bone +Y head->tail; +Z = character front (-Y) for spine/arms/legs, "
                           "+Z up for Foot/ToeBase/Toe_End (GLOBAL_NEG_Y / GLOBAL_POS_Z)",
        "seconds_total": round(time.time() - t0, 1),
    })
    return {"outputs": outputs, "stats": stats, "checks": checks, "warnings": warnings}
