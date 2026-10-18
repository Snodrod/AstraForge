# AstraForge: pitfalls and solutions

Sources: the installer and studio code, `docs/forge/*.md`, `README.md`. Always start with `scripts\af_status.ps1` (read-only).

## 1. Environment: Windows, Git Bash, PowerShell

| Symptom | Cause | What to do |
|---|---|---|
| Blender/Godot "cannot open the file", path errors | the **260-character** limit (MAX_PATH); inside an asset another ~110 characters are added (`assets\<uuid>\v001\02_godot_package\…`) | keep the library and runtime in **short paths** (`C:\AstraForge\library`, `C:\AstraForge\runtime`); Setup.exe requires a folder of at most 60 characters; for Pixal3D prefer `C:\AstraForge\ComfyUI-Pixal3D`. Run smoke tests from a short folder such as `C:\afs` |
| In Git Bash `python` does nothing | `python` is the Microsoft Store stub | use `py` (`py -I script.py`; `py -0` lists the versions). For studio scripts use `<ComfyUI dir>\.venv\Scripts\python.exe` |
| `Setup.exe /extract C:\x` opens a window | Git Bash turns `/extract` into a path | run it from PowerShell (or with `MSYS_NO_PATHCONV=1`) |
| `Start ComfyUI (Pixal3D, 8191).bat` does not start from Git Bash | the name has parentheses and a comma | PowerShell `Start-Process -FilePath '…\Start ComfyUI (Pixal3D, 8191).bat'` (or let the studio start the engine with the first job) |
| The wizard/script "breaks" on non-ASCII text | `.ps1` files without a BOM are read as ANSI by Windows PowerShell 5.1 | **all `.ps1` and `.cs` files are UTF-8 with BOM** (`scripts\Stop.ps1` has no BOM but is pure ASCII) |
| The wizard does not unpack a zip, `tar` complains about `C:` | `tar` from Git/MSYS comes first in PATH | the wizard takes `System32\tar.exe` and `System32\curl.exe`; do not substitute them |
| `git: detected dubious ownership` | the AstraForge folder was created by a different Windows user | `git config --global --add safe.directory C:/AstraForge` |
| `git -c …` / `git -C …` refused in a Claude environment | forbidden by the environment's rules | work via `cd <folder>` and plain `git …` |
| `npm` does not work | PATH is broken | `node "C:/Program Files/nodejs/node_modules/npm/bin/npm-cli.js" run build` |
| PowerShell `Get-ChildItem` / `du` over a 35 GB library hangs | a very large number of files | count by first-level directories, `-Depth 1`, a background task |
| Godot leaves a cache and settings in the profile | old runs (before the fix) wrote to `%APPDATA%\Godot\app_userdata\af_godot_check` | the steps now start Godot with `APPDATA`/`LOCALAPPDATA` inside `out_dir\_godot_check\_home`; the old folder may be deleted by hand. Checks are `--headless` only, version **4.5.1**, the console `*_console.exe` |

## 2. What must not be touched

- **The user's ComfyUI on 8188** and Ollama must not be stopped or reconfigured. The studio starts **its own** process on 8190 from the same code/`.venv`, but with separate directories,
  `--disable-all-custom-nodes` and an allow-list (`AstraForgeMesh`, `ComfyUI-MVAdapter`). `Stop.cmd` kills only the Python with `data\runtime\input` and `--port 8190` (and Pixal3D with `data\runtime\pixal3d-temp`).
