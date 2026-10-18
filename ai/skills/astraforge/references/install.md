# AstraForge: installation from scratch on a new Windows PC

All facts come from `installer\Install.ps1`, `installer\pixal3d\pixal3d.ps1`, `installer\mvadapter\mvadapter.ps1`, `scripts\Start.ps1`, `README.md`, `installer\setup\Setup.cs`.
APP = the application folder. On a new PC install into a short path, `C:\AstraForge`. The component list with URLs is in `components.md`.

## 0. Requirements

| What | Value |
|---|---|
| OS | Windows 10/11 x64. Administrator rights are **not needed** (per `README.md` and the header of `Install.ps1`; Ollama is installed into the user profile, `%LOCALAPPDATA%\Programs\Ollama`) |
| GPU | NVIDIA for image-to-3D: Hunyuan3D — from 8 GB (12 is better); MV-Adapter — from 8 GB (only 512 px on 8 GB), comfortable at 10+; **Pixal3D — RTX 30/40/50 (compute_cap >= 8.0), from 8 GB with a warning, 10+ is better** (peak about 9.5–10.8 GB with 4 views); with less than 8 GB the wizard does not offer Pixal3D/MV-Adapter. Without NVIDIA the interface and Blender work, and CPU PyTorch is installed |
| RAM | the installer files state no requirement (the wizard does not check it); 32 GB or more is comfortable |
| Disk | 20–40 GB for the base, plus about 46–52 GB for Pixal3D (the Pixal3D install disk needs headroom: estimate + 3.3 GB). The wizard computes the need per disk itself and refuses when space is short |
| Path | **short, Latin letters only**. Blender/Godot break on paths over 260 characters; Setup.exe requires a folder of at most 60 characters; the wizard warns if the Pixal3D path is longer than 90 |
| Network | needed for downloads (about 25 GB for the base; about 42 GB for Pixal3D). An interrupted download resumes when you run it again |
| Tools | `curl.exe` and `tar.exe` from `System32` (present in Windows 10/11; **not** the ones from Git/MSYS), Windows PowerShell 5.1 |

## 1. Getting the code (three ways)

**A. Repository `Snodrod/AstraForge` on GitHub, branch `master`**:
```powershell
git clone https://github.com/Snodrod/AstraForge C:\AstraForge   # short path
```
A folder with `.git` created by another Windows user gives "dubious ownership" → `git config --global --add safe.directory C:/AstraForge`.
The clone has no `node_modules`, `dist`, `config`, `runtime`, models or library — the wizard installs all of that.

