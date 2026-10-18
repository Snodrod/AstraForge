"""AstraForge's isolated logit-to-mesh node. No external services or models."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[4] / 'scripts'))
from marching_mesh import extract_mesh

class AstraMarchingCubes:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {'voxel': ('VOXEL',), 'level': ('FLOAT', {'default': 0.5, 'min': 0.0, 'max': 1.0, 'step': .01})}}
    RETURN_TYPES = ('MESH',)
    FUNCTION = 'execute'
    CATEGORY = 'AstraForge/3D'
    def execute(self, voxel, level):
        import torch
        from comfy_api.latest._util import MESH
        from comfy_extras.nodes_save_3d import pack_variable_mesh_batch
        from comfy.utils import ProgressBar
        progress = ProgressBar(len(voxel.data))
        vertices, faces = [], []
        for field in voxel.data:
            v, f = extract_mesh(field.float().cpu().numpy(), level)
            vertices.append(torch.from_numpy(v))
            faces.append(torch.from_numpy(f))
            progress.update(1)
        if len(vertices) == 1:
            return (MESH(vertices[0].unsqueeze(0), faces[0].unsqueeze(0)),)
        return (pack_variable_mesh_batch(vertices, faces),)

NODE_CLASS_MAPPINGS = {'AstraMarchingCubes': AstraMarchingCubes}
NODE_DISPLAY_NAME_MAPPINGS = {'AstraMarchingCubes': 'AstraForge · Marching Cubes / original logits'}
