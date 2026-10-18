"""attach_prop — put items into a rigged character's hands (contract §6) and check the result.

Request:
  inputs.character   rigged .blend / .glb / .fbx with mixamorig bones (Mixamo names, §5)
  inputs.item        .glb / .blend item with socket_grip_main (item_kit / item_from_mesh output)
  inputs.clips       optional list of clip files (.fbx / .glb, armature-only Mixamo clips are fine), each a
                     path or {"file", "name", "loop"}; the action is named after the file (or name) and
                     gets the contract §7 suffix '-loop' when loop is true (default for idle / walk / run)
  params.hand        r | l (default r)
  params.mode        bone_child (item stays a separate object under the hand's mount socket; swappable)
                     | skin_rigid (item joined into the skinned mesh, weight 1.0 on the hand bone)
  params.offset      {"loc": [x, y, z] m, "rot_deg": [x, y, z]} per-item fine tune in mount-socket space
                     (kept on the item node; a socket-matching swap does not reproduce it)
  params.refit_socket true: bake the offset into the hand socket instead - socket_mount_hand_<side> is
                     moved onto the item's socket_grip_main (rest pose, it stays a child of the hand bone)
                     and the item hangs under it with an identity offset, so a later swap by matching
                     socket_grip_main to the socket reproduces this grip (the move is written to
                     attach.json "socket_refit" and to the socket's extras). Weapons / props only; also
                     per attachment
  params.grip_dir    [x, y, z] or up / down / forward / back / out / in: +Z of a hand socket this step
                     has to create (the rig has none), see _items_lib.grip_dir_vector
  params.fit         auto (default: shields -> fitted mount, other items -> frame) | frame | rest | clips
  params.standoff    shields: "auto" (push the board off an open hand, <= 12 cm) or metres
  params.refit_shield_mount  true: refit socket_mount_shield_<side> even if the rig already has one
  params.replace     true: remove items a previous attach_prop run put in the same hand (bone_child)
  params.item_scale  number or "auto" (character height / 1.8 when it differs by more than 20 %)
  params.clips_check true (default): QA on frames of every action; params.frames_per_clip (12)
  params.attachments list of {"item", "hand", "mode", "offset", "fit", "kind", "item_scale", "standoff"}
                     to attach several items in one call (overrides inputs.item / params.hand ...)
  params.preview_clip  action used for the preview frames (default: first clip)
  inputs.extracted   props.json of extract_props (the server passes it when params.extracted is true): every
                     item it wrote is attached at the place it was cut from - the placement socket
                     socket_prop_<hand>_<n> that rig_humanoid kept on the hand bone. Weapons, staffs, bows and
                     banners: the hand socket is refit onto the item's socket_grip_main (refit_socket);
                     shields: socket_mount_shield_<side> is moved onto the shield's grip (fit "placement").
                     The placement sockets are removed afterwards (they coincide with the mounts).
  params.extracted_mode  bone_child (default) | skin_separate | skin_rigid for the extracted items.
                     skin_separate: the item stays its own mesh object, skinned 100 % to the hand bone (an
                     Armature modifier, one vertex group) - rigid like bone_child, and motion_style sees it as a
                     held prop (upright staff in the walk, no tip under the floor, the weapon lies down in the
                     death), which it does not for bone_child objects
  params.extracted_shield_fit  auto (default) | placement | clips | rest: where an extracted (or kit-replaced)
                     shield goes. placement: where the concept had it (the board then turns edge-on and cuts
                     into the arm when a clip swings the wrist); clips: the canonical shield mount fitted over
                     the frames of the character's clips (upright, facing out, clear of the body; replaces
                     rig_humanoid's rest-pose mount). auto: clips when the character already has clips (attach
                     after retarget), otherwise placement
  params.shield_bone hand (default) | forearm: the bone a skin_separate shield is skinned to (forearm: the board
                     follows the forearm like a strapped shield and does not spin with the wrist of a clip;
                     socket_mount_shield_<side> stays on the hand bone, contract §6)
  No items (extract_props found none): the character is passed through unchanged.
Outputs: equipped.glb (animations preserved), equipped.blend, attach.json, preview.png (rest + 3 frames),
grip_closeup.png.

Attachment = the item's socket_grip_main frame coincides with a mount socket of the hand (shared grip
frame: +Z out of the fist on the thumb side, -Y along the knuckles, X = Y x Z):
  * weapons and props -> socket_mount_hand_<side> (created from the hand bone and the hand vertices
    when the rig has none, by _items_lib.hand_socket_frame - the same function rig_humanoid and
    import_mixamo_rig use, so their sockets are reused as they are);
  * shields with fit != frame -> socket_mount_shield_<side>, a child of the hand bone whose frame is the
    hand socket times a fitted rotation + standoff. The fit is computed once per character and hand
    against a canonical shield (contract grip frame: +Z up, -Y face, grip on the back-centre handle;
    _items_lib.shield_proxy / fit_shield_mount), so every kit / item_from_mesh shield reuses the same
    mount and Godot can swap shields by matching socket_grip_main to socket_mount_shield_<side>. A rig
    that already has the socket (rig_humanoid fits it on the rest pose) keeps it untouched unless
    params.refit_shield_mount. The 4x4 is also written as extras.shield_mount_matrix on the hand socket.
bone_child items are parented to their mount socket (glTF: joint -> mount node -> item). The attached
copy's socket_grip_main is dropped when it coincides with the mount; its other sockets get the hand
suffix (socket_grip_off_r, socket_fx_tip_l) so names stay unique and dot-free (contract §6).
"""
import json
import math
import os

import bpy
import numpy as np
from mathutils import Matrix, Vector

import af_common as af
from steps import _items_lib as L


# --------------------------------------------------------------------------- loading

