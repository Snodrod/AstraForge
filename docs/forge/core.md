# Module core: mesh preparation and the shared test run

The Blender steps `cleanup`, `convert`, `separate` (contract §3–4), their shared helpers
`scripts/blender/steps/_mesh_lib.py`, the shared self-test runner `scripts/blender/tests/run_tests.py`
and the Node wrapper `tests/integration/blender.test.mjs`.

Files: `steps/cleanup.py`, `steps/convert.py`, `steps/separate.py`, `steps/_mesh_lib.py`,
`tests/run_tests.py`, `tests/test_core.py`, `tests/integration/blender.test.mjs`.

## Inputs (shared by the three steps)

- The source is `inputs.mesh` **or** `inputs.blend` (.glb/.gltf/.fbx/.obj/.stl/.blend). After a step
  that wrote a .blend, the pipeline passes only `{blend}`, so the chains "cleanup → separate" and
  "… → convert" work through `af_run.py`. If both are given, `mesh` wins.
- If neither is given: `ValueError: inputs.mesh or inputs.blend is required`; if the file is missing:
  `FileNotFoundError: inputs.mesh not found: <path>`.
- Right after import, the importer's helper objects are removed: bone shapes and everything in the
  `glTF_not_exported` collection (for a GLB with a skeleton this is a 2 m wide `Icosphere`, which used
  to end up in the bounding box: height 2.7 m instead of 1.7, an extra mesh in the FBX, a false `small`
  part). The removed names are in `stats.import_helpers_removed`.
- Rig: `cleanup` and `separate` work on static geometry, so the armature and weights are dropped (with
  a warning); `convert` keeps the rig and animation (except for OBJ and Mixamo).
- Before the .blend is saved, unused data is purged (`orphans_purge`): after a remesh, `clean.blend`
  no longer contains the original 4K map (7.7 MB instead of ~26 MB).

## cleanup: from an AI mesh to a working mesh

Order: import → remove helper objects → flatten the hierarchy (meshes in world coordinates, empties
and rig removed, transforms applied) → merge into one object `Body` → rotate, scale to height, bottom
at `z=0`, center X/Y → "before" snapshot → drop custom normals, weld, remove wire edges, lone vertices
and small islands → **either** decimation, **or** voxel remesh + bake → normals and smoothing (with
remesh, **before** the bake) → renormalize (height and `z=0` exact) → export.

| Parameter | Default | What it does |
|---|---|---|
| `height_m` | none (size as is) | scale so the bounding-box height equals the value |
| `rotate_z_deg` | 0 | rotation about Z before everything else (a Tripo creature that faces +X needs `-90`) |
| `ground`, `center` | true, true | bottom at `z=0`, bounding-box center at X=Y=0 |
| `join`, `name` | true, `Body` | merge all meshes. With `join:false` the budget is split in proportion to each object's triangles (with remesh, **after** the remesh); an object gets no less than min(its own tris, 64), so small low-poly pieces are left alone |
| `merge_distance` | `1e-5 × height` | weld (glTF splits the mesh along UV seams); 0 means no weld |
| `remove_islands_below` | 0 | remove pieces whose vertex count is below this fraction of the total |
| `target_tris` | none | decimate to ≤ the value: a planar pass (`planar_angle_deg`, 0.5°) + staged collapse (each stage keeps at least `decimate_stage_ratio` = 0.3) |
| `symmetry` | null | `'X'|'Y'|'Z'`: symmetric collapse |
| `seam_weight` | off | extra seam protection via a vertex group (measured to be worse, so it is off) |
| `voxel_remesh_m` | 0 | voxel remesh; recommended ≈ `0.0033 × height`, a warning above `0.004 × height` |
| `bake_size`, `bake_device`, `bake_samples` | 2048, `CPU`, 4 | Cycles selected-to-active bake |
| `bake_normal`, `bake_emission` | false, false | additionally bake normal (tangent) and emission. With `bake_normal` the remeshed mesh is shaded **fully smooth** (the normal map carries the creases), and `shade_auto_smooth_deg` is not applied to it |
| `bake_extrusion_m`, `bake_ray_m` | `max(2·voxel, 0.005·height)`, `2·extrusion` | cage and ray length |
| `shade_auto_smooth_deg` | 40 | smooth shading with edges sharp above the angle; 0 means flat; null leaves it as it was |
| `recalc_normals` | true | make face directions consistent, outward |
| `preview`, `preview_size` | true, 512 | "before | after" sheet (front and 3/4, identical framing) |

