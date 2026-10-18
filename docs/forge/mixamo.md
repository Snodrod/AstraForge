# Module "mixamo": clip library and retargeting to any humanoid rig

Files: `scripts/blender/steps/retarget.py`, `steps/clip_library.py`, `steps/_anim_lib.py`,
`scripts/blender/tests/test_mixamo.py`, the library `data/library/animations/` (not tracked in git).

## What it does

**`retarget`** transfers Mixamo clips (and any FBX with a humanoid skeleton) onto a skinned rig. Outputs:
- `animated.glb`: one glTF animation per clip, looping ones get the suffix `-loop`, the skin is kept.
  The whole rig hierarchy goes into the file: §6 sockets on bones, props on sockets, child meshes;
- `animated.blend`;
- `clips.json` with metrics;
- `preview.png`: one row per clip, 5 frames, three-quarter view, Workbench.

**`clip_library`** maintains the clip library: `scan`, `import`, `remove`, `preview`, `list`.
The `index.json` format follows contract §7 and also carries a license note.

**`_anim_lib`** is the core, ported from an earlier in-house project: its retargeter, the scene helpers
and `retime_action`. Bindings to that project's paths have been removed.

### Retargeting algorithm

1. The bones on both sides are brought to canonical short Mixamo names; the scheme is chosen by vote.
   Supported:
   - `mixamorig:`, `mixamorig1:` and names without a prefix;
   - the AstraForge template: `Root, Hips, Spine, Chest→Spine2, Neck, Head, HeadEnd→HeadTop_End,
     *UpperArm→*Arm, *LowerArm→*ForeArm, *UpperLeg→*UpLeg, *LowerLeg→*Leg, *Toes→*ToeBase,
     *ToesEnd→*Toe_End`; `Root` and `*HandEnd` are not mapped;
   - Rigify `DEF-`/`ORG-`/metarig (`spine`=Hips … `spine.006`=Head, `upper_arm.L` …);
   - UE: `pelvis, spine_01..03` (UE5 uses `01/03/05`), `clavicle_l, upperarm_l, lowerarm_l, hand_l,
     thigh_l, calf_l, foot_l, ball_l`, fingers.
2. The rest pose is corrected per bone. The shortest-arc swing aligns the target bone with the source
   bone at rest, and the target's own roll is preserved. Then
   `target_world = C · source_delta · C⁻¹ · virtual_rest`.
3. The root scale `k` is the hip-joint height of the target divided by that of the source. Leg IK is
   analytic, two-bone, and the knee stays in the plane of the FK knee.
4. Root-motion handling (below), the loop seam and speed are applied to the world trajectories before
   IK, so the feet stay consistent with the processed pelvis.

### Root motion (`root_motion`)

| mode | locomotion (walk/run) | other clips (idle, attack, hit, cast, death, …) |
|---|---|---|
| `lock` (default) | in place: the straight-line "first → last frame" travel is removed, and the game moves the character at `ground_speed_mps` | the pelvis travel **stays** on Hips: feet that stand in the source stand on the target too |
| `keep` | travel on Hips | travel on Hips |
| `extract` | travel is moved onto a non-deforming `root` bone (Godot root motion) | same |

- An explicit `in_place` on a clip takes precedence over the mode.
  - `in_place: true` removes only the straight start→end travel; the pelvis sway and jerks around it
    remain. On a clip that returns to its starting point (idle, hit) this changes nothing.
  - `in_place: "strict"` removes all horizontal pelvis travel, so the pelvis XY is constant. This is the
    behavior of the earlier implementation. The feet slide by the amount of the sway, and the
    `foot_skate` check reports it.
- Locomotion is determined by category (`walk`, `run`). It can be overridden by the `locomotion` flag
  on the clip or on the library entry, or by the `locomotion` tag.
- Standing loops (idle) in `keep`/`extract` also close the world travel at the seam: the pelvis and
  feet loop without a jump.
- A clip in `lock` with travel over 0.25 m (a lunge, a death) keeps its travel on Hips. The step warns:
  without `extract`, Godot returns the character to its place with a jump at the end of the clip.
