"""CPU-reference parity tests for the narrow native AMD TRELLIS.2 helpers."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

import torch

sys.path.insert(0, str(Path(__file__).parent / "patches"))

from trellis2_amd.attention import na2d, varlen_cross_attention, varlen_self_attention


def self_attention_reference(qkv, lengths):
    outputs = []
    start = 0
    for length in lengths:
        q, k, v = qkv[start:start + length].unbind(dim=1)
        outputs.append(torch.nn.functional.scaled_dot_product_attention(
            q.transpose(0, 1).unsqueeze(0),
            k.transpose(0, 1).unsqueeze(0),
            v.transpose(0, 1).unsqueeze(0),
        )[0].transpose(0, 1))
        start += length
    return torch.cat(outputs, dim=0)


def cross_attention_reference(q, kv, q_lengths, kv_lengths):
    outputs = []
    q_start = 0
    kv_start = 0
    for q_length, kv_length in zip(q_lengths, kv_lengths):
        k, v = kv[kv_start:kv_start + kv_length].unbind(dim=1)
        outputs.append(torch.nn.functional.scaled_dot_product_attention(
            q[q_start:q_start + q_length].transpose(0, 1).unsqueeze(0),
            k.transpose(0, 1).unsqueeze(0),
            v.transpose(0, 1).unsqueeze(0),
        )[0].transpose(0, 1))
        q_start += q_length
        kv_start += kv_length
    return torch.cat(outputs, dim=0)


def na2d_reference(q, k, v, kernel_size, dilation):
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


class SparseAttentionParityTests(unittest.TestCase):
    def test_varlen_self_attention_matches_reference_and_backward(self):
        torch.manual_seed(42)
        lengths = [1, 4, 2]
        qkv = torch.randn(sum(lengths), 3, 2, 8, dtype=torch.float64)
        expected_input = qkv.clone().requires_grad_(True)
        actual_input = qkv.clone().requires_grad_(True)
        expected = self_attention_reference(expected_input, lengths)
        actual = varlen_self_attention(actual_input, lengths)
        torch.testing.assert_close(actual, expected, rtol=1e-10, atol=1e-10)
        actual.sum().backward()
        expected.sum().backward()
        torch.testing.assert_close(actual_input.grad, expected_input.grad, rtol=1e-10, atol=1e-10)

    def test_varlen_cross_attention_accepts_non_contiguous_input(self):
        torch.manual_seed(43)
        q = torch.randn(7, 2, 2, 8, dtype=torch.float64)[:, 0].transpose(0, 1).transpose(0, 1)
        kv = torch.randn(10, 2, 2, 8, dtype=torch.float64)
        self.assertFalse(q.is_contiguous())
        actual = varlen_cross_attention(q, kv, [3, 4], [6, 4])
        expected = cross_attention_reference(q, kv, [3, 4], [6, 4])
        torch.testing.assert_close(actual, expected, rtol=1e-10, atol=1e-10)


class NafParityTests(unittest.TestCase):
    def test_naf_matches_small_cpu_reference_for_odd_shapes_and_dilation(self):
        for shape, dilation in [((3, 3), (1, 1)), ((4, 4), (2, 2)), ((5, 7), (1, 1))]:
            with self.subTest(shape=shape, dilation=dilation):
                torch.manual_seed(44)
                q = torch.randn(1, *shape, 2, 4, dtype=torch.float64)
                k = torch.randn_like(q)
                v = torch.randn(1, *shape, 2, 8, dtype=torch.float64)
                actual = na2d(q, k, v, (3, 3), dilation=dilation, row_block=2)
                expected = na2d_reference(q, k, v, (3, 3), dilation)
                torch.testing.assert_close(actual, expected, rtol=1e-10, atol=1e-10)

    def test_naf_rejects_unsupported_stride(self):
        q = torch.zeros(1, 2, 2, 1, 4)
        with self.assertRaisesRegex(NotImplementedError, "stride=1"):
            na2d(q, q, q, (3, 3), stride=2)


if __name__ == "__main__":
    unittest.main()
