# The "export" module: sprites and Godot packages

Two Blender worker steps (contract `docs/FORGE-CONTRACT.md`):

- `render_sprites` - sprite sheets from an (animated) model: 8 isometric directions (`iso8`) or your
  own set of directions (`custom`);
- `godot_package` - a package folder for Godot 4.5: GLB + a `.tscn` scene with effects on sockets + README;
  the package is validated by a real Godot 4.5.1 without a window.

Module files: `scripts/blender/steps/{render_sprites,godot_package,_export_lib}.py`,
`scripts/blender/data/fx_presets.json`, `scripts/blender/godot/{validate_package,af_fx_mount,af_preview}.gd`,
`scripts/blender/tests/test_export.py`. Nothing is written into any target project.

## Running

```
"C:/Program Files/Blender Foundation/Blender 5.0/blender.exe" -b --factory-startup ^
    --python scripts/blender/af_run.py -- render_sprites request.json
```

```json
{"step": "render_sprites",
 "inputs": {"mesh": "C:/abs/character.glb"},
 "params": {"profile": "iso8", "asset_id": "character", "clips": ["idle", "walk"],
            "height_m": 1.7},
 "out_dir": "C:/abs/out"}
```

A package for a ship (64.8 m, nozzles `size_class: "L"`). Presets are metric; without `params.scale`
the step scales them to the socket and the asset by itself - here `engine_trail`/`thruster_glow` get k = 10 and `nav_light`
gets k = 10.8. An explicit `scale` overrides the auto scale:

```json
{"step": "godot_package",
 "inputs": {"glb": "C:/abs/ship_flat.glb"},
 "params": {"asset_id": "ship_gunship", "profile": "godot", "kind": "ship",
            "fx": [{"preset": "engine_trail", "socket": "socket_thruster_main_port"},
                   {"preset": "engine_trail", "socket": "socket_thruster_main_starboard"},
                   {"preset": "thruster_glow", "socket": "socket_thruster_main_port"},
                   {"preset": "nav_light", "socket": "socket_utility_mast_01", "params": {"scale": 6}}]},
 "out_dir": "C:/abs/out"}
```

Tests: `blender -b --factory-startup --python scripts/blender/tests/run_tests.py -- export`.

## render_sprites

The camera is orthographic, on an empty rig at the origin; the lights (a sun from the top-left-front of the screen,
a cold rim light from behind-right, and a sky light) are children of the same rig. A direction
is obtained by rotating the rig around Z, so the lighting is identical in all directions and the skeleton's root
motion is left untouched. The world origin (the support point under the feet) is projected exactly onto the cell anchor;
1 m = `pixels_per_meter` pixels horizontally, a vertical meter is `ppm·cos(elev)`.

