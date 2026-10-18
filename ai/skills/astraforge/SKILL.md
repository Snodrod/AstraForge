---
name: astraforge
description: AstraForge ("Астрофорж", "кузница ассетов"), a local 3D game-asset studio - one workshop for game assets, subordinate to no particular game. Image or text -> 3D model (Pixal3D, Hunyuan3D) -> views -> rig (Mixamo bones) -> clips (Quaternius CC0, Mixamo) -> held items -> sprites and packages for Godot 4.5. Use it when asked to generate or assemble a 3D model, unit, character, creature, ship, turret or weapon; to rig, animate, make sprites for a game or assets for Godot; to build views (back/left/right) from a single picture; to start, check or repair the studio, Pixal3D, Hunyuan, MV-Adapter or the Blender pipeline "Кузница" (the Forge); or to install the studio from scratch or move it to another computer. Triggers - «3D-модель», «AstraForge», «Астрофорж», «кузница», «сгенерируй модель/юнита/корабль/персонажа», «риг», «анимации», «спрайты для игры», «ассеты для Godot», «Pixal3D», «Hunyuan», «ракурсы», «установи студию», «поставь AstraForge с нуля», «перенеси на другой компьютер». Effects, particles and VFX textures belong to another skill, astraforge-vfx.
---

# AstraForge: a local 3D studio

A web studio on this computer (Windows, NVIDIA GPU): **concept (image/text) -> 3D -> rig -> clips -> items -> effects -> Godot package / sprites**. Everything is local and free:
React + three.js -> Express `127.0.0.1:3210` -> queue -> ComfyUI (Hunyuan3D, SDXL, MV-Adapter on :8190; Pixal3D on :8191) and windowless Blender 5.0.1; packages are checked with a real Godot 4.5.1.
The application is the folder the repository is cloned into (**APP** below, environment variable `ASTRA_APP`; the skill itself lives in `APP/ai/skills/astraforge`, and `~/.claude/skills` holds a junction to it, see `references/install.md` §8; git `master`, the current commit is `git log -1`, GitHub `Snodrod/AstraForge`, version 0.3.0). The asset and clip library is `ASTRA_FORGE_LIBRARY` (default `C:\AstraForge\library`).
**Machine-specific private notes** (local paths, the user's notes, a snapshot of what is installed) are not stored in the repository: if the folder `%USERPROFILE%\.claude\astraforge-local\` exists, read its `README.md` - it supplements this skill.
The studio UI has English and Russian dictionaries (English by default, switchable to Russian). Answer the user in the language they write in, briefly.

**Effects, particles, VFX textures, flipbooks, the "Effects" and "VFX" tabs belong to the `astraforge-vfx` skill** (not duplicated here). Pictures without 3D: `codex-image` (via ChatGPT/Codex) or `local-image-generation`.

## Role: a generic asset workshop

AstraForge is a single, game-agnostic workshop. Responsibility is split like this:

| Who | Responsible for |
|---|---|
| **AstraForge (this skill)** | *How* an asset is made: concept, views, 3D, rig, clips, items, effects, rendering, package; output that passes the target game's contract the first time (export profile and parameters matched to that game's tests) |
| **The target game** | Only *what* it accepts - its contract (frame size, axes, names, budgets, registry format, registry test, licenses) - and *how* files get into its repository |

Games do not build their own 3D pipelines: if something is missing, it becomes a new profile, parameter or recipe of the studio. The studio is not bent to a game's contract, nor the contract to the studio: take the contract of the target game as given and pass its parameters to the export profile.

## When to call this skill

Any work on models/assets for games: "make a model/unit/ship", "rig it", "animate it", "sprites", "Godot package", "views", "why does the studio not start", "install/move the studio".
Do not call it for: pure 2D graphics, video editing, code and data of the games themselves (combat, screens, balance - those follow the rules of their own repositories).

