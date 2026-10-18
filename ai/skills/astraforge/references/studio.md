# AstraForge: architecture and use of the studio

Application version 0.3.0, git `master` (`a7d5cfc` on 2026-10-17). Sources: `README.md`, `ARCHITECTURE.md`, `docs/forge/*.md`, `docs/FORGE-CONTRACT.md`,
`server/*.mjs`, `scripts/*.mjs`, `scripts/blender/steps/`, `src/App.jsx`. Some documents lag behind the code (see "Discrepancies" in `troubleshooting.md`);
the numbers here were checked against the code. The UI has English and Russian dictionaries (English by default); server messages may still be partly in Russian.

## 1. Architecture

```
Browser (React 19 + three.js 0.180, Vite)  -->  Express :3210 (127.0.0.1, server/index.mjs)
                                                 |
        +----------------------------------------+-------------------------------------------+
        | generation (server/jobs.mjs, queue <= 4)| Forge (server/forge*.mjs, /api/forge/*)    | VFX tab (server/vfx-tools.mjs)
        |  +- studio ComfyUI :8190 (Hunyuan3D,    |  +- Blender queue (one at a time;          |  +- web generators :3211
        |  |   SDXL, MV-Adapter; ASTRA_COMFY_DIR) |  |   server/blender.mjs)                    |
        |  |                                      |  |   blender -b --python af_run.py -- <step>|
        |  +- ComfyUI-Pixal3D :8191 (shape)       |  +- Godot 4.5.1 headless (package check)    | Ollama :11434 (assistant, qwen3-vl)
        | shared GPU lock, a named Windows mutex (server/gpu-lock.mjs, gpu-handover.mjs; override with ASTRA_GPU_LOCK_NAME)  |
        +--------------------------------------------------------------------------------------------------------------+
Asset passports, versions, clips, packages: <library> (C:\AstraForge\library). Studio data (projects, generations): <APP>\data.
```

- The server listens on `127.0.0.1` only. Protection: Host in {127.0.0.1, localhost}; Origin = its own host; **every non-GET request to `/api` requires the header `x-astra-client: studio`**
  (otherwise 403, the request must come from the studio). The folders `/data /server /scripts /tests` return 404 over HTTP.
- Generation: at most 4 jobs in the queue; LLM and 3D do not run at the same time (one GPU; the response says to wait).
- The user's main ComfyUI on 8188 is **neither used nor reconfigured** by the studio. Its own ComfyUI on 8190 is a separate process from the code/`.venv` of ComfyUI (see `components.md`).
- Start: `Start.cmd` -> `scripts\Start.ps1 [-NoBrowser]` (reads `config\paths.json`, hides the node window, waits for `/api/health` up to 20 s, opens the browser).
  If 3210 already answers `{"app":"AstraForge"}`, it starts nothing; a foreign application on the port is an error. On startup the server itself brings up ComfyUI 8190 if
  `data\models\checkpoints` contains `hunyuan3d-dit-v2.safetensors`. Pixal3D starts **on demand** (the first job, ~ 6 s on a warm machine, 1-2 min cold)
  or via `POST /api/runtime/pixal3d/start`; an instance already running by hand is used and not stopped by the studio.
- Stop: `Stop.cmd`. Logs: `data\runtime\server.log`, `server-error.log`, `comfy.log`, `pixal3d.log`; Blender logs - `<runtime>\blender\<jobId>\<step>.log`.
- Viewing results: tab -> "Show in scene" on any result with a GLB.

## 2. UI tabs (left column; 10 tabs in `src/App.jsx`)

