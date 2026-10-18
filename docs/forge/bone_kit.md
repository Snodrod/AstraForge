# The bone kit module (bone_kit): skeleton hands, a bone staff, "barrel" toggle fasteners

Blender step: `bone_kit` (`scripts/blender/steps/bone_kit.py`, shared code in `steps/_bone_kit_lib.py`, independent of
the items module). Self-test: `scripts/blender/tests/test_bone_kit.py` - 19 tests, all pass, ~80 s.
Purpose: detailed game-ready bone parts for undead characters: anatomically built hands (instead of merged "spade"
hands with painted-on bones), a bone staff with a talon head, and toggle fasteners that exist as geometry rather than only in
a texture; further use is skeletons and other bone-based units. The design reference is an illustration of a
bone-themed character (not part of the repository).
All numbers below were obtained in one run of `test_bone_kit.make_evidence()` on the code of that time (2026-10-10).

## Running

```
"C:/Program Files/Blender Foundation/Blender 5.0/blender.exe" -b --factory-startup ^
    --python scripts/blender/af_run.py -- bone_kit request.json
"C:/Program Files/Blender Foundation/Blender 5.0/blender.exe" -b --factory-startup ^
    --python scripts/blender/tests/run_tests.py -- bone_kit
```
The figures of this document are produced by (~35 s, the OS temporary folder + an output folder of your choice):
```
"C:/Program Files/Blender Foundation/Blender 5.0/blender.exe" -b --factory-startup --python-expr ^
  "import sys;sys.path[:0]=[r'<repo>/scripts/blender',r'<repo>/scripts/blender/tests'];import test_bone_kit as t;t.make_evidence(None, r'<out dir>')"
```

```json
{"step": "bone_kit", "params": {"part": "skeleton_hand", "side": "l", "pose": "grip",
  "grip_radius_m": 0.018, "rings": ["index", "middle", "ring"], "style": "aged", "seed": 0},
 "out_dir": "abs/out"}
```
Outputs of one part: `part.glb`, `part.blend`, `part.json` (dimensions, triangles, sockets with world frames, bones,
the list of details, weights, grip contact, materials, GLB summary), `preview.png` (`af.render_views`, `color='MATERIAL'`;
for the `aged` style - `VERTEX`, so that the vertex colors are visible). `result.json` / `AFRESULT` - per the contract §3.

Common parameters: `part` (`skeleton_hand | bone_staff | bone_toggle`), `style` (`clean | aged`), `seed`,
`palette` (`bone`, `gold`, `crystal`, `crystal_glow` - `#rrggbb`; `crystal_emission` - a number), `budget_tris`,
`preview` (true), `preview_size` (384).

`params.batch` is a list of dicts (each on top of the common parameters, parts can be mixed) or a number N (N adjacent
seeds of the same part): one subfolder per variant (the name is sanitized to `[A-Za-z0-9_-]` and checked to be inside
`out_dir`), `contact_sheet.png`, `batch.json`; `glb` and `blend` in the response are lists. Sheet: `sheet_cols` (4),
`sheet_tile` (256), `sheet_title`.

## 1. skeleton_hand - a skeleton hand

| parameter | default | what it does |
|---|---|---|
| `side` | `r` | `l` / `r` |
| `pose` | `open` | `open`, `relaxed`, `claw`, `fist`, `grip` |
| `length_m` | 0.19 | from the wrist crease to the tip of the middle finger (for a 1.8 m figure); everything is scaled from it |
| `finger_spread_deg` | by pose (9 / 4 / 15 / 2) | finger spread (index +1, middle +0.15, ring −0.55, pinky −1.1 units) |
| `curl` | by pose | 0..1: a number, a list of 5 (`thumb..pinky`) or `{finger: c}`; MCP = 85°·c, PIP = 100°·c, DIP = 70°·c (`claw` has its own law) |
| `grip_radius_m` | 0.018 | radius of the grip cylinder; the `grip` pose wraps around it, the socket of every pose lies on its axis |
| `grip_tilt_deg` | 0 | tilt of the grip axis to the line of the knuckles (0 = the shaft is perpendicular to the hand, as in the reference) |
| `rings` | `[]` | fingers with a gold ring (`"thumb"`, `"index"`, …), the ring sits on the proximal phalanx, neighbors are offset along the bone |
| `ring_at` | 0.66 | position of the ring along the proximal phalanx |
| `origin` | `wrist` | `wrist` - the center of the wrist joint at zero; `ground` - bottom at z = 0, X/Y center at zero |

