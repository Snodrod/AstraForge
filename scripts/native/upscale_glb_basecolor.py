#!/usr/bin/env python3
"""Upscale embedded glTF base-color atlases without changing geometry or rig data."""

import argparse
import copy
import hashlib
import io
import json
import math
import struct
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_glb(path):
    raw = path.read_bytes()
    magic, version, declared_length = struct.unpack_from("<4sII", raw, 0)
    if magic != b"glTF" or version != 2 or declared_length != len(raw):
        raise ValueError("Expected a valid glTF 2.0 GLB")
    offset = 12
    document = None
    binary = None
    while offset < len(raw):
        chunk_length, chunk_type = struct.unpack_from("<I4s", raw, offset)
        offset += 8
        chunk = raw[offset:offset + chunk_length]
        offset += chunk_length
        if chunk_type == b"JSON":
            document = json.loads(chunk.decode("utf-8"))
        elif chunk_type == b"BIN\x00":
            binary = chunk
    if document is None or binary is None:
        raise ValueError("GLB must contain JSON and BIN chunks")
    return raw, document, binary


def write_glb(path, document, binary):
    json_bytes = json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    json_bytes += b" " * ((-len(json_bytes)) % 4)
    binary = bytes(binary) + b"\x00" * ((-len(binary)) % 4)
    total_length = 12 + 8 + len(json_bytes) + 8 + len(binary)
    with path.open("wb") as handle:
        handle.write(struct.pack("<4sII", b"glTF", 2, total_length))
        handle.write(struct.pack("<I4s", len(json_bytes), b"JSON"))
        handle.write(json_bytes)
        handle.write(struct.pack("<I4s", len(binary), b"BIN\x00"))
        handle.write(binary)


