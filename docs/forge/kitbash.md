# The "kitbash" module: procedural details on a finished model

The `kitbash` step of the Blender worker places detailed procedural parts onto an already finished (most often AI-generated) ship model
— at its sockets or at given points — and cuts out the generator's old soft geometry beneath them. The parts
are built for LOD0/1/2 and merged into the existing objects by AF material, so the number of surfaces and materials
does not grow. The contract is `docs/FORGE-CONTRACT.md` (§3 call, §4 axes, §6 sockets).

Files: `scripts/blender/steps/kitbash.py` (the step), `scripts/blender/steps/_kitbash_lib.py` (part builders,
cut-out zones), the shared kit `_ship_lib.Kit` and `_ship_lib.rotary_barrels` (a six-barrel block; `turret_gen` also takes it
with `barrel_style: rotary`), tests `scripts/blender/tests/test_kitbash.py`.

## Input and output

| | |
|---|---|
| `inputs.blend` | `ship.blend` of the `ship_from_mesh` step (or of a previous `kitbash`): LOD0 + roots `<id>_lod1/2`, sockets, AF materials and the forge metadata `scene["af_ship"]` (ship manifest, budget class, length, nozzle frame) |
| `inputs.mesh` | `ship.glb`; next to it `ship_lod1.glb`, `ship_lod2.glb` and `ship.json` are picked up if present (materials `AF_*.001` from the second import are merged into the LOD0 materials) |
| outputs | `ship.glb`, `ship_lod1.glb`, `ship_lod2.glb`, `ship.blend` (with updated metadata), `ship.json` (ship manifest: triangles, sockets, dimensions, hash, `generator_version += af_kitbash_v1`), `preview.png`, `lod_strip.png` |

In the forge pipeline the step receives the `.blend` of the previous model step (slots `blend, mesh`); `godot_package`
after it takes the GLB of `kitbash`.

## Parts (`params.parts`)

All dimensions are metres in the Blender ship frame (nose +Y, top +Z, starboard +X). `at` — a socket name or a mask
(`socket_turret_*`), a point `[x, y, z]`, a list of points or `[x, y]` (antennas and lights — ray-snapped to the hull).
`mirror: "x"` duplicates point parts to the other side. `remove` — a cut-out zone for the old geometry
(`{cylinder: {p0, p1, radius}}`, `{sphere: {center, radius}}`, `{box: {min, max}}`) or the short form
`{radius, depth, inset, above, cap}`; `false` — do not cut. `lod_max` — up to which LOD the part is built
(default: gatlings, nozzles, blocks — 2; pipes, masts, grilles — 1; thin antennas and lights — 0).