**Anatomy.** 27 bones as separate closed meshes: 8 carpal bones (scaphoid, lunate, triquetrum,
pisiform, trapezium, trapezoid, capitate, hamate - irregular ellipsoids in a dense block), 5 metacarpals,
14 phalanges (the thumb - proximal and distal, the others - proximal, middle, distal). The bones are bodies of revolution
by profile with epiphyses: a wide base with a concave articular cup, a shaft flattened on the palmar side, a slight
dorsal curve, two condyles on the heads of the phalanges, the head of a metacarpal is a sphere around the MCP center
of rotation, the distal phalanx has a tuberosity. The lengths are average adult ones (for a 19 cm hand: metacarpals 6.6 / 6.4 / 5.7 / 5.1 and 4.5 cm
for the thumb; proximal phalanges 3.9 / 4.4 / 4.1 / 3.2 and 3.1; middle 2.3 / 2.8 / 2.7 / 1.9; distal 1.7 / 1.9 /
1.85 / 1.6 and 2.3), the metacarpal fan is 7 / 1.5 / −5 / −11°, with a transverse arch of the palm. The chain of the middle finger is exactly equal to
`length_m` (the scale is adjusted after the seed spread of lengths ±1.5 %). Joints: the center of rotation is inside the head of the
proximal bone, the gap is 1.2 mm, the cup of the next bone embraces the head - the bones do not intersect when bending.
The thumb is opposable: radial and palmar abduction, pronation (64–117° depending on the pose), flexion
CMC / MCP / IP.

**Poses.** `open` - a hanging open hand with slightly bent spread fingers (like the right hand in the reference),
`relaxed` - flexion grows toward the pinky, `claw` - extended MCP and bent PIP/DIP, `fist` - a fist, the thumb is
fitted by optimization onto the middle phalanges of the index and middle fingers. `grip`: the cylinder is placed by a search (grid +
refinement) so that the palm (the heads of the metacarpals) almost touches it, and each phalanx in turn is bent until it touches
(tangential wrapping over the real cross-sections of the bones and rings); the thumb goes around the cylinder from the radial side toward
the fingers (a grid over CMC + coordinate descent, a penalty for intersections with the fingers). Intersections of the parts are checked by BVH in
all poses.

**Armature.** Mixamo bones for its own side: `mixamorig:<Left|Right>Hand` and
`mixamorig:<Left|Right>Hand{Thumb,Index,Middle,Ring,Pinky}{1..4}` - 21 bones (Hand + 20 finger bones, as in the contract
§5; the original task said "20 including Hand", which differs from §5, where every finger has 1..4). Rest = the generated
pose. The bone heads are the centers of rotation of the joints, Hand: wrist → MCP of the middle finger; Thumb1 = the thumb metacarpal;
`*4` are end bones without weight. The local Z of a bone is the dorsal side of its segment. Each detail as a whole (weight 1.0)
is bound to exactly one bone: the carpals and metacarpals II–V → Hand, metacarpal I → Thumb1, phalanges → their own bones, a ring -
to the bone of its phalanx. The mesh is a child of the armature + an Armature modifier; in the GLB - one skin with 21 joints.

**Hand frame.** The wrist at zero, fingers hanging along −Z, the thumb toward −Y (forward of the character, §4), the palm toward the body:
+X for the right hand, −X for the left. The left hand is an exact mirror of the right (a test compares vertices).

**Socket `socket_mount_hand_<side>`** (§6) is a child of the Hand bone, lies on the axis of the grip cylinder between the planes of the
middle and ring fingers; +Z is along the axis, out of the fist on the thumb side (in the hand frame −Y), −Y is along the
knuckles toward the fingers (down), X = Y × Z. The position does not depend on the pose: in `grip` the fingers wrap exactly the item on
this socket; extras: `game_socket`, `kind`, `frame`, `grip_radius_m`. Aligning the staff's `socket_grip_main` with this
socket gives an error of 0.0 mm (checked in the evidence).

### Measurements (right hand, seed 0)

| pose | tris / budget | dimensions, m (X×Y×Z) | intersections | notes |
|---|---|---|---|---|
| open | 2 656 / 3 000 | 0.070×0.087×0.173 | 0 | chain 0.1900 m; with 3 rings 2 848 |
| relaxed | 2 656 | — | 0 | MCP/PIP/DIP of the pinky 41 / 48 / 33° |
| claw | 2 656 | — | 0 | PIP 69–76°, DIP 56–60° |
| fist | 2 656 | — | 0 | MCP 85, PIP 100, DIP 70° |
| grip (R 18 mm) | 2 656 | — | 0 | the distal phalanges are 0.7–0.85 mm from the cylinder, no bone cuts in |
| grip + 3 rings, left | 2 784 | 0.072×0.074×0.115 | 0 | the rings touch the cylinder with an overlap of up to 0.46 mm |