- `extract` takes an existing bone above Hips (`Root` in the template, `root` in UE) or adds `root` at
  the origin.

How this differs from the earlier implementation. Each change was found on a preview or in measurements:

| Earlier | Now | Why |
|---|---|---|
| Bone direction = its tail | direction toward the head of the child bone along the chain; for leaf bones (a hand without fingers, a toe) the parent's swing | FBX leaf-bone tails are heuristic: on a Mixamo T-pose the hand "looks" 27° to the side, and wrists broke |
| `C` from the Neck−Pelvis axis | `C` = only a rotation about the vertical | with a slouched A rest and a straight T rest of one skeleton, `C` picked up a tilt and the whole clip leaned over |
| `k` from the head height of Hips | `k` from the hip joints (heads of UpLeg) | in the AstraForge template the joints are 5 cm below Hips: the legs did not reach |
| IK on the ankle only | on contact the ball (ToeBase) is pinned, otherwise the ankle | with a different foot length the ball slid during heel lift |
| — | a zero-length source bone (on one archer rig `Neck` coincides with `Head`) is not copied, the target bone follows its parent | its rotation moves nothing on the source, but on a 12 cm neck of another character it folded the head into the chest |
| — | a neck and head without a usable reference keep the world orientation of their rest | in any rest pose the head is upright and faces forward |
| `lock_root_xy` removes all pelvis XY on every clip | `lock`: only locomotion is made in place, and only the straight travel is removed | standing clips slid (a hit reaction: 24 cm, see below); for walking, the removed sway gave 3–10 times the source's sliding |

**Next in the chain: `motion_style`** (2026-10-11, `docs/forge/motion_style.md`): each character's own
manner on top of the retargeted clips (tempo, stride, arms, lean, slouch, sway, limp, looseness,
stiffness; feet stay planted, loops have no seam) and procedural deaths (8 variants instead of the 5
library ones). The output of `retarget` (`animated.blend` + `clips.json`) is its input.

## `retarget` parameters

```json
{"step": "retarget",
 "inputs": {"mesh": "abs/rigged.glb|.fbx"}  /* or {"blend": "abs/file.blend"} */,
 "params": {
   "clips": [{"source": "library:mixamo-walk | abs/path.fbx | mixamo-walk",
              "name": "walk", "trim": [1, 60], "loop": true, "in_place": true,
              "locomotion": true, "fps": 30, "speed": 1.0, "mirror": false}],
   "library": "…/data/library/animations",
   "root_motion": "lock|keep|extract",
   "leg_ik": true, "foot_pin": "ball|ankle", "loop_blend": 8,
   "facing": "keep|rest", "hips_drop": 1.0, "hips_height_m": null, "floor": true,
   "hand_floor": {"l": 0.03}, "ground": "feet|mesh", "floor_tolerance_m": 0.02,
   "apply_transforms": false,
   "preview": true, "preview_cols": 5, "preview_tile": 256,
   "keep_existing_actions": false},
 "out_dir": "abs/out"}
```

- `loop` defaults from the category (idle/walk/run → loop). The glTF animation name is `name` or
  `name-loop`. Without `name`, the library id without the source prefix (`mixamo-`, `fbx-`) is used.
- Loop seam: the difference between the last and first frame is spread smoothly over the last
  `loop_blend` frames through a slerp of the delta. The first and last frame coincide.
- There is one `fps` for the whole GLB. With different `fps` in one request, the fps of the first clip
  is used and a warning is issued. `speed` and `trim` resample the source at fractional frames.
- `apply_transforms: true` bakes the rotation and scale of the rig object and its meshes into the data.
  Sockets and props keep their world position, and a socket's scale becomes 1 (a single
  `transform_apply` is not enough: a socket on a bone drifted by 13 cm). Without this flag, a non-zero
  rotation or scale of the rig gives the warning `rig_transform_identity` (contract §4).
- `keep_existing_actions: false` deletes only this rig's actions: the assigned ones, those in its NLA,
  and orphan actions with its bones. Actions of other objects (props, a second skeleton, shape keys)
  stay in the `.blend` and do not go into the GLB.

