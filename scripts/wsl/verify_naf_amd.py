"""Numerical and GPU checks for the bounded NAF attention fallback."""
import json
import sys

import torch


def reference_na2d(q, k, v, kernel_size, dilation):
    kh, kw = kernel_size
    dh, dw = dilation
    batch, height, width, heads, _ = q.shape
    out = torch.zeros(*q.shape[:-1], v.shape[-1], dtype=v.dtype, device=v.device)
    for b in range(batch):
        for y in range(height):
            for x in range(width):
                for h in range(heads):
                    scores = []
                    values = []
                    for ky in range(kh):
                        for kx in range(kw):
                            sy = y + (ky - kh // 2) * dh
                            sx = x + (kx - kw // 2) * dw
                            valid = 0 <= sy < height and 0 <= sx < width
                            scores.append((q[b, y, x, h] * k[b, sy, sx, h]).sum() if valid else torch.zeros((), dtype=q.dtype))
                            values.append(v[b, sy, sx, h] if valid else torch.zeros_like(v[b, 0, 0, h]))
                    weights = (torch.stack(scores) * (q.shape[-1] ** -0.5)).softmax(dim=0)
                    out[b, y, x, h] = (weights[:, None] * torch.stack(values)).sum(dim=0)
    return out


def main():
    sys.path.insert(0, sys.argv[1])
    from src.layers.attentions import na2d

    worst = 0.0
    for shape, dilation in [((3, 3), (1, 1)), ((4, 4), (2, 2)), ((5, 7), (1, 1))]:
        torch.manual_seed(42)
        q = torch.randn(1, *shape, 2, 4, dtype=torch.float64)
        k = torch.randn_like(q)
        v = torch.randn(1, *shape, 2, 8, dtype=torch.float64)
        actual = na2d(q, k, v, (3, 3), dilation=dilation)
        expected = reference_na2d(q, k, v, (3, 3), dilation)
        worst = max(worst, (actual - expected).abs().max().item())
    assert worst < 1e-10, worst

    gpu_error = None
    if torch.cuda.is_available():
        torch.manual_seed(43)
        q = torch.randn(1, 8, 8, 4, 64, device='cuda')
        k = torch.randn_like(q)
        v = torch.randn(1, 8, 8, 4, 128, device='cuda')
        actual = na2d(q, k, v, (9, 9), dilation=(1, 1), backend='cutlass-fna')
        expected = na2d(q.cpu(), k.cpu(), v.cpu(), (9, 9), dilation=(1, 1))
        gpu_error = (actual.cpu() - expected).abs().max().item()
        assert gpu_error < 2e-5, gpu_error
    print(json.dumps({'worst_cpu_abs': worst, 'gpu_cpu_max_abs': gpu_error, 'hip': torch.version.hip}))


if __name__ == '__main__':
    main()
