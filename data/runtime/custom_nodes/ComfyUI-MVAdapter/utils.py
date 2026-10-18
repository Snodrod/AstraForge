# Adapted from https://github.com/Limitex/ComfyUI-Diffusers/blob/main/utils.py

import io
import os
import torch
import requests
import numpy as np
from PIL import Image
from omegaconf import OmegaConf
from torchvision.transforms import ToTensor
from diffusers.pipelines.stable_diffusion.convert_from_ckpt import (
    assign_to_checkpoint,
    conv_attn_to_linear,
    create_vae_diffusers_config,
    renew_vae_attention_paths,
    renew_vae_resnet_paths,
)
from diffusers import (
    AutoencoderKL,
    DDIMScheduler,
    DDPMScheduler,
    DEISMultistepScheduler,
    DPMSolverMultistepScheduler,
    DPMSolverSinglestepScheduler,
    EulerAncestralDiscreteScheduler,
    EulerDiscreteScheduler,
    HeunDiscreteScheduler,
    KDPM2AncestralDiscreteScheduler,
    KDPM2DiscreteScheduler,
    UniPCMultistepScheduler,
    LCMScheduler,
    StableDiffusionXLPipeline,
)

from .mvadapter.pipelines.pipeline_mvadapter_t2mv_sdxl import MVAdapterT2MVSDXLPipeline
from .mvadapter.pipelines.pipeline_mvadapter_i2mv_sdxl import MVAdapterI2MVSDXLPipeline
from .mvadapter.pipelines.pipeline_mvadapter_i2mv_sd import MVAdapterI2MVSDPipeline
from .mvadapter.pipelines.pipeline_mvadapter_t2mv_sd import MVAdapterT2MVSDPipeline
from .mvadapter.utils import (
    get_orthogonal_camera,
    get_plucker_embeds_from_cameras_ortho,
    make_image_grid,
)


NODE_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")


def local_sdxl_config():
    """AstraForge patch: folder with SDXL configs and tokenizers (no weights), or None.

    <app>/data/runtime/custom_nodes/ComfyUI-MVAdapter -> <app>/data/models/mvadapter/sdxl-config
    (override with ASTRA_SDXL_CONFIG).
    """
    env = os.environ.get("ASTRA_SDXL_CONFIG")
    if env and os.path.isdir(env):
        return env
    here = os.path.dirname(os.path.abspath(__file__))
    guess = os.path.normpath(
        os.path.join(here, "..", "..", "..", "models", "mvadapter", "sdxl-config")
    )
    return guess if os.path.isfile(os.path.join(guess, "model_index.json")) else None

PIPELINES = {
    "StableDiffusionXLPipeline": StableDiffusionXLPipeline,
    "MVAdapterT2MVSDXLPipeline": MVAdapterT2MVSDXLPipeline,
    "MVAdapterI2MVSDXLPipeline": MVAdapterI2MVSDXLPipeline,
    "MVAdapterI2MVSDPipeline": MVAdapterI2MVSDPipeline,
    "MVAdapterT2MVSDPipeline": MVAdapterT2MVSDPipeline,
}

SCHEDULERS = {
    "DDIM": DDIMScheduler,
    "DDPM": DDPMScheduler,
    "DEISMultistep": DEISMultistepScheduler,
    "DPMSolverMultistep": DPMSolverMultistepScheduler,
    "DPMSolverSinglestep": DPMSolverSinglestepScheduler,
    "EulerAncestralDiscrete": EulerAncestralDiscreteScheduler,
    "EulerDiscrete": EulerDiscreteScheduler,
    "HeunDiscrete": HeunDiscreteScheduler,
    "KDPM2AncestralDiscrete": KDPM2AncestralDiscreteScheduler,
    "KDPM2Discrete": KDPM2DiscreteScheduler,
    "UniPCMultistep": UniPCMultistepScheduler,
    "LCM": LCMScheduler,
}

MVADAPTERS = [
    "mvadapter_t2mv_sdxl.safetensors",
    "mvadapter_i2mv_sdxl.safetensors",
    "mvadapter_i2mv_sdxl_beta.safetensors",
    "mvadapter_t2mv_sd21.safetensors",
    "mvadapter_i2mv_sd21.safetensors",
]


