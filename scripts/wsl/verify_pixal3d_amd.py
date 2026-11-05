import argparse
import importlib.metadata
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import torch

assert torch.version.hip, 'ROCm PyTorch required'
assert torch.cuda.is_available(), 'AMD GPU unavailable'
assert '9070 XT' in torch.cuda.get_device_name(0), torch.cuda.get_device_name(0)

import cumesh
import flex_gemm
import o_voxel

x = torch.randn((256, 256), device='cuda')
assert torch.isfinite(x @ x).all().item()

result = {
    'gpu': torch.cuda.get_device_name(0),
    'torch': torch.__version__,
    'hip': torch.version.hip,
    'extensions': {
        name: importlib.metadata.version(name)
        for name in ('cumesh', 'flex_gemm', 'o_voxel')
    },
}

parser = argparse.ArgumentParser()
parser.add_argument('--ready', type=Path)
parser.add_argument('--source-root', type=Path)
args = parser.parse_args()
if args.ready:
    sources = {}
    if args.source_root:
        for name in ('CuMesh', 'FlexGEMM', 'TRELLIS.2'):
            repo = args.source_root / name
            sources[name] = subprocess.check_output(
                ['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True
            ).strip()
    result['sources'] = sources
    result['verifiedAt'] = datetime.now(timezone.utc).isoformat()
    args.ready.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
print(json.dumps(result, ensure_ascii=False))
