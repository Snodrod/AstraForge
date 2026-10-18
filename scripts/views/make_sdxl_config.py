"""Build a config-only diffusers folder for SDXL base 1.0 without any network access.

diffusers' `from_single_file` normally fetches the SDXL configs and tokenizers from the Hugging Face hub.
MV-Adapter runs on the `sd_xl_base_1.0.safetensors` that AstraForge already has, so everything except the
weights is rebuilt here: tokenizer files and the two CLIP text configs come from the ComfyUI checkout
(it ships the same tokenizer), the UNet / VAE / scheduler configs are the published SDXL base values.

    python make_sdxl_config.py <out_dir> [comfy_dir]

Idempotent: an existing complete folder is left alone.
"""
import json
import os
import shutil
import sys
from pathlib import Path

UNET = {
    "_class_name": "UNet2DConditionModel",
    "act_fn": "silu",
    "addition_embed_type": "text_time",
    "addition_embed_type_num_heads": 64,
    "addition_time_embed_dim": 256,
    "attention_head_dim": [5, 10, 20],
    "block_out_channels": [320, 640, 1280],
    "center_input_sample": False,
    "class_embed_type": None,
    "class_embeddings_concat": False,
    "conv_in_kernel": 3,
    "conv_out_kernel": 3,
    "cross_attention_dim": 2048,
    "cross_attention_norm": None,
    "down_block_types": ["DownBlock2D", "CrossAttnDownBlock2D", "CrossAttnDownBlock2D"],
    "downsample_padding": 1,
    "dual_cross_attention": False,
    "encoder_hid_dim": None,
    "encoder_hid_dim_type": None,
    "flip_sin_to_cos": True,
    "freq_shift": 0,
    "in_channels": 4,
    "layers_per_block": 2,
    "mid_block_only_cross_attention": None,
    "mid_block_scale_factor": 1,
    "mid_block_type": "UNetMidBlock2DCrossAttn",
    "norm_eps": 1e-05,
    "norm_num_groups": 32,
    "num_attention_heads": None,
    "num_class_embeds": None,
    "only_cross_attention": False,
    "out_channels": 4,
    "projection_class_embeddings_input_dim": 2816,
    "resnet_out_scale_factor": 1.0,
    "resnet_skip_time_act": False,
    "resnet_time_scale_shift": "default",
    "reverse_transformer_layers_per_block": None,
    "sample_size": 128,
    "time_cond_proj_dim": None,
    "time_embedding_act_fn": None,
    "time_embedding_dim": None,
    "time_embedding_type": "positional",
    "timestep_post_act": None,
    "transformer_layers_per_block": [1, 2, 10],
    "up_block_types": ["CrossAttnUpBlock2D", "CrossAttnUpBlock2D", "UpBlock2D"],
    "upcast_attention": None,
    "use_linear_projection": True,
}
VAE = {
    "_class_name": "AutoencoderKL",
    "act_fn": "silu",
    "block_out_channels": [128, 256, 512, 512],
    "down_block_types": ["DownEncoderBlock2D"] * 4,
    "force_upcast": True,
    "in_channels": 3,
    "latent_channels": 4,
    "layers_per_block": 2,
    "norm_num_groups": 32,
    "out_channels": 3,
    "sample_size": 1024,
    "scaling_factor": 0.13025,
    "up_block_types": ["UpDecoderBlock2D"] * 4,
}
SCHEDULER = {
    "_class_name": "EulerDiscreteScheduler",
    "beta_end": 0.012,
    "beta_schedule": "scaled_linear",
    "beta_start": 0.00085,
    "clip_sample": False,
    "interpolation_type": "linear",
    "num_train_timesteps": 1000,
    "prediction_type": "epsilon",
    "sample_max_value": 1.0,
    "set_alpha_to_one": False,
    "sigma_max": None,
    "sigma_min": None,
    "skip_prk_steps": True,
    "steps_offset": 1,
    "timestep_spacing": "leading",
    "trained_betas": None,
    "use_karras_sigmas": False,
}
MODEL_INDEX = {
    "_class_name": "StableDiffusionXLPipeline",
    "force_zeros_for_empty_prompt": True,
    "add_watermarker": None,
    "scheduler": ["diffusers", "EulerDiscreteScheduler"],
    "text_encoder": ["transformers", "CLIPTextModel"],
    "text_encoder_2": ["transformers", "CLIPTextModelWithProjection"],
    "tokenizer": ["transformers", "CLIPTokenizer"],
    "tokenizer_2": ["transformers", "CLIPTokenizer"],
    "unet": ["diffusers", "UNet2DConditionModel"],
    "vae": ["diffusers", "AutoencoderKL"],
}


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding="utf8")


def main(argv):
    out = Path(argv[1])
    # ComfyUI install: argv[2], else env ASTRA_COMFY_DIR, else <app>/runtime/ComfyUI
    comfy = Path(argv[2] if len(argv) > 2 else (os.environ.get("ASTRA_COMFY_DIR")
                                                or Path(__file__).resolve().parents[2] / "runtime" / "ComfyUI"))
    marker = out / "model_index.json"
    if marker.exists() and (out / "tokenizer_2" / "vocab.json").exists():
        print("sdxl-config already built:", out)
        return 0
    src_tok = comfy / "comfy" / "sd1_tokenizer"
    clip_l = comfy / "comfy" / "sd1_clip_config.json"
    clip_g = comfy / "comfy" / "clip_config_bigg.json"
    for need in (src_tok / "vocab.json", src_tok / "merges.txt", clip_l, clip_g):
        if not need.exists():
            raise SystemExit("ComfyUI file not found: %s" % need)
    for name in ("tokenizer", "tokenizer_2"):
        folder = out / name
        folder.mkdir(parents=True, exist_ok=True)
        for f in ("vocab.json", "merges.txt", "special_tokens_map.json", "tokenizer_config.json"):
            shutil.copyfile(src_tok / f, folder / f)
    # ComfyUI's copy says model_max_length 8192; the CLIP text encoders accept 77 tokens
    for name in ("tokenizer", "tokenizer_2"):
        p = out / name / "tokenizer_config.json"
        data = json.loads(p.read_text(encoding="utf8"))
        data["model_max_length"] = 77
        p.write_text(json.dumps(data, indent=2), encoding="utf8")
    # SDXL's second tokenizer pads with "!" (id 0) instead of <|endoftext|>
    for f in ("special_tokens_map.json", "tokenizer_config.json"):
        p = out / "tokenizer_2" / f
        data = json.loads(p.read_text(encoding="utf8"))
        data["pad_token"] = "!"
        p.write_text(json.dumps(data, indent=2), encoding="utf8")
    write(out / "text_encoder" / "config.json", json.loads(clip_l.read_text(encoding="utf8")))
    g = json.loads(clip_g.read_text(encoding="utf8"))
    g["architectures"] = ["CLIPTextModelWithProjection"]
    write(out / "text_encoder_2" / "config.json", g)
    write(out / "unet" / "config.json", UNET)
    write(out / "vae" / "config.json", VAE)
    write(out / "scheduler" / "scheduler_config.json", SCHEDULER)
    write(marker, MODEL_INDEX)
    print("sdxl-config built:", out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
