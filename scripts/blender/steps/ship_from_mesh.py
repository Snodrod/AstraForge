"""ship_from_mesh — a generated ship mesh (Hunyuan / Meshy / TripoSR ...) -> ship asset (AstraForge ship contract).

What it does (honestly: heuristics on an unstructured AI mesh, not a rebuild):
  1. imports inputs.mesh, drops empties/cameras/collision meshes, joins all meshes into one and WELDS it
     (generated GLBs are split at every UV / normal seam; collapse decimation of an unwelded mesh tears it);
  2. orients it: up = input +Z (glTF +Y) unless params.up; length axis = 2D PCA in the plane normal to up;
     nose sign = params.nose or a vote of three cues (narrower end, blunt aft-facing area, bright emissive
     pixels of the base colour texture) with a confidence value;
  3. scales the length to length_m, centres the bbox on the origin (root = geometric centre);
  4. decimates the single welded mesh to the class budget (collapse), assigns AF_* material labels on that
     topology, and only then splits it by material; LOD1 / LOD2 are decimated copies of the same labelled
     single mesh (no open borders between material pieces), checked by top/side silhouette IoU vs LOD0;
  5. materials: 'classify' (per-face base-colour sample -> HSV rules -> flat material factors with cluster mean
     colours), 'keep_texture' (one AF_Armor with the source albedo downscaled to the class budget + optional
     emissive split) or 'single';
  6. sockets, each with extras.confidence: thrusters = blobs of area-sampled aft-facing surface at the tail
     (a blob is split only when its density along the long axis has two peaks); on an X-symmetric mesh the
     detections are mirrored and merged (asymmetric results get a lower confidence); turrets = area-sampled
     dorsal (then ventral) flats, paired across X on symmetric meshes; nav lights at the X extremes;
     antenna at the top; docking at the belly;
  7. COL_SIMPLE (<= 64 verts), export GLB + LODs + .blend (orphans purged) + manifest + preview with
     socket markers + a same-camera LOD strip.

Request params:
  class          role (fighter, corvette, frigate ...) or budget class ship_s | ship_m | ship_l (default ship_m);
                 a length_m outside the class range moves the budget class (warning)
  length_m       default per role / class
  nose           auto | +x | -x | +y | -y (input axes after import, Blender frame); up: auto | +z | -z | +x ...
  materials      classify | keep_texture | single (default classify); emissive: bool (default true)
  turret_count   default per class; sockets: bool (default true)
  target_tris    LOD0 triangle target (default 90 % of the class budget, max 97 %)
  source         manifest source enum; default guessed from the input path (hunyuan/trellis/triposr/
                 instantmesh in the path), otherwise 'external'
  generator      free text; default guessed from the path (meshy, tripo, hunyuan3d ...) otherwise 'unknown'
  asset_id       default ship_<s|m|l>_<file stem>_<sha1 of the file, 6 hex>
  thruster_frame thrust | exhaust (see _ship_lib); faction (default neutral), tier, uv
                 (keep | smart | box), lods, preview, preview_size, preview_sockets, preview_lods
  palette        input (default: colours sampled from the input / concept, glow and status lights included)
                 | faction (the faction palette replaces them); livery {key: colour | {rgb, metal, rough,
                 strength}} is applied last
  paint          {image (concept with alpha, e.g. prepare_concept's reference PNG), mask?, camera: 'fit' |
                 {location, rotation_matrix, lens_mm, resolution} | json path, mirror: x | none, regions:
                 [{box: [u0, v0, u1, v1] (0..1 of the image), label, only?}], thresholds, markings: armor | keep}
                 -> materials = 'paint': per-face material labels projected from the concept (see paint_mesh)
  armor_texture  kit texture name or {texture, tile_m, normal_strength, regions: [{texture, name, box: {min, max}}]}
                 (scripts/blender/data/kits/kits.json; no kit ships by default - add your own tileable
                 albedo/normal pair there) on AF_Armor with metric box UVs
  decals         [{text | lines, font, side: port | starboard | both | top, position: [y, z] | [x, y], size: [w, h],
                 bold, align, color, wear, line_scales, slashed_zero}] -> one RGBA atlas, AF_Marking (BLEND)
  turret_sockets flats (default: flat dorsal / ventral patches) | protrusions (barrel-like protrusions of the
                 input: sockets at their base, axis outward) | auto (protrusions if >= 2, else flats)
  lod_targets    {lod1: tris, lod2: tris} caps below the class budget (room for kitbash parts)
"""
import hashlib
import json
import os

import bmesh
import bpy
import numpy as np
from mathutils import Matrix, Vector

import af_common as af
from steps import _ship_lib as sl
from steps import ship_gen

CLASS_DEFAULT_LEN = {"ship_s": 15.0, "ship_m": 50.0, "ship_l": 150.0}
CLASS_TURRETS = {"ship_s": 2, "ship_m": 4, "ship_l": 6}
CLASS_TURRET_SIZE = {"ship_s": "S", "ship_m": "M", "ship_l": "L"}
TEX_BUDGET = {"ship_s": 1024, "ship_m": 2048, "ship_l": 2048}
AXES = {"+x": (1, 0, 0), "-x": (-1, 0, 0), "+y": (0, 1, 0), "-y": (0, -1, 0), "+z": (0, 0, 1), "-z": (0, 0, -1)}
SOURCES = ("procedural", "geometry_nodes", "hunyuan3d", "trellis", "instantmesh", "triposr", "manual", "mixed",
              "external")
# (substring of the input path, manifest source, generator)
PROVENANCE = (("hunyuan", "hunyuan3d", "hunyuan3d"), ("trellis", "trellis", "trellis"),
              ("triposr", "triposr", "triposr"), ("instantmesh", "instantmesh", "instantmesh"),
              ("meshy", "external", "meshy"), ("tripo", "external", "tripo"), ("rodin", "external", "rodin"))


def file_hash(path, n=6):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:n]


def resolve(req):
    """Resolved parameters + warnings for every replaced or clamped value."""
    p = dict(req.get("params") or {})
    warn = []
    path = (req.get("inputs") or {}).get("mesh")
    if not path:
        raise ValueError("inputs.mesh is required")
    if not os.path.isfile(path):
        raise ValueError("inputs.mesh not found: %s" % path)
    cls_in = str(p.get("class", "ship_m")).lower()
    if cls_in in sl.ROLES:
        role, cls = cls_in, sl.ROLES[cls_in]["cls"]
        length = sl.ROLES[cls_in]["length"]
    elif cls_in in CLASS_DEFAULT_LEN:
        role, cls, length = None, cls_in, CLASS_DEFAULT_LEN[cls_in]
    else:
        raise ValueError("unknown class %r" % cls_in)
    length = float(p.get("length_m", length))
    if length <= 0:
        raise ValueError("length_m must be > 0")
    a, b = sl.CLASS_LENGTH[cls]
    if not a <= length <= b:
        new = next((c for c, (a2, b2) in sl.CLASS_LENGTH.items() if a2 <= length <= b2),
                   "ship_s" if length < a else "ship_l")
        warn.append("length_m %.1f outside the %s range %.0f-%.0f m: budget_class %s used" % (length, cls, a, b, new))
        cls = new
    nose = str(p.get("nose", "auto")).lower()
    if nose != "auto" and (nose not in AXES or nose[1] == "z"):
        warn.append("nose %r invalid (auto | +x | -x | +y | -y); auto used" % nose)
        nose = "auto"
    up = str(p.get("up", "auto")).lower()
    if up != "auto" and up not in AXES:
        warn.append("up %r invalid; auto used" % up)
        up = "auto"
    paint = resolve_paint(p["paint"], warn) if p.get("paint") else None
    mode = str(p.get("materials", "paint" if paint else "classify")).lower()
    if mode not in ("classify", "keep_texture", "single", "paint"):
        warn.append("materials %r unknown (classify | keep_texture | single | paint); classify used" % mode)
        mode = "classify"
    if mode == "paint" and not paint:
        warn.append("materials=paint needs params.paint; classify used")
        mode = "classify"
    pal_mode = str(p.get("palette", "input")).lower()
    if pal_mode not in ("input", "faction"):
        warn.append("palette %r unknown (input | faction); input used" % pal_mode)
        pal_mode = "input"
    tsock = str(p.get("turret_sockets", "flats")).lower()
    if tsock not in ("flats", "protrusions", "auto"):
        warn.append("turret_sockets %r unknown (flats | protrusions | auto); flats used" % tsock)
        tsock = "flats"
    tcue = str(p.get("thruster_cue", "auto")).lower()
    if tcue not in ("auto", "glow", "geometry"):
        warn.append("thruster_cue %r unknown (auto | glow | geometry); auto used" % tcue)
        tcue = "auto"
    lod_targets = {}
    for k, v in (p.get("lod_targets") or {}).items():
        if k in ("lod1", "lod2") and isinstance(v, (int, float)) and v >= 50:
            lod_targets[k] = int(v)
        else:
            warn.append("lod_targets.%s ignored (lod1 | lod2, >= 50 tris)" % k)
    tur = int(p.get("turret_count", CLASS_TURRETS[cls]))
    if tur < 0:
        warn.append("turret_count %d clamped to 0" % tur)
        tur = 0
    cap = int(sl.BUDGETS[cls]["tris"] * 0.97)
    tgt = int(p.get("target_tris", int(sl.BUDGETS[cls]["tris"] * 0.9)))
    if tgt > cap or tgt < 200:
        warn.append("target_tris %d clamped to %d..%d" % (tgt, 200, cap))
        tgt = max(200, min(cap, tgt))
    low = os.path.abspath(path).lower().replace("\\", "/")
    src_guess, gen_guess = "external", "unknown"
    for key, s_, g_ in PROVENANCE:
        if key in low:
            src_guess, gen_guess = s_, g_
            break
    source = str(p.get("source", src_guess)).lower()
    generator = str(p.get("generator", gen_guess))
    if source not in SOURCES:
        warn.append("source %r is not in the source enum; 'external' written (generator keeps the name)" % source)
        if "generator" not in p:
            generator = source
        source = "external"
    fr = str(p.get("thruster_frame", "thrust")).lower()
    if fr not in sl.THRUSTER_FRAMES:
        warn.append("thruster_frame %r unknown; thrust used" % fr)
        fr = "thrust"
    uv = str(p.get("uv", "keep")).lower()
    if uv not in ("keep", "smart", "box"):
        warn.append("uv %r unknown; keep used" % uv)
        uv = "keep"
    psz = int(p.get("preview_size", 300))
    if not 120 <= psz <= 800:
        warn.append("preview_size %d clamped to 120..800" % psz)
        psz = max(120, min(800, psz))
    stem = os.path.splitext(os.path.basename(path))[0].lower()
    stem = "".join(c if c.isalnum() else "_" for c in stem).strip("_") or "mesh"
    while "__" in stem:
        stem = stem.replace("__", "_")
    if not stem[0].isalpha():
        stem = "m" + stem
    fh = file_hash(path)
    out = {
        "role": role, "budget_class": cls, "length_m": length, "nose": nose, "up": up, "materials": mode,
        "emissive": bool(p.get("emissive", True)), "turret_count": tur, "sockets": bool(p.get("sockets", True)),
        "target_tris": tgt, "source": source, "generator": generator, "thruster_frame": fr,
        "faction": str(p.get("faction", "neutral")), "tier": int(p.get("tier", 1)), "seed": int(p.get("seed", 0)),
        "preview": bool(p.get("preview", True)), "preview_size": psz,
        "preview_sockets": bool(p.get("preview_sockets", True)), "preview_lods": bool(p.get("preview_lods", True)),
        "lods": bool(p.get("lods", True)), "uv": uv,
        "input_sha1_6": fh, "palette": pal_mode, "livery": p.get("livery") or {}, "paint": paint,
        "armor_texture": resolve_armor(p.get("armor_texture"), warn),
        "decals": resolve_decals(p.get("decals"), warn), "turret_sockets": tsock, "lod_targets": lod_targets,
        "thruster_cue": tcue,
    }
    if p.get("asset_id"):
        out["asset_id"] = str(p["asset_id"])
    else:
        base = ("ship_%s_%s" % (cls.split("_")[1], stem))[:52].rstrip("_")
        out["asset_id"] = "%s_%s" % (base, fh)
    out["class"] = role or cls
    return out, warn


# ----------------------------------------------------------------------------- import / weld / orient
def load_joined(path):
    objs = af.import_any(path)
    col_suffix = ("-col", "-convcol", "-colonly", "-convcolonly")
    meshes = [o for o in objs if o.type == "MESH" and not o.name.endswith(col_suffix)
              and not o.name.startswith("COL_")]
    if not meshes:
        raise ValueError("no mesh in %s" % path)
    bpy.context.view_layer.update()
    world = {o: o.matrix_world.copy() for o in meshes}
    for o in meshes:
        o.parent = None
        o.matrix_world = world[o]
    for o in list(objs):
        if o not in meshes:
            bpy.data.objects.remove(o, do_unlink=True)
    bpy.context.view_layer.update()
    af.apply_transforms(meshes)
    ob = af.join(meshes, name="src_mesh")
    bpy.context.view_layer.update()
    return ob


def weld(ob):
    """Merge seam-split vertices (distance 1e-5 x bbox diagonal), drop degenerate faces and loose geometry,
    recalc normals. UVs are per loop, so they survive. Returns before/after statistics."""
    me = ob.data
    if getattr(me, "has_custom_normals", False):
        af.select_only([ob])
        try:
            bpy.ops.mesh.customdata_custom_splitnormals_clear()
        except RuntimeError:
            pass
    nv = len(me.vertices)
    co = np.empty(nv * 3)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3)
    diag = float(np.linalg.norm(co.max(0) - co.min(0))) if nv else 1.0
    bm = bmesh.new()
    bm.from_mesh(me)
    v0 = len(bm.verts)
    b0 = sum(1 for e in bm.edges if e.is_boundary)
    bmesh.ops.remove_doubles(bm, verts=list(bm.verts), dist=max(1e-5 * diag, 1e-8))
    bmesh.ops.dissolve_degenerate(bm, dist=max(1e-7 * diag, 1e-10), edges=list(bm.edges))
    loose = [v for v in bm.verts if not v.link_faces]
    if loose:
        bmesh.ops.delete(bm, geom=loose, context="VERTS")
    bmesh.ops.recalc_face_normals(bm, faces=list(bm.faces))
    v1 = len(bm.verts)
    b1 = sum(1 for e in bm.edges if e.is_boundary)
    bm.to_mesh(me)
    bm.free()
    me.update()
    return {"verts_before": v0, "verts_after": v1, "boundary_edges_before": b0, "boundary_edges_after": b1,
            "weld_distance_m": round(1e-5 * diag, 8)}