| Tab | What it does |
|---|---|
| **Create** | A concept (PNG/JPG/WEBP: one object on a neutral background) or a description (SDXL -> picture). Category: creature / item / ship. **"Views"**: from the front view (a character in a T-pose) creates left/back/right - the source is "ChatGPT" (via Codex, better with details) or "Local" (MV-Adapter), or your own files in any slot. **"3D engine"**: Pixal3D (more accurate, ~ 4 min, ~ 10 GB VRAM; the default if installed) or Hunyuan3D (faster). "Geometry quality" (Hunyuan only): draft/standard/high, shape Hunyuan 2.0/2.1, surface level, seed, background auto/alpha/keep. Buttons **"Create 3D model"** (from one image) and **"Create 3D from views"** (2-4 views). The result is a GLB in the scene and in "Results" |
| **Forge** | A Blender-based wizard: *What are we making -> Prepare -> Skeleton -> Motions -> Items -> Effects -> Export*; "Jobs" (all builds, progress, log, cancel), "Package" (series: seeds, GLB folders), "Review" (concept next to the animation: Approve / Rework / Reject, a "game unit" field, export of decisions). Details - `docs/forge/ui.md`, `REVIEW.md` |
| **Bones** | The old in-browser editor: Humanoid/Quadruped/Mechanical/Custom templates, distance binding, weight brush, Idle/Walk/Run clips (procedural blanks), GLB export with a skeleton. Not an auto-rig (for game characters use the Forge) |
| **Textures** | UV atlas (xatlas, on CPU), procedural PBR maps 512-4096 (metal, stone, wood, cloth), a brush on Base Color, "Bake visible color" from a reference, "Compare with reference" (silhouette IoU, camera fitting) |
| **Animation** | Rotation/hover/flight, keyframes, duration 1-60 s; WebM recording at 30 fps; scene export |
| **Effects** | Particle emitters on a model/bone (12 Forge presets + "Empty"), live preview, a texture generator (10 types, 20 recipes, flipbooks), "ZIP for Godot 4.5". -> `astraforge-vfx` skill |
| **VFX** | A catalog of 31 external texture generators, launch/install/folder, ready PNGs are applied to the object. -> `astraforge-vfx` skill |
| **Sprites** | Isometric sprites in 8 directions (35 degrees above the horizon, transparent background), a ZIP of "PNG + Godot SpriteFrames `.tres`", "Bake all clips into GLB". (Profiles and quality - the Forge `render_sprites` step) |
| **AI** | An assistant: local Ollama (the model the installer picked by VRAM; default `qwen3:8b`), OpenAI or Anthropic via an API key; it proposes safe changes (material, transform, motion, effect) applied with a button; arbitrary code and OS commands are not executed |
| **Tripo** | Cloud Tripo (15 operations via the official SDK); paid tasks **must not be run without the user's decision** |

Bottom left: "Import 3D" (GLB, OBJ, STL; GLB with embedded textures only), "Results". Top: "Projects" (`.astra.json`/gzip, up to 512 MB unpacked),
export of GLB/OBJ/WebM/PNG/project (a copy of every export goes to `data\exports`). Hotkeys: W/E/R, F, Space, Delete, Ctrl+D/S/Z, Ctrl+Shift+Z.
An unsaved scene is not restored after a page reload.

## 3. The Forge: passport, versions, library

- **Asset passport** `<library>\assets\<uuid>\asset.json`: `id, name, kind (humanoid|creature|item|ship|prop), created, updated, source, profile ({height_m,...}), pipeline[], clips[], props[], fx[], exports[], qa, versions[]`.
  `source.type`: `job` (a studio generation), `asset` (another passport/studio model), `upload` (GLB/FBX/OBJ up to 300 MB, by signature), `params` (generators without a file).
- **Versions** `v001, v002, ...` are a run recipe and are **never overwritten**; step k writes into `v00N\<kk>_<step>\` (`request.json`, `result.json`, outputs). `from`: `source` (default), `latest`, `v003`.
  Repeating a recipe gives the same geometry. Export always starts from the latest completed version. The export steps (`godot_package`, `render_sprites`) do not change the model.
- **Step result** (`AFRESULT {json}` / `result.json`): `ok, outputs{glb,blend,preview,...}, stats, checks[{name,ok,value,limit,severity}], warnings`. A check with `severity:error` and `ok:false` fails the step;
  warnings do not. Paths are absolute.
- **Trash**: deleting = moving to `<library>\trash\`. `qa.reviewed` is set only by a human (the "Review" tab).
- Library layout: `assets\`, `jobs\<jobId>.json`, `batches\`, `trash\`, `animations\{index.json,mixamo\,quaternius\}`, `kits\`, `props\`, `rigs\`.
- Conventions: Z up, meters; characters and creatures face **-Y** in Blender (like Mixamo; +Z in glTF); ships/turrets/props - nose **+Y** (-Z in glTF).
  Human bones use Mixamo names (`mixamorig:*`, 25 main + fingers). Sockets `socket_<kind>_<place>` + `extras.game_socket`: for a human `socket_mount_hand_r|l`,
  `socket_mount_shield_r|l`, `socket_mount_back`, `socket_mount_hip_*`, `socket_fx_head`, `socket_fx_chest`; for an item `socket_grip_main|off`, `socket_fx_tip`.
- Queue: Blender one at a time (`ASTRA_BLENDER_CONCURRENCY`), step timeout 15 min (cleanup 30, render 60). GPU steps (`render_sprites`; `cleanup` with `voxel_remesh_m>0` and `bake_device=GPU`) take the GPU lock.

## 4. CLI `node scripts/forge.mjs ...` (from the APP folder)

```
status                                  Blender, Godot, library, queue, GPU lock
steps | presets                         the step registry (with parameters) and ready-made pipelines
assets | show <id>                      passports | one passport (versions, pipeline, exports)
create --kind humanoid|creature|item|ship|prop --name <name>
       source: --from-glb <GLB|FBX|OBJ> | --from-job <jobId> | --from-asset <id> | --params <json|file>
       recipe: [--preset <id> | --steps <file.json>]
