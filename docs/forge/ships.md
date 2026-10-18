# Ships module: ship_gen, turret_gen, ship_from_mesh

Three steps of the Blender worker, following `docs/FORGE-CONTRACT.md`:

| Step | What it does |
|---|---|
| `ship_gen` | procedural sci-fi ship: 9 roles x 3 layouts x 4 factions, seed, livery, LOD0/1/2, sockets, COL_SIMPLE, manifest |
| `turret_gen` | turret with a `socket_mount -> pivot_yaw -> pivot_pitch -> socket_muzzle_NN` rig and three glTF clips |
| `ship_from_mesh` | AI-generated ship mesh -> seam welding, orientation, scale, decimation to a budget, `AF_*` materials, heuristic sockets with confidence |

Files: `scripts/blender/steps/ship_gen.py`, `turret_gen.py`, `ship_from_mesh.py`, shared code in `steps/_ship_lib.py`,
tests in `scripts/blender/tests/test_ships.py`.

## Axes, names, sockets

- Blender: nose **+Y**, up +Z, starboard +X, mirror at X = 0; glTF (`export_yup`): nose **-Z**, up +Y. The root is at the
  center of the bounding box (check `origin_x_centered` <= 1 cm); the length is exactly `length_m` (+/- 3 %).
- Meshes `M_hull_<material>` (joined by material, 7-8 surfaces); a turret has `M_root_*`, `M_yaw_*`, `M_pitch_*`,
  `M_barrel_steeltrim`. Collision `COL_SIMPLE-convcolonly`, <= 64 vertices. LODs are separate files `*_lod1.glb`, `*_lod2.glb`.
- Materials come only from the `AF_*` library with PBR values; faction and livery change values, not names. Glass uses
  `alphaMode BLEND`, emission lives in separate materials.
- Sockets `socket_<kind>_<place>`; the place vocabulary is `forward`/`aft`, `port`/`starboard`, `dorsal`/`ventral`, `main`.
  `extras`: `game_socket`, `kind`, `size_class` on turret/thruster/launcher/cargo, `mount_d_m` on turret mounts
  (ring diameter) **and on every `socket_thruster_*`** (nozzle exit diameter, m; `godot_package` uses it to scale
  effects), `fx` and `side`/`color` on lights, `frame` on thrusters, `confidence`/`heuristic` on ship_from_mesh. The check
  `thruster_mount_d_m` (error) reads `mount_d_m > 0` on all thrusters from the GLB.
- Frames: turret/utility/dock/sensor/lights - Blender +Z outward, +Y forward (glTF +Y outward, -Z forward). Lights:
  `socket_fx_nav_port` (red) at -X, outward -X; `socket_fx_nav_starboard` (green) at +X, outward +X - the check
  `nav_lights_sides` reads this from the GLB. Muzzle: glTF -Z = firing direction.
- The thruster frame is selectable through the `thruster_frame` parameter: `thrust` (default, glTF -Z = thrust, i.e.
  forward along the ship) or `exhaust` (glTF -Z = exhaust, same as the muzzle). The chosen frame is written to
  `extras.frame` and to the manifest `axis`; the check `thruster_frame` compares it against the GLB.

## ship_gen

A per-role design function builds the ship into a single bmesh: section lofts (profiles with a waist step for
military/industrial, a superellipse for sleek, a lobed one for alien), airfoil wings and fins, turned nozzles with a
glowing interior `AF_EmissiveDrive`, superstructures with a sloped brow and a glazing strip, a cockpit canopy with a frame.
Panels are `inset` hull faces; large hatches are a raised `AF_SteelTrim` lid (a dark recess read as a hole), side hatches
are a steel frame with an armored door. On alien ships the glowing seams run along whole hull rings (`inset_region` over a
row of faces) rather than random rectangles; alien claws are no thinner than ~1.6 % of the length. Greebles are placed by
rules, the starboard side is mirrored to port. Turret mounts are chosen by priority (side/side pairs together; if one slot
is left and only pairs are free, the single mount is swapped for a pair).

