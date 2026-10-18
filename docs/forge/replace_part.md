# The replace_part module: replacing a part of a mesh with an ROI reconstruction

The Blender step `replace_part` removes a region from a finished mesh (for example, a smoothed-out skull) and puts
in its place a detail generated separately from a high-resolution crop. It is meant for small but noticeable details
that got too few pixels in the input of the full-figure generator: in the input of Hunyuan the skull of the test
character took about 120 pixels, while in the ROI crop it fills almost the whole frame.

Files: `scripts/blender/steps/replace_part.py`, `scripts/blender/steps/_replace_lib.py`,
`scripts/blender/tests/test_replace_part.py`.
`_mesh_lib.py` is used read-only.

## Order of work of the step

1. **Body.** Import, merging of meshes, optional normalization (`height_m`, `rotate_z_deg`,
   `ground`, `center` - as in `separate`), welding with `merge_distance` (glTF splits vertices along UV seams;
   without welding the neck cut gives 14 fragments instead of one ring), smoothing by a 40° angle.
2. **Removing the region.** The plane `cut_plane` cuts the mesh only under the footprint of `region`
   (the box, expanded by 50 %). The faces above the plane are collected into pieces connected without crossing the
   plane; a piece is removed if at least `min_inside` (0.5) of its area is inside the box. This way a slightly
   tight box still takes the whole head, while the edge of a hood that enters the box stays.
   Pieces under the plane that touch it only where the removed part touched it, and hang down no deeper
   than `hang_m`, are removed too - this is the old lower jaw going below the neck cut. The neck
   continues into the torso and stays. The open loops of the body on the plane are remembered.
3. **Detail.** Import, welding, removal of crumbs (`part_min_island`), working decimation to
   `max(4·part_tris, 100 000)`. If eyes are enabled, the eye sockets are searched for in the working frame, and small
   separate islands inside them (the generator sculpts a glowing eye as a ball) are removed.
4. **Alignment.** An initial similarity without rotation from the robust extents of the top 60 % of both
   surfaces (top, width and depth by the 2–98 percentiles) - the neck stub and the "stand" at the bottom
   do not affect it. Then a symmetric ICP with trimming of the worst 15 % of pairs and a common scale (Umeyama
   in closed form); the points of the detail below the cut plane do not take part. Rotation is off by default:
   the crop and the body were captured by one camera, the detail is already oriented as in the reference, and rotation
   toward the old surface pulls it toward the errors of the old head (measurement in the repo history). The report is
   the RMS "point - surface" in both directions, without the floor from the sampling step.
5. **Cutting the detail** by the plane raised by `seam_gap`. Pieces below the cut: if the section lies
   inside the body loop (or closer than 0.3 of its radius), it is the detail's own neck stub, together with the
   stand glued to it, and it is removed; a piece that does not touch the cut at all is a floating fragment of the
   stand and is removed; the others, hanging down no deeper than `keep_hang`, stay - this is the new
   lower jaw, and it is not cut flat. Then the crumbs and the final decimation to `part_tris`
   (the whole budget goes to the skull itself); the boundary vertices are returned to the plane.
6. **Texture `front_projection`.** The detail is temporarily returned to its generator frame, where the crop camera
   looks along +Y. The crop is registered to the detail by silhouette: an initial approximation from the top
   edge and the width of the top 60 %, then a search for scale and shift by the IoU of a dense surface sample
   with the crop mask (alpha or, for JPG, a key by the frame color). The faces looking at the camera get UVs into the
   crop (slot `part_front_projection`), the rest get the slot `part_back_fill` with the median color of the
   outer strip of the object. In the outer 5 % of the object width the photo fades smoothly into this color: these
   pixels land on grazing faces, so the seam between the slots is not visible as a step. Background pixels
   are never taken: the color of the object is stretched outward. `occlusion: true` additionally requires
   direct visibility from the camera; on the skull this gave more ragged light spots in the folds, so it is
   off.
