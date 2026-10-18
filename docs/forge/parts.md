# Generation by parts (`scripts/parts/`, step `assemble_parts`, 2026-10-13)

In our ComfyUI, Hunyuan3D-2mv builds the whole body as a single latent: `VAEDecodeHunyuan3D` with `octree_resolution 512` splits
the bounding box of the entire figure into 512 voxels along its long side, so the head (~0.27 m out of 1.8) gets ~70 voxels
and a hand gets only a handful. The idea: cut the head, torso, arms and legs out of the references separately, generate each
part from a frame that **fills the generator's frame** (the same 512 voxels per part), then place the parts onto the whole
mesh and fuse them into one model.

Tested on a test humanoid character (`<work dir>/mv/`): the head came out noticeably better than in the whole mesh
(helmet crest and cheek guards, nose, eyes, mouth), the torso and arms are on par with the whole mesh, the legs are on par
but worse in silhouette. Details and numbers are in the "Measurements" section.

## Files

| File | What |
|---|---|
| `scripts/parts/parts_prep.py` | cutting: part boxes from the silhouette, `parts/<part>/mv_front.png`, `mv_back.png`, `parts.json`, `boxes_preview.png` |
| `scripts/parts/parts_generate.py` | Hunyuan3D-2mv for each part through ComfyUI, `parts/<part>/mesh_<seed>.glb` |
| `scripts/parts/build_parts.py` | one command: cutting -> generation -> assembly in Blender, prints the GLB path and a report |
| `scripts/blender/steps/assemble_parts.py`, `_parts_lib.py` | the Blender step "Assembly by parts" |
| `server/forge-steps.mjs` | registry (module `parts`, step 28), `server/forge-pipeline.mjs` — inputs `concept` and `parts` |
| `scripts/blender/tests/test_assemble_parts.py` (8 tests), `tests/forge-steps.test.mjs`, `tests/forge-pipeline.test.mjs` | tests |

The cutting and generation scripts run on Python from the ComfyUI venv (`<ComfyUI dir>/.venv/Scripts/python.exe`: numpy, scipy, PIL),
the assembly step runs in Blender 5.0 (numpy only).

## Running

```
<ComfyUI dir>/.venv/Scripts/python.exe scripts/parts/build_parts.py ^
  --front C:/.../mv/ref_front.png --back C:/.../mv/ref_back.png ^
  --base  C:/.../mv/mesh_mv_703.glb --out C:/.../parts ^
  --seeds 703 --mv-out C:/.../mv/mesh_mv_parts.glb
```

Finished part meshes are reused (`--force` regenerates; `--skip-prep`, `--skip-generate` run only the following stages;
`--param voxel_m=0.003 --param target_tris=60000` set step parameters). Stage by stage:

```
parts_prep.py     --front ref_front.png --back ref_back.png --out parts/ [--boxes boxes.json] [--margin 0.045] [--parts head,legs]
parts_generate.py --parts-dir parts/ [--seeds 703 11 | head=703,11 torso=5] [--only head] [--force]
blender -b --factory-startup --python scripts/blender/af_run.py -- assemble_parts request.json
```

