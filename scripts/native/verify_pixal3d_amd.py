"""Fail-fast HIP/SDPA diagnostics for native Windows AMD Pixal3D."""
from __future__ import annotations

import argparse
import gc
import json
import math
import platform
import re
import sys
from pathlib import Path

MANDATORY_CHECKS = (
    "python_import", "hip_import", "gpu_available", "gpu_architecture",
    "matmul_fp32", "matmul_fp16", "matmul_bf16", "sdpa", "memory_cleanup",
)
SUPPORTED_ARCHITECTURES = {"gfx1201"}


def gpu_architecture(properties) -> str:
    value = str(getattr(properties, "gcnArchName", "") or "")
    match = re.search(r"gfx\d+", value, re.IGNORECASE)
    return match.group(0).lower() if match else "unknown"


def native_ready(checks, mandatory=MANDATORY_CHECKS) -> bool:
    by_name = {item.get("name"): item.get("status") for item in checks}
    return all(by_name.get(name) == "pass" for name in mandatory)


def run_probe() -> dict:
    report = {
        "schemaVersion": 1,
        "platform": "Windows" if sys.platform == "win32" else platform.system(),
        "gpu": {"name": "", "architecture": "unknown", "vramBytes": 0},
        "torch": {"version": "", "hip": ""},
        "checks": [],
        "nativeReady": False,
    }
    torch = None

    def check(name, operation):
        try:
            details = operation()
            report["checks"].append({"name": name, "status": "pass", "details": str(details or "ok")})
            return True
        except Exception as error:  # diagnostic report must preserve the real failing operation
            report["checks"].append({"name": name, "status": "fail", "details": f"{type(error).__name__}: {error}"})
            return False

    def import_python():
        if sys.platform != "win32":
            raise RuntimeError("Native AMD generation is supported only on Windows.")
        executable = str(Path(sys.executable).resolve())
        if re.match(r"^/mnt/[a-z]/", executable, re.IGNORECASE):
            raise RuntimeError("WSL Python is not a native runtime.")
        return executable

    def import_torch():
        nonlocal torch
        import torch as imported
        torch = imported
        report["torch"] = {"version": str(torch.__version__), "hip": str(torch.version.hip or "")}
        if not torch.version.hip:
            raise RuntimeError("This PyTorch is not a ROCm/HIP build.")
        return report["torch"]

    def gpu_available():
        if torch is None or not torch.cuda.is_available():
            raise RuntimeError("ROCm GPU is unavailable through PyTorch.")
        properties = torch.cuda.get_device_properties(0)
        report["gpu"] = {
            "name": str(torch.cuda.get_device_name(0)),
            "architecture": gpu_architecture(properties),
            "vramBytes": int(properties.total_memory),
        }
        return report["gpu"]

    def architecture_check():
        architecture = report["gpu"]["architecture"]
        if architecture not in SUPPORTED_ARCHITECTURES:
            raise RuntimeError(f"Unsupported or unknown architecture for the initial native profile: {architecture}")
        return architecture

    def matmul(dtype, tolerance):
        if torch is None:
            raise RuntimeError("PyTorch was not imported.")
        generator = torch.Generator().manual_seed(42)
        left = torch.randn((48, 64), generator=generator, dtype=torch.float32)
        right = torch.randn((64, 40), generator=generator, dtype=torch.float32)
        # Compare like-for-like precision: a low-precision GPU kernel is not expected to match an FP32 accumulation.
        expected = (left.to(dtype) @ right.to(dtype)).float() if dtype != torch.float32 else left @ right
        actual = (left.to("cuda", dtype=dtype) @ right.to("cuda", dtype=dtype)).float().cpu()
        torch.cuda.synchronize()
        if not math.isfinite(float(actual.abs().max())):
            raise RuntimeError("Non-finite GPU result.")
        torch.testing.assert_close(actual, expected, rtol=tolerance, atol=tolerance)
        return f"maxAbs={(actual - expected).abs().max().item():.6g}"

    def sdpa_check():
        if torch is None:
            raise RuntimeError("PyTorch was not imported.")
        generator = torch.Generator().manual_seed(43)
        tensors = [torch.randn((1, 4, 16, 64), generator=generator, dtype=torch.float32) for _ in range(3)]
        cpu_q, cpu_k, cpu_v = [item.clone().requires_grad_(True) for item in tensors]
        expected = torch.nn.functional.scaled_dot_product_attention(cpu_q, cpu_k, cpu_v)
        expected.sum().backward()
        gpu_tensors = [item.to("cuda").requires_grad_(True) for item in tensors]
        actual = torch.nn.functional.scaled_dot_product_attention(*gpu_tensors)
        if actual.device.type != "cuda":
            raise RuntimeError("SDPA silently left the GPU.")
        actual.sum().backward()
        torch.cuda.synchronize()
        torch.testing.assert_close(actual.cpu(), expected, rtol=1e-4, atol=1e-5)
        for gpu_tensor, cpu_tensor in zip(gpu_tensors, (cpu_q, cpu_k, cpu_v)):
            torch.testing.assert_close(gpu_tensor.grad.cpu(), cpu_tensor.grad, rtol=1e-4, atol=1e-5)
        return "forward and backward match CPU"

    def memory_cleanup():
        if torch is None:
            raise RuntimeError("PyTorch was not imported.")
        gc.collect()
        torch.cuda.empty_cache()
        baseline = int(torch.cuda.memory_allocated(0))
        temporary = torch.empty((4 * 1024 * 1024,), dtype=torch.float32, device="cuda")
        temporary.fill_(1.0)
        peak = int(torch.cuda.memory_allocated(0))
        del temporary
        gc.collect()
        torch.cuda.empty_cache()
        torch.cuda.synchronize()
        allocated = int(torch.cuda.memory_allocated(0))
        if allocated != baseline:
            raise RuntimeError(f"Temporary GPU allocation was not released: baseline={baseline}, current={allocated}")
        return f"baseline={baseline}, peak={peak}, current={allocated}"

    check("python_import", import_python)
    if check("hip_import", import_torch):
        if check("gpu_available", gpu_available):
            check("gpu_architecture", architecture_check)
            check("matmul_fp32", lambda: matmul(torch.float32, 1e-4))
            check("matmul_fp16", lambda: matmul(torch.float16, 2e-2))
            check("matmul_bf16", lambda: matmul(torch.bfloat16, 2e-2))
            check("sdpa", sdpa_check)
            check("memory_cleanup", memory_cleanup)
    report["nativeReady"] = native_ready(report["checks"])
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", default="data/runtime/diagnostics/native-amd/base-probe.json")
    args = parser.parse_args()
    report = run_probe()
    destination = Path(args.json).resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
