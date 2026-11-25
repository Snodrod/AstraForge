import unittest

import torch
from flex_gemm.ops.grid_sample import grid_sample


class WeightedSumTest(unittest.TestCase):
    def test_torch_weighted_sum_handles_invalid_indices(self):
        self.assertTrue(hasattr(grid_sample, '_indice_weighted_sum'), 'Native forward needs a PyTorch weighted sum')
        feats = torch.tensor([
            [1.0, 2.0],
            [3.0, 5.0],
            [7.0, 11.0],
        ])
        indices = torch.tensor([
            [0, 1, 0xffffffff, 2],
            [2, 0xffffffff, 1, 0],
        ], dtype=torch.uint32)
        weight = torch.tensor([
            [0.1, 0.2, 0.0, 0.3],
            [0.4, 0.0, 0.5, 0.6],
        ])
        expected = torch.tensor([
            [0.1 * 1 + 0.2 * 3 + 0.3 * 7, 0.1 * 2 + 0.2 * 5 + 0.3 * 11],
            [0.4 * 7 + 0.5 * 3 + 0.6 * 1, 0.4 * 11 + 0.5 * 5 + 0.6 * 2],
        ])
        actual = grid_sample._indice_weighted_sum(feats, indices, weight)
        torch.testing.assert_close(actual, expected)


if __name__ == '__main__':
    unittest.main()