7. **Eyes.** For each camera rotation −45…45° in 15° steps an orthographic depth map is built by rays against a BVH;
   a hollow = the morphological closing of the height minus the height, window 0.34 of the silhouette
   width. In the band at 35–92 % of the height a pair of spots is selected: both deep, at the same level,
   spaced 0.15–0.75 of the width apart, of similar volume; the nose and single pits are rejected. The gaze is the
   perpendicular to the line of the eyes (more precise than the grid of rotations). A sphere of radius `radius_factor` × the
   radius of the socket is placed in the middle of the depth, not further than the back wall. With a texture the sphere is
   shifted to the green glow spot in the crop inside its socket - to the same place as in the reference. Material
   `eye_glow` with emission, sockets `socket_fx_eye_l` (+X, the character's left eye) and `socket_fx_eye_r`,
   local +Z outward (contract §6).
8. **Seam.** The detail and the spheres are joined with the body. The longest body loop on the plane and the
   longest detail loop on the raised plane are connected by `bridge_loops` (a different number of edges
   is allowed); the other loops are closed. The bridge faces take the body material and the UV of the nearest
   vertex of the body edge, the orientation of the faces is made consistent with the neighbors.
9. **Comparison.** The old body and the result are rendered with Workbench in the same frame: front (the
   reference camera), 3/4 and profile relative to the gaze of the new skull, with geometry outside the
   head region clipped (a staff does not occlude). The crop is recomputed into the same frame; the silhouette IoU and the mean
   color error are computed over the rows above the cut. The depth of the sockets is measured with identical rays
   at the points of the new eyes on the old and new surface.

## Parameters

| Parameter | Default | What it does |
|---|---|---|
| `inputs.mesh` / `inputs.blend` | — | the body |
| `inputs.part` | — | the new detail in the generator frame (forward −Y, as the body) |
| `height_m`, `rotate_z_deg`, `ground`, `center` | none, 0, true, true | normalization of the body before everything else; `region` and `cut_plane` are in these meters |
| `merge_distance`, `shade_auto_smooth_deg` | `1e-5 × height`, 40 | body welding and smoothing |
| `region` | required | `{"min": [x,y,z], "max": [x,y,z]}` around the part being replaced |
| `cut_plane` | z-plane through `region.min` | `{"axis": "z", "value": 1.5}` or `{"point", "normal"}`; the detail is on the +normal side, `"side": "-"` flips the side |
| `min_inside` | 0.5 | the fraction of a piece's area inside the box at which it is removed |
| `hang_m` | 0.25 × box height | hanging depth of old pieces under the cut that are removed together with the region |
| `align` | `icp` | `icp`, `bbox` (only the initial similarity) or `none`: the detail stays in its own pose (after `part_transform`) without scaling to the removed region |
| `part_transform` | none | `{"location": [x,y,z], "rot_deg": [x,y,z] \| "matrix": 3×3 rows, "scale": s}` - the pose of the detail before everything else (a bone_kit hand: wrist at zero -> to the wrist) |
| `keep_materials` | false | true: without `texture` the detail keeps its own materials (the gold of rings, `Bone_aged` with vertex colors); the UV name and color attributes are matched to the body (white where there are none) |
| `cut_part` | true | false: the detail is not cut by the plane and not joined with a bridge, the body hole on the plane is closed with a cap (a hand made of separate bones has no "neck" for a bridge) |
| `icp_rotation` | false | allow ICP rotation |
| `icp_iters`, `icp_trim`, `samples` | 60, 0.85, 6000 | iterations, the fraction of pairs kept, points per surface |
| `max_rotation_deg`, `max_rms_rel` | 30, 0.04 | threshold for rolling ICP back to `bbox`; RMS warning / box diagonal |
| `part_tris` | 20 000 | budget of the detail after the cut (0 - no decimation) |
| `part_min_island`, `crumb_fraction` | 0.0005, 0.002 | crumbs before decimation and after the cut (a fraction of vertices) |
| `part_rotate_z_deg` | 0 | rotation of the detail before everything else |
| `seam_gap` | 0.02 × box height | how far above the plane the detail is cut (the bridge length) |
| `keep_hang` | = `hang_m` | hanging depth of the piece of the detail that stays (the jaw) |
| `texture` | none | `{"image", "crop_px": [l,t,r,b], "mode": "front_projection", "facing_min": 0.12, "fade": 0.05, "occlusion": false}` |
| `eyes` | none | `{"auto": true, "color": [0.25, 1.0, 0.35], "strength": 6, "radius_factor": 0.22, "remove_inner_islands": true, "snap_to_glow": true}`; the color is linear |
| `name` | `replaced` | name of the result object |
| `preview`, `preview_size` | true, 384 | `compare.png`, `compare_views.png` |

**Procedural details (2026-10-10, evening).** Alignment used to scale a bone_kit hand to fit a thick "spade"
(×2.31), and without a texture the gold of the rings was replaced by a flat `part_fill`. Now
`{"align": "none", "part_transform": {...}, "keep_materials": true, "cut_part": false}` puts the detail in the
given pose as is. Only the removal of the region with a cap on the plane remains. The `align_rms` check is not
computed with `align: none`: the old surface is not the target here.

**Sockets.** Body sockets outside the removed region are kept: they pass body normalization and are exported.
Sockets inside the region (on the detail side) leave together with it and land in `region.sockets_dropped`. The
sockets of the detail (for example `socket_mount_hand_l` of a bone_kit hand) are carried with it through
`part_transform`, the rotation and the alignment similarity. Test `test_replace_align_none_keeps_part_pose_materials_and_sockets`.

Outputs: `replaced.glb` (one mesh and the eye sockets), `replaced.blend`, `part_aligned.glb` (the detail
separately, aligned and cut), `part_projection.png`, `compare.png` (crop | old head |
new front | new 3/4 | new profile; a row with texture and a row of geometry), `compare_views.png`
(old and new in 3/4 and profile). Checks: `region_removed`, `part_nonempty`, `no_nan_verts`
(errors); `align_icp_sane`, `align_rms`, `texture_registration_iou` (≥ 0.8), `eyes_found`,
`seam_closed` (warnings).

## Notes from the pilot (an ROI skull)

The step was developed on a test character whose skull was regenerated from a crop of the reference image.

- **Crop.** The skull with a small margin and a short neck stub, the bottom at the top edge of the collar. Upscale
  with Lanczos about ×5 to roughly 845×1024. The alpha was made with rembg u2net on CPU, the same tool and the same
  weights as in the studio; dark pixels of the robe in the bottom 10 % of the frame were cut out.
- **Generation** in the studio itself (ComfyUI, Hunyuan3D 2.1, `quality: high` - 45 steps, octree 512³,
  `meshLevel: 0`, an RTX 4070): about 3 minutes per run, 2.0–2.9 million triangles. The studio limit of 2.5 million
  triangles is calculated for a full figure; an ROI detail fills the whole 512³ grid, so more triangles come out,
  and some runs were marked `failed` because of the limit (see Limitations).
- **Comparison of three seeds** with identical parameters (`height_m: 1.8`, `part_tris: 20000`, texture from the
  crop, eyes auto): the silhouette IoU against the crop rose from about 0.87–0.88 to 0.98, the color error fell from
  about 0.26 to 0.15, the depth of the sockets at the eye points grew from 0–25 mm to 28–40 mm, the body grew by
  about +13.5 thousand triangles, the seam was closed (0 open edges). The seed with the best color error, the deepest
  sockets and the sturdiest lower jaw was chosen.
- **Alignment variants:** `bbox` and `icp` give practically the same result (RMS to the old surface 11.8 -> 11.2 mm,
  IoU 0.981); `icp` + `icp_rotation` lowers the distance to the old head (8.9 mm) but turns the new skull toward its
  wrong orientation: the IoU with the reference falls to 0.89, color 0.149 -> 0.191, and the right eye is found outside the
  socket. This is why rotation is off. RMS is the distance to the **old** smoothed head: it is a measure of
  registration, not of quality.
- **Visual assessment.** The sockets became deep and dark, with the glow where the reference has its spots; the teeth
  are separate geometry in two rows rather than a drawing on a smooth box; there are zygomatic arches, a
  piriform aperture and a mandibular notch. The back of the head got a bone color instead of a purple
  projection of the robe. The neck seam on the front is covered by the collar; in profile the vertebrae of the generator
  are visible above the old neck. The glow looks like a plain green ball in Workbench: the emission is visible in
  EEVEE, Cycles and Godot.
- **Step time** about 46 s, of which 37 s is decimation of the raw 2.87-million-triangle mesh.

## Running

```
"C:/Program Files/Blender Foundation/Blender 5.0/blender.exe" -b --factory-startup ^
  --python scripts/blender/af_run.py -- replace_part request.json
```
```json
{"step": "replace_part",
 "inputs": {"mesh": "C:/.../body.glb", "part": "C:/.../skull_raw.glb"},
 "params": {"height_m": 1.8,
            "region": {"min": [-0.17, -0.22, 1.50], "max": [0.07, 0.10, 1.78]},
            "cut_plane": {"axis": "z", "value": 1.50}, "part_tris": 20000,
            "texture": {"image": "C:/.../skull_crop_rgba.png", "mode": "front_projection"},
            "eyes": {"auto": true, "color": [0.25, 1.0, 0.35], "strength": 6}},
 "out_dir": "C:/.../out"}
```

How to choose the box and the plane: slice the body by height (`separate` in `core.md` does the same).
The plane is placed above the top edge of the hood and below the base of the skull - on the test character
this was z 1.49–1.50, where the section holds only the neck and the horseshoe of the old jaw. The step
removes the lower part of the jaw below the plane and restores it on its own.

## Tests

`blender -b --factory-startup --python scripts/blender/tests/run_tests.py -- replace_part` -
all PASS, about 21 s (2026-10-10, evening: + `test_replace_align_none_keeps_part_pose_materials_and_sockets`):

- `test_umeyama_recovers_similarity` - the closed-form similarity, parsing of `cut_plane`;
- `test_icp_recovers_offset_and_scale` - a sphere with a "nose", scale 0.2 and a shift are found with an accuracy of
  ±0.004 and 6 mm, RMS not above the sampling floor at the true solution;
- `test_replace_synthetic_sphere_man` - on a figure made of spheres the head is replaced. The detail is twice as large
  and shifted, and has a neck stub, a jaw below the cut and a floating stand. Scale
  0.499 and a shift with an error up to 2.3 mm are found. RMS / diagonal 0.006, the old jaw under the cut is removed,
  the stub and the stand of the detail are removed, the jaw is kept, the bridge closed the mesh (0 boundary edges);
  the result is one mesh, the top is 1.75 m;
- `test_replace_bbox_mode_and_errors` - the `bbox` mode, errors for a bad box and without `inputs.part`;
- `test_eye_detection_synthetic` - a hollow sphere rotated by 20° with two through sockets and a
  smaller nose hole: both sockets are found on the ray axis, the left one on +X, the gaze 20 ± 6°, the nose is not
  taken; the depth at the eye point is more than 5 cm, for a whole ball less than 1 cm;
- `test_eyes_and_spheres_in_step` - spheres, material `eye_glow`, sockets with `game_socket` and +Z outward
  in the output GLB;
- `test_front_projection_synthetic` - a disc "red left and blue right half" on a sphere: the colors
  land on their own sides, hidden faces go to the second slot.

## Limitations

- A single photograph: the back of the head and hidden sides are the generator's guess; the texture is taken only from
  faces looking at the camera, the rest is one color without cracks. `qa.reviewed` is not set,
  the check of hidden surfaces is still not done.
- The projection is a photo with baked-in light, not PBR. On grazing faces it is stretched; the color there
  fades smoothly into the fill, but ragged spots in the folds at the cheekbone are visible in 3/4.
- Without rotation the step assumes a common camera for the crop and the body, i.e. a detail from the same picture. For a
  detail from another viewpoint `icp_rotation` or `part_rotate_z_deg` is needed, and the comparison measure with the crop does not
  take rotation into account.
- Eye search is a heuristic for a skull or face seen from the front: two hollows next to each other are required,
  rotation up to ±45°. Profiles and one-eyed creatures are not supported.
- The seam is a bridge between the neck ring of the body and the section of the vertebrae of the detail: an almost
  flat ribbon, covered by the collar in front. There is no local voxel welding or color re-baking at the seam: the bridge
  faces stretch the color of the neck edge. The generator's vertebrae above the old neck look lumpy.
- The detail is not built into the body atlas: the materials `part_front_projection` (its own picture
  of about 845×1024), `part_back_fill` and `eye_glow` are added. A common re-bake may be needed for game
  export.
- Mesh: about +13.5 thousand triangles, 6 separate parts instead of 4 (two eye spheres). The body is welded
  (per-loop UVs are kept), custom normals are replaced by smoothing at a 40° angle.
- (fixed 2026-10-10, evening) `socket_fx_eye_*` now survive `cleanup`, `separate`, `place_parts` and
  `rig_humanoid` (there they become children of the `Head` bone) and reach the Godot package.
- In the studio the limit of 2.5 million triangles rejects ROI generations of `high` quality. The raw GLBs had to be
  taken from the Comfy output folder; this is a decision for the integrator: a separate limit for ROI or
  `quality: standard`.
- All similarity numbers are for one viewpoint. They show that the new head is closer to the reference from the front, but
  they are not a percentage of 3D accuracy.
