"""Path and wheel validation for the isolated native AMD runtime."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

REQUIRED_WHEELS = (
    "rocm_sdk_core-7.2.1-py3-none-win_amd64.whl",
    "rocm_sdk_devel-7.2.1-py3-none-win_amd64.whl",
    "rocm_sdk_libraries_custom-7.2.1-py3-none-win_amd64.whl",
    "torch-2.9.1+rocm7.2.1-cp312-cp312-win_amd64.whl",
    "torchaudio-2.9.1+rocm7.2.1-cp312-cp312-win_amd64.whl",
    "torchvision-0.24.1+rocm7.2.1-cp312-cp312-win_amd64.whl",
)
ROCM_META_PACKAGE = "rocm-7.2.1.tar.gz"
PROTECTED_RUNTIME_NAMES = {"comfyui-pixal3d", "comfyui-pixal3d-amd"}


def find_meta_package(folder: str | Path) -> Path:
    package = Path(folder) / ROCM_META_PACKAGE
    if not package.is_file():
        raise FileNotFoundError(f"Missing ROCm meta package: {package.name}")
    return package


def validate_runtime_root(value: str | Path) -> Path:
    text = str(value)
    root = Path(text)
    if not root.is_absolute() or re.match(r"^/mnt/[a-z](?:/|$)", text, re.IGNORECASE) or "wsl.exe" in text.lower():
        raise ValueError("Native AMD runtime must be an absolute native Windows path, not WSL.")
    resolved = root.resolve(strict=False)
    if resolved.name.lower() in PROTECTED_RUNTIME_NAMES:
        raise ValueError("Refusing to overwrite an existing NVIDIA or WSL Pixal3D runtime.")
    return resolved


def find_wheel_set(folder: str | Path) -> list[Path]:
    root = Path(folder)
    wheels = [root / name for name in REQUIRED_WHEELS]
    missing = [item.name for item in wheels if not item.is_file()]
    if missing:
        raise FileNotFoundError("Missing native AMD wheels: " + ", ".join(missing))
    if any("cu128" in item.name.lower() or "cuda" in item.name.lower() for item in wheels):
        raise ValueError("CUDA wheels are not valid for the native AMD runtime.")
    return wheels


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime", required=True)
    parser.add_argument("--wheel-dir", required=True)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    runtime = validate_runtime_root(args.runtime)
    wheels = find_wheel_set(args.wheel_dir)
    print(json.dumps({"runtime": str(runtime), "wheels": [str(item) for item in wheels], "valid": True}, indent=2))


if __name__ == "__main__":
    main()