When color is baked after a remesh (`stats.bake.colour_source`): `texture` is the Base Color map;
`linked` means any node feeds Base Color (a color attribute, a ramp, ...; the Remesh modifier loses
color attributes); `materials` means the faces have several different base colors (the remesh keeps
one slot); `attribute` means there is no material but there are vertex colors (a temporary material is
set up for the bake). A single flat material has nothing to bake, so the material stays. A color
attribute that no material uses is not baked: glTF and Godot would not show it either.

With a remesh the order is: remesh all objects → split the budget by the new triangle counts →
decimation → triangulation → `recalc_normals` and shading → Smart UV → bake. This way the shipped
mesh is shaded exactly like the one the normal map was baked on (previously the 40° sharp edges
appeared after the bake, producing facets on the skull and hood).

The Workbench preview picks the color mode itself: `TEXTURE` (there is a color map), `VERTEX` (vertex
colors used by a material, or a mesh without material), otherwise `MATERIAL` (a flat color is taken
from the Principled node for the render). So a loss of vertex colors is now visible in
`render_color_mae`. The "after" frame of the sheet shifts together with the model when it is
re-centered: previously removing an island moved the model, and the IoU of a synthetic sphere was
0.668 with the shape unchanged; now it is 0.99+.

Outputs: `clean.glb`, `clean.blend`, `preview.png`, and with a remesh `baked_base_color.png`
(+`baked_normal.png`, `baked_emission.png`; with several objects `baked_<i>_…`).
`stats`: `before` (as received), `welded` (after welding), `after`: triangles, vertices, connected
pieces, boundary and non-manifold edges, NaN, bounding box; `decimation` (stages with timings),
`budgets`, `textures_before/after`, `render_compare`, `bake`, `shading`, `sharp_edges`,
`import_helpers_removed`, `orphans_purged`, `timings_s`.
`checks` are errors: `tris_within_target`, `no_nan_verts`, `height_matches`, `grounded`,
`base_color_texture_kept`, `uv_kept` (without remesh), `silhouette_iou_min` (IoU ≥ 0.85, otherwise
the shape fell apart); warnings: `centered`, `bake_coverage`, `silhouette_iou ≥ 0.97`,
`render_color_mae ≤ 0.08`.

Remesh + bake is a **re-bake of the existing color** onto a new unwrap, not a reconstruction of PBR:
roughness and metalness are not transferred (a constant roughness of 0.8), and a photo projection
stays a projection.

## convert: to GLB / FBX / OBJ and FBX for Mixamo

| Parameter | Default | What it does |
|---|---|---|
| `format` | `glb` | `glb`, `fbx`, `obj` |
| `name` | `model` | output file name |
| `height_m`, `rotate_z_deg` | none, 0 | scale and rotation |
| `ground`, `center` | true if a height or Mixamo is set, otherwise false | a plain conversion does not move the model |
| `mixamo` | false | FBX for upload to Mixamo |
| `mixamo_max_tris` | 50000 | triangle budget |
| `mixamo_weld_m` | 1e-4 | weld the seams (the Mixamo auto-skinner fails on a mesh split along UVs) |
| `mixamo_center` | `bbox` | `head` puts the X axis through the middle of the head (a staff or shield at the side does not shift the Mixamo axis) |
| `texture_max` | 2048 | texture side limit; if the FBX is > 10 MB, textures are halved down to 512 |

