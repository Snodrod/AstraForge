"""Orc-specific rig refinement: deterministic limb masks and a workable neutral stance."""
import json
import math
import os
import time

import bpy
import bmesh
import numpy as np
from mathutils import Matrix, Vector

import af_common as af
from steps import _humanoid_lib as hl


def welded_smooth_weights(body, W, H, iterations):
    """Smooth over coincident UV/normal duplicates without changing the mesh."""
    inv, _, adj = hl.welded_graph(body, H)
    count = np.bincount(inv, minlength=len(adj)).astype(np.float64)
    unique = np.zeros((len(adj), W.shape[1]), dtype=np.float64)
    for c in range(W.shape[1]):
        unique[:, c] = np.bincount(inv, weights=W[:, c], minlength=len(adj)) / np.maximum(count, 1)
    edges = np.array([(u, v) for u, links in enumerate(adj) for v, _ in links if u < v], dtype=np.int64)
    unique = hl.limit_normalize(hl.smooth_weights(edges, unique, 0.5, iterations), 4)
    return unique[inv]


def motion_group_matrix(W, names):
    """Return core/arm/leg influence sums and their source columns."""
    groups = [[] for _ in range(5)]
    for i, name in enumerate(hl.short(n) for n in names):
        gi = 0
        if name.startswith("Left") and name.endswith(("Arm", "ForeArm", "Hand")):
            gi = 1
        elif name.startswith("Right") and name.endswith(("Arm", "ForeArm", "Hand")):
            gi = 2
        elif name.startswith("Left") and name.endswith(("UpLeg", "Leg", "Foot", "ToeBase", "Toe_End")):
            gi = 3
        elif name.startswith("Right") and name.endswith(("UpLeg", "Leg", "Foot", "ToeBase", "Toe_End")):
            gi = 4
        groups[gi].append(i)
    return np.stack([W[:, cols].sum(1) for cols in groups], axis=1), groups


def prepare_orc_skin(body, rig, names, H, params):
    """Split fused anatomical seams, then smooth weights inside each motion group."""
    source = hl.read_weights(body, names)
    W = welded_smooth_weights(body, source, H, int(params.get("pre_smooth_iterations", 4)))
    G, groups = motion_group_matrix(W, names)
    bm = bmesh.new()
    bm.from_mesh(body.data)
    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    face_group = [int(np.argmax(G[[v.index for v in face.verts]].mean(0))) for face in bm.faces]
    boundaries = [e for e in bm.edges if len(e.link_faces) > 1 and any(
        face_group[f.index] != face_group[e.link_faces[0].index] for f in e.link_faces[1:])]
    vertices_before = len(bm.verts)
    bmesh.ops.split_edges(bm, edges=boundaries)
    bm.to_mesh(body.data)
    bm.free()
    body.data.update()

    W = hl.read_weights(body, names)
    G, groups = motion_group_matrix(W, names)
    memberships = [set() for _ in body.data.vertices]
    for poly in body.data.polygons:
        gi = int(np.argmax(G[list(poly.vertices)].mean(0)))
        for vi in poly.vertices:
            memberships[vi].add(gi)
    multi_group = sum(len(member) > 1 for member in memberships)
    labels = np.full(len(memberships), -1, dtype=np.int64)
    for vi, member in enumerate(memberships):
        if not member:
            continue
        gi = max(member, key=lambda k: float(G[vi, k]))
        labels[vi] = gi
        allowed = groups[gi]
        if W[vi, allowed].sum() < 1e-8:
            continue
        mask = np.ones(W.shape[1], dtype=bool)
        mask[allowed] = False
        W[vi, mask] = 0.0
    W = hl.limit_normalize(W, 4)
    post_iterations = int(params.get("post_smooth_iterations", 16))
    W = hl.limit_normalize(hl.smooth_weights(hl.mesh_edges(body), W, 0.5, post_iterations), 4)
    # Keep the dominant post-diffusion motion group at each contact, not a stale pre-smooth label.
    final_groups, _ = motion_group_matrix(W, names)
    final_labels = np.argmax(final_groups, axis=1)
    for vi, gi in enumerate(final_labels):
        mask = np.ones(W.shape[1], dtype=bool)
        mask[groups[gi]] = False
        W[vi, mask] = 0.0
    W = hl.limit_normalize(W, 4)
    hl.write_weights(body, names, W)
    info = {
        "method": "welded_smooth_anatomical_seams",
        "pre_smooth_iterations": int(params.get("pre_smooth_iterations", 4)),
        "post_smooth_iterations": post_iterations,
        "boundary_edges": len(boundaries),
        "vertices_before": vertices_before,
        "vertices_after": len(body.data.vertices),
        "multi_group_vertices": multi_group,
        "unweighted_vertices": int((W.sum(1) < 1e-6).sum()),
    }
    return W, info


