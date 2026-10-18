# Forge: server, Blender queue, GPU lock, CLI ("server" module, 2026-10-10)

Implements `docs/forge/API.md` and §3, §8, §9 of the contract. All user-facing texts are localized in the UI.

| File | What it does |
|---|---|
| `server/blender.mjs` | finds Blender/Godot and their versions (cached), runs a step, parses `AFLOG`/`AFRESULT`/`AFERROR`, logs, timeouts, kills the process tree, FIFO queue with "slots" |
| `server/gpu-lock.mjs` | cross-process GPU lock (named mutex `Global\AF_GPU_RENDER`, shared with any other tool that uses the same name) |
| `server/forge-steps.mjs` | registry of 24 steps (2026-10-11: + `motion_style`, see `docs/forge/motion_style.md`; 2026-10-11, night: + `rig_humanoid.deform_gate`, `motion_style.robe_settle/deform_qa/deform_every/deform_gate/deform_limits`, `render_sprites.adaptive_cell`) and 8 presets (data), parameter validation |
| `server/forge-pipeline.mjs` | engine: passports, versions, input resolution, jobs, export, clips, clip library lock, batches, file serving |
| `server/forge.mjs` | `/api/forge/*` routes |
| `scripts/forge.mjs` | command line (through the server or `--direct`) |
| `scripts/forge-smoke.mjs` | smoke run of all presets in real Blender 5.0.1 and Godot 4.5.1 (outside `npm test`) |
| `tests/forge-*.test.mjs` | 56 fast tests without Blender (the real GPU lock is Windows only) |

Edits to files owned by other modules: `server/index.mjs` — one import line and one call
`registerForge(app,asyncRoute,{getGenerationJob:getJob})` right after `registerTripo`, i.e. after the protective
middleware (loopback Host/Origin, `x-astra-client: studio`). `server/jobs.mjs` — the GPU lock only around
submitting the ComfyUI prompt and waiting for the result (`waitPrompt`): three lines. These two files were not
changed in the fix round.

## Environment variables

| Variable | Default | Meaning |
|---|---|---|
| `ASTRA_BLENDER` | `C:/Program Files/Blender Foundation/Blender 5.0/blender.exe` | path to Blender |
| `ASTRA_BLENDER_CONCURRENCY` | 1 | concurrent Blender processes |
| `ASTRA_FORGE_JOB_CONCURRENCY` | same as Blender | concurrent jobs (a job = a chain of steps) |
| `ASTRA_GODOT` | search for `Godot_v*-stable_win64[_console].exe` in `C:/Program Files/Godot`, `C:/Godot`, `%LOCALAPPDATA%/Programs/Godot` | only used by `/status` |
| `ASTRA_GPU_LOCK_NAME` | `Global\AF_GPU_RENDER` | mutex name (tests use their own `Local\AF_TEST_*`) |
| `ASTRA_FORGE_LIBRARY` | `data/library` | library (passports, jobs, clips, trash) |
| `ASTRA_FORGE_RUNTIME` | `data/runtime` | Blender logs and working folders |
| `ASTRA_FORGE_URL`, `PORT` | `http://127.0.0.1:3210` | server address for the CLI |

Keep the library path short: Blender (Python) on Windows does not open files whose path is longer than
260 characters, and inside an asset another ~110 characters are added (`assets/<uuid>/v001/02_godot_package/…`).

## Data layout

```
data/library/assets/<id>/asset.json              passport (written via a temporary file + rename)
data/library/assets/<id>/source/source.<glb|fbx|obj>  copy of the input (generation job / asset / upload)
data/library/assets/<id>/v001/version.json       version recipe, base, final state (final)
data/library/assets/<id>/v001/asset.snapshot.json passport at launch time (input of godot_package)
data/library/assets/<id>/v001/01_cleanup/        out_dir of step 1: request.json, result.json, outputs
data/library/assets/<id>/exports/<profile>-v003.zip  godot_package / render_sprites archives
data/library/jobs/<jobId>.json                   job records (pid + owner heartbeatAt)
data/library/batches/<batchId>.json              batches
data/library/animations/index.json               clip library (written by the clip_library step)
data/library/trash/assets|clips/                 deleted items (nothing is erased for good)
data/runtime/blender/<jobId>/<step>.log          step log (if a step repeats in a job — <kk>_<step>.log)
data/runtime/forge/uploads/<uuid>/               temporary uploads (meshes — until the end of the request, clip FBX —
                                                 until the end of the import job; leftovers after a crash are removed after a day)
data/runtime/forge/jobs/<jobId>/01_clip_library/ out_dir of jobs without an asset (clip import)
```

## Running a Blender step (`server/blender.mjs`)

`runBlenderStep({step, request, jobId, timeoutSec, gpu, signal, onLog, onProgress, onStart})`:

1. A CPU step takes its place in the queue synchronously at call time — start order = call order (FIFO);
   cancelling a job that is still queued simply removes it from the queue.
2. A GPU step first waits for the GPU lock **outside** the queue (CPU steps keep running meanwhile), and once it has
   the lock it enters the queue ahead of ordinary steps — the GPU idles for no longer than the current step. The lock
   is released in `finally` (on error and on cancel alike).
3. In the slot: `out_dir/request.json`, then
   `blender -b --factory-startup --python scripts/blender/af_run.py -- <step> <out_dir>/request.json`.
4. stdout/stderr are read line by line (UTF-8, `\r\n`) and everything goes to the log. `AFLOG {json}` → `onLog`;
   progress is taken from `progress` (0..1 or 0..100), `pct`, `done/total` or `frame/frames`.
   `AFRESULT` is the result (if the line is missing but `result.json` was written during this run, it is used).
   `AFERROR` is the step error with a trace. If there is neither a result nor `AFERROR` (the `--python` script
   crashed before the af_run handler; Blender exits with code 0), the error text contains the last line of the Python
   exception from stderr, for example `… (code 0): FileNotFoundError: … request.json`.
5. Timeout: from the registry (`timeoutSec`), otherwise 15 min, GPU steps 60 min. Timeout and cancel kill
   the whole process tree: `taskkill /PID <pid> /T /F` (the test verifies that a grandchild dies too).
6. A result with `ok:false` (a failed check with `severity:error`) is not an exception: the engine marks
   the step as failed with the text of the failed checks.

`StepQueue.run(fn, {signal, onStart, front})` passes `fn` a `slot` object: `slot.release()` gives the place
to others, `await slot.reacquire(signal)` takes it again at the front of the queue (`false` on cancel).

## GPU lock (`server/gpu-lock.mjs`)

Node cannot own a Win32 mutex, so a hidden `powershell.exe -EncodedCommand` is started (the execution policy does
not interfere). It creates `System.Threading.Mutex($false,'Global\AF_GPU_RENDER')`, waits for it in 250 ms chunks
(prints `WAITING`), prints `LOCKED` and holds it until a line arrives on stdin or stdin closes (our process died).
The release is `ReleaseMutex` in `finally`.