def mesh_arrays(ob):
    me = ob.data
    me.calc_loop_triangles()
    nv = len(me.vertices)
    V = np.empty(nv * 3)
    me.vertices.foreach_get("co", V)
    V = V.reshape(-1, 3)
    M = np.array(ob.matrix_world)
    V = V @ M[:3, :3].T + M[:3, 3]
    nt = len(me.loop_triangles)
    T = np.empty(nt * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("vertices", T)
    T = T.reshape(-1, 3)
    a, b, c = V[T[:, 0]], V[T[:, 1]], V[T[:, 2]]
    cr = np.cross(b - a, c - a)
    area = np.linalg.norm(cr, axis=1) / 2
    N = cr / np.maximum(np.linalg.norm(cr, axis=1, keepdims=True), 1e-12)
    C = (a + b + c) / 3
    return V, T, C, N, area


def orient(ob, P, bright=None):
    """Rotation that puts the nose on +Y and up on +Z. bright: optional per-triangle emissive weights."""
    V, T, C, N, area = mesh_arrays(ob)
    info = {}
    if P["up"] in AXES:
        up = np.array(AXES[P["up"]], dtype=float)
        info["up_from"] = "params"
    else:
        up = np.array([0.0, 0.0, 1.0])
        info["up_from"] = "input +Z (glTF +Y)"
        ext = V.max(0) - V.min(0)
        if ext[2] > 1.6 * max(ext[0], ext[1]):
            info["up_warning"] = "input is taller than long; check params.up"
    if P["nose"] in AXES and P["nose"][1] != "z":
        e1 = np.array(AXES[P["nose"]], dtype=float)
        e1 = e1 - up * e1.dot(up)
        e1 /= np.linalg.norm(e1)
        info["nose_from"] = "params"
        info["nose_confidence"] = 1.0
        cues = {}
    else:
        basis = np.linalg.svd(np.eye(3) - np.outer(up, up))[0][:, :2]
        X2 = (C - (C * area[:, None]).sum(0) / area.sum()) @ basis
        cov = (X2 * area[:, None]).T @ X2 / area.sum()
        w, vec = np.linalg.eigh(cov)
        d2 = vec[:, np.argmax(w)]
        e1 = basis @ d2
        e1 /= np.linalg.norm(e1)
        info["pca_ratio"] = round(float(w.max() / max(w.min(), 1e-12)), 3)
        sign, conf, cues = nose_vote(V, C, N, area, e1, up, bright)
        e1 = e1 * sign
        info["nose_from"] = "auto vote"
        info["nose_confidence"] = round(conf, 3)
    info["nose_cues"] = cues
    y = e1
    z = up - y * up.dot(y)
    z /= np.linalg.norm(z)
    x = np.cross(y, z)
    R = Matrix((tuple(x), tuple(y), tuple(z)))  # rows: new axes expressed in input coordinates
    return R.to_4x4(), info


def nose_vote(V, C, N, area, e1, up, bright):
    t = V @ e1
    L = t.max() - t.min()
    e2 = np.cross(up, e1)
    u, w = V @ e2, V @ up
    band = 0.1 * L
    mid = (t.max() + t.min()) / 2

    def extent(mask):
        if mask.sum() < 3:
            return 0.0
        return float((u[mask].max() - u[mask].min()) * (w[mask].max() - w[mask].min()))
    ep, em = extent(t > t.max() - band), extent(t < t.min() + band)
    s1 = (em - ep) / max(em, ep, 1e-9)  # + : the +end is narrower (tapered nose)
    tc = C @ e1
    dn = N @ e1
    ap = float(area[(tc > t.max() - band * 1.2) & (dn > 0.7)].sum())
    am = float(area[(tc < t.min() + band * 1.2) & (dn < -0.7)].sum())
    s2 = (am - ap) / max(am + ap, 1e-9)  # + : the -end has more blunt aft-facing area (engines)
    cues = {"narrow_end": round(s1, 3), "blunt_end": round(s2, 3)}
    s = 0.5 * s1 + 0.4 * s2
    if bright is not None and bright.sum() > 1e-6 * area.sum():
        tb = float((tc * bright).sum() / bright.sum())
        s3 = -(tb - mid) / (L / 2)  # + : bright (engine glow) at the -end
        cues["glow_end"] = round(s3, 3)
        s += 0.6 * s3
    cues["score"] = round(s, 3)
    sign = 1.0 if s >= 0 else -1.0
    return sign, min(1.0, abs(s) * 1.5), cues


# ----------------------------------------------------------------------------- materials
def base_image(ob):
    best, best_area = None, -1.0
    me = ob.data
    areas = {}
    for p in me.polygons:
        areas[p.material_index] = areas.get(p.material_index, 0.0) + p.area
    for i, m in enumerate(me.materials):
        if m is None or not m.node_tree:
            continue
        for n in m.node_tree.nodes:
            if n.type == "BSDF_PRINCIPLED":
                link = n.inputs["Base Color"].links
                if link and link[0].from_node.type == "TEX_IMAGE" and link[0].from_node.image:
                    if areas.get(i, 0) > best_area:
                        best, best_area = link[0].from_node.image, areas.get(i, 0)
    return best


def sample_face_colors(ob, img, max_px=1024):
    """Per-polygon sRGB base colour sampled at the UV corners and centroid (image downscaled for memory)."""
    me = ob.data
    if img is None or not me.uv_layers:
        return None
    im = img.copy()
    w, h = im.size
    if max(w, h) > max_px:
        f = max_px / float(max(w, h))
        im.scale(max(1, int(w * f)), max(1, int(h * f)))
        w, h = im.size
    px = np.empty(w * h * 4, dtype=np.float32)
    im.pixels.foreach_get(px)
    bpy.data.images.remove(im)
    px = px.reshape(h, w, 4)
    uv = me.uv_layers.active.data
    nl = len(me.loops)
    UV = np.empty(nl * 2)
    uv.foreach_get("uv", UV)
    UV = UV.reshape(-1, 2)
    starts = np.empty(len(me.polygons), dtype=np.int64)
    counts = np.empty(len(me.polygons), dtype=np.int64)
    me.polygons.foreach_get("loop_start", starts)
    me.polygons.foreach_get("loop_total", counts)
    acc = np.zeros((len(me.polygons), 3), dtype=np.float64)
    cent = np.zeros((len(me.polygons), 2))
    for k in range(int(counts.max()) if len(counts) else 0):
        m = counts > k
        idx = starts[m] + k
        cent[m] += UV[idx]
        xy = UV[idx]
        xi = np.clip((xy[:, 0] % 1.0) * w, 0, w - 1).astype(int)
        yi = np.clip((xy[:, 1] % 1.0) * h, 0, h - 1).astype(int)
        acc[m] += px[yi, xi, :3]
    cent /= np.maximum(counts[:, None], 1)
    xi = np.clip((cent[:, 0] % 1.0) * w, 0, w - 1).astype(int)
    yi = np.clip((cent[:, 1] % 1.0) * h, 0, h - 1).astype(int)
    cols = (acc / np.maximum(counts[:, None], 1) + px[yi, xi, :3] * 2) / 3.0
    return np.clip(cols, 0, 1)


def fallback_face_colors(ob):
    """Per-polygon sRGB colour from a colour attribute or from the per-face material base colour;
    materials with emission are flagged as glow. Returns (cols, glow_mask, emission_srgb) or Nones."""
    me = ob.data
    n = len(me.polygons)
    if n == 0:
        return None, None, None
    ca = me.color_attributes.active_color if me.color_attributes else None
    if ca is not None and len(ca.data) in (len(me.loops), len(me.vertices)):
        cols = np.zeros((n, 3))
        per_loop = len(ca.data) == len(me.loops)
        for p in me.polygons:
            idx = p.loop_indices if per_loop else [me.loops[i].vertex_index for i in p.loop_indices]
            cols[p.index] = np.mean([ca.data[i].color_srgb[:3] for i in idx], axis=0)
        return np.clip(cols, 0, 1), np.zeros(n, bool), np.clip(cols, 0, 1)
    if len(me.materials) < 2:
        return None, None, None
    mcol, mglow, memit = [], [], []
    to_srgb = (lambda v: 12.92 * v if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055)
    for m in me.materials:
        c, g, e = (0.6, 0.6, 0.6), False, (0.0, 0.0, 0.0)
        if m is not None and m.node_tree:
            b = next((x for x in m.node_tree.nodes if x.type == "BSDF_PRINCIPLED"), None)
            if b is not None:
                lin = b.inputs["Base Color"].default_value[:3]
                c = tuple(to_srgb(v) for v in lin)
                g = b.inputs["Emission Strength"].default_value > 0.5 and max(
                    b.inputs["Emission Color"].default_value[:3]) > 0.2
                e = tuple(to_srgb(min(1.0, v)) for v in b.inputs["Emission Color"].default_value[:3]) if g else c
        mcol.append(c)
        mglow.append(g)
        memit.append(e)
    mi = np.empty(n, dtype=np.int64)
    me.polygons.foreach_get("material_index", mi)
    mi = np.clip(mi, 0, len(mcol) - 1)
    # emission colour (sRGB) of glowing materials: palette 'input' keeps it for drive / status
    return np.array(mcol)[mi], np.array(mglow)[mi], np.array(memit)[mi]


def classify(cols, centers, normals, y_min, L, emissive=True, glow_mask=None):
    """HSV rules -> material key per face (cols are sRGB 0..1)."""
    mx = cols.max(1)
    mn = cols.min(1)
    sat = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0.0)
    r, g, b = cols[:, 0], cols[:, 1], cols[:, 2]
    d = np.maximum(mx - mn, 1e-6)
    hue = np.where(mx == r, ((g - b) / d) % 6, np.where(mx == g, (b - r) / d + 2, (r - g) / d + 4)) * 60.0
    lab = np.array(["armor"] * len(cols), dtype=object)
    lab[mx < 0.3] = "graphite"
    lab[mx < 0.1] = "panel"
    lab[(sat < 0.2) & (mx >= 0.3) & (mx < 0.5)] = "trim"
    warm = (hue < 55) | (hue > 340)
    lab[(sat > 0.5) & (mx > 0.4) & warm & (mx <= 0.9)] = "marking"
    bluish = (hue > 175) & (hue < 250)
    up_front = (normals[:, 2] > 0.25) | (normals[:, 1] > 0.4)
    lab[bluish & (sat > 0.3) & (mx > 0.08) & (mx < 0.5) & up_front] = "glass"
    if emissive:
        # explicit emission flags (material colours) win; the bright-and-saturated colour rule is only for
        # textures, where it also catches yellow / orange hull markings (known limitation)
        glow = glow_mask.copy() if glow_mask is not None else (mx > 0.82) & (sat > 0.45)
        aft = centers[:, 1] < y_min + 0.22 * L
        lab[glow & aft] = "drive"
        lab[glow & ~aft] = "status"
    return lab


def smooth_labels(ob, lab, iters=2):
    """Majority filter over edge-adjacent polygons (removes single-face speckle)."""
    me = ob.data
    edge_faces = {}
    for p in me.polygons:
        for ek in p.edge_keys:
            edge_faces.setdefault(ek, []).append(p.index)
    nbrs = [[] for _ in me.polygons]
    for fs in edge_faces.values():
        if len(fs) == 2:
            a, b = fs
            nbrs[a].append(b)
            nbrs[b].append(a)
    for _ in range(iters):
        new = lab.copy()
        for i, nb in enumerate(nbrs):
            if len(nb) < 2:
                continue
            vals = [lab[j] for j in nb]
            best = max(set(vals), key=vals.count)
            if vals.count(best) >= 2 and best != lab[i] and lab[i] not in ("drive", "status"):
                new[i] = best
        lab = new
    return lab


# ----------------------------------------------------------------------------- sockets
def area_samples(V, T, area, sel, density, seed=0, max_pts=250000):
    """Barycentric points over the selected triangles, count proportional to area (>= 1 per triangle).
    Returns (points, triangle index per point, area weight per point)."""
    idx = np.where(sel)[0]
    if len(idx) == 0:
        return np.zeros((0, 3)), np.zeros(0, dtype=np.int64), np.zeros(0)
    a = area[idx]
    tot = float(a.sum())
    n = int(min(max_pts, max(len(idx), tot * density)))
    k = np.maximum(1, np.floor(a / max(tot, 1e-12) * n + 0.5).astype(np.int64))
    if k.sum() > 2 * max_pts:
        k = np.maximum(1, (k * (2.0 * max_pts / k.sum())).astype(np.int64))
    rep = np.repeat(idx, k)
    rng = np.random.default_rng(seed)
    u, v = rng.random(len(rep)), rng.random(len(rep))
    flip = u + v > 1
    u[flip], v[flip] = 1 - u[flip], 1 - v[flip]
    A, B, C = V[T[rep, 0]], V[T[rep, 1]], V[T[rep, 2]]
    P = A + (B - A) * u[:, None] + (C - A) * v[:, None]
    w = area[rep] / np.repeat(k, k)
    return P, rep, w


def blobs_2d(pts, weights, cell):
    """Connected components of an occupancy grid (8-neighbour). Returns list of index arrays."""
    if len(pts) == 0:
        return []
    ij = np.floor(pts / cell).astype(int)
    cells = {}
    for k, key in enumerate(map(tuple, ij)):
        cells.setdefault(key, []).append(k)
    seen, comps = set(), []
    for key in cells:
        if key in seen:
            continue
        stack, comp = [key], []
        seen.add(key)
        while stack:
            c = stack.pop()
            comp.extend(cells[c])
            for di in (-1, 0, 1):
                for dj in (-1, 0, 1):
                    nb = (c[0] + di, c[1] + dj)
                    if nb in cells and nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
        comps.append(np.array(comp))
    comps.sort(key=lambda c: -weights[c].sum())
    return comps


def split_bimodal(comps, pts, w, rounds=5):
    """Split a blob only where its weighted density along its long axis has two peaks with a clear valley
    between them: touching nozzles in a row (elongated blob, valley < 0.5 x the lower neighbour peak) or
    tangent ones (valley < 0.15). The deepest valley over all adjacent peak pairs is cut first; repeated
    until nothing changes. A single ring-shaped exit (round blob, shallow central dip) stays one blob."""
    for _ in range(rounds):
        changed = False
        out = []
        for c in comps:
            q = pts[c]
            if len(c) < 30:
                out.append(c)
                continue
            ext = q.max(0) - q.min(0)
            ax = int(np.argmax(ext))
            elong = ext[ax] / max(ext[1 - ax], 1e-9)
            t = q[:, ax]
            bins = int(np.clip(12 * elong, 16, 64))
            hist, edges = np.histogram(t, bins=bins, weights=w[c])
            hs = np.convolve(hist, [0.25, 0.5, 0.25], mode="same")
            top = hs.max()
            peaks = [i for i in range(bins) if hs[i] > 0.2 * top and (i == 0 or hs[i] >= hs[i - 1])
                     and (i == bins - 1 or hs[i] > hs[i + 1])]
            best = None
            for a_, b_ in zip(peaks, peaks[1:]):
                if b_ - a_ < 2:
                    continue
                vi = a_ + int(np.argmin(hs[a_:b_ + 1]))
                ratio = hs[vi] / max(min(hs[a_], hs[b_]), 1e-12)
                if ((elong > 1.4 and ratio < 0.5) or ratio < 0.15) and (best is None or ratio < best[0]):
                    best = (ratio, vi)
            if best is None:
                out.append(c)
                continue
            cut = (edges[best[1]] + edges[best[1] + 1]) / 2
            parts = [x for x in (c[t < cut], c[t >= cut]) if len(x)]
            out += parts
            changed = changed or len(parts) > 1
        comps = out
        if not changed:
            break
    comps.sort(key=lambda c: -w[c].sum())
    return comps


def x_symmetry(P, L, cell_frac=0.02):
    """Share of occupied surface cells whose X-mirror is occupied too (1 = perfectly mirror-symmetric)."""
    if len(P) == 0:
        return 0.0
    k = np.round(P / (L * cell_frac)).astype(np.int64)
    s1 = set(map(tuple, k))
    km = k.copy()
    km[:, 0] *= -1
    s2 = set(map(tuple, km))
    return len(s1 & s2) / float(max(len(s1), 1))


