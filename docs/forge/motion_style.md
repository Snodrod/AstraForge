# The motion_style module — a personal manner of movement and varied deaths (2026-10-11)

Goal: every character should be different — each walks in its own way, deaths differ, each is unique.
The library has 38 Mixamo clips, of them only 4 distinct walks and 5 deaths; that is too little for a
large cast. The `motion_style` step makes many clips out of one: on top of the `retarget` clips it
applies the character's manner (tempo, stride, arms, lean, hunch, sways, limp, swagger, stiffness) and
replaces the death with a procedural one (8 variants, each with its own spread from the seed).

Files: `scripts/blender/steps/motion_style.py` (the step), `steps/_style_lib.py` (the core),
`scripts/blender/tests/test_motion_style.py` (10 tests), registration in `server/forge-steps.mjs`,
registry tests in `tests/forge-steps.test.mjs` and `tests/forge-routes.test.mjs`.

## Place in the pipeline

```
cleanup → rig_humanoid → retarget → motion_style → render_sprites → godot_package
```

The input is the `animated.blend` of the `retarget` step (slot `blend`); the `clips.json` next to it
supplies categories, loops and locomotion. The outputs are the same as in `retarget`: `styled.glb` (one
glTF animation per clip, the same names, the `-loop` suffix preserved), `styled.blend`, `clips.json`,
`preview.png` (a row per clip). Everything downstream works unchanged: sprites and the Godot package take
the `.blend` and `.glb` from the chain.

## How it works

Each clip is read frame by frame (the `retarget` actions write a key on every frame of every bone) into
world bone rotations and the pelvis position. The manner is applied in local rotations (relative to the
parent) and in the pelvis position, then the legs are solved again with the analytic two-bone IK from
`_anim_lib`: the foot follows its own (with a changed stride — recomputed) trajectory, the foot
orientation is preserved, the ankle is not below the rest height. The write is done with the same
`write_action` as in `retarget`.

| knob | neutral | range | what it does |
|---|---|---|---|
| `tempo` | 1.0 | 0.8–1.25 | clip speed; a loop is resampled to a whole number of frames, the seam is closed; for a walk the ground speed grows |
| `stride` | 1.0 | 0.7–1.35 | step length: on the stance the foot = the source foot shifted at (stride − 1) × the "treadmill" speed, so all support points (heel, flat foot, ball) travel at one speed, just faster; in the swing the shift is returned; step height × √stride; if the leg cannot reach, the heel rises first (up to 5 cm), then the pelvis lowers (up to 7 cm) |
| `arm_swing` | 1.0 | 0.2–2.0 | swing of Arm/ForeArm around the clip's mean position |
| `lean` | 0 | −1…1 | spine lean: +1 = 12° forward, −1 = 12° back; the head gives back 60% of the gaze. A clip that already walks bent over (a zombie shuffle, a combat stance) gets less: after a 12° mean torso lean, the direct lean and hunch fade out towards 27° (`forward_knobs_scale` in the report) |
| `hunch` | 0 | 0…1 | rounded back, shoulders forward and down, neck forward, chin up |
| `head_bob` | 1.0 | 0.3–2.0 | vertical bob of the pelvis and head around the mean |
| `sway` | 1.0 | 0.2–2.5 | lateral sway of the pelvis around the mean |
| `limp`, `limp_side` | 0, l | 0…1 | a "sparing" gait: while the bad leg is under load, the pelvis sinks (up to 4.5 cm) and moves towards it, the torso rolls, the foot drags (the step is 65% lower). Step length and rhythm are symmetric — otherwise the support feet would slide |
| `swagger` | 0 | 0…1 | torso back, chest forward, shoulders back, elbows out, more pelvis rotation and lateral sway |
| `stiffness` | 1.0 | 0.5–1.8 | >1 damps, <1 amplifies all movements of the spine, neck, head, shoulders and arms (amplitude 1/stiffness) |

