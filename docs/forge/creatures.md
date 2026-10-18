# The creatures module: auto-rigging and procedural animation for creatures

Two Blender steps (contract `docs/FORGE-CONTRACT.md`, §3–§7):

| Step | What it does |
|---|---|
| `rig_creature` | creature mesh -> turn the nose to −Y, scale, template skeleton (names §5), weights (bone heat or fallback), sockets §6, `rigged.glb/.blend`, `rig.json`, `preview.png` |
| `anim_creature` | `rigged.blend` -> procedural clips with IK foot placement baked into plain FK keys; `animated.glb/.blend`, `clips.json` (metrics of each clip), `poses.png` |

Code: `scripts/blender/steps/{rig_creature,anim_creature,_creature_lib}.py`, tests: `scripts/blender/tests/test_creatures.py`.

## How rig_creature works

1. **Input and cleanup.** `inputs.mesh` (glb/gltf/fbx/obj/stl/blend) or `inputs.blend` (as in the cleanup -> rig_creature
   pipeline; if both are given, mesh wins). glTF hierarchies (parent empties with scale and quaternion rotation), rigs,
   skins, sockets, shape keys and foreign objects are stripped: only the meshes remain, in rest pose, in world
   position. This is why the step also accepts its own `rigged.glb` (the rig is rebuilt from scratch). The meshes are
   joined, transforms applied, UV seams stitched (`remove_doubles`, 1e‑5 of the bounding size): Tripo models arrive
   as 170–547 "islands" only because glTF cuts them along seams; after stitching, each of the three Tripo test
   models (two horse-like quadrupeds and a winged quadruped) is a single island.
2. **Facing.** The body axis is the principal axis of the **thick** voxels (thin wings/tail/legs are discarded). The
   sign: for ≤ 4 supports, "mass on top" and "height of the thick mass at the ends"; for ≥ 6 supports, the
   distribution of feet along the axis. `params.head_dir` / `params.facing` override the auto detection: the label
   `auto` (default, which is what the server sends), `-Y`, `+Y`, `-X`, `+X` or a vector `[x, y]`. On all 9 test
   meshes the direction was found correctly with confidence 1.0 (quadrupeds +Y, +Y, winged quadruped +X, synthetic
   meshes +X/−Y/+Y/−X, a dog built from parts +X, a `rigged.glb` produced by the step itself −Y).
3. **Scale**: `height_m` or `length_m` (along the body axis); without them the size is kept (with a warning). The
   bottom is at z = 0, the X/Y center at zero, the creature looks toward −Y, its left side is +X.
4. **Shape analysis** (`analyse`):
   - solid voxelization (112 voxels along the long side) and an interior distance transform (DT);
   - **supports** are clusters of vertices near the ground; clusters of one foot are merged: (a) when the gap is
     < 0.045 of the bounding size and (b) **new**: when they belong to one connected component of the lowest voxel
     layer (z < 6 % of the height), lie on the same side, are close along the body (< 0.3 of the bounding size) and
     together give a footprint the size of a foot. This way the front toes and the rear toe of a bird, which touch the
     ground separately and are joined only through the metatarsal joint, produce one foot. Then left/right pairs
     are formed; clusters on the midline (tail, belly) are not counted as supports;
   - **torso** is a morphological opening with a radius slightly larger than the limb thickness; **medial paths**
     (Dijkstra over voxels): foot -> torso (leg), wing tip -> torso (wing), rearmost point -> frontmost point (axis);
   - **classification** (`template: auto`): 0 support pairs - serpent, 1 - bird, 2 - quadruped / winged_quadruped
     (two thin tall side appendages = wings), 3 - hexapod, 4 - octopod.
