# The paint module: painting a mesh from a concept (`paint_from_concept`, 2026-10-11)

Hunyuan3D 2.1 in its own ComfyUI produces **geometry only**: without this step every character of a concept batch
would come out white, both in the GLB for Godot and in sprites. The step takes the picture the mesh was generated from and
bakes a color map from it (and, where there are bright colored spots, an emission map).

**Update 2026-10-13** - the texture "does not drift" and is verified: the back is painted from a back-view reference,
the concept is non-rigidly aligned to the model silhouette (`align: warp`), flat per-face painting (`flat`) is available, and
quality control was added: the `qa_sheet.png` sheet, metrics, and feedback from a local vision model (Ollama). Everything new is enabled by parameters; without them the
result is unchanged (verified: the textures of the test characters match the previous ones byte for byte). See the section
["Back, alignment, flat mode, QA"](#back-alignment-flat-mode-qa-2026-10-13).

Files: `scripts/blender/steps/paint_from_concept.py`, `scripts/blender/steps/_paint_lib.py`, new
`_paint_align.py` (alignment), `_paint_flat.py` (flat mode, palette), `_paint_qa.py` (sheet, thresholds, model),
`scripts/blender/tests/test_paint_from_concept.py`, registration in `server/forge-steps.mjs`, the `inputs.concept` input in
`server/forge-pipeline.mjs` (`_conceptInputs`).

## Inputs

- Model: `inputs.blend` (after `cleanup` - its `clean.blend`; all objects and sockets are kept) or `inputs.mesh`
  (GLB/FBX/OBJ/.blend).
- `inputs.concept` - the concept picture. Best of all is the studio's **generation reference picture**
  (`job.preparedConcept.reference`: a square, the original RGB + rembg alpha, the object in the center). A picture without transparency
  works too: the background is cut out by the edge color.
- `inputs.concept_original` (optional) - `{image, crop, content_rect}`: the original upload and the crop of the reference
  picture. The reference PNG stores color **multiplied by alpha** (measured: for a banner, `RGB 4,4,5` at alpha 11 of 255),
  so wherever rembg left a weak alpha (a thin banner, a magic haze), the color is lost. The step puts the original's straight RGB
  back in place and divides the rest by alpha (without this the silhouette edge darkens).
- `inputs.concept_back` (optional; CLI - `params.concept_back_file`) - the same concept **from behind**: a back-view picture
  in the same conventions (RGBA, alpha = object; the frame may differ from the front one). The model's back is painted from it
  instead of being guessed from the front. The server does not fill this input yet (see "Server wiring").

The server finds the concept by itself (`resolveInputs`): `concept_file` (a path, CLI `--direct` only) > `concept_asset`
(UUID of a studio picture) > the reference picture of the generation job the asset was made from (`source.type: job`; for a copy
of a forge asset - the job of its source). A job without a reference picture falls back to its original upload. If there is nothing - 400
`no_concept` before Blender is launched.

## How it works

1. **Concept masks.** For camera fitting - alpha > 0.5 (small crumbs < 2 % of the largest piece are discarded).
   For color, also pale but clearly colored pixels (alpha >= 0.04, saturation above the background by 0.15, far from the background
   color, near the figure): this way a banner that rembg almost erased gets its color, while a gray shadow on the floor does not.
2. **Light de-lighting** (`delight` 0.25): the large brightness variation (blur of 12 % of the figure height) is pulled toward the
   median, the multiplier is limited to 0.85...1.2. This is not PBR de-lighting: the light painted in the concept stays in the albedo.
3. **Camera from the silhouette.** The Hunyuan model faces -Y, even if the concept is drawn in three-quarter view (a knight with a
   banner: camera at yaw -23 degrees). A search over yaw around the front (+-60 degrees, +-180 degrees for creatures, step 5 degrees) and pitch
   (+-15 degrees) with scale and shift fitting by silhouette area and center; when nearly equal, the smaller yaw wins
   (front and back silhouettes are mirror images). Then Nelder-Mead on IoU at 200 and 400 px (surface points, area sampling). The last
   stage adds **perspective** (inverse distance to the camera): concepts are perspective renders,
   a shield and sword nearer the camera look bigger. Perspective is kept only if IoU grows by 0.005 or more. IoU at full
   resolution is computed with an exact triangle z-buffer; the same pass marks which connected piece of the mesh is visible in
   each pixel (pieces are found by welding vertices by position: glTF splits the mesh at seams).
   **3b. Back camera** (`concept_back`): a second camera is fitted to the silhouette of the back reference by the same algorithm around
   yaw 180 degrees (+-15 degrees, pitch +-6 degrees), with its own z-buffer over all meshes. If the back IoU (`stats.back.iou`) is below 0.5, the back
   reference is ignored and the `concept_back_iou_min` check fails (wrong view or a broken mesh).
   **3c. Alignment** (`align: warp`, below): after the rigid camera the concept (color, alpha, both masks) is bent
   so that its silhouette lies on the model silhouette; the step then works with the bent concept. The same applies to the back reference
   against the model's back silhouette.
4. **Unwrap.** A usable one (single layer, no overlaps, within 0..1) is kept; a Hunyuan mesh has none - Smart UV (66 degrees) and
   AABB repacking with rotation: 30 % of the square -> 37 % in milliseconds (`uv_pack: concave` - 45 %, but ~10 s).
5. **Texels** (2048^2): position and normal of the corner from barycentric coordinates. A texel is **visible** if it faces the
   camera (`facing` > 0.1), passes the z-buffer (tolerance 0.4 % of height + a correction for face slope) and falls in the color mask.
   Visible ones take the concept color (bilinear) with weight `smoothstep(0,1; 0.4; facing)` x a falloff in a band of 0.4 % of the
   figure height at the silhouette edge: grazing faces do not smear a concept pixel into stripes, and the background-removal halo does not get in.
   **With a back reference** the same weight `w_b` is computed for the back camera, and the texel color =
   `w_f * front + w_b*(1 - w_f) * back + (1 - w_f)(1 - w_b) * fill`: the fill (which is also the guess) remains only where
   no camera sees (sides at a grazing angle, armpits, the underside of a cloak). Vertices visible only from behind
   become fill anchors on a par with those visible from the front.
6. **Filling the hidden** (blurred concept, 1.5 % of the figure height; per vertex, with edge smoothing and interpolation
   over triangles; "see-through" - per texel):
   - places occluded from the **front** whose mirror point (across the shoulder symmetry plane) is visible take its color: the body behind a shield;
   - otherwise the concept **"see-through"**: if the pixel in front of the point shows the same mesh piece, the back takes the color of the front
     (the tabard is blue at the back too). Except for the **back of the head** (top of the body column, 13 % of height): the face is not taken see-through there;
   - otherwise "see-through" for the mirror point (the back behind a shield takes the front of the other side);
   - otherwise the visible mirror point, otherwise the nearest visible vertex **of the same piece** (sword, shield, cloak keep their own colors),
     otherwise any nearest one (KD-tree in 3D).
   Island edges: 4 px of dilation, then pull-push (pyramid filling) - no seams, clean mip levels.
   **6b. Flat painting** (`flat: true`): each triangle gets **one** color, the weighted median of its texel colors
   (weights are the share of color coming from the concept; if a triangle has confident texels, the pure fill does not take part). With
   `flat_palette: N` the triangle colors are pulled toward a palette of N colors (k-means over the object's pixels in the concept
   in Lab, deterministic). The result is a faceted low-poly look, as on a flat-shaded reference, without smearing.
7. **Emission** (`emission: auto`): spots that stand out from their surroundings by **both** saturation (+0.15) **and** brightness
   (x1.35) with a cool hue (green - cyan - blue - violet): eyes, crystals, runes. Gold, skin, blood and
   red cloth do not glow; warm hues only if incandescent (fire). The emission map x `emission_strength` (3).
8. **Material**: Principled, Base Color (and Emission) from images, roughness 0.75, metallic 0 - exported to glTF
   (`KHR_materials_emissive_strength`), images are packed into the `.blend`. Glowing meshes (`replace_part` eyes) are not
   repainted; sockets and other objects stay.
9. **QA (always)**: the `qa_sheet.png` sheet, numbers in `stats.qa` and `qa.json`, feedback from a local vision model
   (if installed and the GPU is free). Details below.

## Parameters

| Parameter | Default | What it does |
|---|---|---|
| `size` | 2048 | side of the color map (512/1024/2048/4096) |
| `uv`, `uv_pack` | `auto`, `aabb` | unwrap: keep a usable one / redo; packing after Smart UV |
| `camera` | `fit` | or `{yaw_deg, pitch_deg, px_per_m, offset_px, k_per_m, centre}` from the previous run's `paint_camera.json` |
| `yaw_range_deg`, `yaw_step_deg`, `pitch_range_deg` | empty (60, creatures 180), 5, 15 | camera search |
| `perspective` | true | the stage with distance to the camera |
| `facing_min`, `facing_full` | 0.1 / 0.4 | grazing faces: fill only -> full concept color |
| `depth_tolerance` | 0.004 | z-buffer tolerance, share of height |
| `edge_fade_px` | empty | falloff band at the silhouette edge (0.4 % of the figure height, >= 2) |
| `delight` | 0.25 | strength of removing the large brightness variation, 0 - off |
| `fill_blur`, `fill_smooth` | 0.015, 10 | blur of the fill color (share of figure height), smoothing passes |
| `back_fill` | `through` | `none` - the back is taken only from the nearest visible point |
| `head_guard`, `head_fraction` | empty (yes, except creatures), 0.13 | back of the head without see-through face |
| `mirror`, `mirror_tolerance` | `auto`, 0.03 | symmetry plane (`x` - the X plane), mirror-point tolerance (share of height) |
| `concept_back_file` | - | path to the back-view reference (CLI); the server passes `inputs.concept_back` |
| `camera_back` | `fit` | or a camera from `paint_camera_back.json` |
| `back_yaw_deg`, `back_yaw_range_deg`, `back_pitch_range_deg` | 180, 15, 6 | back camera search |
| `align` | `none` | `warp` - non-rigid alignment of the concept (and the back reference) to the model silhouette |
| `align_max` | 0.06 | maximum displacement, share of the figure height |
| `align_smooth` | 2 | spline stiffness: larger is smoother (0.3 already follows every contour irregularity) |
| `align_iters`, `align_points` | 4, 450 | ICP iterations; contour points per mask (spline anchor points ~ 2x) |
| `flat` | false | one color per triangle |
| `flat_palette` | 0 | with `flat`: a palette of N colors (k-means), 0 - off |
| `qa_vlm` | `auto` | `auto` - model feedback if available; `off` - metrics only |
| `qa_model` | `qwen3-vl:8b` | env `ASTRA_QA_VLM` |
| `qa_url` | `http://127.0.0.1:11434` | Ollama; env `ASTRA_OLLAMA_URL` |
| `qa_min_score` | 6 | below it - the `texture_qa` warning (score 0-10) |
| `qa_free_comfy` | true | before the review, `POST /free` to ComfyUI (env `ASTRA_COMFY_URL`, `http://127.0.0.1:8190`) |
| `qa_wait_comfy_s` | 0 | wait this many seconds for a busy ComfyUI queue; otherwise the review is skipped |
| `qa_size`, `qa_timeout_s` | 384, 300 | sheet tile, px; model call timeout |
| `emission`, `emission_strength` | `auto`, 3 | emission map |
| `roughness`, `metallic` | 0.75, 0 | material constants |
| `objects`, `material_name` | all except glowing; `<object>_painted` | what to paint |
| `concept_asset`, `concept_file` | - | server-side: UUID of a studio picture or a path (CLI) |

## Outputs and report

`painted.glb`, `painted.blend` (images inside), `paint_basecolor.png`, `paint_emission.png` (if emission is
found), `paint_camera.json`, `paint_camera_back.json` (with a back reference), `qa_sheet.png`, `qa.json`, `compare.png`:
the top row - concept | model through the fitted camera | 3/4 | back
(Workbench, flat light + cavity: the Workbench studio light darkened the preview so much that silver looked gray);
the bottom row - silhouette match (green - both, red - concept only, blue - model only) and the same views where the
fill is tinted: **cyan - "see-through"** (the front, seen through the body), **magenta - mirror or nearest point**
(a guess), **green tint - color from the back reference**. Further rows appear conditionally: with `align: warp` - the bent
concept | silhouette match after bending | displacement heat map (blue - none, red - the `align_max` limit); with a
back reference - its row (reference | model through the back camera | silhouette match | 3/4 from behind) and
the same alignment row.

`stats`: `camera` (yaw, pitch, `px_per_m`, `k_per_m`, distance), `camera_perspective` (IoU without and with perspective),
`iou`, `texels` (covered, from the concept, blended, fill, of them "see-through"), `fill` (shares of methods, head band,
mirror plane), `emission`, `color_mae` (flat render through the camera vs. the concept on the common silhouette, sRGB),
`resolution` (mm per concept pixel vs. mm per texel), `mask` (whether the input was multiplied by alpha, whether the original RGB was taken),
`timings_s`. New: `iou_aligned` (IoU after bending; `iou` stays the IoU of the rigid camera), `color_mae_orig` (the same against the
original concept: after bending the color moves together with the geometry, so against the original the number is higher - this is not a regression),
`align` (`front` / `back`: IoU before and after, displacement mean / p95 / max in % of the figure height, Jacobian, number of anchor
points, seconds), `back` (camera, IoU, MAE, share of texels from the back), `flat` (palette, share of confident triangles,
mean Lab delta when pulling), `qa` (metrics, issues, model response).
Checks: `concept_iou_min` (error, IoU < 0.5 - wrong concept or a broken mesh), `concept_back_iou_min` (the same for the
back reference), `glb_base_color_texture` (error, no color map in the GLB), warnings `concept_iou`
(< 0.8; after bending - by `iou_aligned`), `projected_share` (< 8 % of texels from the concept; with a back reference it counts
that too), `color_mae` (> 0.1), `texture_qa` (the model's score is below `qa_min_score` or a metric is over its threshold), and in `warnings` also
`qa_vlm_unavailable: <reason>` when no model feedback was obtained (the step does not fail because of this).

## Back, alignment, flat mode, QA (2026-10-13)

**Where drift comes from.** Projecting a single front concept through a rigid camera *always looks perfect from the projection camera*:
drift cannot be seen from the front picture. It shows from the side, behind and in 3/4: a Hunyuan3D mesh is not rigidly identical to the concept (arm angle,
helmet size, sleeve length), and the color of one detail lands on its neighbor - the sleeve onto the hand, the face onto the helmet edge, stripes on
grazing faces, a guessed back. Three remedies: a back reference (the back is knowledge, not a guess), bending the concept to the model silhouette
(color lands on the detail it actually looks at), flat painting (faces without smeared stripes). A fourth is
a check that shows the remainder.

### Back from a reference

`inputs.concept_back` (CLI - `params.concept_back_file`). The back camera is fitted by the same `fit_camera` around yaw 180 degrees;
the weight `w_b` and the front weight `w_f` are computed by the same class (facing the camera, depth, color mask, edge falloff);
the blend is `w_f * front + w_b(1 - w_f) * back + (1 - w_f)(1 - w_b) * fill`. Colors from both cameras are computed first,
and only then does the fill get what nobody sees. Vertices visible only from behind take part in the fill as anchors -
sides and armpits are pulled toward the back just as they are toward the front. The `paint_back.py` script (a second pass over a finished
`painted.blend`) is replaced by this step: the difference is in the blend formula: there the old color (front + fill) was multiplied by `1 - w_b(1 - w_f)`, while here the weights
of front, back and fill sum to 1.

### Alignment `align: warp`

1. Contours (mask boundaries, outer and inner) of the model through the camera and of the concept are taken, pre-**closed** by
   morphology (0.8 % of height: small holes and cloak teeth do not dictate correspondences), with <= `align_points` points along a contour.
2. A regularized **thin-plate spline** (TPS) maps a model pixel to a position on the concept. The loop is like ICP
   (`align_iters` = 4): the model contour through the current spline -> the nearest concept contour point (and back: the nearest model
   point for each concept point) -> Huber weights (`delta` = 1 % of height; matches farther than `align_max` are not counted) ->
   recompute the spline. Stiffness goes from coarse to fine (x27, x9, x3, x1 of `align_smooth`): first the pose, then the contour.
3. **Interior anchor points**: 250 points deeper than 4 % of the height from the model contour are pinned with zero
   displacement. Without them the spline produced displacement bubbles in the middle of the torso and thighs (the left and right cloak contours
   pulled there in the same direction); now the displacement lives at the silhouette, and thin parts (arms) move as a whole, because their middle is no deeper than 4 %.
4. The spline is computed on a coarse grid (1/8 resolution), the displacement field is upsampled bilinearly, the displacement magnitude is limited to
   `align_max x figure height` (6 % by default), and the concept is resampled bilinearly: `warped(p) = concept(p + d(p))`.
   Colors are extended past the mask by 6 px before resampling, so there is no background at the new edge. Alpha and both masks are bent
   by the same field; everything else (color mask, edge distance, de-lighting, fill, emission) is computed from the
   bent picture.
5. If the IoU after bending is lower than before, the bend is discarded (`align.front.applied: false`, a warning).

Statistics: `align.front.iou_before` -> `iou_after`; `displacement` - mean / p95 / max displacement in % of the figure height
(large means the mesh deviates noticeably from the concept: the metric warning `warp_*_p95_pct` > 4 %); `jacobian_min`,
`squeezed_share` - squeezing and folds of the field (< 0.1 and > 5 % of the area is a warning). Time (CPU, measured): 5.4-5.9 s per 2048^2 picture (spline 2.6-2.8 s: 1100 points,
grid 258^2; resampling color, alpha and masks 2.8-3.0 s), ~2.8 s at 1504^2.

### Flat painting

`flat: true` - one color per triangle: from the triangle's texels a per-component weighted median is taken
(`_paint_flat.triangle_colors`); texels with a concept share >= 0.5 are weighted by share, and when there are such texels the pure fill does not
take part. `flat_palette: N` - pulling toward a k-means palette (Lab, k-means++ with a fixed seed, <= 40,000 pixel
samples of the object, both references together). Each triangle is rasterized in its UV triangle by the same `raster_uv`;
dilation and pull-push are the same. The emission map does not depend on the mode. A triangle without texels (smaller than a texel in
the unwrap) stays uncolored and is not drawn: the mode is designed for a mesh after `cleanup` (10,000 triangles give ~130
texels per triangle at 2048^2).

### QA

Always, with no parameters: the `qa_sheet.png` sheet (labeled tiles, flat light, 384 px: concept front | model front
(projection camera) | concept back | model back | 3/4 left | 3/4 right | side; without a back reference "model back" is a
plain back view and there is no concept-back tile), numbers in `stats.qa.metrics` and in `qa.json`:

| Metric | What it shows | Threshold |
|---|---|---|
| `iou_front`, `iou_back` | IoU of the model and concept silhouettes (rigid camera) | - |
| `iou_front_aligned`, `iou_back_aligned` | the same after bending | < 0.8 |
| `color_mae_front`, `color_mae_back` | flat render through the camera vs. the concept, common silhouette | > 0.1 |
| `warp_front`, `warp_back` | displacement: mean / p95 / max, % of height; Jacobian | p95 > 4 %; Jacobian < 0.1; squeezed > 5 % |
| `texels` | shares of texels from the concept / blended / fill / from the back | fill > 60 % with a back reference |
| `grazing_share` | share of concept texels on faces at more than 60 degrees to the camera (stripe risk) | > 0.35 |

The thresholds are `_paint_qa.THRESHOLDS`; they are also in `qa.json`. The `color_mae` metric does not catch drift by itself (it is computed on the common
silhouette through the same camera), so IoU, displacement and the grazing-face share sit beside it.

**Local model feedback** (`qa_vlm: auto`). The sheet goes to Ollama `POST {qa_url}/api/chat` (`stream: false`,
`temperature: 0`, `keep_alive: 0` - the model is unloaded immediately): the model is told the sheet layout and asked for strict JSON
`{"issues": [{view, part, problem}], "score": 0-10, "verdict": "ok"|"fix"}` with a check: the reference color on its own parts
(face, hands, helmet, sleeves, belt, boots, cloak), no smearing or stripes, the back matches the back reference, no color bleeding
between parts. Key order - issues first, then the score. Before the call:

1. `GET {qa_url}/api/tags` - no Ollama or no `qa_model` model -> the review is skipped (warning `qa_vlm_unavailable`), without
   touching ComfyUI.
2. `GET {comfy}/queue` - the GPU is shared with ComfyUI (mesh generation): if a job is running or queued, the review is skipped ("ComfyUI is busy",
   `qa_wait_comfy_s` can be set); `/free` is not called in this case. If ComfyUI is not running, there is simply no `/free`.
3. `POST {comfy}/free {"unload_models": true, "free_memory": true}` (`qa_free_comfy`, errors ignored), then the model call
   (up to `qa_timeout_s`).

**Important about the model.** `qwen3-vl:8b` is a "thinking" model: left alone it reasons for thousands of tokens on every tile and never
reaches an answer (measured: 8000 tokens, an empty answer, 94 s; the server ignores `think: false`). So the answer is **prefilled**
(`{"issues":` as the last message with the assistant role) and the reasoning is skipped: the review takes 2-6 s with the model already loaded
(a cold load from disk up to ~80 s). Response parsing is tolerant (``` fences, text around, a score outside 0-10 is clamped), any failure is a
warning, and the step does not fail. Another vision model (`qa_model`) works the same way: the prefill is an ordinary JSON continuation.

### Server wiring

The registry `server/forge-steps.mjs` was not changed by this update: the new parameters reach Blender "as is" (the registry writes the
warning "parameter missing from the registry"), and `concept_back_file` is passed as an ordinary parameter without the server checking the path. For the step to
run from the studio without warnings, the following are needed: parameter entries (`concept_back_file` with `server: true, path: true`, `camera_back`,
`align`, `align_max`, `align_smooth`, `align_iters`, `align_points`, `flat`, `flat_palette`, `back_yaw_*`, `qa_*`), the outputs
`camera_back`, `qa_sheet`, `qa`, and in `_conceptInputs` - filling `inputs.concept_back` (from `concept_back_file` / `concept_back_asset`).

### Measurements, test character C01: a skeleton with a red cloak (2026-10-13, CPU; Hunyuan mesh 703 -> `cleanup` 10,000 tris; references 1504^2 `ref_front.png`, `ref_back.png`)

| Run | IoU front / back | MAE front / back | Displacement front / back (mean, p95, max, % of height) | From concept / blended / fill | Step | Model review |
|---|---|---|---|---|---|---|
| (a) the old way, front only | 0.840 / - | 0.025 / - | - | 22.6 / 5.6 / 71.8 % | 10.4 s | 4/10 fix |
| (b) back + `align: warp` | 0.840 -> 0.893 / 0.795 -> 0.886 | 0.024 / 0.015 | 0.53 · 1.7 · 4.7 / 0.67 · 2.3 · 5.7 | 46.1 / 9.3 / 44.5 % (from the back 26.6 %) | 20.9 s | 6/10 fix |
| (c) (b) + `flat`, palette 12 | same | 0.056 / 0.053 | same | same (share of confident triangles 54 %) | 21.9 s | 6/10 fix |

Time without the model review (it takes 6 s with a warm model): (a) 10.4 s; (b) 20.9 s and (c) 21.9 s, of which alignment of both views is 5.7 s, the back camera 1.7 s.
"MAE" after alignment is computed against the bent concept (the one that was projected); against the original in (b): 0.050 / 0.041.

Observations from the contact sheets of these runs:

- (a) Front only: the back is a blurred front "see-through", the sides and 3/4 are smeared red and gray.
- (b) The back is sharp (spine, chain, spiked shoulder blade from the back reference). The side views still guess (no camera sees them): the model's
  remarks concern precisely the cloak from the side.
- (c) Flat faces, 12 colors. Eyes (smaller than a face) are averaged into the skull color - glowing eyes should be set with `replace_part`
  (`eyes`) or with the emission map.

**How the model review relates to the metrics.** The 8B model is a coarse judge: on these sheets 4 / 6 / 6 - it told "the back is blurred" (4) from "the back
from a reference" (6), but could not tell (b) from (c); on a sheet with a deliberately broken camera (70 px offset, yaw +12 degrees, IoU 0.49)
it also gives 4 - this case is caught by the numbers (`concept_iou_min` - error, `texture_qa`: IoU 0.49). So the score threshold is one
of the signals, not a replacement for the metrics.

## Pipeline

- Registry: the `paint_from_concept` step (the paint module, all kinds, CPU, slots `blend, mesh, concept`). 25 steps.
- In a concept pipeline `paint_from_concept` goes right after `cleanup`.
- The texture reaches the end of the chain: `rig_humanoid` and `retarget` take `painted.blend` (material and UV in place),
  `motion_style` exports a GLB with the image, `render_sprites` (Cycles, `grade_materials` adds only a saturation node
  to the map), `godot_package` - the Godot package GLB contains `paint_basecolor` (verified, see the measurements).

Running a single step:
```
"C:/Program Files/Blender Foundation/Blender 5.0/blender.exe" -b --factory-startup ^
  --python scripts/blender/af_run.py -- paint_from_concept request.json
```
```json
{"inputs": {"blend": "C:/.../01_cleanup/clean.blend", "concept": "C:/.../data/assets/<reference>.png",
            "concept_original": {"image": "C:/.../data/assets/<original>.jpg", "crop": [286, 28, 1044, 759],
                                 "content_rect": [76, 89, 758, 731]}},
 "params": {}, "asset": {"kind": "humanoid"}, "out_dir": "C:/.../02_paint_from_concept"}
```

## Measurements (Blender 5.0.1, CPU, 2026-10-11)

Step time is 9-14 s per character (40,000 tris, 2048^2 texture, CPU): camera fitting 3-5 s (with perspective),
raster and texture 4-5 s, preview 1-2 s; memory - hundreds of MB.

Typical results on real character and creature concepts: IoU 0.82-0.92, flat-render color difference 0.026-0.044,
texels "from concept / blended / fill" about 14-26 / 5-8 / 69-79 % (of the fill, 63-89 % is "see-through"),
resolution 1.6-2.6 mm per concept pixel against 1.7-2.2 mm per texel. A synthetic test gives IoU 0.982 (camera yaw 16.0 degrees against a true 15 degrees),
26.7 / 7.8 / 65.6 % and 1.8 s. "Color difference" is the mean |RGB| of the flat render through the fitted camera vs. the concept on the common silhouette (sRGB, 0-1).

What the checks showed on test characters:

- A front-facing figure: the front matches the concept (emblem, straps, face); the back is a blue tabard with a blurred emblem, the back of the helmet is
  silver (head guard), and the reverse of a shield is its front color seen through.
- A figure in a robe: robe, book, pouch and staff are in place; the back is a blue robe with a blurred belt, the hood from behind is the nearest hood color.
  A slab under the feet is Hunyuan geometry, not painting.
- Perspective: for a figure with a sword pointing toward the camera, an ortho camera (pitch +-8 degrees) gives an IoU of 0.777, while perspective with pitch
  +-15 degrees gives 0.815 - only perspective lays the sword along the concept diagonal, and the sword gets its own steel color.
- A creature (yaw search +-180 degrees, no head guard): feathers, body, straps; a glowing stone on the breastplate is the only emission found (21 texels).
- The synthetic test: a cylinder body (red/blue), a head, a shield (a separate piece) in front of the left side, a sword toward the camera,
  a cyan eye. The back is red/blue "see-through", the body behind the shield comes from the mirror (not the gold of the shield), the back of the head is the head color,
  the eye glows.

**Color reaches the end of the chain.** Sprites (Cycles, `render_sprites`) are colored in all clips and all three views; the Godot package GLB contains the `paint_basecolor` image (PNG, 1 material `Body_painted` with
`baseColorTexture`, 5 animations, ~4.7-4.9 MB), and the Godot 4.5.1 check passes.

A note on sprites: a dark slab under the feet of a robed character is a **flat base that Hunyuan glued to the feet**: `cleanup` leaves it,
the rig binds it to the feet, and it tilts with them (hence about 16.7 % of tears on that character's clips). This is a task for cleanup or the rig. Under Cycles light
and `grade_materials` (saturation x1.1) armor looks more bronze than the violet-gray armor of the concept.

Statuses of the test runs are not about color: the characters were marked `failed_qa` by the tear check on clips (idle: 3.3 %, 1.7 %,
16.7 % of torn surface at a threshold of 1 %) - that is the rig and weights, not painting.

Tests: `run_tests.py -- paint_from_concept` - 8 tests, all PASS, 17 s (synthetic, sockets and glowing meshes,
a repeat with the camera from `paint_camera.json`, a reduced concept, raster/z-buffer/pull-push, the emission mask, unwrap and
division by alpha, real character fixtures from disk); `npm test` 144/144; `npm run build` - no errors.
**2026-10-13:** 17 tests, all PASS, ~55 s. Added: the spline on synthetic silhouettes (IoU goes up, hand and sleeve colors in their
places, the displacement limit, no folds); a step with the model's arm lowered by 22 degrees against the concept's horizontal arm (without bending
the sleeve color lands on the hand - 0.75/0.09/0.09, with bending the hand is skin-colored 0.57/0.33/0.25); the back from a reference (green
back against red, also via `concept_back_file`); flat mode (one color per triangle, <= 5 palette colors, the red
chest in place); k-means / Lab / median; the QA sheet and metrics; thresholds and parsing of the model response; the model call **with a mock** of
`_paint_qa._request` (order tags -> queue -> free -> chat, request body, score and warnings, skipping when
Ollama is unavailable / the model is missing / the ComfyUI queue is busy / the call fails / the response is unreadable, no network). The test helper
`_run` sets `qa_vlm: off` by default.

## Honest limitations

- **Resolution.** Concepts of 1376 x 768 with the figure ~700 px tall give **2.5-2.6 mm per pixel** at a height of 1.8 m. A 2048^2
  texel on an AABB unwrap is 1.7-2.2 mm: the texel is slightly smaller than a concept pixel, so the texture merely interpolates it. Small things (skull eyes,
  engraving, rivets) are visible exactly as much as they are visible in those concept pixels. 4096 makes no sense.
- **One picture.** Directly from the concept comes 14-26 % of the covered texels (Hunyuan meshes have many internal and back surfaces:
  7.6 m^2 for a knight where a "human" figure has ~2 m^2). The rest is a **guess**: the back is a blurred front "see-through" (a tabard emblem
  repeats at the back, blurred; so does a book on a belt), the back of the head and a plume take the nearest visible point. This is plausible
  for clothing but is not knowledge.
- **Geometry is not the concept.** Hunyuan is wrong about the position of thin items (a sword points at the camera
  instead of down and sideways; a shield is narrower). Where the mesh sticks out of the concept silhouette there is no color - the fill is used there (magenta on
  `compare.png`). IoU 0.8-0.92 is typical; below 0.8 is a warning.
- **Emission is conservative.** Skeleton eyes in a concept are dim (brightness 0.54, saturation 0.36 over 3-6 pixels) and
  do not pass the threshold; spots that clearly glow do pass (synthetic test). False emission is worse than a missed one:
  eyes can be set with `replace_part` (`eyes`). No emission was found on the test characters.
- **Not PBR.** The concept's light stays in the albedo (only the large variation is removed), roughness and metallic are
  constant. Cycles sprites on top add their own light, so the model on sprites is darker and more contrasty than the concept.
- **Smart UV** on a noisy mesh gives thousands of small islands; seams are hidden by dilation and pull-push, but at heavy
  downscaling (mip) neighboring islands may mix a little.
- **The symmetry plane** is taken from the shoulder band (75-85 % of height); for strongly asymmetric figures the mirror fill
  triggers less often (tolerance 3 % of height) - then "nearest of the same piece" does the work.
- **Silhouette alignment is not semantic.** Contours are bent, not details: what is inside the silhouette (a hand versus a sleeve on an arm
  of the same width) the spline knows only through neighboring contours. If the pose differs so much that the silhouette lies "on the wrong arm",
  bending will not fix it; beyond `align_max` correspondences are discarded (for C01 the limit is not reached: p95 ~ 2 %).
  Extra geometry that is absent from the concept (a Hunyuan flat base) finds no pair farther than `align_max`, and a nearer one
  may pull the contour.
- **Side views are still a guess.** With front and back, three-quarter views and profile are painted from the fill (the sides are visible to both cameras
  only at a grazing angle); `grazing_share` of 12 % for C01 is not smearing but the share of concept on slanted faces, the rest is
  fill. Real knowledge of the sides requires a third reference.
- **The back reference must be the same character** in the same frame; a color mismatch between front and back at the sides shows as a seam
  (there is no dedicated metric: with opposite cameras no texel gets both weights).
- **Flat mode** erases details smaller than a face (eyes, chain, engraving) and on 10,000 Hunyuan triangles gives "noisy" faces -
  there are more faces than drawing details; on a mesh after `cleanup` with a smaller `target_tris` it looks cleaner. Emission stays a map on
  texels.
- **Model review is coarse.** An 8B vision model with a prefilled answer tells coarse things ("the back is blurred") apart and little else - small
  drift (a sleeve on a hand) it cannot tell on a 384 px sheet; the scores for the same sheet repeat and the remarks are often
  formulaic. Decisions are made from the metrics and by eye, and the model is a third signal. The review is not run while ComfyUI is busy.
- **The server** does not know the new parameters and the `concept_back` input (see "Server wiring").