**Seed variety.** Every role has three layouts (`FAMILIES`), the seed cycles through them - neighbouring seeds never share
a layout, the starting layout depends on role and faction; `params.layout` sets it explicitly. Proportions, node
variants, engine count and palette come from an RNG seeded with crc32(seed, role).

| Role | Layouts |
|---|---|
| fighter, interceptor | `arrow` (wing by style, fins by faction), `twin_boom` (two booms with engines, a stabilizer between them), `dart` (needle, delta at the rear, canard, ventral fin) |
| gunship | `brick` (hull + superstructure + missile pylons + nacelles), `hammer` (hammerhead across the nose with a bridge and turrets at the ends), `catamaran` (two hulls + central cockpit + wing deck) |
| corvette | `destroyer` (superstructure amidships), `spear` (bridge near the nose, long deckhouse, nacelles on swept pylons, fin), `arrowhead` (wedge in plan, tower aft, splayed fins) |
| freighter | `truss` (truss with containers on three sides), `flatbed` (two trusses, 2-3 container columns), `bulk` (solid hull with hold covers and a bridge aft) |
| miner | `pods` (nacelles on pylons, drills: double/axial/quad), `crane` (manipulators with drills, hoppers on the sides), `scoop` (funnel scoop with ribs, nacelles under the belly) |
| frigate | `wedge` (wedge, tower aft), `hammer` (hammerhead at the bow, tower amidships, nacelles), `fork` (forked bow with a spinal gun between the prongs) |
| carrier | `flattop` (solid deck, island), `hangar_pods` (spine + two hangar nacelles with open noses), `stepdeck` (lower flight deck + upper hangar block tapering toward the bow) |
| transport | `pods` (capsules around a truss, ring), `drums` (2-3 large habitat drums, vertical radiators), `barge` (solid hull with rows of windows, ring, X radiators) |

`params`: `class` (required), `seed` (1), `layout` (auto | name | index), `length_m` (by role; outside the class range
the budget class is chosen by length), `faction` industrial|military|sleek|alien (military), `turret_count`,
`engine_count` (1-6), `wing_style` (fighter/interceptor: auto|none|delta|swept|forward|straight|x|stub|crescent;
gunship: auto|none|stub), `greeble_density` 0-1 (0.6), `livery` {armor|graphite|trim|panel|glass|status|drive|marking:
`"#rrggbb"` or `[r,g,b]` linear}, `palette_variant` 0-2, `thruster_frame`, `asset_id`, `tier`, `lods` (true),
`preview` (true), `preview_turrets` (true: turret copies in the preview only), `preview_size` (300), `uv` smart|box.
Every clamped, replaced or ignored value produces a line in `warnings`
(for example `engine_count 9 clamped to 6`, `wing_style applies to fighter / interceptor / gunship; ignored for frigate`).

Outputs: `ship.glb`, `ship_lod1.glb`, `ship_lod2.glb`, `ship.blend` (after join, orphans purged), `ship.json`
(manifest), `ship.definition.json` (parameters + chosen layout), `preview.png`. `stats.layout` holds the layout and
variants, `stats.registry_entry` holds the entry for `models.json`.

## turret_gen