run <assetId> (--steps file.json | --preset id) [--from source|latest|v003]
export <assetId> --profile godot|iso8|custom [--params <json|file>] [--out <folder>]
batch <items.json>                      {"items":[{name, kind, source, preset|steps}]}
clips [list] | clips import <FBX folder> | clips remove <clipId>
jobs | job <jobId> [--wait] | cancel <jobId>
common: --direct (no server, in this process) --json --server <URL> --no-wait
```
`--direct` reads paths only from `ASTRA_*` variables (not from `paths.json`!). The `--steps` file is an array `[{"step":"cleanup","params":{...}}, ...]`; only the given parameters are passed to Blender.
Other scripts: `scripts/forge-smoke.mjs`,
`scripts/views/views_generate.mjs` (views), `scripts/views/views_3d.mjs` (3D from a set of views: `--engine pixal3d|hunyuan|auto`), `scripts/parts/build_parts.py` (by parts),
`scripts/vfx/Install-VfxTools.ps1`.

## 5. Pipeline steps (28; `server/forge-steps.mjs`, `GET /api/forge/steps`)

| Step (module) | Purpose | Kinds |
|---|---|---|
| `inspect` (core) | import and statistics: triangles, dimensions, skeleton, clips; a contact sheet of views | all |
| `cleanup` | scale/floor/weld/debris/decimation to `target_tris`; `voxel_remesh_m` - voxel remesh with color baking (needed for torn Pixal3D meshes, harmful on intact ones); GPU conditionally | all |
| `convert` | GLB <-> FBX <-> OBJ; `mixamo:true` - an FBX for uploading to mixamo.com | all |
| `separate` | split a mesh into parts / delete / `slit` cut (a sleeve from a robe) | all |
| `replace_part` | replace a region (skull, hand, pommel) with a part generated from an enlarged crop | all |
| `place_parts` | place copies of a part over the surface/poses (clasps, buttons, gems) | all |
| `paint_from_concept` (paint) | color for bare geometry from a concept picture (camera from the silhouette, back "through", a glow map); `concept_asset`/`concept_file` | all |
| `assemble_parts` (parts) | head/torso/arms/legs are generated separately (`scripts/parts`) and fused into one mesh | humanoid |
| `extract_props` (items) | cut a sword/shield/banner/bow/staff/book out of a generated body into separate GLBs with sockets | humanoid |
| `rig_humanoid` (humanoid) | auto-rig with Mixamo bones, markers, weights, sockets, proportion and tear checks (`deform_gate`), `hard_surface` (rigid plates), `markers` for manual correction | humanoid |
| `prepare_mixamo` / `import_mixamo_rig` | an FBX for manual upload to mixamo.com / accept the downloaded rig ("With Skin") | humanoid |
| `retarget` (mixamo) | library clips onto the character's skeleton (leg IK, loop seams); `root_motion lock/keep/extract`; `clips:[{source:"library:<id>",name,loop,in_place}]` | humanoid |
| `motion_style` | a custom walk and a procedural death for a character (`seed`, `death`: library/auto/fall_back/fall_forward/fall_left/fall_right/kneel_collapse/spin_fall/slump_sit/crumble; `build`: heavy/armored/robed/light/skeleton/zombie), measuring mesh tears on real clips | humanoid |
| `cloth_sim` | cloth physics for robes/cloaks/banners over the clips (a cache for sprites, cloth bones for GLB) | humanoid |
| `clip_library` | scan/import/delete/preview of clips (one job at a time) | - |
| `rig_creature` / `anim_creature` (creatures) | a rig from a template (quadruped, winged_quadruped, bird, hexapod, octopod, serpent, custom, auto) / procedural idle-walk-run-flight-attack-hit-death clips | creature |
| `item_kit` (items) | procedural weapons/shields/staves/potions/blasters (22 kinds: sword, greatsword, dagger, axe, battle_axe, mace, warhammer, spear, halberd, staff, wand, bow, crossbow, shield_round, shield_kite, shield_tower, buckler, torch, potion, book, blaster_pistol, blaster_rifle), `seed`, a variant pack | item/prop |
| `item_from_mesh` | a generated mesh -> a game item (axis, grip, budget) | item/prop |
| `attach_prop` | an item into a hand / onto a shield socket; `extracted:true` - put back the extracted items; collision check over the clips | humanoid/creature |
| `bone_kit` | undead parts without generation: a skeletal hand (27 bones), a bone staff, a clasp | item/prop |
| `ship_gen` (ships) | a procedural ship: 9 classes (fighter, interceptor, gunship, corvette, freighter, miner, frigate, carrier, transport), 4 styles (industrial, military, sleek, alien), LOD, sockets | ship |
| `turret_gen` | a turret `socket_mount -> pivot_yaw -> pivot_pitch -> socket_muzzle_*` | ship/prop |
| `ship_from_mesh` | a generated mesh -> a ship: nose +Y, class budget, `AF_*` materials, sockets, LOD, armor, markings | ship |
| `kitbash` | gatling guns, nozzles, pipes, antennas, lights on the sockets of a finished model | ship/prop |
| `render_sprites` (export) | sprite sheets per direction and clip; profiles `iso8`, `custom`; GPU | all |
| `godot_package` | GLB + `.tscn` + `af_fx_mount.gd` + `fx/*.tscn` + README + a registry entry; checked with a real Godot 4.5.1 | all |

Details are in `docs/forge/<module>.md` (core, humanoid, mixamo, motion_style, cloth_sim, extract_props, paint_from_concept, creatures, items, bone_kit, ships, kitbash, replace_part, parts, export).

### Ready-made pipelines (`presets`)
`humanoid_from_mesh` (cleanup -> rig_humanoid -> retarget -> godot_package), `humanoid_sprites` (... -> render_sprites -> godot_package), `creature_auto`, `item_kit_sword`, `item_from_mesh`,
`ship_procedural`, `ship_from_mesh`, `turret`. An extended fighter chain: cleanup -> paint_from_concept -> extract_props -> rig_humanoid -> retarget -> attach_prop (extracted) ->
motion_style -> cloth_sim -> render_sprites -> godot_package.

An example `steps.json` for a humanoid from a Pixal3D/Hunyuan GLB (check the parameters against `forge.mjs steps`):
```json
[{"step":"cleanup","params":{"target_tris":24000,"height_m":1.8}},
 {"step":"paint_from_concept","params":{}},
 {"step":"rig_humanoid","params":{"height_m":1.8}},
 {"step":"retarget","params":{"root_motion":"lock","clips":[
   {"source":"library:quaternius-idle-loop","name":"idle","loop":true},
   {"source":"library:quaternius-walk-loop","name":"walk","loop":true,"in_place":true},
   {"source":"library:quaternius-sword-attack","name":"attack","in_place":true},
   {"source":"library:quaternius-hit-chest","name":"hit"},
   {"source":"library:quaternius-death01","name":"death","in_place":true}]}},
 {"step":"render_sprites","params":{"profile":"iso8","in_place":"drift"}},
 {"step":"godot_package","params":{"profile":"godot"}}]
```

## 6. 3D generation (the "Create" tab / `POST /api/jobs`)

| Engine | Input | Time (RTX 4070) | VRAM | Notes |
|---|---|---|---|---|
| **Pixal3D** (primary since 2026-10-14) | 1-4 views (the front view is mandatory) | 4 views ~ 232-243 s; 1 view ~ 262-355 s | ~ 9.5-10.8 GB (1 view 6.4) | shape only (textures - `paint_from_concept`); fixed parameters: heun, 25 coarse-shape steps, 12 each at 512/1024, ~ 700k faces, FOV 0.1 rad and azimuths 0/90/180/270 for multiple views; "re-extract surface" is unavailable; silhouette IoU with 4 views 0.971/0.937/0.940/0.938 |
| Hunyuan3D-2 / 2mv / 2.1 | an image; 2mv - 2-4 views | 2mv `high`: 2 views ~ 155 s, 4 views ~ 176-180 s | ~ 6.7 GB in memory after the job (measured at GPU handover) | `quality` draft (15 steps, 128) / standard (25, 256) / high (45, 512); `meshLevel` -1...1; seed; its own Marching Cubes (`AstraForgeMesh`) |
| Views: ChatGPT / MV-Adapter | the front view | ~ 57-65 s per view / ~ 34 s for all | - / ~ 9.7 GB at 640 px | MV-Adapter invents the back, ChatGPT is more accurate; for final-quality models use ChatGPT or your own views |

`POST /api/jobs` body: `{imageId?, prompt?, quality, shapeModel: "default"|"hunyuan21", meshLevel, backgroundMode: "auto"|"alpha"|"keep", hazeFilter: "auto"|"on"|"off", seed, viewSetId?, engine: "hunyuan"|"pixal3d"|"auto"}`
(default `hunyuan`; `auto` = Pixal3D if installed). The two engines never hold the GPU at the same time: before a job the studio waits for the other to finish and asks it to unload its models.
Output normalization: center at zero, height 2, front -Y after import into Blender.

## 7. API (all under `http://127.0.0.1:3210`)

| Group | Routes |
|---|---|
| Studio | `GET /api/health`, `GET /api/status` (ollama, comfy, gpu, pixal3d), `GET/PUT /api/config`, `PUT /api/keys/:provider`, `POST /api/runtime/start`, `POST /api/runtime/pixal3d/start`, `POST /api/shutdown` |
| Files | `POST /api/assets` (multipart `file`: PNG/JPG/WEBP/GLB/OBJ/STL <= 100 MB), `GET /api/assets/:id`, `POST /api/exports`, `GET /api/exports/:id` |
| Generation | `GET/POST /api/jobs`, `POST /api/jobs/:id/cancel, /validate, /reextract` |
| Views | `GET /api/views/status`, `POST /api/views {imageId}`, `GET /api/views/:id`, `GET .../image/:slot`, `PUT/DELETE /api/views/:id/:slot`, `POST .../generate {provider:"codex" or "mvadapter",slots?,overwrite?,seed?,size?,steps?,note?}`, `POST .../cancel` |
| Forge `/api/forge` | `status, steps, presets, assets(+/:id, PATCH, DELETE, /:id/run, /:id/export, /:id/files/...), jobs(+/:id, /:id/cancel), clips(+/:id/poses), batch(+/:id), review, review/export` |
| Projects | `GET/POST /api/projects`, `POST /api/projects/compressed`, `GET /api/projects/:id`, `POST /api/unwrap`, `POST /api/optimize`, `POST /api/reference/prepare`, `POST /api/chat` |
| VFX | `GET /api/vfx-tools`, `.../textures`, `POST /api/vfx-tools/:id/launch, /install, /folder` |
| Tripo | `POST /api/tripo/tasks/:id/<action>` |

Errors are JSON `{error: "text", code}`: `not_found, bad_id, bad_path, busy, validation, no_local_paths, fx_socket_missing, bad_glb, bad_json, bad_state, pixal3d_not_installed, no_part, no_concept`.
A local path cannot be passed in parameters over HTTP (`no_local_paths`): files are uploaded; paths (`part_file`, `concept_file`, `parts_file`) work only with `--direct`.

## 8. The `<APP>\data` layout (not part of git)

`assets\` (images, studio GLBs + `.json`), `jobs\`, `projects\` (gzip JSON), `exports\`, `views\<id>\` (view sets: `raw\ norm\ work\ views.json`), `tripo\`, `python\` (xatlas),
`models\{checkpoints,background,mvadapter}`, `runtime\` (`input output temp user`, `custom_nodes\`, `models.yaml`, `studio.db`, `comfy.log`, `pixal3d.log`, `*.pid`, `pixal3d-temp\`),
`config.json` (connections), `vault.json` (API keys via Windows DPAPI - **a secret, do not publish, do not copy to another PC**), `library.moved-2026-10-11\` (an old copy).

## 9. Quality checks and tests

- Every step writes `checks` (for example `tris_within_target`, `height_matches`, `deform_torn_area`, `foot_skate`, `cell_fit`, `body_scale_px_per_m`, `godot_scene_loads`, `godot_animation_loops`, `godot_engine_errors`).
  A `severity:error` failure stops the step with "Quality checks failed: ...".
- The Godot check is windowless (loading, names, loops, sockets, FX); it does not judge the picture.
- `npm test` is `node --test tests/*.test.mjs` (23 files in `tests/`); Blender tests - `scripts/blender/tests/run_tests.py`.
- External gates that are not part of the studio (scripts on a given machine): posture `posture.py`, `vlm_check.py`.
