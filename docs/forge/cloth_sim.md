# The cloth_sim module: real cloth on character clothing (2026-10-12)

Requirement: robes, cloaks, tabards, skirts and banner cloths must move like cloth. Before this step, all clothing of
generated characters was skinned to the bones like the body: in walk, attack and death it stretched, slid along with
the hips and "swam" (the texture swam with it), and a character lying dead left a standing bell of robe behind
(the `af_death_settle` corrective shape of `motion_style` hid only the end of the death clip).

The `cloth_sim` step runs right after `motion_style`: it finds loose clothing, separates it from the body, simulates
every clip with real Blender cloth (collisions with the body, shafts and the floor), bakes a per-clip cache that
`render_sprites` switches together with the clip, and for GLB (Godot, any engine) transfers the same motion to
secondary cloth bones.

Files: `scripts/blender/steps/cloth_sim.py` (the step), `steps/_cloth_lib.py` (the core), `steps/_export_lib.py`
(`bind_cloth_cache`) and `steps/render_sprites.py` (cache switching), tests `scripts/blender/tests/test_cloth_sim.py`
(10 tests, 82 s, all PASS on 2026-10-12) and `test_export.py::test_render_sprites_plays_cloth_cache`.

## Place in the pipeline

```
cleanup → paint_from_concept → rig_humanoid → retarget → motion_style → cloth_sim → render_sprites → godot_package
```

The input is the `styled.blend` of the `motion_style` step (slot `blend`); the `clips.json` next to it provides loops
and categories. Outputs:

| key | file | what |
|---|---|---|
| `blend` | `cloth.blend` | body without clothing (`Body`), clothing `Body_cloth_<group>` with an Armature modifier and a per-clip cloth cache; hidden `Body_glb` (the whole body with cloth-bone weights) |
| `glb` | `cloth.glb` | rig + bones `cloth_<group>_<chain>_<joint>`, the same clip names, the `af_cloth_settle` morph for the end of death |
| `clips` | `clips.json` | the `clips.json` of the `motion_style` step + a `cloth` entry per clip (mode, timing, per-group checks) |
| `report` | `cloth.json` | segmentation, groups and presets, "cloth / original skin" checks per clip and group, cloth bones, timing |
| `preview` | `preview.png` | "skin / cloth" rows for walk, attack/cast and death |
| `segmentation` | `segmentation.png` | rest pose: cloth regions in color, attached parts lighter |

Everything downstream works unchanged: `render_sprites` takes `cloth.blend` (slot `blend`), `godot_package` takes
`cloth.glb` (slot `glb`).

## How it works

### 1. Segmentation (`_cloth_lib.segment`)

Works on the rest pose from geometry and skin, with character height H:

- **Legs**: horizontal sections from ankles to hips (`leg_slices`, layers of 3 % H, connectivity within a ±3.5 % H
  window). A section around one leg that is no wider than 2.4 radii of the anthropometric capsule is a leg (plate
  armor, boots, trousers), not cloth; a section around both legs is a robe/skirt; a free section without a leg that
  is wider than 8 % H is a hanging flap. The thickness of each leg is measured from its own sections (`leg_bulk.k`),
  and the leg capsules are stretched to it.
- **Lower part** (robe, skirt, tabard, rear flap): below the hip joints, farther than 2 % H from the (fitted) leg
  capsules if the leg was measured, or farther than `leg_margin` (3.5 % H; 2.5 % for `build: robed`; 5 % for
  `armored`/`heavy`). Not cloth: boots and greaves (weight ≥ 0.5 on the shin/foot and a 6.5 % H zone around the
  foot), anything skinned to the arm (sleeve tails), floor plates (the generator's pedestal: faces pointing up near
  the floor and their edge up to 3 % H), props in the hand. A piece must hang from above the knees to below the
  middle of the thigh. It is a robe/skirt if the ring at knee height covers ≥ 75 % of the turn, otherwise a tabard
  (front) with a rear flap, or a rear flap (cloak tail).
