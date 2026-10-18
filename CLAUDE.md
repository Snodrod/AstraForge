# AstraForge: instructions for Claude Code

AstraForge is a local Windows 3D asset studio (image/text to 3D, rig, animation, effects, export). See `README.md` for what it does. This file tells you how to install and run it when the user says things like "install everything" or "set up AstraForge".

## Rules

- Never ask for API keys in chat and never write keys into files. OpenAI, Anthropic and Tripo keys are optional and the user enters them in the app's **Connections** tab (encrypted with Windows DPAPI).
- Never touch `data\` (projects, models, `vault.json`) when reinstalling.
- No administrator rights are needed. Do not run heavy GPU jobs unless asked.
- Use a short Latin-only install path (for example `C:\AstraForge`). If the clone is in a long or non-Latin path, tell the user and suggest moving it before installing.

## Install procedure

`installer\Install.ps1` is an interactive wizard (`Read-Host`). **Do not run it without flags inside Claude Code: it will hang.** Use these steps instead.

1. **Scan, no downloads:**
   `powershell -NoProfile -ExecutionPolicy Bypass -File installer\Install.ps1 -ScanOnly`
   It reports the GPU and VRAM, free disk space and what is already installed.
2. **Show the plan and ask in chat** which optional components to install. Give each size and licence (table below), and warn about licences: Hunyuan3D is not valid in the EU, UK and South Korea; the Pixal3D environment bundles NVIDIA nvdiffrast / nvdiffrec_render under a non-commercial licence (the studio does not call them). Respect the hardware limits: NVIDIA GPU with 8 GB VRAM minimum (12+ recommended), Pixal3D needs RTX 30/40/50 with 10+ GB VRAM and about 52 GB free disk; 16 GB RAM minimum.
3. **Install unattended, in the background** (downloads take more than 10 minutes):
   `powershell -NoProfile -ExecutionPolicy Bypass -File installer\Install.ps1 -Yes -Only node,npm,blender,comfy,comfydeps,hy2,u2net,<chosen ids>`
   Run it with `run_in_background`, and follow progress with `Get-Content installer\install.log -Tail 30`. If a download is interrupted, rerun the same command: it resumes. `-Yes` alone installs the required components plus the wizard's defaults (Pixal3D is never a default). `-DryRun` walks through without downloading.
4. **Verify:** run `-ScanOnly` again and read `config\paths.json` (paths to Node, Blender, ComfyUI, Godot, and `pixal3dDir` if installed). Optionally run `npm test`.
5. **Start the studio:** run `Start.cmd` (or the AstraForge desktop shortcut), then check `http://127.0.0.1:3210/api/health`; the response must contain `"app":"AstraForge"`. The studio's own ComfyUI runs on 8190 and Pixal3D on 8191. Stop with `Stop.cmd`.
6. **Troubleshooting:** read `installer\install.log`, `data\runtime\server-error.log` and `data\runtime\comfy.log`; reinstall only the broken component with `Install.ps1 -Yes -Only <id>`.

The interactive launcher `Install AstraForge.cmd` is for people installing by hand; the user may use it instead of the steps above.

## Component ids

| id | What | Size | Notes |
|---|---|---|---|
| `node`, `npm` | Node.js 22 and UI build | about 0.3 GB | required |
| `blender` | Blender 5.0.1 | about 0.4 GB | required, GPL |
| `comfy`, `comfydeps` | ComfyUI 0.33.2, Python 3.12, PyTorch | about 6 GB | required, GPL |
| `hy2`, `u2net` | Hunyuan3D-2 and background removal | 4.9 GB + 0.2 GB | required; Tencent Hunyuan Community License |
| `hymv` | Hunyuan3D-2mv (3D from several views) | 4.9 GB | optional |
| `hy21` | Hunyuan3D-2.1 | 7.4 GB | optional |
| `sdxl` | SDXL, text to image | 6.9 GB | optional |
| `mvadapter` | MV-Adapter, views from one picture | about 3.8 GB | needs `sdxl`; 10+ GB VRAM preferred |
| `godot` | Godot 4.5.1 for package validation | 77 MB | optional |
| `clips` | Quaternius animations (CC0) | 10 MB | needs Blender |
| `ollama`, `llm` | local Qwen3 assistant; the installer picks qwen3:4b / 8b / 14b by VRAM | 2.5-9 GB | optional |
| `vlm` | qwen3-vl:8b texture and animation checks | 6.1 GB | needs Ollama |
| `vfx` | VFX texture generators | about 0.5 GB | optional |
| `pixal3d`, `pixal3dw`, `pixal3dtest` | Pixal3D (shape from 1-4 views), then weights, then smoke test | about 48.5 GB | RTX 30/40/50, 10+ GB VRAM; NVIDIA non-commercial libs |

List ids in install order. When the user just says "install everything useful", propose the required set plus `hymv,godot,clips,ollama,llm` and ask about the large items (`hy21`, `sdxl`, `mvadapter`, `vlm`, `vfx`, Pixal3D) separately.

## Deeper help

- `ai/skills/` has two skills for AI agents: `astraforge` (install, components, studio CLI, troubleshooting, `scripts/af_status.ps1` health check) and `astraforge-vfx` (effects and VFX textures). To make them visible to Claude Code in every project, link them (cmd, no admin rights needed):
  ```bat
  mklink /J "%USERPROFILE%\.claude\skills\astraforge" "<AstraForge folder>\ai\skills\astraforge"
  mklink /J "%USERPROFILE%\.claude\skills\astraforge-vfx" "<AstraForge folder>\ai\skills\astraforge-vfx"
  ```
- `docs/forge/` documents the Blender "Forge" pipeline, `ARCHITECTURE.md` the app, `GODOT-RIGGING.md` and `SURFACES-SPRITES-GODOT.md` the Godot hand-off.

## Development

Node.js 22.12+. `npm install`, `npm run build`, `npm start` (or `npm run dev`); tests with `npm test`. Keep generated models, `data\`, `runtime\` and secrets out of git.