Mixamo FBX: a single `Body` mesh without a rig, facing −Y in Blender (FBX +Z), feet at zero, default
height 1.8 m, FBX units in centimeters (`FBX_SCALE_ALL`), only the color map in the material, the
texture embedded in the file. A plain conversion keeps the hierarchy and empties (sockets); OBJ writes
only meshes and puts the maps in `textures/`. Blender's FBX exporter does not embed packed images
without a path (which is how textures arrive from glTF), so the step writes the maps to
`out_dir/textures/` and relinks the nodes. Checks by re-importing the written file: `roundtrip_tris`,
`roundtrip_meshes` (the same number of meshes, which catches a stray `Icosphere`), `roundtrip_height`,
`no_nan_verts`, `roundtrip_base_color`, `roundtrip_armature` (if there was a rig); with `height_m`
(and for Mixamo, 1.8 m) also `height_matches` (the height after import against `params.height_m`,
tolerance 1e-3) and `grounded` (bottom at zero); for Mixamo, a single mesh and ≤ 10 MB.

## separate: split into named parts

| Parameter | Default | What it does |
|---|---|---|
| `mode` | `loose` | `loose` splits into connected pieces; `material` by material; `regions` by boxes |
| `height_m`, `rotate_z_deg`, `ground`, `center` | none, 0, true, true | normalization BEFORE the split; boxes are given in final meters |
| `merge_distance` | `1e-5 × height` | weld, otherwise `loose` breaks the mesh into UV islands |
| `min_fraction`, `drop_small`, `names` | 0.01, false, — | (`loose`) small stuff is collected into a part `small` or dropped |
| `regions` | — | `[{"name","min":[x,y,z],"max":[x,y,z]}]` **in meters**; equal names mean a union of boxes |
| `rest_name` | `body` | name of the remainder |
| `origin` | `world` | `center` / `bottom` puts the object origin at the center / bottom of the part |
| `caps` | true | caps on the cuts of `part`/`delete` regions; a region can set its own `caps` |
| `slit_gap_m` | 0.001 × height | separation of the edges of a tear (`slit`, `width_m: 0`) |
| `regions[].kind` | `part` | `part` / `delete` / `slit`, details below |

Regions: the mesh is cut by the box planes (bisect only where faces touch the box), the faces inside go
to the part, and the cuts are closed with caps on both sides (`holes_fill` + triangulation; a cap
takes the UV and material of the neighboring edge, flat shading, normal outward). Edges that were open
in the source are not closed by caps: **a part is closed only if the source was closed** in that area.
Outputs are `parts.glb` (one object per part), `parts.blend`, `preview.png` (parts in random colors |
an "exploded" view with texture); `stats.parts` holds triangles, bounding box, pieces, boundary and
non-manifold edges, `closed` and the volume (for closed parts only).

### Region kinds: part, delete, slit without caps (2026-10-10, evening)

Every region has a `kind`; the same name and kind form one union of boxes.

| `kind` | What it does | Region parameters |
|---|---|---|
| `part` (default) | as before: the faces go to a separate part | `caps`, `facing` |
| `delete` | the faces are deleted, there is no part (an old staff, "shovels", the hidden bottom of a robe) | `caps`, `facing` |
| `slit` | an **open cut**: nothing stitches the two sides back together | `plane` or `curve`, `width_m`, `gap_m`, `caps` |

- `caps` (default `params.caps = true`). `false` leaves the cut edges open on both sides. This is how
  a robe hem is opened: `{"kind": "delete", "caps": false, "facing": [0, 0, -1]}`.
- `facing` + `facing_min` (0.5): only faces whose normal · `facing` ≥ `facing_min` count as inside.
  This is the downward-facing bottom of a robe without the side wall of the hem.