Base plate with bolts, a rotating table + turret with trunnion cheeks (yaw), a mantlet with a status strip (pitch), barrels
as one mesh `M_barrel_steeltrim` (the recoil target). Ring S/M/L = 1.2 / 2.4 / 4.8 m (`ring_m` within the class range
0.3...1.5 / 3 / 6 m). Pivots: `pivot_yaw` "local Z" [-180, 180], `pivot_pitch` "local X" [-10, 60] (L: [-5, 45]).
**Pivots are not sockets** (§6 of the contract): they carry extras `kind: "pivot"`, `axis`, `range_degrees`, but no
`game_socket`; the turret's sockets are only `socket_mount` and `socket_muzzle_NN`. The checks `pivots_not_sockets` and
`turret_sockets_only_mount_muzzles` read this from the GLB; pivot rows stay in the manifest (`kind: "pivot"` is in the schema).
Clips (30 fps): `yaw_sweep-loop` 4 s, `pitch_cycle-loop` 3 s, `fire_recoil` 0.4 s. Aiming at runtime is done by code.
`barrels` outside 1-4 and `ring_m` outside the class are clamped with a warning. Outputs: `turret.glb` (+ LOD1/2 with the
same rig), `turret.blend`, `turret.json`, `turret.definition.json`, `preview.png`.

**`barrel_style: rotary`** - every barrel becomes a six-barrel block (`_ship_lib.rotary_barrels`: drum, 6 barrels around
a spindle, 2 clamps, flared muzzles); the rig and the clips are the same.

## ship_from_mesh

1. Import, drop empties/cameras/collisions, join into one mesh, **weld**: `remove_doubles` at 1e-5 of the diagonal,
   degenerate faces, normals recomputed (UVs are stored per corner and are not lost). A generated GLB is cut along
   every UV/normal seam; without welding, collapse decimation tore the LODs apart.
2. Up = +Z of the input (or `params.up`); the longitudinal axis is the PCA of face areas; the nose is `params.nose` or a
   vote of three cues (tapering end, blunt faces facing back, bright albedo pixels) with `nose_confidence`.
3. Scale to `length_m`, bounding-box center -> 0. Collapse decimation of **one welded mesh** to 90 % of the budget,
   material labels on that topology, and only then the cut by materials. LOD1/2 are decimated copies of the same
   labelled mesh (no open boundaries between material pieces). Check `silhouette_iou_lod1/2`: IoU of the top and side
   silhouettes against LOD0 >= 0.85 (an error for LOD0 >= 3000 tris, a warning for tiny inputs).
   **If collapse stalls above the LOD budget** (edges with 3+ faces - internal sheets of an AI mesh glued to the hull,
   which Blender does not collapse), the LOD is built from a **closed voxel proxy** of LOD0 (`_ship_lib.voxel_proxy_lod`:
   remesh with voxel L/150, then L/220 and L/300 on failure, then collapse to the budget; the material of each face comes
   from the nearest LOD0 face, UVs from the nearest corners, so the cut by materials and the texture work like regular
   LODs). The proxy is accepted only if it fits the budget and the top/side silhouettes against LOD0 are >= 0.85;
   otherwise the collapse result stays, with a warning. The decimation log is `stats.lod_decimation` (`target`, `tris`,
   `method: collapse | voxel_proxy`, passes, silhouette of the collapse variant). Tried and rejected: squeezing with a
   ratio, planar + collapse (2663 tris), cutting edges with 3+ faces (1496 tris, but side IoU 0.73 - collapse drags the
   cut edges apart).
4. Materials: `classify` (albedo color -> HSV rules -> `AF_*` materials, mean color per class; without a texture it uses
   material/vertex colors, and then only materials with emission count as light), `keep_texture` (a single `AF_Armor`
   with a 2048^2 JPEG albedo for ship_m/l, 1024^2 for ship_s), `single`.
5. Sockets with `extras.confidence`. Thrusters: if the stern has `drive` faces pointing back, the patches of those faces
   (the best cue); otherwise faces pointing back within a narrow stern band of 1.5 -> 3 -> 6 -> 10 % of the length. Points
   are taken **uniformly by area**, not by centroids; a patch is split only where the density along the long axis has two
   peaks with a clear valley; the nozzle exit plane is the rearmost vertex in the patch. If the mesh is X-symmetric (share
   of mirrored occupied cells > 0.7), findings are mirrored and merged closer than 0.6 diameter; those confirmed on both
   sides get +0.1, one-sided ones x0.6; on an asymmetric mesh everything gets x0.75 and a warning. Turrets are flat open
   areas the size of the ring (also from area-uniform points), in pairs on a symmetric mesh. Lights are the extreme point
   at -X and its mirror, the antenna is the top, the dock is the flat belly.