- **Cloak**: grows upward from the lower part behind the back while it stands off the torso by more than 3 % H (a
  cloak lying on the back moves with the back).
- **Hood**: stands off the head capsule by > 3 % H above the neck (≥ 300 vertices). **Sleeves** (only for a robe or
  `robed`; `sleeves: true` forces them): skinned to the shoulder/forearm and wider than the forearm by 3.5 % H.
- **Banner**: `find_pole` finds the longest straight tube through a fist (900 directions up to 50° from vertical, a
  ring of vertices around the axis over most of the length); the cloth grows from the vertices on this hand along its
  plane outside the body and outside the shaft. It is accepted if at least 25 × 20 cm, thin, its plane contains the
  shaft, and the cloth hangs from the shaft away from the fist or from its top. The shaft also goes into the group
  (rigid to the hand).
- **Attachment** (`attachment_weights`): geodesic ramps from the seam with the body above the knees, the belts, the
  hood neck, the sleeve seam, the shaft edge and the top of the banner (7–12 % H). Everything above the hip joints is
  1 (belt); a seam below the knees does not hold.
- `params.cloth`: manual boxes `[{kind, min, max, only}]` (rest pose, meters); `false` means no cloth.

Simulation groups: `lower` (lower part + cloak), `hood`, `sleeve_l/r`, `banner_l/r`; each has its own object and
preset.

### 2. Separation and the simulation mesh

The faces of a group go to the object `Body_cloth_<group>` (a copy of the body: UV, material, weights, Armature) and
are removed from the body; the seam vertices exist in both objects and are always attached, so there is no visible
seam. A disc/underside closing the hem of the robe (faces pointing down below 20 % H) is deleted by default
(`caps: delete`): with a moving hem it would stand up like a lid. The clothing itself is simulated (no extra
vertices; duplicates are welded, non-manifold edges repaired). Clothing above 12 000 vertices goes through a QuadriFlow
proxy (`remesh: auto`) whose motion is transferred back to the clothing by embedded deformation (`bind_embedded`: a
vertex follows the corners of the nearest proxy triangle with the rotations of their rings). The free part of the
lower garment hangs from the pelvis (`pelvis_follow`: leg weights are transferred to Hips in proportion to freedom),
so the legs push the robe by collision rather than tearing it by weights. Pieces without attached vertices (inner
layers, patches) get a soft attachment of 0.6.

### 3. Simulation (`setup_cloth`, `simulate`)

- Colliders: body capsules on the bones (anthropometry, legs stretched to the measurement, +6 mm, friction 1),
  capsules of shafts in the hand (`collide_props`; a banner cloth does not collide with its own shaft), the floor.
  Capsules that the clothing already penetrates at rest are narrowed (`fit_capsules`, a skinny character in a robe).
- Each clip: the first pose is held for 20 frames (`preroll`); a loop runs up over a whole cycle to frame 0, then
  the cycle, and the last frames (P/3, 6–15) are stitched to the frames one period earlier (`clip_window`), giving a
  seamless loop. The Blender cloth step is 1/30 s per frame at any fps (`time_scale` = 30 / fps).
- **Shape hold** (`shape_hold`, per preset): loose cloth is pulled gently toward its "pelvis-hung" shape, which keeps
  the silhouette drawn by the generator while the cloth still lags, sways and is pushed by the legs. Near the floor
  (0.2 m) the hold fades out; in deaths it is off, so the robe falls and lies freely.
- A blow-up (a point farther than 1.2 m from the capsules, or NaN) gets one retry with doubled steps and damping,
  then the clip stays on the skin with a warning.

| preset | for | mass / vertex | tension / compression / shear | bending | hold | steps | note |
|---|---|---|---|---|---|---|---|
| `wool_robe` | robe, skirt, flaps, hood, sleeves | 0.30 | 15 / 15 / 8 | 8 | 0.45 | 5 | wool/cotton |
| `heavy_cape` | cloak, rear flap | 0.55 | 30 / 30 / 15 | 6 | 0.30 | 5 | heavy cloak |
| `tabard` | tabard | 0.40 | 30 / 30 / 15 | 10 | 0.35 | 5 | stiff, heraldic |
| `light_silk` | banner | 0.15 | 8 / 8 / 4 | 0.3 | 0.10 | 5 | silk + internal springs |

