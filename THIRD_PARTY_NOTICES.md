# Third-party components

AstraForge is distributed under the MIT license (file `LICENSE`). This means it can also be used
commercially. Models created in the studio belong to the user.

The items listed below are **not included** in the archive. The installer downloads these components from their official sites to the
user's computer, showing their licenses beforehand. The components are distributed under their own licenses.

| Component | Source | License |
|---|---|---|
| Node.js | nodejs.org | MIT |
| npm packages (express, react, three, zod, etc., the `node_modules` folder) | npmjs.com | MIT / ISC / Apache-2.0 / BSD (see `node_modules/*/LICENSE`) |
| Blender 5.0 | blender.org | GPL-3.0-or-later |
| ComfyUI (including version 0.39.0 for Pixal3D) | github.com/comfyanonymous/ComfyUI (now Comfy-Org/ComfyUI) | GPL-3.0 |
| uv, managed Python 3.12 | astral.sh, python.org | MIT / Apache-2.0; PSF License |
| PyTorch | pytorch.org | BSD-3-Clause |
| rembg, onnxruntime, numba, scikit-image, xatlas | PyPI | MIT / BSD |
| u2net model | github.com/danielgatis/rembg | Apache-2.0 |
| Hunyuan3D-2, 2mv, 2.1 models | huggingface.co/tencent | Tencent Hunyuan Community License: **does not apply in the EU, the United Kingdom and South Korea**; with an audience above 1 million monthly users a separate Tencent license is required |
| SDXL base 1.0 model | huggingface.co/stabilityai | CreativeML Open RAIL++-M |
| MV-Adapter weights (`mvadapter_i2mv_sdxl.safetensors`, views from a single image) | huggingface.co/huanngzh/mv-adapter | Apache-2.0 |
| diffusers, peft, accelerate, timm, trimesh, antlr4-python3-runtime (Python packages in the ComfyUI environment for MV-Adapter) | PyPI | Apache-2.0 / MIT / BSD |
| Godot 4.5.1 | godotengine.org | MIT |
| Ollama | ollama.com | MIT |
| Qwen3 and Qwen3-VL models (qwen3-vl:8b, texture and animation checks) | ollama.com/library/qwen3, ollama.com/library/qwen3-vl | Apache-2.0 |
| Quaternius animations (ActionForge library) | actionforge.app | CC0 1.0 |
| **Pixal3D** (code and "shape only" weights) | huggingface.co/TencentARC/Pixal3D | MIT (Tencent) |
| TRELLIS.2 and the ComfyUI-Trellis2 node wrapper (visualbruno), pinned commit; the `ss_dec_conv3d_16l8_fp16` decoder from microsoft/TRELLIS-image-large | github.com/visualbruno/ComfyUI-Trellis2, huggingface.co/microsoft | MIT |
| MoGe-2 ViT-L (`Ruicheng/moge-2-vitl`, camera from a single view) | huggingface.co/Ruicheng/moge-2-vitl | MIT |
| NAF (feature upsampler; release code and weights) | github.com/valeoai/NAF | Apache-2.0 |
| **DINOv3 ViT-L/16** (official timm copy `timm/vit_large_patch16_dinov3.lvd1689m`, converted to the transformers format) | huggingface.co/timm, Meta | **DINOv3 license**: royalty-free, commercial use allowed; military purposes and violation of trade restrictions are prohibited; when passing on the weights or derivatives the license text must be included (it sits next to the model, `LICENSE.md`) |
| utils3d 1.7 (GitHub `EasternJournalist/utils3d`, not the PyPI package of the same name) | github.com/EasternJournalist/utils3d | MIT |
| FlashAttention 2.8.3 (prebuilt wheel by kingbri1), triton-windows, cumesh, flex_gemm, o_voxel, NATTEN | github.com, PyPI | BSD-3-Clause / MIT |
| **nvdiffrast** and **nvdiffrec_render** (installed by the Trellis2 nodes, AstraForge does **not use** them) | NVIDIA | **NVIDIA non-commercial license** |
| meshlib (MeshLib, AMV Consulting) | — | **not installed and not used**: non-commercial license only; the Trellis2 nodes are patched to load without it, and holes are filled by CuMesh (MIT) |
| PyMeshLab (required by the Trellis2 nodes at load time) | PyPI | GPL-3.0 |
| Other Pixal3D engine packages (list and versions: `installer/pixal3d/requirements-lock.txt`): open3d, rembg, onnxruntime-gpu, opencv-python, trimesh, transformers, timm, etc. | PyPI | MIT / Apache-2.0 / BSD |
| Python 3.12.10 (portable build, headers from dev.msi) | python.org | PSF License |

The ComfyUI-MVAdapter node (github.com/huanngzh/ComfyUI-MVAdapter, Apache-2.0) is installed by the wizard into `data/runtime/custom_nodes/ComfyUI-MVAdapter`: the original from GitHub at a pinned commit plus small AstraForge edits (files in `installer/mvadapter/patch`, list in `NOTICE-ASTRAFORGE.md`).

**Pixal3D (optional component).** The engine is installed into a separate folder (`runtime/ComfyUI-Pixal3D` by default) and runs as a separate program on port 8191. At load time the Trellis2 nodes import libraries with special licenses: `nvdiffrast` and `nvdiffrec_render` (NVIDIA, non-commercial license), `meshlib` (non-commercial and educational use) and `pymeshlab` (GPL-3.0). Therefore installation without them is not possible, but AstraForge:

- does not call nvdiffrast, nvdiffrec_render or the Trellis2/Pixal3D texturing (only the shape is used; the preview is rendered in Blender);
- does not call meshlib: hole filling in the Pixal3D graph is done by `Trellis2FillHolesWithCuMesh` (CuMesh, MIT); the installer does not install the meshlib package (`installer/pixal3d/patch_trellis2.py` makes its import optional).

If you use the result in commercial work, check these licenses yourself. Automatic downloading of DINOv3 from a third-party mirror is disabled in the node (the `ASTRAFORGE PATCH` edit in `nodes.py`): the weights are taken only from the official timm copy.

**VFX texture generators ("VFX" tab, optional `vfx` component).** They are not included in the archive. The wizard or
`scripts/vfx/Install-VfxTools.ps1` downloads them from the authors' sites into a separate folder (`ASTRA_VFX_TOOLS`), and
the user runs the PixaFlux, KumoWorks and Slate installers themselves. The full list is in `server/vfx-tools-catalog.json`.

| Component | Source | License |
|---|---|---|
| Material Maker 1.7 | github.com/RodZill4/material-maker | MIT |
| KumoWorks 1.0.1 | github.com/opentoonz/kumoworks | BSD-3-Clause |
| Texture Generator Online, NormalMap Online | github.com/cpetry | MIT |
| DisplacementX (Greeble), built from source | github.com/satelllte/displacementx | GPL-3.0 (the source sits alongside, `web/displacementx-src`) |
| Neon Flames | github.com/jwagner/Neonflames | no license stated: personal use only, do not include in builds |
| PixaFlux, Slate, FlowMap Painter, Pseudo Volume Generator | pixaflux.com, facedownfx.com, teckartist.com, acetix.itch.io | free programs under the authors' terms |
| Impact Decal preset (Material Maker) | materialmaker.org | CC0 |

The Blender scripts and the ComfyUI node from this archive run inside programs under the GPL license. The MIT license
is compatible with the GPL. When distributing a build that already bundles Blender or ComfyUI, the GPL
applies to those programs.