**B. A single file `AstraForge-Setup-<version>.exe`** (MIT, built from the repository). To build a fresh one:
`powershell -ExecutionPolicy Bypass -File installer\build-release.ps1 [-Out <folder>]` → `..\AstraForge-release\` (exe + `.sha256`; inside are the application, `dist`, `node_modules` without dev packages;
it is built with the system `csc.exe` from `%WINDIR%\Microsoft.NET\Framework64\v4.0.30319`; a built `dist` is required).
The file is not signed: Windows shows "Windows protected your PC" → "More info" → "Run anyway".
The Setup window: a folder (default `C:\AstraForge`, or `C:\AstraForgeStudio` if it is occupied by foreign content; path up to 60 characters) → "Install" → unpacking and automatic start of the wizard.
Checking the build without a window (**from PowerShell, not from Git Bash** — there `/extract` is turned into a path): `.\AstraForge-Setup-<version>.exe /extract C:\afx`.

**C. A copy of the folder from another PC**: without `data\models`, `data\vault.json` (DPAPI keys are bound to the Windows user), `config\paths.json`, `data\runtime\node.pid`; then run the wizard.

## 2. The setup wizard

Start it by double-clicking **`Install AstraForge.cmd`** in the root of APP (it runs `powershell.exe -NoProfile -ExecutionPolicy Bypass -File installer\Install.ps1 %*` and waits for a key)
or directly from PowerShell:
```powershell
cd C:\AstraForge
powershell -NoProfile -ExecutionPolicy Bypass -File installer\Install.ps1 -ScanOnly
```
The log is `installer\install.log`; downloads go to `installer\downloads\` (not downloaded again; may be deleted after installation).
The wizard is **interactive** (`Read-Host`); the questions are yes/no with a default answer.

### Switches
| Switch | What it does |
|---|---|
| `-ScanOnly` | only show what is present and what is not (GPU, Node, npm, Blender, ComfyUI, weights, Godot, Ollama and models, clips, MV-Adapter, Pixal3D); changes nothing, exits 0 |
| `-DryRun` | walk through the wizard (questions are asked), show the plan and the per-disk summary, **download and change nothing** |
| `-Yes` | no questions: the **default answers** are taken (see the table below). Optional components whose default answer is "no" (`hy21`, `sdxl`, `vfx`, `pixal3d`) are therefore **not installed** — name them via `-Only` |
| `-Only a,b,c` | reinstall only these components, no questions asked; **list them in installation order** (otherwise, for example, `clips` will not find Blender and `mvadapter` will not find SDXL) |

The full list of ids for `-Only` (`README.md` has no `vfx`; it is in `Install.ps1`):
`node, npm, blender, comfy, comfydeps, hy2, hymv, hy21, sdxl, u2net, godot, clips, ollama, llm, vlm, mvadapter, pixal3d, pixal3dw, pixal3dtest, vfx`.

| id | What it does | Depends on |
|---|---|---|
| `node` | Node.js v22.23.3 in `runtime\node`, writes `node` to paths.json | — |
| `npm` | `npm ci` + `npm run build` in APP | `node` |
| `blender` | Blender 5.0.1 in `runtime\blender`, writes `blender` | — |
| `comfy` | ComfyUI v0.33.2 in `runtime\ComfyUI`, `uv`, Python 3.12, PyTorch, libraries; writes `comfyDir` | — |
| `comfydeps` | only the missing Python libraries of an already installed ComfyUI | a ready ComfyUI |
| `hy2` / `hymv` / `hy21` / `sdxl` | weights in `data\models\checkpoints\` (4.9 / 4.9 / 7.4 / 6.9 GB) | — |
| `u2net` | `data\models\background\models\u2net\u2net.onnx` | — |
| `godot` | Godot 4.5.1 in `runtime\godot`, writes `godot` | — |
| `clips` | 86 Quaternius/ActionForge clips: download, conversion to FBX and import into the library | **Blender** |
| `ollama` | `OllamaSetup.exe` silently; writes `ollama` | — |
| `llm` | `ollama pull qwen3:4b` (or 8b, 14b) chosen by VRAM + writes `provider/ollamaModel/ollamaUrl` to `data\config.json` | `ollama` |
| `vlm` | `ollama pull qwen3-vl:8b` (writes nothing to `config.json`) | `ollama` |
| `mvadapter` | the MV-Adapter node + patches, Python packages in the ComfyUI `.venv`, 3.6 GB of weights, `sdxl-config`; restart the studio afterwards | `comfy`, `sdxl` |
| `pixal3d` | the Pixal3D engine (Python embed, ComfyUI 0.39.0, Trellis2 nodes, PyTorch 2.8.0+cu128, FlashAttention, wheels, utils3d, node patch) + a trial run; writes `pixal3dDir` | NVIDIA, disk space |
| `pixal3dw` | Pixal3D weights, MoGe-2, NAF, DINOv3 (from timm) + conversion | `pixal3d` |
| `pixal3dtest` | a trial ComfyUI run on 8191 and a check of the Trellis2 nodes | `pixal3d`, `pixal3dw` |
| `vfx` | `scripts\vfx\Install-VfxTools.ps1 -Root <vfxTools>`; writes `vfxTools` | — |

A typical order for a clean machine (everything except the heavy Pixal3D):
```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File installer\Install.ps1 -Yes -Only node,npm,blender,comfy,hy2,u2net,godot,clips,sdxl,hymv,mvadapter,ollama,vlm
# later, if Pixal3D is needed (about 46 GB):
powershell -NoProfile -ExecutionPolicy Bypass -File installer\Install.ps1 -Yes -Only pixal3d,pixal3dw,pixal3dtest
```

### What the wizard asks (interactively) and the default answer
Mandatory (no questions, in the plan): `node` (if there is no Node >= 22.12), `npm` (no `node_modules\express` or `dist\index.html`), `blender`, `comfy`/`comfydeps`, `hy2`, `u2net`.
Optional, in order: Hunyuan3D-2mv — **yes**; Hunyuan3D-2.1 — no; SDXL — no; Godot 4.5.1 — **yes**; Quaternius clips — **yes**; VFX generators — no;
MV-Adapter — yes only if SDXL is already installed and there is no 8 GB warning (otherwise no; agreeing adds SDXL to the plan); Ollama + LLM — yes only if Ollama has no models at all;
qwen3-vl:8b — yes if there is >= 8 GB of VRAM and Ollama is present or being installed; Pixal3D — no. At the end a summary (download size, space per disk, licenses; for Hunyuan a warning about the EU/UK/South Korea;
for Pixal3D about nvdiffrast/PyMeshLab), then "Start the installation?" [yes]. A component failure → the question "Continue with the rest?".
At the end: a re-check (`Scan`), the desktop shortcut `AstraForge.lnk` (→ `Start.cmd`, icon `assets\astraforge.ico`), and the question "Start AstraForge now?".

**How to test the wizard safely:** only `-ScanOnly` / `-DryRun`, or dot-sourcing (`. .\installer\Install.ps1` returns before the wizard). A full run
rewrites the desktop shortcut, writes `config\paths.json` and starts the studio.

### What the wizard writes: `config\paths.json`
Keys: `node`, `blender`, `comfyDir`, `godot`, `ollama`, `library`, `forgeRuntime`, `pixal3dDir`, `vfxTools` (and `pixal3dUrl` can be added by hand). The file is not in git.
**It is read by `scripts\Start.ps1`**, which copies the values into environment variables (if the user has not set them) and then starts `node server\index.mjs`:

| paths.json key | Variable | Fallback when neither is set (code) |
|---|---|---|
| `blender` | `ASTRA_BLENDER` | `C:/Program Files/Blender Foundation/Blender 5.0/blender.exe` |
| `godot` | `ASTRA_GODOT` | a search for `Godot_v*-stable_win64[_console].exe` in a few well-known folders such as `C:/Program Files/Godot`, `C:/Godot`, `%LOCALAPPDATA%/Programs/Godot` (the code does **not** look into the `runtime\godot` folder) |
| `comfyDir` | `ASTRA_COMFY_DIR` | no reliable default: the wizard writes `<APP>\runtime\ComfyUI`; when running without `Start.ps1`, set the variable |
| `library` | `ASTRA_FORGE_LIBRARY` | `C:\AstraForge\library` (Windows) |
| `forgeRuntime` | `ASTRA_FORGE_RUNTIME` | `C:\AstraForge\runtime`; the wizard sets `<APP>\data\runtime\forge` if `C:\AstraForge\runtime` does not exist yet or APP lives in `C:\AstraForge` |
| `pixal3dDir` | `ASTRA_PIXAL3D_DIR` | `C:/AstraForge/ComfyUI-Pixal3D` (wizard default: `<APP>\runtime\ComfyUI-Pixal3D`) |
| `pixal3dUrl` | `ASTRA_PIXAL3D_URL` | `http://127.0.0.1:8191` (local http only) |
| `vfxTools` | `ASTRA_VFX_TOOLS` | `C:\AstraForge\tools\vfx` (wizard: `<APP>\runtime\vfx`) |
| `node` | — | `Start.ps1` finds the path itself; otherwise `node.exe` from PATH |

