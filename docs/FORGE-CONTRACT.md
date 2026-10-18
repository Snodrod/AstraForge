# AstraForge Forge: module contract (2026-10-10)

This document is the common agreement for all new "forge" modules: the Blender worker, auto-rig, Mixamo, creatures,
items, ships, sprites, Godot export, the asset passport, batch mode, the wizard UI.
It is changed only by the main session. Modules that do not conform to it are not accepted.

## 1. Why

The existing studio (React + three.js + Express + its own ComfyUI on 8190) builds geometry from an image and from
text, but the rig, weights and animation in it are heuristic and live only in the browser. The forge adds a
reproducible server-side pipeline on headless Blender 5.0: cleanup -> rig -> clips (Mixamo and procedural) ->
hand-held items -> effects -> export to Godot and sprites. Everything is local, no cloud.

## 2. File layout

| Path | What |
|---|---|
| `scripts/blender/af_run.py` | the single entry point into Blender: `-- <step> <request.json>` |
| `scripts/blender/af_common.py` | shared helpers: arguments, GLB/FBX import/export, normalization, report |
| `scripts/blender/steps/<step>.py` | one pipeline step = one module with a function `run(req) -> dict` |
| `scripts/blender/tests/` | step self-tests on synthetics and on assets from disk |
| `server/blender.mjs` | Blender launch, queue, timeouts, result parsing, logs |
| `server/forge.mjs` | asset passports, pipeline, clip library, `/api/forge/*` routes |
| `scripts/forge.mjs` | command line: passport build, packages |
| `src/forge/` | the wizard UI (React) |
| `<library>/assets/<id>/` | passport `asset.json` and versions `v001/...` |
| `<library>/animations/` | clip library: `index.json` and the clip files |
| `<library>/kits/` | part kits (bodies, clothing, weapons, ship parts), if any appear |
| `docs/` | this contract, module reports |

`<library>` is the forge library: by default `C:\AstraForge\library` on Windows (short paths: Blender and Godot break
beyond 260 characters), overridable with `ASTRA_FORGE_LIBRARY`; Blender logs and working files go to
`C:\AstraForge\runtime` (`ASTRA_FORGE_RUNTIME`). The library is not committed to git. Modules do not touch project files
outside those listed; `server/index.mjs`, `src/App.jsx` and `package.json` are edited only by the integrator (one wiring
line per module).

## 3. Calling a Blender step

```
"C:/Program Files/Blender Foundation/Blender 5.0/blender.exe" -b --factory-startup \
    --python scripts/blender/af_run.py -- <step> <request.json>
```

The Blender path is the `ASTRA_BLENDER` variable, otherwise the path above. For steps that need a source .blend, the
request contains `inputs.blend`; `af_run.py` opens it itself (`bpy.ops.wm.open_mainfile`).

`request.json`:
```json
{
  "step": "rig_humanoid",
  "inputs": {"mesh": "abs/path/model.glb"},
  "params": {"height_m": 1.8},
  "out_dir": "abs/path/out"
}
```

A step writes `out_dir/result.json` and prints `AFRESULT {json}` (one line) as the last stdout line.
On error - `AFERROR {"step":..., "error":..., "trace":...}` and exit code 1.

`result.json`:
```json
{
  "ok": true,
  "step": "rig_humanoid",
  "outputs": {"glb": "...", "blend": "...", "preview": "...png"},
  "stats": {"tris": 48000, "bones": 65},
  "checks": [{"name": "unweighted_vertices", "ok": true, "value": 0, "limit": 0}],
  "warnings": ["..."]
}
```

All paths in the request and the response are absolute. A step does not write outside `out_dir` (except the OS temp
folder). Keep paths shorter than 260 characters (the Windows MAX_PATH breaks Blender). A step that launches Godot
redirects the child process's `APPDATA`/`LOCALAPPDATA` into `out_dir` (`_export_lib.godot_env`) so that Godot does not
write cache and settings into the user profile. For checking Godot projects, use only Godot 4.5.1 (`ASTRA_GODOT`), not
4.7. Every `.glb` output is accompanied by a `.blend` for rebuilding if the step changes the scene.
`checks` are machine quality checks; `ok:false` in any check with `"severity":"error"` makes the whole step `ok:false`.
Warnings do not fail the step.

## 4. Coordinate system and scale

