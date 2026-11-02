"""Reviewed ROCm patches for the pinned ComfyUI-Trellis2 source."""
import re
import sys
from pathlib import Path

MARKER = 'ASTRAFORGE AMD ROCm PATCH'
NVDIFFRAST = re.compile(r'^import nvdiffrast\.torch as dr\s*$', re.M)


def replace_once(text, old, new, label):
    if new in text:
        return text
    if old not in text:
        raise RuntimeError(f'{label}: upstream anchor not found')
    return text.replace(old, new, 1)


def patch_nvidia_imports(node):
    for path in node.rglob('*.py'):
        text = path.read_text(encoding='utf-8')
        updated = NVDIFFRAST.sub(
            f'try:  # {MARKER}: texturing is not used by AstraForge\n'
            '    import nvdiffrast.torch as dr\n'
            'except ImportError:\n'
            '    dr = None',
            text,
        )
        if updated != text:
            path.write_text(updated, encoding='utf-8')


def patch_sparse_sdpa(node):
    config = node / 'trellis2/modules/sparse/config.py'
    text = config.read_text(encoding='utf-8')
    text = text.replace("['xformers', 'flash_attn', 'flash_attn_3']", "['xformers', 'flash_attn', 'flash_attn_3', 'sdpa']")
    text = text.replace("Literal['xformers', 'flash_attn']", "Literal['xformers', 'flash_attn', 'sdpa']")
    config.write_text(text, encoding='utf-8')

    nodes = node / 'nodes.py'
    text = nodes.read_text(encoding='utf-8')
    text = replace_once(
        text,
        '"sparse_backend": (["xformers","flash_attn"],{"default":"flash_attn"})',
        '"sparse_backend": (["xformers","flash_attn","sdpa"],{"default":"flash_attn"})',
        'sparse backend input',
    )
    text = replace_once(
        text,
        "        pipeline.low_vram = low_vram\n",
        """        pipeline.low_vram = low_vram
        if os.environ.get('ASTRA_AMD_ROCM') == '1':
            import flex_gemm.kernels.triton.spconv.config as flexgemm_config
            flexgemm_config.allow_tf32 = False
            from .trellis2.modules.sparse.conv import config as sparse_conv_config
            sparse_conv_config.FLEX_GEMM_ALGO = 'implicit_gemm'
""",
        'ROCm convolution backend',
    )
    nodes.write_text(text, encoding='utf-8')

    windowed = node / 'trellis2/modules/sparse/attention/windowed_attn.py'
    text = windowed.read_text(encoding='utf-8')
    text = replace_once(
        text,
        "    if config.ATTN == 'xformers':\n        if 'xops' not in globals():\n            import xformers.ops as xops\n        attn_func_args = {",
        "    attn_func_args = {}  # ASTRAFORGE AMD ROCm PATCH\n    if config.ATTN == 'xformers':\n        if 'xops' not in globals():\n            import xformers.ops as xops\n        attn_func_args = {",
        'window partition',
    )
    text = replace_once(
        text,
        "    elif config.ATTN == 'flash_attn':\n        if 'flash_attn' not in globals():\n            import flash_attn\n        out = flash_attn.flash_attn_varlen_qkvpacked_func(qkv_feats, **attn_func_args)",
        """    elif config.ATTN == 'sdpa':  # ASTRAFORGE AMD ROCm PATCH
        q, k, v = qkv_feats.unbind(dim=1)
        outputs = []
        start = 0
        for length in seq_lens.tolist():
            qi = q[start:start + length].transpose(0, 1).unsqueeze(0)
            ki = k[start:start + length].transpose(0, 1).unsqueeze(0)
            vi = v[start:start + length].transpose(0, 1).unsqueeze(0)
            outputs.append(torch.nn.functional.scaled_dot_product_attention(qi, ki, vi)[0].transpose(0, 1))
            start += length
        out = torch.cat(outputs, dim=0)
    elif config.ATTN == 'flash_attn':
        if 'flash_attn' not in globals():
            import flash_attn
        out = flash_attn.flash_attn_varlen_qkvpacked_func(qkv_feats, **attn_func_args)""",
        'windowed self attention',
    )
    text = replace_once(
        text,
        "    elif config.ATTN == 'flash_attn':\n        if 'flash_attn' not in globals():\n            import flash_attn\n        out = flash_attn.flash_attn_varlen_kvpacked_func(q_feats, kv_feats,",
        """    elif config.ATTN == 'sdpa':  # ASTRAFORGE AMD ROCm PATCH
        k, v = kv_feats.unbind(dim=1)
        outputs = []
        q_start = 0
        kv_start = 0
        for q_length, kv_length in zip(q_seq_lens.tolist(), kv_seq_lens.tolist()):
            qi = q_feats[q_start:q_start + q_length].transpose(0, 1).unsqueeze(0)
            ki = k[kv_start:kv_start + kv_length].transpose(0, 1).unsqueeze(0)
            vi = v[kv_start:kv_start + kv_length].transpose(0, 1).unsqueeze(0)
            outputs.append(torch.nn.functional.scaled_dot_product_attention(qi, ki, vi)[0].transpose(0, 1))
            q_start += q_length
            kv_start += kv_length
        out = torch.cat(outputs, dim=0)
    elif config.ATTN == 'flash_attn':
        if 'flash_attn' not in globals():
            import flash_attn
        out = flash_attn.flash_attn_varlen_kvpacked_func(q_feats, kv_feats,""",
        'windowed cross attention',
    )
    windowed.write_text(text, encoding='utf-8')
    patch_lazy_precision(node)


