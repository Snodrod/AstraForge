"""Operational AMD probes for the native TRELLIS.2 shape-generation path."""
from __future__ import annotations

import argparse
import importlib
import json
import platform
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import torch
import torch.nn.functional as F

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(SCRIPT_DIR / "patches"))

from trellis2_amd.attention import na2d, varlen_self_attention

MANDATORY_SHAPE_CHECKS = ("sparse_sdpa", "naf", "flex_gemm", "o_voxel", "cumesh")
DEFAULT_OUTPUT = REPO_ROOT / "data" / "runtime" / "diagnostics" / "native-amd" / "extensions.json"


def shape_ready(checks):
    statuses = {item.get("name"): item.get("status") for item in checks}
    return all(statuses.get(name) == "pass" for name in MANDATORY_SHAPE_CHECKS)


def _assert_close(actual, expected, rtol=2e-5, atol=2e-5):
    torch.testing.assert_close(actual, expected, rtol=rtol, atol=atol)


def probe_sparse_sdpa():
    torch.manual_seed(1201)
    lengths = [1, 4, 2]
    qkv = torch.randn(sum(lengths), 3, 2, 8, device="cuda", requires_grad=True)
    actual = varlen_self_attention(qkv, lengths)
    expected_parts = []
    start = 0
    for length in lengths:
        q, k, v = qkv[start:start + length].unbind(dim=1)
        expected_parts.append(F.scaled_dot_product_attention(
            q.transpose(0, 1).unsqueeze(0),
            k.transpose(0, 1).unsqueeze(0),
            v.transpose(0, 1).unsqueeze(0),
        )[0].transpose(0, 1))
        start += length
    expected = torch.cat(expected_parts, dim=0)
    _assert_close(actual, expected)
    actual.square().mean().backward()
    if qkv.grad is None or not torch.isfinite(qkv.grad).all():
        raise RuntimeError("Sparse SDPA backward produced invalid gradients")
    return {"shape": list(actual.shape), "forwardBackward": True}


def probe_naf():
    torch.manual_seed(1202)
    q = torch.randn(1, 3, 5, 2, 4, device="cuda", requires_grad=True)
    k = torch.randn_like(q)
    v = torch.randn(1, 3, 5, 2, 6, device="cuda")
    actual = na2d(q, k, v, (3, 3), dilation=(1, 1), row_block=2)
    expected = na2d(q.detach().cpu(), k.cpu(), v.cpu(), (3, 3), dilation=(1, 1), row_block=2).cuda()
    _assert_close(actual, expected, rtol=1e-4, atol=1e-5)
    actual.mean().backward()
    if q.grad is None or not torch.isfinite(q.grad).all():
        raise RuntimeError("NAF backward produced invalid gradients")
    return {"shape": list(actual.shape), "cpuReference": True, "forwardBackward": True}


def probe_flex_gemm():
    from flex_gemm.ops import spconv
    from flex_gemm.ops.spconv.submanifold_conv3d import (
        SubMConv3dFunction,
        sparse_submanifold_conv3d,
    )

    spconv.set_algorithm(spconv.Algorithm.EXPLICIT_GEMM)
    coords = torch.tensor([
        [0, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0],
        [0, 1, 1, 0], [0, 2, 1, 1], [0, 2, 2, 1],
    ], dtype=torch.int32, device="cuda")
    shape = torch.Size([1, 2, 3, 3, 2])
    torch.manual_seed(1203)
    feats = torch.randn(coords.shape[0], 2, device="cuda", requires_grad=True)
    weight = torch.randn(3, 3, 3, 3, 2, device="cuda", requires_grad=True)
    bias = torch.randn(3, device="cuda", requires_grad=True)
    native_cache = SubMConv3dFunction._compute_neighbor_cache(coords, shape, (3, 3, 3), (1, 1, 1))
    reference_cache = SubMConv3dFunction._compute_neighbor_cache_torch(coords, shape, (3, 3, 3), (1, 1, 1))
    if not torch.equal(native_cache["neighbor_map"], reference_cache["neighbor_map"]):
        raise RuntimeError("Native HIP neighbor map differs from the PyTorch reference")

    reference_feats = feats.detach().clone().requires_grad_(True)
    reference_weight = weight.detach().clone().requires_grad_(True)
    reference_bias = bias.detach().clone().requires_grad_(True)
    actual, _ = sparse_submanifold_conv3d(feats, coords, shape, weight, bias, native_cache)
    expected, _ = sparse_submanifold_conv3d(
        reference_feats, coords, shape, reference_weight, reference_bias, reference_cache,
    )
    _assert_close(actual, expected)
    grad = torch.randn_like(actual)
    actual.backward(grad)
    expected.backward(grad)
    _assert_close(feats.grad, reference_feats.grad)
    _assert_close(weight.grad, reference_weight.grad)
    _assert_close(bias.grad, reference_bias.grad)
    return {
        "algorithm": spconv.ALGORITHM,
        "nativeNeighborMap": True,
        "shape": list(actual.shape),
        "forwardBackward": True,
        "triton": False,
    }