**Trap:** `paths.json` is not seen by `node scripts\forge.mjs --direct`, `scripts\views\*.mjs` or a direct `node server\index.mjs` — they need the `ASTRA_*` variables (or the fallbacks from the table).

Other variables: `PORT` (server port, default 3210; but `Start.ps1`/`Stop.ps1` check exactly 3210), `ASTRA_FORGE_URL` (server address for the CLI), `ASTRA_COMFY_URL` (default `http://127.0.0.1:8190`),
`ASTRA_OLLAMA_URL`, `ASTRA_QA_VLM` (`qwen3-vl:8b`), `ASTRA_BLENDER_CONCURRENCY` (1), `ASTRA_FORGE_JOB_CONCURRENCY`, `ASTRA_GPU_LOCK_NAME` (name of the GPU lock; default defined in `server\gpu-lock.mjs`), `ASTRA_VFX_WEB_PORT` (3211),
`ASTRA_FORGE_REVIEW_STATE|ROLES|DECISIONS` (the review tab), `ASTRA_CODEX_CONCURRENCY` (1), `ASTRA_SDXL_CONFIG`,
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `TRIPO_API_KEY` (all optional; the application does **not** read `.env` itself — set the variables in the environment before starting).

## 3. Manual steps (the wizard does not do them)

