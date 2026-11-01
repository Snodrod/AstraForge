import json
import os
import sys
import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from steps import _export_lib as xl

scene = bpy.context.scene
engine, device = xl.set_engine(scene, "CYCLES", 1)
assert device == "hip", f"HIP was not selected: {device}"
scene.render.resolution_x = 64
scene.render.resolution_y = 64
scene.render.resolution_percentage = 100
out = os.environ["ASTRA_AMD_HIP_OUT"]
scene.render.filepath = out
bpy.ops.render.render(write_still=True)
assert os.path.getsize(out) > 0, "empty HIP render"
print("AMD_HIP_OK " + json.dumps({"engine": engine, "device": device, "output": out}))