| Parameter | Default | What |
|---|---|---|
| `profile` | `iso8` | `iso8`, `custom` |
| `asset_id` | file name | snake_case; a name like `01_pathfinder` becomes `a_01_pathfinder` (with a warning) |
| `forward` | `-Y`; `+Y` if `inputs.manifest` -> `kind` is ship/prop/turret | `+Y` adds 180° to all views: S shows the ship's nose |
| `clips` | `all` | a list of names (`idle`) or action names (`idle-loop`) |
| `loop` | by the `-loop`/`_loop` suffix, otherwise idle/walk/run | `{clip: bool}` |
| `frames` | iso8: clip length × fps (≤ `max_frames`=48) | a number or `{clip: n}` |
| `fps` | 12 | |
| `ranges`, `ranges_fps` | the action's range | `{clip: [start, end]}` in frames at `ranges_fps` |
| `in_place` | `auto` | `auto` (loops: remove the pure root drift; one-shots: as is), `lock` (hip XY = first frame), `drift`, `none` |
| `pixels_per_meter` | 100 | fixed scale of the profile |
| `elevation_deg` | iso8 30 (2:1) | camera tilt |
| `directions` | `S,SW,W,NW,N,NE,E,SE` | a subset; `custom`: `[[name, yaw°], ...]` |
| `padding_px` | 6 | margin around the maximum extent of all frames of all clips and directions |
| `cell`, `anchor` | computed | a forced cell (identical across different assets) |
| `height_m`, `center`, `normalize_pose` | -, `feet`, iso8 `rest` | scale to a given height; support = the middle of the lowest 12 % of vertices |
| `max_cell_px`, `min_body_px` | 2048, 24 | a cell side larger than this, or a body projection (max of width/height) smaller than this, is an error **before rendering** |
| `max_sheet_px` | 8192 | soft limit for the sheet side; > 16384 (the Godot texture limit) is an error before rendering |
| `engine`, `samples` | iso8 `EEVEE` 32 | `EEVEE`, `CYCLES` (OptiX/CUDA), `WORKBENCH` |
| `shadow` | false (Cycles only) | shadow catcher + a rim light without its own shadow (`rim_off_catcher`); in iso8/custom the auto cell includes the shadow projection |
| `shadow_alpha_max`, `shadow_edge_fade_px`, `alpha_floor` | 0; 0 (iso8/custom with shadow 4, with a forced cell 16); 6 | shadow alpha cap, shadow edge fade, alpha floor |
| `cols` | iso8 - one row per direction if it fits in `max_sheet_px`, otherwise even rows | columns in the sheet |
| `validate_godot` | true | load the `.tres` in Godot 4.5.1 |

iso8 directions are the character's rotation (it faces -Y per §4): S 0° (facing the camera), SW -45°, W -90°,
NW -135°, N 180°, NE 135°, E 90°, SE 45°; with `forward: "+Y"` 180° is added to all of them.

Protection against wrong units: if `height_m` is not set and the rest-pose height is outside 0.3...15 m, there is a warning with
a `height_m` hint; a cell larger than `max_cell_px`, a body smaller than `min_body_px`, an overhang beyond a fixed cell
by more than half the cell, or a sheet larger than 16384 px give a `ValueError` with a hint before rendering (the GPU lock
is not occupied in vain).

Outputs: PNG sheets (iso8 - `<clip>.png` with all directions as rows; if the stack is taller than `max_sheet_px`,
`<clip>_<DIR>.png` for each direction), `sprites.json` (cell, anchor, `godot_offset` for `AnimatedSprite2D`, `forward`, the clip's `layout`, frame rectangles,
the support points of every frame: `root_px` - the projection of the point under the hips, `feet_px` - the points under the foot bones
(in `feet_bones` order), `feet_mid_px` - their midpoint; fps, loops, `time_scale`), `<asset_id>_frames.tres`
(`SpriteFrames`, animations `<clip>_<DIR>`; paths are relative), `preview.png` (frame 0 of each
direction), `preview_frames.png` (all frames of the first direction), `godot_check.json`.

Checks: `cell_fit` (vertices inside the cell), `body_scale_px_per_m`
(the body scale on the rendered frames - see below), `sprite_size_sane`,
`sheet_size`, `frames_body_inside_cell` (no empty frames, the body is no closer than 2 px to the edge), `alpha_residue_px`, `feet_on_ground_<clip>` (min z of the deformed mesh in loops),
`lighting_spread`, `godot_spriteframes`.

`body_scale_px_per_m` checks the body height by alpha on the rendered frames against the height of the vertex projection at
100 px/m (median over all frames, tolerance 3 %) and the top of the body against the projection through the anchor (<= 3 px).

### `cloth_sim` cloth cache (2026-10-12)