6. Provenance: `source`/`generator` are guessed from the path by default (hunyuan/trellis/triposr/instantmesh ->
   own `source`; meshy/tripo/rodin -> `external` + name), otherwise `external` / `unknown`. The default `asset_id` is
   `ship_<s|m|l>_<file name>_<file sha1, 6 chars>` - two `model.glb` files do not collide.
7. Before saving the .blend, the source materials and images are removed (`orphans_purge`). Outputs: `ship.glb`,
   `ship_lod1/2.glb`, `ship.blend`, `ship.json`, `ship.definition.json` (with orientation and weld stats),
   `preview.png` (markers: red discs - turrets, cyan cones - thrusters), `lod_strip.png` (LOD0 | LOD1 | LOD2 from one
   camera).

### ship_from_mesh: palette, lights, protrusion turrets, concept-image painting, armor, decals

**Palette.** `params.palette`: `input` (default) - the colors of the input itself: mean class colors, and for
`drive`/`status` the emission color of the input materials (or texture) normalized to brightness 1; a glow found only on
thrusters (or only on lights) colors both glow materials. `faction` - the faction palette (`faction`, military by
default). `params.livery` (`"#rrggbb"`, `[r, g, b]` or `{rgb, metal, rough, strength}`) goes on top. Where each color
came from is in `stats.palette_source`, the result in `stats.palette`.

**Navigation lights.** The top view over surface points is opened with a square of ~2.8 % of the length
(`hull_top_analysis`): thin protrusions (barrels, masts, antennas) vanish and the hull body remains; lights go on the
widest cell of the body, and among those the foremost one; height comes from the extreme points there; on a symmetric
mesh they are mirrored.

**Turrets at protrusions.** `turret_sockets: protrusions` (or `auto`: protrusions if there are >= 2) - sockets
`socket_turret_<side>_<forward|aft>` at the base of thin transverse protrusions that the opening cut off from the hull
(length >= 3.5 % L, width <= 35 % of the length, direction along +/-X), the socket +Z points outward along the barrel,
extras `barrel_length_m`. The default is still `flats`. The `kitbash` step places gatling guns exactly on these sockets.

**Thrusters when painting.** A concept image shows the inside of a nozzle from one side, so the glow lies in crescents and
a single nozzle used to split into two. `thruster_cue`: `auto` (with `paint` - by the exit geometry, the glow only
confirms, +0.2 confidence), `glow`, `geometry`; nearby patches of one ring (closer than 0.8 of the size) are merged.

**Painting from a concept image (`params.paint`).** On the full-size welded mesh before decimation:
1. concept mask - the image alpha (a reference PNG from `prepare_concept.py` with a haze filter) or `paint.mask`;
2. pixel labels (`paint_label_image`, v1 rules of `label_image.py` without scipy): a gray closing removes thin dark
   seams, then armor / steel (`trim`) / graphite / panel (very dark) / glow (warm, bright, saturated); radii scale with the
   image width; `regions` - rectangles in image fractions with a label (`only` - change only those labels): this is how a
   bridge window became glass and letters became armor (inscriptions come back as decals);
3. camera `fit`: a search over directions (15 deg x 7 heights, 50 mm) with 2D similarity by area and silhouette center,
   then Nelder-Mead over [azimuth, height, distance, target xyz, roll, focal] at 320 and 640 px (triangle raster by area
   sampling); for fitting, the mesh is temporarily decimated to `fit_tris` (24 000). An explicit camera is a dict or json
   (`{location, rotation_matrix, lens_mm, resolution}`, the v1 format is also accepted);
