# AstraForge: a local 3D asset studio

[Русская версия: README.ru.md](README.ru.md)

AstraForge is a Windows app that turns a picture or a text prompt into a game-ready 3D asset on your own machine: concept → geometry → editor → rig and animation → effects → export (GLB, OBJ, WebM, PNG, isometric sprite sheets, Godot packages). The server listens on `127.0.0.1` only. The UI is available in English and Russian (English by default, switch in the app).

## Requirements

| | Minimum | Recommended |
|---|---|---|
| OS | Windows 10/11 x64 | Windows 11 x64 |
| GPU | NVIDIA, 8 GB VRAM | NVIDIA, 12+ GB VRAM |
| Pixal3D (optional) | RTX 30/40/50 series, 10+ GB VRAM | RTX 40/50, 12+ GB VRAM |
| RAM | 16 GB | 32 GB |
| Free disk | 20-40 GB (+ about 52 GB if you add Pixal3D) | SSD |
| Network | Internet for the first install (downloads of models and tools) | |
| Rights | No administrator rights needed | |

Use a short Latin-only install path such as `C:\AstraForge`.

## Quick start with Claude Code

You need [Claude Code](https://claude.com/claude-code).

```powershell
git clone https://github.com/Snodrod/AstraForge C:\AstraForge
cd C:\AstraForge
claude
```

Then tell Claude Code: **"Install and set up AstraForge for me."** The repository contains a `CLAUDE.md` with the exact procedure. Claude Code will:

1. **Scan your computer without downloading anything:**
   `powershell -NoProfile -ExecutionPolicy Bypass -File installer\Install.ps1 -ScanOnly`
   It reports your GPU and VRAM, free disk space and what is already installed (Node.js, Blender, ComfyUI, Hunyuan3D models, SDXL, Godot, Ollama, Pixal3D).
2. **Show the plan and ask you which optional components to install.** Each has its own size and licence:

   | id | What | Size |
   |---|---|---|
   | `node`, `npm` | Node.js 22 and the UI build (required) | about 0.3 GB |
   | `blender` | Blender 5.0.1 (required) | about 0.4 GB |
   | `comfy` / `comfydeps` | ComfyUI 0.33.2 + Python 3.12 + PyTorch (required) | about 6 GB |
   | `hy2`, `u2net` | Hunyuan3D-2 (image to 3D) and background removal (required) | 4.9 GB + 0.2 GB |
   | `hymv` | Hunyuan3D-2mv: 3D from front and back views | 4.9 GB |
   | `hy21` | Hunyuan3D-2.1 | 7.4 GB |
   | `sdxl` | SDXL: text to image | 6.9 GB |
   | `mvadapter` | MV-Adapter: extra views from one picture (needs SDXL) | about 3.8 GB |
   | `godot` | Godot 4.5.1: validates game packages | 77 MB |
   | `clips` | Quaternius animations (CC0), needs Blender | 10 MB |
   | `ollama`, `llm` | local Qwen3 assistant (model picked by VRAM: qwen3:4b / 8b / 14b) | 2.5-9 GB |
   | `vlm` | qwen3-vl:8b: texture and animation checks | 6.1 GB |
   | `vfx` | VFX texture generators | about 0.5 GB |
   | `pixal3d`, `pixal3dw`, `pixal3dtest` | Pixal3D: shape from 1-4 views, RTX 30/40/50 and 10+ GB VRAM | about 48.5 GB |

3. **Run the installer unattended** (`-Yes -Only <ids>`) in the background, because the wizard is interactive and cannot be answered inside Claude Code. Downloads take tens of minutes; if one is interrupted, rerun the same command and it resumes.
4. **Verify the result** with `-ScanOnly` again and by reading `config\paths.json`.
5. **Start the studio** with `Start.cmd` and check `http://127.0.0.1:3210/api/health`, which must answer `"app":"AstraForge"`.
6. **Troubleshoot** from `installer\install.log`, `data\runtime\server-error.log` and `data\runtime\comfy.log`, reinstalling only the broken component with `Install.ps1 -Yes -Only <id>`.

API keys for OpenAI, Anthropic and Tripo are optional. Enter them in the app's **Connections** tab, where Windows DPAPI encrypts them. Never paste keys into a chat or commit them to the repository.

## Manual install

1. Clone or download this repository to a short path, for example `C:\AstraForge`.
2. Double-click **Install AstraForge.cmd** and answer the wizard's questions. It scans your PC, shows what is missing with sizes and licences, and downloads everything into this same folder (`runtime\`, `data\models\`). An interrupted download continues on the next run. A desktop shortcut **AstraForge** is created at the end.
3. Start the studio with **Start.cmd**.

No git? Download **AstraForge-Setup-0.3.0.exe** from [Releases](https://github.com/Snodrod/AstraForge/releases/latest), run it and choose a folder: it unpacks the app and starts the same wizard. The file is not code-signed, so Windows SmartScreen may warn you: click *More info → Run anyway*.

The installer writes the paths it finds to `config\paths.json`, and `Start.cmd` reads them from there. Command-line options of `installer\Install.ps1`:

- `-ScanOnly`: only inspect the computer;
- `-DryRun`: walk through the wizard without downloading;
- `-Only hy2,u2net`: install the listed components only (no optional questions; list them in install order). Ids: `node`, `npm`, `blender`, `comfy`, `comfydeps`, `hy2`, `hymv`, `hy21`, `sdxl`, `u2net`, `godot`, `clips`, `ollama`, `llm`, `vlm`, `mvadapter`, `pixal3d`, `pixal3dw`, `pixal3dtest`, `vfx`;
- `-Yes`: no questions (installs the required components and the wizard's defaults; Pixal3D is not installed by default).

Log: `installer\install.log`.

## Run and stop

1. Double-click **Start.cmd** (or the **AstraForge** desktop shortcut). The studio opens at **http://127.0.0.1:3210**. It runs its own ComfyUI on port 8190; wait for the Hunyuan3D status to appear.
2. Stop with **Stop.cmd**. It stops this studio's processes and leaves any other ComfyUI and Ollama you run untouched.

Do not delete `data` if you want to keep your projects, models and connections.

## Features

- **Create.** Upload a PNG/JPG/WEBP with one object on a neutral background, or type a prompt (SDXL makes the picture first). rembg removes the background on the CPU and Hunyuan3D builds the geometry.
- **Views.** From one front view (a character in a T-pose) the app creates left, back and right views, either through ChatGPT/Codex or locally with MV-Adapter (offline, about 40 s). "Create 3D from views" then builds the model with Hunyuan3D-2mv from all views. See `docs/forge/views.md` and `docs/forge/views_install.md`.
- **Pixal3D.** Optional shape generation from 1-4 views (geometry only, no textures). See `docs/forge/pixal3d.md`.
- **Editor.** Move, rotate and scale objects; colour, metalness, roughness; primitives; import GLB, OBJ and STL (GLB must embed its textures; external links are blocked).
- **Textures.** UV atlas that keeps weights, PBR maps up to 4K, painting on the model, reference colour projection. See `SURFACES-SPRITES-GODOT.md`.
- **Bones.** Humanoid, Quadruped, Mechanical and Custom skeleton templates, hierarchy editing, rest/pose, distance-based skinning, a weights brush and a library of named clips. See `GODOT-RIGGING.md`.
- **Animation and effects.** Keyframes, ready-made clips (Quaternius CC0, Mixamo), particles (sparks, engine trail, energy field), and a VFX texture workshop (`docs/vfx-tools.md`).
- **Forge.** A Blender-based pipeline for parts, items in hands, rigs, motion styles, kitbash and procedural sci-fi ships and turrets (`docs/forge/`).
- **Export.** GLB, OBJ, WebM, PNG, `.astra.json` projects, isometric sprite sheets (PNG + Godot SpriteFrames) and Godot packages. Export profiles: `godot`, `iso8`, `custom`. Everything is also saved to `data/exports`.
- **Tripo.** An optional tab that connects the official Tripo cloud API (generation, mesh processing, textures, auto-rig, motion, formats). It needs your own API key and credits. See `TRIPO-CAPABILITIES.md`.
- **AI assistant.** Local Ollama by default, or OpenAI / Anthropic with your own key. The assistant only returns a limited set of validated actions (material, transform, motion, effects) that you apply with a button. It never runs arbitrary code or OS commands.

Keyboard shortcuts: W move, E rotate, R scale, F frame, Space play, Delete remove, Ctrl+D duplicate, Ctrl+S save, Ctrl+Z undo, Ctrl+Shift+Z redo.

### Optional components

| Component | What it gives | Size | Requirements |
|---|---|---|---|
| **MV-Adapter** (`mvadapter`) | Builds left/back/right views from one front picture locally and offline. Node `huanngzh/ComfyUI-MVAdapter` (with AstraForge patches) goes to `data\runtime\custom_nodes`, weights to `data\models\mvadapter` | about 3.8 GB (+ SDXL 6.9 GB if missing) | NVIDIA, 10+ GB VRAM preferred |
| **qwen3-vl:8b** (`vlm`) | Local vision model for texture and animation checks (`ASTRA_QA_VLM` changes the model); installed through Ollama | 6.1 GB | Ollama |
| **Pixal3D** (`pixal3d` engine, `pixal3dw` weights, `pixal3dtest` smoke test) | A separate portable ComfyUI on port 8191 (`127.0.0.1` only) with TRELLIS.2 and Pixal3D. Default folder `runtime\ComfyUI-Pixal3D`; override with `ASTRA_PIXAL3D_DIR` (`pixal3dDir` in `config\paths.json`) | about 42 GB download, 48.5 GB on disk (52 GB free recommended) | NVIDIA RTX 30/40/50, 10+ GB VRAM (8 GB with a warning); short path without non-Latin characters |
| **VFX texture generators** (`vfx`) | Material Maker, PixaFlux, KumoWorks, Slate, FlowMap Painter, Pseudo Volume Generator and four offline web generators (`scripts\vfx\Install-VfxTools.ps1`, catalog `server\vfx-tools-catalog.json`) | about 330 MB download, 500 MB on disk | Node.js with npm for DisplacementX |

Pixal3D is installed by a pinned recipe: Python 3.12.10 (signature checked), ComfyUI 0.39.0 and `visualbruno/ComfyUI-Trellis2` at pinned commits, PyTorch 2.8.0+cu128, pinned PyPI packages (`installer\pixal3d\requirements-lock.txt`), FlashAttention 2.8.3, `utils3d` 1.7 from GitHub, and weights from Hugging Face verified with sha256. DINOv3 is taken only from the official timm copy and converted locally; the node's automatic download from a third-party mirror is disabled by the `ASTRAFORGE PATCH`.

## Licences

- **Hunyuan3D** models are distributed under the [Tencent Hunyuan Community License](https://github.com/Tencent-Hunyuan/Hunyuan3D-2), which **does not apply in the European Union, the United Kingdom and South Korea**. Do not use them there.
- **Pixal3D environment:** the Trellis2 node imports **nvdiffrast** and **nvdiffrec_render** (NVIDIA, **non-commercial licence**) and PyMeshLab (GPL-3.0). AstraForge does not call nvdiffrast, nvdiffrec_render or any texturing step. For commercial work, check these licences yourself and do not use the Trellis2/Pixal3D texturing nodes.
- Blender and ComfyUI are GPL; Quaternius / ActionForge animations are CC0.
- AstraForge itself is MIT (see `LICENSE`). Other licences are listed in `THIRD_PARTY_NOTICES.md`.

Generated results depend on the licence of the model that produced them. Check it before shipping anything commercially.

## AI and accounts

Local messages stay on your computer. If you pick a cloud provider (OpenAI or Anthropic), the message and a scene description are sent to its API, which requires your key and quota. Keys are encrypted with Windows DPAPI, stored on the server and never returned to the browser; `OPENAI_API_KEY` and `ANTHROPIC_API_KEY` are also supported. DPAPI keys are bound to your Windows user, so copying the folder to another user does not carry keys over. AI and generation share one GPU and run one after another.

## Limits

No sculpting, per-vertex editing, trained neural auto-rig, IK / foot contact, semantic segmentation or AI retopology locally; auto-rig, neural textures and mesh processing exist only as Tripo cloud operations. Local textures are procedural or projected. Heavy meshes need optimization; gzip projects are limited to 512 MB of unpacked JSON. This is not a replacement for a full DCC editor. Next-stage components are discussed in `RESEARCH-RIGGING-GODOT.md`; the architecture is in `ARCHITECTURE.md`.

## For AI agents

Skills for Claude Code, Codex and other agents are in `ai/skills/` (see `ai/skills/README.md`); `CLAUDE.md` holds the install procedure for Claude Code.

## Development

Node.js 22.12+; `npm install`, `npm run build`, `npm start` (or `npm run dev`). Tests: `npm test`. Logs: `data/runtime/server-error.log` and `data/runtime/comfy.log`. Point to an existing ComfyUI with `ASTRA_COMFY_DIR`. Without ComfyUI the editor still works, but neural generation is unavailable. Forge module docs are in `docs/forge/`.
