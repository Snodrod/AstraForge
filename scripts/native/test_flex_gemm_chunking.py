import unittest
from unittest.mock import patch

import torch
from flex_gemm.ops import spconv
from flex_gemm.ops.spconv import submanifold_conv3d
from flex_gemm.ops.spconv.submanifold_conv3d import Algorithm, SubMConv3dFunction


class ExplicitGemmChunkingTest(unittest.TestCase):
    def test_small_memory_budget_preserves_explicit_gemm_result(self):
        self.assertTrue(
            hasattr(submanifold_conv3d, 'EXPLICIT_GEMM_CHUNK_BYTES'),
            'Explicit GEMM needs a bounded im2col memory budget',
        )
        previous_algorithm = spconv.ALGORITHM
        spconv.ALGORITHM = Algorithm.EXPLICIT_GEMM
        try:
            feats = torch.tensor([
                [0.1, -0.2, 0.3],
                [0.4, 0.5, -0.6],
                [-0.7, 0.8, 0.9],
                [1.0, -1.1, 1.2],
                [-1.3, 1.4, -1.5],
            ], dtype=torch.float32)
            invalid = 0xffffffff
            neighbor_map = torch.tensor([
                [0, 1, invalid, 2, 3, invalid, 4, 0],
                [1, 2, 0, invalid, 4, 3, invalid, 1],
                [2, invalid, 1, 3, 0, 4, 2, invalid],
                [3, 4, 2, 0, invalid, 1, 3, 2],
                [4, 3, invalid, 1, 2, 0, 4, 3],
            ], dtype=torch.uint32)
            weight = torch.arange(4 * 2 * 2 * 2 * 3, dtype=torch.float32).reshape(4, 2, 2, 2, 3)
            weight = (weight - 47.5) / 100
            bias = torch.tensor([0.01, -0.02, 0.03, -0.04], dtype=torch.float32)

            expected = bias.repeat(5, 1).double()
            for row in range(5):
                for kernel_index in range(8):
                    source = int(neighbor_map[row, kernel_index])
                    if source == invalid:
                        continue
                    kernel = (kernel_index // 4, (kernel_index // 2) % 2, kernel_index % 2)
                    expected[row] += weight[:, kernel[0], kernel[1], kernel[2], :].double() @ feats[source].double()

            allocations = []
            original_zeros = torch.zeros
            def recording_zeros(*args, **kwargs):
                allocations.append(tuple(args[0]))
                return original_zeros(*args, **kwargs)

            with patch.object(submanifold_conv3d, 'EXPLICIT_GEMM_CHUNK_BYTES', 64), \
                    patch.object(torch, 'zeros', side_effect=recording_zeros):
                actual = SubMConv3dFunction._sparse_submanifold_conv_forward(
                    feats, {'neighbor_map': neighbor_map}, weight, bias,
                )

            torch.testing.assert_close(actual, expected.float(), atol=1e-6, rtol=1e-6)
            self.assertEqual(allocations, [(8, 3)] * 5)
        finally:
            spconv.ALGORITHM = previous_algorithm


if __name__ == '__main__':
    unittest.main()
