#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
APP_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
ENGINE="$APP_ROOT/runtime/ComfyUI-Pixal3D-AMD"
VENV=/opt/astraforge/pixal3d-venv
SRC=/opt/astraforge/src
mkdir -p "$ENGINE" "$SRC"

export DEBIAN_FRONTEND=noninteractive
if [ ! -x /opt/rocm/bin/hipcc ] || [ ! -f /opt/rocm/include/thrust/complex.h ] || [ ! -f /opt/rocm/include/hipcub/hipcub.hpp ] || [ ! -f /opt/rocm/include/hipsparse/hipsparse.h ] || [ ! -f /opt/rocm/include/hipblas/hipblas.h ]; then
  apt-get update
  apt-get install -y --no-install-recommends rocm-hip-sdk rocthrust-dev hipcub-dev
fi
export PATH="$VENV/bin:/opt/rocm/bin:$PATH"
export LD_LIBRARY_PATH="/opt/rocm/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
export HSA_ENABLE_DXG_DETECTION=1
export PYTORCH_ROCM_ARCH=gfx1201
export GPU_ARCHS=gfx1201
export BUILD_TARGET=rocm
export FORCE_CUDA=1
export CC=hipcc
export CXX=hipcc
export MAX_JOBS="${MAX_JOBS:-4}"

clone_pinned() {
  local url="$1" commit="$2" destination="$3"
  if [ ! -d "$destination/.git" ]; then
    git clone --filter=blob:none "$url" "$destination"
  fi
  if ! git -C "$destination" cat-file -e "$commit^{commit}" 2>/dev/null; then
    git -C "$destination" fetch --depth 1 origin "$commit"
  fi
  git -C "$destination" checkout -q "$commit"
}

if [ ! -x "$VENV/bin/python" ]; then
  python3 -m venv "$VENV"
  "$VENV/bin/python" -m pip install --upgrade pip wheel
fi
if [ ! -L "$ENGINE/.venv" ] && [ ! -d "$ENGINE/.venv" ]; then
  ln -s "$VENV" "$ENGINE/.venv"
fi
if ! "$VENV/bin/python" -c 'import torch; assert torch.version.hip' 2>/dev/null; then
  "$VENV/bin/python" -m pip install \
    'https://repo.radeon.com/rocm/manylinux/rocm-rel-7.2/torch-2.9.1%2Brocm7.2.0.lw.git7e1940d4-cp312-cp312-linux_x86_64.whl' \
    'https://repo.radeon.com/rocm/manylinux/rocm-rel-7.2/torchvision-0.24.0%2Brocm7.2.0.gitb919bd0c-cp312-cp312-linux_x86_64.whl' \
    'https://repo.radeon.com/rocm/manylinux/rocm-rel-7.2/torchaudio-2.9.0%2Brocm7.2.0.gite3c6ee2b-cp312-cp312-linux_x86_64.whl' \
    'https://repo.radeon.com/rocm/manylinux/rocm-rel-7.2/triton-3.5.1%2Brocm7.2.0.gita272dfa8-cp312-cp312-linux_x86_64.whl' \
    'numpy==1.26.4'
fi
rm -f "$VENV"/lib/python3.12/site-packages/torch/lib/libhsa-runtime64.so*
"$VENV/bin/python" -c "import torch; assert torch.version.hip; assert torch.cuda.is_available(); assert '9070 XT' in torch.cuda.get_device_name(0); print(torch.cuda.get_device_name(0))"

clone_pinned https://github.com/Comfy-Org/ComfyUI.git 8010b48d2c1be986613f8e8c761527d205bd756f "$ENGINE/ComfyUI"
clone_pinned https://github.com/visualbruno/ComfyUI-Trellis2.git b0e84c0f29857a444e8bcb5b9be2ea218e96efd0 "$ENGINE/ComfyUI/custom_nodes/ComfyUI-Trellis2"
clone_pinned https://github.com/visualbruno/CuMesh.git d10e54c30ddd03d11472c1431693f985501c7966 "$SRC/CuMesh"
clone_pinned https://gitlab.com/libeigen/eigen.git 3147391d946bb4b6c68edd901f2add6ac1f31f8c "$SRC/CuMesh/third_party/cubvh/third_party/eigen"
clone_pinned https://github.com/visualbruno/FlexGEMM.git db388bd17c34abc697792aa718dca780734c2783 "$SRC/FlexGEMM"
clone_pinned https://github.com/visualbruno/TRELLIS.2.git 65d1e13b4a92296036044df0633242bb9e95abf6 "$SRC/TRELLIS.2"
git -C "$SRC/TRELLIS.2" submodule update --init o-voxel/third_party/eigen

"$VENV/bin/python" -m pip install -r "$ENGINE/ComfyUI/requirements.txt"
"$VENV/bin/python" -m pip install ninja requests pymeshlab plyfile easydict zstandard opencv-python scipy open3d plotly rembg trimesh \
  timm transformers accelerate safetensors huggingface_hub einops utils3d@https://github.com/EasternJournalist/utils3d/archive/83bfa5bda008803f6521ce9547f94023b8afeddd.zip
mkdir -p "$ENGINE/ComfyUI/models/microsoft/TRELLIS.2-4B"
cp -f "$APP_ROOT/installer/pixal3d/af_vram_probe.py" "$ENGINE/ComfyUI/custom_nodes/af_vram_probe.py"
"$VENV/bin/python" "$APP_ROOT/installer/pixal3d/patch_trellis2.py" "$ENGINE/ComfyUI/custom_nodes/ComfyUI-Trellis2"
"$VENV/bin/python" "$SCRIPT_DIR/patch_trellis2_amd.py" "$ENGINE/ComfyUI/custom_nodes/ComfyUI-Trellis2" "$SRC/FlexGEMM" "$SRC/TRELLIS.2/o-voxel"

if git -C "$SRC/CuMesh" apply --check "$SCRIPT_DIR/patches/cumesh-rocm.patch" 2>/dev/null; then
  git -C "$SRC/CuMesh" apply "$SCRIPT_DIR/patches/cumesh-rocm.patch"
elif ! git -C "$SRC/CuMesh" apply --reverse --check "$SCRIPT_DIR/patches/cumesh-rocm.patch" 2>/dev/null; then
  echo 'CuMesh ROCm patch does not match the pinned source' >&2
  exit 1
fi

build_extension() {
  local module="$1" source_dir="$2"
  if "$VENV/bin/python" -c "import $module" >/dev/null 2>&1; then
    echo "$module already imports; skipping rebuild"
    return
  fi
  "$VENV/bin/python" -m pip install --no-build-isolation --no-deps "$source_dir"
}
build_extension cumesh "$SRC/CuMesh"
build_extension flex_gemm "$SRC/FlexGEMM"
build_extension o_voxel "$SRC/TRELLIS.2/o-voxel"

"$VENV/bin/python" "$SCRIPT_DIR/verify_pixal3d_amd.py" --ready "$ENGINE/wsl-ready.json" --source-root "$SRC"