### Characters in robes and with a staff (2026-10-10, evening)

The first robed test character had three problems. The source idle (a bow stance) turned the torso by
62°. The knees bent and the pelvis dropped by 9–12 cm, and the hem, weighted to the pelvis and thighs,
went under the floor. The hand holding the staff dropped below its rest pose, and the butt of the staff
went 26–29 cm into the floor. In the death clips the robed body fell through the floor by 0.4–0.6 m,
and this happened with all five library deaths. The clip source is not to blame: those clips contain
the same motions, but a robe has a thickness that the source does not.
New parameters (global and per clip):

| parameter | default | what it does |
|---|---|---|
| `facing` | `keep` | `rest`: removes the mean rotation about the vertical at `facing_bone` (Hips). The rotation is performed about the vertical through the target's rest pelvis, and the foot trajectories and travel rotate with it |
| `hips_drop` | 1.0 | the fraction of the pelvis lowering below rest that is kept. 0 means the pelvis is never below rest, the legs are straightened by IK, and the hem stays on the floor. A death needs 1 |
| `hips_height_m` | — | explicit hip-joint height of the target for the root scale `k`. Under a robe the measured height is a guess |
| `floor` | true | the ankle and ball IK targets are not lower than their rest height on the target |
| `hand_floor` | — | `{"l"|"r": dz}`: the two-bone arm IK keeps the wrist no lower than rest − dz. The hand keeps its world orientation, so the staff stays vertical and does not go into the floor |
| `ground` | `feet` | `mesh`: a frame whose lowest body point is below z = 0 lifts the whole body. The lowest point is computed by linear skinning of a sample of `ground_samples` (2000) vertices, the lift is smoothed over ±2 frames, tolerance `ground_tolerance_m` (0). A lying robe settles on the floor |

`clips.json` records for each clip `facing` (the removed angle), `hips_drop` (the lowest source
position and what was kept), `floor_clamped_frames`, `hand_floor_frames`, `ground` (the lifted frames
and the maximum lift) and **`floor`**: the lowest point of the skinned body and of the props under the
rig over `floor_samples` (16) frames. From them the warnings `floor_body_<clip>` /
`floor_props_<clip>` are raised with the tolerance `floor_tolerance_m` of 2 cm. The test
`test_facing_hips_drop_floor_hand_floor_and_ground` checks, on synthetic data: a clip rotated by 40°, a
12 cm squat, "lying under the floor" and a lowered hand.

### Checks (`checks`) and `clips.json` metrics

**Errors** (they fail the step):
- NaN;
- the played-back action matches the computation (< 1 mm);
- loop seam ≤ 0.5° and ≤ 1 mm;
- `in_place`: the pelvis at the end is where it was at the start (≤ 1 mm); for `strict`, pelvis drift
  ≤ 1 mm on all frames;
- names and number of animations in the GLB;
- NaN in the GLB;
- skin;
- `gltf_sockets`: every input object with `game_socket` is a GLB node with `extras.game_socket`.

**Warnings:**
- `foot_skate` greater than `max(2 cm, 1.3 × source)`;
- the hand twist differs from the source by more than 25°;
- the rig transform is not identity.

**How foot sliding (`foot_skate_cm`) is computed.** The contact frames are determined by the
**source**. These are frames where the source ball (otherwise ankle) lies within 2 cm of its minimum
and moves slower than 0.3 m/s in its own "ground" frame. With "In Place" walking, the ground under the
feet moves backward at treadmill speed. On those frames the XY path of the same point of the
**target** foot is summed, with no speed cutoff, in the frame in which the GLB actually plays. For
locomotion in place this is the world in which the game moves the character at a constant
`ground_speed_mps`; for other clips it is the export itself, and for `extract`, with root motion
applied.

