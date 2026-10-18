"""
Convert the official timm copy of DINOv3 ViT-L/16 (timm/vit_large_patch16_dinov3.lvd1689m,
LVD-1689M distillation, DINOv3 licence) to the Hugging Face `transformers` DINOv3ViTModel layout
that ComfyUI-Trellis2 / Pixal3D loads from

    ComfyUI/models/facebook/dinov3-vitl16-pretrain-lvd1689m/{config.json, model.safetensors, preprocessor_config.json}

and verify numerically that the converted model reproduces the timm model through the SAME code path
the node uses (embeddings -> rope -> layers -> F.layer_norm without affine; token order
[CLS, 4 registers, patches]).

AstraForge installer copy. Run with the portable interpreter of the Pixal3D install, e.g.

    python_embeded\\python.exe -I tools\\dinov3_timm_to_hf.py ^
        --timm-dir ComfyUI\\models\\timm\\vit_large_patch16_dinov3.lvd1689m ^
        --out-dir  ComfyUI\\models\\facebook\\dinov3-vitl16-pretrain-lvd1689m

    # logic self-test without any real weights (random weights, no network):
    python_embeded\\python.exe -I tools\\dinov3_timm_to_hf.py --random-weights-test

Note: timm states that the distilled ViT-B/L/H DINOv3 models have all-zero qkv biases, hence the
`vit_large_patch16_dinov3` (no qkv bias) variant is exact; the HF layout gets zero q/v biases.
"""
import argparse
import json
import os
import shutil
import sys
import tempfile

import numpy as np
import torch
import torch.nn.functional as F
from PIL import Image
from safetensors.torch import load_file, save_file

import timm
from transformers import DINOv3ViTConfig, DINOv3ViTModel

HF_KWARGS = dict(
    hidden_size=1024, intermediate_size=4096, num_hidden_layers=24, num_attention_heads=16,
    patch_size=16, num_register_tokens=4, layerscale_value=1.0, rope_theta=100.0,
    query_bias=True, key_bias=False, value_bias=True, proj_bias=True, mlp_bias=True,
    use_gated_mlp=False, hidden_act="gelu", layer_norm_eps=1e-5, image_size=224, num_channels=3,
)
IMNET_MEAN = (0.485, 0.456, 0.406)
IMNET_STD = (0.229, 0.224, 0.225)
N_REG = 4


def build_timm():
    return timm.create_model("vit_large_patch16_dinov3", pretrained=False, num_classes=0)


def timm_to_hf_state(sd, depth=24, dim=1024):
    """Map a timm Eva (DINOv3) state dict to the transformers DINOv3ViTModel state dict."""
    out = {}
    out["embeddings.cls_token"] = sd["cls_token"]
    out["embeddings.register_tokens"] = sd["reg_token"]
    out["embeddings.mask_token"] = torch.zeros(1, 1, dim, dtype=sd["cls_token"].dtype)  # unused at inference
    out["embeddings.patch_embeddings.weight"] = sd["patch_embed.proj.weight"]
    out["embeddings.patch_embeddings.bias"] = sd["patch_embed.proj.bias"]
    for i in range(depth):
        b, h = f"blocks.{i}.", f"layer.{i}."
        out[h + "layer_scale1.lambda1"] = sd[b + "gamma_1"]
        out[h + "layer_scale2.lambda1"] = sd[b + "gamma_2"]
        for n in ("norm1", "norm2"):
            out[h + n + ".weight"] = sd[b + n + ".weight"]
            out[h + n + ".bias"] = sd[b + n + ".bias"]
        q, k, v = sd[b + "attn.qkv.weight"].chunk(3, dim=0)
        out[h + "attention.q_proj.weight"] = q
        out[h + "attention.k_proj.weight"] = k
        out[h + "attention.v_proj.weight"] = v
        if b + "attn.qkv.bias" in sd:                       # qkvb variants
            qb, kb, vb = sd[b + "attn.qkv.bias"].chunk(3, dim=0)
            assert float(kb.abs().max()) == 0.0, "k bias must be zero"
        else:
            qb = sd.get(b + "attn.q_bias", torch.zeros(dim, dtype=q.dtype))
            vb = sd.get(b + "attn.v_bias", torch.zeros(dim, dtype=q.dtype))
        out[h + "attention.q_proj.bias"] = qb
        out[h + "attention.v_proj.bias"] = vb
        out[h + "attention.o_proj.weight"] = sd[b + "attn.proj.weight"]
        out[h + "attention.o_proj.bias"] = sd[b + "attn.proj.bias"]
        out[h + "mlp.up_proj.weight"] = sd[b + "mlp.fc1.weight"]
        out[h + "mlp.up_proj.bias"] = sd[b + "mlp.fc1.bias"]
        out[h + "mlp.down_proj.weight"] = sd[b + "mlp.fc2.weight"]
        out[h + "mlp.down_proj.bias"] = sd[b + "mlp.fc2.bias"]
    out["norm.weight"] = sd["norm.weight"]
    out["norm.bias"] = sd["norm.bias"]
    return {k: v.detach().clone().contiguous().float() for k, v in out.items()}