- `slit`, `width_m: 0` (default) is a **tear**. Faces that touch the box and cross the cut surface are
  split. New edges on the surface inside the box are split apart (`split_edges`): nothing is deleted,
  and the two sides keep coinciding open edges. Where the cut leaves the box, the mesh stays whole,
  which is the end of the slit. `gap_m` (default `params.slit_gap_m = 0.001 × height`) moves each copy
  of the edge by `gap/2` into its own faces. Without it, a rig proxy with a 1e-4 × height weld would
  stitch the slit back together.
- `slit`, `width_m > 0` is a **slab**. A strip of faces `width_m` wide around the surface is deleted,
  and the edges are cut exactly along the offset surfaces. This is how a fused bridge is cut whose
  pinch lines wander a few millimeters away from the plane: on one test sleeve they wander by 2–7 mm.
  With `caps: true` each side is closed by **its own** cap. The edge chains of a side (the front and
  back fold) are stitched by a strip of triangles along the height of the cut, and a U-shaped chain is
  cut at the far end and stitched the same way. There are no long skewed triangles stretching between
  the elbow and the shoulder. The two sides are not connected; only the ends of the slab stay open.
- The cut surface: `plane` is `{"axis": "x", "value": v}` or `{"point", "normal"}`, by default `x` =
  the box center. `curve` is `{"axis": "x", "along": "z", "points": [[z0, x0], [z1, x1], …]}`, a
  piecewise-linear `x = f(z)` extruded along the third axis. The mesh is first cut at the strip
  boundaries, then each strip by its own plane, so the cut is continuous.
- `mode: "slit"` is the same as `regions`, but the default kind is `slit`.
- Report: `stats.regions` under the keys `<name>`, `delete_<name>`, `slit_<name>`: split faces, torn
  edges or deleted faces, edge length, caps per side (`caps["+"/"-"]`, chain lengths), open edges.
  Checks: `slit_<name>_cut` (an error if nothing was cut), `slit_<name>_capped` and
  `delete_<name>_capped` (warnings), `delete_<name>_nonempty`.

**Sockets are preserved.** `cleanup`, `separate` (and `place_parts`, `replace_part`) no longer discard
the empties named `socket_*` or carrying `game_socket` (for example `socket_fx_eye_l/r` from
`replace_part`). They are recorded before the hierarchy is flattened (`_mesh_lib.socket_records`),
returned as root empties (`restore_sockets`), go through the same normalization as the mesh (without
scale), and are exported together with the parts. Their names are in `stats.sockets_kept`.

## Running

```
"C:/Program Files/Blender Foundation/Blender 5.0/blender.exe" -b --factory-startup \
    --python scripts/blender/af_run.py -- cleanup request.json
```
```json
{"inputs": {"mesh": "C:/.../character.glb"},
 "params": {"height_m": 1.8, "target_tris": 30000},
 "out_dir": "C:/.../out"}
```
The next step from a .blend: `{"inputs": {"blend": "C:/.../out/clean.blend"}, "params": {...}}`.
A staff held by the test character (the boxes were found by slicing the mesh every 5 cm; see
`STAFF_REGIONS` in `test_core.py`):
```json
{"mode": "regions", "height_m": 1.8, "regions": [
  {"name": "staff", "min": [0.285, -0.37, -0.01], "max": [0.52, -0.17, 0.70]},
  {"name": "staff", "min": [0.285, -0.37, 0.70],  "max": [0.52, -0.21, 0.99]},
  {"name": "staff", "min": [0.38, -0.38, 1.22],   "max": [0.66, -0.16, 1.81]}]}
```
Tests:
```
blender -b --factory-startup --python scripts/blender/tests/run_tests.py -- core [-k real] [--list]
node --test tests/integration/blender.test.mjs          # all modules; FORGE_TEST_MODULE=core for one
```
`run_tests.py` finds `test_*.py`, calls `af_common.reset_scene()` before each `test_*`, prints
`PASS/FAIL/SKIP name seconds`, and at the end `TESTRESULT {json}`; the exit code is 1 on any failure
(and for a module that fails to import). SKIP is a `unittest.SkipTest` or a returned string that
starts with `skip`. The Node wrapper looks for Blender (`ASTRA_BLENDER` or the default path) and is
skipped without it; the timeout is 40 minutes, and the filters are `FORGE_TEST_MODULE`,
`FORGE_TEST_FILTER`. It lives in `tests/integration/`, so `npm test` (`tests/*.test.mjs`) does not
touch it.

