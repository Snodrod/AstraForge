"""paint_from_concept - colour a geometry-only mesh (Hunyuan3D 2.1 output) from its concept image.

inputs.mesh (.glb/.gltf/.fbx/.obj/.blend) or inputs.blend (opened by af_run; every object stays),
inputs.concept: the concept image, RGBA with alpha = subject (the studio's prepared reference PNG:
original RGB + rembg alpha; its colour x alpha storage is divided back); an image without transparency
is keyed by its border colour. Optional inputs.concept_original = {image, crop, content_rect}: the
original upload and the reference framing - its straight RGB replaces the reference RGB, so a banner
rembg dropped to a faint alpha keeps its colour (the server fills it for assets made from a generation).
Optional inputs.concept_back (or params.concept_back_file): the reference seen from behind, same
conventions - the back of the model is then painted from it instead of guessed from the front.

Algorithm (docs/forge/paint_from_concept.md):
  1. concept masks: alpha > 0.5 for the camera fit; for colour also faint but clearly coloured pixels
     (saturated, far from the background colour; desaturated floor shadows stay out). Light
     de-lighting (large-scale luminance towards the median, factor 0.85..1.2).
  2. orthographic camera fitted to the silhouette: yaw around the model front (-Y; Hunyuan turns a
     3/4 concept to face front, so the knight's camera sits at yaw -23 deg) and pitch, 2D similarity
     (scale + offset); coarse grid (near-ties go to the smaller yaw: front / back silhouettes mirror
     each other), then Nelder-Mead on the IoU (area-sampled surface points splatted at 200 and 400 px).
     The IoU at full resolution comes from an exact z-buffer that also labels the connected piece
     (welded by position) in front of every pixel.
  2b. back reference: a second camera fitted around yaw 180 to the back silhouette, its own z-buffer.
  2c. align "warp": the camera is rigid, the generated mesh is not the concept (arm angle, helmet size,
     sleeve length). The concept (colour + alpha + masks) is bent by a regularised thin-plate spline so its
     silhouette lies on the model's silhouette through the camera (ICP-style boundary matching, Huber
     weights, displacement capped by align_max x subject height); everything below then runs on the warped
     concept. Same for the back reference against the model's back silhouette.
  3. UVs: kept when usable (one layer, unique, inside 0..1), else Smart UV project + AABB repack.
  4. texels of the UV raster (default 2048): world position and corner normal by barycentrics;
     a texel is VISIBLE when it faces the camera (facing > facing_min), passes the z-buffer depth test
     (tolerance depth_tolerance x height + slope term) and lands inside the concept's colour mask.
     Visible texels sample the concept bilinearly with weight smoothstep(facing_min, facing_full)
     x a fade over the outer edge_fade_px of the subject (no grey rembg halo, no smeared stripes on
     grazing faces). With a back reference the same weight w_b is computed for the back camera and
     colour = w_f x front + w_b (1 - w_f) x back + (1 - w_f)(1 - w_b) x fill: only texels that neither
     camera sees are guessed. Everything else takes the FILL (blurred concept colours, per vertex,
     smoothed over the welded mesh graph and interpolated across triangles), chosen in this order:
       - occluded FRONT faces whose mirror twin (across the body's symmetry plane) is visible take the
         twin's colour: the body behind a shield;
       - other hidden faces take the concept straight in front of them when that pixel shows the same
         connected piece (the back sees the front through the body; per texel, not per vertex) - except
         the back of the head (top of the body column), which must not get the face;
       - else the through-projection of the mirror point (the back behind a shield);
       - else the mirror twin, else the nearest visible vertex of the same piece (sword, shield, cape keep
         their own colours), else the nearest visible vertex.
     Texture borders: 4-px dilation, then pull-push fill (no seams, clean mip maps).
  4b. flat: every triangle gets ONE colour - the weighted median of its texels (confident samples only when
     there are any), optionally snapped to a k-means palette of the concept's subject pixels.
  5. emission: glowing spots that stand out from their neighbourhood in saturation AND luminance with a
     cool hue (eyes, crystals, runes); gold, skin and red cloth never glow, warm hues only white-hot ->
     emission map x emission_strength on the visible texels.
  6. Principled material (base colour + optional emission, roughness / metallic constants), glTF
     exportable, images packed into the .blend; sockets, other objects and emissive meshes (e.g.
     replace_part eyes) are kept.
  7. QA (always): qa_sheet.png (concept front | model front | concept back | model back | 3/4 left |
     3/4 right | side, flat light), numeric metrics, and - when a local Ollama vision model is installed and
     the GPU is free - its verdict (qa.json).

params (all optional):
  size 2048               texture side (512 / 1024 / 2048 / 4096)
  uv "auto"               auto (keep usable UVs, else Smart UV) | keep | smart
  uv_pack "aabb"          repack after Smart UV: aabb (~37 % of the square, ms) | concave (~45 %, ~10 s) | none
  uv_angle_deg 66, uv_margin 0.003
  camera "fit"            "fit" or {yaw_deg, pitch_deg, px_per_m, offset_px: [x, y]} in concept pixels
                          (paint_camera.json of an earlier run)
  yaw_range_deg null      +- search around the model front (-Y); null: 60, creatures (asset kind) 180
  yaw_step_deg 5, pitch_range_deg 15
  perspective true        last fit stage adds the camera distance (kept when the IoU gains >= 0.005)
  concept_back_file null  back reference (CLI; the server passes inputs.concept_back)
  camera_back "fit"       "fit" or an explicit camera dict (paint_camera_back.json)
  back_yaw_deg 180, back_yaw_range_deg 15, back_pitch_range_deg 6     back camera search
  align "none"            none | warp: non-rigid silhouette alignment of the concept(s) to the model
  align_max 0.06          largest displacement, fraction of the subject height
  align_smooth 2.0        thin-plate stiffness (larger = smoother, 0.3 follows every noisy contour bump)
  align_iters 4, align_points 450
  flat false              one colour per triangle (robust median of its texels)
  flat_palette 0          with flat: snap the triangle colours to this many k-means colours (0 = off)
  facing_min 0.1, facing_full 0.4      grazing faces fade from fill (<= min) to concept (>= full)
  depth_tolerance 0.004   z-buffer tolerance, fraction of the model height (+ slope term)
  edge_fade_px null       fade band at the subject edge, concept px (null: 0.4 % of the subject height, >= 2)
  delight 0.25            0 = off
  fill_blur 0.015         blur of the concept colours used for the fill, fraction of the subject height
  fill_smooth 10          smoothing passes of the per-vertex fill over the welded mesh graph
  back_fill "through"     through | none (nearest visible vertex only)
  head_guard null         back of the head without the face; null: on except for creatures
  head_fraction 0.13      head band below the top of the body column, fraction of the height
  mirror "auto"           auto (shoulder-band symmetry plane) | x (plane X = median) | none
  mirror_tolerance 0.03   the mirror point must lie this close (fraction of the height) to a visible vertex
  emission "auto"         auto | none
  emission_strength 3.0   Principled emission strength (glTF KHR_materials_emissive_strength)
  roughness 0.75, metallic 0.0
  objects []              names of meshes to paint (default: every mesh except emissive-only ones)
  material_name null      default <object>_painted
  concept_max_px 2048     larger concepts are scaled down first (the reference PNG may be up to 4096 px)
  preview true, preview_size 384
  qa_vlm "auto"           auto | off: local vision-model review of qa_sheet.png (Ollama); skipped (warning
                          qa_vlm_unavailable) when Ollama / the model is missing or ComfyUI is busy
  qa_model null           default env ASTRA_QA_VLM or "qwen3-vl:8b"
  qa_url null             default env ASTRA_OLLAMA_URL or http://127.0.0.1:11434
  qa_min_score 6          warning texture_qa below this score (0..10)
  qa_free_comfy true      POST /free to ComfyUI (ASTRA_COMFY_URL, default http://127.0.0.1:8190) first
  qa_wait_comfy_s 0       wait up to this long for a busy ComfyUI queue before skipping the review
  qa_size 384, qa_timeout_s 300
outputs: glb (painted.glb), blend (painted.blend, images packed), basecolor (paint_basecolor.png),
  emission (paint_emission.png when glow was found), compare (compare.png: concept | front through the
  fitted camera | 3/4 | back, Workbench flat light + cavity; second row: silhouette overlap (green both,
  red concept only, blue model only) and the same views with the fill tinted: cyan = through-projection,
  magenta = mirror / nearest guess, green tint = from the back reference; warp row (aligned concept |
  overlap after the warp | displacement heat map) and back row with a back reference), camera
  (paint_camera.json), camera_back (paint_camera_back.json), qa_sheet (qa_sheet.png), qa (qa.json).
stats: camera, iou_fit, iou, texels (covered, projected, blended, filled, filled_through + shares, back_*),
  fill (core vertices, mirror / through / mirror_through / same_piece / global shares, head band), emission,
  color_mae (flat render through the fitted camera vs the concept, on the common silhouette),
  resolution (mm per concept px vs mm per texel), mask (premultiplied input, original RGB), align, back,
  flat, qa (metrics, vlm, problems), timings_s.
checks: concept_iou_min (error, IoU >= 0.5: wrong concept or broken mesh), concept_back_iou_min (error, back
  reference only), glb_base_color_texture (error), concept_iou (warning >= 0.8), projected_share (warning
  >= 0.08), color_mae (warning <= 0.1), texture_qa (warning: vision-model score or metric thresholds).
"""
import json
import math
import os
import time

