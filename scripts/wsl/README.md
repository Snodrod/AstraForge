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

The RX 9070 XT verification selected `ASTRA_AMD_NAF_ROW_BLOCK=2`, which is also the patch default. Blocks 1/2/4/8 retained exact parity with block 1 in the post-optimization benchmark; block 2 took 16.68 ms at 64×64 versus 22.24 ms for block 1 and allocated 174,884,864 bytes at peak. Padding is bounded to each row block rather than a full padded tensor. AMD single-view vertex coloring also sets `ASTRA_AMD_NAF_TILE=2` by default to use upstream tiled NAF; multi-view coloring is not supported. The full profile and opt-in vertex-color results are recorded in `docs/forge/pixal3d.md`.