**Seed.** `seed` is a number or a string (a batch passes the concept's file name). Knobs not set
explicitly are chosen from it deterministically (`_style_lib.style_from_seed`): each within a plausible
band, with a limp for ~16% of characters and swagger for ~25%; the rest walk healthy. `build` shifts the
choice: `heavy` is slower and stiffer, `armored` slightly slower with a smaller arm swing, `robed` with a
short stride, `light` faster, `skeleton` stiff and hunched, `zombie` limps and hunches. Without a seed
the knobs are neutral. Explicit values win over the seed. The final manner and the source of each knob
(`param` / `seed` / `neutral`) are written to `clips.json`.

**Category weights** (`CATEGORY_WEIGHTS`): a walk takes everything; idle — no stride, with half of the
arm swing and the limp (weight on the healthy leg); attack and cast keep the clip's arms and legs, take
60% of the lean and hunch, tempo at 60%; hit — similar; death is not touched (it has its own timing).
`clips` sets values for an individual clip (by name or category) or `false` — do not touch; `apply` —
which categories get the manner at all.

**Items in hands** (`hold_props`). An item rigidly attached to the hand (weight ≥ 0.99, farther than
12 cm from the wrist) is found from the `rig_humanoid` weights. If at rest it points up and rises above
the hand by 0.3 m (a staff, a banner, a spear, a bow), then in idle, walk and hit it stays almost
vertical: the hand cancels the forearm swing that the 22-bone clips militia/angel/zombie without hand
keys would pass on to the item (test: the staff tilt in a walk with `arm_swing` 1.8 without correction is
noticeably larger and drops to < 25°). In all clips none of the ~100 points of the item goes under the
floor — the hand turns the item until the point lies on the floor (a staff rests on the ground, a sword
lies beside the body).

## Deaths

`death`: `library` (the library death as is) | `auto` (a variant from the seed; `crumble` only with
`undead: true`) | a variant explicitly. `death_blend` < 1 mixes the procedural death with the library
one (rotations via slerp, then the body is laid on the floor again). The starting pose is the first
frame of the library death (the stance the character dies from).

| variant | what happens |
|---|---|
| `fall_back` | recoil from a frontal hit (torso and head back, arms forward/up/down — by seed), knees buckle, the body tips over backwards, hits the ground with a bounce, the head whips back and settles on its side, arms spread on the floor |
| `fall_forward` | a hit from behind, a fall face down, arms forward or along the body, head turned to the side |
| `fall_left`, `fall_right` | a hit from the side, a fall onto the side, the upper leg bent more (like the "recovery position") |
| `kneel_collapse` | the legs give way — the knees hit the floor (free fall of the pelvis), a second of swaying on the knees, then a fall forward with a roll onto the side; the shins stay on the floor |
| `spin_fall` | a hit to the shoulder turns the body 110–200°, then a fall backwards |
| `slump_sit` | the legs fail, the character sits down (the pelvis hits the floor with a bounce) and slumps forward onto a side or falls onto the back |
| `crumble` | undead: staggering with a fading tremor ("the magic leaves the bones"), then a collapse downwards through the knees and spine, a heap on the floor |

Timing physics. Tipping over is an inverted-pendulum equation θ'' = (3g / 2L)·sin θ with an impulse
from the hit: a slow start, a fast end; the ground impact 0.8–1.0 s after the fall begins (on the knees —
1.4 s), a damped bounce of 3–4°, a head whip, settling; afterwards 0.25 s of stillness. Everything is
scattered from the seed: direction ±18°, impulse strength, timing ±10%, arm pose, head turn, which knee
is stronger.

Floor. 1) The torso, head and thighs (their vertices by dominant bone) lie exactly on the floor after the
impact: a tipped body that went under the floor is turned back around the same support point (the feet
stay in place, the body lies at a smaller angle), the remainder is a lift; before the impact the body is
only lifted if it went into the floor. 2) Wrists, elbows, ankles, knees — by two-bone IK above the floor;
then each limb segment (thigh, shin, foot, toes, shoulder, forearm, hand) is turned by its own vertices
around its own joint until the lowest one lies on the floor. 3) An item in the hand lies flat after the
impact and does not go into the floor. 4) Anything still below the floor (a pauldron, a cloak) lifts the
whole frame. The `death_on_floor_death` check (an error): the lowest vertex of the last frame is within
±2 cm of z = 0.

## Robe after death: `robe_settle` (2026-10-11, night)

Under linear skinning the robe/skirt of a lying body is a rigid cone carried by the pelvis and thighs:
after the fall it sticks out as a "bell" (pilot: a robed test character). A cheap fix without a
simulation is a **corrective shape**:

1. On the last frame of the death, every cloth vertex (the cloth mask: carried by Hips ≥ 0.25 below the
   hip joints — the same mask as for the death floor fit) that is above whatever lies under it (non-cloth
   body within 4 cm horizontally, otherwise the floor) is lowered onto that support, keeping 20% of its
   height above it: the robe lies on the legs and on the floor.
2. The world displacements are converted into shape displacements in the rest pose through the skinning
   Jacobian of that frame, measured numerically (three unit displacements through the Armature
   modifier) — the shape gives exactly this figure on the lying body.
3. The shape `af_death_settle` is driven by a driver from a property of the Hips bone; the death clip
   raises it 0 → 1 between the ground impact and the settling (from ¼ to ⅔ of the way after the impact),
   all other clips hold 0 (otherwise the property would stay set in the next clip after the death).
   In Blender it is seen by `render_sprites` and the preview; the glTF exporter samples armature-driven
   shapes into a morph-weight animation: in `styled.glb` the morph is `af_death_settle`, with weights
   0 → 1 in the death and 0 in other clips (checked by reading the GLB).
4. The shape is not created if there is no cloth or the "bell" is lower than 10 cm. Report:
   `death_info.robe_settle` (`bell_height_cm` before, `bell_height_after_cm` after, `max_drop_cm`,
   vertices, ramp frames). `robe_settle: false` turns it off.

Robed test character (fall_forward): the bell 107 cm → 21.5 cm (7,214 vertices lowered, by up to 86 cm);
the body is still on the floor (`death_on_floor`). On the test robe mannequin — the same behavior and the
morph in the GLB. Limitation: this is not cloth — the robe does not crumple into folds and ignores
collisions, it simply lies on the support; in the first frames of the fall the cone stays a cone.

## Tears on real clips: `deform_qa` (2026-10-11, night)

The `rig_humanoid` pose check (arms straight up and forward, a deep squat, a long step) is a proxy: real
clips contain few such poses. `motion_style` measures the same thing on the **character's final clips**
(after `retarget` and the manner), `_style_lib.ClipDeformQA`:

- every `deform_every`-th frame (by default every 2nd and always the last) of each clip is evaluated on
  the real mesh (Armature + shapes, including `af_death_settle`); "torn" is defined as in the pose
  check: the share of the **pose area** occupied by triangles that grew more than 3×; floor stubs and
  slivers are excluded;
- the thresholds are the same as in the pose check: **1%** in a frame where both arms are no higher than
  90° from "hanging down" (the "arms forward" reference point), **8%** with an arm raised by 170° (the
  "arms up" reference), linear in between by the most raised arm (`clip_limit`); the robe cloth has its
  own share, **4%**;
- per clip: the worst frame relative to its threshold, the share, the frame class (`main` /
  `raised_<angle>` / `arms_up`), the bones on which it tears, the number of frames over the threshold
  (`clips.json` → `deform`, `stats.deform_qa`); `clip_torn_<clip>` checks with the `deform_gate`
  severity (`error` by default: a character whose real clips tear does not pass the step with the
  numbers; `warning` — report only). `deform_limits` changes the thresholds.

Honest result of piloting: clips are **no softer** than poses. They contain what the poses do not:
elbow bend (a shield merged with the forearm tears at the shoulder), torso twists, deaths. Thresholds
other than those of the pose check should only be set explicitly through `deform_limits`.

## Measurements on an archer test character (Blender 5.0.1)

`rig_humanoid` rig (bow rigidly in the left hand), militia-idle / angel-walk / archer-attack /
militia-hit / militia-death clips through `retarget`, then `motion_style`: the whole step takes 4 s for
5 clips.

| what | input (`retarget`) | after `motion_style` |
|---|---|---|
| walk, tempo 1.15, stride 1.25: foot sliding / ground speed | 2.6 cm / 1.19 m/s | 2.6 cm / 1.49 m/s (= × 1.25) |
| walk: below the floor | −4.4 cm (toes at heel lift-off) | −1.4 cm (toes bend up) |
| attack: below the floor | −16.7 cm (the lower limb of the bow) | +0.1 cm (the hand turns the bow) |
| death: below the floor | −61 cm (militia-death sinks on this rig) | procedural: the end on the floor (±0.3 cm), 8 variants differ |
| loop seam | 0° / 0 mm | 0° / 0 mm |