If the `.blend` came from the `cloth_sim` step (`docs/forge/cloth_sim.md`), the character's clothing (`Body_cloth_<group>`) carries
a baked simulation: one shape key per clip frame and, per clip, a shape-key action;
the object property `af_cloth_cache` = `{armature action: key action}`. On every clip switch (`_bind`) `render_sprites`
calls `_export_lib.bind_cloth_cache(clip actions)`: the clothing gets this clip's action enabled and its Armature
modifier disabled (the keys hold finished positions), and the key values of the previous clip are zeroed (otherwise a key
animated by the previous action keeps its last value). A clip without a cache and the rest-pose measurement (`_rest_points`,
iso8 normalization) use ordinary skinning (keys 0, Armature enabled). The cell and the first-pass checks are computed from the
simulated cloth. Test: `test_render_sprites_plays_cloth_cache` (a plane with a "left/right" cache of 0.6 m in
two clips: in each clip's sheet the cloth is on its own side, and the cell holds both).

## godot_package

The GLB from `cloth_sim` (`cloth.glb`) is an ordinary character GLB: the same armature plus `cloth_<group>_<chain>_<joint>` bones,
the same clip names; the package and the Godot 4.5.1 validation work unchanged (pilot: the scene loaded, `idle`/`walk`
loops, 9 sockets, no engine errors).

| Parameter | Default | What |
|---|---|---|
| `asset_id` | required | `^[a-z][a-z0-9]*(_[a-z0-9]+)*$` |
| `profile` | `godot` | `godot` |
| `forward_axis` | as in the GLB | `-Z`: the `model` node in the scene is rotated 180° around Y for `look_at`; the GLB is not touched |
| `source_forward` | from the passport `kind` / `params.kind`; otherwise a guess with a warning | the GLB's nose: ships/props/turrets `-Z`, characters/creatures/items `+Z`; the guess: thruster/turret/launcher/muzzle sockets -> `-Z`, otherwise `+Z` |
| `kind` | - | the asset kind when there is no passport (`ship`, `humanoid`, ...) |
| `fx` | `manifest.fx` | `[{preset, socket, params}]`; params: `scale`, `color`, `amount`, `lifetime`, any preset field |
| `fx_auto_scale` | true | false - presets at native size when there is no `params.scale` |
| `autoplay` | - | the animation on `_ready` (`idle-loop` or `idle`; `af_fx_mount.play_animation` tries the name as is and without one `-loop`/`_loop`/`-cycle`/`_cycle` suffix) |
| `comment` | passport name | free-form comment for the package |
| `inputs.sprites` | - | `sprites.json` from `render_sprites`: the sheets and `.tres` are put into `sprites/` and validated |
| `fx_preview`, `fx_preview_frames` | false, `[14, 30, 46]` | Movie Maker frames into `fx_preview.png` (the Godot window runs for ~3 s, hence off by default) |

**Effect scale.** Presets are metric: each is tuned to a reference - `reference_socket_m` (the seat/nozzle
diameter: `engine_trail`, `thruster_glow` 0.45 m, `muzzle_flash` 1 m) and `reference_size_m` (the asset's long
axis: ship effects 6 m, human-scale 1.8 m, `smoke` 3 m, `muzzle_flash` 1 m). Without `params.scale`, k is taken as follows:
1) the socket diameter (`diameter_m` / `mount_d_m` / `nozzle_d_m`) ÷ `reference_socket_m`; 2) the `size_class` of the socket or
of its parent (a turret rig `socket_mount -> ... -> socket_muzzle_*`) - the midpoint of the class range: S 1.0, M 2.25,
L 4.5 m ÷ `reference_socket_m` (for nozzles S ≈ 2.2, M 5, L 10); 3) the asset's long axis in the rest pose ÷
`reference_size_m`; 4) otherwise 1. k multiplies all lengths: `quad_size`, speeds, damping, accelerations, gravity,
the emitter shape, the light radius and `visibility_aabb` (otherwise the effect is culled when the box leaves the frame).
The k used and its source are written to `af_package.json` (`fx[].scale`, `scale_source`, `quad_size_m`,
`visibility_aabb`) and to the README. The warning check `fx_scale_vs_asset`: a particle smaller than 1 % of the asset's long axis,
or a "particle/asset" ratio more than 4 times off the preset's computed one (the second rule does not apply when
k comes from a socket diameter or `size_class`: such an effect is fitted to its own muzzle or nozzle, not to the whole
ship). `af_fx_mount.gd` removes the socket's scale
relative to the package root (a socket under a 0.01 node from a centimeter FBX), while the root's own scale
still scales the effects together with the model.

