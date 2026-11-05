import argparse
import json
import os
import time
from pathlib import Path

import torch
from o_voxel.convert import tiled_flexible_dual_grid_to_mesh
from torch.profiler import ProfilerActivity, profile


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--json', required=True)
    parser.add_argument('--size', type=int, default=48)
    parser.add_argument('--tile-size', type=int, default=16)
    args = parser.parse_args()

    size = args.size
    linear = torch.arange(size ** 3, dtype=torch.int32, device='cuda')
    coords = torch.stack((
        linear // (size * size),
        (linear // size) % size,
        linear % size,
    ), dim=1).contiguous()
    torch.manual_seed(1204)
    dual = torch.rand((len(coords), 3), device='cuda') * 0.6 + 0.2
    intersected = torch.ones((len(coords), 3), dtype=torch.bool, device='cuda')
    weight = torch.rand((len(coords), 1), device='cuda') + 0.1
    kwargs = {
        'aabb': [[-0.5, -0.5, -0.5], [0.5, 0.5, 0.5]],
        'grid_size': size,
        'tile_size': args.tile_size,
        'train': False,
    }

    tiled_flexible_dual_grid_to_mesh(coords, dual, intersected, weight, **kwargs)
    torch.cuda.synchronize()
    started = time.perf_counter()
    with profile(activities=[ProfilerActivity.CPU, ProfilerActivity.CUDA]) as profiler:
        vertices, faces = tiled_flexible_dual_grid_to_mesh(coords, dual, intersected, weight, **kwargs)
        torch.cuda.synchronize()
    wall_ms = (time.perf_counter() - started) * 1000

    operations = []
    for event in profiler.key_averages():
        device_ms = getattr(event, 'self_device_time_total', 0) / 1000
        cpu_ms = event.self_cpu_time_total / 1000
        operations.append({
            'name': event.key,
            'calls': event.count,
            'selfCpuMs': round(cpu_ms, 3),
            'selfDeviceMs': round(device_ms, 3),
        })
    operations.sort(key=lambda item: item['selfDeviceMs'] + item['selfCpuMs'], reverse=True)
    report = {
        'size': size,
        'tileSize': args.tile_size,
        'coords': len(coords),
        'vertices': len(vertices),
        'faces': len(faces),
        'wallMs': round(wall_ms, 3),
        'torch': torch.__version__,
        'device': torch.cuda.get_device_name(),
        'operations': operations,
    }
    output = Path(args.json)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({key: value for key, value in report.items() if key != 'operations'}), flush=True)
    print(json.dumps(report['operations'][:12], indent=2), flush=True)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'{type(error).__name__}: {error}', flush=True)
        os._exit(1)
    os._exit(0)