| kind | What it builds | Main fields |
|---|---|---|
| `gatling` | a rotary six-barrel gun: a base plate and ring on the wall, a housing with a bevel, cooling fins, a feed box, a cable, an amber indicator, a drum, 6 barrels around the axis, 2 clamps, flash hiders | `length` (barrels), `cluster_d`, `housing: [length, width, height]`, `barrels`, `seat: auto` (rays outward onto the niche wall after the stub is cut out) or metres along the axis, `muzzle_socket` |
| `nozzle` | a ribbed bell (dark graphite, 2 chords per rib), open band rings and a steel rim at the cut, a glowing interior in two tones (depth — `AF_EmissiveDrive`, the band at the cut — `AF_EmissiveStatus`), a hot throat, a steel collar with flanges, 4 piston actuators, a mounting plate covering the nacelle | `diameter` (default the socket's `mount_d_m`), `length`, `throat_d`, `collar_len`, `ribs`, `bands`, `actuators`, `axis` (exhaust, default -Y) |
| `piping` | a smooth pipe through points (Catmull-Rom, transported frames), ends and flanges; several in parallel | `points`, `radius`, `count`, `spread`, `material`, `flanges` |
| `antenna`, `mast` | `whip` (a base, a thickening, a thin whip, an amber light at the tip), `mast` (a post with a cap and crossbars), `rod`, `dome` (a sensor sphere) | `height`, `style`, `radius`, `light`, `socket` |
| `nav_light` | a light housing and a glowing lens (`AF_EmissiveStatus`) along the surface normal | `snap: top \| side \| port \| starboard \| none`, `side: both`, `size`, `socket` (a name or `[left, right]`) |
| `vent_grille` | a frame, inclined slats, a dark bottom | `at: [y, z]`, `side`, `size: [length, height]`, `slats`, `depth` |
| `block` | a block with a bevel and a panel insert (for example to cover a cut-out) | `size`, `material`, `bevel`, `taper`, `panel`, `axis`, `up` |
| `remove` | cut-out only | `regions`, `cap` |

Order of work: the cut-out zones of all parts are applied to all LODs → a BVH is built from LOD0 → the parts
are placed (the gatling seat — the median of rays around the cut-out stub; antennas — onto the roof; under the stub of
an AI antenna the roof is found with rays around it, and the cut is closed with a cap) → each LOD gets its own `Kit(lod)` →
split by material and merge into the LOD object with the same material (UV — a metric box unwrap, the tile size
is taken from the material's `af_tile_m`, i.e. armour with the `ship_from_mesh` texture lies without a scale seam).

## Sockets

- a gatling at `socket_turret_<place>`: the socket stays (the AF frame, +Z outward = the barrel axis), gets extras
  `builtin: gatling_6barrel` and `seat_offset_m`; `socket_muzzle_<place>` is added (glTF -Z = fire direction,
  +Y = up; if the barrel points up/down, "up" is the ship's nose), extras `mount` = the turret name and
  `diameter_m` = 2.5 x `cluster_d` (the flash envelope: `godot_package` scales `muzzle_flash` by it);
- a nozzle at `socket_thruster_*`: the frame is untouched, `mount_d_m` and `size_class` follow the new cut,
  `builtin: nozzle_ribbed_bell`;
- `socket` on an antenna / light: the socket is created or moved onto the part (the previous one's extras are kept);
  `drop_sockets` removes unneeded ones (for example `socket_utility_antenna`).

## Checks

The class budget (`budget_checks`: LOD0/1/2, surfaces, materials, nodes, MB, names), `collision_vertices`
(COL_SIMPLE is recomputed from LOD0 with the parts), `kitbash_parts_placed` (each part found an anchor and the hull),
`thruster_frame` and `thruster_mount_d_m`, `nav_lights_sides`, `muzzle_frame_fire_axis` (the glTF -Z of the muzzle points
away from the turret), LOD1/2 silhouettes against LOD0 >= 0.85, `manifest_schema`, the asset inspector (a warning).

## Running

```
"C:/Program Files/Blender Foundation/Blender 5.0/blender.exe" -b --factory-startup ^
  --python scripts/blender/af_run.py -- kitbash request.json
```
```json
{"step": "kitbash", "inputs": {"blend": "C:/work/ship_from_mesh/ship.blend"},
 "params": {"parts": [
   {"kind": "gatling", "at": "socket_turret_*", "length": 5.0, "cluster_d": 1.05, "housing": [1.9, 1.7, 1.5],
    "remove": {"radius": 0.85}},
   {"kind": "nozzle", "at": "socket_thruster_*", "diameter": 5.4, "length": 4.2, "throat_d": 2.9,
    "collar_len": 1.2, "ribs": 32, "remove": {"radius": 3.05, "depth": 5.0}},
   {"kind": "antenna", "at": [0.9, 9.8], "height": 4.2, "socket": "socket_utility_mast_01"}],
  "drop_sockets": ["socket_utility_antenna"]},
 "out_dir": "C:/work/runs/kb"}
```
Tests: `blender -b --factory-startup --python scripts/blender/tests/run_tests.py -- kitbash`.

## Measurements (2026-10-10, Blender 5.0.1, CPU)

**A test ship** (input — `ship.blend` of the `ship_from_mesh` step with markup from the concept: LOD0 22,913 tris,
LOD1 6,741, LOD2 1,666): 24 part entries → 4 gatlings on `socket_turret_*`, 2 nozzles on `socket_thruster_*`, 7 pipe entries
(16 pipes with mirroring), 3 blocks and 2 cylinder tanks at the stern, 1 mast + 3 whips + a sensor dome, 10 lights, 2 grilles,
2 cut-out zones. The step takes 2.7 s.

| | LOD0 | LOD1 | LOD2 |
|---|---|---|---|
| triangles before → after (budget ship_m) | 22,913 → **39,396** (40,000) | 6,741 → **10,300** (12,000) | 1,666 → **2,239** (3,000) |
| AI faces cut out | 3,849 | 1,272 | 304 |
| parts, tris: gatlings / nozzles / pipes / antennas and mast / lights / grilles / blocks | 4,880 / 8,088 / 4,600 / 1,340 / 880 / 376 / 168 | 752 / 2,592 / 1,204 / 60 / — / 144 / 72 | 176 / 664 / — / — / — / — / 36 |

Surfaces 9, materials 9 (did not grow), nodes 27, 2.54 MB, COL_SIMPLE 64 vertices (coverage 0.993), silhouettes
LOD1 0.991 / 0.982, LOD2 0.979 / 0.957 (top / side), sockets 16 (+4 `socket_muzzle_*`, 2 masts and a sensor on the
parts, lights moved to the bow lamps). The asset inspector with `--class ship_m --strict`: PASS for LOD0/1/2 and the package GLB.

Tests: `run_tests.py -- kitbash` — 4 tests (builders and zones, all part kinds on a procedural gunboat from GLB +
LOD + ship.json, cut-out of a stub barrel and seating a gatling on the wall, `turret_gen` rotary), ~2.5 s.

## Limitations (honestly)

- Parts are placed at the sockets and points defined by the recipe (their coordinates were picked from mesh sections and
  the concept); the step does not "understand" the shape — it will not find by itself where the pipes or antennas are on an AI model.
- The cut-out is removal of faces by centre in the zone (+ an optional `holes_fill` patch), not a boolean operation: the edge of the cut is
  ragged and hides under the part (the nozzle mounting plate, the gatling housing, a block). Remnants of AI geometry around parts
  (soft folds at the base of masts, bits of armour in niches) remain.
- Gatling seating is the median of four rays onto the wall around the cut-out barrel; on an asymmetric AI mesh the left and
  right gatlings may sit with a difference of up to ~0.5 m in X (muzzles at 16.35 / 15.82 m in the test ship).
- Parts have no UV unwrap of their own: a metric box projection; part materials are flat AF ones (graphite, steel, panel,
  glow) with no wear textures.
- The nozzle glow is two emissive materials, with no volume or gradient; in EEVEE with scene lighting and AgX it reads as
  peach, in the game the colour and strength are set by the material (`AF_EmissiveDrive` 1.0 / 0.27 / 0.05 x 4).