`clips.json` fields:
- `foot_skate_cm` / `foot_skate_src_cm`: sliding of the target and of the source;
- `foot_plant_drift_cm` / `..._src_cm`: the largest drift of a planted foot from its plant point;
- `foot_slide_cm`: the previous heuristic, kept for comparison;
- `ground_speed_mps` and `ground_velocity_mps`;
- `hips_drift_m` (pelvis XY range), `hips_end_offset_m`, `hips_net_travel_m`, `root_travel_m`;
- `seam_rot_deg/seam_pos_mm`, `k_hip`, `arm_dir_err_deg`, `hand_twist_deg`, `ik_unreached_frames`,
  `ball_pinned_frames`;
- `degenerate_source_bones`, `mapped_bones`, `unmapped_bones`.

At the top level of `clips.json`: `exported_objects`, `sockets`, `rig_transform`.

## `clip_library` parameters

```json
{"step": "clip_library", "params": {"action": "import",
  "folder": "<folder with downloaded Mixamo FBX>", "pattern": "**/*.fbx",
  "files": ["abs.fbx", {"path": "abs.fbx", "name": "Sword idle", "category": "idle", "tags": ["sword"],
                        "source": "mixamo"}],
  "ids": ["mixamo-…"], "library": "…/data/library/animations"}, "out_dir": "abs/out"}
```

- **The source, id and folder are set by one value, `source`.** It is `files[].source` if given.
  Otherwise `mixamo` for a skeleton with Mixamo names (`mixamorig:` or no prefix) and `fbx` for the
  rest (UE, Rigify, the template). The entry gets `source`, the id `<source>-<slug>`, the file
  `<source>/<id>.fbx` and `<source>/<id>.poses.json`.
- `import` copies the file and writes `poses.json`: 12 moments, 22 joints, glTF axes (Y up, facing
  +Z), links for the stick figures in the UI.
- Deduplication on two levels: an identical SHA1 or an identical motion (12 samples of world joints
  within 3 mm at an equal frame count). From a group of duplicates the smaller file without skin is
  copied, and the name is taken from the "production" file (not `superseded/_prev`, not
  `candidates`). The other paths go into `also_found`, and their names into `aliases`.
- The category is determined from the file and folder name (`idle, walk, run, attack, cast, hit,
  death, jump, emote, other`). Tags: `sword, shield, bow, spear, magic, mounted, torch, unarmed`.
- Entry fields:
  - contract fields: `id, name, category, source, file, skeleton, frames, fps, root_motion, tags,
    imported_from, has_skin, sha1`;
  - additional: `bones, duration, frame_range, hips_travel_m, rest_pose T/A, size, poses, aliases,
    also_found, imported`.
- License (`index.json → licence`). Mixamo clips may be used in your own projects, including
  commercial ones. The raw FBX files may not be redistributed or sold as standalone assets, so the
  library stays local. Only the user downloads from mixamo.com; the forge does not go online.
- The step deliberately writes to `params.library`, i.e. outside `out_dir`: the library is shared
  state.

## How to run

```
"C:/Program Files/Blender Foundation/Blender 5.0/blender.exe" -b --factory-startup ^
  --python scripts/blender/af_run.py -- retarget <request.json>
"…/blender.exe" -b --factory-startup --python scripts/blender/af_run.py -- clip_library <request.json>
"…/blender.exe" -b --factory-startup --python scripts/blender/tests/run_tests.py -- mixamo
```

There are 18 tests, about 24 s (2026-10-10, evening: + `test_facing_hips_drop_floor_hand_floor_and_ground`); all pass, both in the shared run and in the one-liner. The synthetic
tests need no assets. They check:
- name schemes;
- categories;
- `retime_action`;
- retargeting onto a rig with arms down and a different roll;
- `extract`, `mirror`, `speed/fps/trim`;
- a zero-length bone;
- the full library cycle, including a UE clip with `source: fbx`;
- **a standing clip with feet planted under default parameters**: the feet stay planted in the
  re-imported GLB as well, and `strict` is caught by the check;
- sockets, props and child meshes in the GLB;
- foreign actions in the `.blend` and outside the GLB;
- the rig scale warning and `apply_transforms`.

The `test_real_*` tests use external fixture assets (Mixamo-rigged characters, a rig with arms down,
the AstraForge template, individual clips, plus `index.json`) and are skipped when those are not
available.