def clean_orc_weights(verts, names, W, joints, params):
    """Partition arm/leg influences while preserving torso and two-sided cloth weights."""
    params = params or {}
    verts = np.asarray(verts, dtype=np.float64)
    out = np.asarray(W, dtype=np.float64).copy()
    short = [hl.short(n) for n in names]
    col = {n: i for i, n in enumerate(short)}
    arm_cols = [i for i, n in enumerate(short) if n.endswith(("Arm", "ForeArm", "Hand"))]
    leg_cols = [i for i, n in enumerate(short) if n.endswith(("UpLeg", "Leg", "Foot", "ToeBase", "Toe_End"))]
    groin = float(params.get("groin_z", 0.828))
    hand_radius = float(params.get("hand_radius_m", 0.105))
    wrist_radius = float(params.get("wrist_radius_m", 0.075))

    def segment_distance(points, a, b):
        ab = b - a
        denom = max(float(np.dot(ab, ab)), 1e-12)
        t = np.clip(((points - a) @ ab) / denom, 0.0, 1.0)
        closest = a + t[:, None] * ab
        return np.linalg.norm(points - closest, axis=1)

    hand_zone = np.zeros(len(verts), dtype=bool)
    for side in ("Left", "Right"):
        hand = np.asarray(joints[side + "Hand"], dtype=np.float64)
        tip = np.asarray(joints.get(side + "HandTip", hand), dtype=np.float64)
        elbow = np.asarray(joints[side + "ForeArm"], dtype=np.float64)
        hand_zone |= segment_distance(verts, hand, tip) < hand_radius
        hand_zone |= segment_distance(verts, elbow, hand) < wrist_radius

    below = verts[:, 2] < groin + 0.12
    protected_hands = below & hand_zone
    legs_only = below & ~hand_zone
    before = out.copy()
    out[legs_only[:, None] & np.isin(np.arange(len(names)), arm_cols)[None, :]] = 0.0
    out[protected_hands[:, None] & np.isin(np.arange(len(names)), leg_cols)[None, :]] = 0.0

    arm_sum = out[:, arm_cols].sum(axis=1)
    leg_sum = out[:, leg_cols].sum(axis=1)
    mixed = (arm_sum > 1e-6) & (leg_sum > 1e-6)
    out[np.ix_(mixed & (arm_sum >= leg_sum), leg_cols)] = 0.0
    out[np.ix_(mixed & (arm_sum < leg_sum), arm_cols)] = 0.0

    # If an automatic solver left almost nothing after the partition, use the anatomical chain.
    empty = out.sum(axis=1) < 1e-6
    for i in np.nonzero(empty)[0]:
        side = "Left" if verts[i, 0] >= 0 else "Right"
        if protected_hands[i]:
            bone = side + "Hand"
        elif verts[i, 2] >= float(joints[side + "Leg"][2]):
            bone = side + "UpLeg"
        elif verts[i, 2] >= float(joints[side + "Foot"][2]):
            bone = side + "Leg"
        else:
            bone = side + "Foot"
        out[i, col[bone]] = 1.0

    out = hl.limit_normalize(out, 4)
    info = {
        "leg_vertices_arm_removed": int(((before[:, arm_cols].sum(1) - out[:, arm_cols].sum(1)) > 1e-5).sum()),
        "hand_vertices_leg_removed": int((protected_hands & (before[:, leg_cols].sum(1) > 1e-5)).sum()),
        "protected_hand_vertices": int(protected_hands.sum()),
        "mixed_vertices_resolved": int(mixed.sum()),
        "fallback_vertices": int(empty.sum()),
        "unweighted_vertices": int((out.sum(1) < 1e-6).sum()),
    }
    return out, info