4. projection of face centers with a z-buffer from a triangle sample (a face is visible if it is the nearest in its
   pixel; pixels without a sample - minimum 3 x 3), only faces with `toward > min_toward` (0.3: grazing faces take their
   neighbors);
5. the hidden side by X mirror: the surface nearest to the mirrored point (BVH) and its label, if the normals agree;
6. the rest - the majority of labelled neighbors within 2.5 % L (niche floors next to dark walls stay dark), otherwise
   the rules (a stern facing back is steel; everything else is armor);
7. majority smoothing over adjacent faces, absorption of islands (dark < 1.5e-3 L^2, armor < 4e-4 L^2; connectivity
   islands over the whole dark mask), after decimation `smooth_lod0` more passes on LOD0;
8. class colors are means over visible faces (the bright half by luminance: shadows are not albedo), dark ones with a
   lower albedo limit, glow with saturation restored (`glow_gamma` 1.6: bright areas of the image clip to white).
Outputs: `paint_camera.json`, `paint_overlay.png` (green - common, red - concept only, blue - mesh only),
`stats.paint` (camera, silhouette IoU, label sources, colors).

**Armor (`params.armor_texture`).** Kit set `scripts/blender/data/kits/kits.json`: a seamless hexagonal-plate armor kit
(`*_armor_hex`) and a plain-panel kit (`*_armor_plain`); the source and the build method of each are recorded in
kits.json (build script `build_*_armor_textures.py`). On `AF_Armor` there is an albedo (with a multiplier for the palette
color) and a normal map, UVs are a box unwrap in meters / `tile_m`; `regions` with `box` give a separate material
(`AF_ArmorPlain`). The tile size is stored on the material (`af_tile_m`).

**Decals (`params.decals`).** `{text, side: port | starboard | both | top, position: [y, z] | [x, y], size: [w, h],
font, bold, align, color, wear, line_scales, slashed_zero}`. The text is drawn by the Blender font engine (Workbench,
ortho camera) into an RGBA atlas with wear (chips, scratches); the decal mesh is a grid in the plane of the mean hull
normal under the text, projected onto the hull and lifted by 0.055 % L; material `AF_Marking` (BLEND). On the starboard
side the text reads correctly (reading direction toward the bow). LOD1 is coarser; if it does not fit the LOD1 budget it
is removed (with a warning). The default font is **DejaVu Sans Bold** (`C:/Windows/Fonts/DejaVuSans-Bold.ttf`; the
Bitstream Vera / DejaVu license is free, a texture with rasterized text may be distributed without restrictions; the font
file itself is not copied). **Bahnschrift** does not work: it is a variable font with overlapping contours, and Blender's
curve fill makes holes in it (T becomes "V", the tail of Q breaks) - verified; its Microsoft license allows using rendered
text but not redistributing the file.

## Running

```
"C:/Program Files/Blender Foundation/Blender 5.0/blender.exe" -b --factory-startup ^
  --python scripts/blender/af_run.py -- ship_gen request.json
```
```json
{"step": "ship_gen", "params": {"class": "frigate", "seed": 2, "faction": "military"}, "out_dir": "D:/out/frigate"}
{"step": "turret_gen", "params": {"size": "M", "barrels": 2, "faction": "industrial", "seed": 4}, "out_dir": "D:/out/t"}
{"step": "ship_from_mesh", "inputs": {"mesh": "D:/gen/ship.glb"}, "params": {"class": "gunship"}, "out_dir": "D:/out/fm"}
```
Tests: `blender -b --factory-startup --python scripts/blender/tests/run_tests.py -- ships` (~25 s).
Test temp folders are deleted after a successful test; previews no longer leave `af_ship_prev_*` in %TEMP%.

## Measurements (2026-10-10, Blender 5.0.1, CPU)

Test fleet: 18 ships, two layouts per role, plus 4 turrets. Building a ship with LODs and a preview takes 0.74-1.30 s.