# ---- the two feature paths being compared -------------------------------------------------------
@torch.no_grad()
def feats_hf_node_path(hf, image):
    """Exactly ComfyUI-Trellis2 DinoV3ProjFeatureExtractor.extract_features (transformers < 5 branch)."""
    image = image.to(hf.embeddings.patch_embeddings.weight.dtype)
    hidden = hf.embeddings(image, bool_masked_pos=None)
    pos = hf.rope_embeddings(image)
    for layer in hf.layer:
        hidden = layer(hidden, position_embeddings=pos)
    return F.layer_norm(hidden, hidden.shape[-1:])


@torch.no_grad()
def feats_timm_same_path(tm, image):
    """timm model, same stages, final affine norm replaced by the node's non-affine layer_norm."""
    x = tm.patch_embed(image)
    x, rot = tm._pos_embed(x)
    x = tm.norm_pre(x)
    for blk in tm.blocks:
        x = blk(x, rope=rot)
    return F.layer_norm(x, x.shape[-1:])


def load_test_images(path, sizes):
    imgs = {}
    base = None
    if path and os.path.exists(path):
        im = Image.open(path)
        if im.mode == "RGBA":                               # premultiply on white like a cut-out
            bg = Image.new("RGB", im.size, (0, 0, 0))
            bg.paste(im, mask=im.split()[3])
            im = bg
        base = im.convert("RGB")
    rng = np.random.RandomState(0)
    for s in sizes:
        if base is not None:
            arr = np.asarray(base.resize((s, s), Image.LANCZOS), dtype=np.float32) / 255.0
            label = f"photo{s}"
        else:
            arr = rng.rand(s, s, 3).astype(np.float32)
            label = f"noise{s}"
        t = torch.from_numpy(arr).permute(2, 0, 1)[None]
        t = (t - torch.tensor(IMNET_MEAN).view(1, 3, 1, 1)) / torch.tensor(IMNET_STD).view(1, 3, 1, 1)
        imgs[label] = t
    return imgs