`presets: {"lower": "heavy_cape"}` or `{"banner_l": {"base": "light_silk", "mass": 0.1}}` changes a group.

### 4. Per-clip cache and sprites

The result of each clip (cloth; outside the narrow seam band it is a pure simulation) is baked into shape keys of the
clothing: a key per clip frame with absolute positions, and a shape-key action per clip with keys 0/1 (linear between
frames). The property `af_cloth_cache` = `{armature action: shape-key action}`. In `_bind`, `render_sprites` calls
`_export_lib.bind_cloth_cache(clip actions)`: the clothing gets the clip's action enabled and its Armature modifier
turned off; the keys of the previous clip are zeroed. A clip without a cache (`clips` is not "all") and the rest-pose
measurement run on skinning. See `docs/forge/export.md` → "Cloth cache cloth_sim".

### 5. GLB: cloth bones

For engines without baked simulation: for each group, bone chains from the attachment line (one per ~20 cm, up to 16
chains, 4 joints each); the chain root is a child of the body bone it hangs from. Keys in each clip: the chain joints
are the centers of cloth bands at equal geodesic levels from the simulation of this clip (without a simulation, a
Verlet spring chain with gravity, damping and capsule collision); a bone places its head on its joint (translation
keys) and rotates toward the next one (rotation only, no twist). Weights of the `Body_glb` body: first "attachment ×
body skin + freedom × chains", then a least-squares fit to the simulated clothing over all frames of all clips
(4 influences, `fit_weights`). End of death: bones cannot lay the robe cone on the floor (they swing it as a fan), so
the corrective morph `af_cloth_settle` places the clothing exactly where the simulation left it; it is driven by a
property of the Hips bone that death raises 0 → 1 after the impact (the other clips hold 0). The GLB is exported from
the re-read `cloth.blend`: drivers created in the same session are baked into morph weights by the glTF exporter only
after re-reading (measured: before, constant 0; after, 0 → 1).

### 6. Checks (`cloth.json` → `clips.<clip>.groups.<group>`)

On every 2nd frame of the clip, over the visible clothing, "cloth" against "original skin" (a copy of the clothing
before the changes):

- `penetration_frac`: the share of free points deeper than 1 cm inside the body capsules (worst frame),
  `depth_max_cm`; check `cloth_penetration_<clip>_<group>`: ≤ 3 % or no worse than the skin + 1 %;
- `stretch_p99`: the 99th percentile of edge length / rest length over free edges; `torn_frac`: the share of area
  that grew > 3× (the `motion_style` measure); `torn_frac_garment`: the same over the whole garment; check
  `cloth_stretch_*`: ≤ 1.6 or no worse than the skin + 0.05;
- death: `death_floor_min_cm` (clothing on the floor, not under it; check `cloth_death_floor_*` ≥ −2 cm),
  `death_lying_height_cm` against the skin;
- `proxy.far_from_body_m`: stability (`cloth_stable_*`), `sim_s`, `s_per_frame`;
- GLB: `gltf_animations`, `gltf_skin`, `gltf_no_nan` (errors), `cloth_bones`, `gltf_death_morph` (peak morph weight:
  death 1, the others 0), `cloth_time_budget` (300 s by default).

Cloth checks are warnings by default (`qa_gate: warning`); `error` makes them a gate of the step.

## Parameters

