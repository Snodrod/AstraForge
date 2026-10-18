# AstraForge changes to ComfyUI-MVAdapter

Upstream: https://github.com/huanngzh/ComfyUI-MVAdapter (Apache-2.0, see LICENSE). Local changes:

1. `utils.py`: `local_sdxl_config()` returns `<app>/data/models/mvadapter/sdxl-config` (config-only SDXL folder built by
   `scripts/views/make_sdxl_config.py`) so `from_single_file` never touches the Hugging Face hub.
2. `nodes.py`: `LdmPipelineLoader` passes `config=<that folder>, local_files_only=True`; VAE slicing/tiling falls back to
   `pipeline.vae.enable_*` (diffusers >= 0.40).
3. `astra_views.py` + `__init__.py`: node `AstraMVAdapterViews` (local-only, frees the GPU after use).
4. The `assets/` folder (README pictures) was removed.
