import sys, json
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'data' / 'python'))
import numpy as np
import xatlas

source, destination = map(Path, sys.argv[1:3])
payload = json.loads(source.read_text(encoding='utf8'))
atlas = xatlas.Atlas()
triangle_count = 0
largest_extent = 0.0
for mesh in payload['meshes']:
    vertices = np.asarray(mesh['positions'], dtype=np.float32).reshape(-1, 3)
    faces = np.asarray(mesh['indices'], dtype=np.uint32).reshape(-1, 3)
    triangle_count += len(faces)
    largest_extent = max(largest_extent, float(np.ptp(vertices, axis=0).max()))
    atlas.add_mesh(vertices, faces)
options = xatlas.PackOptions()
options.padding = 4
options.bilinear = True
charts = xatlas.ChartOptions()
if triangle_count > 100000:
    # Bound dense charts before parameterization: an entire million-triangle
    # robe in one chart otherwise dominates the CPU solve for many minutes.
    charts.max_chart_area = max(largest_extent * largest_extent * 0.025, 1e-8)
    charts.max_boundary_length = max(largest_extent * 0.8, 1e-4)
atlas.generate(chart_options=charts, pack_options=options)
if atlas.atlas_count > 1:
    raise ValueError('UV generation produced multiple atlases; split the asset first')
out = []
for n, mesh in enumerate(payload['meshes']):
    mapping, faces, uv = atlas[n]
    out.append({'id': mesh['id'], 'mapping': mapping.tolist(), 'indices': faces.flatten().tolist(), 'uv': uv.flatten().tolist()})
destination.write_text(json.dumps({'meshes': out, 'width': atlas.width, 'height': atlas.height}, separators=(',', ':')), encoding='utf8')
print('UV atlas ready')