- Compatibility: any other tool that serialises its GPU renders on the same named mutex runs strictly in turn with
  the forge and ComfyUI.
- Holder crash: if the holder process is killed, Windows marks the mutex abandoned; the next waiter gets it
  (`ABANDONED` → `lock.abandoned=true`). If nobody is waiting, the mutex object disappears with the last handle and
  a new acquisition succeeds as well. Both cases are covered by a test.
- `timeoutMs` and `AbortSignal`: when the wait is cancelled, the holder is killed (it did not hold the mutex yet).
- `failOpen:true` (this is how Blender steps and ComfyUI call it): if PowerShell cannot start, work
  continues without the lock and a warning in the log — generation does not break because of an auxiliary process.
- Who takes the lock: `render_sprites` always; `cleanup` — only with `voxel_remesh_m > 0` and
  `bake_device = 'GPU'` (the `gpuWhen` rule in the registry); generation in `server/jobs.mjs` — for the duration of
  each ComfyUI prompt (SDXL and Hunyuan separately), the wait is visible in the job status.
- Job queue: for the duration of a GPU step (waiting for the lock + render) the job gives up its place in the job
  queue, so jobs with CPU steps and clip import do not stand behind a render that waits for the GPU for up to 60 minutes.
  After the GPU step the job takes a place again — ahead of jobs that have not started yet.
- `/status.gpuLock.held` — whether this process holds the lock (not the whole computer).

## Step registry (`server/forge-steps.mjs`)

Table `STEP_LIST`: `{step, module, title, help, kinds, inputs:{slots, required, formats, manifest}, outputs,
params, gpu | gpuWhen, timeoutSec, export, scope}`. Parameter schema: `{type, default, min, max, enum,
nullable, items, minItems, maxItems, alt, title, help, server, auto, path}`. `alt` — fallback schemas: a value
that fails the main one is accepted if it fits one of them (`head_dir` — a label `auto|−Y|+Y|−X|+X` or a
vector `[x, y]`; `item_scale` — a number or `'auto'`; `speed` — a number or `{clip: m/s}`); the UI draws
the main schema. `inputs.manifest: true` — the server puts a snapshot of the passport into `inputs.manifest` (`render_sprites`,
`godot_package`). Compound parameters that the schema does not describe are checked by the `STEP_CHECKS` table before launch
(`retarget`/`attach_prop` clips, `attach_prop.attachments`, `separate.regions` zones, `bone_kit`: `part` is required,
`origin` and `length_m` per part, `batch` variants; `replace_part`: `region`, `cut_plane`, UUID `part_asset`,
`part_transform`; `place_parts`: `points` are required and are parsed point by point; `rig_humanoid`: `grip_dirs`, `socket_bones`,
`arm_bleed`; `retarget`: `facing`/`hips_drop`/`ground`/`hand_floor` global and per clip; `attach_prop`: `grip_dir`,
`refit_socket`; `separate`: zone kinds and `slit` cut surfaces). It is edited as data; the code below the tables needs no changes.

21 steps (2026-10-10, evening, after the modules were ready — `docs/forge/bone_kit.md`, `docs/forge/replace_part.md`, the final
`run(req)`):
- `bone_kit` (items module, a generator without inputs, kinds item/prop): `part` = `skeleton_hand | bone_staff | bone_toggle`
  and the parameters of all three parts (hand: `side`, `pose`, `curl`, `finger_spread_deg`, `grip_radius_m`, `grip_tilt_deg`,
  `rings`, `ring_at`; staff: `grip_fraction`, `grip_off_fraction`, `segments`, `shaft_radius_m`, `crystal_*`, `talons`,
  `talon_length_m`, `bend`; toggle: `gold`, `loop`, `bone_radius_m`; common: `style`, `seed`, `length_m` and `origin`
  — each part has its own defaults, so the registry leaves them empty, `palette`, `budget_tris`, `batch`, `sheet_*`, preview).
  Outputs: `glb`, `blend`, `json`, `preview`; for a variant batch — `glb`/`blend` lists, `contact_sheet`, `batch_json`.
- `replace_part` (core module, all kinds): the body comes from the chain (`inputs.mesh`, otherwise `inputs.blend`), **new slot
  `inputs.part`** — the part: `part_asset` (UUID of another forge asset; the server takes its latest GLB, and without versions —
  the uploaded source, so an upload works too) or `part_file` (a path, CLI `--direct` only); both are server-side and
  do not go to Blender. Launch pre-check: without `region` or without a part — 400 (`no_part`), a foreign asset must
  exist. The rest are `run(req)` parameters: `region`, `cut_plane`, `height_m`/`rotate_z_deg`/`ground`/`center`,
  `align` and ICP, `part_tris`, crumbs, `seam_gap`, `hang_m`/`keep_hang`, `texture` (path to a crop — CLI only),
  `eyes`, `name`, preview. Outputs: `glb`, `blend`, `part_glb`, `compare`, `compare_views`.

2026-10-10, night (`docs/forge/kitbash.md`, `docs/forge/ships.md`): **22 steps**.
- `kitbash` (ships module, kinds ship/prop, slots `blend, mesh` — after `ship_from_mesh` it takes its `ship.blend` with LODs and
  metadata; `godot_package` after it receives the `kitbash` GLB): `parts` — a list of parts `{kind: gatling | nozzle |
  piping | antenna | mast | nav_light | vent_grille | block | remove, at, …}`, `drop_sockets`, `budget_class`, `lods`,
  preview, inspector. `STEP_CHECKS.kitbash` checks before launch the part kind, `at` (socket/mask, a point `[x, y, z]` /
  `[x, y]` or a list), the pipe `points` (≥ 2 points), `remove` zones (`cylinder | sphere | box` or
  `{radius, depth, inset, above, cap}`). Outputs: `glb`, `blend`, `preview`, `lod_strip`, `manifest`, `lod1`, `lod2`.
- `ship_from_mesh`: `materials` += `paint`; new `palette` (input | faction), `livery`, `paint` (an object; `image` —
  a path, CLI `--direct` only; over HTTP — `paint.asset`: UUID of a studio image, the server substitutes
  `data/assets/<id>.png|jpg|jpeg|webp` into `paint.image`; if there is no image the job fails before Blender starts),
  `armor_texture` (set name or object), `decals` (a list; `STEP_CHECKS` requires `text` and `position` + `size`),
  `turret_sockets` (flats | protrusions | auto), `lod_targets`, `thruster_cue`.
- `turret_gen`: `barrel_style` (plain | rotary) — the default matches the module (the wizard writes it into the recipe).
- Generation (`server/schema.mjs`, not a forge step): the `hazeFilter` field (auto | on | off) — see "Exhaust haze filter".