## Measurements (Blender 5.0.1, CPU 32 threads, RTX 4070; 2026-10-10, fix round)

Timing: "fresh process" is `timings_s.total` from `result.json` of a separate `af_run.py` run
(without Blender startup, ~1–2 s); "warm" is the same run inside `run_tests.py`. The fixtures below
are test assets, so some characters are named only generically.

| Run | Result |
|---|---|
| Test character, decimation 120k → 30k | 119 997 → 29 939 tris; fresh process 3.9 s (decimation 1.1 s), warm 3.0 s; weld 26 835 vertices: 2447 pieces → 4, boundary edges 49 646 → 22, non-manifold 1 → 12; the 4096² color map and the normal/roughness/metallic/emission maps are kept; silhouette IoU 0.998, color difference 0.031 |
| Same, seam protection by a vertex group | worse: color difference 0.035–0.038 against 0.025 without it (30k); at weight 0 the collapse gets stuck at 52 659 tris (measured in the previous round) |
| Test character, 1.1M tris (high-poly variant) → 30k | fresh process 21.3 s, warm 20.0 s: weld 3.0, planar pass 5.1 (1 107 672 → 952 284), collapse 7.0 + 1.9 + 0.5 s (952 284 → 285 684 → 85 704 → 29 940); 3 islands (140 vertices) removed; IoU 0.998 |
| Test character, voxel 6 mm → 12k + bake 2048 (color + normal) | fresh process 10.2 s: remesh 0.2, decimation 3.0 (385 180 → 115 554 → 34 666 → 11 976), Smart UV 0.1, bake 1.8 + 1.5 s (CPU); ray misses 0.15 %; 11 976 tris, boundary and non-manifold edges 0, 5 pieces; normals aligned before the bake (4 faces flipped), sharp edges 0; IoU 0.994, color difference 0.058; `clean.blend` 7.7 MB |
| Same without `bake_normal` | 40° sharp edges (3385), color difference 0.045: the rise to 0.058 above comes only from fully smooth shading, as Workbench does not show the normal map |
| Same, voxel 12 mm (previous round) | holes and ragged edges on a thin hand, IoU 0.980, difference 0.060, hence the warning above 0.4 % of height |
| Tripo creature 19 988 → 7 984, rotation −90°, height 1.6 m | fresh process 1.6 s, warm 1.1 s; 220 pieces (UV cuts) → 1; boundary edges 5 806 → 34, non-manifold 0 → 34 (these are edges of the source that only show after welding the UV cuts); the 2048² map is kept; IoU 0.989, color difference 0.034 |
| Staff (separate, 3 boxes) | fresh process 4.0 s, warm 3.5 s; part `staff` 14 265 tris, 2 pieces (below and above the fist), 0.33 × 0.17 × 1.80 m, caps of 136 faces on each side, open cut edges 0. But `closed:false`: 6 boundary and 3 non-manifold edges are inherited from the source. The remainder `body` is 106 398 tris (16 boundary, 9 non-manifold, also from the source) |
| Chain via .blend | `separate {blend: clean.blend 30k}` → `staff` 3 559 / `body` 26 752 tris, caps 63/63; `convert {blend: clean.blend}` → FBX 21.7 MB, re-import 29 939 tris, 1.8 m, 1 mesh |
| GLB with a skeleton (a Mixamo character → GLB) | the helper `Icosphere` is removed in all steps: `convert` FBX has 1 mesh, 1 armature, 1.7 m, bottom 0; GLB with `height_m 1.8` is 1.8 m, bottom 0, `height_matches`/`grounded` ok; `cleanup join:false` gives 1 object, 1.7 m; `separate loose` gives 1 part `part_00` (17 358 tris), no false `small` |
| `join:false`, three objects (synthetic) | decimation to 3000: budgets [2170, 12, 817], the 12-tri cube is left whole; voxel 4 cm → 3000: budgets by post-remesh counts [1881, 719, 398], cube volume within 15 % (a tetrahedron would give a third), IoU 0.965 |
| Vertex colors (synthetic) | decimation: the color attribute stays, preview `VERTEX`, difference 0.015; remesh: bake through the attribute node (`linked`), difference 0.006, 100 % of probes red/blue on their own sides; a mesh without a material (`attribute`) behaves the same |
| `core` tests | 32 tests, all PASS, 59 s (2026-10-10 evening: + tear/slab/curve, `delete` with `facing`, sockets in `cleanup`/`separate`); before that 28 tests, 44–45 s over three runs (20 s of it the million-triangle one); Node wrapper `FORGE_TEST_MODULE=core` passes, 46 s; `npm test` 87/87 |