def find_thrusters(V, T, C, N, area, lab, y_min, L, max_n=6):
    glow = np.isin(lab, ["drive"]) if lab is not None else np.zeros(len(T), bool)
    facing = N[:, 1] < -0.6
    glow_aft = glow & (C[:, 1] < y_min + 0.25 * L) & (N[:, 1] < -0.3)  # nozzle interiors face aft
    cand = np.zeros(len(T), bool)
    band_used = 0.1
    if area[glow_aft].sum() > 0.0008 * L * L:
        # emissive nozzle interiors are the strongest cue: they never merge with a flat aft hull face
        cand = glow_aft
        band_used = 0.25
    else:
        # nozzle exits stick out furthest aft: tight band first (a flat aft hull face sits a little further
        # forward and would merge neighbouring nozzles into one blob), widen if it holds too little area
        for band in (0.015, 0.03, 0.06, 0.1):
            cand = (C[:, 1] < y_min + band * L) & facing
            band_used = band
            if area[cand].sum() > 0.002 * L * L:
                break
    if not cand.any():
        return []
    P, rep, w = area_samples(V, T, area, cand, 6000.0 / max(float(area[cand].sum()), 1e-12), seed=3)
    w = w * np.where(glow[rep], 3.0, 1.0)
    w = w * (1.0 + 2.0 * np.clip(1.0 - (P[:, 1] - y_min) / (band_used * L), 0.0, 1.0))  # prefer the aft-most
    xz = P[:, [0, 2]]
    cell = max(L * (0.006 if band_used >= 0.25 else 0.012), 1e-3)  # finer grid for emissive discs
    comps = split_bimodal(blobs_2d(xz, w, cell), xz, w)
    total = w.sum()
    out = []
    for comp in comps[:max_n * 2]:
        cw = w[comp]
        if cw.sum() < 0.04 * total:
            continue
        q = xz[comp]
        c = (q * cw[:, None]).sum(0) / cw.sum()
        ext = q.max(0) - q.min(0)
        hasglow = bool(glow[rep[comp]].any())
        # exit plane = aft-most vertex of the geometry inside the blob footprint (the glow disc sits deeper)
        foot = ((C[:, 0] - c[0]) ** 2 + (C[:, 2] - c[1]) ** 2 < (0.6 * max(ext)) ** 2) & (C[:, 1] < y_min + 0.25 * L)
        y_exit = float(V[T[foot]][..., 1].min()) if foot.any() else float(P[comp, 1].min())
        out.append(dict(x=float(c[0]), z=float(c[1]), y=y_exit, diam=float(max(ext)),
                        conf=min(1.0, 0.35 + cw.sum() / total + (0.3 if hasglow else 0.0)),
                        cue="emissive+aft_faces" if hasglow else "aft_faces"))
        if len(out) >= max_n:
            break
    # crescents of one ring (a glow interior seen only partly, e.g. painted from one camera) come out as two
    # blobs closer than their size: merge them (two separate nozzles in a row sit >= one diameter apart)
    merged = True
    while merged and len(out) > 1:
        merged = False
        for i in range(len(out)):
            for j in range(i + 1, len(out)):
                a, b = out[i], out[j]
                d = ((a["x"] - b["x"]) ** 2 + (a["z"] - b["z"]) ** 2) ** 0.5
                if d < 0.8 * max(a["diam"], b["diam"]):
                    wa, wb = a["conf"], b["conf"]
                    a.update(x=(a["x"] * wa + b["x"] * wb) / (wa + wb), z=(a["z"] * wa + b["z"] * wb) / (wa + wb),
                             y=min(a["y"], b["y"]), diam=max(a["diam"], b["diam"], d + (a["diam"] + b["diam"]) / 2),
                             conf=min(1.0, max(wa, wb) + 0.05), cue=a["cue"] + "+merged")
                    out.pop(j)
                    merged = True
                    break
            if merged:
                break
    return out


def symmetrize(dets, L, tol_frac=0.02):
    """Mirror detections across X = 0 and merge pairs closer than 0.6 x diameter. A socket confirmed on both
    sides (or on the centre line) keeps its confidence (+0.1); one seen on one side only is kept mirrored
    with 0.6 x confidence."""
    pts = []
    for i, d in enumerate(dets):
        pts.append(dict(d, src=i, mirrored=False))
        pts.append(dict(d, x=-d["x"], src=i, mirrored=True))
    clusters = []
    for p in pts:
        for cl in clusters:
            q = cl[0]
            r = 0.6 * max(min(p["diam"], q["diam"]), 1e-6)
            if (p["x"] - q["x"]) ** 2 + (p["z"] - q["z"]) ** 2 < r * r:
                cl.append(p)
                break
        else:
            clusters.append([p])
    out = []
    for cl in clusters:
        srcs = {p["src"] for p in cl}
        orig = [p for p in cl if not p["mirrored"]]
        x = float(np.mean([p["x"] for p in cl]))
        if abs(x) < tol_frac * L:
            x = 0.0
        d = dict(cl[0])
        d.update(x=x, z=float(np.mean([p["z"] for p in cl])), y=float(min(p["y"] for p in cl)),
                 diam=float(np.mean([p["diam"] for p in cl])))
        both = len(srcs) >= 2 or x == 0.0
        base = max(p["conf"] for p in cl)
        d["conf"] = min(1.0, base + 0.1) if both else base * 0.6
        d["paired"] = bool(both)
        d["seen"] = len(orig)
        out.append(d)
    # drop the mirror image of a cluster already represented (pairs come out twice otherwise)
    uniq = []
    for d in sorted(out, key=lambda e: (-e["conf"], e["x"])):
        if any(abs(d["x"] - u["x"]) < 1e-6 and abs(d["z"] - u["z"]) < 1e-6 for u in uniq):
            continue
        uniq.append(d)
    return uniq


def find_flats(V, T, N, area, L, ring, count, sign=1.0, y_lim=0.42, P=None, rep=None, w=None):
    """Top-surface flat patches (normal ~ +Z (sign=1) or -Z) big enough for a mount ring, from area samples."""
    if P is None:
        P, rep, w = area_samples(V, T, area, np.ones(len(T), bool), 60000.0 / max(float(area.sum()), 1e-12),
                                 seed=5)
    up = N[rep, 2] * sign > 0.92
    inside = np.abs(P[:, 1]) < y_lim * L
    cell = max(ring * 0.5, 1e-3)
    ij_all = np.floor(P[:, :2] / cell).astype(np.int64)
    zs_all = P[:, 2] * sign
    top = {}
    for key, z in zip(map(tuple, ij_all), zs_all):
        if key not in top or z > top[key]:
            top[key] = z
    flat_area, flat_z, flat_xy = {}, {}, {}
    for k in np.where(up & inside)[0]:
        key = tuple(ij_all[k])
        if zs_all[k] < top[key] - ring * 0.15:
            continue  # covered by something above
        flat_area[key] = flat_area.get(key, 0.0) + w[k]
        flat_z[key] = max(flat_z.get(key, -1e9), zs_all[k])
        flat_xy[key] = flat_xy.get(key, 0.0) + P[k, :2] * w[k]
    need = (ring * 0.5) ** 2 * 0.6
    cands = []
    for key in flat_area:
        win = [(key[0] + di, key[1] + dj) for di in (0, 1) for dj in (0, 1)]
        tot = sum(flat_area.get(c, 0.0) for c in win)
        zs = [flat_z[c] for c in win if c in flat_z]
        if tot < 4 * need or len(zs) < 4 or (max(zs) - min(zs)) > ring * 0.12:
            continue
        cxy = sum((flat_xy[c] for c in win if c in flat_xy), np.zeros(2)) / max(tot, 1e-12)
        cx, cy = float(cxy[0]), float(cxy[1])
        if abs(cx) < ring * 0.6:
            cx = 0.0  # on the centreline
        cands.append((tot, cx, cy, float(np.mean(zs)) * sign))
    cands.sort(key=lambda c: (-c[0], abs(c[1])))
    out = []
    for tot, cx, cy, cz in cands:
        if any((cx - o["x"]) ** 2 + (cy - o["y"]) ** 2 < (ring * 1.6) ** 2 for o in out):
            continue
        out.append(dict(x=cx, y=cy, z=cz, conf=min(1.0, 0.3 + tot / (16 * need))))
        if len(out) >= count:
            break
    return out


def pair_flats(cands, count, ring, symmetric):
    """On a symmetric mesh every off-centre turret flat gets its mirror partner (pairs stay together)."""
    if not symmetric:
        return cands[:count]
    out = []
    for c in cands:
        if len(out) >= count:
            break
        if c["x"] == 0.0:
            new = [c]
        else:
            if count - len(out) < 2:
                continue
            ax = abs(c["x"])
            new = [dict(c, x=ax), dict(c, x=-ax)]
        if any((n["x"] - o["x"]) ** 2 + (n["y"] - o["y"]) ** 2 < (ring * 1.6) ** 2 for n in new for o in out):
            continue
        out += new
    return out


def socket_tag(x, y, L, base):
    side = "" if abs(x) < 0.03 * L else ("_starboard" if x > 0 else "_port")
    zone = "forward" if y > 0.12 * L else ("aft" if y < -0.12 * L else "mid")
    return "%s_%s%s" % (base, zone, side)


# ----------------------------------------------------------------------------- palette (input colours)
def _lin(c):
    return tuple(sl.srgb_to_linear(float(x)) for x in c)


def glow_rgb(lin_rgb, gamma=1.0):
    """Emission colour from a mean linear colour: hue kept, brightest channel = 1. gamma > 1 restores the
    saturation of glow pixels that the picture clipped towards white (concept art, renders)."""
    m = max(lin_rgb)
    if m <= 1e-6:
        return None
    return tuple(round((float(v) / m) ** gamma, 4) for v in lin_rgb)


DARK_FLOOR = {"graphite": 0.03, "panel": 0.018, "trim": 0.08}  # linear luminance floors (shadowed samples)


def floor_dark(rgb, key):
    """Concept pixels of dark materials are mostly in shadow: lift the colour to a minimum albedo (hue kept)."""
    lum = 0.2126 * rgb[0] + 0.7152 * rgb[1] + 0.0722 * rgb[2]
    lo = DARK_FLOOR.get(key)
    if lo is None or lum >= lo or lum <= 1e-6:
        return rgb
    return tuple(min(1.0, c * lo / lum) for c in rgb)


def apply_livery(pal, livery, warnings):
    """params.livery {key: '#rrggbb' | [r, g, b] linear | {rgb, metal, rough, strength}} over the palette."""
    alias = {"accent": "graphite", "hull": "armor", "lights": "status", "engine": "drive"}
    for k, v in (livery or {}).items():
        key = alias.get(k, k)
        if key not in pal:
            warnings.append("livery key %r ignored (expected %s)" % (k, ", ".join(sl.MATS)))
            continue
        spec = v if isinstance(v, dict) else {"rgb": v}
        if spec.get("rgb") is not None:
            pal[key]["rgb"] = sl.parse_color(spec["rgb"])
            if key in ("status", "drive"):
                pal[key]["emit"] = pal[key]["rgb"]
        for f in ("metal", "rough", "strength"):
            if spec.get(f) is not None:
                pal[key][f] = float(spec[f])
    return pal


# ----------------------------------------------------------------------------- hull analysis (top view)
def hull_top_analysis(Ps, L, cell_frac=1.0 / 256, open_frac=0.028):
    """Top-view occupancy of area samples, opened by a square of ~open_frac x L: the opened mask is the main
    hull body (thin protrusions such as gun barrels, masts and antennas vanish), the residual components are
    protrusions. Returns dict(cell, lo, occ, main, protrusions=[...])."""
    cell = max(L * cell_frac, 1e-4)
    lo = Ps[:, :2].min(0) - 3 * cell
    ij = np.floor((Ps[:, :2] - lo) / cell).astype(np.int64)
    w, h = int(ij[:, 0].max()) + 4, int(ij[:, 1].max()) + 4
    occ = np.zeros((h, w), bool)
    occ[ij[:, 1], ij[:, 0]] = True
    occ = sl.binary_close(occ, 1)
    r = max(2, int(round(open_frac * L / cell)))
    opened = sl.binary_open(occ, r)
    lab, sizes = sl.label_components(opened)
    main = lab == 1 if len(sizes) else opened
    resid = occ & ~sl.max_filter(main, 1)
    rl, rs = sl.label_components(resid)
    near = sl.max_filter(main, 2)
    prot = []
    zcell = {}
    for k in range(len(rs)):
        if rs[k] < 6:
            break
        m = rl == k + 1
        yy, xx = np.nonzero(m)
        att = m & near
        if not att.any():
            continue
        ay, ax = np.nonzero(att)
        a = np.array([ax.mean(), ay.mean()])
        pts = np.stack([xx, yy], 1).astype(float)
        dist = np.linalg.norm(pts - a, axis=1)
        tip = pts[int(np.argmax(dist))]
        length = float(dist.max()) * cell
        width = float(m.sum()) * cell * cell / max(length, cell)
        d = (tip - a) / max(np.linalg.norm(tip - a), 1e-9)
        prot.append({"cells": m, "attach": a * cell + lo + cell / 2, "tip": tip * cell + lo + cell / 2,
                     "dir": d, "length_m": length, "width_m": width, "area_m2": float(m.sum()) * cell * cell})
    return {"cell": cell, "lo": lo, "occ": occ, "main": main, "protrusions": prot, "open_m": r * cell}


def _cells_of(points_xy, top):
    ij = np.floor((points_xy - top["lo"]) / top["cell"]).astype(np.int64)
    h, w = top["occ"].shape
    ij[:, 0] = np.clip(ij[:, 0], 0, w - 1)
    ij[:, 1] = np.clip(ij[:, 1], 0, h - 1)
    return ij


def gun_protrusions(top, Ps, L, min_len=0.035, max_width=0.045):
    """Barrel-like protrusions (long, thin, pointing sideways or forward/aft): socket base on the main hull
    silhouette edge, z = median height of the barrel samples, axis = outward along the barrel."""
    out = []
    ij = _cells_of(Ps[:, :2], top)
    for p in top["protrusions"]:
        if p["length_m"] < min_len * L or p["width_m"] > max_width * L:
            continue
        d = p["dir"]
        # broadside barrels: pointing sideways (a nose tip or a forward spike is not a gun stub), thin
        if abs(d[0]) < 0.8 or p["width_m"] > 0.35 * p["length_m"]:
            continue
        inside = p["cells"][ij[:, 1], ij[:, 0]]
        if inside.sum() < 5:
            continue
        z = float(np.median(Ps[inside, 2]))
        axis = np.array([round(float(d[0])), round(float(d[1])), 0.0]) if max(abs(d)) > 0.92 else \
            np.array([d[0], d[1], 0.0]) / max(np.linalg.norm(d), 1e-9)
        out.append({"x": float(p["attach"][0]), "y": float(p["attach"][1]), "z": z, "axis": axis.tolist(),
                    "length_m": round(p["length_m"], 3), "width_m": round(p["width_m"], 3),
                    "tip": [float(p["tip"][0]), float(p["tip"][1]), z]})
    return out