```json
{"step": "cloth_sim",
 "inputs": {"blend": "abs/05_motion_style/styled.blend"},
 "params": {"cloth": "auto", "build": "robed", "leg_margin": null, "hood": true, "sleeves": "auto", "banners": true,
            "presets": null, "clips": "all", "remesh": "auto", "remesh_above": 12000, "proxy_edge_m": 0.045,
            "quality": null, "preroll": 20, "from_bind": 0, "caps": "delete", "pelvis_follow": true,
            "inflate_m": 0.006, "collider_friction": 1.0, "collide_props": true, "shape_hold": null,
            "max_disp_m": 1.2, "qa_every": 2, "qa_gate": "warning", "penetration_limit": 0.03, "stretch_limit": 1.6,
            "cloth_bones": true, "bone_spacing_m": 0.2, "bone_joints": 4, "bone_chains_max": 16,
            "fit_weights": true, "death_morph": true, "time_budget_s": 300, "preview": true},
 "out_dir": "abs/06_cloth_sim"}
```

Run: `blender -b --factory-startup --python scripts/blender/af_run.py -- cloth_sim request.json`.
Tests: `blender -b --factory-startup --python scripts/blender/tests/run_tests.py -- cloth_sim` (10 PASS, 82 s: loop
timing and seam, capsules and fitting, transfer by embedded deformation, the cache switches with the clip,
segmentation of a mannequin robe, an end-to-end run on the mannequin with GLB, a pass-through without clothing, a real
generated robed character; the last test cleans up its temporary folders and skips when its fixture is absent).

## Pilot findings (2026-10-12)

A pilot ran on four generated humanoid characters (a robed skeleton, a robed caster with sleeves, a robed figure with
two banners, an armored figure with a tabard); Blender 5.0.1, CPU simulation. The whole step for 5 clips together with
GLB and preview took 15–97 s, of which the simulation took 7–56 s; the cloth bones numbered 4–84 per character.
Simulation speed: 0.03–0.08 s per simulated frame per group of 5–6 thousand vertices (5 steps, collision quality 2);
a clip with run-up and hold is 80–190 frames.

Checks on visible clothing, "cloth / original skin" (the worst of every 2nd frame; the free part is `af_cloth_free` ≥
0.8):

- **Penetration** of cloth into the body dropped by a factor of 3–30 for all of them (a tabard in walk: 88 % → 13 %;
  under skinning it passed through the thighs).
- **Banners** under skinning tore (stretch 37–59, "torn" 76–86 %: the generator skinned the cloth half to the head
  or foot); now they hang whole on the shaft (1.0–1.5).
- **Death**: the clothing lies on the floor (hem 0.4–0.9 cm); a lying robe is 32 cm lower than under skinning
  (without the `motion_style` corrective shape).
- The other side: **local stretching** of the cloth is above that of the skin (p99 2–4 against 1–2, "torn" 5–20 %).
  The cloth crumples and stretches where the legs push the inner edge of the robe and at thin mesh bands from the
  Hunyuan generator; on a 100 px/m sprite this shows as a ragged hem in some frames.
- GLB weight fit error before → after fitting: 2.5–9.2 cm → 2.4–6.8 cm for robes and the tabard, 0.6–2.2 → 0.5–1.9 cm for banners; the death
  morph removes 10–25 cm (p90).

GLB in Godot 4.5.1 (`godot_package` + a custom headless script, playing every clip, 8 points): all four packages
load, `idle`/`walk` loops = 1, 9 sockets, no engine errors; cloth bones are present, chain tips move relative to the
pelvis (0.37 m in walk and 0.63 m in cast for the robed skeleton; 0.11 m for the tabard), and the weight of the morph
`af_cloth_settle` at the end of `death` = 1, 0 in the other clips.