def image_bytes(document, binary, source_index):
    image = document["images"][source_index]
    if "bufferView" not in image:
        raise ValueError(f"Image {source_index} is not embedded")
    view = document["bufferViews"][image["bufferView"]]
    start = view.get("byteOffset", 0)
    return binary[start:start + view["byteLength"]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--size", type=int, default=4096)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--atlas-dir", type=Path, required=True)
    args = parser.parse_args()
    if args.size < 2 or args.size > 16384:
        raise ValueError("--size must be between 2 and 16384")

    original_raw, source, original_binary = read_glb(args.input)
    document = copy.deepcopy(source)
    base_color = []
    for material_index, material in enumerate(document.get("materials", [])):
        texture_ref = material.get("pbrMetallicRoughness", {}).get("baseColorTexture")
        if texture_ref is None:
            continue
        texture_index = texture_ref["index"]
        source_index = document["textures"][texture_index]["source"]
        base_color.append({
            "materialIndex": material_index,
            "material": material.get("name", ""),
            "textureIndex": texture_index,
            "sourceIndex": source_index,
        })
    if not base_color:
        raise ValueError("No embedded base-color texture found")

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.atlas_dir.mkdir(parents=True, exist_ok=True)
    new_binary = bytearray(original_binary)
    upscales = []

    for source_index in sorted({entry["sourceIndex"] for entry in base_color}):
        source_bytes = image_bytes(source, original_binary, source_index)
        original_image = Image.open(io.BytesIO(source_bytes))
        original_image.load()
        original_size = list(original_image.size)
        target_size = [args.size, args.size]
        upscaled = original_image.resize(target_size, Image.Resampling.LANCZOS)
        png_buffer = io.BytesIO()
        upscaled.save(png_buffer, format="PNG", optimize=True, compress_level=9)
        png_bytes = png_buffer.getvalue()

        new_binary.extend(b"\x00" * ((-len(new_binary)) % 4))
        new_offset = len(new_binary)
        new_binary.extend(png_bytes)
        new_view_index = len(document["bufferViews"])
        document["bufferViews"].append({
            "buffer": 0,
            "byteOffset": new_offset,
            "byteLength": len(png_bytes),
        })
        image = document["images"][source_index]
        image["bufferView"] = new_view_index
        image["mimeType"] = "image/png"
        image.pop("uri", None)

        original_path = args.atlas_dir / f"basecolor-{source_index}-original.png"
        atlas_path = args.atlas_dir / f"basecolor-{source_index}-{args.size}.png"
        original_path.write_bytes(source_bytes)
        atlas_path.write_bytes(png_bytes)

        roundtrip = upscaled.resize(original_image.size, Image.Resampling.LANCZOS).convert("RGBA")
        original_rgba = original_image.convert("RGBA")
        original_array = np.asarray(original_rgba, dtype=np.float32)
        roundtrip_array = np.asarray(roundtrip, dtype=np.float32)
        difference = np.abs(original_array - roundtrip_array)
        mse = float(np.mean((original_array - roundtrip_array) ** 2))
        mae = float(np.mean(difference))
        psnr = None if mse == 0 else float(10 * math.log10((255.0 ** 2) / mse))

        comparison = Image.new("RGB", (original_image.width * 2, original_image.height), "#171b22")
        comparison.paste(original_rgba.convert("RGB"), (0, 0))
        comparison.paste(roundtrip.convert("RGB"), (original_image.width, 0))
        draw = ImageDraw.Draw(comparison)
        draw.rectangle((0, 0, 285, 26), fill="#000000")
        draw.text((8, 7), "Original embedded atlas", fill="#ffffff")
        draw.rectangle((original_image.width, 0, original_image.width * 2, 26), fill="#000000")
        draw.text((original_image.width + 8, 7), f"{args.size} upscale -> {original_image.width} roundtrip", fill="#ffffff")
        comparison_path = args.atlas_dir / f"basecolor-{source_index}-comparison.png"
        comparison.save(comparison_path, format="PNG", optimize=True)

        upscales.append({
            "sourceIndex": source_index,
            "originalSize": original_size,
            "targetSize": target_size,
            "originalMode": original_image.mode,
            "originalSha256": sha256(source_bytes),
            "upscaledSha256": sha256(png_bytes),
            "originalAtlas": str(original_path),
            "upscaledAtlas": str(atlas_path),
            "comparison": str(comparison_path),
            "newBufferView": new_view_index,
            "roundtrip": {
                "resampling": "Lanczos 4x upscale followed by Lanczos downscale",
                "meanAbsoluteError8Bit": mae,
                "maxAbsoluteError8Bit": int(difference.max()),
                "psnrDb": psnr,
            },
        })

    if not document.get("buffers") or document["buffers"][0].get("buffer") not in (None, 0):
        raise ValueError("Unexpected buffer layout")
    document["buffers"][0]["byteLength"] = len(new_binary)
    write_glb(args.output, document, new_binary)

    _, verified, verified_binary = read_glb(args.output)
    for entry in upscales:
        embedded = image_bytes(verified, verified_binary, entry["sourceIndex"])
        embedded_image = Image.open(io.BytesIO(embedded))
        if list(embedded_image.size) != [args.size, args.size]:
            raise RuntimeError("Embedded atlas dimensions failed verification")
        if sha256(embedded) != entry["upscaledSha256"]:
            raise RuntimeError("Embedded atlas bytes failed verification")

    preserved_sections = ["asset", "scene", "scenes", "nodes", "meshes", "skins", "animations", "accessors", "samplers"]
    report = {
        "ok": True,
        "input": str(args.input),
        "output": str(args.output),
        "inputBytes": len(original_raw),
        "outputBytes": args.output.stat().st_size,
        "inputSha256": sha256(original_raw),
        "outputSha256": sha256(args.output.read_bytes()),
        "baseColorMaterials": base_color,
        "upscales": upscales,
        "preservedJsonSections": {
            name: source.get(name) == verified.get(name) for name in preserved_sections
        },
        "originalBinaryPrefixUnchanged": verified_binary[:len(original_binary)] == original_binary,
        "otherImagesUnchanged": all(
            image_bytes(source, original_binary, index) == image_bytes(verified, verified_binary, index)
            for index in range(len(source.get("images", [])))
            if index not in {entry["sourceIndex"] for entry in upscales}
        ),
        "method": "Non-generative Lanczos resampling of the exact embedded base-color image; no repainting or synthesized details",
    }
    if not all(report["preservedJsonSections"].values()) or not report["originalBinaryPrefixUnchanged"] or not report["otherImagesUnchanged"]:
        raise RuntimeError("Geometry, rig, or unrelated texture verification failed")
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "ok": True,
        "output": str(args.output),
        "report": str(args.report),
        "upscales": [{"size": item["targetSize"], **item["roundtrip"]} for item in upscales],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
