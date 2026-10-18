"""anim_creature — procedural, foot-planted clips for a creature rigged by rig_creature.

inputs.blend  rigged.blend from rig_creature (af_run opens it before run())
params
  clips         list (default: every clip that fits the template) of
                idle-loop, walk-loop, run-loop, fly-loop, glide-loop (winged only), attack, hit, death
  fps           30 (default)
  in_place      true (default). false = root bone travels forward (root motion), feet stay world-locked
  cycle_frames  int (walk-loop) or {"clip": frames}
  stride_m      float (walk-loop) or {"clip": metres travelled per cycle}
  speed         float (walk-loop) or {"clip": m/s}; with stride -> cycle length, alone -> cycle length
                (metres per second, NOT a multiplier)
  tempo         multiplier on every clip's default length (2 = twice as fast); cycle_frames/speed win
  run_gait      'trot' (default) | 'gallop'   (quadrupeds)
  bird_gait     'walk' (default) | 'hop'
  attack        'auto' (default per template) | 'bite' | 'claw' | 'lunge' | 'strike'
  death_side    'r' (default) | 'l' — side the creature falls on
  hover_m       flight height of the body for fly/glide (default 0.3 x height)
  pose_view     view for poses.png ('left', 'three_quarter', 'top', ...), default per template
  tile          pose tile size in px (default 240)
outputs: animated.glb (one glTF animation per clip, '-loop' suffix on loops), animated.blend, clips.json,
         poses.png (rows = clips in clips.json order, columns = key frames)

Gaits: quadruped walk = lateral sequence LH,LF,RH,RF at phases 0/.25/.5/.75, run = trot (or gallop);
hexapod = alternating tripod; octopod = back-to-front wave (walk) / alternating tetrapod (run);
bird = alternating walk or two-footed hop; serpent = travelling sine wave along the spine/tail chain.
Feet are solved with analytic IK every frame and baked into plain FK rotation keys (no constraints, no
control bones in the export). Swing curves leave and meet the ground at ground speed.
Serpents with in_place:false move the root one wavelength per cycle (slip-free undulation).
Folded-wing rigs (wings fitted along the flank) get no fly-loop/glide-loop and damped wing motion.

QA (clips.json, measured on the deformed mesh) uses limits scaled to the creature (qa_limits): foot slide
<= max(0.5 cm, 1.5 % leg height), penetration <= max(0.3 cm, 0.5 % height), loop seam <= min(0.05 cm,
0.05 % height). The last key of a loop is fn(1.0) evaluated on its own, so the seam error and the seam
acceleration (<= 1.5 x the largest interior frame acceleration) are real measurements.
"""
import json
import os

import bpy
import numpy as np

import af_common as af
from steps import _creature_lib as L

ALL_CLIPS = ["idle-loop", "walk-loop", "run-loop", "fly-loop", "glide-loop", "attack", "hit", "death"]


def qa_limits(rig):
    """QA limits scaled to the creature (cm). A fixed 2 cm slide would be 2.6 % of a 0.3 m rat's body
    but nothing for a dragon, so: foot slide <= max(0.5 cm, 1.5 % of the leg height), ground penetration
    <= max(0.3 cm, 0.5 % of the height), loop seam <= min(0.05 cm, max(0.001 cm, 0.05 % of the height)),
    seam acceleration <= 1.5 x the largest interior frame acceleration + max(0.005 cm, 0.01 % height)."""
    H_cm = rig.H * 100.0
    Lz_cm = (rig.Lz if rig.legs else 0.3 * rig.H) * 100.0
    return {"foot_slide_cm": round(max(0.5, 0.015 * Lz_cm), 3),
            "ground_penetration_cm": round(max(0.3, 0.005 * H_cm), 3),
            "loop_seam_error_cm": round(min(0.05, max(0.001, 0.0005 * H_cm)), 4),
            "seam_accel_ratio": 1.5,
            "seam_accel_floor_cm": round(max(0.005, 0.0001 * H_cm), 4),
            "basis": {"leg_height_m": round(Lz_cm / 100.0, 4), "height_m": round(rig.H, 4)}}


def _find(kind):
    for o in bpy.data.objects:
        if kind == "arm" and o.type == "ARMATURE" and o.get("af_rig"):
            return o
        if kind == "mesh" and o.type == "MESH" and o.get("af_creature_mesh"):
            return o
    return None


