# The AstraForge forge: building assets automatically and embedding them in games

State as of 2026-10-11. Everything runs locally: Blender 5.0.1 without a window, Godot 4.5.1 for checks,
Hunyuan3D 2.1 in its own ComfyUI on port 8190. No cloud is used.

## 1. Launching

- Double-click `Start.cmd`, address http://127.0.0.1:3210. Stop with `Stop.cmd`.
- In the studio, the **"Forge"** tab holds the wizard (what we create -> preparation -> skeleton -> motions -> items ->
  effects -> export), "Jobs" (all builds, with a "Show in scene" button), and "Batch".
- Command line from the AstraForge folder: `node scripts/forge.mjs presets`, `create`, `run`, `batch`,
  `export`, `clips import`. The `--direct` flag works without a running studio.

## 2. Ready-made pipelines (presets)

| Preset | What it does |
|---|---|
| `humanoid_from_mesh` | cleanup -> Mixamo auto-rig -> clips from the library (root motion) -> Godot package |
| `creature_auto` | cleanup -> creature rig (template detected automatically) -> procedural gaits -> Godot |
| `item_kit_sword`, `item_from_mesh` | a weapon from the kit or from a generated model -> Godot |
| `ship_procedural`, `ship_from_mesh`, `turret` | ships and turrets -> package |

Example: a character from a picture in one command.
```
node scripts/forge.mjs create --kind humanoid --name "Knight" --from-glb model.glb --preset humanoid_from_mesh
```
The model comes from a studio generation (`--from-job <id>`), from a GLB/FBX/OBJ file, or from another asset.

## 3. Characters

1. **Shape.** Picture or text -> Hunyuan 2.1 generation in the studio. For important details (skull, hand,
   pommel) run a separate generation from an enlarged crop and use `replace_part`. Hunyuan 2.1 produces geometry only:
   color comes from `paint_from_concept` applied to the generation's source picture (see `docs/forge/paint_from_concept.md`).
2. **Details that beat the neural net.** `bone_kit` (skeletal hands, a bone staff, clasps), `item_kit`
   (22 kinds of weapons, shields and props), `place_parts` (placing details over a surface or by poses),
   `separate` (cutting merged props apart, `slit` cuts between a sleeve and a robe).
3. **Items separately.** `extract_props` cuts a sword, shield, banner, bow, staff or book out of the generated body
   (the fist stays with the body, caps are added at the cuts, paint is kept on both parts); each item becomes its own GLB with
   grip sockets. An item that fails the quality check is replaced with a kit item of the same size in its colors
   (`docs/forge/extract_props.md`). Without this the sword bends with the hand, the shield crumples, the plate armor stretches.
4. **Rig.** `rig_humanoid`: Mixamo bone names, automatic markers (chin, groin, wrists,
   elbows, knees) that can be corrected in the wizard, proportion and break checks. Sockets for hands, shields, back, head.
   Hard plate armor (`hard_surface`): metal plates identified by paint are bound whole to their own bone.
5. **Item in hand.** `attach_prop`: aligns the item's `socket_grip_main` with the hand socket; shields go
   through `socket_mount_shield_*`. Items are swapped through sockets. With `extracted: true` all items cut out
   in this run go back to their places (`skin_separate` is a rigid separate mesh on the hand); a cut-out shield
   on a character with clips goes to the shield socket, fitted to the clip frames (it stands, facing outward and forward).
6. **Motions.** `retarget` transfers Mixamo clips to any skeleton: in place for walking, root motion
   for Godot, protection against sinking and twisting for characters in robes; `motion_style` provides a custom manner and death.

Mixamo: download clips from mixamo.com (FBX, "Without Skin", 30 fps) and drag them into the
"Motions" tab or run `node scripts/forge.mjs clips import <folder>`. The library holds 124 clips (38 Mixamo and 86 Quaternius/ActionForge, CC0);
it lacks running, jumps, strafes and emotes. Uploading a character to the Mixamo site is prepared by the
`prepare_mixamo` step; the forge itself does not go online.

## 4. Creatures

`rig_creature` with templates `quadruped`, `winged_quadruped`, `bird`, `hexapod`, `octopod`, `serpent`
or `auto`; `anim_creature` builds idle, step, run, flight, gliding, attack, hit and death with
foot placement. Height is set in the passport (`profile.height_m`), otherwise the file size stays.

## 5. Items and ships

- **Weapons and props:** `item_kit` (parameters, style, seed, variant pack) or `item_from_mesh`.
- **Procedural ship:** `ship_gen` (9 classes, 4 styles, layout families, seed).
- **Ship from a concept:** Hunyuan generation -> `ship_from_mesh` (paint from the concept, armor,
  markings, sockets, LOD, collision) -> `kitbash` (machine guns, nozzles, pipes, masts, lights on sockets).
- **Turret:** `turret_gen` with the rig `socket_mount -> pivot_yaw -> pivot_pitch -> socket_muzzle_NN`.

## 6. Batch mode

`node scripts/forge.mjs batch items.json`, where `items.json` = `{"items":[{name, kind, source, preset|steps}]}`;
in the wizard use the "Batch" tab (for example, 20 ships with seeds 1-20). Each asset gets an
`asset.json` passport with versions; re-running a recipe yields the same geometry.

## 7. Embedding in projects

| Project | Export profile | Result | How to embed |
|---|---|---|---|
| Any Godot 4 | `godot` | an `<asset_id>/` folder: GLB, `.tscn`, an effects mounting script, `fx/*.tscn` | copy the folder into `res://` and instantiate the scene |

The forge never writes into other projects by itself: packages and registry entries are placed in
`C:\AstraForge\library\assets\<id>\exports\`. All packages have been checked with a real Godot 4.5.1.

## 8. Limitations

- Neural-net geometry is soft: close-ups need parametric details on top of it.
- Color from a picture is a photo projection, not PBR; the hidden side is a guess by the generator or a mirror.
- Positions of details and cuts on non-standard models are still tuned from measurements.
- The forge library lives in `C:\AstraForge\library` (a short path). Another folder can be set with the environment
  variable `ASTRA_FORGE_LIBRARY`.
- Tripo is connected, but paid operations are not run without the owner's decision.

Details per module are in `docs/forge/*.md`, the contract is in `docs/FORGE-CONTRACT.md`, the API is in `docs/forge/API.md`.