def patch_lazy_precision(node):
    path = node / 'trellis2/pipelines/trellis2_image_to_3d.py'
    text = path.read_text(encoding='utf-8')
    text = replace_once(
        text,
        "            self.models['sparse_structure_decoder'].to(self._device)\n",
        """            self.models['sparse_structure_decoder'].to(self._device)
            if os.environ.get('ASTRA_AMD_ROCM') == '1':
                # ASTRAFORGE AMD ROCm PATCH: avoid MIOpen fp16 3D-conv NaNs.
                self.models['sparse_structure_decoder'].convert_to_fp32()
""",
        'lazy sparse decoder precision',
    )
    text = replace_once(
        text,
        "            self.models['shape_slat_decoder'].to(self._device)\n",
        """            self.models['shape_slat_decoder'].to(self._device)
            if os.environ.get('ASTRA_AMD_ROCM') == '1':
                from ..modules.utils import MIX_PRECISION_MODULES
                decoder = self.models['shape_slat_decoder']
                decoder.blocks.float()
                def convert_to_bf16(module):
                    if isinstance(module, MIX_PRECISION_MODULES):
                        for parameter in module.parameters():
                            parameter.data = parameter.data.bfloat16()
                decoder.blocks.apply(convert_to_bf16)
                decoder.dtype = torch.bfloat16
                original_decoder_forward = decoder.forward
                def bf16_safe_forward(x, *args, **kwargs):
                    return original_decoder_forward(x.replace(x.feats.float()), *args, **kwargs)
                decoder.forward = bf16_safe_forward
""",
        'lazy shape decoder precision',
    )
    path.write_text(text, encoding='utf-8')