- Blender: Z up, meters, 1 BU = 1 m. Transforms applied (scale 1, rotation 0), bottom at `z = 0`, center in X/Y at zero.
- **Characters and creatures** face **-Y** in Blender (like Mixamo): glTF with `export_yup=True` gives +Z forward.
- **Ships, turrets, stations, props** use the following convention: nose **+Y** in Blender, nose **-Z** in glTF,
  starboard +X, mirror at `X = 0`, root at the geometric center of the hull.
- An export profile may turn the character to face -Z for Godot (the profile's `forward_axis`);
  the direction is always written to the passport and to the export metadata.
- Default height: a human is 1.8 m; creatures and ships come from the passport parameters.

## 5. Skeletons

**Humanoids use Mixamo names**, so that Mixamo clips apply without renaming:
`mixamorig:Hips, Spine, Spine1, Spine2, Neck, Head, HeadTop_End, LeftShoulder, LeftArm, LeftForeArm,
LeftHand, RightShoulder, RightArm, RightForeArm, RightHand, LeftUpLeg, LeftLeg, LeftFoot, LeftToeBase,
LeftToe_End, RightUpLeg, RightLeg, RightFoot, RightToeBase, RightToe_End` (25 main) plus fingers
`{Left,Right}Hand{Thumb,Index,Middle,Ring,Pinky}{1..4}` (40) - fingers are optional (`params.fingers`).
The rest pose is the one the mesh arrived in (A, T or arms down); clip retargeting accounts for the pose difference (§7).

**Creatures use templates** (`quadruped`, `bird`, `winged_quadruped`, `hexapod`, `octopod`, `serpent`, `custom`).
Names: `root`, `pelvis`, `spine_01..`, `neck_01..`, `head`, `jaw`, `tail_01..`, legs
`leg_<front|mid|mid2|back>_<l|r>_<1..3>` and `paw_<front|mid|mid2|back>_<l|r>`, wings `wing_<l|r>_<1..3>`.
Folded wings are fitted along the flank and marked `folded` in `rig.json` (no flight is built for them).
The full list for each template is in `docs/forge/creatures.md` (written by the creatures module).

## 6. Attachment points (sockets)

One convention for all types: an empty `socket_<kind>_<place>` (ASCII, snake_case, no dots) with the custom property
`game_socket` = the same name (it goes into the glTF extras), plus `kind` and, if needed, `size_class` ("S"/"M"/"L").
Helper: `af_common.add_socket`. The parent is a bone (`parent_type = BONE`) or an object.

| Type | Sockets |
|---|---|
| human | `socket_mount_hand_r`, `socket_mount_hand_l`, `socket_mount_shield_r`, `socket_mount_shield_l`, `socket_mount_back`, `socket_mount_hip_l`, `socket_mount_hip_r`, `socket_fx_head`, `socket_fx_chest` |
| creature | `socket_fx_head`, `socket_fx_mouth`, `socket_mount_back` (saddle), `socket_fx_tail` |
| item | `socket_grip_main` (grip point), `socket_grip_off` (second hand, if any), `socket_fx_tip` |
| ship | kinds: `turret`, `launcher`, `thruster`, `utility`, `cargo`, `docking`, `sensor`, `mount`, `muzzle`, `fx`; turret rig `socket_mount -> pivot_yaw -> pivot_pitch -> socket_muzzle_01...` |

The shield socket `socket_mount_shield_<side>` is a child of the hand bone: the frame of the hand socket x a fitted
rotation and offset, computed once per character from a canonical shield (+Z up, -Y is the front face, the grip is a
handle in the center of the back). Any shield is swapped by aligning its `socket_grip_main` with this socket.

Orientation. The hand socket and the item's `socket_grip_main` share the same frame: local **+Z** runs along the blade
or shaft away from the hand, local **-Y** is the blade edge / front side. Attaching an item = aligning the item's
`socket_grip_main` with `socket_mount_hand_*`. For ships: local +Y is "outward" for a turret, local -Z is forward for a
nozzle and a muzzle. The turret rig nodes `pivot_yaw`/`pivot_pitch` are not sockets: they have extras `axis` and
`range_degrees`, and no `game_socket`.

The default nozzle frame is `thrust` (glTF -Z is the thrust direction, i.e. forward along the ship); the `exhaust`
variant is available through `params.thruster_frame`. Thruster sockets carry `mount_d_m` (nozzle exit diameter, m) -
effects are scaled by it. Blasters are in the item frame by default (muzzle -Y); with the nose along +Y as for props -
`params.forward = '+Y'`.

Effect axis: the local +Y of an effect node in Godot = +Z of the socket in Blender = "outward"; muzzles fire along
Godot -Z (= Blender +Y).

## 7. Clips and Mixamo

Library: `<library>/animations/index.json`:
```json
{"clips": [{"id": "mixamo-sword-idle", "name": "Sword idle", "category": "idle",
  "source": "mixamo", "file": "mixamo/mixamo-sword-idle.fbx", "skeleton": "mixamo",
  "frames": 60, "fps": 30, "root_motion": false, "tags": ["sword"], "imported_from": "..."}]}
```
Categories: `idle, walk, run, attack, cast, hit, death, jump, emote, fly, other`. Procedural creature clips have the
source `procedural:<clip>` (from `anim_creature/clips.json`). The clip identifier sets the folder:
`mixamo-<slug>` lives in `mixamo/`, `fbx-<slug>` in `fbx/`.

In-place modes: `in_place: true` removes the straight-line hip drift from the start to the end of the clip and keeps the
sway (otherwise the feet slide); `in_place: 'strict'` removes all horizontal hip motion. With `root_motion: 'lock'`
only locomotion clips (walk/run or the tag `locomotion`) are played in place; standing clips keep the hip motion so the
feet do not slide. `extract` moves the horizontal motion onto the non-deforming `root` bone for root motion in Godot.
`clips.json` records `foot_skate_cm`, `foot_skate_src_cm` and the thresholds.
Retargeting works as follows: a per-bone rest-pose correction, root x the ratio of hip heights, optional removal of the
root's horizontal drift. Each clip in the output GLB is a separate glTF animation named after the clip; looped clips get
the suffix `-loop` (this is how Godot's import recognizes them). Procedural clips of creatures and ships are stored the
same way.
Uploading a character to Mixamo and downloading from the site is done only by hand by the owner or with their explicit
consent: the forge prepares the FBX for upload and accepts the downloaded files, but does not go online itself.

## 8. Asset passport `asset.json`

```json
{
  "id": "uuid", "name": "Test character", "kind": "humanoid|creature|item|ship|prop",
  "created": "iso", "updated": "iso",
  "source": {"type": "image|text|params|import", "image": "...", "prompt": "...", "params": {}},
  "profile": {"height_m": 1.8, "template": "quadruped"},
  "pipeline": [{"step": "cleanup", "params": {}, "status": "done|failed|pending",
                "result": {}, "version": "v002", "at": "iso"}],
  "clips": [{"id": "idle", "source": "library:mixamo-sword-idle|procedural:walk",
             "loop": true, "in_place": true, "trim": [1, 60]}],
  "props": [{"asset": "uuid", "socket": "socket_mount_hand_r"}],
  "fx": [{"preset": "engine_trail", "socket": "socket_thruster_main_l", "params": {}}],
  "exports": [{"profile": "godot|iso8|custom", "path": "...", "at": "iso"}],
  "qa": {"checks": [], "reviewed": false}
}
```
Rebuild = run the `pipeline` in order with the same parameters. Versions are never overwritten.
`qa.reviewed` is set only by a human.

## 9. Queue and the graphics card

Blender steps go through a single queue `server/blender.mjs` (1 at a time by default,
`ASTRA_BLENDER_CONCURRENCY`). Steps that render on the GPU (`render_sprites`, `preview` in Cycles) take the same lock
as generation in ComfyUI 8190, so as not to share 12 GB with Hunyuan. The default step timeout is 15 minutes, render -
60 minutes. Step log: `data/runtime/blender/<jobId>/<step>.log`.

## 10. Checks and report

Each module delivers:
1. A self-test in `scripts/blender/tests/test_<module>.py`: argument-less `test_*()` functions that fail via `assert`;
   temporary files only in `tempfile.mkdtemp()`. Tests on real assets from disk are named `test_real_*` and skip
   themselves (`return`) if the file is missing. Common run:
   `blender -b --factory-startup --python scripts/blender/tests/run_tests.py -- [module]`;
   from Node - `tests/integration/blender.test.mjs` (skipped if Blender is not found; not part of `npm test`).
2. A run on a real asset from disk with a preview picture and numbers in `docs/forge/<module>.md`.
3. An honest list of limitations. Do not pass off silhouette fitting as anatomy, or a photo projection as PBR.

The fast `npm test` tests (34 existing) must stay green.