def probe_o_voxel():
    from o_voxel.serialize import decode_seq, encode_seq

    coords = torch.tensor([
        [0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1],
        [7, 3, 2], [31, 17, 9], [1023, 511, 255],
    ], dtype=torch.int32)
    results = {}
    for mode in ("z_order", "hilbert"):
        cpu_code = encode_seq(coords, mode=mode)
        gpu_code = encode_seq(coords.cuda(), mode=mode)
        if not torch.equal(cpu_code, gpu_code.cpu()):
            raise RuntimeError(f"O-Voxel {mode} GPU encoding differs from CPU")
        decoded = decode_seq(gpu_code, mode=mode)
        if not torch.equal(decoded.cpu(), coords):
            raise RuntimeError(f"O-Voxel {mode} round trip failed")
        results[mode] = True

    from o_voxel.convert import flexible_dual_grid_to_mesh, tiled_flexible_dual_grid_to_mesh

    coords = torch.tensor([
        [x, y, z]
        for x in range(2)
        for y in range(2)
        for z in range(2)
    ], dtype=torch.int32, device="cuda")
    dual_vertices = torch.full((len(coords), 3), 0.5, device="cuda")
    intersected = torch.ones((len(coords), 3), dtype=torch.bool, device="cuda")
    split_weight = torch.ones((len(coords), 1), device="cuda")
    kwargs = {
        "aabb": [[0.0, 0.0, 0.0], [2.0, 2.0, 2.0]],
        "grid_size": 2,
        "train": False,
    }
    direct = flexible_dual_grid_to_mesh(coords, dual_vertices, intersected, split_weight, **kwargs)
    tiled = tiled_flexible_dual_grid_to_mesh(
        coords, dual_vertices, intersected, split_weight, tile_size=1, **kwargs,
    )
    for name, (vertices, faces) in (("direct", direct), ("tiled", tiled)):
        if vertices is None or faces is None or not len(vertices) or not len(faces):
            raise RuntimeError(f"O-Voxel {name} conversion returned an empty mesh")
        if int(faces.min()) < 0 or int(faces.max()) >= len(vertices):
            raise RuntimeError(f"O-Voxel {name} conversion returned out-of-range face indices")
        areas = torch.cross(
            vertices[faces[:, 1]] - vertices[faces[:, 0]],
            vertices[faces[:, 2]] - vertices[faces[:, 0]],
            dim=-1,
        ).square().sum(dim=-1)
        if int((areas == 0).sum()) != 0:
            raise RuntimeError(f"O-Voxel {name} conversion returned degenerate faces")

    def face_keys(vertices, faces):
        triangles = vertices[faces].cpu().numpy()
        triangles.sort(axis=1)
        return sorted(map(tuple, triangles.reshape(len(triangles), -1).tolist()))

    direct_keys = face_keys(*direct)
    tiled_keys = face_keys(*tiled)
    if direct_keys != tiled_keys:
        raise RuntimeError(
            "O-Voxel tiled and direct conversion produced different topology: "
            f"direct={len(direct_keys)}, tiled={len(tiled_keys)}, "
            f"directOnly={len(set(direct_keys) - set(tiled_keys))}, "
            f"tiledOnly={len(set(tiled_keys) - set(direct_keys))}"
        )
    return {
        "gpuCpuParity": True,
        "roundTrip": results,
        "meshConversion": {
            "vertices": len(direct[0]),
            "faces": len(direct[1]),
            "directTiledTopologyMatch": True,
        },
    }