def nav_points_on_hull(top, Ps, symmetric):
    """Nav lights on the main hull body (not on barrel tips): the widest cells of the opened top-view mask on
    each side, the most forward of them; z = height of the outermost samples there."""
    main = top["main"]
    cell, lo = top["cell"], top["lo"]
    yy, xx = np.nonzero(main)
    if not len(xx):
        return None
    X = xx * cell + lo[0] + cell / 2
    Y = yy * cell + lo[1] + cell / 2
    res = {}
    for side in (-1, 1):
        ext = (X * side).max()
        sel = (X * side) >= ext - 1.5 * cell
        k = int(np.argmax(np.where(sel, Y, -1e9)))
        x0, y0 = X[k], Y[k]
        near = (np.abs(Ps[:, 1] - y0) < 2.5 * cell) & ((Ps[:, 0] * side) >= side * x0 - 2.5 * cell) & \
               ((Ps[:, 0] * side) <= side * x0 + 2.0 * cell)
        if near.sum() < 3:
            near = (np.abs(Ps[:, 1] - y0) < 4 * cell) & (np.abs(Ps[:, 0] - x0) < 4 * cell)
        if not near.any():
            return None
        q = Ps[near]
        j = int(np.argmax(q[:, 0] * side))
        res[side] = np.array([q[j, 0], y0, float(np.median(q[(q[:, 0] * side) >= q[j, 0] * side - cell, 2]))])
    if symmetric:
        p = res[-1] if abs(res[-1][0]) >= abs(res[1][0]) else res[1] * np.array([-1, 1, 1])
        res[-1] = np.array([-abs(p[0]), p[1], p[2]])
        res[1] = np.array([abs(p[0]), p[1], p[2]])
    return res


# ----------------------------------------------------------------------------- concept painting
PAINT_CODES = {"armor": 1, "graphite": 2, "trim": 3, "panel": 4, "glass": 5, "glow": 6, "marking": 7, "ignore": 255}
PAINT_NAMES = {v: k for k, v in PAINT_CODES.items()}
PAINT_THRESHOLDS = {"dark": 0.17, "very_dark": 0.07, "steel_max": 0.42, "steel_warm_max": 0.03, "glow_r": 0.55,
                    "glow_r_minus_b": 0.28, "glow_sat": 0.42, "glow_lum": 0.33}


def resolve_paint(p, warnings):
    """params.paint -> normalised dict (paths checked)."""
    if isinstance(p, str):
        p = {"image": p}
    if not isinstance(p, dict) or not p.get("image"):
        raise ValueError("params.paint needs {image: <concept image with alpha or mask>}")
    img = str(p["image"])
    if not os.path.isfile(img):
        raise ValueError("paint.image not found: %s" % img)
    mask = p.get("mask")
    if mask and not os.path.isfile(str(mask)):
        raise ValueError("paint.mask not found: %s" % mask)
    cam = p.get("camera", "fit")
    if isinstance(cam, str) and cam != "fit":
        if not os.path.isfile(cam):
            raise ValueError("paint.camera must be 'fit', a camera dict or a camera json file: %r" % cam)
        with open(cam, "r", encoding="utf-8-sig") as fh:
            cam = json.load(fh)
    if isinstance(cam, dict) and cam.get("file"):
        with open(cam["file"], "r", encoding="utf-8-sig") as fh:
            cam = json.load(fh)
    mirror = str(p.get("mirror", "x")).lower()
    if mirror not in ("x", "none"):
        warnings.append("paint.mirror %r unknown (x | none); x used" % mirror)
        mirror = "x"
    th = dict(PAINT_THRESHOLDS)
    for k, v in (p.get("thresholds") or {}).items():
        if k in th:
            th[k] = float(v)
        else:
            warnings.append("paint.thresholds key %r ignored" % k)
    regions = []
    for i, r in enumerate(p.get("regions") or []):
        box = r.get("box")
        if not (isinstance(box, (list, tuple)) and len(box) == 4) or r.get("label") not in PAINT_CODES:
            warnings.append("paint.regions[%d] ignored: needs box [u0, v0, u1, v1] (0..1 of the image) and label "
                            "in %s" % (i, ", ".join(PAINT_CODES)))
            continue
        only = [PAINT_CODES[x] for x in (r.get("only") or []) if x in PAINT_CODES]
        regions.append({"box": [float(x) for x in box], "label": r["label"], "only": only})
    return {"image": img, "mask": mask, "camera": cam, "mirror": mirror, "thresholds": th, "regions": regions,
            "markings": str(p.get("markings", "armor")).lower(),
            "fit_tris": int(p.get("fit_tris", 24000)), "smooth": int(p.get("smooth", 6)),
            "fill_radius": float(p.get("fill_radius", 0.025)),
            "min_dark_island": float(p.get("min_dark_island", 1.5e-3)), "smooth_lod0": int(p.get("smooth_lod0", 4)),
            "glow_gamma": float(p.get("glow_gamma", 1.6)), "min_toward": float(p.get("min_toward", 0.3)),
            "min_armor_island": float(p.get("min_armor_island", 4e-4))}


def paint_label_image(img, P, mask_img=None):
    """Concept pixels -> material codes (PAINT_CODES; 0 = background). The mask is the image alpha (or an
    explicit mask image), largest components only. Thin dark lines (hex seams, scratches) are removed by a
    grey closing before thresholding; dark / steel blobs are opened; glow = warm, bright, saturated pixels."""
    rgb = np.clip(img[..., :3], 0, 1)
    if mask_img is not None:
        m = mask_img[..., :3].mean(-1) > 0.5 if mask_img[..., 3].min() > 0.99 else mask_img[..., 3] > 0.5
    else:
        m = img[..., 3] > 0.5
    if m.all() or not m.any():
        raise ValueError("paint image has no usable alpha / mask (needs the subject cut out)")
    m = sl.largest_component(m, 0.02)
    H, W = m.shape
    k = max(1, int(round(W / 2000.0)))  # radii tuned at 2000 px (a gunship concept), scaled with the image
    th = P["thresholds"]
    lum = rgb @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    mx, mn = rgb.max(-1), rgb.min(-1)
    sat = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0)
    lc = sl.median3(sl.min_filter(sl.max_filter(lum, 2 * k), 2 * k))
    warm = sl.box_mean(rgb[..., 0] - rgb[..., 2], 2 * k)
    lab = np.zeros((H, W), np.uint8)
    lab[m] = 1
    steel = sl.binary_open(m & (lc >= th["dark"]) & (lc < th["steel_max"]) & (warm < th["steel_warm_max"]), 2 * k)
    dark = sl.binary_open(m & (lc < th["dark"]), 2 * k)
    lab[steel] = 3
    lab[dark] = 2
    vdark = sl.binary_open(dark & (lc < th["very_dark"]), 2 * k)
    lab[vdark] = 4
    glow = sl.binary_open(m & (rgb[..., 0] > th["glow_r"]) & ((rgb[..., 0] - rgb[..., 2]) > th["glow_r_minus_b"])
                          & (sat > th["glow_sat"]) & (lum > th["glow_lum"]), k)
    lab[glow] = 6
    for r in P["regions"]:
        u0, v0, u1, v1 = r["box"]
        x0, x1 = int(round(min(u0, u1) * W)), int(round(max(u0, u1) * W))
        y0, y1 = int(round(min(v0, v1) * H)), int(round(max(v0, v1) * H))
        sub = lab[y0:y1, x0:x1]
        sel = (sub > 0) if not r["only"] else np.isin(sub, r["only"])
        sub[sel] = PAINT_CODES[r["label"]]
    return lab, m


def _face_arrays(me):
    nf = len(me.polygons)
    C = np.empty(nf * 3)
    me.polygons.foreach_get("center", C)
    N = np.empty(nf * 3)
    me.polygons.foreach_get("normal", N)
    A = np.empty(nf)
    me.polygons.foreach_get("area", A)
    return C.reshape(-1, 3), N.reshape(-1, 3), A


def face_adjacency(me):
    """Pairs (a, b) of polygons sharing a manifold edge (vectorised over loops)."""
    nl = len(me.loops)
    lv = np.empty(nl, dtype=np.int64)
    me.loops.foreach_get("vertex_index", lv)
    ls = np.empty(len(me.polygons), dtype=np.int64)
    lt = np.empty(len(me.polygons), dtype=np.int64)
    me.polygons.foreach_get("loop_start", ls)
    me.polygons.foreach_get("loop_total", lt)
    poly = np.repeat(np.arange(len(me.polygons)), lt)
    nxt = np.arange(nl) + 1
    last = ls + lt - 1
    nxt[last] = ls
    e = np.sort(np.stack([lv, lv[nxt]], 1), 1)
    key = e[:, 0] * (len(me.vertices) + 1) + e[:, 1]
    o = np.argsort(key, kind="stable")
    ks, fs = key[o], poly[o]
    idx = np.where(ks[1:] == ks[:-1])[0]
    # keep only edges shared by exactly two loops (manifold)
    prev_ok = (idx == 0) | (ks[np.maximum(idx - 1, 0)] != ks[idx])
    next_ok = (idx + 2 >= len(ks)) | (ks[np.minimum(idx + 2, len(ks) - 1)] != ks[idx])
    sel = idx[prev_ok & next_ok]
    return fs[sel], fs[sel + 1]


def paint_mesh(ob, P, L, warnings, log):
    """Per-polygon material labels of the (oriented, scaled) full-resolution mesh from the concept: camera fit to the
    silhouette (or explicit), z-buffered projection of polygon centres, X-mirror for the hidden side, nearest-
    labelled fill, majority smoothing and island cleanup. Returns (codes per polygon, info) where info holds the
    camera, IoU and the mean input colour of every label (sRGB, from the projected polygons)."""
    from mathutils.kdtree import KDTree
    pp = P["paint"]
    img = sl.load_image(pp["image"])
    mimg = sl.load_image(pp["mask"]) if pp["mask"] else None
    lab_img, mask = paint_label_image(img, pp, mimg)
    H, W = mask.shape
    me = ob.data
    me.calc_loop_triangles()
    V = np.empty(len(me.vertices) * 3)
    me.vertices.foreach_get("co", V)
    V = V.reshape(-1, 3)
    nt = len(me.loop_triangles)
    T = np.empty(nt * 3, dtype=np.int64)
    me.loop_triangles.foreach_get("vertices", T)
    T = T.reshape(-1, 3)
    tri_poly = np.empty(nt, dtype=np.int64)
    me.loop_triangles.foreach_get("polygon_index", tri_poly)
    info = {"image": pp["image"], "image_size": [W, H]}
    # ---- camera
    if pp["camera"] == "fit":
        tmp = ob.copy()
        tmp.data = me.copy()
        bpy.context.scene.collection.objects.link(tmp)
        sl.decimate_to([tmp], pp["fit_tris"])
        tm = tmp.data
        tm.calc_loop_triangles()
        Vf = np.empty(len(tm.vertices) * 3)
        tm.vertices.foreach_get("co", Vf)
        Tf = np.empty(len(tm.loop_triangles) * 3, dtype=np.int64)
        tm.loop_triangles.foreach_get("vertices", Tf)
        bpy.data.objects.remove(tmp, do_unlink=True)
        bpy.data.meshes.remove(tm)
        flog = []
        cam, fit_iou, finfo = sl.fit_camera(Vf.reshape(-1, 3), Tf.reshape(-1, 3), mask, log=flog)
        info.update(camera_from="fit", fit_iou_work=fit_iou, fit=finfo)
        af.log("paint camera fitted", iou=fit_iou, params=cam["params"])
    else:
        cam = sl.cam_normalize(pp["camera"])
        if list(cam["resolution"]) != [W, H]:
            sx = W / float(cam["resolution"][0])
            if abs(sx - H / float(cam["resolution"][1])) > 0.01:
                warnings.append("paint.camera resolution %s differs in aspect from the image %dx%d"
                                % (cam["resolution"], W, H))
            cam = dict(cam, resolution=[W, H])
        info["camera_from"] = "params"
    # silhouette IoU of the full mesh at (up to) 1000 px wide
    wq = min(1000, W)
    resq = (wq, int(round(H * wq / float(W))))
    px, py, zc = sl.cam_project(cam, V, resq)
    info["silhouette_iou"] = round(sl.mask_iou(sl.tri_raster(px, py, T, resq[0], resq[1]),
                                               sl.resize_mask(mask, wq)), 4)
    info["camera"] = cam
    # ---- z-buffer at the image resolution from area samples of every triangle
    px, py, zc = sl.cam_project(cam, V)
    a, b, c = np.stack([px[T[:, 0]], py[T[:, 0]]], 1), np.stack([px[T[:, 1]], py[T[:, 1]]], 1), \
        np.stack([px[T[:, 2]], py[T[:, 2]]], 1)
    za, zb, zcc = zc[T[:, 0]], zc[T[:, 1]], zc[T[:, 2]]
    area = np.abs((b[:, 0] - a[:, 0]) * (c[:, 1] - a[:, 1]) - (b[:, 1] - a[:, 1]) * (c[:, 0] - a[:, 0])) / 2
    n = np.minimum(np.ceil(area * 2.5).astype(np.int64), 6000) + 1
    rep_ = np.repeat(np.arange(nt), n)
    rng = np.random.default_rng(1)
    uu, vv = rng.random(len(rep_)), rng.random(len(rep_))
    flip = uu + vv > 1
    uu[flip], vv[flip] = 1 - uu[flip], 1 - vv[flip]
    sx = a[rep_, 0] + (b[rep_, 0] - a[rep_, 0]) * uu + (c[rep_, 0] - a[rep_, 0]) * vv
    sy = a[rep_, 1] + (b[rep_, 1] - a[rep_, 1]) * uu + (c[rep_, 1] - a[rep_, 1]) * vv
    sz = za[rep_] + (zb[rep_] - za[rep_]) * uu + (zcc[rep_] - za[rep_]) * vv
    ok = (sx >= 0) & (sx < W - 0.5) & (sy >= 0) & (sy < H - 0.5) & (sz > 0)
    zbuf = np.full((H, W), np.inf)
    np.minimum.at(zbuf, (np.round(sy[ok]).astype(int), np.round(sx[ok]).astype(int)), sz[ok])
    del sx, sy, sz, rep_, uu, vv
    C, N, Af = _face_arrays(me)
    nf = len(C)
    fx, fy, fz = sl.cam_project(cam, C)
    loc = np.array(cam["location"])
    tc = loc - C
    toward = (tc * N).sum(1) / np.maximum(np.linalg.norm(tc, axis=1), 1e-9)
    ix = np.clip(np.round(fx).astype(int), 0, W - 1)
    iy = np.clip(np.round(fy).astype(int), 0, H - 1)
    inside = (fx >= 0) & (fx < W - 0.5) & (fy >= 0) & (fy < H - 0.5) & (fz > 0)
    # visible: the face centre is (within tolerance) the nearest surface at its pixel; pixels no sample hit
    # fall back to the nearest depth of the 3x3 neighbourhood (a plain 3x3 minimum would hide recessed
    # bay walls next to an overhanging edge)
    zfill = np.where(np.isfinite(zbuf), zbuf, 1e30)
    zn = sl.min_filter(zfill, 1)
    zp = zfill[iy, ix]
    zref = np.where(zp < 1e29, zp, zn[iy, ix])
    tol = 0.004 * L
    vis = inside & (toward > pp["min_toward"]) & (fz <= zref + tol)
    samp = lab_img[iy, ix]
    code = np.zeros(nf, np.int16)
    projected = vis & (samp > 0) & (samp != 255)
    code[projected] = samp[projected]
    rgbf = np.clip(img[iy, ix, :3], 0, 1)
    info["projected_faces"] = int(projected.sum())
    # ---- X mirror: hidden faces take the label of the projected face nearest to their mirror point
    src = np.where(code > 0, 1, 0).astype(np.int8)
    if pp["mirror"] == "x" and projected.any():
        # the surface point nearest to the X-mirror of each hidden face centre (BVH: exact on large faces too)
        from mathutils.bvhtree import BVHTree
        bvh = BVHTree.FromPolygons(V.tolist(), T.tolist())
        rad = 0.01 * L
        mir = 0
        for i in np.where(code == 0)[0]:
            loc, nrm, ti, d = bvh.find_nearest(Vector((-C[i, 0], C[i, 1], C[i, 2])), rad)
            if ti is None:
                continue
            k = int(tri_poly[ti])
            if not projected[k]:
                continue
            if N[k, 0] * -N[i, 0] + N[k, 1] * N[i, 1] + N[k, 2] * N[i, 2] > 0.3:
                code[i] = code[k]
                src[i] = 2
                mir += 1
        info["mirrored_faces"] = mir
    # ---- fill: still unlabelled faces take the majority of nearby labelled faces (bay floors next to dark
    # walls stay dark), else geometry rules
    lab_idx = np.where(code > 0)[0]
    rest = np.where(code == 0)[0]
    filled = 0
    if len(lab_idx) and len(rest):
        kd2 = KDTree(len(lab_idx))
        for j, i in enumerate(lab_idx):
            kd2.insert(C[i], j)
        kd2.balance()
        rad = pp["fill_radius"] * L
        new = {}
        for i in rest:
            votes = {}
            for co, j, d in kd2.find_n(C[i], 8):
                if j is None or d > rad:
                    continue
                k = lab_idx[j]
                cv = int(code[k])
                if cv == 6:
                    continue
                votes[cv] = votes.get(cv, 0.0) + 1.0 / (0.2 * rad + d)
            if votes:
                new[i] = max(votes, key=votes.get)
        for i, cv in new.items():
            code[i] = cv
            src[i] = 3
        filled = len(new)
    info["filled_faces"] = filled
    y_min = float(V[:, 1].min())
    rest = code == 0
    code[rest & (C[:, 1] < y_min + 0.2 * L) & (N[:, 1] < -0.5)] = 3
    code[code == 0] = 1
    src[rest] = 4
    if pp["markings"] != "keep":
        code[code == 7] = 1  # markings come back as decals (params.decals)
    # ---- majority smoothing over edge-adjacent faces (glow never votes, never changes)
    fa, fb = face_adjacency(me)
    for _ in range(max(0, pp["smooth"])):
        cnt = np.zeros((nf, 8), np.int32)
        lb = code.copy()
        lb[lb == 6] = 0
        lb = np.clip(lb, 0, 7)
        np.add.at(cnt, (fa, lb[fb]), 1)
        np.add.at(cnt, (fb, lb[fa]), 1)
        cnt[:, 0] = 0
        best = cnt.argmax(1)
        win = cnt[np.arange(nf), best]
        chg = (win >= 2) & (best != code) & (code != 6) & (best > 0)
        code[chg] = best[chg]

    def components(m):
        # connected over the whole mask (a dark bay of mixed graphite / panel / steel is one island)
        comp = np.where(m, np.arange(nf), nf)
        same_ = m[fa] & m[fb]
        ea, eb = fa[same_], fb[same_]
        for _ in range(4000):
            m1 = np.minimum(comp[ea], comp[eb])
            old = comp.copy()
            np.minimum.at(comp, ea, m1)
            np.minimum.at(comp, eb, m1)
            comp[m] = comp[comp[m]]
            if np.array_equal(comp, old):
                break
        return comp

    def absorb(m, max_area):
        comp = components(m)
        ids = comp[m]
        tot = np.bincount(ids, weights=Af[m], minlength=nf + 1)
        small = m & (tot[np.minimum(comp, nf)] < max_area)
        cnt = np.zeros((nf + 1, 8), np.float64)
        bnd = small[fa] & ~small[fb]
        np.add.at(cnt, (comp[fa[bnd]], np.clip(code[fb[bnd]], 0, 7)), Af[fa[bnd]])
        bnd2 = small[fb] & ~small[fa]
        np.add.at(cnt, (comp[fb[bnd2]], np.clip(code[fa[bnd2]], 0, 7)), Af[fb[bnd2]])
        cnt[:, 6] = 0
        tgt = cnt.argmax(1)
        has = cnt.max(1) > 0
        ch = small & has[comp] & (tgt[comp] > 0)
        code[ch] = tgt[comp[ch]]
        return int(ch.sum())

    info["islands_absorbed"] = [absorb(np.isin(code, [2, 3, 4]), pp["min_dark_island"] * L * L),
                                absorb(code == 1, pp["min_armor_island"] * L * L)]
    # ---- mean input colour per label (projected faces; light materials: the lit upper half by luminance)
    colours = {}
    lum = rgbf @ np.array([0.2126, 0.7152, 0.0722])
    for cv in np.unique(code):
        sel = projected & (code == cv)
        if sel.sum() < 20:
            continue
        if cv != 6:  # the lit half: shadowed pixels are not the albedo
            sel = sel & (lum >= np.median(lum[sel]))
        if cv == 6:
            mxv = rgbf.max(1)
            sat = (mxv - rgbf.min(1)) / np.maximum(mxv, 1e-6)
            sel = sel & (sat >= np.median(sat[sel]))
        w = Af[sel]
        colours[PAINT_NAMES[int(cv)]] = [round(float(x), 4) for x in (rgbf[sel] * w[:, None]).sum(0) / max(w.sum(),
                                                                                                         1e-12)]
    info["colours_srgb"] = colours
    info["label_faces"] = {PAINT_NAMES[int(k)]: int((code == k).sum()) for k in np.unique(code)}
    info["sources"] = {"projected": int((src == 1).sum()), "mirrored": int((src == 2).sum()),
                       "filled": int((src == 3).sum()), "rule": int((src == 4).sum())}
    if info["silhouette_iou"] < 0.8:
        warnings.append("paint: concept silhouette IoU %.3f < 0.8 — check the camera (paint.camera) and the "
                        "image mask" % info["silhouette_iou"])
    return code, info