| # | asset_id | layout | class | L x W x H, m | tris LOD0 / LOD1 / LOD2 | IoU LOD2 / LOD1 | surfaces | nodes | MB | turrets | thrusters | COL verts / coverage |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | ship_fighter_military_002 | arrow | ship_s | 14x10.1x2.3 | 4920 / 1594 / 555 | 0.97 / 0.98 | 8 | 18 | 0.23 | 2 | 1 | 64 / 0.997 |
| 2 | ship_fighter_industrial_001 | twin_boom | ship_s | 14x12.0x2.3 | 4578 / 1490 / 502 | 0.96 / 0.98 | 8 | 18 | 0.20 | 2 | 1 | 64 / 0.998 |
| 3 | ship_interceptor_sleek_001 | dart | ship_s | 18x7.5x3.2 | 6352 / 2069 / 719 | 0.97 / 0.97 | 8 | 19 | 0.25 | 2 | 2 | 64 / 0.998 |
| 4 | ship_interceptor_alien_001 | arrow | ship_s | 18x9.6x2.5 | 4964 / 1621 / 563 | 0.96 / 0.97 | 8 | 19 | 0.21 | 2 | 2 | 64 / 0.999 |
| 5 | ship_gunship_military_002 | hammer | ship_m | 42x22.2x14.3 | 6040 / 1965 / 671 | 0.96 / 0.98 | 8 | 24 | 0.28 | 4 | 2 | 64 / 0.996 |
| 6 | ship_gunship_industrial_001 | catamaran | ship_m | 42x14.5x8.8 | 11020 / 3204 / 1227 | 0.99 / 0.99 | 8 | 24 | 0.43 | 4 | 3 | 64 / 0.989 |
| 7 | ship_corvette_military_001 | destroyer | ship_m | 70x22.0x20.3 | 8112 / 2640 / 913 | 0.97 / 0.98 | 8 | 24 | 0.38 | 4 | 4 | 64 / 0.993 |
| 8 | ship_corvette_alien_001 | spear | ship_m | 70x27.0x22.0 | 6462 / 2110 / 717 | 0.96 / 0.98 | 8 | 24 | 0.28 | 4 | 4 | 64 / 0.997 |
| 9 | ship_freighter_industrial_002 | truss | ship_m | 60x14.8x12.2 | 8656 / 2819 / 966 | 0.91 / 0.99 | 8 | 22 | 0.44 | 2 | 3 | 64 / 0.977 |
| 10 | ship_freighter_sleek_002 | bulk | ship_m | 60x13.0x27.0 | 5624 / 1836 / 636 | 0.97 / 0.98 | 8 | 22 | 0.24 | 2 | 3 | 64 / 0.983 |
| 11 | ship_miner_industrial_002 | pods | ship_m | 48x18.0x17.2 | 4356 / 1426 / 490 | 0.92 / 0.98 | 8 | 21 | 0.20 | 2 | 2 | 64 / 0.992 |
| 12 | ship_miner_military_002 | crane | ship_m | 48x16.3x15.6 | 5376 / 1757 / 596 | 0.91 / 0.97 | 8 | 21 | 0.27 | 2 | 2 | 64 / 0.993 |
| 13 | ship_frigate_military_001 | fork | ship_l | 160x46.4x43.9 | 10660 / 3471 / 1185 | 0.95 / 0.97 | 8 | 29 | 0.46 | 8 | 4 | 64 / 0.991 |
| 14 | ship_frigate_alien_002 | hammer | ship_l | 160x67.3x61.1 | 8062 / 2627 / 893 | 0.98 / 0.99 | 8 | 27 | 0.35 | 8 | 3 | 64 / 0.996 |
| 15 | ship_carrier_industrial_001 | flattop | ship_l | 240x66.4x58.2 | 8820 / 2608 / 1002 | 0.94 / 0.95 | 8 | 26 | 0.43 | 6 | 4 | 64 / 0.994 |
| 16 | ship_carrier_military_001 | hangar_pods | ship_l | 240x96.7x81.7 | 13004 / 4229 / 1447 | 0.94 / 0.96 | 8 | 27 | 0.60 | 6 | 4 | 64 / 0.996 |
| 17 | ship_transport_sleek_001 | pods | ship_l | 200x81.9x36.4 | 11532 / 3751 / 1278 | 0.98 / 0.98 | 7 | 23 | 0.43 | 4 | 4 | 64 / 0.982 |
| 18 | ship_transport_industrial_001 | barge | ship_l | 200x73.3x73.3 | 9944 / 2934 / 1109 | 0.96 / 0.99 | 8 | 24 | 0.48 | 4 | 4 | 64 / 0.995 |