5. **Joints** (`build_layout`): the pelvis against the roots of the hind legs, the withers at the roots of the front
   legs (for a bird, at the wings); the head at the bend of the neck→nose axis; the jaw only when the mouth is
   clearly open (vertical rays through the mesh give 4 hits on ≥ 25 % of the head; **new**: a "gap" wider than 3
   voxels whose middle lies inside the solid volume is overlapping parts, not a mouth: on the dog built from parts
   the head + muzzle no longer produce a false jaw); the legs of mammals/arthropods/birds are as before (see the
   segment table below); wings - shoulder at the torso, elbow/wrist at 30/62 % of the way to the tip; mirrored
   averaging of pairs (`symmetrize`).
   **Folded wings (new).** If the wings of a `bird`/`winged_quadruped` do not stand out as thin parts (folded and
   fused with the flank), the `wing_<l|r>_1..3` chain is **fitted** along the upper flank: the shoulder at the
   withers slightly below the back line, the tip at the rearmost upper-lateral mass (not the tail), the
   elbow/wrist at 30/62 % inside the outer layer of the flank. This is a control chain for small motions, not wing
   anatomy (a real folded wing is a zigzag). In `rig.json` such wings have `"folded": true`; the `wings_separate`
   check is a warning, its text is in warnings. If only one wing is found, the other one is mirrored.
6. **Weights.** First bone heat (`ARMATURE_AUTO`), run **on a copy** scaled up so that the bounding size is ≥ 4.5 m
   and the median edge is ≥ 5 cm (the Blender solver works with absolute tolerances and silently fails on small
   triangles: on one test quadruped the median edge was 1.6 cm and every bone failed, at 2.5 cm and above it works).
   The weights do not depend on scale and are read back onto the original mesh. In `auto` mode heat is rejected
   if: > 2 % of vertices have no weight, a bone has no vertices, or > 1 % of vertices on a leg belong to the
   opposite side; **the reason is written to warnings** ("bone heat rejected (…): distance weights used instead")
   and to `rig.json → weights.heat.rejected`. Fallback weights: distance to the bone chain normalized by DT, region
   masks, edge smoothing, ≤ 4 influences; for meshes made of several pieces (**new**) the smoothing also goes
   through "bridges" between vertices of different islands closer than `max(2.5 voxels, median edge)` (≤ 6 per
   vertex), plus spatial smoothing. For both modes, after that: limb bones fade toward the midline, loose geometry
   near the legs hands the weight of the lower segments to the thigh/shoulder, and everything below the fetlock joint
   goes rigidly to `paw_*`.
7. **Sockets** (§6, empties with `game_socket`, `kind`, `size_class`, parented to a bone, world rotation identity):
   `socket_fx_head`, `socket_fx_mouth` (between the tips of the upper and lower jaw; **if there is a `jaw`, on the
   `jaw` bone**, otherwise on `head`), `socket_mount_back` (the bottom of the back saddle; serpents have none),
   `socket_fx_tail`.
8. Temporary render tiles (`_views/`) are removed from `out_dir`.

## Template skeletons (exact names, §5)

`root` does not deform (at the origin, looks toward −Y), all other bones deform. Legs are
`leg_<pos>_<l|r>_1..3` + `paw_<pos>_<l|r>`, wings are `wing_<l|r>_1..3`. `_l` is always on +X. `[jaw]` exists only
with an open mouth. The number of tail bones depends on the tail length (`params.tail`: auto | true | false | N).

| Template | Bones |
|---|---|
| `quadruped` | `root, pelvis, spine_01, spine_02, spine_03, neck_01, neck_02` (+`neck_03` for a long neck), `head, [jaw], tail_01..tail_N` (N = 2–6), `leg_front_l_1, leg_front_l_2, leg_front_l_3, paw_front_l`, the same for `_r`, `leg_back_l_1, leg_back_l_2, leg_back_l_3, paw_back_l`, the same for `_r` |
| `winged_quadruped` | everything as in `quadruped` + `wing_l_1, wing_l_2, wing_l_3, wing_r_1, wing_r_2, wing_r_3` |
| `bird` | `root, pelvis, spine_01, spine_02, neck_01, neck_02, neck_03, head, [jaw], tail_01, tail_02, leg_back_l_1, leg_back_l_2, leg_back_l_3, paw_back_l, leg_back_r_1, leg_back_r_2, leg_back_r_3, paw_back_r, wing_l_1, wing_l_2, wing_l_3, wing_r_1, wing_r_2, wing_r_3` (the legs of a bird are hind limbs; folded wings use the same names) |
| `hexapod` | `root, pelvis, spine_01, spine_02, neck_01, head, [jaw], tail_01..tail_N` (abdomen, N = 1–3), `leg_{front,mid,back}_{l,r}_{1,2,3}, paw_{front,mid,back}_{l,r}` |
| `octopod` | `root, pelvis, spine_01, head, [jaw], tail_01..tail_N` (abdomen, N = 1–3), `leg_{front,mid,mid2,back}_{l,r}_{1,2,3}, paw_{front,mid,mid2,back}_{l,r}` — **`mid2` and the template itself extend §5** (the contract has three positions and no `octopod`) |
| `serpent` | `root, pelvis, spine_01..spine_06` (`spine_segments`), `neck_01, neck_02, head, [jaw], tail_01..tail_06` (`tail_segments`) |
| `custom` | `root` + chains from `params.custom_chains` (`<name>_01..` or explicit `bones`) |