import bpy
import numpy as np

import af_common as af
from steps import _mesh_lib as ml
from steps import _paint_align as al
from steps import _paint_flat as fl
from steps import _paint_lib as pl
from steps import _paint_qa as qa

MAGENTA = np.array([1.0, 0.0, 1.0], dtype=np.float32)
CYAN = np.array([0.0, 0.9, 1.0], dtype=np.float32)
GREEN = np.array([0.1, 0.9, 0.2], dtype=np.float32)


def _open_input(req, warnings):
    inputs = req.get("inputs") or {}
    if inputs.get("mesh"):
        src = os.path.abspath(str(inputs["mesh"]))
        if not os.path.isfile(src):
            raise FileNotFoundError("inputs.mesh not found: %s" % src)
        if src.lower().endswith(".blend"):
            af.open_blend(src)
        else:
            af.reset_scene()
            af.import_any(src)
        return src
    blend = inputs.get("blend")
    if not blend:
        raise ValueError("inputs.mesh or inputs.blend is required")
    blend = os.path.abspath(str(blend))
    if not os.path.isfile(blend):
        raise FileNotFoundError("inputs.blend not found: %s" % blend)
    if os.path.normcase(os.path.abspath(bpy.data.filepath or "")) != os.path.normcase(blend):
        af.open_blend(blend)   # af_run opens it already; direct calls (tests) do not
    return blend


def _targets(meshes, names, warnings):
    if names:
        sel = [o for o in meshes if o.name in set(names)]
        missing = sorted(set(names) - {o.name for o in sel})
        if missing:
            warnings.append("objects not found: " + ", ".join(missing))
        return sel
    out = []
    for o in meshes:
        mats = [m for m in o.data.materials if m is not None]
        if mats and all(pl.material_is_emissive(m) for m in mats):
            continue   # glowing eyes and the like keep their own material
        out.append(o)
    return out


# ---------------------------------------------------------------------- concept views
def _load_view(path, orig, max_px, warnings, label):
    """One reference image -> dict(px straight RGBA top first, fit / col masks, mask info, subject height)."""
    px = pl.load_rgba(path)
    Hc, Wc = px.shape[:2]
    px, premult = pl.unpremultiply(px)
    merged = False
    if isinstance(orig, dict) and orig.get("image") and orig.get("crop") and orig.get("content_rect"):
        if os.path.isfile(str(orig["image"])):
            px, merged = pl.merge_original(px, pl.load_rgba(str(orig["image"])), orig["crop"], orig["content_rect"])
        else:
            warnings.append("inputs.%s_original.image not found: %s" % (label, orig["image"]))
    scaled_from = None
    if max(Hc, Wc) > max_px > 0:   # 4096 px references: same detail per texel, 4x less work
        f = max_px / float(max(Hc, Wc))
        scaled_from = [Wc, Hc]
        px = pl.resize_area(px, max(1, int(round(Wc * f))), max(1, int(round(Hc * f))))
        Hc, Wc = px.shape[:2]
    fit_mask, col_mask, mask_info = pl.concept_masks(px)
    mask_info.update({"premultiplied_input": premult, "original_rgb": merged, "scaled_from": scaled_from})
    rows = np.nonzero(fit_mask.any(axis=1))[0]
    return {"path": path, "px": px, "H": Hc, "W": Wc, "fit": fit_mask, "col": col_mask, "mask_info": mask_info,
            "subject_h": float(rows.max() - rows.min() + 1)}


def _derive(px, fit_mask, col_mask, p, with_emission):
    """Everything the projection samples, from the (possibly warped) concept: edge distance, de-lit colours,
    padded colours for direct sampling and blurred colours for the fill, emission map."""
    rows = np.nonzero(fit_mask.any(axis=1))[0]
    subject_h = float(rows.max() - rows.min() + 1)
    fade_k = p.get("edge_fade_px")
    fade_k = max(2, int(round(0.004 * subject_h))) if fade_k is None else max(0, int(fade_k))
    edge_d = pl.edge_distance(col_mask, fade_k + 1)
    rgb0 = px[..., :3].copy()
    rgb_d, delight_info = pl.delight(rgb0, col_mask, float(p.get("delight", 0.25)), 0.12 * subject_h)
    rgb_s, _ = pl.dilate_colors(rgb_d, col_mask, 4)
    blur_r = max(1, int(round(float(p.get("fill_blur", 0.015)) * subject_h)))
    rgb_b, _ = pl.dilate_colors(pl.masked_blur(rgb_d, col_mask, blur_r), col_mask, 4)
    d = {"subject_h": subject_h, "fade_k": fade_k, "edge_d": edge_d, "rgb_d": rgb_d, "rgb_s": rgb_s, "rgb_b": rgb_b,
         "delight_info": delight_info, "blur_r": blur_r, "col": col_mask, "fit": fit_mask, "emi_img": None,
         "emi_info": {"mode": "none"}}
    if with_emission:
        emi_mode = str(p.get("emission", "auto")).lower()
        emi_info = {"mode": emi_mode}
        emi_img = None
        if emi_mode not in ("none", "off", "false"):
            emi_mask, info = pl.emission_mask(rgb0, fit_mask, subject_h)
            emi_info.update(info)
            if emi_mask.any():
                lin = pl.srgb_to_linear(rgb0)
                peak = np.maximum(lin.max(axis=2, keepdims=True), 1e-4)
                glow = pl.linear_to_srgb(np.clip(lin / peak, 0, 1))      # hue kept, full brightness
                emi_img = np.where(emi_mask[..., None], glow, 0.0).astype(np.float32)
                ys, xs = np.nonzero(emi_mask)
                emi_info["mean_color_srgb"] = [round(float(c), 3) for c in glow[emi_mask].mean(axis=0)]
                emi_info["bbox_px"] = [int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max())]
        d["emi_img"], d["emi_info"] = emi_img, emi_info
    return d


class _View:
    """One reference camera: z-buffer + the concept's colour mask -> per world point visibility and weight."""

    def __init__(self, cam, zb, derived, tol0, f_min, f_full):
        self.cam, self.zb = cam, zb
        self.col_mask, self.edge_d, self.fade_k = derived["col"], derived["edge_d"], derived["fade_k"]
        self.H, self.W = self.col_mask.shape
        self.px_m = 1.0 / cam.s
        self.tol0, self.f_min, self.f_full = tol0, f_min, f_full

    def __call__(self, P, N):
        """-> (u, v, visible bool, weight, facing) for world points with unit normals."""
        u, v, z = self.cam.project(P)
        facing = (N * self.cam.toward(P)).sum(axis=1).astype(np.float32)
        ins = (u >= 0) & (u < self.W) & (v >= 0) & (v < self.H)
        j = np.clip(np.floor(u).astype(np.int64), 0, self.W - 1)
        i = np.clip(np.floor(v).astype(np.int64), 0, self.H - 1)
        tan = np.sqrt(np.clip(1 - facing ** 2, 0, 1)) / np.maximum(facing, 0.15)
        tol = self.tol0 + 0.75 * self.px_m * np.minimum(tan, 6.0)
        vis = ins & (facing > self.f_min) & (z <= self.zb[i, j] + tol) & self.col_mask[i, j]
        w_edge = np.clip((self.edge_d[i, j] - 1.0) / max(self.fade_k, 1), 0, 1) if self.fade_k > 0 else 1.0
        w = np.where(vis, pl.smoothstep(self.f_min, self.f_full, facing) * w_edge, 0.0).astype(np.float32)
        return u, v, vis, w, facing