def patch_naf(root):
    path = root / 'src/layers/attentions.py'
    text = path.read_text(encoding='utf-8')
    text = replace_once(
        text,
        """try:
    NATTEN_RECENT = False
    from natten.functional import na2d_av, na2d_qk
except:
    NATTEN_RECENT = True
    from natten import na2d
""",
        """try:
    NATTEN_RECENT = True
    from natten import na2d
except ImportError:
    # ASTRAFORGE AMD ROCm PATCH: bounded PyTorch equivalent for NAF on ROCm.
    NATTEN_RECENT = True
    def na2d(q, k, v, kernel_size, dilation=(1, 1), stride=1, backend=None, scale=None):
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
        k = F.pad(k, (pad_w, pad_w, pad_h, pad_h))
        v = F.pad(v, (pad_w, pad_w, pad_h, pad_h))
        outputs = []
        import os
        row_block = max(1, min(height, int(os.environ.get('ASTRA_AMD_NAF_ROW_BLOCK', '4'))))
        for start in range(0, height, row_block):
            rows = min(row_block, height - start)
            band_height = rows + (kh - 1) * dh
            band = slice(start, start + band_height)
            kb = F.unfold(k[:, :, band, :], kernel_size=(kh, kw), dilation=(dh, dw))
            vb = F.unfold(v[:, :, band, :], kernel_size=(kh, kw), dilation=(dh, dw))
            kb = kb.reshape(batch, heads, dim, kh, kw, rows, width).permute(0, 5, 6, 1, 3, 4, 2)
            vb = vb.reshape(batch, heads, value_dim, kh, kw, rows, width).permute(0, 5, 6, 1, 3, 4, 2)
            weights = torch.einsum('brwnd,brwnkld->brwnkl', q[:, start:start + rows], kb) * scale
            weights = weights.flatten(-2).softmax(dim=-1).unflatten(-1, (kh, kw))
            outputs.append(torch.einsum('brwnkl,brwnkld->brwnd', weights, vb))
        return torch.cat(outputs, dim=1)
""",
        'NAF NATTEN import',
    )
    path.write_text(text, encoding='utf-8')
    hubconf = root / 'hubconf.py'
    text = hubconf.read_text(encoding='utf-8')
    hubconf.write_text(text.replace(
        'dependencies = ["torch", "natten"]',
        'dependencies = ["torch"]  # ASTRAFORGE AMD ROCm PATCH: PyTorch NAF fallback',
    ), encoding='utf-8')


def patch_o_voxel(root):
    # Shape-only export never enters postprocess.to_glb(); avoid loading the
    # NVIDIA-only rasterizer merely because o_voxel.__init__ imports it.
    patch_nvidia_imports(root / 'o_voxel')
    path = root / 'src/convert/flexible_dual_grid.cpp'
    text = path.read_text(encoding='utf-8')
    # HIP declares these vector names globally; keep the CPU-only algorithms
    # and their operator[] support under project-local names.
    for old, new in (('float3', 'ov_float3'), ('int3', 'ov_int3'), ('int4', 'ov_int4')):
        text = re.sub(rf'\b{old}\b', new, text)
    text = text.replace(
        'struct ov_int4 {int x, y, z, w; int& operator[](int i) {return (&x)[i];}};',
        'struct ov_int4 {size_t x, y, z, w; size_t& operator[](int i) {return (&x)[i];}};',
    )
    text = text.replace('ov_int4 quad_indices{i,', 'ov_int4 quad_indices{static_cast<size_t>(i),')
    path.write_text(text, encoding='utf-8')


def main():
    if sys.argv[1] == '--naf':
        node = Path(sys.argv[2]).resolve()
        patch_naf(node)
        print(f'{MARKER}: patched NAF at {node}')
        return
    node = Path(sys.argv[1]).resolve()
    patch_nvidia_imports(node)
    patch_sparse_sdpa(node)
    if len(sys.argv) > 2:
        flex_config = Path(sys.argv[2]) / 'flex_gemm/kernels/triton/spconv/config.py'
        text = flex_config.read_text(encoding='utf-8')
        flex_config.write_text(text.replace('allow_tf32 = True', 'allow_tf32 = False'), encoding='utf-8')
    if len(sys.argv) > 3:
        patch_o_voxel(Path(sys.argv[3]))
    print(f'{MARKER}: patched {node}')


if __name__ == '__main__':
    main()