- Do not lose `data\runtime\custom_nodes\AstraForgeMesh\__init__.py`; do not delete `data\` (projects, models, connections); `data\vault.json` (DPAPI) is a secret — keep it out of archives and git.
- **The GPU lock** (a named Windows mutex, see `ASTRA_GPU_LOCK_NAME` and `server\gpu-lock.mjs`) is shared with any other process that takes the same lock: external renders, GPU Blender steps and generations queue up. "Waiting for the GPU to be released" is normal (up to 60 min, then the error "The GPU is busy with another process for more than 3600 s").
- Paid clouds (Tripo, Meshy) and the download of models and packages happen only on the user's decision; Mixamo only by hand of the user.
- Neon Flames (VFX) without a license must not go into a distributed build. Hunyuan3D models must not be used in the EU, the UK and South Korea.

## 3. Installation and startup

| Symptom | Cause / solution |
|---|---|
| The wizard hangs on a question | it is interactive: use `-Yes -Only …` (default answers; `hy21`, `sdxl`, `vfx`, `pixal3d` are not installed without `-Only`) |
| `clips` fails with "Blender is needed" | order in `-Only`: `blender` before `clips` |
| `mvadapter`: "SDXL is not downloaded yet" | install `sdxl` (6.9 GB) first; afterwards restart the studio (`Stop`, `Start`): the node allow-list is read when ComfyUI starts |
| "Port 3210 is in use by another application" | a foreign application is on 3210; `PORT` changes only the server/CLI, while `Start.ps1`/`Stop.ps1` are hard-wired to 3210 |
| "Port 8190 is taken by another ComfyUI" | a foreign ComfyUI on 8190; free the port or connect its address in "Connections" |
| Port 3211 is taken (`EADDRINUSE`) | `ASTRA_VFX_WEB_PORT` |
| "Node.js was not found" / "Dependencies are missing" / "The interface has not been built" | no Node >= 22.12, `node_modules\express` or `dist\index.html` → the wizard components `node`, `npm` |
| The studio's ComfyUI does not start by itself | the server auto-starts it only if `data\models\checkpoints\hunyuan3d-dit-v2.safetensors` is exactly there (not just in some other ComfyUI's `models`) → "Connections → start the 3D engine" (`POST /api/runtime/start`); log `data\runtime\comfy.log` |
| "The studio server does not know the route …" | the studio was started before the code update → `Stop.cmd`, `Start.cmd`; or `--direct` in the CLI |
| The "Forge" module is "not connected" | `/api/forge` answers non-JSON: an old server; restart |
| `forge.mjs --direct` / `views_*.mjs` do not see Blender, Godot, Pixal3D | they read **only** the `ASTRA_*` variables, not `config\paths.json` (that is read by `Start.ps1`); set `ASTRA_BLENDER`, `ASTRA_GODOT` (the wizard installs Godot into `runtime\godot`, but the code looks for it only via `ASTRA_GODOT` and in `C:\Program Files\Godot`, `C:\Godot`, `%LOCALAPPDATA%\Programs\Godot`) |
| The "AI" chat does not answer on a new PC | the configured model is missing in Ollama — install `llm` with the wizard (it writes `data\config.json`) or choose a model in "Connections"; Ollama must be running |
| No job starts | the queue holds at most 4; an LLM answer and 3D exclude each other (one GPU) |
| Setup.exe "protected your PC" | the file is not signed → "More info" → "Run anyway" |
| A full wizard run "just to check" | **do not**: it rewrites the desktop shortcut and opens the studio; only `-ScanOnly`/`-DryRun` |

## 4. Pixal3D

- **DINOv3** — only from the official copy `timm/vit_large_patch16_dinov3.lvd1689m` with the conversion `tools\dinov3_timm_to_hf.py`; the gated facebook repository and third-party re-uploads are not needed. Auto-download of DINOv3 from a third-party mirror in the node is disabled by the `ASTRAFORGE PATCH` (`installer\pixal3d\patch_trellis2.py`).
- **utils3d** — only `github.com/EasternJournalist/utils3d` (1.7). The package of that name on PyPI belongs to someone else.
- **meshlib** (non-commercial license) is not installed; holes are closed by CuMesh (MIT). **nvdiffrast/nvdiffrec_render** (non-commercial) are installed because the node imports them, but the studio never calls them: the graph must contain no `Texturing`, `Rasteriz`, `Render`, `UnWrap`, `nvdiff` nodes — do not use Trellis2/Pixal3D texturing.
- About 10 GB of free VRAM is needed (peak above the base: 4 views 9.5–10.8 GB, 1 view 6.4 GB). If Blender, a WebGL browser or a game holds it — "out of video memory". The two engines (Pixal3D and Hunyuan) do not run simultaneously: the studio unloads the other one itself.
- The first start takes 1–2 minutes (cold caches), then about 6 s; cancelling a job can take up to a minute (it stops at a node boundary).
- **Pixal3D meshes can be torn** (a skeleton: 161 pieces, open edges). Without `voxel_remesh_m: 0.004` in `cleanup`, `rig_humanoid` does not distribute weights and the model freezes in the T-pose. **But on a whole mesh remeshing does harm**
  (an archer test: after `cleanup` with 0.004 and 12k triangles only straps were left without a body, while `result.json` said "ok"). Remesh only torn meshes; after `cleanup` **look at the preview** (connectivity and volume), not at the status.
- The triangle budget for thin meshes (a skeleton) is 24k: at 10k a bracer becomes a cube and fingers become plates. For humans with a face `target_tris` is about 24,000.
- After rigging: `smooth_arm_weights.py`, `arm_rigid.py` (the bracer rigid on the forearm, the hand whole) — otherwise "spikes" and crooked bracers. If the rig refused (asymmetric arms), set `params.markers` (a mirror of the correct arm);
  the autorig takes a T-pose for "arms down", `build_unit.sh` substitutes the arm markers itself (`arm_markers.py`).
- Pixal3D builds ONLY the shape: color comes from `paint_from_concept` (from the front and back views; the back is painted by `paint_back.py`, the step paints it only "through").
- One view is worse than four (IoU 0.89/0.71/0.86/0.73 against 0.97/0.94/0.94/0.94); for a final character use views. Verified on a humanoid in a T-pose; quadrupeds and objects were not verified.

## 5. Views

- The input is a front view in a **T-pose, with no weapon** in the hands (a weapon confuses the side views). Characters not in a T-pose, quadrupeds and objects were not verified.
- "Left" = the camera on the side of the character's left hand (he looks to the left edge of the frame). MV-Adapter: azimuth 90° = "left", 270° = "right". On a symmetric character swapping left/right barely changes the result.
- MV-Adapter: at most 640 px with 4 views on 12 GB; at 768 px the GPU starts swapping (do not use it); 6 views only at 512; it invents the back details — for drafts and work without a network. ChatGPT is for the final (about 60 s per view), but the picture goes to OpenAI and a subscription and the Codex app are needed.
- `views_generate.mjs --status` → `codex.available` and `mvadapter.ready` must be `true`; `mvadapter.ready=false`: no weights, no `sdxl-config` (`scripts\views\make_sdxl_config.py`) or ComfyUI was started before the node was installed.
- `codex.exe` is not in PATH — it is found via `Get-AppxPackage OpenAI.Codex`. Pictures "via GPT" — the `codex-image` skill (about 45–65 s per picture), `openai-visual` does not work without an API key.

## 6. Rig, clips, posture, weapon

- **Posture.** After `retarget`, Quaternius clips rotate the clavicles back by 31–44° ("shoulders turned too far back", then "hands in the pelvis"). The `posture.py` correction: clavicles toward rest +6°, the shoulder is rotated back (the arm direction does not change,
  otherwise the hands go to the pelvis), a hanging arm +4° forward; **a raised arm (> 35°, a caster stance) is not touched**. Gates over all clips: hands outside the hips and not in front of the pelvis, clavicles in the idle stance <= 18°, in walking <= 15° on average.
  On failure do not start the render. For a ghost use `--floating`.
- **Quaternius/ActionForge clips** are "in place" in the library (the `root` bone is zeroed; otherwise a strike carried the model 2.5 m up; the originals are in `animations\_backup_quaternius_rootmotion`). A sword strike is `quaternius-sword-attack` (no hop; `regular-c` is a jump);
  the stance is `quaternius-idle-loop` (in the archer clip the legs are too wide). Before moving, run `clip_audit.py` (pelvis lift).
- Mixamo: FBX Binary, **Without Skin**, 30 fps. Builds at 12 fps with Mixamo's 30 fps keys give sluggish animations — export with fps=30. Some needed clips of the Mixamo library (run, jumps, strafes, emotes) are absent — the user downloads them.
- **"The weapon is glued"**: a skeleton with fingers is needed (Quaternius, 65–71 bones); the weapon grip is computed (`grip_solve.py`: the tip = the grip's +Z); a shield is `attach_prop` with `fit: frame`, `standoff 0.03`, a kite shield `rot_deg [0,45,0]`; the fingers of low-poly models are merged — `fist.py` bends a fist.
  Extracted items (`extract_props`) are returned by `attach_prop` with `extracted:true`; an item that failed the check is replaced by an item from the kit.
- `rig_humanoid` refuses: "the model does not look like a human", "arms are asymmetric", mesh tears (`deform_torn_area` > 1 %). In the `--qa clips` mode: `deform_gate: warning`, the tears on real clips in `motion_style` decide (1 % base, 8 % hands above the head, 4 % robe).
  Robes tear (`robe: true` tears off a boot toe on some meshes — check), the hem is fixed by `fix_weights.py`.
- Broken meshes: a "fused" blade, a block-shaped sword is replaced. Wings: folded ones get their own bones and a flap (`wing_flap.py`); open ones are sculpted by Hunyuan as plates.
- Local check with `qwen3-vl:8b`: short yes/no questions, `think:false`, **`num_ctx` 16384** (the default 4096 gives an empty answer). Its answer is a hint; the numeric gates are what matters.

## 7. Export and Godot

- Godot **4.5.1** only; do not upgrade projects to another Godot version. If Godot reports "Nonexistent function … in base 'GDScript'" the class cache is stale: `Godot_…_console.exe --headless --editor --quit --path .`.
- The Godot 4.5 importer strips the `loop`/`cycle`/`loop_mode` tags from a clip name one after another and renames from the original name → `pitch_cycle-loop` gives "Animation not found". `godot_package` renames such clips itself (`clip_renames`, `clips[].package_name`).
- Sockets are found by `extras.game_socket`, not by node name (the importer with `gltf/naming_version=2` replaces dots); names are ASCII snake_case. `pivot_yaw`/`pivot_pitch` are not sockets.
- GL Compatibility does not support particle trails (`trail_enabled`); `engine_trail` is made of large semi-transparent particles in world coordinates. The FX scale is a heuristic (`fx_auto_scale`); a particle < 1 % of the asset's long axis gives the warning `fx_scale_vs_asset`.
- The headless Godot check does not judge the picture: loading, names, loops, scale, attachment. A visual preview is `fx_preview` (a window of about 3 s).
- EEVEE in Blender 5.0 has no shadow catcher: shadow in sprites only with `engine: CYCLES`. A cell that overflows the frame is a matter for `cell_fit`, not an automatic correction.
- Wrong model units are caught **before** the render (`min_body_px`, `max_cell_px`): `height_m` in the parameters. Neural geometry is soft — close-ups need parametric details (`bone_kit`, `item_kit`, `place_parts`).
- Color from a picture is a photo projection, not PBR; the hidden side is a guess or a mirror; a silhouette IoU of 86.5 % does not mean anatomical accuracy — do not pass a fit off as an "exact copy".

## 8. Library and data

- Paths in passports are **absolute** (`asset.json`, `v00N\version.json`, `result.json`): moving the library to another path breaks `from: latest` of old versions (there is no recalculation code). Keep `C:\AstraForge\library`.
- Versions are never overwritten; deleting = moving to `trash\`.
- Two simultaneous processes on one library: clips are guarded by the library lock (one `clip_library` task at a time; "The asset files are busy with another program…" — wait).

## 9. Documentation vs. reality (known discrepancies)

1. `docs/forge/server.md` says "24 steps" and `ASTRA_FORGE_LIBRARY` defaulting to `data/library`; the code has **28 steps** (`listSteps()`; the last added is `assemble_parts`) and the default library is `C:\AstraForge\library` (`defaultLibraryDir`).
2. `docs/forge/ui.md` says "the ninth tab"; `src/App.jsx` has **ten** (Create, Forge, Bones, Textures, Animation, Effects, VFX, Sprites, AI, Tripo).
3. `docs/forge/views_install.md` ("MV-Adapter download is not added to the installer") is **out of date**: `Install.ps1` has `mvadapter`, `vlm`, `pixal3d`, `pixal3dw`, `pixal3dtest`, `vfx`.
4. `README.md`: "Start.cmd takes paths from `config\paths.json`". The file is read only by `Start.ps1` (and `pixal3d.mjs`); `forge.mjs --direct` and `views_*.mjs` do not read it.
5. `README.md`: Pixal3D "by default in `runtime\ComfyUI-Pixal3D`"; in the code `DEFAULT_PIXAL3D_DIR = C:/AstraForge/ComfyUI-Pixal3D` (the wizard writes `pixal3dDir` to `paths.json`).
6. `docs/forge/GUIDE.md` names Hunyuan 2.1 as the main engine; the main shape engine is now Pixal3D (since 2026-10-14). `ARCHITECTURE.md` describes only Hunyuan 2.0.
7. `GODOT-RIGGING.md` and `SURFACES-SPRITES-GODOT.md` refer to a check in Godot 4.7.2; the Forge checks only 4.5.1.
8. The wizard's estimate for the VFX install is about 330 MB (the disk requirement in the wizard was raised to 500 MB on 2026-10-17); the real size with installers and the database export can reach about 500 MB.
9. After conversion the wizard deletes the original DINOv3 copy `ComfyUI\models\timm\vit_large_patch16_dinov3.lvd1689m\model.safetensors` (1.2 GB); if you installed by hand it can remain. Harmless.
