"""retarget - Mixamo (or any humanoid) clips onto a humanoid rig, contract section 7.

inputs.blend  - .blend with a skinned humanoid armature (af_run opens it), or
inputs.mesh   - skinned .glb/.gltf/.fbx with a humanoid armature.
params:
  clips: [{source: abs .fbx | "library:<id>" | "<id>", name, trim: [first, last] (source frames),
           loop (default by category: idle/walk/run), in_place (see root_motion), fps (30),
           speed (1.0), mirror (false), locomotion (default: category walk/run, or the library
           entry's "locomotion" flag / tag)}]
  library: clip library folder (default ASTRA_FORGE_LIBRARY/animations, else C:/AstraForge/library/animations)
  root_motion: "lock" (default) - locomotion clips (walk/run) are played in place: the horizontal
                                  hips travel is removed and the game moves the character at
                                  clips.json ground_speed_mps. Every other clip (idle, attack,
                                  hit, cast, death, ...) KEEPS its hips motion on the Hips, so feet
                                  planted in the source stay planted. An explicit in_place wins
                                  (in_place:true on a standing clip makes the feet skate - the
                                  foot_skate check reports it);
               "keep"           - travel stays on the Hips unless in_place:true;
               "extract"        - travel moves to a non-deforming root bone (Godot root motion);
                                  an existing top bone above the Hips (Root/root) is reused,
                                  otherwise 'root' is added at the origin.
  leg_ik: true   - analytic two-bone leg IK keeps the feet on the (scaled) source trajectory
  foot_pin: "ball" (default) - while the source ball of the foot is on the ground the target ball
            is pinned (ankle otherwise); "ankle" - ankle only
  loop_blend: 8  - frames of the seam cross-fade for loop clips
  facing: "keep" (default) | "rest" - the clip's mean yaw of facing_bone (Hips; e.g. "Spine2") is
          removed: a sideways stance (archer idle, ~45 deg) faces the rest direction. Per clip too
  hips_drop: 1.0 - share of the hips going below their rest height that is kept (0: never below rest,
             the legs straighten by IK - a robe hem weighted to the hips stays on the floor in a
             crouching idle; a death needs 1). Per clip too
  hips_height_m - explicit target hip height for the root scale k (the measured one is a guess under
                  a robe)
  floor: true - leg IK targets never below the target's rest ankle / ball heights
  ground: "feet" (default: only the leg IK floor above) | "mesh" - a frame whose lowest skinned point
          (ground_samples 2000 random body vertices, linear blend skinning) is below z = 0 lifts the
          whole body by that much, smoothed over +-2 frames (a robe lying in a death clip rests on the
          floor instead of sinking into it); ground_tolerance_m 0. Per clip too
  hand_floor: {"l"|"r": dz} - arm IK keeps that wrist at most dz below its rest height; the hand keeps
              its world orientation (a held staff stays out of the floor). Per clip too
  floor_tolerance_m: 0.02, floor_samples: 16 - clips.json "floor": lowest point of the skinned body and
              of the props under the rig over sampled frames; warnings floor_body_* / floor_props_*
  apply_transforms: false - bake a rotated/scaled rig object (and its meshes) to identity first;
                            otherwise a non-identity rig transform is only reported (warning)
  preview: true, preview_cols: 5, preview_tile: 256
  keep_existing_actions: false - otherwise the actions of the target rig (assigned, in its NLA or
                                 orphan actions of its bones) are removed so the GLB holds exactly
                                 the requested clips; actions of other objects stay in the .blend
                                 and are kept out of the GLB
outputs: animated.blend, animated.glb (one glTF animation per clip, "-loop" suffix for loops,
         skin preserved, the rig's whole hierarchy incl. contract section 6 sockets and props),
         clips.json, preview.png (row per clip, three-quarter view).
"""
import json
import math
import os

import bpy
from mathutils import Vector

import af_common as af

try:
    from steps import _anim_lib as lib
except ImportError:  # imported with steps/ itself on sys.path
    import _anim_lib as lib


def _side_map(spec, what):
    out = {}
    for k, v in (spec or {}).items():
        side = {"l": "Left", "left": "Left", "r": "Right", "right": "Right"}.get(str(k).lower())
        if side is None:
            raise ValueError("%s keys must be l / r" % what)
        out[side] = float(v)
    return out


