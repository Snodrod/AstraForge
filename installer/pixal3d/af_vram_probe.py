"""AstraForge helper (not part of ComfyUI-Trellis2): device-wide VRAM peak probe for the evaluation runs.

    POST /af/vram/start   -> starts a 20 ms sampler of torch.cuda.mem_get_info(), remembers the baseline
    POST /af/vram/stop    -> returns {"baseline_used_mb", "peak_used_mb", "peak_delta_mb", "samples"}
    GET  /af/vram         -> current used/total

The numbers are DEVICE-WIDE (other programs on the GPU count too), so the peak is reported together with the
baseline taken at start.
"""
import threading
import time

import torch
from aiohttp import web
from server import PromptServer

_state = {"thread": None, "stop": None, "peak": 0, "base": 0, "n": 0}


def _used_mb():
    free, total = torch.cuda.mem_get_info()
    return (total - free) / 2**20, total / 2**20


def _sampler(stop):
    while not stop.is_set():
        try:
            used, _ = _used_mb()
            _state["n"] += 1
            if used > _state["peak"]:
                _state["peak"] = used
        except Exception:
            pass
        time.sleep(0.02)


@PromptServer.instance.routes.get("/af/vram")
async def af_vram(request):
    used, total = _used_mb()
    return web.json_response({"used_mb": used, "total_mb": total})


@PromptServer.instance.routes.post("/af/vram/start")
async def af_vram_start(request):
    if _state["stop"] is not None:
        _state["stop"].set()
    used, _ = _used_mb()
    _state.update(peak=used, base=used, n=0, stop=threading.Event())
    t = threading.Thread(target=_sampler, args=(_state["stop"],), daemon=True)
    _state["thread"] = t
    t.start()
    return web.json_response({"baseline_used_mb": used})


@PromptServer.instance.routes.post("/af/vram/stop")
async def af_vram_stop(request):
    if _state["stop"] is not None:
        _state["stop"].set()
    out = {"baseline_used_mb": _state["base"], "peak_used_mb": _state["peak"],
           "peak_delta_mb": _state["peak"] - _state["base"], "samples": _state["n"]}
    return web.json_response(out)


NODE_CLASS_MAPPINGS = {}
NODE_DISPLAY_NAME_MAPPINGS = {}