def parity(tm, hf, device, image_path, sizes):
    tm = tm.to(device).eval()
    hf = hf.to(device).eval()
    rows = []
    for label, img in load_test_images(image_path, sizes).items():
        img = img.to(device)
        a = feats_timm_same_path(tm, img)
        b = feats_hf_node_path(hf, img)
        n = (img.shape[-1] // 16) ** 2
        assert a.shape == b.shape == (1, 1 + N_REG + n, 1024), (a.shape, b.shape)
        d = (a - b).abs()
        row = dict(
            image=label, device=str(device), shape=list(b.shape),
            max_abs_patch=float(d[:, 1 + N_REG:].max()), max_abs_cls=float(d[:, :1].max()),
            max_abs_reg=float(d[:, 1:1 + N_REG].max()), max_abs_all=float(d.max()),
            mean_abs_feature=float(b.abs().mean()), max_abs_feature=float(b.abs().max()),
        )
        # full models incl. final affine norm: timm forward_features vs HF last_hidden_state
        fa = tm.forward_features(img)
        with torch.no_grad():
            fb = hf(pixel_values=img).last_hidden_state
        row["max_abs_with_final_norm"] = float((fa - fb).abs().max().detach())
        rows.append(row)
    return rows


def write_preprocessor_config(out_dir):
    cfg = {
        "do_normalize": True, "do_rescale": True, "do_resize": True,
        "image_mean": list(IMNET_MEAN), "image_std": list(IMNET_STD),
        "resample": 2, "rescale_factor": 1 / 255, "size": {"height": 224, "width": 224},
        "image_processor_type": "DINOv3ViTImageProcessorFast",
    }
    with open(os.path.join(out_dir, "preprocessor_config.json"), "w") as f:
        json.dump(cfg, f, indent=2)


def make_random_timm_dir(path):
    """Self-test helper: random-weight timm checkpoint in the same on-disk format as the hub repo."""
    torch.manual_seed(0)
    tm = build_timm()
    sd = {}
    for k, v in tm.state_dict().items():
        if "rope" in k:
            continue
        if k.endswith("gamma_1") or k.endswith("gamma_2"):
            sd[k] = torch.empty_like(v).uniform_(0.2, 1.0)
        elif k.endswith("norm.weight") or ".norm1.weight" in k or ".norm2.weight" in k:
            sd[k] = 1 + 0.1 * torch.randn_like(v)
        elif v.ndim == 1:
            sd[k] = 0.02 * torch.randn_like(v)
        else:
            sd[k] = 0.02 * torch.randn_like(v)
    os.makedirs(path, exist_ok=True)
    save_file({k: v.contiguous() for k, v in sd.items()}, os.path.join(path, "model.safetensors"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--timm-dir")
    ap.add_argument("--out-dir")
    ap.add_argument("--random-weights-test", action="store_true")
    ap.add_argument("--image", default=None,
                    help="optional photo for the parity test; random noise images are used without it")
    ap.add_argument("--sizes", default="512,1024")
    ap.add_argument("--report")
    a = ap.parse_args()
    sizes = [int(s) for s in a.sizes.split(",")]

    tmp = None
    if a.random_weights_test:
        tmp = tempfile.mkdtemp(prefix="dinov3_selftest_")
        a.timm_dir = os.path.join(tmp, "timm_random")
        a.out_dir = os.path.join(tmp, "hf_out")
        make_random_timm_dir(a.timm_dir)
        print("self-test with RANDOM weights in", tmp)
    if not a.timm_dir or not a.out_dir:
        ap.error("--timm-dir and --out-dir are required (or use --random-weights-test)")

    # 1) timm model with the checkpoint
    tm = build_timm()
    sd = load_file(os.path.join(a.timm_dir, "model.safetensors"))
    miss, unexp = tm.load_state_dict(sd, strict=False)
    print("timm load: missing", miss, "unexpected", unexp)
    assert not [m for m in miss if "rope" not in m], "timm checkpoint incomplete"
    assert not unexp, "unexpected keys in timm checkpoint"
    tm = tm.float().eval()

    # 2) HF model with mapped weights
    cfg = DINOv3ViTConfig(**HF_KWARGS)
    hf = DINOv3ViTModel(cfg)
    miss, unexp = hf.load_state_dict(timm_to_hf_state(tm.state_dict()), strict=True)
    print("HF strict load OK")
    # rope sanity: timm periods vs HF inv_freq
    p = tm.rope.periods.detach().cpu().float()
    inv = hf.rope_embeddings.inv_freq.detach().cpu().float()
    print("rope: timm periods", tuple(p.shape), " HF inv_freq", tuple(inv.shape),
          " max|1/periods-inv_freq| =", float((1.0 / p - inv).abs().max()))

    # 3) parity (CPU fp32 = reference numbers; CUDA fp32 as a second view)
    # CPU only for the smallest size (a 1024 px ViT-L forward on CPU is very slow), CUDA for all sizes
    rows = parity(tm, hf, torch.device("cpu"), a.image, sizes[:1])
    if torch.cuda.is_available():
        rows += parity(tm, hf, torch.device("cuda"), a.image, sizes)
    for r in rows:
        print(json.dumps(r))
    worst = max(r["max_abs_patch"] for r in rows)
    print(f"WORST max abs diff of patch-token features: {worst:.3e}  (target < 1e-3, ideal < 1e-4)")

    # 4) save in the layout the node expects
    hf = hf.cpu()
    os.makedirs(a.out_dir, exist_ok=True)
    hf.save_pretrained(a.out_dir, safe_serialization=True)
    write_preprocessor_config(a.out_dir)
    lic = os.path.join(a.timm_dir, "LICENSE.md")
    if os.path.exists(lic):
        shutil.copyfile(lic, os.path.join(a.out_dir, "LICENSE.md"))
    # reload from disk exactly like the node does and re-check on the saved copy
    hf2 = DINOv3ViTModel.from_pretrained(a.out_dir).eval()
    rows2 = parity(tm, hf2, torch.device("cpu"), a.image, sizes[:1])
    print("reload-from-disk check:", json.dumps(rows2[0]))
    if a.report:
        with open(a.report, "w") as f:
            json.dump(dict(rows=rows, reload=rows2, worst_patch_max_abs=worst), f, indent=2)
    print("saved ->", a.out_dir, os.listdir(a.out_dir))
    if tmp:
        shutil.rmtree(tmp, ignore_errors=True)
    sys.exit(0 if worst < 1e-3 else 2)


if __name__ == "__main__":
    main()
