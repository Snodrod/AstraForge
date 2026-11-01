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
            # ASTRAFORGE AMD ROCm PATCH: avoid MIOpen fp16 3D-conv NaNs.
            pipeline.models['sparse_structure_decoder'].convert_to_fp32()
            from .trellis2.modules.utils import MIX_PRECISION_MODULES
            decoder = pipeline.models['shape_slat_decoder']
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
            import flex_gemm.kernels.triton.spconv.config as flexgemm_config
            flexgemm_config.allow_tf32 = False
            from .trellis2.modules.sparse.conv import config as sparse_conv_config
            sparse_conv_config.FLEX_GEMM_ALGO = 'explicit_gemm'
""",
        'ROCm model precision',
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