2026-10-10, night (character kit v2): **23 steps**. The entries were checked against `run(req)` of the modules
(`place_parts.py`, `replace_part.py`, `rig_humanoid.py` + `_humanoid_lib.skin`, `attach_prop.py`, `retarget.py`,
`separate.py` + `_mesh_lib.SlitSurface`); registry defaults equal module defaults (a test compares the table).
- **`place_parts`** "Part placement" (core module, all kinds): copies of a part on the target surface or in an explicit pose.
  Slots `mesh, blend, part`: the target is the chain model (GLB/FBX/OBJ preferred, otherwise `.blend`), the default part
  is `inputs.part` from `part_asset` (UUID of an asset, the server takes its latest GLB, without versions — the uploaded source) or
  `part_file` (a path, CLI `--direct` only; takes precedence over `part_asset`); both are server-side and do not go to Blender. `points`
  (required): `[x, z]` (front view), `[x, y, z]` or `{at, asset | part, name, scale, roll_deg, snap: surface | none,
  rot_deg | matrix, standoff_m, sockets, rest_percentile, normal_patch, max_tilt_deg}`; `points[].asset` — UUID of the
  point's part, the server substitutes `points[i].part` = its latest GLB (the recipe keeps the UUID); if every point
  has a part, `part_asset` is not needed. The rest are module parameters with its defaults: `target_objects`, `ray` (`+Y` | `-Y` |
  `+X` | `-X` | `+Z` | `-Z`, +Y), `contact` (footprint | center), `normal` (fit | hit | axis), `footprint_grid` ([5, 3]),
  `footprint_fill` (0.8), `rest_percentile` (100, 0–100), `normal_patch` (1), `max_tilt_deg` (45), `up` ([0, 0, 1]),
  `search_m` (empty — from the part), `join` (true), `name`, `part_sockets` (true), normalization `height_m`/`rotate_z_deg`/
  `ground`/`center` (**off by default**: points are in the file's frame), preview 512. `STEP_CHECKS.place_parts`: points
  are non-empty, each is a 2D/3D point or an object with known keys only; `snap none` needs a 3D point; `scale` > 0;
  `matrix` is a rotation (determinant > 0); `asset` is a UUID and not together with `part`; `footprint_grid` ≥ 1; `up` is non-zero.
  Launch pre-check (as for `replace_part`): without a part — 400 `no_part`, the `part_asset` and `points[].asset` assets
  must exist (404 with the point number). Outputs: `glb`, `blend`, `preview`.
- `replace_part`: `align` += `none` (the part stays in its own pose, no fitting to the removed area); `part_transform`
  `{location, rot_deg | matrix, scale}` (extra keys, for example `loc`, are rejected); `keep_materials` (false);
  `cut_part` (true).
- `rig_humanoid`: `grip_dirs` (`{"l"|"r": [x, y, z] | up | down | forward | back | out | in}`), `keep_input_sockets`
  (true), `socket_bones` (`{socket: bone}`), `replace_sockets` ([]), `island_max_frac` (0.01), `surface_aware` (true),
  `surface_margin_h` (0.05), `arm_bleed` ([0.1, 0.35]; `null`/`false` — off, `null` is sent explicitly).
- `attach_prop`: `refit_socket` (false), `grip_dir` (a direction string or `[x, y, z]`, empty — none); both also on an
  item inside `attachments`.
- `retarget`: `facing` (keep | rest), `facing_bone` (empty — Hips), `hips_drop` (1, 0–1), `hips_height_m`, `floor`
  (true), `hand_floor` (`{"l"|"r": dz}`), `ground` (feet | mesh), `ground_tolerance_m` (0), `ground_samples` (2000),
  `floor_tolerance_m` (0.02), `floor_samples` (16). For a clip the module reads `facing`, `facing_bone`, `hips_drop`,
  `hand_floor`, `ground` (the clip overrides the global value); `checkClipList` checks them. Tolerances and samples are global only.
- `separate`: `mode` += `slit` (zones with the default kind slit), `caps` (true), `slit_gap_m` (empty — 0.001 × height).
  `STEP_CHECKS.separate`: `kind` part | delete | slit; a name is required for a part (for delete/slit without a name the module gives
  `delete_<i>`/`slit_<i>`); `caps` — yes/no; `facing` is non-zero, `facing_min` −1…1; `plane` (`{axis, value}` |
  `{point, normal}`), `curve` (`{axis, along, points}` — different axes, ≥ 2 pairs, `along` without repeats), `width_m` and
  `gap_m` ≥ 0 — only for a slit zone (otherwise an error saying the zone must be marked `kind: "slit"`).
- UI (`src/forge/forge-core.js`, `ENUM_LABELS`): localized labels for the values of `ship_gen.wing_style` (none, delta,
  swept, forward-swept, straight, X-shaped, short, crescent), `thruster_frame`, `uv`,
  `turret_gen.barrel_style` rotary, `item_kit.sheet_scale`, all `bone_kit` enumerations, the new enumerations of
  `place_parts`/`retarget`/`replace_part`/`separate`; axes (`+Y`…) and numbers stay as they are. The label table is shared by all
  steps, so `forward` is labelled for wings, while `attach_prop.grip_dir` is a string, not an enumeration.

2026-10-11, night (paint from concept, `docs/forge/paint_from_concept.md`): **25 steps**.
- **`paint_from_concept`** "Paint from concept" (paint module, all kinds, CPU): Hunyuan3D 2.1 produces geometry only — the
  step paints the mesh from the picture it was made from. Slots `blend, mesh, concept`: the model is the chain's `.blend`
  (all objects and sockets stay), otherwise GLB/FBX/OBJ; the **new slot `inputs.concept`** is resolved by
  `resolveInputs` → `_conceptInputs`: `concept_file` (a path, CLI `--direct` only, `path:true`) > `concept_asset` (UUID of a
  studio image, `data/assets/<id>.png|jpg|jpeg|webp`) > the generation reference image the asset was made from
  (`source.type: job`, and for a copy of a forge asset — the job of its source, up to 4 links): `job.preparedConcept.reference`
  (original RGB + rembg alpha, `data/assets/<reference.id>.png`). Together with it goes `inputs.concept_original =
  {image, crop, content_rect}` — the original upload (`reference.originalId` / `job.imageId`) and the framing
  `reference.framing`: the step takes straight RGB from there (in the reference PNG the color is multiplied by alpha, a weak alpha loses color).
  A job without a reference image — its source (the step cuts out the background by the edge). Nothing at all — 400 `no_concept` (for the first
  step — at pre-check, before Blender). `concept_asset` and `concept_file` are server-side and do not go to Blender.
  Module parameters: `size` (512 | 1024 | 2048 | 4096, 2048), `uv` (auto | keep | smart), `uv_pack` (aabb | concave | none),
  `uv_angle_deg`, `uv_margin`, `camera` ("fit" or `{yaw_deg, pitch_deg, px_per_m > 0, offset_px}` from `paint_camera.json`;
  `STEP_CHECKS.paint_from_concept`), `yaw_range_deg` (empty — 60, 180 for creatures), `yaw_step_deg`, `pitch_range_deg` (15),
  `perspective` (true), `facing_min`/`facing_full` (0.1 / 0.4; check: full > min), `depth_tolerance`, `edge_fade_px`,
  `delight`, `fill_blur`, `fill_smooth`, `back_fill` (through | none), `head_guard` (empty — yes, except creatures),
  `head_fraction`, `mirror` (auto | x | none), `mirror_tolerance`, `emission` (auto | none), `emission_strength`,
  `roughness`, `metallic`, `objects`, `material_name`, preview. The step reads the asset kind from `request.asset.kind`.
  Outputs: `glb`, `blend`, `basecolor`, `emission`, `compare`, `camera`.

Important: **only explicitly set parameters go to Blender** (by a preset, by the user, by the asset
source). `default` in the registry is a hint for the UI and the basis for "GPU/timeout" decisions; the real defaults
stay in the step modules. This way the registry cannot override a smarter step default
(for example `cleanup.merge_distance = 1e-5 × height`, `name = "Body"`). Parameters unknown to the registry
are passed through as is, with a warning in the job. An explicit `null` for a parameter whose default is `null`
("empty — the module decides") is not sent either: a module that reads `p.get(key, default)` must not get `None`.

The server fills in empty ones itself: `auto:'asset_slug'` — `asset_id` of `godot_package`, `render_sprites`,
`ship_gen`, `turret_gen`, `ship_from_mesh` (snake_case ASCII: `profile.asset_id` of the passport if set, otherwise
a transliteration of the name + 8 characters of the UUID, for example `mask_check_043d11f1`; the ship GLB and the Godot package get one
id); `auto:'animations'` — the clip library folder for `retarget.library` and `clip_library.library` (for
HTTP clients always the server's, even if one is passed). `server:true` (`attach_prop.prop_asset`,
`replace_part`/`place_parts` `part_asset` and `part_file`, `paint_from_concept` `concept_asset` and `concept_file`) does not go to Blender. `path:true` (`import_mixamo_rig.texture_source`,
`clip_library.folder/files`, `replace_part.part_file`, `place_parts.part_file`, `paint_from_concept.concept_file`) — a local path, which only the
CLI `--direct` can set.

`GET /steps` returns `gpu: true | false | 'conditional'` (`'conditional'` — for `cleanup`, the rule is in `gpuWhen`).

**Cross-check with the modules (2026-10-10, integration).** All 19 entries were checked against the real `run(req)` and parameter parsing
of each module in `scripts/blender/steps/*.py` (not only against the documents): names, types, defaults, enumerations,
ranges, input slots, output keys, timeouts. Main points:
- `cleanup`, `separate` — slots `mesh, blend` (a clean GLB is preferred over a `.blend` with service objects;
  after a step that produced only a .blend or FBX, that one is taken), `convert` — `blend, mesh`; `cleanup` gained
  `planar_angle_deg`, `decimate_stage_ratio`, `seam_weight`, `bake_samples/normal/emission/extrusion_m/ray_m`,
  `smart_uv_angle_deg`, `preview(_size)`; `convert` — `name`, `ground/center` (empty — by height/Mixamo),
  `mixamo_*`, `texture_max`; `separate` — normalization before separation, `regions: [{name, min:[x,y,z],
  max:[x,y,z]}]` in meters after normalization (not fractions of height; checked before launch), `names`, `drop_small`,
  `rest_name`, `origin`, `explode`.
- `rig_humanoid` — `height_ref`, `front`, `rigid` (rule objects), `robe_feet`, `prune_limbs`,
  `smooth_iterations`, `objects`, `deform_test`, `intersections`, `min_confidence`, `fix_yaw`, `max_yaw_deg`;
  output keys `markers_json/markers_png/poses_png`. `prepare_mixamo.lite` is **false** by default (as in the module),
  `tris/tex/weld/center/prerigged/rig_params/force_prerigged`. `import_mixamo_rig` — `retex`, `keep_animation`,
  `clip`, `loop`, `sockets`, `max_influences`.
- `retarget` — help for `root_motion` (lock: only walk/run stay in place, the other clips keep the hip travel
  so the feet stay planted; keep — as in the clip; extract — horizontal travel goes to a non-deforming `root` bone),
  `clips[].in_place` = `true | false | 'strict'`, `clips[].locomotion`, `foot_pin`, `loop_blend`,
  `apply_transforms`, `keep_existing_actions`, `preview_cols/tile`; clips are checked before launch.
- `clip_library` — the `list` action, `pattern`, `files` — paths or `{path, name, category, tags, source}`.
- `rig_creature` — `head_dir`/`facing` = `auto|−Y|+Y|−X|+X` or `[x, y]`, `weights` (auto|heat|distance),
  `tail` (auto|yes|no|number of bones), `jaw`, `leg_ratios`, `voxel_res`, `symmetrize`, `snap_facing`, snake segments;
  templates with `octopod`. `anim_creature` — a rig `.blend` only; `speed` — **m/s** for a walk loop or `{clip: m/s}`,
  empty by default (the step decides); the tempo multiplier is `tempo` (the module understands it, 1); `clips` is empty by default
  (all that fit the template); `stride_m/cycle_frames` as a number or `{clip: …}`, gaits, `attack`, `death_side`,
  `hover_m`, `pose_view`, `tile`.
- `item_kit` — preview size `preview_size` (empty — 384, 320 in a package), `size` — item size (crossbow,
  blasters), `forward` (`-Y` item frame | `+Y`), `budget_tris`, `sheet_cols/tile/scale/title`;
  in a package `glb`/`blend` are lists (the engine takes the first). `item_from_mesh` — `kind`, `type`, `length_m`, `crop`,
  `tip_end`, `edge` (auto or `[x,y,z]`), `allow_over_budget`, `budget_tris`, `min_island`, `texture_max`.
- `attach_prop` — `attachments`, `fit`, `standoff` (m or auto), `thumb_hint`, `item_scale` (number or auto),
  `replace`, `refit_shield_mount`, `socket_along`, `frames_per_clip`, `preview_clip`; the character may be an FBX;
  `clips` is a server-side parameter (see "Input resolution").
- `ship_gen` — `layout`, `thruster_frame` (thrust | exhaust, thrust by default), `engine_count` 1–6,
  `wing_style` as an enumeration (auto), `palette_variant`, `tier`, `lods`, `preview_turrets`, `uv`;
  `style` = faction (the module reads `faction`, `style` is its synonym; there is no separate `faction` in the registry, otherwise
  the wizard, which writes out generator defaults, would override the choice). `turret_gen` — `ring_m`, `clips`, `lods`.
  `ship_from_mesh` — `inputs.mesh` only; `up`, `materials`, `emissive`, `target_tris`, `thruster_frame`,
  `source/generator/faction`, `uv`, preview; output `lod_strip`.
- `render_sprites` — `forward`, `height_m`, `in_place`, `directions`, `cell/anchor`, `shadow`, `grade`,
  `max_cell_px/min_body_px/max_sheet_px`, `event_frames`, `validate_godot` …; `pixels_per_meter`, `elevation_deg`,
  `fps` are empty by default (the profile decides: iso8 30°). `godot_package` — `source_forward`,
  `fx_auto_scale`, `autoplay`, `validate`, `fx_preview*`.
- Generator defaults (`item_kit`, `ship_gen`, `turret_gen`) match the modules: the wizard writes them into the
  recipe explicitly (`generatorParams` in `src/forge/forge-core.js`), a test compares them.

Presets (`GET /presets`): `humanoid_from_mesh` (retarget `root_motion: 'extract'` — the step is in place, the travel of standing
Mixamo clips, attacks and deaths that end about 1 m away goes to the `root` bone for Godot root motion),
`humanoid_sprites` (`root_motion: 'lock'`, plus sprites; **`render_sprites` runs before `godot_package`**,
so that the package takes `inputs.sprites` and puts the sheets into `sprites/`), `creature_auto` (sets no speed or tempo; the size
comes from the passport, see below), `item_kit_sword`, `item_from_mesh`, `ship_procedural` and `ship_from_mesh` (an `engine_trail` plume on the mask
`socket_thruster_*`), `turret`. All preset parameters exist in the registry (a test). Export profiles:
`godot` → `godot_package`, `iso8` → `render_sprites`.

## Pipeline (`server/forge-pipeline.mjs`)

**Asset source** (`POST /assets`): `job` — a copy of the GLB of a finished generation job
(`data/assets/<asset.id>.glb`; the job is looked up in server memory, then in `data/jobs/<id>.json`);
`asset` — the latest GLB of another forge passport or a studio model (`data/assets/<id>` .glb/.obj/.stl/.fbx);
`upload` — GLB/FBX/OBJ by signature (`glTF`, `Kaydara FBX Binary` or `; FBX`, text OBJ), up to 300 MB;
`params` — without a file, `source.params` are mixed into the first generator step (`item_kit`, `ship_gen`,
`turret_gen`, `bone_kit`): on top of the preset parameters but below explicit `steps`.

**Creature size from the passport** (`applyProfileSize`, at launch): if the `rig_creature` step has neither `height_m`
nor `length_m`, the server takes `profile.height_m` (stored by the wizard's "Preparation" step), otherwise `profile.length_m`; for
a creature asset `cleanup` without `height_m` gets `profile.height_m`. The values go into the recipe as explicit parameters,
explicit step parameters take precedence over the passport. If there is no size anywhere, the file's native scale stays, and after `rig_creature`
the server compares the dimensions with the template (`creatureSizeWarning`, `CREATURE_SIZE_RANGES`: quadruped height 0.3–6 m,
winged_quadruped 0.3–12, bird 0.08–5, hexapod/octopod 0.05–6; serpent length 0.3–40; custom — no check) and, if the size
is implausible, writes a warning to the step and to the job. Earlier `creature_auto` left a horse-sized creature at 0.87 m.

**Versions.** Each run is a new `v00N` folder (created atomically with `mkdir`), step `k` writes to
`v00N/<kk>_<step>/`. `from` in `POST /run`: `source` (**default**) — from the source; `latest` — from the final
state of the last *completed* version (if there is none — from the source, with a warning); `v003` — from this
version (must be completed). Export is always `from: latest`.

**Input resolution.** The chain state is `{blend, glb, mesh, exports}`. A step with slots `blend|mesh` gets
`inputs.blend` if a `.blend` exists, otherwise `inputs.mesh` = the current GLB (or the source FBX/OBJ if the format
suits the step). A step that produced `glb`/`blend` replaces the model entirely (the old `.blend` does not survive a new
GLB). **Export steps (`godot_package`, `render_sprites`) do not change the model**: their outputs (including the package GLB)
are recorded in `state.exports[<step>]`, so `render_sprites` after `godot_package` gets the rig or animation `.blend`,
and `version.final` of a version with an export keeps the previous `.blend/.glb` — so the next `from: latest` runs,
`attach_prop` and asset copies take the model, not the packaged GLB. Steps without outputs
(`inspect`) do not change the state.
Special cases: `godot_package` — `inputs.glb` + `inputs.manifest` (passport snapshot) + `inputs.sprites` if `render_sprites`
ran earlier for the same model (its `sprites_json`; a new model step resets the exports);
`render_sprites` — the current `.blend`/model + `inputs.manifest` (the asset kind sets the axis: ships, props,
turrets +Y); `anim_creature` — only the creature's rig `.blend`; an FBX of the chain (the `convert` output) also fits the `mesh` slot.
`attach_prop` — `inputs.character` = the current GLB, otherwise .blend, otherwise FBX (an uploaded rig or the
`convert` output); `inputs.item` = the latest GLB of the asset from `props[0]` (or `params.prop_asset`) if
`params.attachments` is not set. Over HTTP an item in `attachments` is given by `asset` (UUID of the item asset): the server substitutes
`item` = its latest GLB (the recipe keeps the UUID); an `item` path is CLI `--direct` only. `params.clips` is a
server-side parameter: `library:<id>`, `category:<category>[:<tag>]` or `{source, name, loop}` are resolved against the
library as for `retarget` and go to the step in `inputs.clips` as `{file, name, loop}`. `replace_part` and `place_parts` —
the chain model (`inputs.mesh`, otherwise `inputs.blend`) + `inputs.part` from `part_file` (CLI) or `part_asset`; for
`place_parts` `points[].asset` are resolved into `points[i].part` (the asset's latest GLB), and without a default part the step
runs if every point has a part. Generators have no inputs.
The checks "does the first step have an input", the presence of clips for `retarget`/`attach_prop` and of items for `attach_prop`
are done before launch (a 400/404 answer immediately).

**Step errors.** The step's `AFERROR` reaches the job unchanged: `result.error` = "Step '…' failed:
<step text>", `result.af_error` = the step text itself, `result.trace` — the trace. For example, `render_sprites` with wrong
units fails before rendering with `ValueError: projected model is only … px (… fix the units with params.height_m …)`;
the UI shows a frame with this text as is (`humanizeError` does not rewrite text that is already localized).

**Socket masks in fx.** `godot_package.fx[].socket` may be a mask (`*` — any sequence, `?` —
one character): before the step the server reads the JSON block of the input GLB (without Blender), takes sockets by the same rule
as the step (`extras.game_socket` or a `socket_*` name), and replaces the mask entry with entries for each match
(alphabetically). If `params.fx` is not set and the passport's `fx` has masks, they are expanded too. A mask with no
matches is a step error (`fx_socket_missing`) with a list of the model's sockets. The recipe in the passport
keeps the mask, `request.json` gets the expanded list.

**`retarget` clips.** `library:<id>` or `category:<category>[:<tag>]` are resolved against
`data/library/animations/index.json` before the step: of the matching ones the first by `id` is taken (deterministic);
if there is no such category — an error with a hint about importing. The step receives `source:'library:<id>'`, `file`
(absolute path), `name` (the category by default), `loop` (by category idle/walk/run). The passport recipe keeps
the requested `category:…`, and the actual choice is written to `resolvedClips`.

**Local paths.** HTTP clients pass bytes, not paths: `POST /run`, `/export`, `/batch` reject (400,
`no_local_paths`) parameters with `path:true` and `library`, clip sources other than `library:`, `category:` and a bare
clip id, clips with `file`/`path` keys, `attachments` items with `item`/`path`, `place_parts.part` and `place_parts.points[]`
points with a `part` key (even a relative path: over HTTP a point's part is `asset` only), and any
strings that look like a path (`C:\…`, `\\server\…`, `/…`, `file:`, `..`). The last line of defence: `_prepareParams` always sets the server library. CLI `--direct`
(`allowLocalPaths`) may do all of this.

**Passport.** `pipeline[]` = the recipe of the last *completed* version (all entries `done`), followed by the steps
of runs that are still queued/running (`pending`), and of the last failed run after it (`failed`).
Replaying the `done` entries in order rebuilds the last completed version; a failed run no longer
overwrites the recipe and the thumbnail, and the entries of a running run do not vanish when the next one starts.
The contract statuses are `pending|done|failed` (cancel and skip = `failed` with `result.cancelled/skipped`), `result` is
the whole `result.json`, `urls` are links to the outputs. Additionally (outside §8): `versions[]` (version, base, status,
result), `qa.checks` (step checks of the latest runs; `qa.reviewed` changes only through an explicit PATCH),
`exports[]` (`{profile, step, version, path, url, bytes, at}`).

**Jobs.** `data/library/jobs/<id>.json`: job statuses `queued|running|completed|failed|cancelled`, step statuses
`queued|running|completed|failed|cancelled|skipped` with `progress`, `message`, `logTail`, `logFile`,
`request`. Progress is written at most once a second, transitions immediately; a new job is written immediately and **before**
`version.json` and the passport (a version that mentions a job always has its record). The passport and `version.json`
are completed before the job becomes `completed`.
- A failure to write the job file (antivirus, an indexer, OneDrive holding the file; a read-only folder) does not crash
  the studio: the state stays in memory, the error goes to the server log, and the write is retried with a pause
  (0.5 s … 30 s) and on the next change. No code path of a job leaves an unhandled rejection.
- The job owner rewrites `heartbeatAt` every 20 s. Someone else's unfinished job is considered alive
  (`external`: it can only be cancelled where it runs) only if its `pid` is alive **and** the heartbeat is fresher than
  2 minutes; otherwise it is marked `failed` ("interrupted: the process … ended (by a server restart or by closing
  the window)") together with the passport steps and the version — at startup, on `GET /jobs`, `GET /jobs/:id`, on asset deletion.
  A record with our own `pid` that was not created by this process is from an earlier process with the same `pid`.
  This way a reused Windows `pid` (the Start.cmd window was closed without SIGINT) does not leave eternal "external" jobs.
- A `queued/running` version without a job record older than 2 minutes is marked `failed` at startup
  ("The job record of this version was not saved").

**Export.** After a successful `godot_package`/`render_sprites` the `outputs.package` folder (or the whole
`out_dir` without `request.json` and `_views`, or a ready `outputs.zip`) is packed into
`assets/<id>/exports/<profile>-<version>.zip`.

## Routes (in addition to API.md)

- `POST /assets` also accepts multipart: `file` + fields `kind`, `name`, `source` (JSON), `profile` (JSON).
- `POST /assets/:id/run` understands `{preset}` instead of `{steps}`; `from` defaults to `source`; the response is `{jobId, version, warnings}`.
- `POST /batch` also accepts multipart: `items` (JSON) + `files[]`; an item with `source:{type:'upload', fileIndex}`.
  Local paths (`source.path`) work only in CLI `--direct`: the server does not read files by paths from a request.
- `POST /clips`: field `files[]` (or `files`), up to 500 files (as many as the engine accepts); non-FBX files are skipped and
  listed in `rejected`. The files lie in `data/runtime/forge/uploads/<uuid>/` (with their original names — the clip id
  is built from them), the `clip_library` step copies them to `animations/mixamo/`, the folder is deleted when the job ends.
  While an import is running (in this or another process), a second import and `DELETE /clips/:id` get 409 `busy`.
- **Clip library lock.** Besides this check at enqueue time, the `clip_library` step itself runs under a library
  lock: one library job at a time. In-process it is a mutex, between processes (the studio and CLI `--direct` share
  `data/library/animations`) it is the file `animations/.library.lock`, created with `wx` (`{pid, host, token, owner}`);
  the holder refreshes its time every 20 s. The file of a dead process or one not refreshed for 2 minutes (a reused
  pid) is taken over immediately. A job waiting for the lock writes "Waiting for the clip library…" and is cancelled
  without a trace. `DELETE /clips/:id` takes the same lock without waiting (busy — 409). This way two imports (or an import and
  a deletion) no longer overwrite each other's `index.json`, even if both jobs managed to get into the queue.
- `GET /jobs/:jobId/steps/:index/log` — the step log (`?download=1`).
- `GET /assets/:id/files/<v00N|source|exports>/…` — a whitelist of extensions (glb, gltf, fbx, blend, png,
  json, tres, tscn, gd, md, zip, txt, log), `..`/backslashes forbidden, a `realpath` check inside the asset
  folder; `zip, blend, fbx, tres, tscn, gd, log, txt, md` are served with `Content-Disposition: attachment`,
  the rest with `?download=1`.
- Errors: `{error, code}`; codes `bad_id, not_found, bad_params, no_input, clips_missing, bad_signature,
  busy, validation, no_local_paths, fx_socket_missing, bad_glb, bad_json, …`. A broken JSON body in a request to
  `/api/forge/*` (rejected by the shared `express.json` before the forge router) is caught by the error handler
  that `registerForge` installs on `/api/forge`: 400 `bad_json` (a body that is too large — 413
  `too_large`). The global handler of `index.mjs` is untouched for the other routes.

## Command line

```
node scripts/forge.mjs status | steps | presets | assets | show <id> | jobs | job <jobId> [--wait] | cancel <jobId>
node scripts/forge.mjs create --kind humanoid --name "Test character" --from-glb model.glb --preset humanoid_from_mesh
node scripts/forge.mjs create --kind item --params "{\"type\":\"axe\",\"seed\":7}" --preset item_kit_sword
node scripts/forge.mjs run <id> --steps steps.json [--from latest]
node scripts/forge.mjs export <id> --profile godot --out <target folder>   (zip + unpacking)
node scripts/forge.mjs batch items.json        (source.path paths are relative to the batch file)
node scripts/forge.mjs clips | clips import <FBX folder> | clips remove <clipId>
```

Common flags: `--direct` (no server), `--json`, `--server URL`, `--no-wait`. Without `--direct` the CLI talks to the
server with the header `x-astra-client: studio`; progress is polled once a second. `--no-wait` together with
`--direct` is ignored with a warning on stderr: without a server the job lives only in this process, and an early
exit would kill it. Ctrl+C in `--direct` cancels the job (Blender is killed), in server mode it only
stops watching. Exit codes: 0 — success, 1 — job/server error, 2 — invalid arguments.

## Verification

- `node --test tests/*.test.mjs`: **144/144** (2026-10-11, night, paint from concept: +1 registry `paint_from_concept`, +1 pipeline — the concept from the generation reference image with the original and framing, `concept_asset`, `concept_file` CLI only, the `no_concept` pre-check; `forge-routes` expects 25 steps). Before that 142/142 (2026-10-11, night: +1 registry — `deform_gate`, `robe_settle`, `deform_qa`, `adaptive_cell` with module defaults and checks; 2026-10-11: 141 with `motion_style`). Before that 140/140 (2026-10-10, night, character kit v2: +6 registry — `place_parts`, `replace_part`,
  `rig_humanoid`, `attach_prop`, `retarget`, `separate` with module defaults and checks; +1 pipeline — `place_parts`:
  target from the chain, `inputs.part` from `part_asset`, `points[].asset` → GLB, pre-checks, paths only through the CLI; +1 UI —
  localized enumeration labels; `forge-routes` expects 23 steps). Before that 132/132 (2026-10-10, night: +1 registry kitbash / paint / decals / rotary, +1 pipeline kitbash after
  `ship_from_mesh` and `paint.asset`, +6 generation schema `hazeFilter` in `tests/forge-schema.test.mjs`); before that 124/124 (forge server — 61: `forge-steps` 14, `forge-blender` 10,
  `forge-gpu-lock` 4, `forge-pipeline` 27, `forge-routes` 6; UI `forge-ui-core` 29; the rest — the studio);
  2026-10-10, evening, +5: creature size from the passport and a warning about an implausible size (registry and pipeline),
  `bone_kit`/`replace_part` in the registry and `inputs.part` from an asset or file (CLI), `bone_kit` as a generator;
  tests clean up their temporary folders. The integration round added 10: registry cross-check against the modules (core, retarget,
  creatures, items, ships/export, presets), `attach_prop` with FBX/several items/clips, sprites in the
  Godot package, the `AFERROR` text through the real Blender runner (a fake Blender), the clip library lock.
- Smoke run of all presets in real Blender 5.0.1 and Godot 4.5.1: `node scripts/forge-smoke.mjs`
  (`--root C:/afs` — a short working folder because of MAX_PATH, `--only id,id` — a subset of cases).
- `npm run build` — green (only the existing warning about a large chunk).
- Real runs (Blender 5.0.1, Godot 4.5.1):
  - `create --kind ship --preset ship_procedural --direct` in a temporary library: the mask expanded into
    `socket_thruster_main_port` and `socket_thruster_main_starboard`, all `godot_package` checks are green
    (`fx_sockets_exist`, `godot_validation`, `godot_fx_attached`, …); `ship_gen` and the package got one `asset_id`;
    `version.final.glb` — the `ship.glb` of the `ship_gen` step.
    Then `export --profile godot`: `v002` took `ship.glb`/`ship.blend` from `v001`, not the package GLB.
  - A real `node server/index.mjs` (port 3341, ComfyUI not started, temporary library): broken JSON on
    `/api/forge/assets` → 400 `bad_json`; `texture_source: C:/Windows/win.ini` → 400 `no_local_paths`;
    stopped through its own `/api/shutdown`. The temporary library was deleted.

## Limitations and what the integrator should check

- Smoke run of 2026-10-10, evening: all failures of the first run were fixed — LOD2 of `ship_from_mesh`
  (the voxel proxy of the far LOD), the staff of a humanoid after cleanup (`_humanoid_lib._chain_axis`), "Animation not found"
  of the turret (clip renaming in the package), creature size, the `render_sprites` → `godot_package` order, the wizard's
  `in_place`, registration of `bone_kit`/`replace_part`.
- Smoke run of 2026-10-10, morning: all presets except those listed below pass in real
  Blender 5.0.1 and Godot 4.5.1, including `turret` (`pivot_*` nodes are no longer sockets — a fix in the ships/export modules).
  - `ship_from_mesh` on a Meshy-generated ship with the preset class `fighter` (budget ship_s) fails the
    `tris_lod2` check at 2699 against a limit of 1500: the LOD2 target (≈ 1425) is not reached by collapse decimation.
    Suspect: the LOD loop in `scripts/blender/steps/ship_from_mesh.py` (no retry/fitting to the budget) and
    `_ship_lib.decimate_to`. The same chain with `class: gunship` (as in `docs/forge/ships.md`) passes completely,
    including a package with two plumes.
  - A humanoid with a held staff, through the humanoid presets, fails earlier than the expected "gap": after `cleanup` (40,000 tris,
    1.8 m) `rig_humanoid` does not find the staff in the hand, the arms are asymmetric by 0.231 H → the rig is not built
    (`arm_symmetry`, `landmark_confidence` 0.25). The same `rig_humanoid` on the original mesh (120k) finds
    the staff and honestly fails on `deform_torn_area` 11.6 % / arms up 15.6 % (as in `docs/forge/humanoid.md`).
    That is, decimation to 40,000 breaks item recognition in `_humanoid_lib`; to be solved by the humanoid module
    (robustness to a decimated mesh) or by the integrator (the cleanup budget in the humanoid presets).
- With the `humanoid_sprites` preset, sprites rendered with `in_place: 'drift'`: with the new
  `root_motion: 'lock'` a death clip drifts by 1.19 m and does not fit a fixed 320 px sprite cell
  (`cell_fit`, `frames_body_inside_cell` — step errors). With `drift` the straight root drift is removed from all clips,
  and feet slide slightly in death.
- (fixed 2026-10-10, evening) `src/forge/forge-core.js` → `retargetClips` set `in_place: true` on every clip; now
  only on locomotion clips (walk/run, a tag or the `locomotion` flag), standing clips have no key (`root_motion` decides), an explicit
  user choice is preserved (`docs/forge/ui.md`).
- (fixed 2026-10-10, evening) `bone_kit` and `replace_part` in the registry, `replace_part` got the `inputs.part` slot (see "Step registry").
- (fixed 2026-10-10, night) `place_parts` in the registry, `replace_part.align: none` and the new step parameters of the character kit v2
  are no longer passed as "unknown" (see "Step registry", 23 steps).
- `retarget` receives extra keys in every clip (`requested`, `clip_id`, `file`, `category`, `skeleton`) —
  the module ignores them, but keep it in mind.
- Step log: per job `data/runtime/blender/<jobId>/<step>.log`; in contract §9 —
  `data/runtime/blender/<jobId>.log`. One wording is needed.
- In `docs/forge/API.md` (not this module's file) it is worth adding: `from` defaults to `source`; `gpu: 'conditional'` in `/steps`.
- `/status.gpuLock.held` shows only this process; whether the mutex is held by someone else is not
  checked (it would cost ~0.3 s of PowerShell on every poll).
- Outside Windows the GPU lock is a stub with a warning.
- ComfyUI generation waits for the lock for up to 60 minutes (for example while a long render by another tool is running), then the job
  fails with a clear error; each prompt adds ~0.2 s for starting the PowerShell holder.
  LLM chat (Ollama) does not take the lock — that is outside the module's scope.
- Jobs run one at a time (like Blender), except GPU steps, which give up their place in the job queue
  while waiting for the GPU and rendering; a batch of 20 items runs sequentially.
- After sleep/hibernation longer than 2 minutes another process (for example the CLI) may mark a still-living server
  job as interrupted; at its next write the server rewrites its state and the version result itself.
- Progress inside a step exists only where the step prints `AFLOG` with progress.
- A live capture of the GPU lock simultaneously with a real render by another tool and ComfyUI generation was not tested —
  serialization, an abandoned mutex, timeout and cancel were tested on test mutex names.
- `%TEMP%` holds ~800 `af-*` folders from past test runs (before this round the tests did not delete them);
  they can be deleted manually when nobody is running tests.

## Exhaust haze filter (prepare_concept)

Problem: rembg u2net leaves a semi-transparent exhaust haze in the concept's alpha, and Hunyuan3D completes it as long
flared "petals" of the nozzles. The filter in `scripts/prepare_concept.py` (`filter_haze`) removes such haze from the alpha after
background removal (or from the alpha of an already prepared PNG) and before cropping to the bounding size. Alpha only: the opaque
core (alpha ≥ 200) is not changed.

How it decides (all sizes are relative, resolution does not matter):

1. The core is the large 8-connected regions with alpha ≥ 200 (specks < 2 % of the largest are dropped); the soft area is alpha > 5 outside the core.
2. The soft area is opened with a disk of radius 0.6 % of √(core area): the thin antialiased hull edge disappears, wide blobs stay.
3. A blob is haze if all of these hold: area ≥ 0.3 % of the core; the soft region touches the core; the blob center lies in the outer 20 % of
   the core along the main axis (or beyond the core); ≥ 50 % of the blob is outside the convex hull of the core; ≥ 60 % (by alpha weight) of the pixels
   have a haze color (dark/smoky, gray, warm orange-red, bright cold glow).
4. The haze and its thin tails are zeroed; a 2-pixel rim around the core is not touched.

Modes (`<background>[:haze=<auto|on|off>]`, `auto` by default):

| haze | Behavior |
|---|---|
| `auto` | the filter runs only if the core is ≥ 1.25 times wider than tall (ships, vehicles); portrait and square silhouettes (characters) are not touched; does not work with the `keep` background |
| `on` | the filter always runs (including for a vertical ship) |
| `off` | no filter, behavior as before |

How to pass it: the `hazeFilter` field (`auto` | `on` | `off`, `auto` by default) in the body of `POST /api/jobs`. `generationSchema`
(`server/schema.mjs`) appends it to `backgroundMode`: `on`/`off` give `auto:haze=on`, `keep:haze=off` and so on,
`auto` leaves `backgroundMode` as is; `jobs.mjs` and `preprocess.mjs` know nothing about the filter. The job
stores both `hazeFilter` and the folded `backgroundMode` (re-extraction of the mesh takes it as is). From the command
line: `prepare_concept.py <src> <dst> auto:haze=on <reference.png> <metadata.json>`; self-test —
`python prepare_concept.py --selftest` (synthetic ship with haze, character, `off`, ship without haze).

What is recorded: the preparation metadata (`framing` in `preparedConcept.reference`) gained the key
`haze: {mode, applied, removed_px, components, reason}`; `reason` is in English, with the size, the share outside the hull and
the side of the ship where the haze was found (or why the filter did not trigger). A `Haze filter: {...}` line goes to stdout too.

Measured on a ship concept (frame 2000×1116) against a hand-made mask v1 (plumes cut out by hand). A full run of
`prepare_concept.py` with rembg at `haze=off` reproduces the studio's preparation pixel for pixel (`prep_ref.png`):

| Alpha threshold | IoU before filter | IoU after |
|---|---|---|
| > 5 (what the crop sees) | 0.9008 | 0.9708 |
| > 64 | 0.9678 | 0.9885 |
| > 128 (threshold of the hand mask) | 0.9999 | 0.9982 |
| soft IoU (by alpha weight) | 0.9684 | 0.9815 |

63,136 px (8.6 % of the core area) were removed — both hazes to the right of the nozzles; the nozzle bells and all hull geometry are intact
(core pixels did not change). The crop bounding size shrank from 1881×918 to 1806×851. The remaining difference at threshold 5 is the soft
hull edge (it is absent in the hand mask). On a set of 12 character and creature concepts `auto` touches nothing, and `on` found nothing either.
Filter time is about 1.4 s on 2258×2258.

Honest limitations:

- Tuned on one real concept and on synthetic data; no other ships with a plume were at hand.
- Haze that rembg made almost opaque (alpha ≥ 200) is not removed — this is deliberate: such a plume cannot be reliably
  told apart from a smooth dark hull panel.
- A soft glow around the whole hull merged with the haze into one blob leaves together with it; a semi-transparent detail at the end of a
  ship (glass, a fin, a force field) is indistinguishable from haze for the filter — set `off`.
- A vertical ship (nose up/down) is not filtered in `auto` — `on` is needed.
- Soft wisps remain at the underside of the hull (a shadow, not exhaust) and a thin edge; within ≤ 2 px of the core edge the filter does not cut.
- The effect on shape (silhouette IoU 0.88 → 0.97 in v1) was obtained with a hand alpha; a repeat Hunyuan3D run with an automatic
  alpha was not made in this round — the alpha at threshold 128 matches the hand one at 0.998.