def _clip_configs(clips, library, mode, P=None):
    P = P or {}
    cfgs, names = [], set()
    for i, c in enumerate(clips):
        if isinstance(c, str):
            c = {"source": c}
        if not c.get("source"):
            raise ValueError("clip %d has no source" % i)
        path, entry = lib.resolve_clip_source(c["source"], library)
        stem = os.path.splitext(os.path.basename(path))[0]
        if c.get("name"):
            name = str(c["name"]).strip()
        elif entry:
            name = entry["id"]
            pre = (entry.get("source") or "mixamo") + "-"
            if name.startswith(pre):
                name = name[len(pre):]
        else:
            name = lib.slug(stem)
        category = (entry or {}).get("category") or lib.guess_category(name + " " + stem)
        loop = bool(c.get("loop", lib.default_loop(category)))
        locomotion = lib.is_locomotion(category, entry, c)
        # lock: only locomotion is played in place, standing clips keep their hips motion
        raw_ip = c["in_place"] if "in_place" in c else (mode == "lock" and locomotion)
        strict = isinstance(raw_ip, str) and raw_ip.strip().lower() == "strict"
        if isinstance(raw_ip, str) and not strict:
            raise ValueError("clip %s: in_place must be true, false or 'strict'" % name)
        in_place = "strict" if strict else bool(raw_ip)
        if in_place:
            motion = "in_place_strict" if strict else "in_place"
        else:
            motion = "extract" if mode == "extract" else "keep"
        base = name[:-5] if name.endswith("-loop") else name
        gltf = base + "-loop" if loop else base
        if gltf in names:
            raise ValueError("duplicate clip name: %s" % gltf)
        names.add(gltf)
        trim = c.get("trim")
        if trim is not None and (len(trim) != 2 or float(trim[1]) <= float(trim[0])):
            raise ValueError("clip %s: trim must be [first, last] with last > first" % name)
        cfgs.append({"index": i, "name": base, "gltf_name": gltf, "path": path,
                     "library_id": entry["id"] if entry else None, "category": category,
                     "loop": loop, "in_place": in_place, "motion": motion,
                     "locomotion": locomotion,
                     "seam_travel": loop and motion in ("keep", "extract") and not locomotion,
                     "fps": float(c.get("fps", 30)), "speed": float(c.get("speed", 1.0)),
                     "mirror": bool(c.get("mirror", False)), "trim": trim,
                     "facing": str(c.get("facing", P.get("facing", "keep"))).lower(),
                     "facing_bone": c.get("facing_bone", P.get("facing_bone")),
                     "hips_drop": float(c.get("hips_drop", P.get("hips_drop", 1.0))),
                     "hand_floor": _side_map(c.get("hand_floor", P.get("hand_floor")), "hand_floor"),
                     "ground": str(c.get("ground", P.get("ground", "feet"))).lower()})
        if cfgs[-1]["ground"] not in ("feet", "mesh"):
            raise ValueError("clip %s: ground must be feet or mesh" % name)
        if cfgs[-1]["facing"] not in ("keep", "rest"):
            raise ValueError("clip %s: facing must be keep or rest" % name)
        if not 0.0 <= cfgs[-1]["hips_drop"] <= 1.0:
            raise ValueError("clip %s: hips_drop must be within 0..1" % name)
        if cfgs[-1]["speed"] <= 0 or cfgs[-1]["fps"] <= 0:
            raise ValueError("clip %s: fps and speed must be positive" % name)
    return cfgs


