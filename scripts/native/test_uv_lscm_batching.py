import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import torch

ROOT = Path(__file__).resolve().parents[2]
COMFYUI = ROOT / 'runtime/ComfyUI-Pixal3D-AMD-Native/ComfyUI'
sys.path.insert(0, str(COMFYUI))
from comfy_extras.mesh3d.uv_unwrap import parameterize


class LscmSolveBatchingTest(unittest.TestCase):
    def test_solver_preserves_results_within_batch_limit(self):
        self.assertTrue(hasattr(parameterize, '_solve_batched'), 'LSCM solve needs bounded GPU batches')
        generator = torch.Generator().manual_seed(42)
        matrices = torch.randn((8, 12, 12), dtype=torch.float64, generator=generator)
        matrices = matrices @ matrices.transpose(1, 2) + torch.eye(12, dtype=torch.float64)
        rhs = torch.randn((8, 12), dtype=torch.float64, generator=generator)
        expected = torch.linalg.solve(matrices, rhs)
        batches = []
        original_solve = torch.linalg.solve

        def recording_solve(a, b):
            batches.append(a.shape[0])
            return original_solve(a, b)

        with patch.object(parameterize, 'LSCM_SOLVE_BATCH_SIZE', 3), \
                patch.object(torch.linalg, 'solve', side_effect=recording_solve):
            actual = parameterize._solve_batched(matrices, rhs)

        torch.testing.assert_close(actual, expected)
        self.assertEqual(batches, [3, 3, 2])

    def test_amd_default_solver_uses_single_chart_batches(self):
        matrices = torch.eye(4, dtype=torch.float64).repeat(3, 1, 1)
        rhs = torch.arange(12, dtype=torch.float64).reshape(3, 4)
        expected = torch.linalg.solve(matrices, rhs)
        batches = []
        original_solve = torch.linalg.solve

        def recording_solve(a, b):
            batches.append(a.shape[0])
            return original_solve(a, b)

        with patch.object(torch.linalg, 'solve', side_effect=recording_solve):
            actual = parameterize._solve_batched(matrices, rhs)

        torch.testing.assert_close(actual, expected)
        self.assertEqual(batches, [1, 1, 1])


if __name__ == '__main__':
    unittest.main()