# Reference from : https://github.com/huggingface/diffusers/blob/main/scripts/convert_vae_pt_to_diffusers.py
def custom_convert_ldm_vae_checkpoint(checkpoint, config):
    vae_state_dict = checkpoint

    new_checkpoint = {}

    new_checkpoint["encoder.conv_in.weight"] = vae_state_dict["encoder.conv_in.weight"]
    new_checkpoint["encoder.conv_in.bias"] = vae_state_dict["encoder.conv_in.bias"]
    new_checkpoint["encoder.conv_out.weight"] = vae_state_dict[
        "encoder.conv_out.weight"
    ]
    new_checkpoint["encoder.conv_out.bias"] = vae_state_dict["encoder.conv_out.bias"]
    new_checkpoint["encoder.conv_norm_out.weight"] = vae_state_dict[
        "encoder.norm_out.weight"
    ]
    new_checkpoint["encoder.conv_norm_out.bias"] = vae_state_dict[
        "encoder.norm_out.bias"
    ]

    new_checkpoint["decoder.conv_in.weight"] = vae_state_dict["decoder.conv_in.weight"]
    new_checkpoint["decoder.conv_in.bias"] = vae_state_dict["decoder.conv_in.bias"]
    new_checkpoint["decoder.conv_out.weight"] = vae_state_dict[
        "decoder.conv_out.weight"
    ]
    new_checkpoint["decoder.conv_out.bias"] = vae_state_dict["decoder.conv_out.bias"]
    new_checkpoint["decoder.conv_norm_out.weight"] = vae_state_dict[
        "decoder.norm_out.weight"
    ]
    new_checkpoint["decoder.conv_norm_out.bias"] = vae_state_dict[
        "decoder.norm_out.bias"
    ]

    new_checkpoint["quant_conv.weight"] = vae_state_dict["quant_conv.weight"]
    new_checkpoint["quant_conv.bias"] = vae_state_dict["quant_conv.bias"]
    new_checkpoint["post_quant_conv.weight"] = vae_state_dict["post_quant_conv.weight"]
    new_checkpoint["post_quant_conv.bias"] = vae_state_dict["post_quant_conv.bias"]

    # Retrieves the keys for the encoder down blocks only
    num_down_blocks = len(
        {
            ".".join(layer.split(".")[:3])
            for layer in vae_state_dict
            if "encoder.down" in layer
        }
    )
    down_blocks = {
        layer_id: [key for key in vae_state_dict if f"down.{layer_id}" in key]
        for layer_id in range(num_down_blocks)
    }

    # Retrieves the keys for the decoder up blocks only
    num_up_blocks = len(
        {
            ".".join(layer.split(".")[:3])
            for layer in vae_state_dict
            if "decoder.up" in layer
        }
    )
    up_blocks = {
        layer_id: [key for key in vae_state_dict if f"up.{layer_id}" in key]
        for layer_id in range(num_up_blocks)
    }

    for i in range(num_down_blocks):
        resnets = [
            key
            for key in down_blocks[i]
            if f"down.{i}" in key and f"down.{i}.downsample" not in key
        ]

        if f"encoder.down.{i}.downsample.conv.weight" in vae_state_dict:
            new_checkpoint[f"encoder.down_blocks.{i}.downsamplers.0.conv.weight"] = (
                vae_state_dict.pop(f"encoder.down.{i}.downsample.conv.weight")
            )
            new_checkpoint[f"encoder.down_blocks.{i}.downsamplers.0.conv.bias"] = (
                vae_state_dict.pop(f"encoder.down.{i}.downsample.conv.bias")
            )

        paths = renew_vae_resnet_paths(resnets)
        meta_path = {"old": f"down.{i}.block", "new": f"down_blocks.{i}.resnets"}
        assign_to_checkpoint(
            paths,
            new_checkpoint,
            vae_state_dict,
            additional_replacements=[meta_path],
            config=config,
        )

    mid_resnets = [key for key in vae_state_dict if "encoder.mid.block" in key]
    num_mid_res_blocks = 2
    for i in range(1, num_mid_res_blocks + 1):
        resnets = [key for key in mid_resnets if f"encoder.mid.block_{i}" in key]

        paths = renew_vae_resnet_paths(resnets)
        meta_path = {"old": f"mid.block_{i}", "new": f"mid_block.resnets.{i - 1}"}
        assign_to_checkpoint(
            paths,
            new_checkpoint,
            vae_state_dict,
            additional_replacements=[meta_path],
            config=config,
        )

    mid_attentions = [key for key in vae_state_dict if "encoder.mid.attn" in key]
    paths = renew_vae_attention_paths(mid_attentions)
    meta_path = {"old": "mid.attn_1", "new": "mid_block.attentions.0"}
    assign_to_checkpoint(
        paths,
        new_checkpoint,
        vae_state_dict,
        additional_replacements=[meta_path],
        config=config,
    )
    conv_attn_to_linear(new_checkpoint)

    for i in range(num_up_blocks):
        block_id = num_up_blocks - 1 - i
        resnets = [
            key
            for key in up_blocks[block_id]
            if f"up.{block_id}" in key and f"up.{block_id}.upsample" not in key
        ]

        if f"decoder.up.{block_id}.upsample.conv.weight" in vae_state_dict:
            new_checkpoint[f"decoder.up_blocks.{i}.upsamplers.0.conv.weight"] = (
                vae_state_dict[f"decoder.up.{block_id}.upsample.conv.weight"]
            )
            new_checkpoint[f"decoder.up_blocks.{i}.upsamplers.0.conv.bias"] = (
                vae_state_dict[f"decoder.up.{block_id}.upsample.conv.bias"]
            )

        paths = renew_vae_resnet_paths(resnets)
        meta_path = {"old": f"up.{block_id}.block", "new": f"up_blocks.{i}.resnets"}
        assign_to_checkpoint(
            paths,
            new_checkpoint,
            vae_state_dict,
            additional_replacements=[meta_path],
            config=config,
        )

    mid_resnets = [key for key in vae_state_dict if "decoder.mid.block" in key]
    num_mid_res_blocks = 2
    for i in range(1, num_mid_res_blocks + 1):
        resnets = [key for key in mid_resnets if f"decoder.mid.block_{i}" in key]

        paths = renew_vae_resnet_paths(resnets)
        meta_path = {"old": f"mid.block_{i}", "new": f"mid_block.resnets.{i - 1}"}
        assign_to_checkpoint(
            paths,
            new_checkpoint,
            vae_state_dict,
            additional_replacements=[meta_path],
            config=config,
        )

    mid_attentions = [key for key in vae_state_dict if "decoder.mid.attn" in key]
    paths = renew_vae_attention_paths(mid_attentions)
    meta_path = {"old": "mid.attn_1", "new": "mid_block.attentions.0"}
    assign_to_checkpoint(
        paths,
        new_checkpoint,
        vae_state_dict,
        additional_replacements=[meta_path],
        config=config,
    )
    conv_attn_to_linear(new_checkpoint)
    return new_checkpoint


