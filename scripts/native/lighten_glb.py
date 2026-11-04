import argparse
import json
from pathlib import Path

import pymeshlab
import trimesh


def lighten(input_path, output_path, target_faces=120000):
    if target_faces < 3:
        raise ValueError("target_faces must be at least 3")

    input_path = Path(input_path)
    output_path = Path(output_path)
    mesh = trimesh.load(input_path, force="mesh", process=False)
    source_triangles = len(mesh.faces)

    if source_triangles > target_faces:
        mesh_set = pymeshlab.MeshSet()
        mesh_set.add_mesh(pymeshlab.Mesh(vertex_matrix=mesh.vertices, face_matrix=mesh.faces))
        mesh_set.meshing_decimation_quadric_edge_collapse(targetfacenum=target_faces)
        reduced = mesh_set.current_mesh()
        mesh = trimesh.Trimesh(
            vertices=reduced.vertex_matrix(),
            faces=reduced.face_matrix(),
            process=False,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    mesh.export(output_path)
    report = {
        "input": str(input_path.resolve()),
        "output": str(output_path.resolve()),
        "sourceTriangles": source_triangles,
        "triangles": len(mesh.faces),
        "targetTriangles": target_faces,
        "method": "pymeshlab.meshing_decimation_quadric_edge_collapse",
    }
    print(json.dumps(report))
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--target-faces", type=int, default=120000)
    args = parser.parse_args()
    lighten(args.input, args.output, args.target_faces)
