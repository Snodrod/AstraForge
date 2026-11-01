#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
ENGINE="$(cd -- "$SCRIPT_DIR/../.." && pwd)/runtime/ComfyUI-Pixal3D-AMD"
export HSA_ENABLE_DXG_DETECTION=1
export LD_LIBRARY_PATH="/opt/rocm/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export PATH="/opt/rocm/bin:$PATH"
export PYTORCH_ROCM_ARCH=gfx1201
export ASTRA_AMD_ROCM=1
export ATTN_BACKEND=sdpa
export SPARSE_ATTN_BACKEND=sdpa
export FLEX_GEMM_ALGO=explicit_gemm
exec "$ENGINE/.venv/bin/python" "$ENGINE/ComfyUI/main.py" \
  --listen 127.0.0.1 --disable-auto-launch "$@"
