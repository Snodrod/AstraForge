# AstraForge: component inventory (what, which version, from where, to where, who uses it)

Sources: `installer\Install.ps1` (APP = the folder the repository is cloned into), `installer\pixal3d\manifest.json`,
`installer\pixal3d\requirements-lock.txt`, `installer\mvadapter\manifest.json`, `THIRD_PARTY_NOTICES.md`, `scripts\vfx\Install-VfxTools.ps1`,
`server\vfx-tools-catalog.json`, `package.json`, `server\*.mjs`. Links and sizes are taken from the files; **link liveness was not checked on 2026-10-17**
(downloading while compiling the inventory was forbidden). "Wizard" = `installer\Install.ps1`; the component id is what `-Only` takes (see `install.md`).
Size is "download / on disk"; sha256 is given where the installer files record it (for the rest the wizard checks only the size).

Default install location: `<APP>` is the application folder (by default the wizard installs into it: `<APP>\runtime\...`, `<APP>\data\models\...`).
"Reference machine" below means the machine the numbers were measured on; its own folders are not listed.

## 1. Programs

| id | Component | Version / tag | From (URL) | Size | License | To | Used by / port |
|---|---|---|---|---|---|---|---|
| `node` | Node.js | v22.23.3 (>= 22.12 required; the reference machine has system v26.7.0) | `https://nodejs.org/dist/v22.23.3/node-v22.23.3-win-x64.zip` | ~ 32 MB | MIT | `<APP>\runtime\node\node.exe` | studio server `server\index.mjs`, CLI `scripts\forge.mjs`; port 3210 |
| `npm` | npm dependencies + UI build | `package-lock.json`; `npm ci` + `npm run build` (Vite 7) | npmjs.com | ~ 250 MB | MIT/ISC/Apache/BSD of the packages | `<APP>\node_modules`, `<APP>\dist` | UI and server |
| `blender` | Blender | 5.0.1 (build 2025-12-16) | `https://download.blender.org/release/Blender5.0/blender-5.0.1-windows-x64.zip` | ~ 401 MB | GPL-3.0-or-later | `<APP>\runtime\blender\...\blender.exe` (or a system install such as `C:\Program Files\Blender Foundation\Blender 5.0\`) | every Forge step (`af_run.py`), sprite rendering; variable `ASTRA_BLENDER` |
| `godot` | Godot | 4.5.1-stable (only this one; not 4.7) | `https://github.com/godotengine/godot/releases/download/4.5.1-stable/Godot_v4.5.1-stable_win64.exe.zip` | 76,685,794 B | MIT | `<APP>\runtime\godot` (or any folder pointed to by `ASTRA_GODOT`) | windowless checking of `godot_package`/`render_sprites` packages; `ASTRA_GODOT` |
| - | uv (installed by the wizard itself for `comfy`/`comfydeps`/`mvadapter`) | **latest** (not pinned) | `https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip` | - | MIT/Apache-2.0 | `<APP>\runtime\uv\uv.exe`; Python in `runtime\python`, cache in `runtime\uv-cache` | creates the Python 3.12 `.venv` for ComfyUI, installs packages |
| `comfy` | The studio's ComfyUI | v0.33.2 | `https://github.com/comfyanonymous/ComfyUI/archive/refs/tags/v0.33.2.zip` | ~ 6 GB including Python 3.12 and PyTorch (wizard estimate) | GPL-3.0 | `<APP>\runtime\ComfyUI` (key `comfyDir` can point elsewhere) | Hunyuan3D, SDXL, MV-Adapter; **port 8190** (started by the studio itself, see below) |
| `comfydeps` | ComfyUI Python packages | PyTorch **unpinned**: `torch torchvision torchaudio` from `https://download.pytorch.org/whl/cu128` (no NVIDIA - `.../cpu`); then ComfyUI's `requirements.txt` and `rembg onnxruntime numba scikit-image xatlas` (also unpinned) | pytorch.org, PyPI | ~ 1 GB (if ComfyUI is already there) | BSD-3 / MIT / Apache | the ComfyUI `.venv` | on the reference machine in the ComfyUI `.venv`: Python 3.12.11, torch 2.11.0+cu128, torchvision 0.26.0, torchaudio 2.11.0, numpy 2.5.2, transformers 5.15.1, huggingface_hub 1.28.0, diffusers 0.40.0, accelerate 1.15.0, peft 0.21.2, timm 1.0.30, trimesh 5.1.1, rembg 2.0.81, onnxruntime 1.29.0, numba 0.67.0, scikit-image 0.26.0 |
| `ollama` | Ollama | 0.40.4 on the reference machine | `https://ollama.com/download/OllamaSetup.exe` (silent install `/VERYSILENT /NORESTART /SUPPRESSMSGBOXES`) | 1,578,217,944 B | MIT | `%LOCALAPPDATA%\Programs\Ollama\ollama.exe`; models in `OLLAMA_MODELS` | LLM assistant, qwen3-vl checks; **port 11434** |
| `pixal3d` | ComfyUI-Pixal3D (engine) | see section 3 | see section 3 | download ~ 5.5 GB, ~ 12 GB on disk (estimate) | MIT (Pixal3D, TRELLIS.2), BSD-3 (PyTorch, FlashAttention) + special ones (section 3) | `<APP>\runtime\ComfyUI-Pixal3D` (key `pixal3dDir` can point elsewhere) | shape from 1-4 views; **port 8191** |
| `vfx` | VFX texture generators | a catalog of 31 entries (section 7) | see section 7 | ~ 330 MB (wizard estimate; 495 MB on disk on the reference machine) | various | `<APP>\runtime\vfx` (key `vfxTools`; can point elsewhere) | the "VFX" tab; web generators on **port 3211** |
| - | The studio's own ComfyUI node `AstraForgeMesh` (Marching Cubes) | in the repository | `data\runtime\custom_nodes\AstraForgeMesh\__init__.py` (**do not lose it**) | small | MIT (AstraForge code) | in git (exception in `.gitignore`: `!data/runtime/custom_nodes/`) | surface extraction for Hunyuan3D |
| `mvadapter` | ComfyUI-MVAdapter node + packages | 1.0.2, commit `9d6d8afeff135d81a31f16dde5291d8b40dbb0a4` | `https://github.com/huanngzh/ComfyUI-MVAdapter/archive/9d6d8afeff135d81a31f16dde5291d8b40dbb0a4.zip` + AstraForge patches from `installer\mvadapter\patch` (`astra_views.py`, `utils.py`, `nodes.py`, `__init__.py`) | ~ 0.2 GB (node + packages) | Apache-2.0 | `<APP>\data\runtime\custom_nodes\ComfyUI-MVAdapter` (a git clone already contains it whole, patches included; it is not in Setup.exe - the wizard installs it again) | views locally; whitelisted in the studio's ComfyUI launch |

MV-Adapter Python packages (in the studio ComfyUI's `.venv`, **without upgrading ones already installed**, constraints = the current `pip freeze`):
`diffusers==0.40.0`, `accelerate==1.15.0`, `peft==0.21.2`, `timm==1.0.30`, `trimesh==5.1.1`, `antlr4-python3-runtime==4.9.3`,
`importlib_metadata==9.0.1`, `zipp==4.1.1`, plus, unversioned, `omegaconf einops kornia scikit-image opencv-python-headless safetensors`.
The SDXL configs (without weights) are built by `scripts\views\make_sdxl_config.py` into `<APP>\data\models\mvadapter\sdxl-config`.

How the studio starts its own ComfyUI (`server\runtime.mjs`): `python.exe main.py --listen 127.0.0.1 --port 8190 --extra-model-paths-config data\runtime\models.yaml
--output-directory/--input-directory/--temp-directory/--user-directory data\runtime\... --database-url sqlite:///data/runtime/studio.db
--disable-all-custom-nodes --whitelist-custom-nodes AstraForgeMesh ComfyUI-MVAdapter --lowvram --reserve-vram 1`, with the variables
`HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`. So the code and `.venv` come from `ASTRA_COMFY_DIR`, while the working folders, database
and nodes are the studio's own, in `<APP>\data\runtime`; **the user's custom nodes and port 8188 are not touched**.

## 2. Hunyuan3D weights, image weights and auxiliary files

| id | What | URL | To | Size (bytes) | License |
|---|---|---|---|---|---|
| `hy2` | Hunyuan3D-2 (shape from an image; mandatory) | `https://huggingface.co/tencent/Hunyuan3D-2/resolve/main/hunyuan3d-dit-v2-0/model.fp16.safetensors` | `data\models\checkpoints\hunyuan3d-dit-v2.safetensors` | 4,928,151,562 | Tencent Hunyuan Community License (**not valid in the EU, the United Kingdom, South Korea**; > 1 million monthly users requires a Tencent license) |
| `hymv` | Hunyuan3D-2mv (from several views) | `https://huggingface.co/tencent/Hunyuan3D-2mv/resolve/main/hunyuan3d-dit-v2-mv/model.fp16.safetensors` | `data\models\checkpoints\hunyuan3d-dit-v2-mv.safetensors` | 4,928,151,562 | the same |
| `hy21` | Hunyuan3D-2.1 (Comfy-Org format; selected with `shapeModel: hunyuan21`) | `https://huggingface.co/Comfy-Org/hunyuan3D_2.1_repackaged/resolve/main/hunyuan_3d_v2.1.safetensors` | `data\models\checkpoints\hunyuan_3d_v2.1.safetensors` | 7,365,943,290 | the same (`THIRD_PARTY_NOTICES.md` gives the source as huggingface.co/tencent) |
| `sdxl` | SDXL base 1.0 (concept from text; MV-Adapter base) | `https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors` | `data\models\checkpoints\` **or** `<comfyDir>\models\checkpoints\` (the studio sees both) | 6,938,078,334 | CreativeML Open RAIL++-M |
| `u2net` | u2net.onnx (rembg: background removal on CPU) | `https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx` | `data\models\background\models\u2net\u2net.onnx` (the folder is set by `U2NET_HOME`) | 175,997,641 | Apache-2.0 per `THIRD_PARTY_NOTICES.md` (the wizard prints "MIT" in its plan - a discrepancy) |
| `mvadapter` | `mvadapter_i2mv_sdxl.safetensors` (repository `huanngzh/mv-adapter`) | `https://huggingface.co/huanngzh/mv-adapter/resolve/main/mvadapter_i2mv_sdxl.safetensors` | `data\models\mvadapter\` | 3,602,537,816, sha256 `260e486d507247db30601d22de317e3f9c07f75a29912d43ed5c3a4aab4db4c9` | Apache-2.0 |

For `hy2`/`hymv`/`hy21`/`sdxl`/`u2net` the wizard checks **only the size** (no sha256 is recorded in `Install.ps1`; `hy2` and `hymv` have the same size).
Download: `curl.exe -L --fail --retry 5 -C -` into a `.part` file, then rename (an interrupted download resumes).

## 3. Pixal3D (TRELLIS.2): a separate portable ComfyUI, port 8191, shape ONLY

All versions and hashes are in `installer\pixal3d\manifest.json` (generated 2026-10-14; it reproduces a reference `ComfyUI-Pixal3D` install).
Installed by `installer\pixal3d\pixal3d.ps1`; the on-disk description is `ASTRAFORGE_README.md` in the install folder. The post-install check is `P3Smoke`
(`installer\pixal3d\smoke_test.py`, a trial start on 8191 and a stop).

| Component | Version / commit | URL | Size / sha256 | License |
|---|---|---|---|---|
| Python (embed) | 3.12.10, the "Python Software Foundation" signature is verified | `https://www.python.org/ftp/python/3.12.10/python-3.12.10-embed-amd64.zip`; headers for triton - `.../3.12.10/amd64/dev.msi` | - | PSF |
| pip | 26.2.1 (wheel from PyPI, sha256 from the PyPI response) | `https://pypi.org/pypi/pip/26.2.1/json` | - | MIT |
| ComfyUI | 0.39.0, commit `8010b48d2c1be986613f8e8c761527d205bd756f` | `https://github.com/Comfy-Org/ComfyUI/archive/8010b48d2c1be986613f8e8c761527d205bd756f.zip` | - | GPL-3.0 |
| PyTorch | torch 2.8.0, torchvision 0.23.0, torchaudio 2.8.0 (+cu128) | index `https://download.pytorch.org/whl/cu128` | ~ 3.5 GB | BSD-3 |
| FlashAttention | 2.8.3 (kingbri1 wheel, cu128 torch2.8.0 cp312) | `https://github.com/kingbri1/flash-attention/releases/download/v2.8.3/flash_attn-2.8.3+cu128torch2.8.0cxx11abiFALSE-cp312-cp312-win_amd64.whl` | 127,588,011 B, `709d6663d74f96bb0304b5ba2ffee84a4dafbcb56d73d35bc90b7ca8cd44261b` | BSD-3 |
| ComfyUI-Trellis2 (visualbruno) | 1.0.27, commit `b0e84c0f29857a444e8bcb5b9be2ea218e96efd0` | `https://github.com/visualbruno/ComfyUI-Trellis2/archive/b0e84c0f29857a444e8bcb5b9be2ea218e96efd0.zip` (inside `wheels/Windows/Torch280`) | - | MIT |
| node wheels | cumesh 1.0, flex_gemm 0.0.1, o_voxel 0.0.1, nvdiffrast 0.4.0, nvdiffrec_render 0.0.0, natten 0.21.6 (cp312 win_amd64) | in the node archive | sha256: cumesh `fc5124db...6de4`, nvdiffrast `e19fd7a3...f208`, nvdiffrec_render `4318a7c0...325e`, flex_gemm `fa0da34b...f004`, o_voxel `b9cb2d30...f2e4`, natten `1ba8ca20...4451` (for Blackwell, RTX 50, compute >= 12: folder `Blackwell`, `546e6365...aec10`) | see below |
| utils3d | 1.7, commit `83bfa5bda008803f6521ce9547f94023b8afeddd` - **GitHub EasternJournalist, not the PyPI package of the same name** | `https://github.com/EasternJournalist/utils3d/archive/83bfa5bda008803f6521ce9547f94023b8afeddd.zip` (installed with `--no-deps --no-build-isolation`) | - | MIT |
| PyPI packages | 145 pinned packages | `installer\pixal3d\requirements-lock.txt`, `pip install --no-deps -r` | ~ 1.3 GB | MIT/Apache/BSD (among them `pymeshlab` 2025.7.post1 - GPL-3.0) |
| Node patch | `patch_trellis2.py` marks `nodes.py` with the line `ASTRAFORGE PATCH`: the auto-download of DINOv3 from a third-party mirror is replaced by an error; `meshlib` is made optional (holes are filled by CuMesh) | - | - | - |

Key pinned packages (from the lock): `numpy==2.5.2`, `transformers==4.57.6`, `timm==1.0.30`, `huggingface_hub==0.36.2`, `triton-windows==3.4.0.post21`,
`open3d==0.20.0`, `rembg==2.0.85`, `onnxruntime-gpu==1.31.0`, `opencv-python==5.0.0.93`, `trimesh==5.1.1`, `kornia==0.8.3`, `safetensors==0.8.0`, `Pillow 12.3.0`.

**Pixal3D weights** (about 36.5 GB in total, about 46 GB on disk together with the engine; sha256 is checked against the Hugging Face file listing, `HfTree`):

| File (under `ComfyUI\models\...`) | Repository | Size (bytes) | sha256 |
|---|---|---|---|
| `TencentARC\Pixal3D\ckpts\ss_dec_conv3d_16l8_fp16.safetensors` (copy in `microsoft\TRELLIS-image-large\ckpts\`) | `TencentARC/Pixal3D` | 147,591,972 | `1c76d4a40519aa2d711cc263a8404105231ac26db31d946bed48b84fee79009a` |
| `...\shape_dec_next_dc_f16c32_fp16.safetensors` | same | 948,490,494 | `e3b718d3e43e4f8780e9a24ac6fff231811a67e3b058e336e10fe654c911d581` |
| `...\ss_flow_img_dit_1_3B_64_bf16.safetensors` | same | 5,359,822,584 | `6d0147bb347eb61fa81c304cc68c7fd69d447e91870357a549b47e1d0fd77242` |
| `...\ss_flow_img_dit_1_3B_64_bf16_mv.safetensors` | same | 5,359,822,584 | `e7ce63050e317cff70ed2c6dfcc549e668cd948c47347f31985fd4996b4ce5a6` |
| `...\slat_flow_img2shape_dit_1_3B_1024_bf16.safetensors` | same | 5,546,764,048 | `3e6d1df0e62bc5bc863320d1d63d6b996235eef4abb8b851e878c88f316369f1` |
| `...\slat_flow_img2shape_dit_1_3B_1024_bf16_mv.safetensors` | same | 5,546,764,048 | `37b75c8f0f089a7e3e825dec035ac83b9c989efd14d0c8ada6a620d2be561c71` |
| `...\slat_flow_img2shape_dit_1_3B_512_bf16.safetensors` | same | 5,546,764,048 | `63569838c1da93f010e2fa805b202ecf802cc758830230abac202a81b70e5b45` |
| `...\slat_flow_img2shape_dit_1_3B_512_bf16_mv.safetensors` | same | 5,546,764,048 | `043a0d7fc7ee81792fcb22f836c91f5aaf69f4fda15e25c38488db13dd7b77ec` |
| `Ruicheng\moge-2-vitl\model.pt` | `Ruicheng/moge-2-vitl` (MIT) | 1,305,030,700 | `3eefd4abb2102f38f12b2d1992e5ff15e4923e5431c67dd494afe157e0111cd5` |
| `timm\vit_large_patch16_dinov3.lvd1689m\model.safetensors` -> converted by `tools\dinov3_timm_to_hf.py` into `facebook\dinov3-vitl16-pretrain-lvd1689m\model.safetensors` (match tolerance 1e-3, report `logs\dinov3_parity.json`) | `timm/vit_large_patch16_dinov3.lvd1689m` - **the official timm copy only** | 1,212,347,640 | `45172f209c9583c40538afc26b60a07033e6fcc2e8c30228338e6b2e932e7941` |
| NAF `cache\torch\hub\checkpoints\naf_release.pth` + code `cache\torch\hub\valeoai_NAF_main` (commit `37f2dfc180f2de53d98bd601109c0da0dd6b0f43`) | `https://github.com/valeoai/NAF/releases/download/model/naf_release.pth` | 2,664,431 | `c096c1ab2217a5c3ac136365f721685e2201379cb69d509cfb0261183847c98f` |
| auxiliary json/README/LICENSE/NOTICE/pipeline*.json of Pixal3D | `TencentARC/Pixal3D` | small | - |

Pixal3D licenses: Pixal3D and TRELLIS.2, the node wrapper, MoGe - MIT; NAF - Apache-2.0; DINOv3 - the DINOv3 license (royalty-free, commercial use
allowed, military purposes and violation of trade restrictions forbidden; include `LICENSE.md` when passing the weights on). **nvdiffrast and nvdiffrec_render (NVIDIA) are
under a non-commercial license**: they are installed only because the node imports them on load; AstraForge never calls them (the graph has no nodes `Texturing`,
`Rasteriz`, `Render`, `UnWrap`, `nvdiff` - this is checked by `tests\pixal3d.test.mjs`). `meshlib` (non-commercial) is not installed. For commercial work, check the licenses yourself.

## 4. Local LLMs (Ollama)

| Model | Purpose | From | Size | License |
|---|---|---|---|---|
| `qwen3:4b` / `qwen3:8b` / `qwen3:14b` (id `llm`) | **the studio assistant model**, picked by the wizard by VRAM (< 8 GB / 8–16 GB / 16+ GB) and written to `data\config.json`; the default in `server\schema.mjs` is `qwen3:8b`; the "AI" tab, translation of prompt text | `ollama pull <name>` | 2.5 / 5.2 / 9.3 GB | Apache-2.0 (Qwen3) |
| `qwen3-vl:8b` (id `vlm`) | the "vision" model: texture checks (`paint_from_concept`, `qa_model`) and animation checks (`_tools\vlm_check.py`); variables `ASTRA_QA_VLM`, `ASTRA_OLLAMA_URL` | `ollama pull qwen3-vl:8b` | 6.1 GB per the wizard (6.2 GB in `ollama list`) | Apache-2.0 |
| `qwen3:4b` / `qwen3:8b` / `qwen3:14b` (id `llm`) | what the wizard **suggests** for a new PC: < 8000 MiB VRAM -> 4b (2500 MB), >= 8000 -> 8b (5200 MB), >= 16000 -> 14b (9300 MB); it installs the model and writes it into `data\config.json` (`provider: ollama`) | `ollama pull <name>` | see left | Apache-2.0 |

Cloud providers (OpenAI, Anthropic) and Tripo are connected with keys in "Connections" (DPAPI store `data\vault.json`, bound to the Windows user). Paid
Tripo operations are not run without the user's decision.

## 5. Clip libraries and kits

| What | From | Size / contents | License | To |
|---|---|---|---|---|
| ActionForge animations (Quaternius), id `clips` | manifest `https://actionforge.app/assets/animation-library/manifests/library.json`, files `.../animation-library/<file>`; the wizard converts them to FBX (Blender, `installer\af_clips_to_fbx.py --library`) and imports them with the `clip_library` step | 86 clips, ~ 10 MB (sword, shield, magic, bow, death, etc.) | CC0 1.0 (the site code is closed (c) CGstuff - do not copy it) | `<library>\animations\quaternius\` (id `quaternius-<clip>`); clips are "in place" (root zeroed) |
| Mixamo animations | **manual only**: mixamo.com, an Adobe account, FBX Binary, **Without Skin**, 30 fps, Keyframe Reduction none; the studio never goes to the network for them | 38 clips on the reference machine (`<library>\animations\mixamo\`, id `mixamo-<name>`) | Adobe: free for games and films, **do not redistribute the FBX files themselves** | `clips import <folder>` or drag-and-drop in the "Forge -> Motions" tab |
| Quaternius Universal Base Characters [Standard], Modular Character Outfits - Fantasy [Standard], Universal Animation Library 2 [Standard] | the archives are supplied by the user; pack page `https://quaternius.com/packs/universalbasecharacters.html` | character base (65 bones with fingers), hairstyles, clothing | CC0 1.0 (`kits\quaternius\LICENSE.md`) | `<library>\kits\quaternius\...`, skeleton `<library>\rigs\quaternius-human\` (71 bones), items `<library>\props\actionforge\` |
| Human Base Meshes v1.4.1 (Blender Studio) | outside git, for rig and unit tests | - | CC0 | a folder of your choice (not part of the repository) |

## 6. Pictures: views, concepts

| What | How it works | Requirements |
|---|---|---|
| ChatGPT through the Codex app (`OpenAI.Codex` from the Microsoft Store, 26.1007.9446.0 on the reference machine) | `scripts\views\codex_image.mjs` calls `codex exec` (sign-in via ChatGPT, no API key) and takes the PNG from `~\.codex\generated_images\`; ~ 57-65 s per view; the picture goes to OpenAI; the `codex-image` skill does the same for ordinary pictures | a ChatGPT subscription, `codex.exe` is not in PATH (found via `Get-AppxPackage`) |
| Local MV-Adapter | SDXL + `mvadapter_i2mv_sdxl`, ~ 34 s for all views (640 px, 30 steps), peak ~ 9.7 GB VRAM | `mvadapter` + `sdxl` |
| SDXL (concept from text) | called inside the generation job | `sdxl` |

## 7. VFX generators ("VFX" tab, id `vfx`)

The catalog is `server\vfx-tools-catalog.json` (31 entries: desktop 3, installer 3, web-local 4, file 1, web-online 5, store 4, paid 7, guide 4; source -
Simon Schreibt "Textures for VFX", the Texture Generators section). Downloaded by `scripts\vfx\Install-VfxTools.ps1 [-Root] [-Only a,b] [-DryRun]` into `ASTRA_VFX_TOOLS`
(default `C:\AstraForge\tools\vfx`). Details and scenarios are in the **`astraforge-vfx`** skill (only the composition is listed here).

| id | Version | Download URL | License | To |
|---|---|---|---|---|
| material-maker | 1.7 | `https://github.com/RodZill4/material-maker/releases/download/1.7/material_maker_1_7_windows.zip` | MIT | `apps\material-maker` |
| flowmap-painter | 0.9.2 | `https://teckArtist.com/ToolsnStuff/FlowMapPainter-0.9.2-win32.zip` | freeware | `apps\flowmap-painter` |
| pseudo-volume | 2.7 | manually from `https://acetix.itch.io/pseudo-volume-generator` ("Download Now -> No thanks"), put `3D Volume Generator 2.7 Win.zip` into `downloads\` | "name your price" | `apps\pseudo-volume` |
| pixaflux | build 20260528 | `http://pixaflux.com/bin/PixaFlux.20260528.exe` (+ `.../blender/pixaflux.zip`, `.../wiki/samples/Samples.zip`) | freeware | `downloads\` (the user runs the installer) |
| kumoworks | 1.0.1 | `https://github.com/opentoonz/kumoworks/releases/download/v1.0.1/KumoWorksSetup.exe` | BSD-3 | `downloads\` |
| slate | - | `https://s3.eu-west-2.amazonaws.com/facedownfx/Slate/Builds/Slate_Installer.zip` | freeware | `downloads\` |
| texture-generator-online, normalmap-online | gh-pages branch | archives `github.com/cpetry/TextureGenerator-Online`, `.../NormalMap-Online` | MIT | `web\texture-generator`, `web\normalmap` |
| displacementx (Greeble) | main branch, built with Astro via npm | `github.com/satelllte/displacementx` | GPL-3.0 | `web\displacementx-src` -> `web\displacementx` |
| neonflames | master | `github.com/jwagner/Neonflames` | **no license: personal use only, do not include in builds** | `web\neonflames` |
| impact-decal | id 1008 | `https://www.materialmaker.org/api/getMaterial?id=1008` | CC0 | `apps\material-maker-files\impact_decal.ptex` |

The other 17 entries (online generators, stores, paid Substance Designer packs, articles) are links only; unavailable on 2026-10-16: Fateloom, four Flipped Normals products (404).

## 8. Cross-cutting license constraints

- AstraForge is MIT (`LICENSE`); models created in the studio belong to the user.
- Hunyuan3D: Tencent Hunyuan Community License (the EU/United Kingdom/South Korea are excluded). Pixal3D is MIT, but the non-commercial nvdiffrast/nvdiffrec_render and GPL-3.0 PyMeshLab sit in its environment.
- Blender and ComfyUI are GPL; the studio's scripts run inside them, and MIT is compatible with GPL; distributing a build with bundled Blender/ComfyUI falls under the GPL.
- Do not redistribute Mixamo FBX files; only baked frames/exports go into a game.
- Asset-license rules and quotas of a target game's repository are defined by that repository.
