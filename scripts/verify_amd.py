import json
import torch

result = {
    "torch_version": torch.__version__,
    "hip_version": torch.version.hip,
    "gpu_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
}
assert result["hip_version"], "ROCm/HIP PyTorch is required"
assert result["gpu_name"] and "9070 XT" in result["gpu_name"], result
print(json.dumps(result, ensure_ascii=False))