## Step 0. Check the state (read-only, ~10 s)

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\.claude\skills\astraforge\scripts\af_status.ps1"   # [-App <folder>] [-NoVersions] [-Json]
```
A table of present / missing / disabled / running: the application, ports (3210, 8190, 8191, 3211, 11434; 8188 belongs to someone else, informational only), Node/Blender/Godot, Hunyuan/Pixal3D/SDXL/MV-Adapter weights, Ollama and its models,
the library and clips, VFX programs. "Studio is off" is a normal result, not an error. The script starts and downloads nothing. A detailed snapshot of the machine, if present, is in `%USERPROFILE%\.claude\astraforge-local\`.

## Start and stop

| Action | Command |
|---|---|
| Start (usually done by the user) | `Start.cmd` in APP or `powershell -NoProfile -ExecutionPolicy Bypass -File "<APP>\scripts\Start.ps1" -NoBrowser`; address http://127.0.0.1:3210 (`/api/health` -> `{"app":"AstraForge"}`) |
| Stop | `Stop.cmd` (kills only the studio's processes; **the user's own ComfyUI :8188 and Ollama stay up**) |
| Logs | `<APP>\data\runtime\server.log`, `server-error.log`, `comfy.log`, `pixal3d.log`; Blender steps - `<runtime>\blender\<jobId>\<step>.log` |

Do not run `node server/index.mjs` "just to check": it brings up ComfyUI on the GPU. Pixal3D starts by itself on the first job (cold start 1-2 min). Default ports are 3210 (studio), 8190, 8191, 3211; `Start.ps1`/`Stop.ps1` assume exactly 3210.

## Studio map

**Tabs** (left column, 10): *Create* (concept, **Views**, engine choice Pixal3D/Hunyuan, "Create 3D model" / "Create 3D from views") · *Forge* (wizard Prepare -> Skeleton -> Motions -> Items -> Effects -> Export,
"Jobs", "Package", "Review") · *Bones* (the old in-browser rig) · *Textures* (UV, PBR, brush, comparison with a reference) · *Animation* (keys, rotation, WebM) · *Effects* and *VFX* (-> `astraforge-vfx`) ·
*Sprites* (iso 8 directions, "all clips into GLB") · *AI* (Ollama/OpenAI/Anthropic assistant) · *Tripo* (cloud, paid - do not run without the user's decision).

**CLI** (from APP; the server must be running, or use `--direct` - then paths come only from `ASTRA_*` variables, not from `config\paths.json`):
```
node scripts/forge.mjs status | steps | presets | assets | show <id> | jobs | job <id> [--wait] | cancel <id>
node scripts/forge.mjs create --kind humanoid|creature|item|ship|prop --name "Name" (--from-glb f.glb | --from-job <jobId> | --from-asset <id> | --params '{}') [--preset <id> | --steps steps.json]
node scripts/forge.mjs run <assetId> (--steps steps.json | --preset <id>) [--from source|latest|v003]
node scripts/forge.mjs export <assetId> --profile godot|iso8|custom [--out <folder>]
node scripts/forge.mjs batch items.json | clips list | clips import <FBX folder> | clips remove <id>
node scripts/views/views_generate.mjs --input front.png --out <folder> --provider codex|mvadapter     # views
node scripts/views/views_3d.mjs --views <folder> --out mesh.glb --engine pixal3d|hunyuan|auto [--seed N]  # 3D from views
```
**The Forge pipeline is 28 Blender steps** (`forge.mjs steps`): `inspect cleanup convert separate replace_part place_parts paint_from_concept assemble_parts extract_props rig_humanoid prepare_mixamo import_mixamo_rig retarget motion_style cloth_sim clip_library
rig_creature anim_creature item_kit item_from_mesh attach_prop bone_kit ship_gen turret_gen ship_from_mesh kitbash render_sprites godot_package`; 8 ready-made presets. Every run creates a new version `v00N` of the asset passport (never overwritten); a step's result is `outputs` + `checks`.
The list of steps with their purpose, the API, the passport and the data layout are in `references/studio.md`.

**Export profiles:** `godot` (a folder `<id>/` with GLB, `.tscn`, FX) · `iso8` (8-direction sprite sheets) · `custom` (sprites with parameters you set to match the target game's contract). The studio itself **never writes** into other projects.

## Typical path "picture -> game unit"

1. Concept: PNG 3:4, full-height character, **T-pose, nothing held in the hands**, light background (`codex-image` or SDXL in the studio).
2. "Views": from the front view create left/back/right (ChatGPT - final quality, MV-Adapter - an offline draft).
3. 3D from the four views in **Pixal3D** (about 4 min, about 10 GB VRAM; shape only) -> GLB.
4. Forge: `cleanup -> paint_from_concept -> rig_humanoid -> retarget (Quaternius/Mixamo) -> motion_style -> render_sprites / godot_package`. After `cleanup` **look at the preview** (torn Pixal3D meshes need `voxel_remesh_m: 0.004`, intact ones do not).
5. Gates: red `checks` stop the step; clip posture (`posture.py`) and the preview are checked before rendering; headless Godot 4.5.1; the "Review" tab.
6. Export with the export profile **using the parameters of the target game's contract**, then hand the package over under that game's rules.
Detailed recipe, commands and `steps.json` examples are in `references/studio.md`.

## Where to read next (by task)

| Task | File |
|---|---|
| Install from scratch / on another PC, wizard keys, `paths.json`, `ASTRA_*` variables, moving the library, post-install check | `references/install.md` |
| What exactly is downloaded and from where (URL, versions, sizes, sha256, licenses, ports, who uses it) | `references/components.md` |
| What is actually installed on this machine now (versions, paths, weights, Ollama models) | `scripts/af_status.ps1`; private snapshot - `%USERPROFILE%\.claude\astraforge-local\` (if present) |
| Architecture, tabs, all steps, CLI, API, passport, versions, library, `data\` | `references/studio.md` |
| Errors, Windows/Git Bash/PowerShell pitfalls, Pixal3D, rig, posture, Godot; discrepancies between the docs and reality | `references/troubleshooting.md` |
| Effects, particles, VFX textures, flipbooks, Simon Schreibt generators | the **`astraforge-vfx`** skill |
| Primary sources in APP | `README.md`, `ARCHITECTURE.md`, `docs/forge/GUIDE.md` (guide), `docs/FORGE-CONTRACT.md` (contract), `docs/forge/API.md`, `docs/forge/pixal3d.md`, `views.md`, `export.md`, `THIRD_PARTY_NOTICES.md` |
| History and decisions | `git log`, `docs/`; the user's notes - via links from `%USERPROFILE%\.claude\astraforge-local\README.md` (if present) |

## Rules (never break these)

1. **Downloads only with the user's consent.** Before any install/download show the list, sizes and licenses (`components.md`) and wait for a "yes". Do not run the whole install wizard "just to check":
   only `-ScanOnly` / `-DryRun` (a full run rewrites the desktop shortcut and starts the studio).
2. **Do not touch** the user's ComfyUI on 8188 (if `ASTRA_COMFY_DIR` points at its folder, code and models are read-only for us) and Ollama; the studio's `data\`; `data\vault.json` (DPAPI keys, a secret); other people's working copies of projects. Never publish keys or tokens.
3. **There is one GPU**: generations, GPU Blender rendering and any other GPU pipeline take turns through the shared GPU lock (a named Windows mutex, overridable with `ASTRA_GPU_LOCK_NAME`); Pixal3D and Hunyuan never run at the same time. Do not launch heavy work without need.
4. **Godot - only 4.5.1** (`ASTRA_GODOT` or the `godot` key in `config\paths.json`), not 4.7. Keep paths short (<= 260 characters): the library (`ASTRA_FORGE_LIBRARY`) must not be moved after the first assets exist (paths in passports are absolute).
5. **Paid and manual:** Tripo/Meshy only on the user's decision; Mixamo clips are downloaded by the user; ChatGPT views send the picture to OpenAI (say so).
6. **Target projects:** the studio's server and CLI never write into a game repository. Files are placed in a game repository by an agent working under that repository's own rules (branch, tests, report, license files); do not bypass its hooks. In repositories without agent rules, only with the user's knowledge.
7. **Honesty:** silhouette fitting and photo projection are not an "exact copy" and not PBR; "ok" in a step status does not replace looking at the preview; neural geometry is soft.
8. **Claude environment:** in Git Bash use `py` instead of `python`; `git -C`/`git -c` are forbidden - work through `cd`; all `.ps1`/`.cs` files are UTF-8 with BOM; `curl`/`tar` come from System32; do not run `Setup.exe /extract` from Git Bash.
9. For Hunyuan3D the Tencent license forbids use in the EU, the United Kingdom and South Korea; nvdiffrast/nvdiffrec_render (in the Pixal3D environment) are non-commercial - the studio does not call them, and Trellis2 texturing must not be used.
10. When a piece of work is done, report briefly: what was done, how it was verified (numbers), what remains; put useful findings into the studio's `docs/` or into the user's notes if `astraforge-local` points to them.
