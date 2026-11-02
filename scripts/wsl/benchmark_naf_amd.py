"""Short ROCm NAF row-block benchmark; never runs a model or changes the runtime."""
import json
import os
import sys
import time

import torch


def main():
    if not torch.cuda.is_available():
        raise RuntimeError('A ROCm GPU is required for the NAF benchmark.')
    sys.path.insert(0, sys.argv[1])
    from src.layers.attentions import na2d

    records = []
    for height, width in [(32, 32), (64, 64)]:
        torch.manual_seed(4200 + height)
        q = torch.randn(1, height, width, 4, 64, device='cuda')
        k = torch.randn_like(q)
        v = torch.randn(1, height, width, 4, 128, device='cuda')
        reference = None
        for block in [1, 2, 4, 8]:
            os.environ['ASTRA_AMD_NAF_ROW_BLOCK'] = str(block)
            torch.cuda.empty_cache()
            torch.cuda.reset_peak_memory_stats()
            try:
                actual = na2d(q, k, v, (9, 9), dilation=(1, 1))
                if reference is None:
                    reference = actual.detach().clone()
                max_abs = (actual - reference).abs().max().item()
                if max_abs >= 2e-5:
                    raise AssertionError(f'NAF parity failed for block {block}: {max_abs}')
                torch.cuda.synchronize()
                started = time.perf_counter()
                for _ in range(2):
                    actual = na2d(q, k, v, (9, 9), dilation=(1, 1))
                torch.cuda.synchronize()
                elapsed = (time.perf_counter() - started) / 2
                record = {
                    'shape': list(q.shape),
                    'rowBlock': block,
                    'seconds': elapsed,
                    'peakBytes': torch.cuda.max_memory_allocated(),
                    'maxAbs': max_abs,
                }
                records.append(record)
                print(json.dumps(record), flush=True)
                del actual
            except (torch.OutOfMemoryError, RuntimeError) as error:
                if 'out of memory' not in str(error).lower():
                    raise
                records.append({'shape': list(q.shape), 'rowBlock': block, 'error': str(error)})
                print(json.dumps(records[-1]), flush=True)
                break
        del q, k, v, reference
        torch.cuda.empty_cache()
    print(json.dumps({'hip': torch.version.hip, 'records': records}))


if __name__ == '__main__':
    main()