Each fix-related test was verified by mutation: if the old behavior is restored, the test fails. Seven
mutations were checked: the former `lock`, export of skinned meshes only, no action filter, deleting
all actions, no baking, a single `transform_apply`, and the old sliding metric.

## What to download from Mixamo

For variety of gaits, deaths, weapon attacks and casts (2026-10-11), see the section "What to download
from Mixamo for variety" in `docs/forge/motion_style.md`. Below is the original list of 2026-10-10.

These are search queries on mixamo.com; the exact name on the site may differ. Download FBX, Without
Skin, 30 fps, and enable "In Place" for locomotion. Use the same character (or Y Bot) throughout.

- run: "Running", "Standard Run", "Sword And Shield Run"; sprint: "Fast Run";
- jump: "Jump", "Running Jump", "Falling Idle" + "Landing";
- strafe: "Left Strafe Walking", "Right Strafe Walking", "Left/Right Strafe" (run),
  "Sword And Shield Strafe"; step back: "Walking Backwards";
- turns: "Left Turn 90", "Right Turn 90" (the root turns on the pelvis, see the limitations);
- block: "Sword And Shield Block", "Standing Block Idle"; dodge: "Standing Dodge Backward",
  "Dodging Left/Right", "Sword And Shield Roll" or "Quick Roll";
- attacks: "Sword And Shield Slash", "Great Sword Slash", "Punching", "Standing Melee Kick",
  "Standing Draw Arrow";
- cast: "Standing 1H Magic Attack", "Standing 2H Magic Area Attack", "Spell Casting";
- reactions: "Hit Reaction", "Standing React Large From Front/Back";
- **a neutral death**: "Dying", "Falling Back Death", "Falling Forward Death";
- emotes: "Victory Idle", "Waving", "Taunt", "Cheering", "Quick Formal Bow";
- neutral: "Breathing Idle", "Walking".

## Measurements (Blender 5.0.1, 30 fps, 2026-10-10)

The time of the whole step for 5 clips is 2.4–4.5 s, depending on the preview size (the retarget takes
about 1 s, the rest is GLB export and preview). In all runs `ok: true`, NaN 0, loop seam 0.0°, the GLB
holds exactly the requested animations, and the durations match. The "sliding" and "foot drift"
columns show target / source × k in cm, over the source contact frames. "Pelvis range" is the largest
deviation of the pelvis in XY from the first frame.

**(a) Test character 1** (Mixamo auto-rig, A-pose, 22 bones, 17 358 tris), library clips and the
character's own walk, `lock` by default:

| clip | frames | mode | k | sliding | foot drift | pelvis range, m | IK not reached, frames |
|---|---|---|---|---|---|---|---|
| idle-loop | 77 | keep | 1.212 | 0.4 / 0.4 | 0.0 / 0.0 | 0.007 | 0 |
| walk-loop | 122 | in_place, 0.30 m/s | 0.988 | 9.6 / 9.6 | 3.5 / 3.5 | 0.293 (jerks of the walk) | 0 |
| attack | 71 | keep | 1.212 | 10.1 / 9.8 | 1.5 / 1.4 | 1.146 (lunge) | 23 |
| hit | 30 | keep | 1.212 | 2.7 / 2.6 | 1.9 / 1.8 | 0.064 | 0 |
| death | 70 | keep | 1.212 | 5.7 / 5.5 | 3.2 / 3.2 | 1.092 (ends at 1.09 m) | 0 |

This character's walk drags a foot (the ball is pinned on all frames), so even the source has 9.6 cm.

**(b) Test character 2** (Mixamo auto-rig) with clips of a bow stance (33 bones, hands animated),
`root_motion: extract`. A `root` bone was added, and the GLB has 23 joints.