Hierarchy: `root → pelvis → spine_01 → … → spine_N → neck_01 → … → head → jaw`; `pelvis → tail_01 → …`;
`leg_*_1` is a child of the nearest body bone; `leg_1 → leg_2 → leg_3 → paw` are connected; `wing_*_1` is a child of
the nearest body bone. Meaning of the leg segments: mammal - humerus/femur, forearm/shin, metacarpus/metatarsus,
fetlock+hoof; arthropod - femur (up to the knee), tibia, metatarsus, tarsus; bird - thigh (inside the body), shin,
tarsometatarsus, toes. Examples: a horse-like quadruped - 27 bones, a dog built from parts - 26, a winged quadruped -
34, a spider - 39.

## How anim_creature works

- **Solver** (`Rig.solve`) repeats Blender's pose computation and places the legs analytically (two-segment IK with a
  pole taken from the rest bend); every frame is baked into rotation keys; the file contains no constraints and no
  control bones.
- **Gaits**: quadruped walk - LH, LF, RH, RF (phases 0/.25/.5/.75, 65 % stance), run - trot or `run_gait: gallop`
  (experimental); hexapod - tripods; octopod - wave (walk) / groups of four (run); bird - walk or `bird_gait: hop`;
  serpent - a traveling sine wave. With `in_place: false` the root advances one stride per cycle; **for a serpent
  (new) one wavelength per cycle** (the wave stands still in the world - crawling without lateral slipping), and the
  bone targets travel along with the root.
- **Crouch** - if straight legs cannot reach, the body is lowered until the ratio is ≤ 0.985; > 5 % of height
  produces a warning.
- **Ground protection**: tail/neck/head do not go below the rest clearance; a swinging foot is not below the rest
  level; fly/glide - clearance 3 % of height; death - the corpse lies on the ground; **serpent locomotion (new)** -
  the root is raised by a constant amount equal to the maximum penetration over the cycle (0.57 cm for the test
  serpent instead of 0.55–0.71 cm underground).
