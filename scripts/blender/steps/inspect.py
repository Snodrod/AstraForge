"""inspect — import a model and report stats; optional preview sheet.

inputs.mesh: path (.glb/.gltf/.fbx/.obj/.stl/.blend); params.preview: bool (default true),
params.views: list of view names (af_common.VIEWS), params.size: px.
"""
import os

import af_common as af


def run(req):
    af.reset_scene()
    objs = af.import_any(req["inputs"]["mesh"])
    stats = af.scene_stats(objs)
    outputs = {}
    if req["params"].get("preview", True):
        views = req["params"].get("views", ["front", "left", "back", "three_quarter"])
        outputs["preview"] = af.render_views(af.out_path(req, "preview.png"), objs, views=views,
                                             size=req["params"].get("size", 384))
    mixamo = any(b.name.startswith("mixamorig") for a in af.armatures(objs) for b in a.data.bones)
    stats["mixamo_skeleton"] = mixamo
    stats["file"] = os.path.basename(req["inputs"]["mesh"])
    return {"outputs": outputs, "stats": stats}