"IoU" is the lower of the top and side silhouette IoU against LOD0. The height of large ships includes masts and
antennas. LOD1 = 29-33 %, LOD2 = 11 % of LOD0. Generic budgets (tris LOD0/LOD1/LOD2): ship_s 20000/6000/1500, ship_m
40000/12000/3000, ship_l 60000/18000/5000; <= 9 surfaces, <= 9 materials, 3/6/12 MB.

**Nodes.** The budget has two limits: the per-class limit (<= 32 per ship, <= 16 per turret) and a stricter row
(hull <= 24, turret <= 12). The first is an error, the second is the warning `nodes_strict_row`. It is exceeded by 4 of
the 18 ships (frigates with 29 and 27, carriers with 26 and 27: 8 / 6 turrets + thrusters + lights) and by 3 of the 4
turrets (13-15 nodes).

**Collision.** "Coverage" = volume of COL_SIMPLE / volume of the full convex hull of the mesh (0.977-0.999). It does
**not** check COL volume against hull volume: the "hull" volume of a kitbash of intersecting parts is undefined.

**Seed variety** (military, three seeds per role). IoU of the top and side silhouettes between seeds 1-3 of the same role
and faction, before the fix (military): corvette 0.978 top, gunship 0.970 side, frigate 0.959 top; all 9 roles had at
least one pair > 0.85. After: 9 roles x 4 factions x seed triples 1-3 and 4-6 (216 pairs) - maximum **0.843** (sleek
carrier); the test on military 1-3 - maximum 0.811. The test requires < 0.85 for both top and side for every pair.

Turrets:

| asset_id | size | barrels | ring, m | height, m | tris LOD0 / LOD1 / LOD2 | surfaces | nodes | MB |
|---|---|---|---|---|---|---|---|---|
| turret_s_sleek_b1_003 | S | 1 | 1.20 | 0.78 | 2108 / 678 / 228 | 7 | 12 | 0.10 |
| turret_m_military_b2_001 | M | 2 | 2.40 | 1.64 | 2344 / 760 / 254 | 7 | 13 | 0.11 |
| turret_l_industrial_b3_002 | L | 3 | 4.80 | 3.29 | 2716 / 836 / 296 | 7 | 14 | 0.13 |
| turret_l_alien_b4_002 | L | 4 | 4.80 | 3.29 | 3104 / 1006 / 340 | 7 | 15 | 0.14 |

ship_from_mesh on synthetic inputs:

| input | weld: vertices / boundary edges | tris in -> LOD0 / LOD1 / LOD2 | IoU LOD2 top / side | nose | sockets (confidence) | MB glb / blend |
|---|---|---|---|---|---|---|
| synthetic: ship_gen corvette `arrowhead`, merged, nose -> -X, scale 0.03 | 8988 -> 3856 / 8536 -> 0 | 7440 -> 7440 / 2422 / 800 | 0.983 / 0.963 | auto, 1.0 | 4 of 4 adjacent bells in a row, exactly at the original sockets; 3 of 4 turrets (all on the underside) | 0.31 / 0.41 |
| review primitive: box + cone + 2 cylinders with n-gon caps, 4 objects, no materials/UVs, scale 0.01, nose -X, OBJ | 121 -> 121 | 226 -> 226 / 72 / 24 | 0.978 / 0.473 (warning: input < 3000 tris) | auto, 1.0 | exactly 2 thrusters at x = +/-0.666 m (0.95), 2 turrets on the back | 0.02 / 0.09 |