| clip | frames | mode | k | sliding | foot drift | pelvis range, m |
|---|---|---|---|---|---|---|
| idle-loop | 154 | extract | 1.077 | 0.0 / 0.0 | 0.0 / 0.0 | 0.003 |
| walk-loop | 37 | in_place, 1.12 m/s | 1.068 | **3.5 / 1.8** | 2.0 / 1.2 | 0.075 |
| attack (shoot) | 151 | extract | 1.068 | 3.1 / 3.1 | 0.3 / 0.5 | 0.076 |
| hit | 39 | extract | 1.068 | 7.8 / 7.5 | 1.8 / 1.7 | 0.112 |
| death | 94 | extract | 1.068 | **3.8 / 2.2** | 1.1 / 0.9 | 1.102, on `root` 1.047 |

The `foot_skate` warnings are in bold. For the walk and death on a character with shorter legs, the
feet slide a little more than the source. Right-hand twist, source/target: 0.7/1.1°, 10.5/10.9°,
102.7/101.3°, 48.5/48.3°, 95.6/98.3°. Degenerate source bones: `Neck`, `RightHandIndex1`,
`RightHandIndex3`.

**(c) A synthetic rig with arms down**: pelvis 1.0 m against 0.86 for Mixamo, roll of all bones
−1.1 rad. Clips in T-pose: an idle from the library, the rest from test character 1. `arm_dir_err`
≤ 0.03°. An independent check (the played-back action against the re-imported FBX) keeps the arm
directions within 3°; without the rest-pose correction the arms would be off by ≈90°.

| clip | mode | sliding | foot drift |
|---|---|---|---|
| idle-loop | keep | 0.1 / 0.1 | 0.0 / 0.0 |
| walk-loop | in_place | 11.5 / 11.5 | 4.3 / 4.3 |
| attack | keep | 1.7 / 1.7 | 0.4 / 0.4 |
| hit | keep | 1.1 / 1.1 | 0.3 / 0.3 |
| death | keep | 7.7 / 7.7 | 2.6 / 2.7 |

**(d) The AstraForge template** (names from `src/rig-core.js`, model width = height, as for a T-pose;
input through `.blend`, `extract` onto its own `Root`). 24 of 27 bones were mapped; `Root`,
`LeftHandEnd` and `RightHandEnd` are not mapped, as intended.

| clip | mode | sliding | foot drift | IK not reached |
|---|---|---|---|---|
| idle-loop | extract | 0.4 / 0.3 | 0.0 / 0.0 | 0 |
| walk-loop | in_place | 2.6 / 2.6 | 1.8 / 1.8 | 2 |
| attack | extract | 9.7 / 10.0 | 1.8 / 1.9 | 0 |
| hit | extract | 2.5 / 2.6 | 1.6 / 1.7 | 0 |
| death | extract | 7.2 / 7.2 | 2.5 / 2.5 | 0 |

**(e) A regression from a review note**: standing clips on test character 1. The former `lock`
behavior equals the current `in_place: "strict"`.

| clip | default: sliding / foot drift | strict (old `lock`) | source |
|---|---|---|---|
| a hit reaction (source feet motionless) | 0.1 / 0.0 | 48.2 / **23.7** | 0.1 / 0.0 |
| an idle with a look-around | 0.1 / 0.0 | 21.5 / 7.5 | 0.1 / 0.0 |
| a low sword-and-shield attack (travel 2.26 m) | 5.6 / 1.7 | 118.3 / 65.5 | 5.8 / 2.2 |

In the re-imported GLB the ankles of the hit reaction move by no more than 0.5 mm; with strict, by
23.7 cm. The pelvis range of the hit (0.24 m) is now preserved.

**(f) Walking in place: straight travel against strict** (on test character 1, sliding target /
source, cm): its own walk 9.6 against 57.7 (source 9.6), a second walk 2.7 against 8.3 (2.7), a third
3.0 against 10.8 (2.0), a fourth 2.1 against 21.1 (2.1).

**(g) The chain of the preset "Humanoid from a model"**: `rig_humanoid` (the current version of the
humanoid module) on a test `model.glb` (9.7 s), then `retarget` with library clips idle/walk/attack/
hit/death and default parameters, then `godot_package` (`validate: false`, Godot was not run).
- `animated.glb`: 5 animations, 25 joints, 17 031 tris and **7 §6 sockets at the time of measurement**
  (`socket_mount_hand_l/r`, `socket_mount_back`, `socket_mount_hip_l/r`, `socket_fx_head`,
  `socket_fx_chest`; since 2026-10-10 the rig adds `socket_mount_shield_l/r`, 9 in total) with
  `extras.game_socket`.