def run(req):
    p = req["params"]
    arm, ob = _find("arm"), _find("mesh")
    if arm is None or ob is None:
        raise ValueError("inputs.blend has no rig_creature armature/mesh (af_rig / af_creature_mesh)")
    scene = bpy.context.scene
    fps = int(p.get("fps", 30))
    scene.render.fps = fps
    rig = L.Rig(arm, ob)
    cb = L.ClipBuilder(rig, p)
    lim = qa_limits(rig)
    SLIDE, PEN, SEAM = lim["foot_slide_cm"], lim["ground_penetration_cm"], lim["loop_seam_error_cm"]
    wanted = p.get("clips") or ALL_CLIPS
    warnings = []
    clips = []
    for name in wanted:
        if name not in ALL_CLIPS:
            raise ValueError("unknown clip %r (known: %s)" % (name, ", ".join(ALL_CLIPS)))
        c = cb.build(name)
        if c is None:
            if name in cb.skipped:
                warnings.append("clip %s skipped: %s" % (name, cb.skipped[name]))
            elif name in (p.get("clips") or []):
                warnings.append("clip %s does not apply to template %s" % (name, rig.template))
            continue
        clips.append(c)
    # rest mesh for QA
    assign = L.assign_action
    assign(arm, None)
    for pb in arm.pose.bones:
        pb.rotation_quaternion = (1, 0, 0, 0)
        pb.location = (0, 0, 0)
    scene.frame_set(0)
    V0 = L.eval_verts(ob)
    _, _, E = L.mesh_np(ob)
    contacts = L.contact_sets(rig, V0)
    acts, report = [], []
    lo, hi = V0.min(0), V0.max(0)
    checks = []
    for c in clips:
        act, info = L.bake_clip(rig, c)
        if c.ground:
            dmax, dmin = L.ground_fix(rig, act, c)
            info["ground_fix_cm"] = [round(dmin * 100, 2), round(dmax * 100, 2)]
        q = L.qa_clip(rig, act, c, V0, E, contacts)
        bb = q.pop("_bbox")
        if c.loop or c.name in ("attack", "hit") or c.name == "death":
            lo, hi = np.minimum(lo, bb[0]), np.maximum(hi, bb[1])
        entry = {"name": c.name, "loop": c.loop, "frames": c.frames + (0 if c.loop else 1),
                 "keys": c.frames + 1, "fps": fps, "duration_s": round(c.frames / fps, 4)}
        entry.update(info)
        entry.update(q)
        report.append(entry)
        acts.append(act)
        af.log("clip baked", clip=c.name, slide=q.get("foot_slide_cm"), pen=q["ground_penetration_cm"],
               seam=q.get("loop_seam_error_cm"))
        if q.get("foot_slide_cm") is not None:
            checks.append(af.check("foot_slide_cm:" + c.name, q["foot_slide_cm"] <= SLIDE,
                                   q["foot_slide_cm"], SLIDE))
        checks.append(af.check("ground_penetration_cm:" + c.name, q["ground_penetration_cm"] <= PEN,
                               q["ground_penetration_cm"], PEN))
        if c.loop:
            checks.append(af.check("loop_seam_cm:" + c.name, q["loop_seam_error_cm"] <= SEAM,
                                   q["loop_seam_error_cm"], SEAM))
            acc_lim = round(lim["seam_accel_ratio"] * q["interior_accel_max_cm"] + lim["seam_accel_floor_cm"], 4)
            entry["seam_accel_limit_cm"] = acc_lim
            checks.append(af.check("loop_seam_accel_cm:" + c.name, q["seam_accel_cm"] <= acc_lim,
                                   q["seam_accel_cm"], acc_lim,
                                   note="velocity pop at the loop point vs the largest interior frame"))
        checks.append(af.check("edge_stretch:" + c.name, q["edge_stretch_p999"][1] <= 1.6,
                               q["edge_stretch_p999"], [0.6, 1.6], severity="warning"))
        if info.get("crouch_cm", 0) > 5.0 * rig.H:          # > 5 % of the height (H in metres -> cm)
            warnings.append("%s: body lowered by %.1f cm to keep the feet reachable (straight rest legs)"
                            % (c.name, info["crouch_cm"]))
        if info.get("ik_reach_error_cm", 0) > 1.0:
            warnings.append("%s: IK could not reach a foot target by %.1f cm" % (c.name, info["ik_reach_error_cm"]))
    L.stash_actions(arm, acts)
    scene.frame_start, scene.frame_end = 0, max([c.frames for c in clips] + [1])
    outputs = {}
    view = p.get("pose_view") or {"hexapod": "three_quarter", "octopod": "three_quarter",
                                  "serpent": "top"}.get(rig.template, "left")
    if p.get("poses", True) and clips:
        outputs["poses"] = L.render_pose_sheet(af.out_path(req, "poses.png"), rig, list(zip(acts, clips)),
                                               (lo, hi), view=view, tile=int(p.get("tile", 240)))
    # every glTF animation is one clip; loops carry the '-loop' suffix (Godot import convention)
    L.assign_action(arm, None)
    outputs["blend"] = af.save_blend(af.out_path(req, "animated.blend"))
    sock = [o for o in bpy.data.objects if o.get("game_socket")]
    outputs["glb"] = af.export_glb(af.out_path(req, "animated.glb"), objects=[arm, ob] + sock, animations=True)
    names = L.glb_animation_names(outputs["glb"])
    checks.append(af.check("glb_animations", sorted(names) == sorted(c.name for c in clips), names,
                           [c.name for c in clips]))
    cj = {"template": rig.template, "fps": fps, "in_place": bool(p.get("in_place", True)),
          "poses_png": {"rows": [c.name for c in clips], "columns": "key frames: loops 0,1/4,1/2,3/4; "
                        "one-shots 0,35%,50%,70%,100% (death 0,30%,55%,80%,100%)", "view": view},
          "limits": lim,
          "clips": report, "glb_animations": names}
    path = af.out_path(req, "clips.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(cj, fh, ensure_ascii=False, indent=1)
    outputs["clips_json"] = path
    stats = {"template": rig.template, "clips": [c.name for c in clips], "bones": len(arm.data.bones),
             "glb_animations": len(names),
             "max_foot_slide_cm": max([r.get("foot_slide_cm") or 0 for r in report] + [0]),
             "max_penetration_cm": max([r["ground_penetration_cm"] for r in report] + [0])}
    return {"outputs": outputs, "stats": stats, "checks": checks, "warnings": warnings}