- **Mixamo clips** — downloaded by the user at mixamo.com (Adobe account): FBX Binary, **Without Skin**, 30 fps; then `node scripts\forge.mjs clips import <folder>` or drag and drop in the tab "Forge → Motions".
  Uploading a character to the Mixamo site is prepared by the `prepare_mixamo` step; the Forge itself never goes to the network.
- **Quaternius kits** (Universal Base Characters, Modular Outfits Fantasy) — the archives are supplied by the user; they go to `<library>\kits\quaternius`.
- **ChatGPT views** — the Codex app (Microsoft Store, `OpenAI.Codex`) signed in with ChatGPT; without it the local MV-Adapter remains.
- **VFX**: the installers of PixaFlux, KumoWorks, Slate are launched by the user with a button in the "VFX" tab; Pseudo Volume Generator (itch.io) and the free Gumroad / the orange box "orders" are done by hand.
- **Cloud keys** (OpenAI, Anthropic, Tripo) — optional, in "Connections". Tripo is paid: do not run it without the user's decision.
- Hugging Face: the wizard **uses no tokens**; if a repository requires a login, download the file by hand into the path given in `components.md`.

## 4. Verification after installation

```powershell
# 1. state (read-only) — a present/absent/disabled table
powershell -NoProfile -ExecutionPolicy Bypass -File "$env:USERPROFILE\.claude\skills\astraforge\scripts\af_status.ps1" -App C:\AstraForge
# 2. start without a browser (hidden node process; log data\runtime\server*.log; PID data\runtime\node.pid)
powershell -NoProfile -ExecutionPolicy Bypass -File C:\AstraForge\scripts\Start.ps1 -NoBrowser
curl.exe -s http://127.0.0.1:3210/api/health          # {"app":"AstraForge","version":"0.3.0"}
curl.exe -s http://127.0.0.1:3210/api/status          # ollama, comfy.ready, gpu, pixal3d{installed,running,missing}
# 3. the Forge: Blender, Godot, library, queue, GPU lock
node C:\AstraForge\scripts\forge.mjs status
# 4. views: codex.available, mvadapter.ready
node C:\AstraForge\scripts\views\views_generate.mjs --status
# 5. Pixal3D (with 8191 running)
C:\AstraForge\ComfyUI-Pixal3D\python_embeded\python.exe -I C:\AstraForge\ComfyUI-Pixal3D\tools\smoke_test.py --port 8191
# 6. Node tests without Blender
cd C:\AstraForge; npm test
```
Stopping: `Stop.cmd` (or `scripts\Stop.ps1`): POST `/api/shutdown`, then it kills only the Python with `data\runtime\input` and `--port 8190`, and the ComfyUI-Pixal3D started by the studio
(marker `data\runtime\pixal3d-temp`); **the main ComfyUI on 8188 and Ollama stay**. A Pixal3D started by hand with a batch file is not touched.