def run(req):
    """Refine an existing rigged BLEND without touching its source file."""
    os.makedirs(req["out_dir"], exist_ok=True)
    t0 = time.time()
    params = req.get("params") or {}
    outputs, checks, warnings = {}, [], []
    bodies = [o for o in bpy.context.scene.objects if o.type == "MESH" and any(
        m.type == "ARMATURE" and m.object for m in o.modifiers)]
    rigs = [o for o in bpy.context.scene.objects if o.type == "ARMATURE"]
    if len(bodies) != 1 or len(rigs) != 1:
        raise ValueError("rig_orc_refine expects exactly one skinned mesh and one armature")
    body, rig = bodies[0], rigs[0]
    names = hl.deform_names(rig)
    H = float(params.get("height_m", 1.8))

    W, weight_info = prepare_orc_skin(body, rig, names, H, params)

    def rotate_world(name, axis, degrees):
        pb = rig.pose.bones[hl.b(name)]
        bpy.context.view_layer.update()
        current = pb.matrix.copy()
        pivot = current.translation.copy()
        rotation = Matrix.Rotation(math.radians(degrees), 4, axis)
        pb.matrix = Matrix.Translation(pivot) @ rotation @ Matrix.Translation(-pivot) @ current
        bpy.context.view_layer.update()

    head_yaw = float(params.get("head_yaw_deg", 35.0))
    leg_inward = float(params.get("leg_inward_deg", 5.0))
    rotate_world("Head", "Z", head_yaw)
    for side, sign in (("Left", 1.0), ("Right", -1.0)):
        rotate_world(side + "UpLeg", "Y", sign * leg_inward)
        rotate_world(side + "Foot", "Y", -sign * leg_inward)

    # Bake the corrected silhouette and the pose together: the exported rest pose needs no corrective action.
    old_mod = next(m for m in body.modifiers if m.type == "ARMATURE" and m.object == rig)
    af.select_only([body])
    bpy.ops.object.modifier_apply(modifier=old_mod.name)
    af.select_only([rig])
    bpy.ops.object.mode_set(mode="POSE")
    bpy.ops.pose.select_all(action="SELECT")
    bpy.ops.pose.armature_apply(selected=False)
    bpy.ops.object.mode_set(mode="OBJECT")
    mod = body.modifiers.new("Armature · Orc neutral", "ARMATURE")
    mod.object = rig
    mod.use_vertex_groups = True
    bpy.context.view_layer.update()

    min_z = float(hl.mesh_verts(body)[:, 2].min())
    rig.location.z -= min_z
    bpy.context.view_layer.update()
    hl.reset_pose(rig)

    rig.name = "ORC_RIG"
    rig.data.name = "ORC_Deform_25"
    body.name = "ORC_Body"
    rig.show_in_front = True
    rig["profile"] = "orc-neutral-v1"
    rig["head_yaw_deg"] = head_yaw
    rig["leg_inward_deg"] = leg_inward
    rig["forward"] = "-Y in Blender; +Z after glTF Y-up export"
    body["profile"] = "orc-neutral-v1"

    collections = {}
    for label in ("01 · Core", "02 · Left limbs", "03 · Right limbs", "04 · Ends"):
        collection = rig.data.collections.get(label) or rig.data.collections.new(label)
        collections[label] = collection
    for bone in rig.data.bones:
        short = hl.short(bone.name)
        if short.startswith("Left"):
            label, palette = "02 · Left limbs", "THEME03"
        elif short.startswith("Right"):
            label, palette = "03 · Right limbs", "THEME02"
        elif short.endswith(("HeadTop_End", "Toe_End")):
            label, palette = "04 · Ends", "THEME05"
        else:
            label, palette = "01 · Core", "THEME04"
        collections[label].assign(bone)
        bone.color.palette = palette

    text = bpy.data.texts.get("ORC_START_HERE_RU") or bpy.data.texts.new("ORC_START_HERE_RU")
    text.clear()
    text.write(
        "ORC RIG · neutral-v1\n\n"
        "25 костей Mixamo, 22 deform bones, 9 sockets.\n"
        "Нейтральная стойка запечена в mesh и rest pose; corrective action не нужен.\n"
        "Вращайте кости ORC_RIG в Pose Mode. Имена mixamorig:* сохранены для ретаргета.\n"
        "Цвета: ядро — синее, левая сторона — зелёная, правая — красная.\n"
        "Веса разделены масками рук и ног; не включайте Bone Heat повторно без сохранения копии.\n"
        "Перед production проверяйте poses.png и result.json: предельные позы остаются обязательными.\n"
        f"Поворот головы: {head_yaw:.1f}°; сведение ног: {leg_inward:.1f}°.\n"
    )

    W = hl.read_weights(body, names)
    short_names = [hl.short(n) for n in names]
    arm_cols = [i for i, n in enumerate(short_names) if n.endswith(("Arm", "ForeArm", "Hand"))]
    leg_cols = [i for i, n in enumerate(short_names) if n.endswith(("UpLeg", "Leg", "Foot", "ToeBase", "Toe_End"))]
    mixed = int(((W[:, arm_cols].sum(1) > 1e-4) & (W[:, leg_cols].sum(1) > 1e-4)).sum())
    unweighted = int((W.sum(1) < 1e-6).sum())
    max_influences = int((W > 1e-4).sum(1).max())
    checks.append(af.check("unweighted_vertices", unweighted == 0, unweighted, 0))
    checks.append(af.check("bones", len(rig.data.bones) == 25, len(rig.data.bones), 25))
    checks.append(af.check("max_influences", max_influences <= 4, max_influences, 4))
    checks.append(af.check("arm_leg_weight_mix", mixed == 0, mixed, 0))

    deform = hl.deformation_test(body, rig, names, W, H, intersections=True, log=af.log)
    worst_pose = max(("arms_forward", "squat", "stride"), key=lambda p: deform[p]["torn_area_frac"])
    torn = deform[worst_pose]["torn_area_frac"]
    arms_up = deform["arms_up"]["torn_area_frac"]
    checks.append(af.check("deform_torn_area", torn <= hl.TORN_LIMITS["main"], torn,
                           hl.TORN_LIMITS["main"], note="worst: " + worst_pose))
    checks.append(af.check("deform_torn_area_arms_up", arms_up <= hl.TORN_LIMITS["arms_up"], arms_up,
                           hl.TORN_LIMITS["arms_up"]))
    worst = max(deform[p]["max_stretch"] for p in hl.TEST_POSES)
    p999 = max(deform[p]["p999_stretch"] for p in hl.TEST_POSES)
    nsi = max(deform[p]["new_self_intersection_frac"] for p in hl.TEST_POSES)
    checks.append(af.check("deform_max_edge_stretch", worst <= 6.0, worst, 6.0, severity="warning"))
    checks.append(af.check("deform_p999_edge_stretch", p999 <= 3.5, p999, 3.5, severity="warning"))
    checks.append(af.check("deform_new_self_intersections", nsi <= 0.02, nsi, 0.02, severity="warning"))

    outputs["rest_png"] = af.render_views(af.out_path(req, "rest.png"), [body],
                                           views=("front", "left", "three_quarter"), size=512, color="TEXTURE")
    outputs["poses_png"] = hl.render_poses(af.out_path(req, "poses.png"), body, rig, size=256)
    sockets = [o for o in bpy.context.scene.objects if o.name.startswith("socket_")]
    outputs["glb"] = af.export_glb(af.out_path(req, "orc-neutral.glb"), objects=[rig, body] + sockets,
                                    animations=False)
    af.select_only([rig])
    outputs["blend"] = af.save_blend(af.out_path(req, "orc-neutral.blend"))
    profile = {
        "format": "astraforge.orc-rig/1",
        "profile": "orc-neutral-v1",
        "source_blend": req.get("inputs", {}).get("blend"),
        "frame": "Blender: forward -Y, Z up, character left +X; glTF export is Y-up",
        "head_yaw_deg": head_yaw,
        "leg_inward_deg": leg_inward,
        "groin_z": float(params.get("groin_z", 0.828)),
        "hand_radius_m": float(params.get("hand_radius_m", 0.105)),
        "wrist_radius_m": float(params.get("wrist_radius_m", 0.075)),
        "weights": weight_info,
    }
    profile_path = af.out_path(req, "orc-rig-profile.json")
    with open(profile_path, "w", encoding="utf-8") as fh:
        json.dump(profile, fh, ensure_ascii=False, indent=2)
    outputs["profile_json"] = profile_path

    stats = {
        "bones": len(rig.data.bones),
        "deform_bones": len(names),
        "verts": len(body.data.vertices),
        "tris": hl.tri_count(body),
        "weights": weight_info,
        "arm_leg_weight_mix": mixed,
        "stance": {"head_yaw_deg": head_yaw, "leg_inward_deg": leg_inward,
                   "ground_correction_m": round(-min_z, 6)},
        "deformation": deform,
        "deform_gate": "error",
        "sockets": sorted(o.name for o in sockets),
        "seconds_total": round(time.time() - t0, 1),
    }
    return {"outputs": outputs, "stats": stats, "checks": checks, "warnings": warnings}
