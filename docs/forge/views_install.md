# Views: what is installed and how (MV-Adapter in its own ComfyUI)

State as of 2026-10-13. Relates to `docs/forge/views.md`. Everything downloaded was explicitly authorised:
the `huanngzh/mv-adapter` weights, the `huanngzh/ComfyUI-MVAdapter` node and the Python packages of this node. Nothing else was downloaded
(no BiRefNet, no other VAE, no SDXL in diffusers format).

## What and where

| What | From | To | Size |
|---|---|---|---|
| `mvadapter_i2mv_sdxl.safetensors` (Apache-2.0) | huggingface.co/huanngzh/mv-adapter | `data/models/mvadapter/` | 3.60 GB |
| ComfyUI-MVAdapter node 1.0.2 (Apache-2.0) | github.com/huanngzh/ComfyUI-MVAdapter (zip of the main branch) | `data/runtime/custom_nodes/ComfyUI-MVAdapter/` | 0.5 MB without the README images |
| SDXL configs and tokenizers (no weights) | assembled locally by `scripts/views/make_sdxl_config.py` from ComfyUI files and the published SDXL base values | `data/models/mvadapter/sdxl-config/` | 2 MB |
| SDXL base 1.0 and its own VAE | already present: `<ComfyUI dir>/models/checkpoints/sd_xl_base_1.0.safetensors` | — | — |

The node is added to the launch whitelist: `server/runtime.mjs` now starts ComfyUI with
`--whitelist-custom-nodes AstraForgeMesh ComfyUI-MVAdapter` and the variables `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`
(a safeguard: no node of this engine will go to the network).

## Python packages (`<ComfyUI dir>/.venv`)

Before installation a `pip freeze` was saved (131 lines). Only missing packages were installed, with version constraints
`pip install -c freeze_before.txt ...`: pip could not touch any package that was already installed. Summary of the before/after diff:

```diff
+ accelerate==1.15.0
+ antlr4-python3-runtime==4.9.3        # needed by omegaconf, which was already installed without it
+ diffusers==0.40.0
+ importlib_metadata==9.0.1
+ peft==0.21.2
+ timm==1.0.30
+ trimesh==5.1.1
+ zipp==4.1.1
```

In total 139 lines against 131: eight additions, zero changes, zero removals.

Verified after installation (versions the same as before):

| Package | Before | After |
|---|---|---|
| torch | 2.11.0+cu128 | 2.11.0+cu128 |
| torchvision | 0.26.0+cu128 | 0.26.0+cu128 |
| torchaudio | 2.11.0+cu128 | 2.11.0+cu128 |
| numpy | 2.5.2 | 2.5.2 |
| transformers | 5.15.1 | 5.15.1 |
| huggingface_hub | 1.28.0 | 1.28.0 |
| safetensors, tokenizers, pillow, scipy | 0.8.0, 0.22.2, 12.3.0, 1.18.0 | unchanged |

`pip check` reports one remark that existed before the installation: `submitit 1.5.4 requires cloudpickle`
(the submitit package is needed by demucs and has nothing to do with views).

The node's `requirements.txt` demands `diffusers==0.31.0`, `transformers==4.46.3`, `huggingface_hub==0.24.6`,
`accelerate==1.1.1`. That would downgrade `transformers` 5.15.1 and `huggingface_hub` 1.28.0, on which the rest of ComfyUI runs,
so the pins were not applied. The node manifest (`pyproject.toml`) allows `>=`, so `diffusers 0.40` is fine.

Before/after files: `docs/forge/views_freeze_before.txt`, `docs/forge/views_freeze_after.txt`.

## Node patches (list in `data/runtime/custom_nodes/ComfyUI-MVAdapter/NOTICE-ASTRAFORGE.md`)

1. `utils.local_sdxl_config()` and `LdmPipelineLoader`: `from_single_file(config=<local folder>, local_files_only=True)`.
   Without this diffusers goes to huggingface.co for the SDXL configs.
2. `enable_vae_slicing/tiling` in diffusers 0.40 moved from the pipeline to `pipeline.vae`: a fallback path is added.
3. A new node `AstraMVAdapterViews` (`astra_views.py`): loading, generation and freeing of VRAM in a single node.
   The stock nodes keep the entire pipeline (~8 GB) on the GPU between jobs, which gets in the way of Hunyuan3D in the same process.
   The node: `unload_all_models()` before work, manual offload of the text encoders and VAE (`offload=text+vae`), full memory return afterwards.

Why not `enable_model_cpu_offload`: accelerate copies kwargs dictionaries, while MV-Adapter passes the cache of reference hidden
states inside a dictionary: the cache was lost (`KeyError: ...attn1.processor`). Manual offload does not do this.

## Memory and speed (RTX 4070 12 GB, 4 views, 30 steps)

Peak `torch.cuda.max_memory_allocated`; the system desktop takes another ~1.3 GB:

| Size | VRAM peak | Time | Conclusion |
|---|---|---|---|
| 512x512 | 8.7 GB | 3 steps in 3.6 s (~10 s after loading the weights) | fits with margin |
| 640x640 | 9.7 GB | 34 s for the whole job (30 steps) | **default** |
| 768x768 | 11.0 GB (12.5 without manual offload) | 77 s for 3 steps including loading: the GPU spills to swap | do not use |

Six views (45° added) are computed only at 512. Without manual offload the peak at 640 would be ~11 GB.
The app limits the size itself (`safeSize`) and on an out-of-memory error repeats at 512.

## How to check after a reinstall

```
node scripts/views/views_generate.mjs --status          # codex.available and mvadapter.ready must be true
node scripts/views/views_generate.mjs --input front.png --out out --provider mvadapter
```

If `mvadapter.ready=false`: no weights, no `sdxl-config` (run `scripts/views/make_sdxl_config.py`)
or ComfyUI was started before the node was installed (Stop.cmd, then Start.cmd).

## What is not done

The installer (`installer/Install.ps1`) does not download the MV-Adapter weights and node: this is a maintainer decision
(for licensing, the line is already in `THIRD_PARTY_NOTICES.md`).