def probe_cumesh():
    from cumesh import CuMesh

    vertices = torch.tensor([
        [0.0, 0.0, 0.0], [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0], [0.0, 0.0, 1.0],
    ], device="cuda")
    faces = torch.tensor([[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]], dtype=torch.int32, device="cuda")
    mesh = CuMesh()
    mesh.init(vertices, faces)
    mesh.compute_face_normals()
    mesh.compute_vertex_normals()
    read_vertices, read_faces = mesh.read()
    _assert_close(read_vertices, vertices)
    if not torch.equal(read_faces, faces):
        raise RuntimeError("CuMesh changed the tetrahedron face indices")
    face_normals = mesh.read_face_normals()
    vertex_normals = mesh.read_vertex_normals()
    expected = torch.nn.functional.normalize(torch.cross(
        vertices[faces[:, 1]] - vertices[faces[:, 0]],
        vertices[faces[:, 2]] - vertices[faces[:, 0]],
        dim=-1,
    ), dim=-1)
    _assert_close(face_normals, expected, rtol=1e-4, atol=1e-5)
    if not torch.isfinite(vertex_normals).all():
        raise RuntimeError("CuMesh produced invalid vertex normals")

    large_vertex_count = 4_091_147
    large_face_count = 7_921_414
    large_vertices = torch.arange(
        large_vertex_count * 3, dtype=torch.float32, device="cuda",
    ).reshape(-1, 3) / large_vertex_count
    large_faces = (
        torch.arange(large_face_count * 3, dtype=torch.int32, device="cuda")
        .reshape(-1, 3) % large_vertex_count
    )
    large_mesh = CuMesh()
    large_mesh.init(large_vertices, large_faces)
    copied_vertices, copied_faces = large_mesh.read()
    if not torch.equal(copied_faces, large_faces):
        face_mismatches = (copied_faces != large_faces).any(dim=1)
        mismatch_index = int(face_mismatches.nonzero()[0])
        vertex_mismatches = int((copied_vertices != large_vertices).any(dim=1).sum())
        raise RuntimeError(
            "CuMesh large round trip changed mesh data: "
            f"faceMismatches={int(face_mismatches.sum())}, "
            f"vertexMismatches={vertex_mismatches}, firstFace={mismatch_index}, "
            f"actual={copied_faces[mismatch_index].tolist()}, "
            f"expected={large_faces[mismatch_index].tolist()}"
        )
    _assert_close(copied_vertices, large_vertices)
    del large_mesh, large_vertices, large_faces, copied_vertices, copied_faces

    open_vertices = torch.tensor([
        [0.0, 0.0, 0.0], [1.0, 0.0, 0.0],
        [1.0, 1.0, 0.0], [0.0, 1.0, 0.0],
    ], device="cuda")
    open_faces = torch.tensor([[0, 1, 2], [0, 2, 3]], dtype=torch.int32, device="cuda")
    open_mesh = CuMesh()

    def check_open_mesh(stage):
        current_vertices, current_faces = open_mesh.read()
        if not torch.isfinite(current_vertices).all():
            raise RuntimeError(f"CuMesh open-square {stage} produced non-finite vertices")
        if current_faces.numel():
            if int(current_faces.min()) < 0 or int(current_faces.max()) >= len(current_vertices):
                raise RuntimeError(
                    f"CuMesh open-square {stage} produced out-of-range faces: "
                    f"vertices={len(current_vertices)}, min={int(current_faces.min())}, "
                    f"max={int(current_faces.max())}"
                )
            repeated = (
                (current_faces[:, 0] == current_faces[:, 1])
                | (current_faces[:, 0] == current_faces[:, 2])
                | (current_faces[:, 1] == current_faces[:, 2])
            )
            if int(repeated.sum()):
                raise RuntimeError(f"CuMesh open-square {stage} produced repeated face indices")
        print(
            f"CuMesh open-square {stage}: vertices={len(current_vertices)}, "
            f"faces={len(current_faces)}",
            flush=True,
        )
        return current_vertices, current_faces

    open_mesh.init(open_vertices, open_faces)
    check_open_mesh("init")
    open_mesh.get_edges()
    check_open_mesh("edges")
    open_mesh.get_boundary_info()
    check_open_mesh("boundary info")
    if open_mesh.num_boundaries == 0:
        raise RuntimeError("CuMesh open-square boundary detection returned no boundaries")
    open_mesh.get_vertex_edge_adjacency()
    check_open_mesh("vertex-edge adjacency")
    open_mesh.get_vertex_boundary_adjacency()
    check_open_mesh("vertex-boundary adjacency")
    open_mesh.get_manifold_boundary_adjacency()
    check_open_mesh("manifold-boundary adjacency")
    open_mesh.read_manifold_boundary_adjacency()
    check_open_mesh("read manifold-boundary adjacency")
    open_mesh.get_boundary_connected_components()
    check_open_mesh("boundary components")
    open_mesh.get_boundary_loops()
    check_open_mesh("boundary loops")
    if open_mesh.num_boundary_loops == 0:
        raise RuntimeError("CuMesh open-square boundary loop detection returned no loops")
    open_mesh.fill_holes(max_hole_perimeter=10.0)
    filled_vertices, filled_faces = check_open_mesh("fill holes")
    if len(filled_vertices) != 5 or len(filled_faces) != 6:
        raise RuntimeError(
            "CuMesh open-square fill produced unexpected counts: "
            f"vertices={len(filled_vertices)}, faces={len(filled_faces)}"
        )
    return {
        "vertices": int(mesh.num_vertices),
        "faces": int(mesh.num_faces),
        "edges": int(mesh.num_edges),
        "finiteNormals": True,
        "largeRoundTrip": {
            "vertices": large_vertex_count,
            "faces": large_face_count,
            "exactMatch": True,
        },
        "openSquareFill": {
            "vertices": len(filled_vertices),
            "faces": len(filled_faces),
        },
    }


