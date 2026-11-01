# Project-owned WSL runtime

- Distro: `AstraForge-AMD` (WSL 2, Ubuntu 24.04)
- VHDX: `runtime/wsl/distro` (ignored)
- Engine: `runtime/ComfyUI-Pixal3D-AMD` (models and runtime state ignored)
- Python environment: `/opt/astraforge/pixal3d-venv` inside the project VHDX, exposed to the engine as `.venv`
- GPU runtime: ROCm 7.2.1, ROCDXG 1.2.0, `gfx1201`
- Port: `127.0.0.1:8191`
- Stop: `Stop.cmd`; terminate only `AstraForge-AMD`, never another distribution or all of WSL

The ROCm PyTorch smoke test must identify `AMD Radeon RX 9070 XT` and complete a GPU allocation before model downloads or native extension builds begin.