Qualitative review: the hem lags and drags in walk; in cast the robe hangs and crumples at the floor instead of a
rigid bell; in death the robe stands for one frame as a tube after the hit, then lies down; the hem is ragged in places
(the generator's mesh). Narrow robes show little difference on the sprite (the legs fill them). Banners hang on their
shafts under their own weight and sway instead of tearing as under skinning. A tabard no longer passes through the
thighs in walk and attack. Worse than before: the robe silhouette is softer than drawn, some frames have a ragged hem,
the belt and bags do not collide. Summary: deaths and banners are a clear improvement, robes in walk a moderate one, and
the tabard-through-body bug is fixed.

## Registration (for the integrator)

The step does not touch integrator files. It is registered in `server/forge-steps.mjs` right after `motion_style`:
step `cloth_sim`, module `mixamo`, kinds `['humanoid']`, input slot `blend` (required, format `blend`), outputs
`glb`, `blend`, `clips`, `report`, `preview`, `segmentation`, timeout 30 minutes; its params correspond to the JSON in
"Parameters" above (for `cloth` the value `'auto'` finds the regions itself, `false` means no cloth, and
`[{kind, min, max, only}]` are boxes in the rest pose in meters, `kind`: robe | skirt | tabard | back_panel | cape |
hood | sleeve | banner | none). The studio reads the registry at start, so restart it after a registry change; a run
with `--direct` works immediately.

A pipeline preset may place `{ step: 'cloth_sim', params: {} }` between `motion_style` (or `retarget`; the step also
works on `animated.blend`) and `render_sprites`. `render_sprites` and `godot_package` take `cloth.blend` and
`cloth.glb` from the chain themselves (slots `blend`, `glb`). In a per-character roles file one can set
`"cloth": false` (no cloth), `"cloth": [{"kind": "cape", "min": [...], "max": [...]}]` or
`"cloth_presets": {"lower": "heavy_cape"}`. It is worth surfacing the groups and `timing.total_s` from
`outputs.cloth_sim.report` (`cloth.json`) in a batch report.

Registry test (`tests/forge-steps.test.mjs`): the `cloth_sim` step is in the registry, the module
`scripts/blender/steps/cloth_sim.py` exists, the slots are `['blend']`, the outputs contain `glb` and `blend`.

## Honest limitations

- **The generator mesh is not a sewing pattern.** Hunyuan produces a single shell with sculpted folds, overlapped
  layers and thin bands; the cloth is simulated over this mesh. Hence local stretching (p99 2–4, "torn" 5–20 % for
  robes) and a ragged hem in some frames; self-collisions are off (by design; with them the folds would not pass
  through each other, but the step would be 3–5 times slower).
- **The silhouette is held by a pull.** Robes drawn as a rigid bell sag under honest gravity; the shape hold (0.45 for
  a robe) keeps the silhouette at the price of part of the freedom. In deaths the hold is off. `shape_hold` is
  tunable.
- **The collision body is capsules**: anthropometry with leg thickness fitted from the sections and narrowing under
  the clothing. Belt, bags and pauldrons do not collide; the penetration depth in the worst frame is still 3–7 cm
  (almost always at the belt and in the groin, where the cloth is attached).
- **Segmentation is a heuristic.** Tested on the pilot characters and a mannequin: robes, tabard, banners and sleeves
  are found; errors happen. A second "flap" at the floor (the pedestal) is now cut off, and a false "shaft" along a
  sleeve is not counted as a prop (a rigidity check at the fist). A cloak on the back, lying on the body, stays on
  skin (moves with the back). The manual boxes `params.cloth` (and a roles-file `cloth`) are the fallback.
- **A banner** hangs under gravity: if the generator drew the cloth unfurled by wind, it drops. There is no wind.
- **GLB is an approximation.** Cloth bones (4 influences, fitted to the simulation) follow the cloth with a mean
  error of 2–7 cm; small folds are not transferred. The end of death is exact (the morph), the middle of the fall is
  not. In Blender the glTF importer does not show the morph animation on the armature action (Godot does).
- **Only for clips that exist in the `.blend`.** A new clip needs a new run of the step. The cache is stored as shape
  keys: `cloth.blend` is 10–62 MB (`styled.blend` is ~6 MB), the GLB hardly grows (bones + one morph: 4.9–6.7 MB).
- **Reset retry.** A clip that blows up (not seen in the pilot) is recomputed once, then stays on skin.
- The "before" numbers are the original clothing skin without the corrective shape `af_death_settle` (in
  `cloth.blend` the body without clothing keeps it; the clothing does not: its end of death comes from the
  simulation).
