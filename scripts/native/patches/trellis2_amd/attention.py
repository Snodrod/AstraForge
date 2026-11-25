"""SDPA and bounded PyTorch neighborhood attention for native AMD."""
from __future__ import annotations

import os

import torch
import torch.nn.functional as F


def varlen_self_attention(qkv, seq_lens):
    q, k, v = qkv.unbind(dim=1)
    outputs = []
    start = 0
    for length in seq_lens.tolist() if isinstance(seq_lens, torch.Tensor) else seq_lens:
        outputs.append(F.scaled_dot_product_attention(
            q[start:start + length].transpose(0, 1).unsqueeze(0),
            k[start:start + length].transpose(0, 1).unsqueeze(0),
            v[start:start + length].transpose(0, 1).unsqueeze(0),
        )[0].transpose(0, 1))
        start += length
    return torch.cat(outputs, dim=0)


def varlen_cross_attention(q, kv, q_seq_lens, kv_seq_lens):
    k, v = kv.unbind(dim=1)
    outputs = []
    q_start = 0
    kv_start = 0
    q_lengths = q_seq_lens.tolist() if isinstance(q_seq_lens, torch.Tensor) else q_seq_lens
    kv_lengths = kv_seq_lens.tolist() if isinstance(kv_seq_lens, torch.Tensor) else kv_seq_lens
    for q_length, kv_length in zip(q_lengths, kv_lengths):
        outputs.append(F.scaled_dot_product_attention(
            q[q_start:q_start + q_length].transpose(0, 1).unsqueeze(0),
            k[kv_start:kv_start + kv_length].transpose(0, 1).unsqueeze(0),
            v[kv_start:kv_start + kv_length].transpose(0, 1).unsqueeze(0),
        )[0].transpose(0, 1))
        q_start += q_length
        kv_start += kv_length
    return torch.cat(outputs, dim=0)


def na2d(q, k, v, kernel_size, dilation=(1, 1), stride=1, backend=None, scale=None, row_block=None):
    if stride != 1:
        raise NotImplementedError('The ROCm NAF fallback requires stride=1')
    kh, kw = kernel_size if isinstance(kernel_size, tuple) else (kernel_size, kernel_size)
    dh, dw = dilation if isinstance(dilation, tuple) else (dilation, dilation)
    scale = q.shape[-1] ** -0.5 if scale is None else scale
    batch, height, width, heads, dim = q.shape
    value_dim = v.shape[-1]
    k = k.permute(0, 3, 4, 1, 2).reshape(batch * heads, dim, height, width)
    v = v.permute(0, 3, 4, 1, 2).reshape(batch * heads, value_dim, height, width)
    pad_h = (kh - 1) * dh // 2
    pad_w = (kw - 1) * dw // 2
    outputs = []
    block = row_block if row_block is not None else int(os.environ.get('ASTRA_AMD_NAF_ROW_BLOCK', '2'))
    block = max(1, min(height, block))
    for start in range(0, height, block):
        rows = min(block, height - start)
        band_height = rows + (kh - 1) * dh
        band_start = max(0, start - pad_h)
        band_end = min(height, start + rows + pad_h)
        pad_top = band_start + pad_h - start
        pad_bottom = start + band_height - band_end - pad_h
        kb = F.pad(k[:, :, band_start:band_end, :], (pad_w, pad_w, pad_top, pad_bottom))
        vb = F.pad(v[:, :, band_start:band_end, :], (pad_w, pad_w, pad_top, pad_bottom))
        kb = F.unfold(kb, kernel_size=(kh, kw), dilation=(dh, dw))
        vb = F.unfold(vb, kernel_size=(kh, kw), dilation=(dh, dw))
        kb = kb.reshape(batch, heads, dim, kh, kw, rows, width).permute(0, 5, 6, 1, 3, 4, 2)
        vb = vb.reshape(batch, heads, value_dim, kh, kw, rows, width).permute(0, 5, 6, 1, 3, 4, 2)
        weights = torch.einsum('brwnd,brwnkld->brwnkl', q[:, start:start + rows], kb) * scale
        weights = weights.flatten(-2).softmax(dim=-1).unflatten(-1, (kh, kw))
        outputs.append(torch.einsum('brwnkl,brwnkld->brwnd', weights, vb))
    return torch.cat(outputs, dim=1)