def _bool_param(v, default=False):
    if v is None:
        return default
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "yes", "on")
    return bool(v)


def run(req):
    p = req["params"]
    warnings = []
    timings = {}
    t_all = time.time()
    t0 = time.time()

    def lap(name):
        nonlocal t0
        timings[name] = round(time.time() - t0, 2)
        af.log(name, seconds=timings[name])
        t0 = time.time()

    inputs = req.get("inputs") or {}
    concept_path = inputs.get("concept")
    if not concept_path:
        raise ValueError("inputs.concept is required (the concept image the mesh was generated from)")
    concept_path = os.path.abspath(str(concept_path))
    if not os.path.isfile(concept_path):
        raise FileNotFoundError("inputs.concept not found: %s" % concept_path)
    back_path = inputs.get("concept_back") or p.get("concept_back_file")
    if back_path:
        back_path = os.path.abspath(str(back_path))
        if not os.path.isfile(back_path):
            raise FileNotFoundError("inputs.concept_back not found: %s" % back_path)
    align_mode = str(p.get("align", "none")).lower()
    if align_mode not in ("none", "warp"):
        raise ValueError("align must be 'none' or 'warp', got %r" % p.get("align"))
    flat_mode = _bool_param(p.get("flat"), False)
    flat_palette = max(0, int(p.get("flat_palette", 0) or 0))
    if flat_palette and not flat_mode:
        warnings.append("flat_palette ignored: flat is false")
        flat_palette = 0
    src = _open_input(req, warnings)
    helpers = ml.helper_shapes()
    meshes = [o for o in bpy.context.scene.objects
              if o.type == "MESH" and o not in helpers and len(o.data.polygons)]
    if not meshes:
        raise ValueError("no mesh objects in the input")
    targets = _targets(meshes, p.get("objects") or [], warnings)
    if not targets:
        raise ValueError("nothing to paint: no target meshes (params.objects?)")
    lap("open")

    # ------------------------------------------------------------------ concept
    max_px = int(p.get("concept_max_px", 2048))
    fv = _load_view(concept_path, inputs.get("concept_original"), max_px, warnings, "concept")
    px, Hc, Wc = fv["px"], fv["H"], fv["W"]
    fit_mask, col_mask, mask_info, subject_h = fv["fit"], fv["col"], fv["mask_info"], fv["subject_h"]
    px0, fit0 = px, fit_mask                       # the reference as given (before a warp)
    lap("concept")

    # ------------------------------------------------------------------ mesh data + camera
    kind = str((req.get("asset") or {}).get("kind") or p.get("kind") or "").lower()
    datas = {o.name: pl.mesh_data(o) for o in meshes}
    offs, Vs, Ts, tri_piece = 0, [], [], []
    comp_of = {}
    weld = {}
    comp_base = 0
    for o in meshes:
        d = datas[o.name]
        Vs.append(d["co"])
        Ts.append(d["tv"] + offs)
        offs += len(d["co"])
        diag = float(np.linalg.norm(d["co"].max(axis=0) - d["co"].min(axis=0))) if len(d["co"]) else 1.0
        comp, canon, nc, cedges = pl.welded_pieces(d["co"], d["edges"], 1e-5 * max(diag, 1e-3))
        weld[o.name] = (canon, nc, cedges)
        comp = comp.astype(np.int64) + comp_base
        comp_base = int(comp.max()) + 1 if len(comp) else comp_base
        comp_of[o.name] = comp
        tri_piece.append(comp[d["tv"][:, 0]])
    V_all = np.concatenate(Vs)
    T_all = np.concatenate(Ts)
    tri_piece = np.concatenate(tri_piece)
    zmin, zmax = float(V_all[:, 2].min()), float(V_all[:, 2].max())
    H = max(zmax - zmin, 1e-6)
    pts = None
    cam_p = p.get("camera", "fit")
    if isinstance(cam_p, dict):
        cam = pl.OrthoCam.from_dict(cam_p, (Wc, Hc))
        cam.res = (Wc, Hc)
        iou_fit = None
        cam_info = {"explicit": True}
    else:
        yaw_range = p.get("yaw_range_deg")
        yaw_range = (180.0 if kind == "creature" else 60.0) if yaw_range is None else float(yaw_range)
        pts = pl.area_samples(V_all, T_all, min(200000, max(60000, 3 * len(T_all))), seed=1)
        cam, iou_fit, cam_info = pl.fit_camera(
            pts, fit_mask, yaw_range=yaw_range, yaw_step=float(p.get("yaw_step_deg", 5)),
            pitch_range=float(p.get("pitch_range_deg", 15)), perspective=bool(p.get("perspective", True)))
    lap("camera")
    u_all, v_all, z_all = cam.project(V_all)
    zb, piece_buf = pl.zbuffer(u_all, v_all, z_all, T_all, Wc, Hc, tri_label=tri_piece)
    mesh_mask = np.isfinite(zb)
    iou = pl.mask_iou(mesh_mask, fit_mask)
    tol0 = float(p.get("depth_tolerance", 0.004)) * H
    f_min = float(p.get("facing_min", 0.1))
    f_full = float(p.get("facing_full", 0.4))
    lap("zbuffer")

    # ------------------------------------------------------------------ back reference: its own camera
    back = None
    back_info = None
    checks_extra = []
    if back_path:
        try:
            bv = _load_view(back_path, inputs.get("concept_back_original"), max_px, warnings, "concept_back")
            bcam_p = p.get("camera_back", "fit")
            if isinstance(bcam_p, dict):
                bcam = pl.OrthoCam.from_dict(bcam_p, (bv["W"], bv["H"]))
                bcam.res = (bv["W"], bv["H"])
                biou_fit, binfo = None, {"explicit": True}
            else:
                if pts is None:
                    pts = pl.area_samples(V_all, T_all, min(200000, max(60000, 3 * len(T_all))), seed=1)
                bcam, biou_fit, binfo = pl.fit_camera(
                    pts, bv["fit"], yaw_range=float(p.get("back_yaw_range_deg", 15)), yaw_step=5.0,
                    pitch_range=float(p.get("back_pitch_range_deg", 6)),
                    yaw_center=float(p.get("back_yaw_deg", 180)), perspective=bool(p.get("perspective", True)))
            bu, bvv, bz = bcam.project(V_all)
            bzb = pl.zbuffer(bu, bvv, bz, T_all, bv["W"], bv["H"])
            bmask = np.isfinite(bzb)
            biou = pl.mask_iou(bmask, bv["fit"])
            back = dict(bv, cam=bcam, zb=bzb, mesh_mask=bmask, iou=biou, iou_fit=biou_fit, cam_info=binfo,
                        px0=bv["px"], fit0=bv["fit"])
            if biou < 0.5:
                checks_extra.append(af.check("concept_back_iou_min", False, round(biou, 4), 0.5,
                                             note="the back reference does not fit the model (wrong view or "
                                                  "broken mesh): ignored"))
                back_info = {"concept": back_path, "ignored": True, "iou": round(biou, 4),
                             "camera": bcam.to_dict()}
                back = None
            else:
                checks_extra.append(af.check("concept_back_iou_min", True, round(biou, 4), 0.5))
        except ValueError as exc:
            warnings.append("back reference ignored: %s" % exc)
            back_info = {"concept": back_path, "ignored": True, "error": str(exc)}
            back = None
        lap("back_camera")

    # ------------------------------------------------------------------ non-rigid alignment
    align_info = {"mode": align_mode}
    field_f = field_b = None
    if align_mode == "warp":
        kw = {"align_max": float(p.get("align_max", 0.06)), "smooth": float(p.get("align_smooth", 2.0)),
              "iters": int(p.get("align_iters", 4)), "max_points": int(p.get("align_points", 450))}
        px, fit_mask, col_mask, info_f, field_f = al.align_concept(
            px, fit_mask, col_mask, mesh_mask, subject_h, pl.dilate_colors, **kw)
        align_info["front"] = info_f
        if back is not None:
            bpx, bfit, bcol, info_b, field_b = al.align_concept(
                back["px"], back["fit"], back["col"], back["mesh_mask"], back["subject_h"], pl.dilate_colors, **kw)
            back.update({"px": bpx, "fit": bfit, "col": bcol})
            align_info["back"] = info_b
        for side, inf in (("front", info_f), ("back", align_info.get("back"))):
            if inf is None:
                continue
            if not inf.get("applied"):
                warnings.append("align: %s warp not applied (%s)" % (side, inf.get("skipped", "no change")))
            else:
                af.log("align_" + side, iou_before=inf["iou_before"], iou_after=inf["iou_after"],
                       p95_pct=inf["displacement"]["p95_pct"])
        lap("align")

    # ------------------------------------------------------------------ what the projection samples
    dv = _derive(px, fit_mask, col_mask, p, with_emission=True)
    edge_d, rgb_d, rgb_s, rgb_b = dv["edge_d"], dv["rgb_d"], dv["rgb_s"], dv["rgb_b"]
    delight_info, blur_r, fade_k = dv["delight_info"], dv["blur_r"], dv["fade_k"]
    emi_img, emi_info = dv["emi_img"], dv["emi_info"]
    px_m = 1.0 / cam.s
    visibility = _View(cam, zb, dv, tol0, f_min, f_full)
    col_mask_d = pl.dilate(col_mask, 2)
    bview = None
    if back is not None:
        bd = _derive(back["px"], back["fit"], back["col"], p, with_emission=False)
        back["derived"] = bd
        bview = _View(back["cam"], back["zb"], bd, tol0, f_min, f_full)
    lap("prepare")

    # symmetry plane (mirror fill) and head band (no through-projection on the back of the head)
    mirror_mode = str(p.get("mirror", "auto")).lower()
    fill_info = {}
    if mirror_mode == "x":
        mpt = np.array([float(np.median(V_all[:, 0])), 0.0, 0.0])
        mnrm = np.array([1.0, 0.0, 0.0])
        fill_info["mirror"] = {"mode": "x", "point": [round(float(mpt[0]), 4), 0.0], "angle_deg": 0.0}
    elif mirror_mode == "auto":
        mpt, mnrm, minfo = pl.symmetry_plane(V_all)
        fill_info["mirror"] = dict(minfo, mode="auto")
    else:
        mpt = mnrm = None
        fill_info["mirror"] = {"mode": "none"}
    head_guard = p.get("head_guard")
    head_guard = (kind != "creature") if head_guard is None else bool(head_guard)
    head_z = None
    if head_guard:
        hp, hn = (mpt, mnrm) if mpt is not None else (np.array([float(np.median(V_all[:, 0])), 0, 0]),
                                                       np.array([1.0, 0, 0]))
        head_z, top_z = pl.head_band(V_all, hp, hn, frac=float(p.get("head_fraction", 0.13)))
        fill_info["head_band_z"] = [round(head_z, 4), round(top_z, 4)]
    through_mode = str(p.get("back_fill", "through")).lower()

    def through_ok(u, v, piece, facing, zpos):
        """The concept pixel in front of a hidden point shows the same connected piece (not a shield or
        sword in front of the body) and the point is not on the back of the head."""
        if through_mode == "none":
            return np.zeros(len(u), dtype=bool)
        ins = (u >= 0) & (u < Wc) & (v >= 0) & (v < Hc)
        j = np.clip(np.floor(u).astype(np.int64), 0, Wc - 1)
        i = np.clip(np.floor(v).astype(np.int64), 0, Hc - 1)
        same = np.zeros(len(u), dtype=bool)
        for dy, dx in ((0, 0), (1, 0), (-1, 0), (0, 1), (0, -1)):   # vertices sit on silhouette edges
            same |= piece_buf[np.clip(i + dy, 0, Hc - 1), np.clip(j + dx, 0, Wc - 1)] == piece
        ok = ins & col_mask_d[i, j] & same
        if head_z is not None:
            ok &= ~((zpos > head_z) & (facing < 0.1))
        return ok

    # ------------------------------------------------------------------ per-vertex fill
    # core = confidently visible vertices (their blurred concept colour; with a back reference also the
    # vertices only the back camera sees); every other vertex takes, in
    # order: the mirror point (occluded FRONT faces whose mirror twin is visible: body behind a shield),
    # the concept straight in front of it (backs: the front seen through the body, blurred), the mirror
    # point (other hidden faces), the nearest core vertex of the same connected piece, else any.
    core_pts, core_col, core_comp = [], [], []
    vert = {}
    for o in meshes:
        d = datas[o.name]
        u, v, vis, w, facing = visibility(d["co"], d["vn"])
        core_f = vis & (w >= 0.999) & (facing >= f_full)
        cols = np.zeros((len(d["co"]), 3), dtype=np.float32)
        if core_f.any():
            cols[core_f] = pl.bilinear(rgb_b, u[core_f], v[core_f])
        core_b = np.zeros(len(core_f), dtype=bool)
        if bview is not None:
            ub, vb, visb, wb, facb = bview(d["co"], d["vn"])
            core_b = visb & (wb >= 0.999) & (facb >= f_full) & ~core_f
            if core_b.any():
                cols[core_b] = pl.bilinear(back["derived"]["rgb_b"], ub[core_b], vb[core_b])
        core = core_f | core_b
        vert[o.name] = {"core": core, "core_back": core_b, "cols": cols, "u": u, "v": v, "facing": facing}
        core_pts.append(d["co"][core])
        core_col.append(cols[core])
        core_comp.append(comp_of[o.name][core])
    core_pts = np.concatenate(core_pts)
    core_col = np.concatenate(core_col)
    core_comp = np.concatenate(core_comp)
    fill_info["core_vertices"] = int(len(core_pts))
    if len(core_pts) < 3:
        raise ValueError("the model shows almost nothing through the fitted camera (%d visible vertices); "
                         "concept IoU %.3f" % (len(core_pts), iou))
    mirror_tol = float(p.get("mirror_tolerance", 0.03)) * H
    kd_all = pl.kd_tree(core_pts)
    min_core = 1      # a piece with any visible vertex keeps its own colours (sword, shield, cape)
    comp_ids, comp_counts = np.unique(core_comp, return_counts=True)
    comp_kd = {}
    cnt = {"mirror": 0, "through": 0, "mirror_through": 0, "same_piece": 0, "global": 0}
    for o in meshes:
        d = datas[o.name]
        vi = vert[o.name]
        comp = comp_of[o.name]
        free = ~vi["core"]
        tw = (vi["core"] & ~vi["core_back"]).astype(np.float32)   # back-only vertices: no front colour through
        if free.any():
            q_idx = np.nonzero(free)[0]
            Q = d["co"][q_idx]
            qc = comp[q_idx]
            fq = vi["facing"][q_idx]
            uq, vq = vi["u"][q_idx], vi["v"][q_idx]
            # nearest core vertex, same piece first
            n_col = np.zeros((len(q_idx), 3), dtype=np.float32)
            same = np.zeros(len(q_idx), dtype=bool)
            for cid in np.unique(qc):
                k = np.searchsorted(comp_ids, cid)
                if k >= len(comp_ids) or comp_ids[k] != cid or comp_counts[k] < min_core:
                    continue
                if cid not in comp_kd:
                    sel = np.nonzero(core_comp == cid)[0]
                    comp_kd[cid] = (pl.kd_tree(core_pts[sel]), sel)
                kd, sel = comp_kd[cid]
                rows_ = np.nonzero(qc == cid)[0]
                ii, _dd = pl.kd_nearest(kd, Q[rows_])
                n_col[rows_] = core_col[sel[ii]]
                same[rows_] = True
            rest = np.nonzero(~same)[0]
            if len(rest):
                ii, _dd = pl.kd_nearest(kd_all, Q[rest])
                n_col[rest] = core_col[ii]
            m_ok = np.zeros(len(q_idx), dtype=bool)
            mt_ok = np.zeros(len(q_idx), dtype=bool)
            m_col = n_col
            if mpt is not None:
                Qr = pl.reflect(Q, mpt, mnrm)
                im, dm = pl.kd_nearest(kd_all, Qr)
                m_ok = (core_comp[im] == qc) & (dm < mirror_tol)
                m_col = core_col[im]
                ur, vr, _zr = cam.project(Qr)
                mt_ok = through_ok(ur, vr, qc, fq, Qr[:, 2])
            t_ok = through_ok(uq, vq, qc, fq, Q[:, 2])
            # occluded front faces: mirror twin first; backs: straight through, else the mirror point's
            # through-projection (the back behind a shield takes the other side's front), else the twin
            m_first = m_ok & (fq > 0.15)
            use_t = t_ok & ~m_first
            use_mt = mt_ok & ~m_first & ~use_t
            use_m = m_ok & ~use_t & ~use_mt
            res = n_col.copy()
            res[use_m] = m_col[use_m]
            if use_t.any():
                res[use_t] = pl.bilinear(rgb_b, uq[use_t], vq[use_t])
            if use_mt.any():
                res[use_mt] = pl.bilinear(rgb_b, ur[use_mt], vr[use_mt])
            vi["cols"][q_idx] = res
            tw[q_idx] = use_t.astype(np.float32)
            cnt["mirror"] += int(use_m.sum())
            cnt["through"] += int(use_t.sum())
            cnt["mirror_through"] += int(use_mt.sum())
            rest_m = ~use_m & ~use_t & ~use_mt
            cnt["same_piece"] += int((rest_m & same).sum())
            cnt["global"] += int((rest_m & ~same).sum())
        smooth_n = int(p.get("fill_smooth", 10))
        canon, nc, cedges = weld[o.name]
        vi["cols"] = pl.smooth_fill_welded(vi["cols"], vi["core"], canon, nc, cedges, smooth_n)
        vi["tw"] = pl.smooth_fill_welded(tw[:, None], vi["core"], canon, nc, cedges, min(smooth_n, 4))[:, 0]
    nfree = max(1, sum(cnt.values()))
    fill_info.update({"filled_vertices": nfree, "blur_px": blur_r, "back_fill": through_mode,
                      "head_guard": head_guard, "mirror_tolerance_m": round(mirror_tol, 4)})
    fill_info.update({k + "_share": round(v_ / nfree, 4) for k, v_ in cnt.items()})
    lap("vertex_fill")

    # ------------------------------------------------------------------ palette (flat colouring)
    palette = None
    flat_info = {"enabled": flat_mode}
    if flat_mode and flat_palette:
        samples = [rgb_d[pl.erode(col_mask, 2)]]
        if back is not None:
            samples.append(back["derived"]["rgb_d"][pl.erode(back["col"], 2)])
        palette, pal_share = fl.kmeans_palette(np.concatenate(samples), flat_palette, seed=1)
        flat_info["palette"] = ["#%02x%02x%02x" % tuple(int(round(float(c) * 255)) for c in col) for col in palette]
        flat_info["palette_share"] = [round(float(s_), 4) for s_ in pal_share]
        lap("palette")

    # ------------------------------------------------------------------ textures per target
    size = int(p.get("size", 2048))
    uv_mode = str(p.get("uv", "auto")).lower()
    uv_pack = str(p.get("uv_pack", "aabb")).lower()
    emi_strength = float(p.get("emission_strength", 3.0))
    outputs = {}
    tex_stats = {"covered": 0, "projected": 0, "blended": 0, "filled": 0, "filled_through": 0,
                 "back_projected": 0, "back_blended": 0}
    qa_acc = {"concept_texels": 0, "grazing": 0}
    flat_acc = {"triangles": 0, "with_texels": 0, "confident": 0.0, "snap_de_sum": 0.0}
    uv_infos = {}
    debug_swaps = []
    emi_texels = 0
    multi = len(targets) > 1
    for ti, o in enumerate(targets):
        d = datas[o.name]
        usable, uinfo = pl.uv_report(d) if uv_mode != "smart" else (False, {"reason": "uv: smart"})
        if uv_mode == "keep" and d["uv"] is None:
            raise ValueError("uv: keep, but %s has no UV layer" % o.name)
        if (uv_mode == "auto" and not usable) or uv_mode == "smart":
            ml.smart_uv(o, angle_deg=float(p.get("uv_angle_deg", 66.0)), margin=float(p.get("uv_margin", 0.003)))
            if uv_pack in ("aabb", "convex", "concave"):
                pl.pack_uv(o, margin=float(p.get("uv_margin", 0.003)) * 0.7, shape=uv_pack)
            datas[o.name] = d = pl.mesh_data(o)
            uinfo["unwrapped"] = "smart_uv" + ("+pack_" + uv_pack if uv_pack != "none" else "")
        else:
            uinfo["unwrapped"] = "kept"
        uv_infos[o.name] = uinfo
        lap("uv_%d" % ti)
        tri_of, b1, b2 = pl.raster_uv(d["uv"], size)
        flat = np.nonzero(tri_of.ravel() >= 0)[0]
        uinfo["texel_coverage"] = round(len(flat) / float(size * size), 4)
        tri = tri_of.ravel()[flat]
        del tri_of
        l1 = b1.ravel()[flat][:, None]
        l2 = b2.ravel()[flat][:, None]
        del b1, b2
        l0 = 1.0 - l1 - l2
        tvt = d["tv"][tri]
        cp = d["co"][tvt]
        P = (l0 * cp[:, 0] + l1 * cp[:, 1] + l2 * cp[:, 2]).astype(np.float32)
        cn = d["cn"][tri]
        N = l0 * cn[:, 0] + l1 * cn[:, 1] + l2 * cn[:, 2]
        N = (N / np.maximum(np.linalg.norm(N, axis=1, keepdims=True), 1e-9)).astype(np.float32)
        del cp, cn
        lap("raster_%d" % ti)
        u, v, vis, w, facing = visibility(P, N)
        mb = np.zeros_like(w)
        if bview is not None:
            ub, vb, visb, wb, facb = bview(P, N)
            mb = (wb * (1.0 - w)).astype(np.float32)          # the back's share of what the front does not cover
        vi = vert[o.name]
        F = vi["cols"][tvt]
        fill = (l0 * F[:, 0] + l1 * F[:, 1] + l2 * F[:, 2]).astype(np.float32)
        TW = vi["tw"][tvt]
        tt = (l0[:, 0] * TW[:, 0] + l1[:, 0] * TW[:, 1] + l2[:, 0] * TW[:, 2]).astype(np.float32)
        del F, TW, N
        tt *= through_ok(u, v, comp_of[o.name][tvt[:, 0]], facing, P[:, 2])
        del P
        th = np.nonzero((tt > 0.01) & (w < 0.999))[0]
        dbg = (fill * 0.45 + MAGENTA * 0.55).astype(np.float32)
        if len(th):
            thc = pl.bilinear(rgb_b, u[th], v[th])
            tk = tt[th, None]
            fill[th] = tk * thc + (1 - tk) * fill[th]
            dbg[th] = tk * (thc * 0.6 + CYAN * 0.4) + (1 - tk) * dbg[th]
        # colour = w_f front + w_b (1 - w_f) back + (1 - w_f)(1 - w_b) fill
        rest_w = (1.0 - w - mb)[:, None]
        col = fill * rest_w
        dbg = dbg * rest_w
        vi_ = np.nonzero(w > 0)[0]
        samp = None
        if len(vi_):
            samp = pl.bilinear(rgb_s, u[vi_], v[vi_])
            ww = w[vi_, None]
            col[vi_] += ww * samp
            dbg[vi_] += ww * samp
        ib = np.nonzero(mb > 0)[0]
        if len(ib):
            sb = pl.bilinear(back["derived"]["rgb_s"], ub[ib], vb[ib])
            mbb = mb[ib, None]
            col[ib] += mbb * sb
            dbg[ib] += mbb * (sb * 0.85 + GREEN * 0.15)
        cw = w + mb
        tex_stats["covered"] += len(flat)
        tex_stats["projected"] += int((cw >= 0.999).sum())
        tex_stats["blended"] += int(((cw > 0) & (cw < 0.999)).sum())
        tex_stats["filled"] += int((cw <= 0).sum())
        tex_stats["filled_through"] += int(((cw <= 0) & (tt > 0.5)).sum())
        tex_stats["back_projected"] += int((mb >= 0.5).sum())
        tex_stats["back_blended"] += int(((mb > 0) & (mb < 0.5)).sum())
        # QA number: concept-coloured texels on grazing faces (smear risk)
        conf_t = cw >= 0.5
        if conf_t.any():
            fdom = np.where(w >= mb, facing, facb if bview is not None else facing)
            qa_acc["concept_texels"] += int(conf_t.sum())
            qa_acc["grazing"] += int((conf_t & (fdom < 0.5)).sum())
        if flat_mode:
            tcol, tinfo = fl.triangle_colors(tri, col, np.minimum(cw, 1.0), len(d["tv"]))
            has = ~np.isnan(tcol[:, 0])
            if palette is not None and has.any():
                snapped = fl.snap_to_palette(tcol[has], palette)
                flat_acc["snap_de_sum"] += float(np.abs(fl.srgb_to_lab(snapped) - fl.srgb_to_lab(tcol[has]))
                                                 .sum(axis=1).mean()) * int(has.sum())
                tcol[has] = snapped
            col = tcol[tri]
            flat_acc["triangles"] += len(d["tv"])
            flat_acc["with_texels"] += tinfo["triangles_with_texels"]
            flat_acc["confident"] += tinfo["confident_share"] * tinfo["triangles_with_texels"]
        tex = np.zeros((size * size, 3), dtype=np.float32)
        tex[flat] = col
        valid = np.zeros(size * size, dtype=bool)
        valid[flat] = True
        tex = tex.reshape(size, size, 3)
        valid = valid.reshape(size, size)
        tex, v2 = pl.dilate_texels(tex, valid, 4)
        tex = pl.pull_push(tex, v2)
        tdb = np.zeros((size * size, 3), dtype=np.float32)
        tdb[flat] = dbg
        tdb, v3 = pl.dilate_texels(tdb.reshape(size, size, 3), valid, 4)
        tdb = pl.pull_push(tdb, v3)
        suffix = ("_" + ml.safe_name(o.name, "mesh")) if multi else ""
        base_png = af.out_path(req, "paint_basecolor%s.png" % suffix)
        base_img = pl.image_from_array("paint_basecolor%s" % suffix, tex, "sRGB", base_png)
        outputs["basecolor" + suffix] = base_png
        dbg_img = pl.image_from_array("paint_debug%s" % suffix, tdb, "sRGB", None, pack=False)
        emi_tex_img = None
        if emi_img is not None and len(vi_):
            es = pl.bilinear(emi_img, u[vi_], v[vi_]) * w[vi_, None]
            if float(es.max()) > 0.05:
                et = np.zeros((size * size, 3), dtype=np.float32)
                et[flat[vi_]] = es
                ev = np.zeros(size * size, dtype=bool)
                ev[flat] = True
                et, _ = pl.dilate_texels(et.reshape(size, size, 3), ev.reshape(size, size), 2)
                emi_png = af.out_path(req, "paint_emission%s.png" % suffix)
                emi_tex_img = pl.image_from_array("paint_emission%s" % suffix, et, "sRGB", emi_png)
                outputs["emission" + suffix] = emi_png
                emi_texels += int((es.max(axis=1) > 0.05).sum())
        old = [m for m in o.data.materials if m is not None]
        mat = pl.painted_material(p.get("material_name") or (o.name + "_painted"), base_img, emi_tex_img,
                                  emi_strength, float(p.get("roughness", 0.75)), float(p.get("metallic", 0.0)))
        o.data.materials.clear()
        o.data.materials.append(mat)
        if len(o.data.polygons):
            o.data.polygons.foreach_set("material_index", np.zeros(len(o.data.polygons), dtype=np.int32))
        o.data.update()
        for m in old:
            if m.users == 0:
                bpy.data.materials.remove(m)
        debug_swaps.append((mat.node_tree.nodes["paint_base_color"], base_img, dbg_img))
        lap("texture_%d" % ti)
    cov = max(tex_stats["covered"], 1)
    tex_stats.update({"size": size, "projected_share": round(tex_stats["projected"] / cov, 4),
                      "blended_share": round(tex_stats["blended"] / cov, 4),
                      "filled_share": round(tex_stats["filled"] / cov, 4),
                      "back_share": round((tex_stats["back_projected"] + tex_stats["back_blended"]) / cov, 4)})
    emi_info["texels"] = emi_texels
    emi_info["strength"] = emi_strength if emi_texels else 0.0
    if flat_mode:
        flat_info.update({"triangles": flat_acc["triangles"], "triangles_with_texels": flat_acc["with_texels"],
                          "confident_share": round(flat_acc["confident"] / max(flat_acc["with_texels"], 1), 4)})
        if palette is not None:
            flat_info["snap_mean_delta_lab"] = round(flat_acc["snap_de_sum"] / max(flat_acc["with_texels"], 1), 3)

    # ------------------------------------------------------------------ previews + colour MAE
    win = _window(fit0 | mesh_mask)
    psize = int(p.get("preview_size", 384))
    concept_tile = _crop(px0, win, psize)                         # the reference as given; top row first
    act_tile = _crop(px, win, psize) if align_info.get("front", {}).get("applied") else concept_tile
    flat_tile = pl.render_camera_tiles(meshes, cam, win, psize, light="FLAT", cavity=False)[::-1]
    mae, mae_delit = _mae(act_tile, _crop(np.concatenate([rgb_d, px[..., 3:]], axis=2), win, psize), flat_tile)
    mae_orig = _mae(concept_tile, concept_tile, flat_tile)[0] if act_tile is not concept_tile else mae
    back_tiles = None
    if back is not None:
        bwin = _window(back["fit0"] | back["mesh_mask"])
        bconcept_tile = _crop(back["px0"], bwin, psize)
        bact_tile = _crop(back["px"], bwin, psize) if align_info.get("back", {}).get("applied") else bconcept_tile
        bflat_tile = pl.render_camera_tiles(meshes, back["cam"], bwin, psize, light="FLAT", cavity=False)[::-1]
        bmae, _ = _mae(bact_tile, bact_tile, bflat_tile)
        bmae_orig = _mae(bconcept_tile, bconcept_tile, bflat_tile)[0] if bact_tile is not bconcept_tile else bmae
        back_tiles = (bwin, bconcept_tile, bact_tile, bflat_tile)
    lo, hi = af.world_bbox(meshes)
    if p.get("preview", True):
        dirs = [(-0.7, -0.7, 0.3), (0.0, 1.0, 0.12)]
        row1 = [pl.over(concept_tile[::-1]), pl.over(pl.render_camera_tiles(meshes, cam, win, psize))]
        row1 += [pl.over(t) for t in pl.render_view_tiles(meshes, dirs, psize, bbox=(lo, hi))]
        for node, _base, dbg in debug_swaps:
            node.image = dbg
        row2 = [_overlay(fit0, mesh_mask, win, psize)[::-1], pl.over(pl.render_camera_tiles(meshes, cam, win, psize))]
        row2 += [pl.over(t) for t in pl.render_view_tiles(meshes, dirs, psize, bbox=(lo, hi))]
        for node, base, _dbg in debug_swaps:
            node.image = base
        rows = [row1, row2]
        if align_info.get("front", {}).get("applied"):
            cap_px = float(p.get("align_max", 0.06)) * subject_h
            rows.append([pl.over(act_tile[::-1]), _overlay(fit_mask, mesh_mask, win, psize)[::-1],
                         _heat(field_f, fit0 | mesh_mask, cap_px, win, psize)[::-1]])
        if back_tiles is not None:
            bwin, bconcept_tile, bact_tile, bflat_tile = back_tiles
            row_b = [pl.over(bconcept_tile[::-1]), pl.over(pl.render_camera_tiles(meshes, back["cam"], bwin, psize)),
                     _overlay(back["fit0"], back["mesh_mask"], bwin, psize)[::-1]]
            row_b += [pl.over(t) for t in pl.render_view_tiles(meshes, [(0.6, 0.75, 0.25)], psize, bbox=(lo, hi))]
            rows.append(row_b)
            if align_info.get("back", {}).get("applied"):
                cap_b = float(p.get("align_max", 0.06)) * back["subject_h"]
                rows.append([pl.over(bact_tile[::-1]), _overlay(back["fit"], back["mesh_mask"], bwin, psize)[::-1],
                             _heat(field_b, back["fit0"] | back["mesh_mask"], cap_b, bwin, psize)[::-1]])
        outputs["compare"] = _sheet(af.out_path(req, "compare.png"), rows)
    for _node, _base, dbg in debug_swaps:
        if dbg.users == 0:
            bpy.data.images.remove(dbg)
    lap("previews")

    # ------------------------------------------------------------------ QA sheet (always) + metrics
    qsize = int(p.get("qa_size", 384))
    # flat_tile / concept_tile are top row first; render_view_tiles returns bottom row first
    q_front = flat_tile
    if qsize != psize:
        q_front = pl.render_camera_tiles(meshes, cam, win, qsize, light="FLAT", cavity=False)[::-1]
    q_concept = concept_tile if qsize == psize else _crop(px0, win, qsize)
    qdirs = [(0.7, -0.7, 0.25), (-0.7, -0.7, 0.25), (1.0, 0.0, 0.1)]       # character's left / right 3/4, left side
    views = pl.render_view_tiles(meshes, qdirs + ([] if back_tiles is not None else [(0.0, 1.0, 0.1)]), qsize,
                                 bbox=(lo, hi), light="FLAT", cavity=False)
    tiles = [("CONCEPT FRONT", "ref", pl.over(q_concept)[..., :3]),
             ("MODEL FRONT", "model", pl.over(q_front)[..., :3])]
    if back_tiles is not None:
        bwin, bconcept_tile, _bact, bflat_tile = back_tiles
        q_bconcept = bconcept_tile if qsize == psize else _crop(back["px0"], bwin, qsize)
        q_bflat = bflat_tile
        if qsize != psize:
            q_bflat = pl.render_camera_tiles(meshes, back["cam"], bwin, qsize, light="FLAT", cavity=False)[::-1]
        tiles.append(("CONCEPT BACK", "ref", pl.over(q_bconcept)[..., :3]))
        tiles.append(("MODEL BACK", "model", pl.over(q_bflat)[..., :3]))
    else:
        tiles.append(("MODEL BACK", "model", pl.over(views[3])[::-1][..., :3]))
    for lab, tl in zip(("3/4 LEFT", "3/4 RIGHT", "SIDE"), views[:3]):
        tiles.append((lab, "model", pl.over(tl)[::-1][..., :3]))
    qa_sheet_path = af.out_path(req, "qa_sheet.png")
    pl.save_png_top(qa_sheet_path, qa.build_sheet(tiles, cols=4))
    outputs["qa_sheet"] = qa_sheet_path
    lap("qa_sheet")

    # ------------------------------------------------------------------ outputs
    cam_doc = dict(cam.to_dict(), iou=round(iou, 4), concept=concept_path)
    outputs["camera"] = af.out_path(req, "paint_camera.json")
    with open(outputs["camera"], "w", encoding="utf-8") as fh:
        json.dump(cam_doc, fh, indent=2)
    if back is not None:
        outputs["camera_back"] = af.out_path(req, "paint_camera_back.json")
        with open(outputs["camera_back"], "w", encoding="utf-8") as fh:
            json.dump(dict(back["cam"].to_dict(), iou=round(back["iou"], 4), concept=back_path), fh, indent=2)
    export = [o for o in bpy.context.scene.objects if o.type in ("MESH", "EMPTY", "ARMATURE") and o not in helpers]
    outputs["glb"] = af.export_glb(af.out_path(req, "painted.glb"), objects=export,
                                   animations=bool(bpy.data.actions),
                                   skins=any(o.type == "ARMATURE" for o in export))
    n_img, n_base, n_emi = pl.glb_images(outputs["glb"])
    outputs["blend"] = af.save_blend(af.out_path(req, "painted.blend"))
    lap("export")

    # ------------------------------------------------------------------ report
    surf = 0.0
    for o in targets:
        d = datas[o.name]
        P3 = d["co"][d["tv"]]
        surf += float(np.linalg.norm(np.cross(P3[:, 1] - P3[:, 0], P3[:, 2] - P3[:, 0]), axis=1).sum() / 2)
    texel_mm = 1000.0 * math.sqrt(surf / max(tex_stats["covered"], 1))
    f_info = align_info.get("front") or {}
    iou_aligned = f_info.get("iou_after", round(iou, 4)) if f_info.get("applied") else round(iou, 4)
    b_info = align_info.get("back") or {}
    metrics = {
        "iou_front": round(iou, 4), "iou_front_aligned": iou_aligned,
        "color_mae_front": mae, "color_mae_front_orig": mae_orig,
        "warp_front": f_info.get("displacement") if f_info.get("applied") else None,
        "texels": {"from_concept_share": tex_stats["projected_share"], "blended_share": tex_stats["blended_share"],
                   "fill_share": tex_stats["filled_share"], "from_back_share": tex_stats["back_share"]},
        "grazing_share": round(qa_acc["grazing"] / max(qa_acc["concept_texels"], 1), 4),
        "grazing_note": "share of concept-coloured texels on faces tilted > 60 deg from the camera (smear risk)",
    }
    if back is not None:
        metrics.update({
            "iou_back": round(back["iou"], 4),
            "iou_back_aligned": b_info.get("iou_after", round(back["iou"], 4)) if b_info.get("applied")
            else round(back["iou"], 4),
            "color_mae_back": bmae, "color_mae_back_orig": bmae_orig,
            "warp_back": b_info.get("displacement") if b_info.get("applied") else None})
    problems = qa.judge(metrics, has_back=back is not None)
    lap("qa_metrics")
    vlm = qa.run_vlm(qa_sheet_path, p, back is not None, log=af.log)
    lap("qa_vlm")
    min_score = float(p.get("qa_min_score", 6))
    if not vlm.get("ran") and not vlm.get("off"):
        warnings.append("qa_vlm_unavailable: %s" % vlm.get("skipped", "unknown"))
    bad = bool(problems) or (vlm.get("ran") and vlm.get("score", 10) < min_score)
    note_parts = ["%s %s (limit %s)" % (q["metric"], q["value"], q["limit"]) for q in problems]
    if vlm.get("ran"):
        note_parts.insert(0, "vision model %s/10 (%s), minimum %s" % (vlm["score"], vlm["verdict"], min_score))
    note = "; ".join(note_parts)
    qa_doc = {"version": 1, "sheet": qa_sheet_path, "metrics": metrics, "thresholds": dict(qa.THRESHOLDS),
              "problems": problems, "vlm": vlm, "min_score": min_score, "ok": not bad,
              "timings_s": {"sheet": timings.get("qa_sheet"), "metrics": timings.get("qa_metrics"),
                            "vlm": timings.get("qa_vlm")}}
    outputs["qa"] = af.out_path(req, "qa.json")
    with open(outputs["qa"], "w", encoding="utf-8") as fh:
        json.dump(_plain(qa_doc), fh, indent=2, ensure_ascii=False)
    checks = [
        af.check("concept_iou_min", iou >= 0.5, round(iou, 4), 0.5,
                 note="silhouette of the model through the fitted camera vs the concept mask"),
        af.check("glb_base_color_texture", n_base >= len(targets), n_base, len(targets)),
        af.check("concept_iou", iou_aligned >= 0.8, iou_aligned, 0.8, severity="warning"),
        af.check("projected_share", tex_stats["projected_share"] >= 0.08, tex_stats["projected_share"], 0.08,
                 severity="warning", note="texels coloured straight from the concept"),
    ] + checks_extra
    if mae is not None:
        checks.append(af.check("color_mae", mae <= 0.1, mae, 0.1, severity="warning",
                               note="flat render through the fitted camera vs the concept (sRGB, common silhouette)"))
    checks.append(af.check("texture_qa", not bad, vlm.get("score") if vlm.get("ran") else None, min_score,
                           severity="warning", note=note or "metrics within limits"))
    if bad:
        warnings.append("texture_qa: %s" % (note or "vision model score below %s" % min_score))
    if iou_aligned < 0.8:
        warnings.append("concept IoU %.3f: colours may land off their parts (pose / perspective differs from "
                        "the concept%s)" % (iou_aligned, "" if align_mode == "warp" else "; try align: warp"))
    timings["total"] = round(time.time() - t_all, 2)
    stats = {
        "input": src, "concept": concept_path, "concept_size": [Wc, Hc], "subject_px": int(subject_h),
        "mask": mask_info, "camera": cam.to_dict(), "camera_search": cam_info.get("coarse") if isinstance(cam_info, dict) else None,
        "camera_refine": cam_info.get("refine") if isinstance(cam_info, dict) else None,
        "camera_perspective": cam_info.get("perspective") if isinstance(cam_info, dict) else None,
        "iou_fit": iou_fit, "iou": round(iou, 4), "iou_aligned": iou_aligned, "height_m": round(H, 4),
        "targets": [o.name for o in targets], "kept_meshes": [o.name for o in meshes if o not in targets],
        "uv": uv_infos, "texels": tex_stats, "fill": fill_info, "emission": emi_info, "delight": delight_info,
        "edge_fade_px": fade_k, "color_mae": mae, "color_mae_delit": mae_delit, "color_mae_orig": mae_orig,
        "align": align_info, "flat": flat_info,
        "resolution": {"mm_per_concept_px": round(1000.0 * px_m, 3), "mm_per_texel": round(texel_mm, 3),
                       "surface_m2": round(surf, 4),
                       "note": "the concept carries the detail: texels finer than a concept pixel only "
                               "interpolate it"},
        "glb": {"images": n_img, "base_color_materials": n_base, "emissive_materials": n_emi},
        "sockets_kept": sorted(o.name for o in export if o.type == "EMPTY"),
        "qa": {"metrics": metrics, "problems": problems, "vlm": vlm, "ok": not bad},
        "timings_s": timings,
    }
    if back is not None:
        stats["back"] = {"concept": back_path, "concept_size": [back["W"], back["H"]], "mask": back["mask_info"],
                         "camera": back["cam"].to_dict(), "iou_fit": back["iou_fit"], "iou": round(back["iou"], 4),
                         "iou_aligned": metrics["iou_back_aligned"], "color_mae": bmae, "color_mae_orig": bmae_orig,
                         "camera_search": back["cam_info"].get("coarse"), "camera_refine": back["cam_info"].get("refine"),
                         "camera_perspective": back["cam_info"].get("perspective"),
                         "texels_from_back_share": tex_stats["back_share"]}
    elif back_info is not None:
        stats["back"] = back_info
    return {"outputs": outputs, "stats": stats, "checks": checks, "warnings": warnings}


