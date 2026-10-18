"""AstraForge addition to ComfyUI-MVAdapter (Apache-2.0): one self-contained node for the "views" feature.

`AstraMVAdapterViews` loads SDXL base + the MV-Adapter image-to-multiview weights from LOCAL files only,
generates the requested azimuths from one reference image and hands every byte of GPU memory back, so the
next Hunyuan3D job in the same ComfyUI process finds the card free. The stock nodes keep the whole pipeline
cached on the GPU between prompts, which is not an option on a 12 GB card shared with Hunyuan3D.
"""
import gc
import os
import re

import torch

import folder_paths
from comfy import model_management as mm

from .utils import local_sdxl_config, convert_images_to_tensors, convert_tensors_to_images, PIPELINES
from .nodes import DiffusersMVSampler, DiffusersMVSchedulerLoader, LdmPipelineLoader


def _default_adapter_dir():
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", "..", "..", "models", "mvadapter"))


def _manual_offload(pipe, device, dtype):
    pipe.unet.to(device=device, dtype=dtype)
    pipe.cond_encoder.to(device=device, dtype=dtype)
    for module in (pipe.text_encoder, pipe.text_encoder_2, pipe.vae):
        module.to("cpu")

    def visiting(modules, owner, name):
        original = getattr(owner, name)

        def wrapped(*args, **kwargs):
            for m in modules:
                m.to(device)
            try:
                return original(*args, **kwargs)
            finally:
                for m in modules:
                    m.to("cpu")
                torch.cuda.empty_cache()
        setattr(owner, name, wrapped)

    visiting([pipe.text_encoder, pipe.text_encoder_2], pipe, "encode_prompt")
    visiting([pipe.vae], pipe.vae, "encode")
    visiting([pipe.vae], pipe.vae, "decode")

    # diffusers derives the execution device from the first component (the VAE, now on the CPU)
    class OnDevice(type(pipe)):
        @property
        def _execution_device(self):
            return device

    pipe.__class__ = OnDevice


class AstraMVAdapterViews:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "reference": ("IMAGE",),
                "ckpt_name": (folder_paths.get_filename_list("checkpoints"),),
                "azimuths": ("STRING", {"default": "0,90,180,270"}),
                "width": ("INT", {"default": 768, "min": 256, "max": 1024, "step": 64}),
                "height": ("INT", {"default": 768, "min": 256, "max": 1024, "step": 64}),
                "steps": ("INT", {"default": 30, "min": 4, "max": 100}),
                "cfg": ("FLOAT", {"default": 3.0, "min": 0.0, "max": 20.0, "step": 0.1}),
                "seed": ("INT", {"default": 21, "min": 0, "max": 0xFFFFFFFFFFFFFFFF}),
                "prompt": ("STRING", {"multiline": True, "default": "high quality, best quality"}),
                "negative_prompt": ("STRING", {"multiline": True, "default": "watermark, ugly, deformed, noisy, blurry, low contrast"}),
                "offload": (["text+vae", "none"], {"default": "text+vae"}),
            },
            "optional": {
                "adapter_dir": ("STRING", {"default": ""}),
                "adapter_name": ("STRING", {"default": "mvadapter_i2mv_sdxl.safetensors"}),
                "shift_scale": ("FLOAT", {"default": 8.0, "min": 0.0, "max": 50.0, "step": 1.0}),
            },
        }

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "run"
    CATEGORY = "AstraForge/3D"

    def run(self, reference, ckpt_name, azimuths, width, height, steps, cfg, seed, prompt, negative_prompt,
            offload, adapter_dir="", adapter_name="mvadapter_i2mv_sdxl.safetensors", shift_scale=8.0):
        azimuth_list = [int(x) % 360 for x in re.split(r"[,\s]+", azimuths.strip()) if x]
        if not azimuth_list:
            raise ValueError("azimuths is empty")
        adapter_dir = adapter_dir or _default_adapter_dir()
        if not os.path.isfile(os.path.join(adapter_dir, adapter_name)):
            raise FileNotFoundError("MV-Adapter weights not found: %s" % os.path.join(adapter_dir, adapter_name))
        if not local_sdxl_config():
            raise FileNotFoundError("SDXL config folder is missing: run scripts/views/make_sdxl_config.py")
        # ComfyUI keeps Hunyuan3D & co. on the GPU between prompts: give the memory back first
        mm.unload_all_models()
        mm.soft_empty_cache()
        device = mm.get_torch_device()
        dtype = torch.float16
        pipe = None
        try:
            pipe, vae, _ = LdmPipelineLoader().create_pipeline(ckpt_name, "MVAdapterI2MVSDXLPipeline")
            scheduler = DiffusersMVSchedulerLoader().load_scheduler(pipe, "DDPM", True, "interpolated", shift_scale)[0]
            pipe.vae = vae
            pipe.scheduler = scheduler
            pipe.init_custom_adapter(num_views=len(azimuth_list))
            pipe.load_custom_adapter(adapter_dir, weight_name=adapter_name)
            pipe.cond_encoder.to(device=device, dtype=dtype)
            if offload == "text+vae":
                # The UNet with its multi-view/reference attention (~7.5 GB) stays on the card; the text
                # encoders and the VAE visit it only while they work. (accelerate's enable_model_cpu_offload
                # copies the kwargs dicts and breaks MV-Adapter's reference cache, so this is done by hand.)
                _manual_offload(pipe, device, dtype)
            else:
                pipe = pipe.to(device, dtype)
            pipe.vae.enable_slicing()
            torch.cuda.reset_peak_memory_stats()
            sampler = DiffusersMVSampler()
            images = sampler.sample(
                pipe, len(azimuth_list), prompt, negative_prompt, height, width, steps, cfg, seed,
                reference_image=reference, azimuth_degrees=azimuth_list,
            )[0]
            peak = torch.cuda.max_memory_allocated() / 2 ** 30
            print("[AstraMVAdapterViews] views=%s size=%dx%d steps=%d peak VRAM %.2f GB" % (azimuth_list, width, height, steps, peak), flush=True)
            return (images.cpu(),)
        finally:
            if pipe is not None:
                try:
                    pipe.maybe_free_model_hooks()
                    pipe.to("cpu")
                except Exception:
                    pass
            del pipe
            gc.collect()
            mm.soft_empty_cache()
            torch.cuda.empty_cache()


NODE_CLASS_MAPPINGS = {"AstraMVAdapterViews": AstraMVAdapterViews}
NODE_DISPLAY_NAME_MAPPINGS = {"AstraMVAdapterViews": "AstraForge · MV-Adapter views (local, frees GPU)"}