def probe_optional(module_name):
    importlib.import_module(module_name)
    return {"importOnly": True}


PROBES = {
    "sparse_sdpa": probe_sparse_sdpa,
    "naf": probe_naf,
    "flex_gemm": probe_flex_gemm,
    "o_voxel": probe_o_voxel,
    "cumesh": probe_cumesh,
    "natten": lambda: probe_optional("natten"),
    "nvdiffrast": lambda: probe_optional("nvdiffrast"),
}


def write_report(output, metadata, checks):
    report = {
        **metadata,
        "shapeReady": shape_ready(checks),
        "checks": checks,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    metadata = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "platform": platform.platform(),
        "python": platform.python_version(),
        "torch": torch.__version__,
        "hip": torch.version.hip,
        "cudaAvailable": torch.cuda.is_available(),
        "gpu": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
    }
    checks = []
    write_report(args.output, metadata, checks)
    for name, probe in PROBES.items():
        item = {"name": name, "mandatory": name in MANDATORY_SHAPE_CHECKS}
        try:
            item["details"] = probe()
            item["status"] = "pass"
        except ModuleNotFoundError as exc:
            item["status"] = "unsupported" if not item["mandatory"] else "fail"
            item["error"] = str(exc)
        except Exception as exc:
            item["status"] = "fail"
            item["error"] = f"{type(exc).__name__}: {exc}"
            item["traceback"] = traceback.format_exc(limit=8)
        checks.append(item)
        write_report(args.output, metadata, checks)
        print(f"{name}: {item['status']}", flush=True)
        if item["status"] != "pass":
            print(item["error"], file=sys.stderr, flush=True)
    if torch.cuda.is_available():
        torch.cuda.synchronize()
        torch.cuda.empty_cache()
    ready = shape_ready(checks)
    print(json.dumps({"shapeReady": ready, "report": str(args.output)}, ensure_ascii=False))
    return 0 if ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