def _plain(o):
    """JSON-safe copy (numpy scalars / arrays)."""
    if isinstance(o, dict):
        return {str(k): _plain(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_plain(v) for v in o]
    if isinstance(o, np.generic):
        return o.item()
    if isinstance(o, np.ndarray):
        return o.tolist()
    return o


# ---------------------------------------------------------------------- preview helpers
def _window(mask, pad=0.05):
    ys, xs = np.nonzero(mask)
    x0, x1, y0, y1 = xs.min(), xs.max() + 1, ys.min(), ys.max() + 1
    side = int(max(x1 - x0, y1 - y0) * (1 + 2 * pad)) + 2
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    return (int(round(cx - side / 2.0)), int(round(cy - side / 2.0)), side)


def _crop(img, win, size):
    """Crop a top-first (h, w, c) image to the square window (padding with zeros) and resize."""
    x0, y0, side = win
    h, w = img.shape[:2]
    out = np.zeros((side, side, img.shape[2]), dtype=np.float32)
    sx0, sy0 = max(0, x0), max(0, y0)
    sx1, sy1 = min(w, x0 + side), min(h, y0 + side)
    if sx1 > sx0 and sy1 > sy0:
        out[sy0 - y0:sy1 - y0, sx0 - x0:sx1 - x0] = img[sy0:sy1, sx0:sx1]
    if img.shape[2] == 4:   # premultiply for the resize, then back
        out[..., :3] *= out[..., 3:4]
        r = pl.resize_area(out, size, size)
        r[..., :3] /= np.maximum(r[..., 3:4], 1e-6)
        return np.clip(r, 0, 1)
    return pl.resize_area(out, size, size)


def _mae(concept_tile, delit_tile, flat_tile):
    a = concept_tile[..., 3] > 0.5
    b = flat_tile[..., 3] > 0.5
    both = pl.erode(a & b, 1)
    if both.sum() < 20:
        return None, None
    m1 = float(np.abs(concept_tile[..., :3][both] - flat_tile[..., :3][both]).mean())
    m2 = float(np.abs(delit_tile[..., :3][both] - flat_tile[..., :3][both]).mean())
    return round(m1, 4), round(m2, 4)


def _overlay(fit_mask, mesh_mask, win, size):
    """Green = both, red = concept only, blue = model only (top row first, opaque)."""
    m = np.stack([fit_mask, mesh_mask], axis=2).astype(np.float32)
    c = _crop(m, win, size) > 0.5
    a, b = c[..., 0], c[..., 1]
    out = np.ones((size, size, 4), dtype=np.float32)
    out[..., :3] = (0.82, 0.82, 0.84)
    out[a & b, :3] = (0.36, 0.72, 0.38)
    out[a & ~b, :3] = (0.86, 0.25, 0.22)
    out[~a & b, :3] = (0.25, 0.40, 0.90)
    return out


def _heat(field, region, cap_px, win, size):
    """Displacement of the warp over `region` as a colour map (blue = none, yellow = half the cap, red = the
    cap); top row first, opaque."""
    mag = np.sqrt((field ** 2).sum(-1)) / max(cap_px, 1e-6)
    t = np.clip(mag, 0.0, 1.0)
    rgb = np.stack([t, 0.25 + 0.65 * (1.0 - np.abs(2 * t - 1.0)), 1.0 - t], axis=-1).astype(np.float32)
    rgba = np.concatenate([rgb, region[..., None].astype(np.float32)], axis=2)
    c = _crop(rgba, win, size)
    a = c[..., 3:4]
    out = np.ones((size, size, 4), dtype=np.float32)
    out[..., :3] = c[..., :3] * a + np.array((0.82, 0.82, 0.84), dtype=np.float32) * (1 - a)
    return out


def _sheet(path, rows, gap=6, bg=(0.82, 0.82, 0.84)):
    """Rows of bottom-first opaque tiles -> one PNG (rows top to bottom)."""
    built = []
    for tiles in rows:
        h = max(t.shape[0] for t in tiles)
        parts = []
        for k, t in enumerate(tiles):
            parts.append(t)
            if k < len(tiles) - 1:
                g = np.ones((h, gap, 4), dtype=np.float32)
                g[..., :3] = bg
                parts.append(g)
        built.append(np.concatenate(parts, axis=1))
    w = max(r.shape[1] for r in built)
    out = []
    for k, r in enumerate(reversed(built)):   # bottom-first: last row first
        if r.shape[1] < w:
            pad = np.ones((r.shape[0], w - r.shape[1], 4), dtype=np.float32)
            pad[..., :3] = bg
            r = np.concatenate([r, pad], axis=1)
        out.append(r)
        if k < len(built) - 1:
            g = np.ones((gap, w, 4), dtype=np.float32)
            g[..., :3] = bg
            out.append(g)
    return ml.save_png(path, np.concatenate(out, axis=0))
