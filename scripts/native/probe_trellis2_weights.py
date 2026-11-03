"""Load each real single-view 512 model once in the native AMD runtime."""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
RUNTIME = REPO_ROOT / "runtime" / "ComfyUI-Pixal3D-AMD-Native"
COMFYUI = RUNTIME / "ComfyUI"
NODE_ROOT = COMFYUI / "custom_nodes" / "ComfyUI-Trellis2"
REPORT = REPO_ROOT / "data" / "runtime" / "diagnostics" / "native-amd" / "weights.json"

os.environ["ASTRA_AMD_ROCM"] = "1"
os.environ["ASTRA_PIXAL3D_NATIVE"] = "1"
sys.path.insert(0, str(COMFYUI))

import torch

spec = importlib.util.spec_from_file_location(
    "astraforge_native_trellis2",
    NODE_ROOT / "__init__.py",
    submodule_search_locations=[str(NODE_ROOT)],
)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

loader = module.NODE_CLASS_MAPPINGS["Trellis2LoadModel"]()
(pipeline,) = loader.process(
    modelname="TencentARC/Pixal3D",
    backend="sdpa",
    device="cuda",
    low_vram=True,
    keep_models_loaded=False,
    conv_backend="flex_gemm",
    sparse_backend="sdpa",
    use_reconviagen=False,
    pixal3d_multiview=False,
)

checks = []
for stage in [
    "moge_model",
    "pixal3d_image_cond_ss",
    "pixal3d_image_cond_shape_512",
    "sparse_structure_model",
    "shape_slat_flow_model_512",
    "shape_slat_decoder",
]:
    started = time.perf_counter()
    torch.cuda.reset_peak_memory_stats()
    print(f"PROBE_LOAD {stage}", flush=True)
    getattr(pipeline, f"load_{stage}")()
    elapsed = time.perf_counter() - started
    peak = torch.cuda.max_memory_allocated()
    checks.append({"stage": stage, "status": "pass", "seconds": round(elapsed, 3), "peakBytes": peak})
    print(f"PROBE_UNLOAD {stage}", flush=True)
    getattr(pipeline, f"unload_{stage}")()
    torch.cuda.empty_cache()

result = {
    "schemaVersion": 1,
    "backend": "windows-rocm",
    "model": "TencentARC/Pixal3D",
    "workflow": "single512",
    "torch": torch.__version__,
    "device": torch.cuda.get_device_name(0),
    "checks": checks,
    "weightsReady": all(item["status"] == "pass" for item in checks),
}
REPORT.parent.mkdir(parents=True, exist_ok=True)
REPORT.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
print(json.dumps(result, indent=2), flush=True)