def run(req):
    P, I = req["params"], req.setdefault("inputs", {})
    os.makedirs(req["out_dir"], exist_ok=True)
    library = P.get("library") or lib.DEFAULT_LIBRARY
    mode = P.get("root_motion", "lock")
    if mode not in ("lock", "keep", "extract"):
        raise ValueError("root_motion must be lock|keep|extract, got %r" % mode)
    leg_ik = bool(P.get("leg_ik", True))
    clips = P.get("clips") or []
    if not clips:
        raise ValueError("params.clips is empty")
    warnings, checks = [], []

    # ---------------------------------------------------------------- target
    if I.get("blend"):
        want = os.path.normcase(os.path.abspath(I["blend"]))
        if os.path.normcase(os.path.abspath(bpy.data.filepath or "")) != want:
            af.open_blend(I["blend"])
        objs = list(bpy.context.scene.objects)
    elif I.get("mesh"):
        af.reset_scene()
        objs = af.import_any(I["mesh"])
    else:
        raise ValueError("inputs.blend or inputs.mesh is required")
    rig, scheme, tmap = lib.find_target_rig(objs)
    af.log("target rig", rig=rig.name, scheme=scheme, bones=len(rig.data.bones), mapped=len(tmap))
    missing = [c for c in lib.ESSENTIAL if c not in tmap.values()]
    if missing:
        warnings.append("target lacks essential bones: %s" % ", ".join(missing))
    cfgs = _clip_configs(clips, library, mode, P)

    # contract section 4: the rig object should carry no rotation/scale (Godot gets the node as is)
    meshes = lib.skinned_meshes(rig)
    ts = lib.transform_state(rig)
    if not ts["identity"] and P.get("apply_transforms"):
        try:
            done = lib.apply_rig_transforms(rig, meshes)
            warnings.append("applied rotation %.2f deg / scale %s of %s to: %s" %
                            (ts["rotation_deg"], ts["scale"], rig.name, ", ".join(done)))
            ts = lib.transform_state(rig)
        except Exception as exc:
            warnings.append("apply_transforms failed (%s) - the rig keeps its transform" % exc)
    checks.append(af.check("rig_transform_identity", ts["identity"],
                           {"rotation_deg": ts["rotation_deg"], "scale": ts["scale"]},
                           {"rotation_deg": 0, "scale": [1, 1, 1]}, severity="warning",
                           note="contract section 4: applied transforms; params.apply_transforms "
                                "bakes them"))
    if not ts["identity"]:
        warnings.append("rig %s has object rotation %.2f deg / scale %s: the GLB armature node "
                        "carries it (contract section 4 wants applied transforms; set "
                        "params.apply_transforms)" % (rig.name, ts["rotation_deg"], ts["scale"]))

    keep_existing = bool(P.get("keep_existing_actions"))
    mine = lib.rig_actions(rig, meshes)
    if not keep_existing:
        old = [a.name for a in mine]
        for o in [rig] + meshes:
            ad = o.animation_data
            if ad is not None:
                ad.action = None
                for t in list(ad.nla_tracks):
                    ad.nla_tracks.remove(t)
        for a in mine:
            bpy.data.actions.remove(a)
        mine = []
        if old:
            warnings.append("removed %d pre-existing action(s) of %s: %s" %
                            (len(old), rig.name, ", ".join(old[:8])))
    others = [a.name for a in bpy.data.actions if a not in mine]
    if others:
        warnings.append("%d action(s) of other objects kept in the .blend, not exported: %s" %
                        (len(others), ", ".join(others[:8])))
    if rig.animation_data:
        rig.animation_data.action = None
    for pb in rig.pose.bones:
        if len(pb.constraints):
            warnings.append("bone %s has constraints - they act on top of the clips" % pb.name)
    hips_name = next(n for n, c in tmap.items() if c == "Hips")
    root, notes = lib.prepare_rig(rig, hips_name, want_root=any(c["motion"] == "extract"
                                                                for c in cfgs))
    warnings += notes
    tr = lib.TargetRig(rig, tmap)
    tr.root = root
    if P.get("hips_height_m"):
        # the measured hip height (UpLeg heads over the lowest foot) is a guess under a robe; an
        # explicit value sets the root scale k = hips_height_m / source hip height
        warnings.append("hip height %.3f m (measured %.3f m) from params.hips_height_m" %
                        (float(P["hips_height_m"]), tr.height))
        tr.height = float(P["hips_height_m"])
    for b in tr.order:
        if not b.use_inherit_rotation or b.inherit_scale not in ("FULL", "FIX_SHEAR", "AVERAGE"):
            warnings.append("bone %s inherits non-standard - rotations may diverge" % b.name)

    stick = None
    if not meshes:
        warnings.append("no skinned mesh bound to %s - preview uses bone sticks, GLB has the "
                        "armature only" % rig.name)

    scene = bpy.context.scene
    out_fps = cfgs[0]["fps"]
    for c in cfgs[1:]:
        if abs(c["fps"] - out_fps) > 1e-6:
            warnings.append("clip %s asks fps %g; one GLB has one sampling rate - using %g" %
                            (c["name"], c["fps"], out_fps))
            c["fps"] = out_fps
    hidden = []
    for m in meshes:
        if not m.hide_viewport:
            m.hide_viewport = True
            hidden.append(m)

    ground_samples = None
    if any(c["ground"] == "mesh" for c in cfgs) and meshes:
        ground_samples = lib.skin_samples(rig, meshes, n=int(P.get("ground_samples", 2000)))

    # ---------------------------------------------------------------- clips
    report, actions = [], []
    for cfg in cfgs:
        af.log("clip", name=cfg["gltf_name"], source=os.path.basename(cfg["path"]))
        before = lib.snapshot()
        keep_fps = (scene.render.fps, scene.render.fps_base)
        src, sact, src_fps, has_skin = lib.import_fbx_clip(cfg["path"])
        sscheme, smap = lib.map_armature(src)
        r0, r1 = sact.frame_range
        if cfg["trim"]:
            f0, f1 = float(cfg["trim"][0]), float(cfg["trim"][1])
            if f0 < r0 - 1e-6 or f1 > r1 + 1e-6:
                warnings.append("clip %s: trim %s outside source range %s - ends hold" %
                                (cfg["name"], cfg["trim"], [r0, r1]))
        else:
            f0, f1 = float(math.floor(r0)), float(math.ceil(r1))
        n = max(2, int(round((f1 - f0) / src_fps / cfg["speed"] * cfg["fps"])) + 1)
        times = [f0 + (f1 - f0) * i / (n - 1) for i in range(n)]
        opts = {"leg_ik": leg_ik, "mirror": cfg["mirror"], "loop": cfg["loop"],
                "loop_blend": int(P.get("loop_blend", 8)), "motion": cfg["motion"],
                "seam_travel": cfg["seam_travel"], "foot_pin": P.get("foot_pin", "ball"),
                "facing": cfg["facing"], "facing_bone": cfg["facing_bone"], "hips_drop": cfg["hips_drop"],
                "floor": bool(P.get("floor", True)), "hand_floor": cfg["hand_floor"],
                "ground": cfg["ground"], "ground_samples": ground_samples if cfg["ground"] == "mesh" else None,
                "ground_tolerance": float(P.get("ground_tolerance_m", 0.0))}
        keys, met, travel = lib.retarget_clip(tr, src, smap, times, opts)
        lib.cleanup(before)
        scene.render.fps, scene.render.fps_base = keep_fps
        act = lib.write_action(rig, cfg["gltf_name"], keys, n)
        act.use_cyclic = cfg["loop"]
        actions.append(act)
        ev = lib.evaluate_action(
            tr, act, n, root_travel=cfg["motion"] == "extract" and not cfg["seam_travel"],
            hips_travel=cfg["motion"] == "keep" and not cfg["seam_travel"])
        # ---- feet, judged in the frame the GLB is actually played in. Only an in-place
        # locomotion clip is moved by the game (at a constant ground velocity); every other clip
        # plays exactly as exported (with root motion applied by Godot for 'extract').
        v_src = lib.source_ground_velocity(met["src_feet"])
        game_v = Vector((0.0, 0.0, 0.0))
        if cfg["in_place"] and cfg["locomotion"]:
            game_v = (travel[-1] - travel[0]) / max(n - 1, 1) - v_src
        game = [game_v * i for i in range(n)]
        feet = {s: {k: [p + game[i] if p is not None else None for i, p in enumerate(v)]
                    for k, v in tracks.items()} for s, tracks in ev["feet_tracks"].items()}
        # skating against the SOURCE contacts: frames where the source ball/ankle is on the
        # ground and still (in its ground frame) - the target's contact point should not move
        contacts = lib.source_contacts(met["src_feet"], cfg["fps"], v_src)
        sk = lib.contact_skate(feet, contacts)
        sk_src = lib.contact_skate(met["src_feet"], contacts, offset=[-v_src * i for i in range(n)])
        skate, src_skate = sk["skate_cm"], sk_src["skate_cm"]
        skate_lim = round(max(2.0, 1.3 * src_skate), 1)
        # legacy self-contact heuristic (kept for comparison with earlier reports)
        fs = lib.foot_slide(feet, cfg["fps"])
        fs_src = lib.foot_slide(met["src_feet"], cfg["fps"])
        slide, src_slide = fs["slide_cm"], fs_src["slide_cm"]
        net_travel = travel[-1].length
        h0, h1 = ev["hips_track"][0], ev["hips_track"][-1]
        end_offset = math.hypot(h1.x - h0.x, h1.y - h0.y)
        consistency = max(((a - b).length for a, b in zip(ev["hips_track"], met["hips_internal"])),
                          default=0.0)
        tw = met["hand_twist_deg"]
        tw_max = max([abs(v["target"]) for v in tw.values() if v["target"] is not None] or [0.0])
        tw_dev = max([abs(v["target"] - v["source"]) for v in tw.values()
                      if v["target"] is not None and v["source"] is not None] or [0.0])
        root_travel = (travel[-1].length if cfg["motion"] == "extract" else 0.0)
        rec = {
            "name": cfg["name"], "gltf_name": cfg["gltf_name"], "source": cfg["path"],
            "library_id": cfg["library_id"], "category": cfg["category"],
            "source_skeleton": sscheme, "source_has_skin": has_skin, "source_fps": src_fps,
            "trim": [f0, f1], "frames": n, "fps": cfg["fps"], "C_angle_deg": met["C_angle_deg"],
            "duration": round((n - 1) / cfg["fps"], 4), "loop": cfg["loop"],
            "in_place": cfg["in_place"], "root_motion": cfg["motion"], "speed": cfg["speed"],
            "locomotion": cfg["locomotion"],
            "mirror": cfg["mirror"], "k_hip": met["k_hip"], "leg_ik": met["leg_ik"],
            "foot_skate_cm": skate, "foot_skate_src_cm": src_skate,
            "foot_plant_drift_cm": sk["drift_cm"], "foot_plant_drift_src_cm": sk_src["drift_cm"],
            "foot_skate_per_side": sk["per_side"], "foot_skate_src_per_side": sk_src["per_side"],
            "foot_slide_cm": slide, "foot_slide_src_cm": src_slide,
            "foot_contact_raw_cm": fs["raw_cm"], "foot_moving_frames": fs["moving_frames"],
            "foot_moving_frames_src": fs_src["moving_frames"],
            "ground_speed_mps": round(game_v.length * cfg["fps"], 3),
            "ground_velocity_mps": [round(x * cfg["fps"], 3) for x in game_v],
            "ground_speed_src_mps": round(v_src.length * cfg["fps"], 3),
            "hand_twist_deg": tw, "hand_twist_max_deg": tw_max,
            "hips_drift_m": ev["hips_drift_m"], "hips_end_offset_m": round(end_offset, 4),
            "hips_net_travel_m": round(net_travel, 4),
            "root_travel_m": round(root_travel, 4),
            "seam_rot_deg": ev["seam_rot_deg"] if cfg["loop"] else None,
            "seam_pos_mm": ev["seam_pos_mm"] if cfg["loop"] else None,
            "loop_blend_frames": met["loop_blend_frames"],
            "ball_pinned_frames": met["ball_pinned_frames"],
            "ik_unreached_frames": met["ik_unreached_frames"],
            "ik_worst_miss_cm": met["ik_worst_miss_cm"],
            "arm_dir_err_deg": met["arm_dir_err_deg"], "nan": ev["nan"],
            "eval_vs_internal_mm": round(consistency * 1000.0, 3),
            "mapped_bones": met["mapped_bones"], "unmapped_bones": met["unmapped_bones"],
            "degenerate_source_bones": met["degenerate_source_bones"],
            "facing": met["facing"], "hips_drop": met["hips_drop"], "ground": met["ground"],
            "floor_clamped_frames": met["floor_clamped_frames"], "hand_floor_frames": met["hand_floor_frames"],
        }
        report.append(rec)
        tag = cfg["gltf_name"]
        checks.append(af.check("no_nan_" + tag, ev["nan"] == 0, ev["nan"], 0))
        checks.append(af.check("eval_matches_internal_" + tag, consistency < 1e-3,
                               round(consistency * 1000, 3), 1.0, note="mm"))
        if cfg["loop"]:
            ok = ev["seam_rot_deg"] <= 0.5 and ev["seam_pos_mm"] <= 1.0
            checks.append(af.check("loop_seam_" + tag, ok,
                                   [ev["seam_rot_deg"], ev["seam_pos_mm"]], [0.5, 1.0],
                                   note="deg, mm between first and last pose"))
        if cfg["in_place"] == "strict":
            checks.append(af.check("in_place_" + tag, ev["hips_drift_m"] <= 0.001,
                                   ev["hips_drift_m"], 0.001, note="strict: hips XY drift, m"))
        elif cfg["in_place"]:
            checks.append(af.check("in_place_" + tag, end_offset <= 0.001, round(end_offset, 4),
                                   0.001, note="hips XY end vs start, m (sway %.3f m kept)" %
                                   ev["hips_drift_m"]))
        checks.append(af.check("foot_skate_" + tag, skate <= skate_lim, skate, skate_lim,
                               severity="warning",
                               note="cm the target contact point moves while the source foot is "
                                    "planted, vs max(2, 1.3 x source); plant drift %.1f cm "
                                    "(source %.1f)" % (sk["drift_cm"], sk_src["drift_cm"])))
        checks.append(af.check("hand_twist_" + tag, tw_dev <= 25.0, round(tw_dev, 1), 25.0,
                               severity="warning", note="target vs source twist span, deg"))
        if (cfg["motion"] == "keep" and not cfg["loop"] and not cfg["locomotion"]
                and net_travel > 0.25):
            warnings.append("clip %s ends %.2f m from its start: the travel stays on the Hips so "
                            "planted feet stay planted; without root_motion 'extract' the game "
                            "sees a snap back at the clip end" % (tag, net_travel))
        af.log("clip done", name=tag, frames=n, skate_cm=skate, src_skate_cm=src_skate,
               plant_drift_cm=sk["drift_cm"], seam=rec["seam_rot_deg"], drift=ev["hips_drift_m"])

    # ---------------------------------------------------------------- outputs
    for m in hidden:
        m.hide_viewport = False
    scene.render.fps = int(round(out_fps))
    scene.render.fps_base = 1.0
    scene.frame_start = 0
    scene.frame_end = report[0]["frames"] - 1
    lib.assign_action(rig, actions[0])
    scene.frame_set(0)

    # ---- floor: lowest point of the skinned body and of the props (meshes under the rig) per clip
    fl_lim = float(P.get("floor_tolerance_m", 0.02))
    descend = [o for o in bpy.context.scene.objects if o.type == "MESH" and o not in meshes and
               _under(o, rig)]
    for act, rec in zip(actions, report):
        fr = lib.floor_report(rig, meshes, descend, act, rec["frames"], samples=int(P.get("floor_samples", 16)))
        rec["floor"] = fr
        tag = rec["gltf_name"]
        if fr.get("body_min_z_m") is not None:
            checks.append(af.check("floor_body_" + tag, fr["body_min_z_m"] >= -fl_lim, fr["body_min_z_m"], -fl_lim,
                                   severity="warning", note="lowest point of the skinned body over %d sampled "
                                   "frames, m (z = 0 is the floor)" % fr["frames_sampled"]))
        if fr.get("props_min_z_m") is not None:
            checks.append(af.check("floor_props_" + tag, fr["props_min_z_m"] >= -fl_lim, fr["props_min_z_m"],
                                   -fl_lim, severity="warning", note="lowest point of the props held by the rig, m"))
    lib.assign_action(rig, actions[0])
    scene.frame_set(0)

    outputs = {}
    # the rig with its whole hierarchy: sockets (contract section 6), props on sockets, child
    # meshes - godot_package reads this GLB, nothing of the character may be dropped
    mine = [a for a in mine if _alive(a)]  # a kept action may have been replaced by a new clip
    exp_objs, notes = lib.export_objects(rig, meshes)
    warnings += notes
    sockets_in = sorted({o["game_socket"] for o in exp_objs if "game_socket" in o.keys()})
    with lib.gltf_only_actions(actions + mine, exp_objs, rig) as only:
        glb = af.export_glb(af.out_path(req, "animated.glb"), objects=exp_objs,
                            animations=True, skins=True, animation_mode="ACTIONS")
    warnings += only.notes
    outputs["glb"] = glb
    summ = lib.glb_summary(glb)
    got = [a["name"] for a in summ["animations"]]
    want = [c["gltf_name"] for c in cfgs] + [a.name for a in mine]
    checks.append(af.check("gltf_animations", sorted(got) == sorted(want), got, want))
    checks.append(af.check("gltf_sockets", set(sockets_in) <= set(summ["sockets"]),
                           summ["sockets"], sockets_in,
                           note="every input object with game_socket is a GLB node with "
                                "extras.game_socket"))
    checks.append(af.check("gltf_no_nan", all(a["nan"] == 0 for a in summ["animations"]),
                           sum(a["nan"] for a in summ["animations"]), 0))
    for rec in report:
        a = next((x for x in summ["animations"] if x["name"] == rec["gltf_name"]), None)
        rec["gltf_duration"] = a["duration"] if a else None
    if meshes:
        checks.append(af.check("gltf_skin", bool(summ["skins"]) and summ["skinned_primitives"] > 0,
                               {"skins": summ["skins"], "skinned_primitives":
                                summ["skinned_primitives"]}, ">=1"))
    outputs["blend"] = af.save_blend(af.out_path(req, "animated.blend"))

    if P.get("preview", True):
        pm = meshes
        if not pm:
            stick = make_stick_mesh(rig)
            pm = [stick]
        outputs["preview"] = lib.render_clip_grid(
            af.out_path(req, "preview.png"), tr, pm,
            [(a, r["loop"], r["frames"]) for a, r in zip(actions, report)],
            cols=int(P.get("preview_cols", 5)), tile=int(P.get("preview_tile", 256)))
        if stick is not None:
            me = stick.data
            bpy.data.objects.remove(stick, do_unlink=True)
            bpy.data.meshes.remove(me)

    doc = {"rig": rig.name, "scheme": scheme, "fps": out_fps, "root_motion": mode,
           "root_bone": root, "leg_ik": leg_ik, "glb": glb, "rig_transform": ts,
           "exported_objects": [o.name for o in exp_objs], "sockets": summ["sockets"],
           "target_mapped": {n: c for n, c in tmap.items()},
           "target_unmapped": [b.name for b in rig.data.bones if b.name not in tmap],
           "clips": report}
    cj = af.out_path(req, "clips.json")
    with open(cj, "w", encoding="utf-8") as fh:
        json.dump(af._jsonable(doc), fh, ensure_ascii=False, indent=1)
    outputs["clips"] = cj

    stats = {"rig": rig.name, "scheme": scheme, "bones": len(rig.data.bones),
             "mapped_bones": len(tmap), "clips": len(report),
             "frames_total": sum(r["frames"] for r in report),
             "tris": af.tri_count(meshes) if meshes else 0,
             "gltf_animations": got, "gltf_joints": summ["skins"][0]["joints"] if summ["skins"] else 0,
             "gltf_sockets": summ["sockets"],
             "per_clip": {r["gltf_name"]: {k: r[k] for k in (
                 "frames", "root_motion", "foot_skate_cm", "foot_skate_src_cm",
                 "foot_plant_drift_cm", "foot_plant_drift_src_cm", "foot_slide_cm",
                 "foot_slide_src_cm", "hips_drift_m", "seam_rot_deg", "k_hip",
                 "ground_speed_mps")} for r in report}}
    return {"outputs": outputs, "stats": stats, "checks": checks, "warnings": warnings}