## Honest limitations

- Decimation is Blender's built-in collapse: it interpolates UVs by itself and the quadrics have no
  separate seam handling. At 120k → 30k the texture holds (color difference 0.03), at 120k → 10k it
  is 0.056 and thin facial details soften. The `symmetry` option does almost nothing on AI meshes
  (they are not symmetric), and the step warns about it.
- Welding joins touching cloth layers: on the test character non-manifold edges went 1 → 12, and on
  the Tripo creature 34 show after welding (from the source); the step reports this but does not fix
  it. `recalc_normals` flipped 58 faces of the test character and 14 of the creature; it is a
  heuristic, and for single-sided cards (hair, a cloak without thickness) it is better turned off.
- Remesh + bake: thin parts tear with a voxel coarser than about 0.4 % of height; voxel remesh leaves
  small floating pieces (5 pieces after 6 mm on the test character); only color is transferred (and
  optionally normal/emission), roughness and metalness are not; the bake cage is constant, so in deep
  folds a ray can pick up a neighboring surface; thin dark lines are possible along UV island edges.
  With `bake_normal` the mesh is always fully smooth, and hard faces are conveyed only by the map.
- Splitting by regions uses axis-aligned boxes only. Anything fused inside a fist stays with the
  body: a ~23 cm piece of the staff shaft stayed in the fist of the test character, so the staff came
  out in two pieces. A part is closed only if the source is closed: the staff has 6 boundary and 3
  non-manifold edges from the source mesh, hence `closed:false` and no volume is computed. The caps
  are flat and their UVs are stretched from the cut edge (this is not a cross-section texture). Cuts
  and triangulation add triangles (106 398 + 14 265 = 120 663 against 119 949 after welding).
- `render_compare` is a rough stand-in for the eye: Workbench without PBR and normal maps, two views;
  it catches a lost silhouette and a color shift but does not judge topology. The error threshold
  `silhouette_iou_min` 0.85 was verified on a thin object (a bumpy 4 cm × 1.8 m shaft, ~11 pixels in
  the preview, 5 804 tris): 1500 tris gives IoU 0.984, 600 gives 0.965, 300 gives 0.955 (warning), 150
  gives 0.873 (bumps partly smoothed, passes), 80 gives 0.788 (bumps gone, the shaft flat and twisted;
  the step fails). On thin objects the `silhouette_iou` warning fires earlier than a loss is visible.
- The Mixamo FBX step only prepares the file: uploading and downloading are done by hand (contract
  §7). The model must already face −Y (`rotate_z_deg`); the direction is not detected automatically.
- `convert` without normalization keeps the hierarchy as is (parent transforms are not applied); OBJ
  loses the rig and animation (with a warning). `cleanup`/`separate` drop the rig.
- A missing `inputs.blend` never reaches the step: `af_run.py` opens the .blend itself and fails with
  `RuntimeError: Cannot read file "<path>"` (the path is in the message).