The step's `request.json`: `{"step": "assemble_parts", "inputs": {"mesh": "<whole mesh>", "concept": "<ref_front.png>",
"parts": "<parts/parts.json>"}, "params": {...}, "out_dir": "..."}`. Through the forge: `parts_file` and `concept_file` are
local paths, available only through the CLI `--direct` (they cannot be set over HTTP); `concept_asset` is a studio image; without
it the asset's generation reference image is used. The step comes after the whole mesh has been obtained (`humanoid`); after it
comes `cleanup` as usual (see "How to feed the result into the character build").

Requirements for the references: **T-pose, facing -Y** (the character's left = +X = the right side in the front view), front and back references
as RGBA (alpha = the figure) of the same height, as produced by `prep_mv.py`.

## How it works

### 1. Cutting (`parts_prep.py`)

Everything is based on the silhouette of the front reference (alpha >= 128, small debris under 2 % is discarded):

- **head** — from the top of the head to the neck: the shoulders are the first row that is wider than everything above it by a factor of 1.45; the neck is the narrowest
  row between the widest row of the head (helmet, hood) and the shoulders;
- **arms (T-pose)** — the rows where the silhouette is one and a half times wider than the torso; the torso width and its axis are taken at chest level right under the
  arms; an arm is the columns beyond the torso edges and the rows it occupies;
- **torso** — from the neck to the hem of the upper garment (the row where the legs diverge: going up from the feet, the last row with two
  separate legs), width — at the chest plus a margin, and at the hem if the hem is wider;
- **legs** — from that same row down to the soles.

At every joint the parts overlap each other by `margin` = **4.5 % of the height** on both sides (joint overlap 9 %): the assembly
has something to align and fuse. The back-view boxes are the front boxes mirrored about the torso axis of each view
(the character's left is on the right in the front view and on the left in the back view), followed by a small shift search (+-1.5 % of the height) by the IoU
of the mirrored front cut against the back one (on the test character the arms went 0.94 -> 0.96-0.97, the other parts unchanged). To use your own box instead of the automatic one:
`--boxes boxes.json` (`{"head": [x0, y0, x1, y1], ...}` in pixels of the front reference, or a `parts.json` itself).

For each part: a shared frame for the front and back views (by the bounding box of the silhouette inside the box, the same size for both views, each with its own centre),
the figure outside the box is removed from alpha, the long side of the frame = 1280 px at the centre of a white 1536 px canvas — the input format of
`gen_mv.py`. `parts.json` holds the boxes in pixels of the references (front and back), the bounding box of the silhouette in the box, the
cut -> canvas transform (`frame`: size, scale, offsets), reference rows (`landmarks`), and the mesh paths.

Resolution gain: the head is cut out with a magnification of x5.9 (frame 191x217 px -> 1280), the torso x1.9, the arms x2.7, the legs x2.8
(the whole body — x1.1).

### 2. Generation (`parts_generate.py`)

The graph is word for word that of `gen_mv.py` (ImageOnlyCheckpointLoader -> CLIPVisionEncode front and back ->
Hunyuan3Dv2ConditioningMultiView -> EmptyLatentHunyuan3Dv2(3072) -> KSampler 45 steps, cfg 5 -> VAEDecodeHunyuan3D (octree 512)
-> AstraMarchingCubes -> SaveGLB). GPU: one request at a time; before each one it checks `GET /queue` and waits for an empty
queue (someone else's job), and `/free` and `/interrupt` are never called. Seeds: `--seeds 703` (default) for all parts,
`--seeds head=703,11` — several for one part. Result: `parts/<part>/mesh_<seed>.glb`; `parts.json` gets `meshes` and the
selected `mesh`.

### 3. Assembly (`assemble_parts`)

1. **Whole mesh**: join it, scale to `height_m`, feet at z = 0, centre in X/Y (like `cleanup`). It is better to take the raw Hunyuan mesh
   (`mesh_mv_703.glb`, 715 thousand triangles) rather than the 24-thousand `clean.glb`: it is needed for depths and gap filling,
   details matter, and cleaning would already have eaten them. The mesh and the parts are welded by vertices (glTF splits vertices along normal seams).
2. **Camera**: orthographic, strictly from the front (yaw and pitch = 0), scale and shift fitted by IoU to the silhouette of the
   front reference (Nelder-Mead at 200/400/800 px, like the `paint_from_concept` fit without rotation). Pixel boxes of the
   parts -> boxes in metres. IoU of the whole mesh with the reference is 0.9375.
3. **Placing a part** (the part mesh is in the generator's coordinate system: box centre at zero, long side 2):
   - **X / Z — by silhouette.** Per-axis scale and shift: the initial ones come from the bounding box of the silhouette in the box, then Nelder-Mead
     by the IoU of the part's front projection with the reference silhouette inside the box. The reference is the truth for the front view: the part was generated
     exactly from this cut, while the whole mesh is less accurate there (the helmet is smaller and blurrier: the silhouette IoU of the whole mesh is 0.9375,
     for the parts inside their boxes 0.91-0.98). The generator does not preserve proportions: for the torso with sleeve stumps the width came out 15 %
     larger than in the image — hence per-axis scale rather than uniform.
   - **Y (depth) — from the whole mesh**, because the images do not define depth. The shift is the median of the difference of the depth maps
     (front and back surface) of the part and the whole mesh over the pixels visible in both; the Y scale is the front-to-back thickness of the whole mesh
     (`depth_scale: base`, within 0.8-1.25): for the test character the whole mesh is 9-24 % thicker than the parts, and without
     this neighbouring parts would meet in steps. `part` — the part's own proportions.
   - **ICP** (`refine: icp`) — symmetric trimmed ICP, a rigid rotation of at most `icp_max_rot_deg` + uniform scale
     <= `icp_scale_range`, against the surface of the whole mesh in the box expanded by `box_expand` — in the same boxes it **measured worse than the
     default** (see "Measurements"): the whole mesh pulls the part toward its own errors. The residual (trimmed RMS and
     median of part <-> whole mesh distances, the share of points within 5 mm) is computed in any mode.
   - **Flip**: the part is also tried rotated by 180° around Z (the generator might have put the face on the back); it is taken if the
     residual drops by at least 40 %.
4. **Joints.** Where the boxes of two parts overlap in a slab (thickness 5-20 % of the height, long side no shorter than 1.2
   of the thickness; corner touches such as "helmet bust — sleeve" are not a joint), each part is cut along the line of the slab where the cross-sections of both
   parts match best (the sum of differences of the cross-section extents along the other two axes in a thin slice of +-8 mm, within 70 % of the
   slab), +-`joint_overlap_m` (2 voxels): the parts overlap only in a thin layer. The boolean operation uses the Manifold solver
   (0.7 s per 700 thousand triangles), Exact is the fallback. Without this, a full overlap of the margins shows every surface
   mismatch: the ends of the torso's sleeve stumps stuck out of the arm as a flat "patch". Small islands (floating debris from the
   generator with fewer than `island_min` vertices) are removed.
5. **A part without a mesh** (not generated, failed, or could not be placed): the whole mesh inside its box takes its place
   (boolean intersection with a box over the full depth, flat cuts along the box faces — neighbouring parts reach into this box,
   so the volumes overlap).
6. **Fusion**: all parts and pieces of the whole mesh are joined and go through a voxel remesh (union: overlapping shells
   disappear), optionally simplification to `target_tris`, normals, angle-based smoothing, scaling to `height_m`, feet at z = 0.

Outputs: `assembled.glb`, `assembled.blend` (Body + a hidden whole mesh), `preview.png` (a row of the whole mesh above a row of the
assembly: front, back, side, head front and three-quarter), `parts_fit.json` (per part: per-axis scale, shift,
IoU in the box before/after, depth shift and thickness, residuals, cuts; joints; camera; IoU) and, with `export_mv`,
`assembled_mv.glb` in Hunyuan normalisation (box centred at the origin, long side 2, vertices without splits along
normals). Checks: `parts_found`, `watertight` (error on open edges), `manifold`, `no_nan_verts`,
`height_matches`, `grounded`, `silhouette_iou_min` (>= 0.85, error), `silhouette_iou` (not lower than the whole mesh by more than
0.01), `part_residual_<part>` (residual > `icp_warn_m` = 12 mm) and `part_flipped_<part>` (warnings).

## Parameters of the `assemble_parts` step

All are optional; names and hints are in the registry (`server/forge-steps.mjs`).

| Parameter | Default | |
|---|---|---|
| `height_m` | 1.8 | height of the whole mesh and of the result |
| `voxel_m` | empty = 0.0016 x height (2.9 mm) | fusion voxel. The whole body is generated with a ~3.5 mm step, the head with ~0.5 mm: a coarser voxel (`cleanup` uses 0.0033 x height = 6 mm) washes out the gain from the parts. At 4 mm the result is 0.6 M, at 2.9 mm 1.18 M triangles |
| `target_tris` | empty | simplification of the result |
| `refine` | `silhouette` | `silhouette` \| `icp` \| `none` |
| `depth_scale` | `base` | `base` \| `part` \| a multiplier |
| `trim_overlap`, `joint_overlap_m`, `island_min` | yes, 2 voxels, 0.002 | joint trimming, share of vertices for small islands |
| `box_expand`, `icp_iters`, `icp_trim`, `icp_samples`, `icp_max_rot_deg`, `icp_scale_range` | 0.012, 40, 0.8, 15000, 6°, [0.8; 1.25] | ICP target and limits |
| `icp_warn_m` | 0.012 | residual warning threshold |
| `try_flip`, `flip_rms_m` | yes, 0 | check for a 180° flip (0 — always) |
| `fill_gaps`, `gap_min_tris` | yes, 1500 | pieces of the whole mesh in place of parts without a mesh |
| `camera` | `fit` | or `{px_per_m, offset_px, centre}` from `parts_fit.json` |
| `pick` | `{}` | `{part: seed | path}` — which mesh to take |
| `export_mv`, `shade_auto_smooth_deg`, `preview`, `preview_size`, `head_closeup` | no, 40, yes, 384, yes | |

## Measurements on the test character

Inputs: `mv/ref_front.png`, `ref_back.png` (1371², figure 1066x1136 px), whole mesh `mesh_mv_703.glb`
(715 thousand triangles; IoU with the front reference 0.9375). Seed 703 for all parts. Map: `parts/`.

**Boxes (pixels of the front reference)**: head [589, 107, 779, 333], torso [511, 231, 857, 899], arm `arm_l` (on the right in the
front view) [750, 306, 1228, 451], `arm_r` [140, 306, 618, 451], legs [499, 797, 871, 1264]; neck row 282, hem/leg
fork row 848; joint overlap 102 px (9 % of the height), `boxes_preview.png`.

**Generation** (RTX 4070): 156-160 s per part, 788 s for five parts (+156 s for repeating the torso after the box changed: the first torso
box was wider by 2x45 px because of the lower edge of the sleeves, this is fixed in `parts_prep.py`). Meshes: head 2.37 M
triangles (42 MB), torso 1.22 M, arms 0.47 M each, legs 1.78 M (face spacing ~0.5 mm for the head versus ~3.5 mm for the whole).

**Assembly** (each part, defaults): per-axis scale (X, Y, Z), silhouette IoU in the box, residual against the whole mesh:

| part | scale X / Y / Z | IoU in the box | RMS, mm | median, mm | thickness whole/part |
|---|---|---|---|---|---|
| head | 0.178 / 0.205 / 0.174 | 0.957 | 7.7 | 7.9 | 1.16 |
| torso | 0.538 / 0.594 / 0.553 | 0.980 | 9.3 | 9.6 | 1.09 |
| arm `arm_l` | 0.383 / 0.450 / 0.368 | 0.959 | 6.3 | 6.2 | 1.20 |
| arm `arm_r` | 0.389 / 0.472 / 0.375 | 0.953 | 6.2 | 6.0 | 1.24 |
| legs | 0.346 / 0.428 / 0.383 | 0.914 | 11.0 | 11.1 | 1.18 |

All residuals are below the 12 mm threshold, no flipped parts. Generator aspect deviation: torso X/Z 0.99 (after the box change;
the first version 0.87), legs 0.92 (the weakest spot).

**Result**: silhouette IoU with the front reference **whole mesh 0.9375 -> assembly 0.9575** (assembly/whole mesh 0.921: the figure
is assembled to the proportions of the reference, while the whole mesh is wider than it by ~3 %). The result is 1 183 272 triangles (voxel 2.88 mm), closed
(0 open edges, 0 non-manifold), 1 piece, height 1.8 m. Assembly time 71 s: whole mesh 1.3, camera 2.9, part placement
24.9, joint trimming 29 (boolean operations over 2.4 M), voxel remesh 1.9, metrics 3.8, GLB 1.7, preview 1.9.
The whole `build_parts.py` on ready meshes — 76 s.

**Fit modes** (voxel 4 mm; assembly IoU with the reference, IoU of the parts in the boxes before -> after ICP):

| mode | assembly IoU | note |
|---|---|---|
| `silhouette` (default) | 0.954 | |
| `icp` | 0.951 | IoU in the boxes head 0.957 -> 0.921, legs 0.905 -> 0.890, head rotation 4.4°, RMS smaller (7.4 -> 6.0 mm): ICP against the whole mesh improves "agreement with it", but pulls the part away from the reference |
| `none` (bounding boxes) | 0.954 | |
| `depth_scale: part` | 0.953 | RMS to the whole mesh 9.7-10.5 mm instead of 6-9 |
| without joint trimming | 0.955 | same geometry, but patches on the shoulders |

**Are the parts better than the whole mesh?** The head — yes, and it is visible: in the whole mesh the helmet is smooth, without the crest and cheek guards of
its shape, and the face is only a nose bump; in the assembly the helmet has a vertical bar, cheek guards, eyes, eyebrows, mouth and a collar (`compare.png`, and
after simplification to the same 24 thousand triangles as in the character build, `compare_24k.png`: the simplified head stays
recognisable, the whole mesh's does not). Torso, arms: the shape matches the whole mesh, with slightly crisper edges of the sleeves and hem, and the belt. Legs:
the hem and boots are crisp, but the silhouette is worse than the reference (IoU 0.914 versus 0.98 for the torso) — the generator narrowed the legs by 8 %. The overall silhouette
IoU rose by 2 points, mostly thanks to the head, shoulders and torso.

## How to feed the result into the character build

`mv/mesh_mv_parts.glb` in the work directory is the result in Hunyuan normalisation (bbox centred: X +-0.953,
Y +-0.225, Z +-1.0; for `mesh_mv_703.glb`: X +-0.968, Z +-0.999), 1.18 M triangles, 21 MB, without vertex splits. It passes
`cleanup` (checked with the `cleanup` step at `height_m 1.8`, `target_tris 24000`: 18 s,
IoU before/after 0.998, checklists green; output — `parts/cleanup24k/clean_from_mesh_mv_parts.glb`). `cleanup` without `voxel_remesh_m`
only simplifies; with a voxel remesh of 0.0033 x height the head details get washed out — in that case set `voxel_remesh_m` to no more than 0.0016 x height.
Painting from the concept (`paint_from_concept`) works on an ordinary model: the parts carry no colour.

## Limitations

- **T-pose only, facing -Y**, front and back references of the same height and a shared frame. Part boxes and depth are made
  for a humanoid; creatures, robots, or a figure with props need their own boxes (`--boxes`).
- **Depth is not guaranteed**: the images do not define it, it is taken from the whole mesh (thickness, shift) — the parts are thinner than the whole
  by 9-24 %, but which of them is right is unknown without a side view.
- **Steps at joints**: independently generated surfaces diverge by 5-15 mm; at the root of the arm a thin step is visible
  (side view), at the neck almost none. Joint trimming and seam-line selection remove flat patches but do not smooth the steps themselves
  (seam smoothing is a possible improvement).
- **Legs** are worse than the other parts by silhouette (IoU 0.914, generator aspect 0.92); for the torso with the first box the width came out 15 %
  larger than the image.
- **Size**: the parts weigh tens of megabytes (head 42 MB, 2.4 M), joint trimming takes about 30 s, the whole step ~1.2 min.
- Manifold boolean operations require closed meshes: Hunyuan meshes are such (checked), others go through Exact (slow).
- The default fusion voxel is 2.9 mm: head details finer than that (0.5 mm) are not preserved in the final mesh; `voxel_m` 0.002 gives
  ~2 M triangles.
- The GPU is busy for ~13 min on five parts: cutting and assembly run on the CPU.

## Tests

`blender -b --factory-startup --python scripts/blender/tests/run_tests.py -- assemble_parts` — 8 tests (camera, joints, seam
line selection, export in Hunyuan normalisation, head placement with aspect and thickness errors, a part rotated facing backward,
`icp` / `none` modes, missing parts.json and the no-parts mode), 36 s. Node: `node --test --test-reporter=tap tests/*.test.mjs`
— 167 tests (+1 registry, +1 pipeline: `parts_file` / `concept_file` CLI only, the `parts` input).