def write_paint_overlay(path, mask, cam, objs, size=1000):
    """Silhouette overlay of the camera on the final LOD0 objects: green both, red concept only, blue mesh only.
    Returns (path, IoU)."""
    Vs, Ts, off = [], [], 0
    for o in objs:
        me = o.data
        me.calc_loop_triangles()
        V = np.empty(len(me.vertices) * 3)
        me.vertices.foreach_get("co", V)
        M = np.array(o.matrix_world)
        Vs.append(V.reshape(-1, 3) @ M[:3, :3].T + M[:3, 3])
        T = np.empty(len(me.loop_triangles) * 3, dtype=np.int64)
        me.loop_triangles.foreach_get("vertices", T)
        Ts.append(T.reshape(-1, 3) + off)
        off += len(Vs[-1])
    V, T = np.concatenate(Vs), np.concatenate(Ts)
    H, W = mask.shape
    w = min(size, W)
    res = (w, int(round(H * w / float(W))))
    px, py, _ = sl.cam_project(cam, V, res)
    m = sl.tri_raster(px, py, T, res[0], res[1])
    c = sl.resize_mask(mask, w)
    out = np.zeros((res[1], res[0], 3), np.uint8)
    out[m & c] = (90, 200, 90)
    out[c & ~m] = (230, 60, 60)
    out[m & ~c] = (60, 120, 255)
    return sl.write_png(path, out), round(sl.mask_iou(m, c), 4)


# ----------------------------------------------------------------------------- armour texture (kit)
KITS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "kits")


def load_kits():
    path = os.path.join(KITS_DIR, "kits.json")
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def resolve_armor(spec, warnings):
    if spec in (None, False, ""):
        return None
    if isinstance(spec, str):
        spec = {"texture": spec}
    kits = load_kits()["textures"]
    name = spec.get("texture")
    if name not in kits:
        raise ValueError("armor_texture %r is not a kit texture (%s)" % (name, ", ".join(sorted(kits))))
    out = {"texture": name, "tile_m": float(spec.get("tile_m", kits[name].get("tile_m", 20.0))),
           "normal_strength": float(spec.get("normal_strength", 1.0)), "regions": []}
    for i, r in enumerate(spec.get("regions") or []):
        t = r.get("texture")
        box = r.get("box") or {}
        if t not in kits or not (isinstance(box.get("min"), (list, tuple)) and isinstance(box.get("max"), (list, tuple))):
            warnings.append("armor_texture.regions[%d] ignored: needs texture (kit) and box {min, max} in metres" % i)
            continue
        out["regions"].append({"texture": t, "name": str(r.get("name") or t.replace("armor_", "")),
                               "tile_m": float(r.get("tile_m", kits[t].get("tile_m", out["tile_m"]))),
                               "min": [float(x) for x in box["min"]], "max": [float(x) for x in box["max"]]})
    return out


def _kit_image(name, which, non_color=False):
    kits = load_kits()["textures"]
    path = os.path.join(KITS_DIR, kits[name][which])
    im = bpy.data.images.load(path, check_existing=True)
    if non_color:
        im.colorspace_settings.name = "Non-Color"
    im.pack()
    return im


def texture_material(base, name, kit, tint, normal_strength=1.0):
    """Copy of a library material with the kit albedo (x tint as a Multiply node -> glTF baseColorFactor) and the
    kit normal map. The tile size is kept on the material (af_tile_m) for kitbash UVs."""
    kits = load_kits()["textures"]
    if base.name == name:  # the flat library material keeps its slot until every LOD is re-materialled
        base.name = name + "_flat"
    m = base.copy()
    m.name = name
    nt = m.node_tree
    bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
    ta = nt.nodes.new("ShaderNodeTexImage")
    ta.image = _kit_image(kit, "albedo")
    ta.location = (-700, 250)
    mean = kits[kit].get("mean_linear_rgb", [0.6, 0.55, 0.5])
    f = [max(0.0, min(1.0, t / max(mv, 1e-4))) for t, mv in zip(tint, mean)]
    if max(abs(x - 1.0) for x in f) > 0.03:
        mix = nt.nodes.new("ShaderNodeMix")
        mix.data_type = "RGBA"
        mix.blend_type = "MULTIPLY"
        mix.inputs["Factor"].default_value = 1.0
        mix.location = (-350, 250)
        nt.links.new(ta.outputs["Color"], mix.inputs[6])
        mix.inputs[7].default_value = (*f, 1.0)
        nt.links.new(mix.outputs[2], bsdf.inputs["Base Color"])
    else:
        nt.links.new(ta.outputs["Color"], bsdf.inputs["Base Color"])
    tn = nt.nodes.new("ShaderNodeTexImage")
    tn.image = _kit_image(kit, "normal", non_color=True)
    tn.location = (-700, -150)
    nm = nt.nodes.new("ShaderNodeNormalMap")
    nm.inputs["Strength"].default_value = normal_strength
    nm.location = (-350, -150)
    nt.links.new(tn.outputs["Color"], nm.inputs["Color"])
    nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
    m["af_texture"] = kit
    m.diffuse_color = (*[min(1.0, t) for t in tint], 1.0)
    return m


def box_uv_metres(ob, tile):
    """Dominant-axis (box / triplanar-style) UVs in metres / tile, mirrored on the negative side so the texture
    is never flipped; world space of the object (stable across LODs and kitbash parts)."""
    me = ob.data
    if not me.uv_layers:
        me.uv_layers.new(name="UVMap")
    uv = me.uv_layers[0].data
    if not len(me.loops):
        return
    M = np.array(ob.matrix_world)
    nv = len(me.vertices)
    co = np.empty(nv * 3)
    me.vertices.foreach_get("co", co)
    co = co.reshape(-1, 3) @ M[:3, :3].T + M[:3, 3]
    nl = len(me.loops)
    vi = np.empty(nl, np.int64)
    me.loops.foreach_get("vertex_index", vi)
    npoly = len(me.polygons)
    pn = np.empty(npoly * 3)
    me.polygons.foreach_get("normal", pn)
    pn = pn.reshape(-1, 3) @ M[:3, :3].T
    lt = np.empty(npoly, np.int64)
    me.polygons.foreach_get("loop_total", lt)
    pol = np.repeat(np.arange(npoly), lt)
    ax = np.abs(pn).argmax(1)[pol]
    c = co[vi]
    sgn = np.sign(pn[pol, ax])
    sgn[sgn == 0] = 1
    u = np.where(ax == 0, c[:, 1] * -sgn, np.where(ax == 1, c[:, 0] * sgn, c[:, 0]))
    v = np.where(ax == 2, c[:, 1] * sgn, c[:, 2])
    uv.foreach_set("uv", (np.stack([u / tile, v / tile], 1)).ravel())


def split_faces_in_box(ob, lo, hi, name, material):
    """Move the faces whose centre lies inside the box (world) to a new object (same parent) with `material`."""
    me = ob.data
    M = ob.matrix_world
    C = np.array([(M @ p.center)[:] for p in me.polygons])
    inside = np.all((C >= np.array(lo)) & (C <= np.array(hi)), axis=1)
    if not inside.any():
        return None
    ob2 = ob.copy()
    ob2.data = me.copy()
    ob2.name = name
    ob2.data.name = name
    bpy.context.scene.collection.objects.link(ob2)
    ob2.parent = ob.parent
    ob2.matrix_world = ob.matrix_world.copy()
    for o_, keep in ((ob, False), (ob2, True)):
        bm = bmesh.new()
        bm.from_mesh(o_.data)
        bm.faces.ensure_lookup_table()
        kill = [f for f in bm.faces if bool(inside[f.index]) != keep]
        bmesh.ops.delete(bm, geom=kill, context="FACES")
        bm.to_mesh(o_.data)
        bm.free()
    ob2.data.materials.clear()
    ob2.data.materials.append(material)
    return ob2


# ----------------------------------------------------------------------------- decals
def find_font(name):
    """A font file: an existing path, or a Windows font by name (file stem, case-insensitive)."""
    if name and os.path.isfile(name):
        return name
    fd = os.path.join(os.environ.get("WINDIR", r"C:/Windows"), "Fonts")
    stem = str(name or "DejaVuSans-Bold").lower().replace(" ", "")
    if os.path.isdir(fd):
        files = sorted(os.listdir(fd))
        for f in files:
            if os.path.splitext(f)[0].lower() == stem and f.lower().endswith((".ttf", ".otf")):
                return os.path.join(fd, f)
        for f in files:
            if f.lower().startswith(stem) and f.lower().endswith((".ttf", ".otf")):
                return os.path.join(fd, f)
    raise ValueError("font %r not found (give a Windows font name such as bahnschrift, arialbd, segoeuib or a "
                     "path)" % name)


