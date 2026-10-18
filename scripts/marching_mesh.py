"""Extract a scalar surface without artificial padding; level uses input units."""
import numpy as np
from skimage.measure import marching_cubes

def extract_mesh(volume, level=0.0):
    volume = np.asarray(volume, dtype=np.float32)
    if volume.ndim != 3 or min(volume.shape) < 2 or max(volume.shape) > 513:
        raise ValueError('Expected a 3D volume up to 513 cubed')
    if not np.isfinite(volume).all() or not np.isfinite(level):
        raise ValueError('Nonfinite field values')
    corners = volume[np.ix_([0, volume.shape[0]-1], [0, volume.shape[1]-1], [0, volume.shape[2]-1])]
    # The boundary corners lie outside the object. Infer the field's convention
    # instead of turning the zero-valued padding into an artificial outer shell.
    direction = 'ascent' if float(np.median(corners)) < level else 'descent'
    vertices, faces, _, _ = marching_cubes(volume, level=float(level), gradient_direction=direction, allow_degenerate=False, method='lewiner')
    dimensions = np.asarray(volume.shape, dtype=np.float32)-1
    vertices = (vertices/dimensions*2-1)*1.01
    # Comfy's Hunyuan decoder swaps grid axes; match its mesh coordinate convention.
    vertices = np.ascontiguousarray(vertices[:, ::-1], dtype=np.float32)
    faces = np.ascontiguousarray(faces[:, [0, 2, 1]], dtype=np.int64)
    return vertices, faces