- **Clips**: `idle-loop`, `walk-loop`, `run-loop`, `fly-loop`, `glide-loop` (winged), `attack` (bite / claw / lunge /
  strike), `hit`, `death`. For a rig with folded wings fly/glide are **not built** (warning "clip fly-loop skipped:
  …"), wing motion in the other clips is reduced to 30 %, and in death the wings are not folded again.
- **The loop seam is now honest.** The last key is `fn(1.0)`, computed separately (previously frame 0 was substituted
  and `loop_seam_error_cm` was 0 by construction). In addition there is an acceleration check at the seam: the
  second difference of vertex positions at frame 0 (with frame N−1 as the previous one) ≤ 1.5 × the maximum over
  interior frames + a small threshold. A defect found this way - the tail in `glide-loop` with a frequency of 0.5
  per cycle (a 10.5 cm jerk at the seam versus 0.2 cm inside) - was fixed (`2·sin(w − 0.6k)`). The test deliberately
  bakes a non-periodic loop and checks that the check catches it.
- **Metrics** (`clips.json`, measured on the deformed mesh): `foot_slide_cm` (the center of the support vertices of
  each leg over the stance interval; for a leg lifted during an attack the stance is only while the foot target is
  on the ground, not a fixed window `t < 0.1`), `ground_penetration_cm`, `loop_seam_error_cm`, `seam_accel_cm` /
  `interior_accel_max_cm` / `seam_accel_limit_cm`, `loop_velocity_jump_cm`, `edge_stretch_p999`, `crouch_cm`,
  `ik_reach_error_cm`, speed.
- **Thresholds depend on size (new)** and are written to `clips.json → limits`: slide ≤ max(0.5 cm, 1.5 % of the leg
  height), penetration ≤ max(0.3 cm, 0.5 % of height), seam ≤ min(0.05 cm, max(0.001 cm, 0.05 % of height)),
  acceleration at the seam ≤ 1.5 × interior + max(0.005 cm, 0.01 % of height). These are errors; edge stretch > 1.6 is
  a warning.

## Parameters

`rig_creature`: `inputs.mesh` | `inputs.blend`; `template` (auto), `height_m` | `length_m`, `head_dir` / `facing`
(auto | −Y | +Y | −X | +X | `[x, y]` - where the head points in the input), `tail` (auto | true | false | N),
`jaw` (auto | true | false), `weights` (auto | heat | distance), `custom_chains` (`[{name, parent, points, bones?,
side?}]`, points in the output coordinate system), `leg_ratios`, `voxel_res` (112), `symmetrize` (true),
`spine_segments`/`tail_segments` (serpent, 6/6), `preview` (true).

`anim_creature`: `inputs.blend`; `clips` (all applicable by default), `fps` (30), `in_place` (true),
`cycle_frames` / `stride_m` / `speed` (a number - for walk-loop, or `{"clip": value}`; **`speed` is in m/s, not a
multiplier**), `tempo` (a tempo multiplier for all clips, 1; `cycle_frames`/`speed` take precedence), `run_gait` (trot | gallop),
`bird_gait` (walk | hop), `attack` (auto | bite | claw | lunge | strike), `death_side` (r | l), `hover_m`,
`pose_view`, `tile` (240). The `custom` template gets only `idle-loop`.

Discrepancy with the server: `server/forge-steps.mjs` describes `speed` as a "tempo multiplier" with a default of 1,
while the step reads it as 1 m/s for walk-loop (for the horse-like quadruped the stride becomes 25 frames instead of
32, fussy for small creatures and slowed down for large ones). The integrator needs a fix: `speed` should default to
empty (m/s), or the multiplier should be passed as `tempo`.

## Running

```
"C:/Program Files/Blender Foundation/Blender 5.0/blender.exe" -b --factory-startup --python scripts/blender/af_run.py -- rig_creature req.json
   req.json: {"inputs": {"mesh": ".../quadruped/model.glb"}, "params": {"length_m": 2.4}, "out_dir": ".../rig"}
"C:/Program Files/Blender Foundation/Blender 5.0/blender.exe" -b --factory-startup --python scripts/blender/af_run.py -- anim_creature req2.json
   req2.json: {"inputs": {"blend": ".../rig/rigged.blend"}, "params": {}, "out_dir": ".../anim"}
"C:/Program Files/Blender Foundation/Blender 5.0/blender.exe" -b --factory-startup --python scripts/blender/tests/run_tests.py -- creatures
```
`AF_CREATURE_TEST_OUT=<folder>` keeps the test outputs instead of using a temporary folder (the path must be short:
Blender and Windows do not open paths longer than 260 characters).

## Results (2026-10-10, Blender 5.0.1, CPU)

Tests: **14/14 PASS in 50 s** (`run_tests.py -- creatures`): 3 unit tests (transfer curve, two-segment IK, `head_dir`
labels), 7 synthetic (an insect + `in_place:false`; a spider; a serpent with an open mouth + `in_place:false`; a bird
with spread wings + a check that a non-periodic loop is caught; a bird with folded wings; a dog made of 41 parts
under an empty with scale 0.01 and a quaternion, with the server default parameters, then its own `rigged.glb`
and `rigged.blend` through `inputs.blend`; a custom chain) and 4 on AI-generated test assets (a quadruped, the same
quadruped at its native size without `length_m`, a second quadruped with `weights: distance`, a winged quadruped).
Checked separately through `af_run.py`: the server default parameters (`head_dir: "auto"`), `inputs.blend` only, and
the step's own `rigged.glb` as input - all ok.

| asset | template (auto) | bones | weights | confidence | rig, s | animation, s |
|---|---|---|---|---|---|---|
| quadruped A (Tripo, 18.6k tris, `length_m 2.4`) | quadruped | 27 | bone_heat | 0.90 | 3.0 | 3.9 (6 clips) |
| quadruped A, native size 1.0 m, no parameters | quadruped | 27 | bone_heat (previously silently distance) | 0.90 | 1.5 (no preview) | — |
| quadruped A ×16 (448k tris, 224k vertices) | quadruped | 27 | bone_heat (decision made on a ×5.9 copy) | 0.90 | 21.0 | 5.0 (walk) |
| quadruped B (Tripo) | quadruped | 27 | bone_heat | 0.79 | 2.7 | 2.9 (4 clips) |
| quadruped B, `weights: distance` | quadruped | 27 | distance_fallback | 0.79 | 2.5 | 2.9 |
| winged quadruped (Tripo) | winged_quadruped | 34 | bone_heat | 0.74 | 2.7 | 5.1 (8 clips) |
| synth. insect (39 parts) | hexapod | 32 | distance_fallback (heat: 132 of 2378 without weight) | 0.90 | | |
| synth. spider (65 parts) | octopod | 39 | distance_fallback (heat: 160 of 2938 without weight) | 0.82 | | |
| synth. bird (47 parts) | bird | 24 | distance_fallback (heat: 352 of 3418 without weight) | 0.85 | | |
| synth. bird, wings folded | bird, 2 legs, wings `folded` | 24 | distance_fallback (heat: 588 of 3034 without weight) | 0.64 | | |
| synth. dog made of parts | quadruped | 26 | distance_fallback (heat: 2594 of 2594 without weight) | 0.90 | | |
| synth. serpent | serpent | 18 (with `jaw`) | distance_fallback (heat: `pelvis` without vertices) | 0.75 | | |

The synthetic meshes together with animation take 4–6 s per test. For meshes assembled from primitives Blender heat
fails partially or completely; `auto` takes the fallback weights and writes the reason to warnings.

Clips (all error-level checks passed; "—" means not applicable; the seam - `loop_seam_error_cm` - is now measured):

| asset | clip | slide, cm | penetration, cm | seam, cm | seam accel. / limit, cm | crouch, cm | stretch p99.9 |
|---|---|---|---|---|---|---|---|
| quadruped A | idle / walk / run (trot) | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | 0.22/0.53, 8.3/13.9, 9.0/16.2 | 2.5 / 7.6 / 6.9 | 1.25 / 1.68 / 1.67 |
| quadruped A | attack / hit / death | 0 / 0 / — | 0 / 0 / 0 | — | — | 4.1 / 3.1 / 0 | 1.55 / 1.37 / 1.44 |
| quadruped B (heat) | walk / run / death | 0 / 0 / — | 0 / 0 / 0 | 0 / 0 / — | 7.7/23.4, 9.0/23.6 | 7.1 / 6.1 / 0 | 2.85 / 3.30 / 2.39 |
| quadruped B (distance) | walk / run / death | 0 / 0 / — | 0 / 0 / 0 | 0 / 0 / — | 10.7/20.7, 11.3/23.1 | 7.1 / 6.1 / 0 | 3.80 / 4.41 / 3.43 |
| winged quadruped | walk / run / fly / glide | 0 / 0 / — / — | 0 | 0 | 9.7/22.9, 13.4/24.0, 3.4/7.2, **0.15/0.39** | 5.5 / 4.9 / 0 / 0 | 3.27 / 3.26 / 2.17 / 2.03 |
| winged quadruped | attack (claw) / hit / death | **0** / 0 / — | 0 | — | — | 7.3 / 3.2 / 0 | 2.88 / 1.32 / 1.55 |
| insect | walk / run / death | 0 / 0 / — | 0 | 0 / 0 / — | 2.2/3.8, 4.8/7.2 | 0 | 1.64 / 1.77 / 3.31 |
| spider | walk / run / attack (lunge) | 0 / 0 / **0** | 0 | 0 / 0 / — | 4.2/11.1, 8.3/13.4 | 0 | 1.45 / 1.47 / 1.98 |
| bird (wings spread) | walk / fly / glide / death | 0.03 / — / — / — | 0 | 0 | 3.7/5.6, 1.7/3.6, 0.01/0.15 | 0 | 1.39 / 2.27 / 2.26 / 1.87 |
| bird (wings folded) | walk / run / hit / death | 0.01 / 0.01 / 0.00 / — | 0 | 0 / 0 / — | 2.7/4.1, 2.7/4.2 | 0 | 1.35 / 1.50 / 1.32 / 1.72 |
| dog made of parts | walk / run | 0 / 0 | 0 | 0 / 0 | 5.3/7.9, 5.6/8.5 | 0 | 1.64 / 1.63 |
| serpent | walk / idle / attack | — | **0** / 0.13 / 0.08 | 0 / 0 / — | 0.48/1.41, 0.22/0.36 | 0 | 1.45 / 1.14 / 1.23 |

Changes against the previous report: the glide seam of the winged quadruped has no jerk (it was 10.5 cm at the seam
versus 0.2 cm inside, and the check did not see it); the claw slide of the winged quadruped went 0.85 → 0 and the
spider's lunge 1.57 → 0 - this was the measurement window (the start of the strike was counted as stance); in
lunge the horizontal return of the leg now ends at the moment of touchdown.
Thresholds for these assets: quadruped A slide 1.56 / penetration 1.05 cm, winged quadruped 0.83 / 0.99, quadruped B
2.33 / 1.30, synthetic meshes 0.5–0.73 / 0.3–0.47 cm. Default speeds: quadruped A walk 0.78 m/s, trot 1.87; winged
quadruped 0.42 / 1.00; serpent 0.83 m/s (wavelength 0.55 m per 40 frames - the speed at which it has to be moved in
a game to avoid sliding). `run_gait: gallop` on quadruped A: the body drops by 17.6 cm, the IK falls short by 5.4 cm
in the extreme phases - trot is the default.

In the rig preview: red - left bones (+X), blue - right bones, yellow - the axis, green balls - sockets.

## Limitations (honestly)

- Joints are a **shape fit**, not anatomy: a voxel medial axis + height proportions + bends. On AI meshes the
  knees/hocks can sit several centimeters off; the head of the winged quadruped was taken from proportions
  (confidence 0.6). Folded wings are also a fit along the flank, not a wing that was found.
- **Bone heat.** The Blender solver silently fails on small triangles; it is now computed on an enlarged copy, and
  the quadruped at its native size of 1 m and a 224k-vertex mesh get heat. But on meshes assembled from intersecting
  closed parts (synthetic meshes, kitbash) heat still finds no solution, partially or completely (the dog: none of
  the 2594 vertices got a weight) - then `auto` takes the fallback weights and writes the reason to warnings. The rig
  of a 224k-vertex mesh takes 21 s, mostly heat on the ×5.9 copy.
- The fallback weights on quadruped B are worse than heat (stretch 3.80 versus 2.85 on walk). Important: the
  fallback path is not verified on a **real** fragmented mesh - after seam stitching all three Tripo models are a
  single island; islands and "bridges" have been tested only on synthetic primitives.
- Deformation is linear skinning: edge stretch p99.9 up to 3.3 on the eagle-like paws and wing shoulder of the winged
  quadruped, up to 3.3 on the ribs/rags of quadruped B (4.4 on fallback weights), 3.3 on the insect in death. These
  are warnings. A caparison and a mane have no bones of their own: a leg may pass through the cloth.
- **Folded wings**: fly/glide are not built (unfolding would tear the flank), and wing motion is reduced. For flight
  a mesh with spread wings is needed. Merging feet by the lowest layer can fuse feet if something low connects them
  (a base, a caparison down to the ground) - then the template and the number of legs must be set explicitly.
- Legs that are straight at rest (horses) require a crouch of 5–8 cm in walk/trot (≈3–4 % of height); gallop - 17.6 cm.
- The gaits are procedural (sines + Hermite), not mocap: no turns, slopes or accelerations; the wings flap with a
  rigid pattern; for the winged quadruped the wings at rest are raised vertically, and in flight a ~90° turn at the
  shoulder displaces the feathers.
- The jaw exists only with a clearly open mouth (in the tests - on the synthetic serpent); mandibles/chelicerae get no
  bones.
- Classification by support pairs: a leg raised at rest, stuck-together legs or a tail on the ground off the
  midline give a wrong number of pairs - then `template`/`head_dir` must be set explicitly. The direction detector
  is heuristic (< 0.5 -> warning).
- The QA thresholds are now relative, but the lower bounds (0.5 cm slide, 0.3 cm penetration) are still large for
  creatures a few centimeters in size.
- `custom` - only `idle-loop`; `octopod` and the `mid2` position go beyond the §5 list. The server parameter `speed`
  is understood by the step as m/s (see "Parameters").