def resolve_decals(specs, warnings):
    out = []
    for i, d in enumerate(specs or []):
        if not isinstance(d, dict) or not (d.get("text") or d.get("lines")):
            warnings.append("decals[%d] ignored: needs text" % i)
            continue
        side = str(d.get("side", "both")).lower()
        if side not in ("port", "starboard", "both", "top"):
            warnings.append("decals[%d].side %r unknown; both used" % (i, side))
            side = "both"
        pos = d.get("position") or d.get("center")
        size = d.get("size")
        if not (isinstance(pos, (list, tuple)) and len(pos) in (2, 3)) or not (isinstance(size, (list, tuple))
                                                                             and len(size) == 2):
            warnings.append("decals[%d] ignored: position [y, z] (sides) / [x, y] (top) and size [w, h] m needed" % i)
            continue
        lines = d.get("lines") or str(d["text"]).split("\n")
        out.append({"lines": lines, "line_scales": d.get("line_scales") or [1.0] * len(lines),
                    "font": find_font(d.get("font", "DejaVuSans-Bold")), "side": side,
                    "position": [float(x) for x in pos], "size": [float(x) for x in size],
                    "bold": float(d.get("bold", 0.0)), "slashed_zero": bool(d.get("slashed_zero", False)),
                    "align": str(d.get("align", "left")).upper(),
                    "color": sl.parse_color(d.get("color", "#2a2826")), "wear": float(d.get("wear", 0.35)),
                    "px_per_m": float(d.get("px_per_m", 140.0)), "offset_m": d.get("offset_m"),
                    "text": "\n".join(lines)})
    return out


def decal_atlas(decals, seed=7):
    """Render every decal's text into one RGBA atlas (shelf packing, width <= 2048). Returns (image, cells)."""
    cells = []
    for d in decals:
        a = sl.render_text_mask(d["lines"], d["font"], d["px_per_m"], d["size"], bold_px=d["bold"] * d["px_per_m"],
                                align="CENTER" if d["align"] == "CENTER" else "LEFT", scales=d["line_scales"],
                                slashed_zero=d["slashed_zero"])
        if d["wear"] > 0:
            rng = np.random.default_rng(seed + len(cells))
            h, w = a.shape
            noise = sl.box_mean(rng.random((h, w)).astype(np.float32), 1)
            noise = (noise - noise.mean()) / max(noise.std(), 1e-6)
            chips = noise > (3.2 - 1.2 * d["wear"])
            scratch = np.zeros((h, w), bool)
            for _ in range(int(60 * d["wear"] * (h * w) / 4e5) + 4):
                x0, y0 = rng.integers(0, w), rng.integers(0, h)
                ln = int(rng.integers(8, 50))
                ang = rng.normal(0.35, 0.4)
                t = np.linspace(0, 1, ln * 2)
                xs = np.clip((x0 + t * ln * np.cos(ang)).astype(int), 0, w - 1)
                ys = np.clip((y0 + t * ln * np.sin(ang)).astype(int), 0, h - 1)
                scratch[ys, xs] = True
            a = a * (1 - 0.85 * (chips | scratch))
        cells.append(a)
    Wt = min(2048, max(c.shape[1] for c in cells))
    x = y = row_h = 0
    rects = []
    for c in cells:
        h, w = c.shape
        if w > Wt:
            raise ValueError("decal %dx%d px wider than the atlas" % (w, h))
        if x + w > Wt:
            x, y = 0, y + row_h + 8
            row_h = 0
        rects.append((x, y, x + w, y + h))
        x += w + 8
        row_h = max(row_h, h)
    Ht = 1
    while Ht < y + row_h:
        Ht *= 2
    Wp = 1
    while Wp < Wt:
        Wp *= 2
    atlas = np.zeros((Ht, Wp, 4), np.float32)
    for d, c, (x0, y0, x1, y1) in zip(decals, cells, rects):
        col = [(12.92 * v if v <= 0.0031308 else 1.055 * v ** (1 / 2.4) - 0.055) for v in d["color"]]
        atlas[y0:y1, x0:x1, :3] = col
        atlas[y0:y1, x0:x1, 3] = c
    img = bpy.data.images.new("AF_Marking_decals", Wp, Ht, alpha=True)
    img.pixels.foreach_set(atlas[::-1].ravel())
    img.file_format = "PNG"
    img.pack()
    return img, [{"rect_px": list(r), "tex": [Wp, Ht]} for r in rects]


def decal_material(img, name):
    m = bpy.data.materials.new(name)
    m.use_nodes = True
    nt = m.node_tree
    b = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
    td = nt.nodes.new("ShaderNodeTexImage")
    td.image = img
    td.extension = "EXTEND"
    nt.links.new(td.outputs["Color"], b.inputs["Base Color"])
    nt.links.new(td.outputs["Alpha"], b.inputs["Alpha"])
    b.inputs["Roughness"].default_value = 0.75
    b.inputs["Metallic"].default_value = 0.0
    for attr, val in (("surface_render_method", "BLENDED"), ("blend_method", "BLEND")):
        try:
            setattr(m, attr, val)
        except (AttributeError, TypeError):
            pass
    m.diffuse_color = (0.16, 0.155, 0.15, 1)
    return m


def decal_frames(d, bvh, L):
    """(centre, normal, u, v) per placed copy of a decal: the centre is found by a ray from outside (side: along
    +-X at height z; top: down at x, y), the plane normal is the mean hull normal under the decal; u = reading
    direction (port: towards aft, starboard: towards the nose, top: towards the nose)."""
    out = []
    sides = ["port", "starboard"] if d["side"] == "both" else [d["side"]]
    w, h = d["size"]
    for side in sides:
        if side == "top":
            x, y = d["position"][:2]
            origin, ray = Vector((x, y, 2 * L)), Vector((0, 0, -1))
            u0 = Vector((0, 1, 0))
        else:
            sgn = -1.0 if side == "port" else 1.0
            y, z = d["position"][-2:]
            origin, ray = Vector((sgn * 2 * L, y, z)), Vector((-sgn, 0, 0))
            u0 = Vector((0, -1, 0)) if side == "port" else Vector((0, 1, 0))
        hit = bvh.ray_cast(origin, ray, 4 * L)
        if hit[0] is None:
            continue
        c = hit[0]
        ns = []
        for du in (-0.4, 0.0, 0.4):
            for dv in (-0.35, 0.0, 0.35):
                o2 = origin + u0 * (du * w) + Vector((0, 0, 1)) * (dv * h) if side != "top" else \
                    origin + u0 * (du * w) + Vector((1, 0, 0)) * (dv * h)
                hh = bvh.ray_cast(o2, ray, 4 * L)
                if hh[0] is not None and (hh[0] - c).length < max(w, h):
                    ns.append(hh[1])
        n = sum(ns, Vector()) / max(len(ns), 1) if ns else hit[1]
        n.normalize()
        u = u0 - n * u0.dot(n)
        u.normalize()
        v = n.cross(u)
        if side != "top" and v.z < 0:
            v = -v
        out.append((side, c, n, u, v))
    return out


def build_decals(objs, decals, cells, mat, L, suffix, step, offset):
    """Decal mesh: a grid in each decal plane projected onto the hull along -n (BVH of these objects, glass
    excluded) and lifted by `offset`; quads whose corners miss or jump are dropped. One object."""
    from mathutils.bvhtree import BVHTree
    Vs, Fs, off_ = [], [], 0
    for o in objs:
        if not o.data.materials or not o.data.materials[0] or o.data.materials[0].name.startswith("AF_Glass"):
            continue
        M = o.matrix_world
        Vs += [tuple(M @ vv.co) for vv in o.data.vertices]
        Fs += [tuple(i + off_ for i in p.vertices) for p in o.data.polygons]
        off_ += len(o.data.vertices)
    if not Fs:
        return None, {}
    bvh = BVHTree.FromPolygons(Vs, Fs)
    bm = bmesh.new()
    uvl = bm.loops.layers.uv.new("UVMap")
    stats = {}
    for k, (d, cell) in enumerate(zip(decals, cells)):
        x0, y0, x1, y1 = cell["rect_px"]
        Wt, Ht = cell["tex"]
        w, h = d["size"]
        for side, c, n, u, v in decal_frames(d, bvh, L):
            nu, nv = max(2, int(round(w / step))), max(2, int(round(h / step)))
            grid = {}
            for i in range(nu + 1):
                for j in range(nv + 1):
                    s_, t_ = i / nu, j / nv
                    p = c + u * ((s_ - 0.5) * w) + v * ((0.5 - t_) * h)
                    hit = bvh.ray_cast(p + n * (0.05 * L), -n, 0.1 * L)
                    if hit[0] is None or abs((hit[0] - p).dot(n)) > 0.012 * L:
                        continue
                    grid[(i, j)] = (bm.verts.new(hit[0] + n * offset), s_, t_)
            cnt = 0
            for i in range(nu):
                for j in range(nv):
                    q = [grid.get(kk) for kk in ((i, j), (i, j + 1), (i + 1, j + 1), (i + 1, j))]
                    if any(x is None for x in q):
                        continue
                    try:
                        f = bm.faces.new([x[0] for x in q])
                    except ValueError:
                        continue
                    f.normal_update()
                    if f.normal.dot(n) < 0:
                        f.normal_flip()
                    for lp in f.loops:
                        x = next(e for e in q if e[0] is lp.vert)
                        lp[uvl].uv = ((x0 + x[1] * (x1 - x0)) / Wt, 1.0 - (y0 + x[2] * (y1 - y0)) / Ht)
                    cnt += 1
            stats["%d_%s" % (k, side)] = cnt
    if not bm.faces:
        bm.free()
        return None, stats
    me = bpy.data.meshes.new("M_hull_marking" + suffix)
    bm.to_mesh(me)
    bm.free()
    me.materials.append(mat)
    for p in me.polygons:
        p.use_smooth = True
    ob = bpy.data.objects.new("M_hull_marking" + suffix, me)
    bpy.context.scene.collection.objects.link(ob)
    return ob, stats


def finish_surfaces(objs, parent, suffix, lod, P, mats, ctx, decal_cap=None, warnings=None):
    """Armour texture (+ regions) with metric box UVs and decals on one LOD set. Returns the new object list."""
    out = list(objs)
    arm = P.get("armor_texture")
    if arm:
        if "armor_mat" not in ctx:
            tint = mats["armor"].node_tree.nodes["Principled BSDF"].inputs["Base Color"].default_value[:3] \
                if mats["armor"].node_tree.nodes.get("Principled BSDF") else (0.6, 0.55, 0.5)
            ctx["armor_mat"] = texture_material(mats["armor"], "AF_Armor", arm["texture"], tint,
                                                arm["normal_strength"])
            ctx["region_mats"] = [texture_material(mats["armor"], "AF_Armor" + r["name"].capitalize(), r["texture"],
                                                   tint, arm["normal_strength"]) for r in arm["regions"]]
            ctx["armor_mat"]["af_tile_m"] = arm["tile_m"]
            for r, m in zip(arm["regions"], ctx["region_mats"]):
                m["af_tile_m"] = r["tile_m"]
        a_ = next((o for o in out if o.data.materials and o.data.materials[0] == mats["armor"]), None)
        if a_ is not None:
            a_.data.materials[0] = ctx["armor_mat"]
            for r, m in zip(arm["regions"], ctx["region_mats"]):
                o2 = split_faces_in_box(a_, r["min"], r["max"], "M_hull_armor%s%s" % (r["name"], suffix), m)
                if o2 is not None:
                    out.append(o2)
                    box_uv_metres(o2, r["tile_m"])
            box_uv_metres(a_, arm["tile_m"])
    decals = P.get("decals")
    if decals and lod <= 1:
        if "decal_mat" not in ctx:
            img, cells = decal_atlas(decals)
            used = {m.name for o in out for m in o.data.materials if m}
            dname = "AF_Marking" if "AF_Marking" not in used else "AF_MarkingDecal"
            old = bpy.data.materials.get(dname)
            if old is not None:
                old.name = dname + "_flat"
            ctx["decal_mat"] = decal_material(img, dname)
            ctx["decal_cells"] = cells
            ctx["decal_tex"] = cells[0]["tex"] if cells else None
        L = P["length_m"]
        step = 0.0065 * L if lod == 0 else 0.0125 * L
        off = (decals[0]["offset_m"] or 0.00055 * L) * (1 if lod == 0 else 2)
        dec, st = build_decals(out, decals, ctx["decal_cells"], ctx["decal_mat"], L, suffix, step, off)
        ctx.setdefault("decal_faces", {})["lod%d" % lod] = st
        if dec is not None and decal_cap is not None and 2 * len(dec.data.polygons) > decal_cap:
            if warnings is not None:
                warnings.append("lod%d: decals dropped (%d tris > %d left in the LOD budget)"
                                % (lod, 2 * len(dec.data.polygons), max(0, decal_cap)))
            dme = dec.data
            bpy.data.objects.remove(dec, do_unlink=True)
            bpy.data.meshes.remove(dme)
            dec = None
        if dec is not None:
            dec.parent = parent
            out.append(dec)
    return out


# ----------------------------------------------------------------------------- run
def split_mesh(ob, mats, parent, suffix="", collection=None):
    kit = sl.Kit(lod=0, size_ref=10.0)
    kit.bm.from_mesh(ob.data)
    objs = sl.split_by_material(kit, mats, "hull", parent=parent, angle=40.0, suffix=suffix, collection=collection)
    kit.bm.free()
    return objs