- Sliding target / source, cm: idle 0.4/0.4, walk 2.7/2.7, attack 9.8/9.9, hit 2.7/2.7, death
  5.6/5.6. Foot drift on idle 0.0/0.0.
- `godot_package`: sockets 7, clips 5. Before the fix, sockets were lost in this chain.

The preview rows follow the order of `clips`. The red cross is the origin, the slab is the floor at
z = 0. On the foot-trace plot, red is the left ankle, blue is the right, and the circle is the first
frame.

In the template (d) the shoulder bones start to the side of the chest: `createSkeleton` has no
clavicle from the spine. The preview stick figures draw only bones, so the arms look "detached". This
is the template layout, not the retargeting.

## Honest limitations

- In 22-bone Mixamo clips the hands are not animated: the wrist rigidly follows the forearm. Fingers
  are transferred only if both skeletons have them.
- Palm orientation is not derived from geometry. The swing preserves the target's roll, so if the
  target's palms at rest are turned differently from the Mixamo T-pose (palm down), the forearm twist
  will diverge.
- IK is for the legs; for the arms there is only the `hand_floor` constraint (the wrist no lower than
  rest − dz). There are no two-handed grips or contacts with objects. The hand sockets (§6) are
  exported and follow the hand, but are not fitted to a grip. With a different arm length, a
  two-handed grip and a bow will not meet.
- **Travel of standing clips.** In `lock`, lunging attacks and deaths move away from their place: one
  low sword-and-shield attack by 2.26 m, deaths by 1.0–1.3 m. The feet stand, but without `extract`
  Godot returns the character with a jump at the end of the clip; the step warns about this. Such
  clips need `root_motion: extract`.
- **Sliding when travel is removed.**
  - `in_place: "strict"` on any clip and `in_place: true` on a standing clip with travel slide
    honestly (table e). The `foot_skate` check shows this, but the step is not failed: it is a
    warning.
  - Walking in place keeps the pelvis sway (a 0.29 m jerk on one test walk). The character must be
    moved uniformly at `ground_speed_mps`.
- On a character with shorter legs, a bow-stance walk and death slide 1.6–1.7 cm more than the source
  (3.5 / 1.8 and 3.8 / 2.2 cm). On a lunge, IK does not reach on 23 frames because the target's legs
  are shorter.
- Root extraction is horizontal only (XY). Root rotation (yaw) is not extracted, and turning clips
  rotate the pelvis.
- The loop seam honestly closes only nearly looping clips. If a non-looping clip (an attack) is made
  to loop, the tail gets a noticeable "rubber band".
- `mirror` was verified numerically on synthetic data and not reviewed visually. It assumes a
  symmetric source rest pose.
- The sliding metric relies on source contacts: height up to 2 cm above the minimum, speed up to
  0.3 m/s. Slow dragging (a dragged foot) counts as contact for both source and target. Compare with
  `foot_skate_src_cm`.
- Prop animations inside the rig hierarchy do not go into the GLB: they are disabled for the duration
  of the export, and the step warns. Foreign actions are excluded through an internal filter of the
  Blender 5.0 glTF exporter (`gltf_action_filter`). If the exporter changes, the step warns, and a
  leak will be caught by the `gltf_animations` check.
- Categories and tags are guessed from names. Tags derived from a folder name (archer → bow) may not
  suit neutral motions. Duplicates with different trimming (a different frame count) are not
  recognized.
- Rigify and UE were verified on synthetic rigs (names, arm directions), not on real ones. In a
  generated Rigify rig the DEF bones hang on constraints: a deform-only game rig is needed (the step
  warns about constraints).
- On one test character (Meshy and Mixamo auto-weights) the helmet and neck mesh visibly crumple when
  the head turns by 60°. That is an asset weighting issue, not retargeting.
- The preview is a gray Workbench without row labels.
