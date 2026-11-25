# Project-owned WSL runtime

- Distro: `AstraForge-AMD` (WSL 2, Ubuntu 24.04)
- VHDX: `runtime/wsl/distro` (ignored)
- Engine: `runtime/ComfyUI-Pixal3D-AMD` (models and runtime state ignored)
- Python environment: `/opt/astraforge/pixal3d-venv` inside the project VHDX, exposed to the engine as `.venv`
- GPU runtime: ROCm 7.2.1, ROCDXG 1.2.0, `gfx1201`
- Port: `127.0.0.1:8191`
- Stop: `Stop.cmd`; terminate only `AstraForge-AMD`, never another distribution or all of WSL

The ROCm PyTorch smoke test must identify `AMD Radeon RX 9070 XT` and complete a GPU allocation before model downloads or native extension builds begin.

## NAF parity and bounded benchmark

Reapply only the project patch with `patch_trellis2_amd.py --naf <NAF root>`; do not rerun the installer for patch-only changes. Then run `verify_naf_amd.py <NAF root>` and `benchmark_naf_amd.py <NAF root>` with the existing `/opt/astraforge/pixal3d-venv/bin/python`.

The RX 9070 XT verification selected `ASTRA_AMD_NAF_ROW_BLOCK=2`, which is also the patch default. Blocks 1/2/4/8 retained exact parity with block 1 at 32×32, 64×64, 128×128 and 256×256 in the 2026-11-02 benchmark. Block 2 took 16.46 ms at 64×64, 190.17 ms at 128×128 and 761.50 ms at 256×256; its 256×256 peak allocation was 1,014,112,256 bytes. Block 1 was faster at 128×128, so block 2 is not universally optimal. Padding is bounded to each row block rather than a full padded tensor. AMD single-view vertex coloring also sets `ASTRA_AMD_NAF_TILE=2` by default to use upstream tiled NAF; multi-view coloring is not supported. The full profile and opt-in vertex-color results are recorded in `docs/forge/pixal3d.md`.

## UV base color texturing

Reapply only `patch_trellis2_amd.py` to the existing ComfyUI-Trellis2 node root; do not rerun the installer or update ROCm. The AMD branch uses ComfyUI's existing UV unwrap, tiled barycentric rasterizer and interpolation helpers, with no CuMesh UV unwrap, nvdiffrast or new dependencies. Terminate only `AstraForge-AMD` to reload the Python patch, and restart AstraForge to load the API changes.

Run the verified bounded profile with `AMD_PIXAL3D_UV_TEXTURE=1 AMD_PIXAL3D_QUALITY=draft node scripts/verify_amd_pixal3d_e2e.mjs`. The result is `data/runtime/amd-pixal3d-single-uv.glb`, with `TEXCOORD_0` and an embedded 512 × 512 PNG `baseColorTexture` required by validation. The RX 9070 XT run took 389.1 s and peaked at 14,731.38 MB VRAM; exact geometry and job details are in `docs/forge/pixal3d.md`. Multi-view UV and 1024 geometry with UV are explicitly rejected; a 1024 atlas is not implemented as a verified feature.