def _under(ob, rig):
    p = ob.parent
    while p is not None:
        if p == rig:
            return True
        p = p.parent
    return False


def _alive(idb):
    try:
        idb.name
        return True
    except ReferenceError:
        return False


def make_stick_mesh(rig):
    """Thin box per deforming bone, rigidly weighted - preview body for rigs without a mesh."""
    me = bpy.data.meshes.new("af_sticks")
    verts, faces, groups = [], [], []
    for b in rig.data.bones:
        if not b.use_deform:
            continue
        h, t = b.head_local, b.tail_local
        axis = t - h
        if axis.length < 1e-5:
            continue
        r = max(min(axis.length * 0.12, 0.05), 0.008)
        u = axis.orthogonal().normalized() * r
        v = axis.normalized().cross(u).normalized() * r
        base = len(verts)
        for p in (h, t):
            for du, dv in ((1, 1), (-1, 1), (-1, -1), (1, -1)):
                verts.append(p + u * du + v * dv)
        faces += [(base, base + 1, base + 2, base + 3), (base + 4, base + 7, base + 6, base + 5)]
        for i in range(4):
            j = (i + 1) % 4
            faces.append((base + i, base + 4 + i, base + 4 + j, base + j))
        groups.append((b.name, list(range(base, base + 8))))
    me.from_pydata([tuple(v) for v in verts], [], faces)
    ob = bpy.data.objects.new("af_sticks", me)
    bpy.context.scene.collection.objects.link(ob)
    ob.parent = rig
    ob.matrix_parent_inverse.identity()
    ob.matrix_basis.identity()
    for name, idx in groups:
        vg = ob.vertex_groups.new(name=name)
        vg.add(idx, 1.0, "REPLACE")
    mod = ob.modifiers.new("Armature", "ARMATURE")
    mod.object = rig
    return ob