# Reference from : https://github.com/huggingface/diffusers/blob/main/scripts/convert_vae_pt_to_diffusers.py
def vae_pt_to_vae_diffuser(checkpoint_path: str, force_upcast: bool = True):
    try:
        config_path = os.path.join(
            NODE_CACHE_PATH, "stable-diffusion-v1-inference.yaml"
        )
        original_config = OmegaConf.load(config_path)
    except FileNotFoundError as e:
        print(f"Warning: {e}")

        r = requests.get(
            "https://raw.githubusercontent.com/CompVis/stable-diffusion/main/configs/stable-diffusion/v1-inference.yaml"
        )
        io_obj = io.BytesIO(r.content)
        original_config = OmegaConf.load(io_obj)

    image_size = 512
    device = "cuda" if torch.cuda.is_available() else "cpu"
    if checkpoint_path.endswith("safetensors"):
        from safetensors import safe_open

        checkpoint = {}
        with safe_open(checkpoint_path, framework="pt", device="cpu") as f:
            for key in f.keys():
                checkpoint[key] = f.get_tensor(key)
    else:
        checkpoint = torch.load(checkpoint_path, map_location=device)["state_dict"]

    # Convert the VAE model.
    vae_config = create_vae_diffusers_config(original_config, image_size=image_size)
    vae_config.update({"force_upcast": force_upcast})
    converted_vae_checkpoint = custom_convert_ldm_vae_checkpoint(checkpoint, vae_config)

    vae = AutoencoderKL(**vae_config)
    vae.load_state_dict(converted_vae_checkpoint)

    return vae


def convert_images_to_tensors(images: list[Image.Image]):
    return torch.stack([np.transpose(ToTensor()(image), (1, 2, 0)) for image in images])


def convert_tensors_to_images(images: torch.tensor):
    return [
        Image.fromarray(np.clip(255.0 * image.cpu().numpy(), 0, 255).astype(np.uint8))
        for image in images
    ]


def resize_images(images: list[Image.Image], size: tuple[int, int]):
    return [image.resize(size) for image in images]


def prepare_camera_embed(num_views, size, device, azimuth_degrees=None):
    cameras = get_orthogonal_camera(
        elevation_deg=[0] * num_views,
        distance=[1.8] * num_views,
        left=-0.55,
        right=0.55,
        bottom=-0.55,
        top=0.55,
        azimuth_deg=[x - 90 for x in azimuth_degrees],
        device=device,
    )

    plucker_embeds = get_plucker_embeds_from_cameras_ortho(
        cameras.c2w, [1.1] * num_views, size
    )
    control_images = ((plucker_embeds + 1.0) / 2.0).clamp(0, 1)

    return control_images


def preprocess_image(image: Image.Image, height, width):
    image = np.array(image)
    alpha = image[..., 3] > 0
    H, W = alpha.shape
    # get the bounding box of alpha
    y, x = np.where(alpha)
    y0, y1 = max(y.min() - 1, 0), min(y.max() + 1, H)
    x0, x1 = max(x.min() - 1, 0), min(x.max() + 1, W)
    image_center = image[y0:y1, x0:x1]
    # resize the longer side to H * 0.9
    H, W, _ = image_center.shape
    if H > W:
        W = int(W * (height * 0.9) / H)
        H = int(height * 0.9)
    else:
        H = int(H * (width * 0.9) / W)
        W = int(width * 0.9)
    image_center = np.array(Image.fromarray(image_center).resize((W, H)))
    # pad to H, W
    start_h = (height - H) // 2
    start_w = (width - W) // 2
    image = np.zeros((height, width, 4), dtype=np.uint8)
    image[start_h : start_h + H, start_w : start_w + W] = image_center
    image = image.astype(np.float32) / 255.0
    image = image[:, :, :3] * image[:, :, 3:4] + (1 - image[:, :, 3:4]) * 0.5
    image = (image * 255).clip(0, 255).astype(np.uint8)
    image = Image.fromarray(image)

    return image