def run(req):
    P, warnings = resolve(req)
    cls = P["budget_class"]
    bud = sl.BUDGETS[cls]
    af.reset_scene()
    src = load_joined(req["inputs"]["mesh"])
    tris_in = af.tri_count([src])
    wst = weld(src)
    af.log("welded", **wst)
    img = base_image(src)
    # bright-pixel weights for the nose vote (per triangle via polygon colours)
    cols = sample_face_colors(src, img) if img is not None else None
    bright = None
    if cols is not None and P["emissive"]:
        mx, mn = cols.max(1), cols.min(1)
        sat = np.where(mx > 1e-6, (mx - mn) / np.maximum(mx, 1e-6), 0)
        glow_poly = ((mx > 0.82) & (sat > 0.45)).astype(float)
        me = src.data
        me.calc_loop_triangles()
        tri_poly = np.empty(len(me.loop_triangles), dtype=np.int64)
        me.loop_triangles.foreach_get("polygon_index", tri_poly)
        _, _, _, _, tri_area = mesh_arrays(src)
        bright = glow_poly[tri_poly] * tri_area
    R, oinfo = orient(src, P, bright)
    src.data.transform(R)
    src.data.update()
    V, T, C, N, area = mesh_arrays(src)
    lo, hi = V.min(0), V.max(0)
    s = P["length_m"] / max(hi[1] - lo[1], 1e-9)
    ctr = (lo + hi) / 2
    src.data.transform(Matrix.Scale(s, 4) @ Matrix.Translation(Vector(-ctr)))
    src.data.update()
    # ---- concept painting on the full-resolution mesh (labels survive the decimation as material indices)
    paint_info = None
    if P["materials"] == "paint":
        af.log("painting from the concept", image=os.path.basename(P["paint"]["image"]))
        codes, paint_info = paint_mesh(src, P, P["length_m"], warnings, None)
        me0 = src.data
        C0, _, _ = _face_arrays(me0)
        y0 = float(C0[:, 1].min())
        keymap = {1: "armor", 2: "graphite", 3: "trim", 4: "panel", 5: "glass", 7: "marking"}
        keys = np.array([keymap.get(int(c), "armor") for c in codes], dtype=object)
        glow = codes == 6
        keys[glow & (C0[:, 1] < y0 + 0.22 * P["length_m"])] = "drive"
        keys[glow & ~(C0[:, 1] < y0 + 0.22 * P["length_m"])] = "status"
        me0.materials.clear()
        for key in sl.MATS:
            me0.materials.append(bpy.data.materials.new("af_paint_" + key))
        me0.polygons.foreach_set("material_index", np.array([sl.MI[k] for k in keys], dtype=np.int32))
        me0.update()
        af.log("painted", **{k: v for k, v in paint_info.items() if k in ("silhouette_iou", "label_faces", "sources")})
    # ---- decimate the single welded mesh (no material borders yet)
    target = P["target_tris"]
    if tris_in > target:
        sl.decimate_to([src], target)
    # ---- material labels on the LOD0 topology
    cols = sample_face_colors(src, img) if (img is not None and P["materials"] != "paint") else None
    V, T, C, N, area = mesh_arrays(src)
    me = src.data
    polyC = np.array([p.center[:] for p in me.polygons])
    polyN = np.array([p.normal[:] for p in me.polygons])
    y_min, L = float(V[:, 1].min()), float(V[:, 1].max() - V[:, 1].min())
    mode = P["materials"]
    glow_mask, emit_cols = None, None
    color_source = "base colour texture" if cols is not None else None
    if mode == "classify" and cols is None:
        cols, glow_mask, emit_cols = fallback_face_colors(src)
        color_source = "material / vertex colours" if cols is not None else None
    if mode == "classify" and cols is None:
        warnings.append("materials=classify needs a base colour texture, vertex colours or several materials; "
                        "falling back to single")
        mode = "single"
    if mode == "paint":
        mi0 = np.empty(len(me.polygons), dtype=np.int64)
        me.polygons.foreach_get("material_index", mi0)
        lab = np.array(sl.MATS, dtype=object)[np.clip(mi0, 0, len(sl.MATS) - 1)]
        # the collapse leaves jagged single triangles along label borders: majority filter on LOD0
        lab = smooth_labels(src, lab, iters=P["paint"]["smooth_lod0"])
        color_source = "concept projection (%s)" % os.path.basename(P["paint"]["image"])
    elif mode == "classify":
        lab = classify(cols, polyC, polyN, y_min, L, P["emissive"], glow_mask)
        lab = smooth_labels(src, lab)
    elif mode == "keep_texture":
        lab = np.array(["armor"] * len(me.polygons), dtype=object)
        if cols is not None and P["emissive"]:
            full = classify(cols, polyC, polyN, y_min, L, True)
            lab[np.isin(full, ["drive"])] = "drive"
    else:
        lab = np.array(["armor"] * len(me.polygons), dtype=object)
    if P["decals"]:
        lab[lab == "marking"] = "armor"  # AF_Marking carries the decal atlas
    counts = {k: int((lab == k).sum()) for k in sl.MATS if (lab == k).any()}
    # ---- palette: faction values; with palette 'input' the input's own colours (glow and status included)
    fac = P["faction"] if P["faction"] in sl.FACTIONS else "military"
    pal = sl.palette_for(fac, 0)
    pal_src = {k: "faction %s" % fac for k in sl.MATS}
    if P["palette"] == "input":
        if cols is not None and mode == "classify":
            parea = np.array([p.area for p in me.polygons])
            for k in ("armor", "graphite", "trim", "panel", "marking", "glass", "drive", "status"):
                m = lab == k
                if m.sum() < (3 if k in ("drive", "status") else 20):
                    continue
                src_c = emit_cols if (k in ("drive", "status") and emit_cols is not None) else cols
                mean = (src_c[m] * parea[m, None]).sum(0) / max(parea[m].sum(), 1e-12)
                rgb = _lin(mean)
                if k in ("drive", "status"):
                    g = glow_rgb(rgb)
                    if g is None:
                        continue
                    pal[k]["rgb"] = pal[k]["emit"] = g
                else:
                    pal[k]["rgb"] = rgb
                pal_src[k] = "input"
        elif mode == "paint":
            pc = paint_info["colours_srgb"]
            for name, key in (("armor", "armor"), ("graphite", "graphite"), ("trim", "trim"), ("panel", "panel"),
                              ("glass", "glass")):
                if name in pc:
                    pal[key]["rgb"] = floor_dark(_lin(pc[name]), key)
                    pal_src[key] = "concept"
            g = P["paint"]["glow_gamma"]
            if "glow" in pc and glow_rgb(_lin(pc["glow"]), g):
                for key in ("drive", "status"):
                    pal[key]["rgb"] = pal[key]["emit"] = glow_rgb(_lin(pc["glow"]), g)
                    pal_src[key] = "concept"
        # a glow seen only on the nozzles (or only on running lights) colours both emissive materials
        for a_, b_ in (("drive", "status"), ("status", "drive")):
            if pal_src[a_] in ("input", "concept") and pal_src[b_].startswith("faction"):
                pal[b_]["rgb"] = pal[b_]["emit"] = pal[a_]["emit"]
                pal_src[b_] = pal_src[a_] + " (from %s)" % a_
    pal = apply_livery(pal, P["livery"], warnings)
    for k in P["livery"] or {}:
        pal_src[{"accent": "graphite", "hull": "armor", "lights": "status", "engine": "drive"}.get(k, k)] = "livery"
    mats = sl.make_materials(pal)
    textures = []
    if mode == "keep_texture" and img is not None:
        tmp = img.copy()
        lim = TEX_BUDGET[cls]
        if max(tmp.size) > lim:
            f = lim / float(max(tmp.size))
            tmp.scale(int(tmp.size[0] * f), int(tmp.size[1] * f))
        w2, h2 = tmp.size
        px = np.empty(w2 * h2 * 4, dtype=np.float32)
        tmp.pixels.foreach_get(px)
        bpy.data.images.remove(tmp)
        tex = bpy.data.images.new("AF_Armor_albedo", w2, h2, alpha=False)
        tex.colorspace_settings.name = "sRGB"
        tex.pixels.foreach_set(px)
        tex.file_format = "PNG"
        tex.pack()
        nt = mats["armor"].node_tree
        bsdf = next(n for n in nt.nodes if n.type == "BSDF_PRINCIPLED")
        tn = nt.nodes.new("ShaderNodeTexImage")
        tn.image = tex
        nt.links.new(tn.outputs["Color"], bsdf.inputs["Base Color"])
        textures.append({"map": "albedo", "width": int(tex.size[0]), "height": int(tex.size[1]),
                         "format": "jpeg", "embedded": True})
    # labelled single mesh: slots in MATS order, polygon material_index = label
    src_mats = [m for m in me.materials if m is not None]
    me.materials.clear()
    for key in sl.MATS:
        me.materials.append(mats[key])
    me.polygons.foreach_set("material_index", np.array([sl.MI[x] for x in lab], dtype=np.int32))
    me.update()
    for m in src_mats:  # source materials (and their images) are not used by the asset: purge before save
        if m.users == 0:
            bpy.data.materials.remove(m)
    scene = bpy.context.scene
    root = bpy.data.objects.new(P["asset_id"], None)
    root.empty_display_type = "PLAIN_AXES"
    scene.collection.objects.link(root)
    objs = split_mesh(src, mats, root)
    if P["uv"] == "keep":
        for o in objs:
            if not o.data.uv_layers:
                sl.uv_unwrap([o], "box")
    else:
        sl.uv_unwrap(objs, P["uv"])
    sctx = {}
    objs = finish_surfaces(objs, root, "", 0, P, mats, sctx)
    col, col_info = sl.convex_collision(objs, parent=root)
    # ---- sockets (analysed on the labelled single mesh)
    empties = []
    sym_score = None
    if P["sockets"]:
        V, T, C, N, area = mesh_arrays(src)
        me.calc_loop_triangles()
        tri_poly = np.empty(len(me.loop_triangles), dtype=np.int64)
        me.loop_triangles.foreach_get("polygon_index", tri_poly)
        labs_tri = lab[tri_poly]
        y_min = float(V[:, 1].min())
        L = float(V[:, 1].max() - y_min)
        Ps, reps, ws = area_samples(V, T, area, np.ones(len(T), bool), 60000.0 / max(float(area.sum()), 1e-12),
                                    seed=5)
        sym_score = x_symmetry(Ps, L)
        symmetric = sym_score > 0.7
        # a concept seen from one side paints only part of each nozzle interior (crescents): with paint the
        # nozzles come from the geometry (aft-facing exits) and the painted glow only confirms them
        cue = P["thruster_cue"] if P["thruster_cue"] != "auto" else ("geometry" if mode == "paint" else "glow")
        thr = find_thrusters(V, T, C, N, area, labs_tri if cue == "glow" else None, y_min, L)
        if cue == "geometry":
            drive_c = C[labs_tri == "drive"]
            for o in thr:
                if len(drive_c) and (((drive_c[:, 0] - o["x"]) ** 2 + (drive_c[:, 2] - o["z"]) ** 2 <
                                      (0.6 * o["diam"]) ** 2) & (drive_c[:, 1] < y_min + 0.2 * L)).sum() >= 3:
                    o["conf"] = min(1.0, o["conf"] + 0.2)
                    o["cue"] = "aft_faces+painted_glow"
        if thr and symmetric:
            thr = symmetrize(thr, L)
        elif thr:
            for o in thr:
                o["conf"] *= 0.75
                o["paired"] = False
            warnings.append("mesh is not X-symmetric (score %.2f): thruster sockets not mirrored, confidence "
                            "lowered" % sym_score)
        if not thr:
            warnings.append("no thruster candidates found at the aft end")
        thr.sort(key=lambda o: (round(-o["z"], 2), o["x"]))
        names = ["socket_thruster_main"] if len(thr) == 1 else ["socket_thruster_main_%02d" % (i + 1)
                                                                  for i in range(len(thr))]
        if len(thr) == 2 and abs(thr[0]["z"] - thr[1]["z"]) < 0.3 * thr[0]["diam"] and thr[0]["x"] < 0 < thr[1]["x"]:
            names = ["socket_thruster_main_port", "socket_thruster_main_starboard"]
        fz, fy = sl.THRUSTER_FRAMES[P["thruster_frame"]]
        for nm, o in zip(names, thr):
            e = sl.add_socket(nm, "thruster", (o["x"], o["y"], o["z"]), z_axis=fz, y_hint=fy, parent=root,
                              size=L * 0.02, size_class=sl.size_class_for(o["diam"]), fx="drive",
                              mount_d_m=round(float(o["diam"]), 3),  # nozzle exit diameter estimate (m)
                              confidence=round(o["conf"], 2), heuristic=o["cue"] + (", mirrored" if o.get("paired")
                                                                                    else ""),
                              frame=P["thruster_frame"])
            empties.append(e)
        ring = sl.TURRET_RING[CLASS_TURRET_SIZE[cls]]
        topv = hull_top_analysis(Ps, L)
        guns = gun_protrusions(topv, Ps, L) if P["turret_sockets"] in ("protrusions", "auto") else []
        if symmetric and guns:
            # mirror-merge: one gun per side pair (average |x|, y, z), so the pair stays exact
            merged = []
            for g in sorted(guns, key=lambda g: (g["y"], g["x"])):
                twin = next((m for m in merged if abs(m["y"] - g["y"]) < 0.03 * L and abs(m["z"] - g["z"]) < 0.03 * L
                             and m["x"] * g["x"] < 0), None)
                if twin is None:
                    merged.append(dict(g, seen=1))
                else:
                    twin["seen"] = 2
                    twin["y"] = (twin["y"] + g["y"]) / 2
                    twin["z"] = (twin["z"] + g["z"]) / 2
                    twin["absx"] = (abs(twin["x"]) + abs(g["x"])) / 2
            guns = []
            for m in merged:
                ax_ = abs(m.get("absx", m["x"]))
                for sgn in (-1, 1):
                    a = list(m["axis"])
                    a[0] = abs(a[0]) * sgn if abs(a[0]) > 0.5 else a[0]
                    guns.append(dict(m, x=sgn * ax_, axis=a))
        use_guns = P["turret_sockets"] == "protrusions" or (P["turret_sockets"] == "auto" and len(guns) >= 2)
        if P["turret_sockets"] == "protrusions" and not guns:
            warnings.append("turret_sockets=protrusions: no barrel-like protrusions found; flat patches used")
            use_guns = False
        used = set()
        if use_guns:
            guns = sorted(guns, key=lambda g: (-g["y"], g["x"]))[:max(P["turret_count"], 0) or len(guns)]
            for g in guns:
                side = "port" if g["x"] < 0 else "starboard"
                zone = "forward" if g["y"] > 0 else "aft"
                tag = "%s_%s" % (side, zone)
                k, t0 = 2, tag
                while tag in used:
                    tag = "%s_%02d" % (t0, k)
                    k += 1
                used.add(tag)
                dia = float(min(max(2.0 * g["width_m"], 0.6 * ring), 1.25 * ring))
                e = sl.add_socket("socket_turret_" + tag, "turret", (g["x"], g["y"], g["z"]), z_axis=tuple(g["axis"]),
                                  parent=root, size=L * 0.02, size_class=sl.size_class_for(dia), mount_d_m=round(dia, 3),
                                  confidence=0.85 if g.get("seen", 1) == 2 or not symmetric else 0.6,
                                  heuristic="barrel-like protrusion (len %.1f m) at the hull silhouette edge, axis "
                                            "outward" % g["length_m"], barrel_length_m=g["length_m"])
                empties.append(e)
            flats = []
        else:
            flats = find_flats(V, T, N, area, L, ring, P["turret_count"] * 2, 1.0, P=Ps, rep=reps, w=ws)
            flats = pair_flats(flats, P["turret_count"], ring, symmetric)
            if len(flats) < P["turret_count"]:
                more = find_flats(V, T, N, area, L, ring, (P["turret_count"] - len(flats)) * 2, -1.0, P=Ps, rep=reps,
                                  w=ws)
                flats += pair_flats(more, P["turret_count"] - len(flats), ring, symmetric)
        for o in flats:
            base = "dorsal" if o["z"] > 0 else "ventral"
            tag = socket_tag(o["x"], o["y"], L, base)
            k, t0 = 2, tag
            while tag in used:
                tag = "%s_%02d" % (t0, k)
                k += 1
            used.add(tag)
            nz = (0, 0, 1) if o["z"] > 0 else (0, 0, -1)
            e = sl.add_socket("socket_turret_" + tag, "turret", (o["x"], o["y"], o["z"]), z_axis=nz, parent=root,
                              size=L * 0.02, size_class=CLASS_TURRET_SIZE[cls], mount_d_m=ring,
                              confidence=round(o["conf"], 2), heuristic="dorsal_flat" if o["z"] > 0 else "ventral_flat")
            empties.append(e)
        if not use_guns and len(flats) < P["turret_count"]:
            warnings.append("turret sockets: %d of %d found on flat areas" % (len(flats), P["turret_count"]))
        # nav lights on the main hull body: widest point of the opened top-view mask (thin protrusions such as
        # gun barrels are not hull), the most forward one; mirrored on a symmetric mesh
        navp = nav_points_on_hull(topv, Ps, symmetric)
        if navp is not None:
            port, stbd = navp[-1], navp[1]
            heur = "widest main-hull point (top view opened by %.2f m), most forward%s" % (
                topv["open_m"], ", mirrored" if symmetric else "")
        else:
            port = V[int(np.argmin(V[:, 0]))]
            stbd = np.array([-port[0], port[1], port[2]]) if symmetric else V[int(np.argmax(V[:, 0]))]
            heur = "min_x_vertex (hull analysis failed)"
        e1 = sl.add_socket("socket_fx_nav_port", "fx", tuple(port), z_axis=(-1, 0, 0), parent=root, size=L * 0.015,
                           fx="nav_light", side="port", color="red", confidence=0.75 if navp is not None else 0.4,
                           heuristic=heur)
        e2 = sl.add_socket("socket_fx_nav_starboard", "fx", tuple(stbd), z_axis=(1, 0, 0),
                           parent=root, size=L * 0.015, fx="nav_light", side="starboard", color="green",
                           confidence=0.75 if navp is not None else 0.4, heuristic=heur)
        top = V[int(np.argmax(V[:, 2]))]
        e3 = sl.add_socket("socket_utility_antenna", "utility", tuple(top), parent=root, size=L * 0.015,
                           confidence=0.5, heuristic="max_z_vertex")
        belly = C[(N[:, 2] < -0.85) & (np.abs(C[:, 1]) < 0.25 * L)]
        if len(belly):
            bp = belly[np.argmin(np.abs(belly[:, 0]) + np.abs(belly[:, 1]) * 0.2)]
            dconf = 0.6
        else:
            bp = np.array([0.0, 0.0, V[:, 2].min()])
            dconf = 0.3
        e4 = sl.add_socket("socket_docking_ventral", "docking", tuple(bp), z_axis=(0, 0, -1), parent=root,
                           size=L * 0.015, confidence=dconf, heuristic="belly_flat")
        empties += [e1, e2, e3, e4]
        limit = bud["nodes"] - 1 - len(objs) - 1
        if len(empties) > limit:
            drop = empties[limit:]
            warnings.append("node budget: dropped %s" % ", ".join(e.name for e in drop))
            for e in drop:
                bpy.data.objects.remove(e, do_unlink=True)
            empties = empties[:limit]
    bpy.context.view_layer.update()
    out_glb = af.out_path(req, "ship.glb")
    img_fmt = "JPEG" if textures else "AUTO"  # albedo may be JPEG (budget MB), normal maps PNG only
    af.export_glb(out_glb, [root] + objs + [col] + empties, animations=False, skins=False, export_tangents=False,
                  export_image_format=img_fmt, export_jpeg_quality=88, export_draco_mesh_compression_enable=False)
    summ = sl.glb_summary(out_glb)
    outputs = {"glb": out_glb}
    lod_tris, lod_sets, lod_paths, lod_dec = {}, [objs], {}, {}
    if P["lods"]:
        for lod in (1, 2):
            frac = 0.35 if lod == 1 else 0.12
            tgt = int(min(bud["lod%d" % lod], frac * summ["triangles"]) * 0.95)
            if P["lod_targets"].get("lod%d" % lod):
                tgt = min(tgt, P["lod_targets"]["lod%d" % lod])
            c2 = src.copy()
            c2.data = src.data.copy()
            scene.collection.objects.link(c2)
            dlog = []
            got = sl.decimate_to([c2], tgt, log=dlog)
            dinfo = {"target": tgt, "tris": got, "method": "collapse", "passes": dlog}
            if got > tgt:
                # collapse stalled (edges with 3+ faces on AI meshes): closed voxel proxy of LOD0 instead,
                # if it reaches the budget with the silhouette gate (gate 10) intact
                dinfo["collapse_silhouette_vs_lod0"] = sl.mesh_silhouette_iou([src], [c2])
                prox, pinfo = sl.voxel_proxy_lod(src, tgt, log=dlog)
                if prox is not None and pinfo["tris"] <= tgt and min(pinfo["silhouette_vs_src"].values()) >= 0.85:
                    cme = c2.data
                    bpy.data.objects.remove(c2, do_unlink=True)
                    bpy.data.meshes.remove(cme)
                    warnings.append("lod%d: collapse stopped at %d tris (target %d, edges with 3+ faces); "
                                    "voxel proxy (%.3f m) used" % (lod, got, tgt, pinfo["voxel_m"]))
                    c2, got = prox, pinfo["tris"]
                    dinfo.update(tris=got, method="voxel_proxy", voxel_m=pinfo["voxel_m"],
                                 silhouette_vs_lod0=pinfo["silhouette_vs_src"])
                else:
                    if prox is not None:
                        pme = prox.data
                        bpy.data.objects.remove(prox, do_unlink=True)
                        bpy.data.meshes.remove(pme)
                    warnings.append("lod%d: decimation stopped at %d tris (target %d); voxel proxy did not keep "
                                    "the budget and silhouette" % (lod, got, tgt))
            lod_dec["lod%d" % lod] = dinfo
            lroot = bpy.data.objects.new("%s_lod%d" % (P["asset_id"], lod), None)
            scene.collection.objects.link(lroot)
            lobjs = split_mesh(c2, mats, lroot, suffix="_lod%d" % lod)
            cme = c2.data
            bpy.data.objects.remove(c2, do_unlink=True)
            bpy.data.meshes.remove(cme)
            for o in lobjs:
                if not o.data.uv_layers:
                    sl.uv_unwrap([o], "box")
            cap = int(min(bud["lod%d" % lod], (0.35 if lod == 1 else 0.12) * summ["triangles"])) - sl.mesh_tris(lobjs)
            lobjs = finish_surfaces(lobjs, lroot, "_lod%d" % lod, lod, P, mats, sctx, decal_cap=cap, warnings=warnings)
            path = af.out_path(req, "ship_lod%d.glb" % lod)
            af.export_glb(path, [lroot] + lobjs, animations=False, skins=False, export_tangents=False,
                          export_image_format=img_fmt, export_jpeg_quality=85,
                          export_draco_mesh_compression_enable=False)
            lod_tris["lod%d" % lod] = sl.glb_summary(path)["triangles"]
            outputs["lod%d" % lod] = path
            lod_paths["lod%d" % lod] = path
            lod_sets.append(lobjs)
            for o in lobjs:
                o.hide_render = True
    src_me = src.data
    bpy.data.objects.remove(src, do_unlink=True)
    if src_me.users == 0:
        bpy.data.meshes.remove(src_me)
    checks = sl.budget_checks(summ, cls, lod_tris if P["lods"] else None)
    checks.append(af.check("collision_vertices", col_info["vertices"] <= 64, col_info["vertices"], 64))
    checks.append(af.check("length_m", abs(summ["size_gltf"][2] - P["length_m"]) <= 0.03 * P["length_m"],
                           summ["size_gltf"][2], P["length_m"]))
    conf = oinfo.get("nose_confidence", 0)
    checks.append(af.check("nose_confidence", conf >= 0.3, conf, 0.3, severity="warning",
                           note="low confidence: set params.nose explicitly"))
    thr_s = [x for x in summ["sockets"] if x["extras"].get("kind") == "thruster"]
    checks.append(af.check("thrusters_aft", all(x["position_gltf"][2] > 0 for x in thr_s) and bool(thr_s),
                           len(thr_s), ">=1 at +Z glTF (aft)", severity="warning"))
    checks.append(af.check("thruster_frame", all(sl.thruster_frame_ok(x, P["thruster_frame"]) for x in thr_s),
                           len(thr_s), sl.THRUSTER_AXIS_TEXT[P["thruster_frame"]]))
    checks.append(sl.thruster_diameter_check(thr_s))
    checks.append(ship_gen.nav_checks(summ))
    lod_sil = sl.lod_silhouettes(out_glb, lod_paths) if lod_paths else {}
    for key, v in lod_sil.items():
        # error for real-size meshes; a tiny input (< 3000 tris) cannot keep its silhouette at 12 %
        checks.append(af.check("silhouette_iou_%s" % key, min(v["top"], v["side"]) >= 0.85, v, 0.85,
                               severity="error" if summ["triangles"] >= 3000 else "warning",
                               note="gate 10: top/side silhouette IoU vs LOD0 (same window)"))
    Vx = np.concatenate([np.array([o.matrix_world @ v.co for v in o.data.vertices]) for o in objs])
    asym = float(abs(Vx[:, 0].max() + Vx[:, 0].min()) / max(Vx[:, 0].max() - Vx[:, 0].min(), 1e-9))
    checks.append(af.check("mirror_x_bbox_asymmetry", asym < 0.05, round(asym, 4), 0.05, severity="warning"))
    textured = bool(textures) or bool(P["armor_texture"]) or bool(P["decals"])
    if P["preview"]:
        col.hide_render = True
        markers = socket_markers(empties, mats, P["length_m"]) if P["preview_sockets"] else []
        outputs["preview"] = sl.render_grid(af.out_path(req, "preview.png"), objs + markers, tile=P["preview_size"],
                                            views=("three_quarter_long", "rear_quarter_long", "side", "top", "front"),
                                            color_type="TEXTURE" if textured else "MATERIAL")
        if P["preview_lods"] and len(lod_sets) > 1:
            outputs["lod_strip"] = sl.lod_strip(af.out_path(req, "lod_strip.png"), lod_sets,
                                                tile=P["preview_size"],
                                                color_type="TEXTURE" if textured else "MATERIAL")
        for o in markers:
            me_ = o.data
            bpy.data.objects.remove(o)
            bpy.data.meshes.remove(me_)
    if paint_info is not None:
        cam_path = sl.write_json(af.out_path(req, "paint_camera.json"), paint_info["camera"])
        outputs["paint_camera"] = cam_path
        try:
            pimg = sl.load_image(P["paint"]["image"])
            pmask = (sl.load_image(P["paint"]["mask"])[..., :3].mean(-1) > 0.5) if P["paint"]["mask"] else \
                pimg[..., 3] > 0.5
            outputs["paint_overlay"], paint_info["silhouette_iou_lod0"] = write_paint_overlay(
                af.out_path(req, "paint_overlay.png"), sl.largest_component(pmask, 0.02), paint_info["camera"],
                [o for o in objs if not o.name.startswith("M_hull_marking")])
        except Exception as exc:  # the overlay is a QA picture only
            warnings.append("paint overlay not written: %s" % exc)
    if P["armor_texture"]:
        kt = load_kits()["textures"]
        for name in [P["armor_texture"]["texture"]] + [r["texture"] for r in P["armor_texture"]["regions"]]:
            textures.append({"map": "albedo", "width": kt[name]["albedo_size"][0], "height": kt[name]["albedo_size"][1],
                             "format": "jpeg", "embedded": True})
            textures.append({"map": "normal", "width": kt[name]["normal_size"][0], "height": kt[name]["normal_size"][1],
                             "format": "png", "embedded": True})
    if sctx.get("decal_tex"):
        textures.append({"map": "albedo", "width": sctx["decal_tex"][0], "height": sctx["decal_tex"][1],
                         "format": "png", "embedded": True})
    Pm = dict(P, faction=P["faction"], seed=P["seed"])
    defn = dict(P, generator_version=sl.GEN_FROM_MESH, input=req["inputs"]["mesh"], orientation=oinfo, weld=wst,
                palette_source=pal_src)
    if paint_info is not None:
        defn["paint_result"] = {k: v for k, v in paint_info.items() if k != "fit"}
    outputs["definition"] = sl.write_json(af.out_path(req, "ship.definition.json"), defn)
    outputs.setdefault("blend", af.out_path(req, "ship.blend"))
    manifest = ship_gen.build_manifest(Pm, summ, lod_tris, col, col_info, empties, outputs, req,
                                       source=P["source"], generator="%s + %s" % (P["generator"], sl.GEN_FROM_MESH))
    manifest["textures"] = textures
    manifest["notes"] = ("ship_from_mesh: input %s (sha1 %s), orientation %s (confidence %.2f), materials=%s%s; "
                         "sockets are heuristic (extras.confidence)"
                         % (os.path.basename(req["inputs"]["mesh"]), P["input_sha1_6"], oinfo.get("nose_from"),
                            conf, mode, (", concept silhouette IoU %.3f" % paint_info["silhouette_iou"])
                            if paint_info else ""))
    outputs["manifest"] = sl.write_json(af.out_path(req, "ship.json"), manifest)
    if paint_info is not None:
        checks.append(af.check("paint_silhouette_iou", paint_info["silhouette_iou"] >= 0.8,
                               paint_info["silhouette_iou"], 0.8, severity="warning",
                               note="concept mask vs the mesh seen from the paint camera"))
    # forge metadata inside the .blend (kitbash and later steps read it instead of guessing)
    meta = {"step": "ship_from_mesh", "asset_id": P["asset_id"], "class": P["class"], "budget_class": cls,
            "length_m": P["length_m"], "thruster_frame": P["thruster_frame"], "manifest": manifest,
            "textures": textures, "lod_roots": ["%s_lod1" % P["asset_id"], "%s_lod2" % P["asset_id"]] if P["lods"]
            else [], "collision": col.name, "lod_targets": P["lod_targets"], "palette_source": pal_src}
    bpy.context.scene["af_ship"] = json.dumps(af._jsonable(meta), ensure_ascii=False)
    outputs["blend"] = sl.save_blend_clean(af.out_path(req, "ship.blend"))
    stats = {"asset_id": P["asset_id"], "class": P["class"], "budget_class": cls, "input_tris": tris_in,
             "weld": wst, "tris": summ["triangles"], "tris_lod1": lod_tris.get("lod1"),
             "tris_lod2": lod_tris.get("lod2"), "lod_decimation": lod_dec, "lod_silhouette_iou": lod_sil,
             "surfaces": summ["surfaces_lod0"], "materials": summ["material_names_used"], "nodes": summ["nodes"],
             "glb_mb": summ["mb"], "blend_mb": round(os.path.getsize(outputs["blend"]) / 1048576.0, 2),
             "orientation": oinfo, "material_mode": mode, "face_labels": counts, "color_source": color_source,
             "x_symmetry": None if sym_score is None else round(sym_score, 3),
             "source": P["source"], "generator": P["generator"],
             "sockets": [{"name": x["game_socket"], "confidence": x["extras"].get("confidence"),
                          "heuristic": x["extras"].get("heuristic"), "position_gltf": x["position_gltf"]}
                         for x in summ["sockets"]],
             "collision": col_info, "mirror_asymmetry": round(asym, 4), "dimensions_m": manifest["dimensions_m"],
             "palette_source": pal_src,
             "palette": {k: [round(float(c), 4) for c in pal[k].get("emit") or pal[k]["rgb"]] for k in sl.MATS},
             "decal_faces": sctx.get("decal_faces"),
             "paint": None if paint_info is None else {k: v for k, v in paint_info.items() if k != "fit"}}
    return {"outputs": outputs, "stats": stats, "checks": checks, "warnings": warnings}


def socket_markers(empties, mats, L):
    """Preview-only markers: turret = disc (marking colour), thruster = cone along -Y (drive), others = cube."""
    kit = sl.Kit(lod=0, size_ref=L)
    for e in empties:
        p = e.matrix_world.translation
        kind = e.get("kind")
        z = e.matrix_world.to_3x3() @ Vector((0, 0, 1))
        if kind == "turret":
            D = float(e.get("mount_d_m", 2.4))
            kit.cyl(p + z * D * 0.08, D * 0.5, D * 0.16, axis=z, mat="marking", segs=20)
            kit.cyl(p + z * D * 0.35, D * 0.08, D * 0.5, axis=z, mat="marking", segs=8)
        elif kind == "thruster":
            r = L * 0.012
            kit.cyl(p + Vector((0, -L * 0.03, 0)), r * 1.6, L * 0.06, axis=(0, -1, 0), mat="drive", r2=r * 0.2, segs=12)
        else:
            s = L * 0.008
            kit.box(p, (s, s, s), "status")
    objs = sl.split_by_material(kit, mats, "pv_marker")
    kit.bm.free()
    return objs