A ring = 64 triangles, rings on all five fingers - 2 976 tris. Assembling the hand inside Blender takes 2.5–4 s (the search for the
cylinder and the thumb); 1 380–1 480 vertices; GLB 160–210 KB (skin, UV, vertex colors for `aged`).

## 2. bone_staff - a bone staff

| parameter | default | what it does |
|---|---|---|
| `length_m` | 2.05 | full length up to the top of the crystal |
| `grip_fraction` | 0.62 | `socket_grip_main` at this fraction of the length from the bottom (per the reference) |
| `grip_off_fraction` | 0.45 | `socket_grip_off` (the second hand) |
| `segments` | 3 | 3 or 4 long bones |
| `shaft_radius_m` | 0.018 | shaft radius at the grip (= the default grip radius of the hand) |
| `crystal_height_m` | 0.21 | crystal height, clamped to 0.18–0.24 |
| `crystal_radius_m` | 0.038 | |
| `talons` | 4 | 3 or 4 talons of the head |
| `talon_length_m` | 0.82 × crystal height | |
| `bend` | 1 | multiplier for the bending of the bones (0..2) |
| `origin` | `bbox` | `bbox` - the X/Y center of the bounding box at zero (§4); `axis` - the shaft axis at zero |

As in the reference: the shaft is made of three long bones end to end - the lower "tibia" (a triangular cross-section with a ridge in
front, a bulb-like "ankle" on the ground, a plateau on top), the middle "femur" (condyles at the bottom, a rough line at the back, a
round epiphysis on top), the upper one - with a thin "fibula" along it (the dark groove of the reference) and a wide
condylar flare on top. The joints are knobby, with a "kneecap" in front of each; the bones are slightly bent in
seed-defined directions (the grip bone has half the bend, the tangent at the grip deviates from +Z by less than 0.6°).
Joints are moved apart if they fall into the hand zone (±5 cm from the grip plus the epiphysis): a knob stays above the grip, as in the
reference. The head: a "knee" of two lateral condyles, a petal-like "palm" and 3–4 talons of three phalanges each
(knobby joints, the last phalanx is a sharp claw), pointing outward and curved inward above the crystal; the front
talon stands right in front of the crystal. The talons keep a gap ≥ 3 mm to the crystal (the tilt increases until they
clear). The crystal is an irregular heptagonal prism tapering at the bottom with a two-step faceted tip, flat
shading, a separate material slot `Crystal`: Principled, base #227A42, emission #38C060 × 0.4, roughness 0.18.

Item frame (§6): stands on z = 0, shaft along +Z, the front talon toward −Y. `socket_grip_main` / `socket_grip_off` are on the shaft
centerline, +Z = the tangent of the shaft, −Y is forward (the common grip frame); `socket_fx_tip` is in the center of the crystal,
+Z up (the direction of the effect emission).

Measurements (seed 0): 3 268 tris / 4 500 (3 talons and 4 bones, seed 4 - 3 532); dimensions 0.18×0.18×2.05 m; joints at
0.677 and 1.381 m; `socket_grip_main` at 1.271 m (0.620 L), the shaft radius there is 18.0 mm; `socket_grip_off` at 0.923 m;
`socket_fx_tip` at 1.945 m; crystal 0.21 m; 1 680 vertices, GLB ~125 KB. Assembly < 1 s.

## 3. bone_toggle - a "barrel" fastener with gold

| parameter | default | what it does |
|---|---|---|
| `length_m` | 0.10 | length of the bone |
| `gold` | `rings` | `rings` - two gold rings on the shaft; `caps` - gold sleeves on the inner half of the knobs (like the two fasteners of the reference with gold binding); `none` |
| `loop` | true | a gold loop around the middle, extended backward (what the fastener is sewn on with) |
| `bone_radius_m` | 0.056 × length | |

A short horizontal bone along X with forked knobs at the ends (a "dog bone" silhouette from the front), material
`Gold` - metal 1.0, roughness 0.32. **The origin is the center of the back face** (max Y = 0, centered in X and Z),
local −Y is the outward surface normal: it is placed on a robe by aligning the origin with a surface point and −Y with its
normal. Measurements: `rings` - 532 tris / 600, `caps` - 564, `none` without a loop - 304; dimensions 0.102×0.026×0.034 m,
276 vertices, GLB 25 KB.

## Materials and style