The staff of the mannequin in the militia walk (without hand keys, `arm_swing` 1.8): without
`hold_props` it tilts noticeably, with it — less than 25° (test `test_real_staff_stays_upright_in_the_walk`);
the staff of a robed test character in a walk — 21° against 100–107° at the input, at rest 7° against 32°.

## Checks and `clips.json`

Errors: NaN; loop seam ≤ 0.5° and ≤ 1 mm; animation names in the GLB; sockets; NaN in the GLB; skin;
`death_on_floor_*`; `clip_torn_*` (severity — `deform_gate`). Warnings: `foot_skate_*` (sliding greater
than max(2 cm, 1.3 × input)) and `floor_body_*` (the body below the floor by more than 2 cm).

Per clip: `mode` (`styled` / `procedural_death` / `untouched`), `style` (the clip's final knobs),
`frames_in` → `frames`, `tempo_real`, `foot_skate_cm` and `foot_skate_in_cm` (sliding over the input's
contact frames; for an in-place walk — in the frame where the game moves the character),
`foot_plant_drift_cm`, `ground_speed_mps` and `ground_speed_in_mps` (the speed at which the game must
move the character), `hips_drop_max_cm`, `ik_unreached_frames`, `prop_fixes`, `clip_trunk_lean_deg` and
`forward_knobs_scale`, `floor` (the lowest body point over 16 frames), `seam_rot_deg`/`seam_pos_mm`; for
a death — `death`, `death_info` (`impact_s`, `impact_frame`, `arms`, `direction_deg`, `tilt_back_deg`,
`trunk_lift_cm`, `limb_floor_fixes`, `prop_floor_fixes`, `mesh_floor_correction_cm`,
`settled_trunk_low`), `death_final_lowest_m` (without the robe cloth) and `death_cloth_lowest_m`. At the
top level — `style`, `style_source`, `seed`, `build`, `death`, `held_props`.

## Parameters

```json
{"step": "motion_style",
 "inputs": {"blend": "abs/animated.blend"},
 "params": {"seed": "character_01.jpg", "build": "armored",
            "tempo": null, "stride": null, "arm_swing": null, "lean": null, "hunch": null, "head_bob": null,
            "sway": null, "limp": null, "limp_side": null, "swagger": null, "stiffness": null,
            "clips": {"walk": {"stride": 1.2}, "attack": false}, "apply": null,
            "death": "auto", "death_blend": 1.0, "undead": false, "hold_props": true, "floor": true,
            "floor_tolerance_m": 0.02, "floor_samples": 16, "ground_samples": 2000,
            "preview": true, "preview_cols": 6, "preview_tile": 220,
            "robe_settle": true, "deform_qa": true, "deform_every": 2, "deform_gate": "error", "deform_limits": null},
 "out_dir": "abs/out"}
```

Run: `blender -b --factory-startup --python scripts/blender/af_run.py -- motion_style request.json`.
Tests: `blender -b --factory-startup --python scripts/blender/tests/run_tests.py -- motion_style`.

The studio loads `server/forge-steps.mjs` at startup: for `motion_style` to be visible in the wizard and
in the HTTP batch mode, the studio must be restarted (after the current generation finishes).

## Found during piloting and fixed

- A staff resting on the floor tilted (now the hand lifts the wrist, the staff stands).
- A body in a robe stayed half-sitting after a fall — under linear skinning the robe is a rigid cone
  (now the robe does not hold the body above the floor; its going under the floor is recorded in
  `death_cloth_lowest_m`); see also `robe_settle` above.
- A tipped body "rose" by an angle instead of a lift (now the support point is the edge of the body, the
  turn back is no more than 12°).
- The floor check on a sample of 2000 vertices missed thin parts (now the last third of the death is
  aligned against the real mesh).
- An arm with an item longer than 1.2 m in a death is now lowered along the body.

## Honest limitations

- This is not motion capture: the manner is a deformation of existing clips. Strong values (hunch 1,
  lean 1, limp 1) give a caricature; keep them within plausible bands.
- The limp is built from a pelvis drop, a roll and a dragging foot at a symmetric rhythm: a real limp
  with a shortened stance on the bad leg would make the feet slide at a constant game speed.
- `stride` > 1 on short-legged characters runs into the leg length: the heel rises first, then the
  pelvis lowers (up to 7 cm) — visible as a squat while walking.
- Attacks and casts change only in tempo, lean and stiffness: a different strike is a different clip.
- The library has one suitable cast (`necro-rider-cast`, recorded for a rider; the legs are held by IK);
  the second (`knight-rider-cast`, a horizontal swing) is used only for unarmed characters and with
  short items.
- `deform_qa` measures the whole area, including the hidden one (behind a shield, under a robe) and
  slivers grown from very small triangles: a share above the threshold is not always visible on a
  100 px/m sprite. The thresholds are the same as in the pose check; use `deform_limits` explicitly to
  make them softer.
- `robe_settle` is one corrective shape for the end of the death, not cloth: in the fall the robe stays
  a cone, on the floor it lies without folds and collisions; in the GLB it is a morph with a weight
  animation (Godot imports it).
- A procedural death is key poses with physical timing, not a simulation: the body does not collide with
  itself (an arm can pass through a thigh), clothing does not fall separately, a shield and a cloak do
  not fly off.
- The floor is checked on a sample of 2000 vertices; the final check is over the whole mesh of the last
  frame. Cloaks hanging below the feet can lift the body by 1–5 cm (`extra_lift_cm`).
- An item in the hand stays rigid: a bow does not break, but in a fall it can lie on top of the body.
- Hunyuan generations often fail `rig_humanoid` (a long sword to the side, merged sleeves, a cloak) —
  then the case never reaches `motion_style`.

## What to download from Mixamo for variety

The names are as I remember them in the mixamo.com catalog; these are **search examples**, the exact
name on the site may differ. Download FBX, Without Skin, 30 fps; for walks and runs turn on In Place.
Everything on one character (Y Bot), then `node scripts/forge.mjs clips import <folder>`.

**Walks (the most important — there are 4 now):** "Walking", "Standard Walk", "Female Walk", "Old Man
Walk", "Drunk Walk", "Injured Walk", "Walking With Limp" / "Limping", "Sneak Walk" / "Sneaking Forward",
"Crouched Walking", "Swagger Walk", "Cocky Walk", "Strut Walking", "Sad Walk", "Happy Walk", "Brutal
Walk", "Orc Walk", "Mutant Walking", "Zombie Walk" (other variants), "Sword And Shield Walk", "Great
Sword Walk", "Rifle Walk" (for a crossbow), "Walking Backwards".

**Deaths (5 now):** "Dying", "Falling Back Death", "Falling Forward Death", "Standing Death Left 01/02",
"Standing Death Right 01/02", "Standing Death Backward 01", "Standing Death Forward 01", "Sword And Shield
Death", "Great Sword Death", "Two Handed Sword Death", "Zombie Dying", "Shot And Fall Backwards",
"Headshot", "Knocked Down" / "Knocked Out".

**Attacks by weapon:** sword and shield — "Sword And Shield Slash", "Sword And Shield Attack", "Sword And
Shield Kick", "Sword And Shield Casting"; two-handed — "Great Sword Slash", "Great Sword High Spin
Attack", "Great Sword Jump Attack"; one-handed — "Standing Melee Attack Horizontal", "Standing Melee
Attack Downward", "Standing Melee Attack Backhand"; polearm — "Standing 2H Melee Attack", "Spear Thrust"
(if available); bow — "Standing Aim Recoil", "Standing Draw Arrow", "Standing Aim Walk Forward";
crossbow — "Rifle Aiming Idle", "Shooting" (pistol/rifle clips as a base); unarmed — "Punching", "Hook
Punch", "Mma Kick"; undead — "Zombie Attack", "Zombie Punching", "Zombie Scream".

**Casts:** "Standing 1H Magic Attack 01/02/03", "Standing 2H Magic Attack 01–05", "Standing 2H Magic Area
Attack 01/02", "Standing 2H Cast Spell 01", "Spell Casting", "Magic Heal", "Casting Spell", "Wide Arm
Spell Casting", "Two Hand Spell Casting", "Kneeling Pointing" (a blessing).

**Idle and hits (fewer, but for variety):** "Breathing Idle", "Happy Idle", "Bored", "Looking Around",
"Warrior Idle", "Sword And Shield Idle", "Great Sword Idle", "Standing Idle 03"; "Hit Reaction",
"Standing React Large From Front / Back / Left / Right", "Head Hit", "Stomach Hit", "Big Hit To Head".

Every new clip multiplies the variety of a batch: a rotation over the library picks up everything that
is there, as long as the id is added to the pool of a role.