Blender tests (long, they launch Blender): `blender -b --factory-startup --python scripts\blender\tests\run_tests.py -- [module]`; the integration test
`tests\integration\blender.test.mjs` is not part of `npm test`; a smoke run of all presets in real Blender and Godot — `node scripts\forge-smoke.mjs` (about 5 min).

If `npm` in PATH is broken: `node "C:/Program Files/nodejs/node_modules/npm/bin/npm-cli.js" run build`. Development mode: `npm run dev` (Vite inside the server).

## 5. Updating
`git pull` (in a Claude environment without `git -C`/`git -c`: `cd` into the folder first) → `npm ci` → `npm run build` → `Stop.cmd` → `Start.cmd` (the step registry is read when the server starts: "the studio does not know the route" = it was started before the update).
Do not touch the data (`data\`, the library).

## 6. Moving the library `C:\AstraForge\library` and the data

The library holds: `assets` (asset passports, the bulk of the size), `jobs`, `animations` (Quaternius + Mixamo clips + `index.json`), `kits`, `rigs`, `props`, `batches`, `trash`.
For a **new PC `animations`, `kits`, `rigs`, `props` are usually enough** (about 0.5 GB); `assets` only if the old assets are needed.
```powershell
robocopy C:\AstraForge\library D:\backup\library /E /COPY:DAT /R:1 /W:1       # on the new PC — copy back to the same path
```
- **Paths in passports are absolute** (`asset.json` → `exports[].path`, `v001\version.json` → `outputs.*` contain `C:\\AstraForge\\library\\…`). The engine has no path recalculation code
  (`server\forge-pipeline.mjs` contains no relocation). So put the library at the **same path**; otherwise set `library`/`ASTRA_FORGE_LIBRARY` and treat old asset versions as
  read-only (new `from: source` runs work; the behavior of `from: latest` on old versions under a different path has not been verified).
- Studio data (projects, generations, assets, views): `<APP>\data\{assets,jobs,projects,exports,views,tripo,python,models}`; do not delete; do not copy `data\vault.json`.
- Pixal3D is moved by copying the whole folder (about 46 GB, portable Python, caches inside); afterwards run `pixal3dtest` or `smoke_test.py`.

## 7. If the wizard is unavailable — the manual minimum

1. Node >= 22.12 → in APP run `npm ci`, `npm run build`.
2. Blender 5.0.1 (zip) → the path in `ASTRA_BLENDER`.
3. ComfyUI v0.33.2 (zip) + `uv venv --python 3.12 .venv` + `uv pip install --python .venv\Scripts\python.exe torch torchvision torchaudio --index-url https://download.pytorch.org/whl/cu128`
   + `-r requirements.txt` + `rembg onnxruntime numba scikit-image xatlas` → `ASTRA_COMFY_DIR` (it must contain `.venv\Scripts\python.exe`, `main.py`, `comfy_extras\nodes_hunyuan3d.py`).
4. Weights from the table in `components.md` into `data\models\checkpoints\` and `data\models\background\models\u2net\`.
5. `Start.cmd`. The studio starts ComfyUI on 8190 itself if `hunyuan3d-dit-v2.safetensors` is in `data\models\checkpoints` (otherwise "Connections" → start the 3D engine).

## 8. Skills for LLMs (Claude Code and others)

The skills live in the repository itself: `ai/skills/astraforge` and `ai/skills/astraforge-vfx` (described in `ai/skills/README.md`).
After cloning, connect them to Claude Code with links so that any session sees them (cmd, no administrator rights needed):

```bat
mklink /J "%USERPROFILE%\.claude\skills\astraforge" "<APP>\ai\skills\astraforge"
mklink /J "%USERPROFILE%\.claude\skills\astraforge-vfx" "<APP>\ai\skills\astraforge-vfx"
```

Then add a section to `%USERPROFILE%\.claude\CLAUDE.md` telling sessions that 3D models, rigs, animations, sprites and Godot asset packages
are produced by the AstraForge studio and that they should load the `astraforge` skill (and `astraforge-vfx` for effects) before such work.
For other LLMs hand over the folder `ai/skills/astraforge` as is: these are plain markdown files, start with `SKILL.md`.