Package `<asset_id>/`: `<asset_id>.glb` (byte for byte), `<asset_id>.tscn` (`Node3D` + a GLB instance `model` +
`af_fx_mount.gd`), `af_fx_mount.gd` (on `_ready` it looks for sockets by `extras.game_socket`, then by name with
`: . @ / " %` replaced by `_`, and places the FX; `trigger(socket)` restarts one-shots), `fx/<preset>.tscn`
(`GPUParticles3D` + `ParticleProcessMaterial` + `QuadMesh`/`StandardMaterial3D`, optionally `OmniLight3D`),
`fx/textures/*.png` (procedural), `README.md`, `af_package.json`. All paths inside are relative:
the folder can be placed in any project directory (verified by placing it at `res://packages/<id>/`).

**Sockets and pivots.** A socket is a node with `extras.game_socket` or a name `socket_*`; the check
`socket_names_snake_ascii` (a warning in the `godot` profile) and the socket search in Godot go
only by these. Turret rig nodes `pivot_yaw` / `pivot_pitch` (`_export_lib.is_rig_pivot`) are not counted as sockets,
even if an old GLB carries `game_socket` on them: they are written separately to `af_package.json` -> `pivots` (axis,
range) and to the README; the warning check `rig_pivots` requires `axis` + `range_degrees` and no
`game_socket` (contract §6). Before this fix `pivot_yaw` did not match the `socket_*` pattern and the turret package
failed with an error.

**FX axes.** An effect emits along its local **+Y**. For ordinary sockets this is **Godot +Y = socket +Z in Blender**
("outward": the turret seat normal, the blade/shaft of `socket_mount_hand_*`); `muzzle_flash` goes along **Godot -Z = Blender +Y**
(forward of the muzzle). Nozzle sockets (`kind: thruster` or `socket_thruster_*`) are an exception: the nozzle frame
of Forge ships is not unique (`extras.frame`, `_ship_lib.THRUSTER_FRAMES`, `docs/forge/ships.md`), and the local +Y
can be "up". This was the case: with the `thrust` (default) and `exhaust` frames the `engine_trail` / `thruster_glow` trails
flew upward - the `+Y` of the socket `socket_thruster_main_port` in Godot 4.5.1 was `[0, 1, 0]`, and only the
legacy frame and old GLBs worked. Now for every nozzle socket the step finds the **exhaust axis in the socket's own axes**
(glTF/Godot):