All materials are Principled BSDF → Material Output (in glTF - PBR metallic-roughness): `Bone` #CDBE9C, roughness 0.7;
`Gold` #C9A04A, metallic 1; `Crystal` with emission. The unwrap is `UVMap` (smart project). Style `aged`: the material
`Bone_aged` takes Base Color from the vertex color attribute `Color` (in the GLB - `COLOR_0`, baseColorFactor = 1; verified
by reading the GLB): ivory × shading (AO by rays against a BVH) × stains (3D noise). Because glTF multiplies `COLOR_0` by the
color of all primitives of the mesh, the vertex colors of the gold and the crystal are white; the Workbench preview tints them only
for the duration of rendering.

## Tests

`test_bone_kit.py`: 21 Mixamo bones and hierarchy on both sides; each detail is exactly one bone with weight 1.0, 14 phalanges
on 14 bones, the end `*4` bones without weight; rotating `Index2` moves only the middle and distal phalanges of the index finger;
the "wrist - tip" length = `length_m` (±1.5 %), scale from `length_m`, a budget of 3 000 in all poses and with five
rings; the socket frame is orthonormal, right-handed, +Z toward the thumb, −Y along the fingers, on the palmar side; grip
R 18 and 25 mm - nothing cuts in deeper than 0.6 mm, the distal phalanges are ≤ 4 mm from the cylinder, the socket is on the axis; poses without
intersections and distinguishable; mirroring of left/right and seed determinism. Staff: length ±0.5 %, ≤ 4 500 tris, grip fraction
±0.003, the frame, an 18 mm shaft at the grip and joints farther than 6 cm from it, `socket_fx_tip` in the center of the crystal, the slot `Crystal`
with emission and roughness ≤ 0.2, bone #CDBE9C / 0.7, variants (fraction 0.5, 4 bones and 3 talons, clamping of the crystal height).
Toggle: ≤ 600 tris in all modes, length, origin at the center of the back face, metallic gold. The `run` protocol:
files, a skin with 21 joints, the socket is a child of the Hand joint in the GLB, the staff sockets are in extras; `COLOR_0` only for `aged`,
white colors for the gold; UV and Principled on all parts, 0 non-manifold edges; batch (mixed parts, the name `../../escape`
stays inside `out_dir`, a batch by number); parameter errors.

## Honest limitations

- **The thumb in the grip** is the result of an optimization that runs into the limits (radial abduction 50°, CMC −10°):
  the thumb lies on the shaft from above as a "hook" next to the index finger rather than being anatomically opposed. There is
  contact (0.7 mm), there are no intersections.
- **A grip with rigid bones of real length** gives an angular wrap: on the pinky and the index finger the DIP is almost straight
  (2–16°), the pinky embraces the cylinder as a "hook". By default the grip axis is perpendicular to the hand; in a living hand it
  is oblique by 15–20° (`grip_tilt_deg`).
- **The socket is the same for all poses**: in `open`/`claw` an item on it would hang in the palm, only `grip` clamps it.
- **The rings** in the grip touch the cylinder with an overlap of up to 0.5 mm (the check tolerance is 0.6 mm); the rings are simple bands without
  the ornament of the reference.
- **The bones are procedural** (bodies of revolution and lofts), not a sculpt: no holes, grooves or chips; the wrist is a block of
  irregular ellipsoids; the bases of metacarpals II–V and the base of metacarpal I touch / overlap each other (they are on the same
  bone or at the CMC joint - excluded in the intersection check, not visible under deformation).
- (2026-10-10, night) The hand is placed on a character by `place_parts`, and its `socket_mount_hand_<side>` goes with it.
  `rig_humanoid` keeps this socket as the hand socket of the rig (`docs/forge/humanoid.md`, "Sockets from the input"), so
  the kit's staff sits by aligning the sockets without an offset.
- **Merging with a character rig is not done by this step.** The names match Mixamo, but the rest is the generated pose, and the
  bone rolls are its own (Z is the dorsal side): to merge the hand into a `mixamorig` rig and play Mixamo clips, one has to
  align `<Side>Hand` and account for the difference of rest poses (§7) - this is the job of the humanoid module / the integrator.
- **The hand origin is the wrist**, not the "bottom at z = 0" of §4 (this is more convenient for attaching to a rig); `origin: ground` gives §4.
- **"Aging" is only vertex color**: the resolution equals the mesh density (hand ≈ 1 400 vertices, staff ≈ 1 700),
  no texture is baked; the relief of wear is not modeled.
- **The head**: the talons are polylines of three straight phalanges rather than smooth arcs, they hold the crystal with a 3 mm gap and do not
  touch it; the crystal is opaque (emission + low roughness, no transmission, for glTF compatibility).
- **The preview** in Workbench softens metal and does not glow with emission; comparison images are rendered in EEVEE (for documentation only).
- Time: a hand takes 2.5–4 s inside Blender because of the grip search; tests ~80 s.