def load_character(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".blend":
        af.open_blend(path)
        objs = list(bpy.context.scene.objects)
    else:
        af.reset_scene()
        objs = af.import_any(path)
    arms = [a for a in af.armatures(objs) if L.find_bone(a, "mixamorig:RightHand") or L.find_bone(a, "mixamorig:LeftHand")]
    if not arms:
        raise ValueError("no armature with mixamorig hand bones in %s" % path)
    arm = arms[0]
    bodies = L.skinned_meshes(arm)
    if not bodies:
        raise ValueError("no skinned mesh bound to %s" % arm.name)
    return arm, bodies


def load_item(path, tag):
    objs = af.import_any(path)
    L.dedupe_materials()  # a second kit item re-imports mat_<style>_<role>: keep one material, not '.001'
    socks = {}
    for o in objs:
        nm = o.get("game_socket") or (o.name.split(".")[0] if o.name.startswith("socket_") else None)
        if nm:
            socks[str(nm)] = o
    roots = [o for o in objs if o.parent is None or o.parent not in objs]
    if len(roots) == 1:
        root = roots[0]
    else:
        root = bpy.data.objects.new("item_root_%s" % tag, None)
        bpy.context.scene.collection.objects.link(root)
        for o in roots:
            L.parent_keep(o, root)
        objs.append(root)
    meshes = af.meshes(objs)
    itype = None
    for o in meshes:
        if o.get("af_item_type"):
            itype = str(o["af_item_type"])
    return {"objs": objs, "root": root, "meshes": meshes, "sockets": socks, "type": itype}


# --------------------------------------------------------------------------- pose sampling

def set_pose(arm, action, frame):
    if action is None:
        arm.data.pose_position = "REST"
    else:
        arm.data.pose_position = "POSE"
        L.assign_action(arm, action)
        bpy.context.scene.frame_set(int(frame))
    bpy.context.view_layer.update()


Body = L.Body  # skinned meshes + hand/forearm/arm vertex masks (shared with rig_humanoid)


def sample_poses(arm, body, hand_socks, actions, frames_per_clip, preview_frames):
    plan = [("rest", None, None)]
    for act in actions:
        fr = L.sample_frames(act, frames_per_clip)
        for f in sorted(set(fr) | set(preview_frames.get(act.name, []))):
            plan.append((act.name, act, f))
    poses = []
    for (name, act, f) in plan:
        set_pose(arm, act, f)
        poses.append(L.pose_record(arm, body, hand_socks, name, f))
    set_pose(arm, None, None)
    return poses


# --------------------------------------------------------------------------- QA

def item_local_arrays(item, G):
    """Item mesh vertices / tris in grip-socket space (G = grip world 4x4, normalised)."""
    Gi = np.array(G.inverted())
    cos, tris, off = [], [], 0
    for m in item["meshes"]:
        co, tri, _ = L.mesh_arrays(m)
        co = co @ Gi[:3, :3].T + Gi[:3, 3]
        cos.append(co)
        tris.append(tri + off)
        off += len(co)
    return np.concatenate(cos), np.concatenate(tris)


# QA of one attachment in one pose and the canonical shield-mount fit live in _items_lib (one
# implementation, also used by rig_humanoid / import_mixamo_rig for their rest-pose shield mounts).
to_world = L.to_world
evaluate = L.evaluate_pose
fit_rotation = L.fit_rotation
fit_shield_mount = L.fit_shield_mount
create_mount = L.create_shield_mount
normalized4 = L.normalized4
CANON_UP, CANON_FACE = L.CANON_UP, L.CANON_FACE


# --------------------------------------------------------------------------- placement

def delete_tree(ob):
    for ch in list(ob.children):
        delete_tree(ch)
    bpy.data.objects.remove(ob, do_unlink=True)


def remove_previous(sides):
    """params.replace: delete bone_child items a previous attach_prop run put into these hands."""
    gone = []
    for o in list(bpy.context.scene.objects):
        try:
            if o.get("af_attached_hand") in sides and (o.parent is None or not o.parent.get("af_attached_hand")):
                gone.append(o.name)
                delete_tree(o)
        except ReferenceError:
            pass
    return gone


def place_on_mount(item, mount, M_root):
    item["root"].matrix_world = M_root
    bpy.context.view_layer.update()
    L.parent_keep(item["root"], mount)
    return mount.name


def alive(o):
    try:
        o.name
        return True
    except ReferenceError:
        return False


def skin_rigid(item, arm, body, side):
    """Join the item meshes into the skinned body with weight 1.0 on the hand bone."""
    bone = L.find_bone(arm, L.HAND_BONES[side][0]).name
    bpy.context.view_layer.update()
    sockets = [o for o in item["objs"] if alive(o) and o.type == "EMPTY" and o.get("game_socket")]
    socket_mw = {o: o.matrix_world.copy() for o in sockets}
    uv0 = body.data.uv_layers[0].name if body.data.uv_layers else None
    body_cols = [a.name for a in body.data.color_attributes]
    parts = []
    for m in item["meshes"]:
        mw = m.matrix_world.copy()
        m.parent = None
        m.data = m.data.copy()
        m.data.transform(mw)
        m.matrix_world = Matrix.Identity(4)
        m.modifiers.clear()
        if uv0 and m.data.uv_layers:
            m.data.uv_layers[0].name = uv0
        for a in list(m.data.color_attributes):
            if a.name not in body_cols:
                m.data.color_attributes.remove(a)
        for name in body_cols:
            if name not in m.data.color_attributes:
                ca = m.data.color_attributes.new(name, "FLOAT_COLOR", "POINT")
                ca.data.foreach_set("color", [1.0] * (4 * len(m.data.vertices)))
        for g in list(m.vertex_groups):
            m.vertex_groups.remove(g)
        vg = m.vertex_groups.new(name=bone)
        vg.add(list(range(len(m.data.vertices))), 1.0, "REPLACE")
        parts.append(m)
    for o in sockets:
        o.parent = None
        o.matrix_world = socket_mw[o]
    af.select_only(parts + [body])
    bpy.context.view_layer.objects.active = body
    bpy.ops.object.join()
    for o in sockets:
        L.parent_keep(o, arm, bone)
    root = item["root"]
    if alive(root) and root.type == "EMPTY" and not root.children:
        bpy.data.objects.remove(root)
    return bone, len(parts)


def finish_item_sockets(att):
    """Drop the attached copy's socket_grip_main when it coincides with its mount; give the others the
    hand suffix (socket_grip_off_r, socket_fx_tip_l): unique, dot-free names (contract §6)."""
    out = {}
    for o in list(att["objs"]):
        if not alive(o) or o.type != "EMPTY" or not o.get("game_socket"):
            continue
        base = str(o["game_socket"]).split(".")[0]
        if base == "socket_grip_main" and att["coincident"]:
            out[base] = None
            bpy.data.objects.remove(o, do_unlink=True)
            continue
        out[base] = L.rename_socket(o, "%s_%s" % (base, att["hand"]))
    return out


def skin_separate(item, arm, side, bone_name=None):
    """The item as its own mesh object skinned 100 % to the hand bone (or bone_name, e.g. Hips for a weapon at
    the belt): parent = armature (object), an Armature modifier, one vertex group. Its sockets (grip_off, fx_tip)
    become children of that bone. Rigid like bone_child; motion_style finds a hand item among the skinned meshes
    as a held prop."""
    bone = bone_name or L.find_bone(arm, L.HAND_BONES[side][0]).name
    bpy.context.view_layer.update()
    sockets = [o for o in item["objs"] if alive(o) and o.type == "EMPTY" and o.get("game_socket")]
    socket_mw = {o: o.matrix_world.copy() for o in sockets}
    for o in sockets:
        o.parent = None
        o.matrix_world = socket_mw[o]
    meshes = []
    for m in item["meshes"]:
        mw = m.matrix_world.copy()
        m.parent = None
        if m.data.users > 1:
            m.data = m.data.copy()
        m.data.transform(mw)
        m.matrix_world = Matrix.Identity(4)
        for g in list(m.vertex_groups):
            m.vertex_groups.remove(g)
        vg = m.vertex_groups.new(name=bone)
        vg.add(list(range(len(m.data.vertices))), 1.0, "REPLACE")
        m.parent = arm
        m.parent_type = "OBJECT"
        m.matrix_parent_inverse = arm.matrix_world.inverted()
        mod = m.modifiers.new("Armature", "ARMATURE")
        mod.object = arm
        mod.use_vertex_groups = True
        m["af_prop_item"] = (item.get("extracted") or {}).get("id") or m.name
        m["af_attached_hand"] = side if side in ("r", "l") else "hip"
        meshes.append(m)
    for o in sockets:
        L.parent_keep(o, arm, bone)
    root = item["root"]
    if alive(root) and root.type == "EMPTY" and not [c for c in root.children if alive(c)]:
        bpy.data.objects.remove(root)
    bpy.context.view_layer.update()
    return bone, len(meshes)


def extracted_attachments(path, mode, shield_fit="placement"):
    """attach_prop attachments from extract_props' props.json: one per written item, at its placement
    (shields: shield_fit - placement, or clips / rest for the canonical fitted mount)."""
    with open(path, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    out = []
    for rec in doc.get("items", []):
        f = (rec.get("files") or {}).get("glb")
        if not f or not os.path.exists(f):
            continue
        shield = rec.get("kind") == "shield"
        kind = rec.get("type")
        if shield:
            kind = ((rec.get("kit") or {}).get("params") or {}).get("type") or "shield_kite"
        out.append({"item": f, "hand": rec.get("hand", "r"), "mode": mode, "kind": kind,
                    "fit": shield_fit if shield else "placement", "placement": rec.get("placement_socket"),
                    "refit_socket": not shield,
                    "extracted": {"id": rec.get("id"), "type": rec.get("type"), "source": rec.get("source"),
                                  "kind": rec.get("kind")}})
    return out, doc


def extracted_clip_weight(name):
    """Weight of a clip's frames in the shield fit of an extracted shield: loops x2, a death (on the floor,
    no upright shield possible) dropped."""
    n = str(name).lower()
    if "death" in n or "die" in n:
        return 0.0
    return 2.0 if n.endswith("-loop") else 1.0


def is_identity(M, eps=1e-6):
    return all(abs(M[i][j] - (1.0 if i == j else 0.0)) < eps for i in range(4) for j in range(4))


# --------------------------------------------------------------------------- step

def run(req):
    inp, prm = req["inputs"], req["params"]
    out_dir = req["out_dir"]
    arm, bodies = load_character(inp["character"])
    body_main = max(bodies, key=lambda b: len(b.data.vertices))
    af.log("character loaded", armature=arm.name, bodies=[b.name for b in bodies])
    # height in the REST pose: a .glb / .blend character arrives in a clip pose (a crouch would shrink
    # the QA thresholds and item_scale "auto")
    arm.data.pose_position = "REST"
    bpy.context.view_layer.update()
    zs = np.concatenate([L.mesh_arrays(b)[0][:, 2] for b in bodies])
    height = float(zs.max() - zs.min())
    clips = []
    for entry in inp.get("clips", []) or []:
        path, name = L.clip_spec(entry)
        clips.append(L.import_clip(path, name))
    actions = [a for a in bpy.data.actions if any(fc.data_path.startswith("pose.bones")
                                                  for fc in L.fcurves_of(a))]
    af.log("clips", actions=[a.name for a in actions])
    extracted_doc = None
    shield_fit = None
    if inp.get("extracted"):
        # a shield kept where the concept had it turns edge-on and cuts into the arm once a clip swings the
        # wrist; on a character that already has its clips (attach after retarget) the canonical mount is
        # fitted over the clip frames instead (upright, facing out, clear of the body)
        shield_fit = str(prm.get("extracted_shield_fit", "auto")).lower()
        if shield_fit == "auto":
            shield_fit = "clips" if actions else "placement"
        if shield_fit not in ("placement", "clips", "rest"):
            raise ValueError("extracted_shield_fit must be auto, placement, clips or rest")
        atts_in, extracted_doc = extracted_attachments(inp["extracted"], prm.get("extracted_mode", "bone_child"),
                                                       shield_fit)
    else:
        atts_in = prm.get("attachments") or inp.get("attachments") or [
            {"item": inp.get("item"), "hand": prm.get("hand", "r"), "mode": prm.get("mode", "bone_child"),
             "offset": prm.get("offset"), "fit": prm.get("fit", "auto"), "kind": prm.get("kind"),
             "item_scale": prm.get("item_scale", 1.0)}]
    # items cut from the belt (a mace at the hip): rigid to the Hips at their placement, outside the hand logic
    belts = [a for a in atts_in if a.get("hand") == "hip"]
    atts_in = [a for a in atts_in if a.get("hand") != "hip"]
    belt_report = []
    for b_ in belts:
        ps = L.find_sockets().get(b_.get("placement") or "")
        if ps is None:
            warnings_pre = "belt item %s: placement socket %r missing - skipped" % (os.path.basename(b_["item"]), b_.get("placement"))
            belt_report.append({"item": b_["item"], "skipped": warnings_pre})
            continue
        arm.data.pose_position = "REST"
        bpy.context.view_layer.update()
        it_b = load_item(b_["item"], "belt")
        g_b = it_b["sockets"].get("socket_grip_main")
        Gb = normalized4(g_b.matrix_world.copy()) if g_b is not None else Matrix.Identity(4)
        it_b["root"].matrix_world = normalized4(ps.matrix_world.copy()) @ Gb.inverted() @ it_b["root"].matrix_world
        bpy.context.view_layer.update()
        mode_b = b_.get("mode", "bone_child")
        hips = L.find_bone(arm, "mixamorig:Hips")
        if mode_b in ("skin_separate", "skin_rigid") and hips is not None:
            it_b["extracted"] = b_.get("extracted")
            skin_separate(it_b, arm, "hip", bone_name=hips.name)
            if g_b is not None and alive(g_b):
                bpy.data.objects.remove(g_b, do_unlink=True)
            bpy.data.objects.remove(ps, do_unlink=True)
        else:
            L.parent_keep(it_b["root"], ps)
            it_b["root"]["af_attached_hand"] = "hip"
        belt_report.append({"item": b_["item"], "bone": "mixamorig:Hips", "mode": mode_b, "placement_socket": b_.get("placement"),
                            "extracted": b_.get("extracted")})
    if not atts_in and belts:
        arm.data.pose_position = "POSE"
        glb = af.export_glb(af.out_path(req, "equipped.glb"), objects=None, animations=bool(actions), skins=True)
        outputs = {"glb": glb, "blend": af.save_blend(af.out_path(req, "equipped.blend"))}
        with open(af.out_path(req, "attach.json"), "w", encoding="utf-8") as fh:
            json.dump(af._jsonable({"character": inp["character"], "attachments": [], "belt": belt_report}), fh,
                      ensure_ascii=False, indent=2)
        outputs["json"] = af.out_path(req, "attach.json")
        return {"outputs": outputs, "stats": {"attachments": [], "belt": belt_report, "height_m": round(height, 3)},
                "checks": [], "warnings": []}
    if not atts_in:
        # nothing was extracted: the character goes on unchanged
        arm.data.pose_position = "POSE"
        if actions:
            L.assign_action(arm, actions[0])
        glb = af.export_glb(af.out_path(req, "equipped.glb"), objects=None, animations=bool(actions), skins=True)
        outputs = {"glb": glb, "blend": af.save_blend(af.out_path(req, "equipped.blend"))}
        doc0 = {"character": inp["character"], "attachments": [], "note": "no items to attach",
                "extracted": inp.get("extracted")}
        with open(af.out_path(req, "attach.json"), "w", encoding="utf-8") as fh:
            json.dump(af._jsonable(doc0), fh, ensure_ascii=False, indent=2)
        outputs["json"] = af.out_path(req, "attach.json")
        return {"outputs": outputs, "stats": {"attachments": [], "height_m": round(height, 3)},
                "checks": [af.check("attachments", True, 0, None, severity="warning", note="no items")],
                "warnings": ["no items to attach (extract_props found none)"] if inp.get("extracted") else []}
    warnings = []
    checks = []
    for a in atts_in:
        if a.get("hand", "r") not in ("r", "l"):
            raise ValueError("hand must be 'r' or 'l', got %r" % a.get("hand"))
    replaced = []
    if prm.get("replace"):
        replaced = remove_previous({a.get("hand", "r") for a in atts_in})
    # hand sockets
    hand_socks, hand_info = {}, {}
    arm.data.pose_position = "REST"
    bpy.context.view_layer.update()
    for a in atts_in:
        side = a.get("hand", "r")
        if side not in hand_socks:
            gd = a.get("grip_dir", prm.get("grip_dir"))
            emp, info = L.ensure_hand_socket(arm, body_main, side, thumb_hint=prm.get("thumb_hint"),
                                             along=prm.get("socket_along", 0.42),
                                             grip_dir=None if gd is None else L.grip_dir_vector(gd, side))
            hand_socks[side] = emp
            hand_info[side] = info
            if info.get("baked_prop"):
                warnings.append("hand %s already holds an object baked into the mesh (%d vertices, %.2f m); the "
                                "socket follows its axis and the new item will overlap it" %
                                (side, info["baked_prop"]["vertices"], info["baked_prop"]["extent_m"]))
            checks.append(af.check("hand_socket_%s" % side, emp is not None, emp.name))
    # items
    atts = []
    for i, a in enumerate(atts_in):
        if not a.get("item") or not os.path.exists(a["item"]):
            raise ValueError("item file missing: %r" % a.get("item"))
        it = load_item(a["item"], str(i))
        grip = it["sockets"].get("socket_grip_main")
        checks.append(af.check("att%d_socket_grip_main" % i, grip is not None, sorted(it["sockets"])))
        if grip is None:
            raise ValueError("item %s has no socket_grip_main" % a["item"])
        sc = a.get("item_scale", prm.get("item_scale", 1.0))
        if sc == "auto":
            sc = height / 1.8 if abs(height / 1.8 - 1) > 0.2 else 1.0
        sc = float(sc)
        if sc != 1.0:
            it["root"].matrix_world = Matrix.Scale(sc, 4) @ it["root"].matrix_world
        bpy.context.view_layer.update()
        Gn = normalized4(grip.matrix_world.copy())
        kind = a.get("kind") or it["type"] or ""
        is_shield = kind.startswith("shield") or kind == "buckler"
        co_g, tri_g = item_local_arrays(it, Gn)
        att = dict(it, idx=i, hand=a.get("hand", "r"), mode=a.get("mode", prm.get("mode", "bone_child")),
                   refit_socket=bool(a.get("refit_socket", prm.get("refit_socket", False))),
                   offset=a.get("offset"), fit=a.get("fit", prm.get("fit", "auto")), kind=kind, scale=sc,
                   is_shield=is_shield, G=Gn, root0=it["root"].matrix_world.copy(), co_g=co_g, tri_g=tri_g,
                   up_g=CANON_UP, face_g=CANON_FACE, path=a["item"],
                   standoff=a.get("standoff", prm.get("standoff", "auto")))
        att["placement"] = a.get("placement")
        att["extracted"] = a.get("extracted")
        if att["mode"] not in ("bone_child", "skin_rigid", "skin_separate"):
            raise ValueError("mode must be bone_child, skin_rigid or skin_separate")
        atts.append(att)
    merged = L.dedupe_materials()
    # poses
    body = Body(bodies)
    fpc = int(prm.get("frames_per_clip", 12)) if prm.get("clips_check", True) else 0
    if actions and any(a["is_shield"] and a["fit"] == "clips" for a in atts):
        fpc = max(fpc, 8)   # the clip fit needs frames of every clip, also without clips_check
    prev_act = None
    if actions:
        pc = prm.get("preview_clip")
        prev_act = next((a for a in actions if pc and a.name in (pc, pc + "-loop")), None) or \
            (clips[0] if clips else actions[0])
    preview_frames = {}
    if prev_act is not None:
        a0, a1 = prev_act.frame_range
        preview_frames[prev_act.name] = [int(round(a0 + (a1 - a0) * t)) for t in (0.25, 0.5, 0.75)]
    use_actions = actions if fpc else ([prev_act] if prev_act else [])
    poses = sample_poses(arm, body, hand_socks, use_actions, max(fpc, 1) if fpc else 1, preview_frames)
    af.log("poses sampled", poses=len(poses))
    s_char = height / 1.8
    report = {"character": inp["character"], "height_m": round(height, 4), "armature": arm.name,
              "bodies": [b.name for b in bodies], "actions": [a.name for a in actions],
              "replaced_items": replaced, "merged_materials": merged, "belt": belt_report,
              "hand_sockets": {s: dict(L.socket_record(e), **{"info": hand_info[s]}) for s, e in hand_socks.items()},
              "mounts": {}, "attachments": []}
    # extracted items: where they were cut from (the placement socket rig_humanoid kept on the hand bone)
    socks_all = L.find_sockets()
    arm.data.pose_position = "REST"
    bpy.context.view_layer.update()
    placed_names = set()
    for att in atts:
        if att.get("fit") != "placement":
            if att.get("placement") and att.get("placement") in socks_all:
                placed_names.add(att["placement"])   # extracted shield on the fitted mount: socket unused
            continue
        ps = socks_all.get(att.get("placement") or "")
        if ps is None:
            warnings.append("placement socket %r not in the character: %s attached on the plain hand frame" % (
                att.get("placement"), os.path.basename(att["path"])))
            att["fit"] = "auto" if att["is_shield"] else "frame"
            continue
        att["P_rest"] = normalized4(ps.matrix_world.copy())
        placed_names.add(ps.name)
        if not att["is_shield"]:
            att["offset_matrix"] = poses[0]["H"][att["hand"]].inverted() @ att["P_rest"]
    # shield mounts: once per hand, canonical shield, reused when the rig already has one
    mounts = {}
    for att in atts:
        if att["is_shield"] and att.get("fit") == "placement" and att["hand"] not in mounts:
            side = att["hand"]
            H_rest = poses[0]["H"][side]
            name = L.SHIELD_MOUNT[side]
            existing = L.find_sockets().get(name)
            if existing is None:
                hb = L.find_bone(arm, L.HAND_BONES[side][0])
                existing = af.add_socket(name, parent=arm, bone=hb.name, kind="mount", size=0.06, fitted_for="shield")
                existing.rotation_mode = "QUATERNION"
                status = "created_at_placement"
            else:
                status = "moved_to_placement"
            bpy.context.view_layer.update()
            old = normalized4(existing.matrix_world.copy())
            existing.matrix_world = att["P_rest"]
            bpy.context.view_layer.update()
            O_mount = H_rest.inverted() @ att["P_rest"]
            existing["socket_space_matrix"] = L.flat16(O_mount)
            existing["placed_by_item"] = os.path.basename(att["path"])
            hand_socks[side]["shield_mount_matrix"] = L.flat16(O_mount)
            hand_socks[side]["shield_mount_socket"] = existing.name
            info = {"mode": "placement", "socket": existing.name,
                    "moved_m": round((old.translation - att["P_rest"].translation).length, 4)}
            mounts[side] = {"emp": existing, "O": O_mount, "info": info, "status": status}
    for att in atts:
        fit = att["fit"]
        if fit == "placement":
            att["fit_used"] = "placement"
            continue
        if fit == "auto":
            fit = ("clips" if len(poses) > 1 else "rest") if att["is_shield"] else "frame"
        att["fit_used"] = fit
        side = att["hand"]
        if not (att["is_shield"] and fit in ("rest", "clips")) or side in mounts:
            continue
        H_rest = poses[0]["H"][side]
        name = L.SHIELD_MOUNT[side]
        existing = L.find_sockets().get(name)
        # an extracted shield fitted over the clips replaces rig_humanoid's rest-pose mount
        refit = prm.get("refit_shield_mount") or (att.get("extracted") and fit == "clips")
        if existing is not None and refit:
            bpy.data.objects.remove(existing, do_unlink=True)
            existing = None
        if existing is not None:
            arm.data.pose_position = "REST"
            bpy.context.view_layer.update()
            O_mount = H_rest.inverted() @ normalized4(existing.matrix_world.copy())
            info = {"mode": "existing_mount", "socket": existing.name}
            emp, status = existing, "existing"
        else:
            st = att["standoff"]
            kw = {}
            if att.get("extracted") and fit == "clips":
                # sprites show the clips, never the concept's rest pose; idle / walk loops are on screen most
                kw = {"w_rest": 0.05, "clip_weight": extracted_clip_weight,
                      "front_bias": float(prm.get("shield_front_bias", 1.0))}
            O_mount, info = fit_shield_mount(side, poses, fit, s_char, body.masks,
                                             st if isinstance(st, (int, float)) else "auto", **kw)
            emp, status = create_mount(arm, side, H_rest, O_mount, hand_socks[side], info), "created"
        mounts[side] = {"emp": emp, "O": O_mount, "info": info, "status": status}
        af.log("shield mount", socket=emp.name, status=status)
    # QA per attachment
    for att in atts:
        O_user = att["offset_matrix"] if att.get("offset_matrix") is not None else L.offset_matrix(att["offset"])
        side = att["hand"]
        if att["is_shield"] and att["fit_used"] in ("rest", "clips", "placement"):
            mt = mounts[side]
            O = mt["O"] @ O_user
            att["mount"] = mt["emp"]
            fit_info = dict(mt["info"])
        else:
            O = O_user
            att["mount"] = hand_socks[side]
            fit_info = {"mode": "frame"}
        att["O"] = O
        att["O_user"] = O_user
        att["coincident"] = is_identity(O_user)
        per_pose = [evaluate(att, p, O, s_char, body.masks) for p in poses]
        rest = per_pose[0]
        anim = per_pose[1:]
        tag = "att%d_%s_%s" % (att["idx"], att["kind"] or "item", att["hand"])
        lim_grip = 0.04 * s_char
        checks.append(af.check(tag + ":grip_to_hand_rest_m", rest.get("grip_to_hand_m", 1) <= lim_grip,
                               rest.get("grip_to_hand_m"), round(lim_grip, 3), severity="warning"))
        checks.append(af.check(tag + ":grip_enclosure_rest", rest.get("grip_enclosure", 0) >= 0.5,
                               rest.get("grip_enclosure"), 0.5, severity="warning",
                               note="fraction of radial rays around the grip that hit the hand within 6 cm; "
                                    "open (unclosed) hands score below 0.5"))
        checks.append(af.check(tag + ":axis_inside_arm_rest", rest["axis_inside_arm"] == 0, rest["axis_inside_arm"], 0,
                               note="blade/shaft samples inside the forearm/upper arm at rest"))
        checks.append(af.check(tag + ":penetration_rest", rest["overlap_arm_tris"] + rest["overlap_body_tris"] == 0,
                               {"arm": rest["overlap_arm_tris"], "body": rest["overlap_body_tris"]}, 0, severity="warning"))
        if att["is_shield"]:
            checks.append(af.check(tag + ":hand_through_board_rest", rest["overlap_hand_tris"] == 0,
                                   rest["overlap_hand_tris"], 0, severity="warning",
                                   note="hand triangles crossing this shield at the shared mount (thicker board or "
                                        "handle than the canonical shield)"))
        if anim:
            pen_frames = sum(1 for r in anim if r["overlap_arm_tris"] + r["overlap_body_tris"] > 0)
            in_arm = sum(1 for r in anim if r["axis_inside_arm"] > 0)
            checks.append(af.check(tag + ":penetration_frames", pen_frames == 0, pen_frames, 0, severity="warning",
                                   note="sampled clip frames with item-arm/body triangle overlap (BVH)"))
            checks.append(af.check(tag + ":axis_inside_arm_frames", in_arm == 0, in_arm, 0, severity="warning"))
        if att["is_shield"]:
            checks.append(af.check(tag + ":shield_upright_rest", rest["shield_up_dot"] >= 0.7, rest["shield_up_dot"], 0.7,
                                   severity="warning"))
            checks.append(af.check(tag + ":shield_outward_rest", rest["shield_face_out_dot"] >= 0.3,
                                   rest["shield_face_out_dot"], 0.3, severity="warning"))
            if anim:
                good = sum(1 for r in anim if r["shield_up_dot"] >= 0.7 and r["shield_face_out_dot"] >= 0.0)
                checks.append(af.check(tag + ":shield_upright_frames_ratio", good / len(anim) >= 0.6,
                                       round(good / len(anim), 3), 0.6, severity="warning"))
        att["qa"] = per_pose
        att["fit_info"] = fit_info
    # refit_socket: the hand socket moves onto the item's grip (offset baked into the socket), so a
    # swap by socket matching reproduces the grip; other items of that hand are re-expressed
    arm.data.pose_position = "REST"
    bpy.context.view_layer.update()
    report["socket_refit"] = {}
    for att in atts:
        side = att["hand"]
        if not att["refit_socket"] or side in report["socket_refit"]:
            continue
        if att["is_shield"] and att["fit_used"] in ("rest", "clips", "placement"):
            warnings.append("refit_socket ignored for the shield in hand %s (it hangs on the shield mount)" % side)
            continue
        D = att["O_user"].copy()
        sock = hand_socks[side]
        old = normalized4(sock.matrix_world.copy())
        new = poses[0]["H"][side] @ D
        sock.matrix_world = new
        bpy.context.view_layer.update()
        poses[0]["H"][side] = normalized4(sock.matrix_world.copy())   # placement below uses the new socket
        sock["refit_offset_matrix"] = L.matrix_rows(D)
        sock["refit_by_item"] = os.path.basename(att["path"])
        Dinv = D.inverted()
        for other in atts:
            if other["hand"] == side and other.get("mount") is sock:
                other["O"] = Dinv @ other["O"]
                other["O_user"] = Dinv @ other["O_user"]
                other["coincident"] = is_identity(other["O_user"])
        report["socket_refit"][side] = {
            "socket": sock.name, "item": os.path.basename(att["path"]),
            "delta_pos_m": round(D.translation.length, 6),
            "delta_angle_deg": round(math.degrees(D.to_quaternion().angle), 4),
            "from": L.matrix_rows(old), "to": L.matrix_rows(normalized4(sock.matrix_world.copy()))}
        af.log("hand socket refit", socket=sock.name, **{k: v for k, v in report["socket_refit"][side].items()
                                                        if k.startswith("delta")})
    # placement (rest pose): bone_child items hang under their mount socket
    arm.data.pose_position = "REST"
    bpy.context.view_layer.update()
    for att in atts:
        H = poses[0]["H"][att["hand"]]
        M_root = H @ att["O"] @ att["G"].inverted() @ att["root0"]
        att["bone"] = L.find_bone(arm, L.HAND_BONES[att["hand"]][0]).name
        att["mount_name"] = place_on_mount(att, att["mount"], M_root)
        root = att["root"]
        root["af_attached_hand"] = att["hand"]
        root["af_attached_mount"] = att["mount_name"]
        root["af_attached_item"] = os.path.basename(att["path"])
        base = root.name.split(".")[0]
        root.name = L.unique_socket_name("%s_%s" % (base, att["hand"]))
    bpy.context.view_layer.update()
    # self-check: the attached socket_grip_main sits exactly on its mount (what a socket-matching swap does)
    for att in atts:
        g = att["sockets"]["socket_grip_main"]
        Mg = normalized4(g.matrix_world.copy())
        Mm = normalized4(att["mount"].matrix_world.copy()) @ att["O_user"]
        dpos = (Mg.translation - Mm.translation).length
        dang = math.degrees((Mg.to_3x3().inverted() @ Mm.to_3x3()).to_quaternion().angle)
        att["mount_error"] = {"pos_m": round(dpos, 6), "angle_deg": round(dang, 4)}
        checks.append(af.check("att%d:grip_on_mount" % att["idx"], dpos < 1e-4 and dang < 0.05, att["mount_error"],
                               {"pos_m": 1e-4, "angle_deg": 0.05},
                               note="socket_grip_main of the attached item vs %s (x offset)" % att["mount_name"]))
    # previews
    item_meshes = [m for att in atts for m in att["meshes"]]
    view_objs = bodies + item_meshes
    rows, labels = [], []
    tmp = os.path.join(out_dir, "_preview")
    os.makedirs(tmp, exist_ok=True)
    plan = [("rest", None, None)] + [(prev_act.name, prev_act, f) for f in preview_frames.get(prev_act.name, [])] \
        if prev_act else [("rest", None, None)]
    for k, (name, act, f) in enumerate(plan):
        set_pose(arm, act, f)
        p = af.render_views(os.path.join(tmp, "pose_%d.png" % k), view_objs, views=("front", "left", "three_quarter"),
                            size=int(prm.get("size", 320)))
        rows.append(p)
        labels.append("REST POSE" if act is None else "%s FRAME %d" % (name, f))
    set_pose(arm, None, None)
    outputs = {"preview": L.stack_rows(rows, af.out_path(req, "preview.png"), labels=labels)}
    # grip close-ups at rest: one row per hand
    rr = 0.14 * s_char
    crow, clab = [], []
    for s in sorted(hand_socks):
        c0 = poses[0]["H"][s].translation
        p = L.render_region(os.path.join(tmp, "grip_%s.png" % s), [c0[i] - rr for i in range(3)],
                            [c0[i] + rr for i in range(3)], views=("front", "right" if s == "l" else "left", "three_quarter"),
                            size=300)
        crow.append(p)
        clab.append("%s HAND (REST)" % ("RIGHT" if s == "r" else "LEFT"))
    outputs["grip_closeup"] = L.stack_rows(crow, af.out_path(req, "grip_closeup.png"), labels=clab)
    # skin_rigid joins after the QA / previews (identical motion: weight 1.0 on the hand bone)
    for att in atts:
        if att["mode"] == "skin_rigid":
            bone, n = skin_rigid(att, arm, body_main, att["hand"])
            att["joined_meshes"] = n
        elif att["mode"] == "skin_separate":
            # a shield is strapped to the forearm: params.shield_bone "forearm" skins it there (the wrist then turns
            # the hand, not the board); "hand" keeps the contract's hand bone
            sb = None
            if att["is_shield"] and str(prm.get("shield_bone", "hand")).lower() == "forearm":
                fb = L.find_bone(arm, L.HAND_BONES[att["hand"]][1])
                sb = fb.name if fb is not None else None
            bone, n = skin_separate(att, arm, att["hand"], bone_name=sb)
            att["skinned_meshes"] = n
            att["skinned_bone"] = bone
    # the placement sockets coincide with the mounts now: removed (they would only clutter the Godot scene)
    for nm in placed_names:
        o = L.find_sockets().get(nm)
        if o is not None:
            bpy.data.objects.remove(o, do_unlink=True)
    for att in atts:
        att["socket_names"] = finish_item_sockets(att)
    if actions:
        L.assign_action(arm, prev_act or actions[0])
    arm.data.pose_position = "POSE"
    bpy.context.scene.frame_set(int((prev_act or actions[0]).frame_range[0]) if actions else 1)
    glb = af.export_glb(af.out_path(req, "equipped.glb"), objects=None, animations=bool(actions), skins=True)
    summ = L.glb_summary(glb)
    outputs["glb"] = glb
    outputs["blend"] = af.save_blend(af.out_path(req, "equipped.blend"))
    checks.append(af.check("animations_exported", len(summ["animations"]) >= len(actions), summ["animations"],
                           [a.name for a in actions]))
    need = {"socket_mount_hand_%s" % s for s in hand_socks} | {m["emp"].name for m in mounts.values()}
    checks.append(af.check("mount_sockets_in_glb", need <= set(summ["sockets"]), sorted(summ["sockets"]), sorted(need)))
    bad = sorted(n for n in summ["sockets"] if "." in n or not n.isascii())
    checks.append(af.check("socket_names_clean", not bad, bad, "ASCII, no dots (contract §6)"))
    for side, mt in mounts.items():
        report["mounts"][mt["emp"].name] = dict(L.socket_record(mt["emp"]), status=mt["status"], fit_info=mt["info"],
                                                base_socket=hand_socks[side].name,
                                                socket_space_matrix=L.matrix_rows(mt["O"]))
    for att in atts:
        rest = att["qa"][0]
        anim = att["qa"][1:]
        entry = {"item": att["path"], "kind": att["kind"], "hand": att["hand"], "mode": att["mode"],
                 "bone": att["bone"], "mount": att["mount_name"], "item_scale": att["scale"], "offset": att["offset"],
                 "fit": att["fit_used"], "fit_info": att["fit_info"],
                 "socket_space_matrix": L.matrix_rows(att["O"]),
                 "mount_space_matrix": L.matrix_rows(att["O_user"]),
                 "swap_by_socket_matching": att["coincident"], "grip_on_mount_error": att["mount_error"],
                 "sockets_renamed": att["socket_names"],
                 "rest": rest,
                 "frames": {"count": len(anim),
                            "max_overlap_arm_tris": max([r["overlap_arm_tris"] for r in anim] or [0]),
                            "max_overlap_body_tris": max([r["overlap_body_tris"] for r in anim] or [0]),
                            "frames_with_penetration": sum(1 for r in anim if r["overlap_arm_tris"] + r["overlap_body_tris"]),
                            "frames_axis_inside_arm": sum(1 for r in anim if r["axis_inside_arm"])},
                 "per_pose": att["qa"]}
        if att["is_shield"] and anim:
            entry["frames"]["shield_up_dot_min"] = min(r["shield_up_dot"] for r in anim)
            entry["frames"]["shield_up_dot_mean"] = round(sum(r["shield_up_dot"] for r in anim) / len(anim), 3)
            entry["frames"]["shield_face_out_dot_mean"] = round(sum(r["shield_face_out_dot"] for r in anim) / len(anim), 3)
            entry["frames"]["upright_outward_ratio"] = round(
                sum(1 for r in anim if r["shield_up_dot"] >= 0.7 and r["shield_face_out_dot"] >= 0.0) / len(anim), 3)
        if att.get("joined_meshes"):
            entry["joined_meshes"] = att["joined_meshes"]
        if att.get("skinned_meshes"):
            entry["skinned_meshes"] = att["skinned_meshes"]
            entry["skinned_bone"] = att.get("skinned_bone")
        if att.get("extracted"):
            entry["extracted"] = att["extracted"]
            entry["placement_socket"] = att.get("placement")
        report["attachments"].append(entry)
    report["glb"] = summ
    with open(af.out_path(req, "attach.json"), "w", encoding="utf-8") as fh:
        json.dump(af._jsonable(report), fh, ensure_ascii=False, indent=2)
    outputs["json"] = af.out_path(req, "attach.json")
    stats = {"height_m": round(height, 3), "actions": [a.name for a in actions], "poses_checked": len(poses),
             "belt": belt_report,
             "mounts": {k: v["status"] for k, v in report["mounts"].items()},
             "attachments": [{"kind": e["kind"], "hand": e["hand"], "mode": e["mode"], "fit": e["fit"],
                              "mount": e["mount"],
                              "rest_grip_to_hand_m": e["rest"].get("grip_to_hand_m"),
                              "rest_grip_enclosure": e["rest"].get("grip_enclosure"),
                              "rest_overlap_arm_body": e["rest"]["overlap_arm_tris"] + e["rest"]["overlap_body_tris"],
                              "frames_with_penetration": e["frames"]["frames_with_penetration"]}
                             for e in report["attachments"]],
             "glb_animations": summ["animations"]}
    return {"outputs": outputs, "stats": stats, "checks": checks, "warnings": warnings}