On synthetics the tests compare `mount_d_m` with the geometry: two cylinders of r = 0.38 on 12.6 ship units - +/-25 %,
the thrusters of a merged ship_gen corvette found by ship_from_mesh - 0.5...1.3 of the generator's value. Test
`test_voxel_proxy_lod_budget_materials_uv` covers a synthetic case with a 3-face edge (voxel proxy LOD).

## Fixes from the 2026-10-10 review

Navigation lights (they were on the wrong side and looked into the hull) -> `nav_lights` normalizes the side, check
`nav_lights_sides`, a test over all roles. ship_from_mesh LODs tore -> weld + decimation of one mesh + LODs from its
copies + silhouette check. Seeds produced clones -> three layouts per role, RNG by crc32(seed, role), a silhouette test.
Thrusters on a symmetric primitive (4 instead of 2), empty planes -> area-uniform points, split by two peaks, mirroring.
Default provenance and `asset_id`, orphans in the .blend, `fore` -> `forward`, thruster frame choice, temp folders,
warnings for clamped parameters, alien seams, claws, carrier island, hatches.

## Limitations (honest)

- Ships are assembled from procedural primitives (lofts, prisms, turned bodies, boxes): a clean kitbash for a game
  camera, not hand modeling. No textures - only flat PBR factors; panels are geometry, not normal maps. Smart UV on LOD0
  for a future bake, box UV on LOD1/2.
- The preview is Workbench: emission is drawn as a flat color, no glow; not a final render.
- Three layouts per role are three silhouettes, not endless variety: seeds 1 and 4 of the same role and faction share a
  layout and differ in proportions, node variants and palette. Alien differs by profile, seams, curved wings and claws,
  but layouts are shared across factions.
- Turrets in ship previews are copies for the picture only; they are not in `ship.glb`.
- `size_class` of a thruster is computed from the diameter (S <= 1.5, M <= 3, otherwise L), so frigate (10.8 m) and carrier
  (15.5-18.3 m) thrusters are also "L"; the exact size is in `mount_d_m`.
- <= 32 nodes per ship: with many turrets/thrusters, low-priority sockets are dropped with a warning; the stricter 24/12
  row is not met by frigates, carriers and turrets (warning).
- 7-8 surfaces: the <= 9 limit is met, the <= 4 target is not (warning).
- COL_SIMPLE is convex: on carriers, transports with a ring, miners with drills and a scoop it covers voids. gltf-validator
  and a headless Godot import were not run (manifest `-1` / `SKIPPED`).
- ship_from_mesh is heuristics, not understanding of shape. With symmetric ends the nose confidence drops - then set
  `nose`. Up is always +Z of the input unless `up` is given. On a texture, everything bright and saturated counts as glow -
  yellow-orange markings near the stern too (for inputs without a texture only materials with emission count as light).
  Thrusters without emission are found by faces pointing back: if the flat stern cut of the hull sits almost flush with the
  nozzles, they may merge into one patch. Turrets go on flat places, not where the author intended; a paneled back may
  yield none (synthetic: 3 of 4, all on the underside). Collapse decimation, not retopology: LOD2 of a dense AI mesh is
  faceted (no holes or tears, silhouette >= 0.93). The voxel proxy of a far LOD smooths small details and merges gaps
  thinner than a voxel (L/150 = 9 cm on a 14 m fighter); its UVs are transferred from the nearest corners, so on UV-island
  seams the `keep_texture` texture may smear. `keep_texture` is a projection of the AI albedo, not PBR; a .blend with a
  packed 2048^2 albedo weighs ~8 MB.