| socket `extras.frame` | exhaust axis | meaning of the frame |
|---|---|---|
| `thrust` (the `ship_gen` default) | +Z | glTF -Z is thrust (forward along the ship), the exhaust goes the opposite way |
| `exhaust` | -Z | glTF -Z is the exhaust direction, as for a muzzle |
| none (old GLBs, the test's `build_ship`) | by geometry | usually +Y |

The documented axis is cross-checked against the geometry: after importing into Blender, the world direction of this axis (glTF local +X, +Y,
+Z = Blender +X, +Z, -Y) must point aft - the dot product with the stern >= 0.5 (the stern is Blender -Y for a GLB
with a -Z nose, +Y for a +Z nose; the same `source_forward` as for the scene axes is used). If there is no frame, the name is unfamiliar, or
the frame contradicts the geometry, the one of the six local axes (±X, ±Y, ±Z) that points aft best is taken;
the reason is written to `emit_axis_from` (`extras.frame thrust` / `inferred: most aft-pointing local axis +Y` /
`extras.frame thrust contradicts geometry; inferred: ...`), and a contradiction of a documented frame also produces a
warning. The axis is given to every FX binding on such a socket whose preset emits along +Y (in
`fx_presets.json` that is all but `muzzle_flash` - it and presets with another `direction` are not touched): `fx[].emit_axis` and
`emit_axis_from`, `emit_aft_dot` in `af_package.json` (in `sockets[]` - `frame`, `exhaust_axis`, `exhaust_axis_from`),
a phrase in the README, and the key `"emit_axis": Vector3(x, y, z)` in the scene's `fx_bindings` dictionary. `af_fx_mount.gd` rotates the
FX instance so that its +Y lies on `emit_axis` (`Quaternion(Vector3.UP, axis)`, for -Y a 180° rotation around X)
and after that, as before, removes the socket scale; `Node3D.scale` does not reset the rotation - verified on a socket
under a 0.01 node. For `+Y` the rotation is identity, so the behavior for old GLBs did not change.

Axis checks. `fx_thruster_axis` (error): every such binding has an exhaust axis whose world direction
points aft (>= 0.5). This is a safeguard against data loss, not against a skewed socket: among the six axes there is always
one within ~55° of the stern, so a socket rotated so that no axis matches the real exhaust passes
the check, and the effect goes along the nearest axis (up to ~55° off the stern) - for Forge ships the frames are exact, and for
`ship_from_mesh` it is worth looking at `emit_aft_dot`. `godot_fx_thruster_aft` (error): `validate_package.gd` computes the +Y axis of the
FX node in the package root's axes (`axis_y_root`, from `global_transform`) and compares it with the package's stern, `expect_axis`: +Z for a
GLB with a -Z nose, -Z for a +Z nose, with x and z of opposite sign if the scene rotated the model 180° (`forward_axis` differs from the
GLB's nose); `axis_ok` is a dot product >= 0.9, and for a nozzle canted by the model itself the `expect_dot` threshold is lower
by 0.05 than the measured cant (but not below 0.5), otherwise an honestly canted nozzle would fail the check. The stern is set by
`source_forward`: if it was guessed (no `kind`/passport), there is already a warning about that, and the exhaust axis is checked
against the guessed stern.

`fx_presets.json` presets: `engine_trail`, `thruster_glow`, `aura`, `magic_cast`, `fire`, `smoke`, `sparks`,
`ice`, `poison`, `heal`, `muzzle_flash`, `nav_light`.

Validation: a throwaway project is assembled in `out_dir/_godot_check` (`config_version=5`, `4.5`,
GL Compatibility, file logging and shader cache disabled), the package is copied to `res://packages/<id>/`, then
`--headless --import` and `--headless --script res://validate_package.gd` run. The Godot process is started with
`APPDATA`/`LOCALAPPDATA` = `out_dir/_godot_check/_home` (with `.gdignore`): editor settings and `user://`
stay inside `out_dir`, and nothing is written to `%APPDATA%/Godot` (contract §3). The script prints a line
`AFGODOT {json}`: the scene loaded and instantiated, animations and `loop_mode`, sockets (node, parent, world
position and +Y axis), FX under sockets (scale relative to the root `scale_rel`, the emission axis in root axes
`axis_y_root`, and with `expect_axis` also `axis_dot` / `axis_ok`, `visibility_aabb`, particle size), each FX scene
separately, `SpriteFrames` (frames, loops, speed, frame size). This JSON lies whole in the `godot_validation` check; from it come also
`godot_scene_loads`, `godot_animation_loops`, `godot_sockets_found`,
`godot_fx_attached`, `godot_fx_thruster_aft` (only if there are FX on nozzles), `godot_engine_errors` (the `ERROR`/`SCRIPT ERROR`
lines of the log).

**Clip names for Godot import.** The Godot 4.5 scene importer (`resource_importer_scene.cpp`, `_pre_fix_node`) checks an animation name in turn
for the markers `loop_mode`, `loop`, `cycle` (`-marker`/`_marker` at the end, or `$marker`), sets a loop,
strips the marker and calls `rename_animation(ORIGINAL_name, new)`. If two markers match (`pitch_cycle-loop`: first `-loop`,
then `_cycle`), the second rename looks for the already nonexistent original name - `ERROR: Animation not found: pitch_cycle-loop`,
and the clip stays `pitch_cycle` with a loop. There are no references to the suffixed name in `af_fx_mount.gd`/`af_preview.gd`/`.tscn` - the error
comes from the import itself. `godot_package` models this rule (`_export_lib.godot_import_name`) and in the package GLB renames
such clips to a name that imports to the same `clip_base` without a repeated rename (`godot_safe_clip_name`:
`pitch_cycle-loop` -> `pitch_cycle-cycle` -> in Godot `pitch_cycle`, `loop_mode = 1`). Other clips and the binary part of the GLB are
unchanged (only the JSON block is edited, `rename_glb_animations`). The renames are in `stats.clip_renames`, in `af_package.json`
(`clips[].package_name`) and in the package README; the `godot_animation_loops` check expects the name from the package.

## Measurements (2026-10-10, RTX 4070, Blender 5.0.1, Godot 4.5.1)

Module tests are run with `run_tests.py -- export`; temporary folders are not left behind. Among them:
`test_godot_import_names_and_glb_rename`; the turret test requires `clip_renames`, `pitch_cycle` with `loop_mode = 1` and an empty `godot_engine_errors`;
three exhaust-axis tests; and `test_turret_package_pivots_are_not_sockets`: `turret_gen` -> `godot_package` with a Godot check,
the same turret with obsolete `game_socket` on the pivots (the `rig_pivots` warning, no errors) and the `ship_gen` nozzle's `mount_d_m`
as the `engine_trail` scale source ("socket diameter ...").

**Nozzle exhaust axis (2026-10-10, evening).** Three tests: `test_thruster_frames_fx_aft_godot` (a `ship_gen` fighter, seed 1, in both
frames `thrust` / `exhaust` -> `godot_package` with `engine_trail` + `thruster_glow` on every
`socket_thruster_*`: `emit_axis` = +Z / -Z, in Godot 4.5.1 each FX's `axis_y_root` = `[0, 0, 1]`, i.e. aft;
the socket's own +Y is `[0, 1, 0]` (up) for `thrust` and `exhaust`),
`test_thruster_axis_legacy_inferred_and_rotated_model` (a GLB without `extras.frame` -> the +Y axis is "inferred"; `muzzle_flash` and
non-nozzle sockets get no axis; a `thrust`-frame socket under a 0.01 node - scale removed, rotation kept; a model rotated
by `forward_axis: "+Z"` expects `axis_y_root` = -Z) and `test_thruster_axis_contradicting_frame_and_canted_nozzle` (the
`thrust` frame on sockets with "+Y" geometry - geometry wins, one
warning; an unfamiliar frame; a nozzle canted by 30° - `axis_dot` 0.866 is accepted). Without the rotation in
`af_fx_mount.gd`, the first two tests fail on `godot_fx_thruster_aft`. On a real GLB: a ship_gen ship with four `thrust` nozzles from the library -
`axis_y_root` of all FX is `[0, 0, 1]`, `axis_ok` everywhere, no engine errors.

**Turret** (`turret_gen` M, 2 barrels, military, seed 1 -> `turret_m_military_b2_001`, `kind: turret`, `muzzle_flash` on both muzzles): Godot 4.5.1 headless - the scene loaded and
instantiated, sockets found **3 of 3** (`socket_mount`, `socket_muzzle_01/02`), pivots 2 (outside the socket
list), FX 2 of 2, loops `yaw_sweep`/`pitch_cycle` = 1, `fire_recoil` = 0; all error checks green, 4.3 s
per step (Godot 2.9 s). The `godot_engine_errors` warning ("Animation not found: pitch_cycle-loop" on import) was resolved
on 2026-10-10 in the evening: the clip in the package GLB is called `pitch_cycle-cycle` (see "Clip names for Godot import"), the Godot 4.5.1 log
is clean, `pitch_cycle`/`yaw_sweep` have a loop, `fire_recoil` does not. A read-only GLB inspection (`--class turret --strict`)
of the source and of the packed GLB: PASS, 3 sockets.

**iso8, a test character, default parameters** (raw Mixamo: `idle.fbx` with a skin + `walk.fbx`,
clips `idle-loop`/`walk-loop`, 100 px/m, 48 frames per clip): 2 × 48 × 8 = 768 frames, EEVEE, render 68.6 s
(0.089 s/frame), the step 81.7 s together with the Godot check. Cell 224×200, anchor (112, 158). The sheets are
**5376×3200** (24 columns × 2 even rows per direction; it was 8064×3200 with a half-empty second row),
5.5 and 6.1 MB. Body brightness spread across directions 1.08. Min foot z: idle 0...0.002 m, walk -0.028...0.035 m.
Deviation of the support points from the anchor: hips idle 4.9 px / walk 30.3 px, foot midpoint idle 2.6 px / walk 30.7 px -
this is the clip's own sideways sway; `drift` keeps it, `lock` removes it at the cost of foot sliding. Godot loaded all 16 `SpriteFrames` animations.

**custom + Cycles shadow** (the same walk, 6 frames, views 0° and -60°): the 356×188 cell includes the shadow projection,
12 frames in 6.4 s (0.53 s/frame, OptiX); frames with alpha within 2 px of the edge - **0 of 12** (before the fix the shadow
was cut off by the cell edge with a straight line).

**Unit protection** (through `af_run`): a 1.7 cm marker -> `AFERROR ... projected model is only 1.6 px
(min_body_px=24; ... fix the units with params.height_m ...)`, exit code 1; a 170 m marker (centimeters taken as meters) ->
`sprite cell 4412x15972 px exceeds max_cell_px=2048`, exit code 1, in 1.4 s - before rendering. A 17 m marker at 10 px/m
renders with a `height_m` warning.

**iso8, a static generated model** (Hunyuan, 119 997 tris, 3.0 m -> `height_m` 1.8, scale 0.6): 8 frames in 1.5 s,
cell 160×228 at 100 px/m. Anchoring on the feet shifted the model by
14.6 cm: the extent center is offset by the held item, and the anchor is now under the body. S - facing, the item in the character's left hand on the
right of the screen, N - the back.

**godot_package, a 64.8 m test ship** (36 676 tris, 17 sockets, nozzles
`size_class L`, flare diameter 6.4 m): without `params.scale`, `engine_trail` and `thruster_glow` got k = 10
(L: 4.5 m / 0.45 m), `nav_light` on the mast k = 10.8 (64.8 m / 6 m); the trail particle is 9.5 m,
`visibility_aabb` (-80, -20, -80, 160, 160, 160) - Godot returned the same values, `scale_rel` = 1.0;
`fx_scale_vs_asset` ok; the step took 7.3 s together with the Movie Maker preview, of which Godot import and validation 3.4 s. With `fx_auto_scale: false` (the old behavior) - a dotted line of
beads and an invisible flame, and `fx_scale_vs_asset` gives 4 warnings. The GLB nose `-Z` was guessed from the sockets - with a
warning (in the example above `kind: "ship"` removes it).

**A test character** (`profile: godot`, `forward_axis: -Z`, iso8 sprites): the long axis in the rest pose is 1.70 m, `aura` and
`poison` got k = 0.944, `fire` an explicit 0.5; `idle-loop`/`walk-loop` -> `idle`/`walk` with `loop_mode = 1`,
`socket_fx_chest`/`socket_mount_hand_r` under `BoneAttachment3D`, all FX `scale_rel` = 1.0, the `model` axis -Z,
16 `SpriteFrames` animations without discrepancies, engine errors 0. A 5.85 m test ship with 0.44 m nozzles
(`mount_d_m`) stays at native size (k = 0.978); a socket under a 0.01 scale node gives FX with `scale_rel` = 1.0.

Effects in Godot 4.5.1 (GL Compatibility, Movie Maker frames): a board with all 12 presets and a flying test
ship, and below it a gunship without auto scale and with it (frames 4, 18, 34: the trail accelerates,
the red navigation light on the mast is visible).

## Limitations (honestly)

- The FX auto scale is a heuristic. A class's seat is taken as the midpoint of its range (S 1.0, M 2.25, L 4.5 m); L -> 10
  was tuned by eye on a gunship (flare 6.4 m; the exact ratio would give 14). The ship module writes only `size_class` on nozzles,
  so the exact size will appear once `mount_d_m` is written into the socket. The fallback rule by the asset's long
  axis is crude for small details (a muzzle flash on a whole ship comes out large) - there `params.scale` is needed;
  `fx_scale_vs_asset` catches only obvious misses.
- EEVEE in Blender 5.0 has no shadow catcher, so iso8 has no ground shadow by default; a shadow exists only
  with `engine: CYCLES`. The cell is symmetric about the anchor, so a shadow falling to one side widens it on both
  (356×188 for the test character). Cycles leaves a pale line (alpha ~10) along the frame edge - by default the 4 px shadow fade removes it.
- Pose-specific fits for individual units (lifting a blade above the ground, folding wings, shifting a corpse into the cell) are specific to units and not
  carried over: a model that does not fit the cell is a `cell_fit` error, not an automatic fix. Palette compression of
  sheets is not carried over either - sheets are RGBA (iso8 default is 5.5-6.1 MB per clip).
- Artistic fixes for a specific unit (texture recoloring, a turn of the skeleton, shoulder repair) are outside the module;
  the test character was taken raw from a Mixamo upload, and the shoulder deformations are whatever that rig gave.
- GL Compatibility does not support particle trails (`trail_enabled`, verified: Godot
  writes a warning). `engine_trail` is large semi-transparent particles in world coordinates: in motion they
  merge into a solid trail, standing still they collect into a bright spot at the nozzle (use `thruster_glow` for parking);
  on very fast ships "beads" may show through again.
- The effect presets were checked structurally in Godot (loading, class, material, attachment, scale) and viewed
  by eye in Movie Maker scenes; they were not tuned to any particular game's art direction, and the textures are procedural.
  A visual preview requires running a Godot game windowed (`fx_preview`).
- The FX direction depends on how the source module oriented the socket; the export module does not fix the socket
  frame. Without a passport and `kind`, the GLB nose is guessed (with a warning).
- `forward_axis: -Z` rotates the `model` node in the `.tscn`; the GLB itself stays `+Z`, and the package metadata
  honestly records the GLB's axis.
- `in_place: auto` for loops removes only pure root drift; hip sway stays in the frame.
  The root bone is found heuristically (`mixamorig:Hips`, `Hips`, `root`, `pelvis`, otherwise the first one without a parent),
  the feet by `mixamorig:LeftFoot/RightFoot`, otherwise bones with `foot`/`paw_` in the name.
- iso8 without `frames` takes the clip length (a 4.4 s idle -> 52 frames) and cuts it to `max_frames`; the actual
  speed-up is written to `time_scale`.
- Rider + mount bundles (actions `<clip>|<skeleton>`) are supported by convention, but were not tested on real
  assets.
- The Godot check runs without a window: it does not judge the picture, only loading, names, loop modes, scale and
  attachment. Earlier runs (before this fix) left `%APPDATA%/Godot/app_userdata/af_godot_check`
  (logs, shader cache) - the folder can be deleted by hand.
