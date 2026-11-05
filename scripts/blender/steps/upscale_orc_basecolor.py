"""Replace only the packed Orc base-color image and save a separate BLEND."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

import bpy


def main():
    argv = sys.argv[sys.argv.index("--") + 1:] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("atlas", type=Path)
    parser.add_argument("report", type=Path)
    args = parser.parse_args(argv)

    source = args.input.resolve()
    output = args.output.resolve()
    atlas = args.atlas.resolve()
    if source == output:
        raise ValueError("Input and output BLEND paths must differ")

    bpy.ops.wm.open_mainfile(filepath=str(source))
    rig = bpy.data.objects.get("ORC_RIG")
    body = bpy.data.objects.get("ORC_Body")
    if rig is None or body is None or rig.type != "ARMATURE" or body.type != "MESH":
        raise RuntimeError("Expected ORC_RIG armature and ORC_Body mesh")

    base_color_nodes = []
    for material in bpy.data.materials:
        if not material.use_nodes:
            continue
        for node in material.node_tree.nodes:
            if node.type != "TEX_IMAGE" or node.image is None:
                continue
            for link in material.node_tree.links:
                if link.from_node == node and link.to_socket.name == "Base Color":
                    base_color_nodes.append((material.name, node))
    unique_images = {node.image.as_pointer(): node.image for _, node in base_color_nodes}
    if len(unique_images) != 1:
        raise RuntimeError(f"Expected one base-color image, found {len(unique_images)}")

    source_image = next(iter(unique_images.values()))
    before = {
        "name": source_image.name,
        "size": list(source_image.size),
        "packed": bool(source_image.packed_file),
        "materials": sorted({material for material, _ in base_color_nodes}),
    }
    image = bpy.data.images.load(str(atlas), check_existing=False)
    if list(image.size) != [4096, 4096]:
        raise RuntimeError(f"Unexpected atlas size: {list(image.size)}")
    image.name = "ORC_BaseColor_4096"
    image.pack()
    for _, node in base_color_nodes:
        node.image = image
    source_image_removed = source_image.users == 0
    if source_image_removed:
        bpy.data.images.remove(source_image)

    output.parent.mkdir(parents=True, exist_ok=True)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.wm.save_as_mainfile(filepath=str(output), copy=True)

    deform_bones = [bone.name for bone in rig.data.bones if bone.use_deform]
    sockets = sorted(obj.name for obj in bpy.data.objects if obj.name.startswith("socket_"))
    report = {
        "ok": True,
        "input": str(source),
        "output": str(output),
        "atlas": str(atlas),
        "atlasSha256": hashlib.sha256(atlas.read_bytes()).hexdigest(),
        "before": before,
        "after": {
            "name": image.name,
            "size": list(image.size),
            "packed": bool(image.packed_file),
            "unusedSourceImageRemoved": source_image_removed,
        },
        "objects": {"rig": rig.name, "body": body.name},
        "bones": len(rig.data.bones),
        "deformBones": len(deform_bones),
        "sockets": sockets,
        "bodyVertices": len(body.data.vertices),
        "bodyPolygons": len(body.data.polygons),
        "outputBytes": output.stat().st_size,
        "outputSha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "scope": "Only Base Color texture nodes were assigned to the verified packed 4096 atlas; geometry and rig were not edited",
    }
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("ORC_BLEND_UV4096_OK " + json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
