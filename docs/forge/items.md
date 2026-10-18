# The items module: a kit of weapons and props, items from generated meshes, attaching to a character

Blender steps: `item_kit`, `item_from_mesh`, `attach_prop` (shared code: `scripts/blender/steps/_items_lib.py`).
Self-check: `scripts/blender/tests/test_items.py` has 23 tests, all passing, ~41 s (2026-10-10: + `refit_socket`, `grip_dir`).
All numbers in this document were produced by a single run of `test_items.make_evidence()` on the code of
2026-10-10, 17:10. How to run it: see "How to run".
Compatibility with `server/forge-steps.mjs`: `item_kit` accepts `batch` as a number (N adjacent seeds) or a list, and
`dims` as an object; `item_from_mesh` accepts `budget` and `grip: [x, y, z]`.

## Axes and the grip frame

- An item stands on `z = 0`, Z up, meters, transforms applied. The main axis (blade, haft, handle) is **+Z**, the
  edge / front side is **−Y**. `params.origin`: `bbox` (default, the X/Y center of the bounding box at zero) or `axis`
  (the handle axis at zero).
- `socket_grip_main` is the common grip frame: local +Z leaves the fist on the thumb side (along the blade / haft
  away from the wrist), −Y points where the straightened fingers point (toward the edge / face), X = Y × Z. For a
  sword, axe, staff and shield the frame coincides with the item axes. It is rotated for: blaster pistol grips (+Z
  along the slanted grip, 16°), crossbow stocks (+Z forward along the stock), and the crossbow's `socket_grip_off`
  (left hand from below).
- `socket_grip_off` is the second hand of two-handed items; `socket_fx_tip` is the effect point, with local +Z = the
  emission direction. In extras: `game_socket`, `kind`, `frame`.
- Shields: the face is −Y, a bracket handle stands vertically behind the center (a "center grip"), and the enarme
  strap is modeled.
- Blasters point their muzzle to −Y by default (the item frame). For props whose nose is +Y, use
  `params.forward: "+Y"`: the item is rotated by 180° about Z and the sockets keep their frames relative to the
  geometry. Which variant a consuming project needs is for that project to decide (see the limitations).

## 1. item_kit: the procedural kit

22 types: `sword, greatsword, dagger, axe, battle_axe, mace, warhammer, spear, halberd, staff (head: crystal | skull | orb |
crook), wand, bow (with a string), crossbow, shield_round, shield_kite, shield_tower, buckler, torch, potion, book,
blaster_pistol, blaster_rifle`.

```json
{"step": "item_kit", "params": {"type": "sword", "style": "medieval", "seed": 3, "ornament": 2,
  "palette": {"metal": "#c0c4ca", "grip": {"color": "#402818", "roughness": 0.8}},
  "blade_length": 0.8, "guard": "curved", "pommel": "scent"}, "out_dir": "abs/out"}
```
- `style`: `medieval | dark | elven | scifi | random`; palettes are by material role (`metal, metal_dark, trim, grip, wood,
  accent, gem, bone, emit, …`), and `palette` overrides color / metallic / roughness / emission.
- `seed` changes the proportions and all `auto` substyles. For swords the spread is wider: blade length ±20 %, width
  ±30 %, grip ±20 %, guard ±35 %; blade profile (`straight / tapered / broad / leaf / falchion`, the falchion is now
  in medieval too), guard curvature, pommel, fuller length (`fuller_length`, 0.42–0.82 of the blade), ricasso
  (`ricasso`, in medieval/dark with a 50 % probability, 3.5–6.5 cm, with a taper and a blunt edge). The same seed gives
  the same mesh.
- `ornament` 0–3: 1 is rivets, rings, finials, an emblem; 2 is gems, tassels, the mace's back spike, the book strap; 3
  is runes (emission), the umbo on the kite/tower shield, "wings" on the spear.
- Dimensions per type: `blade_length, blade_width, blade_thickness, grip_length, grip_radius, guard_width, pommel_radius,
  haft_length, haft_radius, head_size, head_radius, flanges, shaft_length, shaft_radius, head_length, head_width,
  length, radius, brace_height, recurve, size, width, height, thickness, flame, shape, emblem`; `budget_tris`.
  `size` is the scale of the crossbow / blasters; the preview size is `preview_size` (the preview used to take `size`
  and inflate the crossbow to 158 m; fixed, with a regression test).
- `params.batch`: a list of variants (each on top of the common params) or a number of adjacent seeds → a subfolder
  per variant (the name is sanitized to `[A-Za-z0-9_-]` and checked to be inside `out_dir`) + `contact_sheet.png` +
  `batch.json`. Batch outputs: `glb` and `blend` are lists (the pipeline takes the first), `preview` is the sheet,
  `json` is `batch.json`. Sheet: `sheet_cols`, `sheet_tile`, `sheet_title`, `sheet_scale` (`common` is a common scale,
  so lengths are comparable; the default if all variants are of one type; otherwise `fit`). Tile caption: triangles,
  ornament, length.

Modeling: section lofts (a blade with a fuller, bevels, ricasso and a point; teeth on the dark style), lathe bodies
(grips with a spiral wrap, pommels, ferrules, flasks), extruded outlines with a bevel (axe heads with a separate
light edge, mace flanges, a pick, a halberd blade), pipes along a curve (bowstring, shield rim, handle bracket, staff
crook), faceted crystals, a low-poly skull. One mesh per item, 3–8 Principled BSDF materials (base color / metallic /
roughness / emission are exported to glTF), a `UVMap` unwrap, smoothing by angle.

Fixed in this round regarding shading: axe cheeks and the halberd blade (extruded outlines of variable thickness)
now get interior vertices (constrained Delaunay triangulation, `plate(fill=…)`), so the radial streaks from fan
triangles are gone; along the way an old bug was found and fixed: the axe-head outline closed as a "bow" (the eye
points went in reverse order), which caused creases at the eye. The mace's back spike is a forged rhombus with a
taper, 2.7 cm thick at the eye (it was a flat wedge). The shield enarme is a 7 mm × 36 mm strap with rivets, standing
off the board. The `materials_principled` check now really verifies that a Principled BSDF is connected to the
Material Output.

Outputs: `item.glb` (socket nodes with extras, `af_item_type` and `af_forward` in the mesh extras), `item.blend`,
`item.json`, `preview.png`.

### Measurements (all types, ornament 2, seed 0)

| type | tris / budget | size, m (X×Y×Z) | type | tris / budget | size, m |
|---|---|---|---|---|---|
| sword | 2074 / 2500 | 0.04×0.18×0.97 | bow | 1184 / 4000 | 0.04×0.20×1.47 |
| greatsword | 2730 / 4000 | 0.06×0.25×1.57 | crossbow | 1018 / 4000 | 0.64×0.84×0.13 |
| dagger | 1566 / 2500 | 0.03×0.12×0.40 | shield_round | 2260 / 4000 | 0.78×0.13×0.78 |
| axe | 1614 / 2500 | 0.05×0.26×0.71 | shield_kite | 3380 / 4000 | 0.58×0.10×1.03 |
| battle_axe | 3186 / 4000 | 0.05×0.49×1.16 | shield_tower | 2164 / 4000 | 0.66×0.16×1.24 |
| mace | 1324 / 2500 | 0.12×0.12×0.64 | buckler | 1500 / 4000 | 0.32×0.16×0.32 |
| warhammer | 2084 / 4000 | 0.07×0.33×1.18 | torch | 1346 / 2500 | 0.08×0.08×0.65 |
| spear | 1238 / 4000 | 0.07×0.07×2.07 | potion | 496 / 2500 | 0.07×0.07×0.18 |
| halberd | 1706 / 4000 | 0.08×0.33×1.96 | book | 884 / 2500 | 0.08×0.20×0.24 |
| staff (orb) | 1658 / 4000 | 0.10×0.10×1.90 | blaster_pistol | 1110 / 2500 | 0.04×0.28×0.17 |
| wand | 752 / 2500 | 0.02×0.02×0.39 | blaster_rifle | 1856 / 4000 | 0.05×1.00×0.24 |

Staffs: skull (dark) 2964, orb 1658, crook 1510. A sweep of 176 builds (22 types × 4 styles × seed 1–2, ornament 3):
the maximum for one-handed items is 2250 / 2500 (sword medieval s1), for two-handed 3664 / 4000 (shield_kite medieval
s1); non-manifold edges: 0 in all of them. The GLB of an item is 28–118 KB. A batch of 25 variants with previews takes
6.7 s.

Variants by seed (common scale): 6 swords with seeds 101–106 have lengths of 0.81–0.97 m; profiles broad / tapered /
falchion / tapered / leaf / leaf; cross and curved guards of different widths (0.13–0.23 m); pommels scent / teardrop /
disc / ball; 1550–1938 tris. Silhouettes now differ by the length, width and shape of the blade, not only by the guard
curvature. 4 staffs: crystal (elven), orb, crook, and a skull with horns and glowing eye sockets (dark, ornament 3);
1510–2964 tris.

## 2. item_from_mesh: an item from a generated mesh

```json
{"step": "item_from_mesh", "inputs": {"mesh": "abs/hunyuan_sword.glb"},
 "params": {"kind": "blade", "length_m": 0.95, "grip": "auto", "texture_max": 1024,
            "crop": {"segment": [[x,y,z],[x,y,z]], "radius": 0.15}}, "out_dir": "abs/out"}
```
Order: import and merge → weld the vertices split along UV seams → optional crop (`crop`: box or capsule) → removal of
small detached pieces → PCA over triangle areas → a profile of sections along the axis → grip → the handle axis from
the section centers → the blade side from the head area → rotation into the item frame, scaling to `length_m` →
Collapse decimation to the budget → textures larger than `texture_max` are reduced → sockets, export. Outputs:
`item.glb/.blend/.json`, `preview.png`, `profile.png` (the section profile: the red line is the main grip, the green
one the second hand).

Grip rules (a length > 1.15 m makes a blade / axe / mace / polearm / staff two-handed and adds `socket_grip_off`):
- blades: the grip is the longest round stretch of constant radius in the handle-side half, measured on vertices in
  6 mm layers and only within 4.5 cm of the axis (the far ends of the guard do not interfere; a narrow spike pommel,
  a teardrop pommel and a ricasso ring are round but shorter or of a different radius). The one-handed grip is the
  middle of the handle; the two-handed one is 5 cm below the guard, with the second hand 11 cm lower (no closer than
  3 cm to the pommel and never closer than 9 cm to the main one);
- axes / maces: one-handed, the thinnest round window near the butt; two-handed, at 0.41 and 0.115 of the length from
  the butt (as in `item_kit`);
- polearms / staffs / wands / torches: a fraction of the length (`GRIP_T`); a known type (`type: spear | halberd`)
  uses the proportions of the kit; the second hand is at 0.14 of the length from the butt (polearms) or 0.14 of the
  length below the main one (staff);
- shields: the center of the back plane + 3.5 cm, the face is the convex side;
- `grip.point` / `grip.t` / `edge` are set manually (needed for firearms, bows and everything off-axis).

Budget: by default the module budget (2500 one-handed / 4000 two-handed, staff, shield); a larger `budget_tris` (for
example the server's) is cut down to the module budget with a warning unless `allow_over_budget` is set. Checks:
`tris_within_budget`, `within_module_budget` (an error; with `allow_over_budget`, a warning), `sockets_in_glb` (on the
exported GLB), `grip_section_holdable` (the section at the grip is round with radius 0.5–4.5 cm, otherwise the grip
landed on the blade / head), `materials_principled`, `length_m`.

Measurements on "generated" copies of kit items (Subdivision ×2, random rotation, scale 1.7):

| item | tris before → after | main grip error | second hand: error / gap | rule |
|---|---|---|---|---|
| sword | 33 600 → 2 462 | 0.5 cm | — | middle of the handle |
| greatsword | 43 631 → 3 939 | 0.2 cm | 0.2 cm / 11 cm | 5 cm below the guard |
| dagger | 25 472 → 2 462 | 0.0 cm | — | middle of the handle |
| axe | 27 952 → 2 462 | 0.4 cm (edge on −Y) | — | thin window near the butt |
| battle_axe | 56 896 → 3 938 | 0.5 cm | 0.2 cm / 34 cm | 0.41 / 0.115 of the length |
| mace | 22 496 → 2 462 | 1.6 cm | — | thin window near the butt |
| warhammer | 34 432 → 3 940 | 0.2 cm | 0.3 cm / 35 cm | 0.41 / 0.115 of the length |
| halberd | 30 048 → 3 940 | 0.5 cm (blade on −Y) | 4.0 cm / 55 cm | fraction 0.42 |
| spear (`type: spear`) | 20 064 → 3 940 | 0.3 cm | 0.1 cm / 50 cm | kit proportions |
| staff (orb) | 26 720 → 3 940 | 10.8 cm | 7.8 cm / 27 cm | fraction 0.56 |
| kite shield | 58 120 → 3 940 | 3.9 cm (2.0 along Y) | — | center of the back, face by curvature |

The two-handed grips pointed out by review (the greatsword 17 cm lower, the battle axe 22 cm) now match the kit to
within 0.5 cm. Robustness to the handle shape: 54 runs (sword, greatsword, dagger × seed 1–6 × medieval / dark /
elven) give a mean error of 0.68 cm and a maximum of 2.9 cm (a dark sword with a spike pommel); by style: medieval ≤
1.1 cm, elven ≤ 0.5 cm, dark ≤ 2.9 cm.

## 3. attach_prop: an item into the hand

```json
{"step": "attach_prop",
 "inputs": {"character": "abs/character/mixamo_rigged.fbx",
            "clips": ["abs/character/anims/attack.fbx", {"file": "abs/character/anims/idle.fbx", "loop": true}]},
 "params": {"attachments": [
   {"item": "abs/sword/item.glb", "hand": "r", "mode": "bone_child"},
   {"item": "abs/kite/item.glb", "hand": "l", "mode": "bone_child", "fit": "auto"}]},
 "out_dir": "abs/out"}
```
Single variant: `inputs.item` + `params.hand / mode / offset / fit / item_scale`. Others: `clips_check` (true),
`frames_per_clip` (12), `preview_clip`, `thumb_hint`, `standoff` (shields), `refit_shield_mount`, `replace`,
`socket_along` (0.42). The shield-socket fitting code and the pose QA (`fit_shield_mount`, `fit_rotation`,
`evaluate_pose`, `Body`, `pose_record`) lives in `_items_lib.py`, and `rig_humanoid` calls it too.

- Character: `.blend`, `.glb`, `.fbx` with `mixamorig:*` bones. The height is measured in the rest pose (it used to be
  measured in the clip pose: re-attaching to its own `equipped.glb` gave 1.45 m instead of 1.70 m).
- Clips: a path or `{"file", "name", "loop"}`; the action name is the file name (or `name`); looped ones get the
  suffix `-loop`, and `loop` defaults to true for the names `idle* / walk* / run*`. For one of the test characters the
  GLB has `attack` and `idle-loop`.
- Hand socket: if the rig has no `socket_mount_hand_<r|l>`, it is created on the hand bone (0.42 of the hand length
  from the wrist, on the palm side by the palm thickness + 1.5 cm; +Z is the thumb side = the character's "forward";
  if an item is sewn into the hand, along its axis, with a warning). **This is the only hand frame of the forge**: the
  same function `_items_lib.hand_socket_frame` builds the hand sockets in `rig_humanoid` and `import_mixamo_rig`
  (`_humanoid_lib.add_human_sockets`), so `attach_prop` takes them as they are. On a real generated humanoid after
  `rig_humanoid`, the socket matches what `attach_prop` would compute itself to 1e-4 (both in `.blend` and after glTF),
  and the kit sword sits without fitting: the grip is 0.8 cm from the hand, the blade along the socket +Z (cosine >
  0.995); this is checked by a test in `test_humanoid` (the real-rig kit sword and shield without refit). For a hand
  holding a found item, `rig_humanoid` passes its direction (`grip_dir`: +Z exactly along the item) and excludes the
  item's vertices (`exclude`); such a socket is more accurate than what `attach_prop` would guess from the sewn-in
  item.
- **Shields have a separate mount socket `socket_mount_shield_<r|l>`** (the fix for the main review remark). It is a
  child of the hand bone, and its frame = the frame of `socket_mount_hand_<side>` × the fitted rotation + an offset.
  The fit is done once per character and hand on a canonical shield (the shield grip frame: +Z up, −Y the face; board
  0.54 × 0.80 m, 16 mm, the back 3.2 cm in front of the grip, a bracket handle), not on a specific shield: 7 680
  rotations are enumerated, the goal is that the shield stands and looks outward at rest (weight 0.35) and on clip
  frames, the 8 best are checked for intersection with the body; then an offset along the face until the open hand no
  longer pierces the board (up to 12 cm). Any kit shield or `item_from_mesh` shield is attached by aligning its own
  `socket_grip_main` with this socket, the same way Godot will do it. If the rig already has the socket (a
  `rig_humanoid`/`import_mixamo_rig` rig, or a rerun on `equipped.blend`), it is used as is and not changed; it is
  re-fitted only with `refit_shield_mount: true`. `rig_humanoid` builds `socket_mount_shield_l/r` with the same fit
  (`_items_lib.rest_shield_mounts` → `fit_shield_mount`, one implementation) for the rest pose, exactly what
  `attach_prop` does with `fit: rest`: on a real humanoid the world matrices of the shield from the rig's socket and
  from a socket rebuilt by `attach_prop` match to 1e-4. With clips, `attach_prop` would fit the socket on their frames
  too, but the rig's socket takes precedence ("once per character"). The name `socket_mount_shield_*` is part of
  the socket naming; the 4×4 matrix is still duplicated in the extras `shield_mount_matrix` of the hand socket for
  early readers.
- `bone_child`: the item is a separate object under its own mount socket (in glTF: hand joint → socket → item);
  `skin_rigid`: the item mesh is merged into the character mesh with weight 1.0 on the hand bone. The self-check
  `grip_on_mount`: the `socket_grip_main` of the attached item matches the mount socket (error 0.0 mm / 0.0°).
  `offset` is a fine adjustment of a specific item in the frame of the mount socket; it lives on the item node and is
  not reproduced when swapping by sockets (it is written to `attach.json`).
- **`refit_socket` (2026-10-10).** `offset` lives on the item node, and swapping by sockets does not reproduce it. With
  `refit_socket: true` (in params or on an attachment), the offset is baked into the socket itself:
  `socket_mount_hand_<side>` in the rest pose is moved onto the item's `socket_grip_main` (`H_new = H · offset`) and
  remains a child of the hand bone. The item hangs under it with an identity offset, `swap_by_socket_matching: true`.
  The world fit matches a run without refit to 1e-4. The move is written to `attach.json → socket_refit`
  (`delta_pos_m`, `delta_angle_deg`, before/after matrices) and to the socket extras (`refit_offset_matrix`,
  `refit_by_item`), so the following swaps place an item the same way. The other items of the same hand are
  recomputed into the new frame. For shields on the shield socket, refit does not apply. The test
  `test_attach_refit_socket_bakes_offset_for_swaps` checks that the world matrices match, that the socket = old ×
  offset, and the swap by alignment.
- **`grip_dir`.** It is `[x, y, z]` or `up/down/forward/back/out/in` in the character's frame (facing −Y, `out` is
  away from the body). It works for a socket that `attach_prop` creates itself when the rig has none: +Z exactly along
  this direction (`_items_lib.grip_dir_vector`, the same `hand_socket_frame`). `rig_humanoid` has the same parameter,
  `params.grip_dirs`.
- Names: on the attached copy `socket_grip_main` is removed (it coincides with the mount socket), the other sockets
  get a hand suffix (`socket_grip_off_r`, `socket_fx_tip_l`), with no dots or repeats; identical materials of two kit
  items are merged (no `mat_medieval_trim.001`). `replace: true` removes the items that a previous run put into the
  same hand.
- Checks (BVH): the distance from the handle axis to the hand, the "grasp" (the share of radial rays that hit the hand
  within 6 cm), item↔hand/body intersections at rest and on frames, points of the blade axis inside the forearm, and
  for a shield the verticality, the face direction and the hand through the board; `mount_sockets_in_glb`,
  `socket_names_clean`, `animations_exported`.
- Outputs: `equipped.glb`, `equipped.blend`, `attach.json` (hand and mount sockets with frames, fit matrices, the
  selection, per-frame checks), `preview.png` (rest + 3 clip frames), `grip_closeup.png`.

### Items extracted from a character: `extracted` (2026-10-12)

`extract_props` (docs/forge/extract_props.md) cuts a sword, shield, banner, bow and staff out of a generated body into
separate `item.glb` files and leaves the place sockets `socket_prop_<hand>_<n>` on the body. `attach_prop` with
`params.extracted: true` (a server flag: the server passes the `props.json` of this run in `inputs.extracted`; the
character is the `.blend` of the previous step: the rig or `retarget`) puts each item where it was cut from:

- weapon, staff, bow, banner: `fit: placement`, offset = hand socket⁻¹ × place socket, then `refit_socket`:
  `socket_mount_hand_<hand>` moves onto the item's `socket_grip_main` (swapping by socket repeats the grip, and a kit
  item of the same frame sits the same way);
- shield: by `extracted_shield_fit` (`auto` by default): `placement` puts `socket_mount_shield_<hand>` onto the grip
  frame of the extracted shield (created if absent); **`clips`** fits the canonical shield socket to the frames of the
  character's clips (`fit_shield_mount`: death does not take part, loops weigh double, rest 5 %, "outward" is tilted
  toward the character's front by `shield_front_bias`, 1), and the rig's rest-fitted socket is replaced; `auto` means
  `clips` if the character already has clips (a pipeline with `attach_prop` after `retarget`), otherwise `placement`.
  The reason: in place of the concept pose, Mixamo clips put the shield on its edge and press it into the arm (in one
  test, 0 of 43 frames had the shield "standing and looking outward" and there were 43 intersections; with `clips`,
  67 % and 3). For fitting, the step takes 8 frames per clip and also with `clips_check: false`; `refit_socket` is not
  needed for a shield;
- an item on the belt (`hand: hip`): rigidly on `Hips` at its place;
- afterwards the place sockets are removed; with no items the character passes through unchanged.

`extracted_mode`: `bone_child` (default) | **`skin_separate`** | `skin_rigid`. `skin_separate` (also in
`attachments[].mode`): the item stays a separate mesh, but skinned 100 % to the hand bone: one vertex group, an
Armature modifier, the armature as parent. The rigidity is the same as `bone_child`, but `motion_style` sees it as an
item in the hand (by weights ≥ 0.99): a staff stays vertical in a walk, the tip is not under the floor, the item lies
down in death. Verification in real clips: `_props_lib.clip_props_qa` after `motion_style` (grip shift, extra
penetration into the body), see extract_props.md.

### item_kit as a replacement for the extracted item

`extract_props` builds a kit item when the extracted one fails the quality check (`replace: auto`) or always
(`always`): `_props_lib.kit_params` translates the type and measurements into kit parameters (sword: `blade_length` =
grip → tip − 3 cm and `blade_width`; staff/spear: `shaft_length`; shield: kite / tower / round / buckler by
proportions with `width`/`height`/`radius`; bow: `length`), the style by side, `ornament`, and `palette` from the
dominant colors of the extracted item (k-means of texture colors over the faces). The build is the same
`item_kit.build_item`.

### Measurements

| run | grip → hand (rest) | grasp | intersections at rest | frames with intersection | other |
|---|---|---|---|---|---|
| test humanoid (1.70 m), sword R, clip attack | 1.2 cm | 0.39 | 0 | 2 of 15 (body, up to 28 tris) | blade in the forearm: 0 frames |
| same character, kite shield L → `socket_mount_shield_l` | 1.1 cm | 0.19 | 0, hand behind the board | 4 of 15 (body, up to 175 tris) | offset 10.4 cm; at rest up·Z = 0.86, face·outward = 0.95; stands and looks outward in 47 % of frames |
| same character, skull staff R, `skin_rigid` | 1.2 cm | 0.39 | 0 | 3 of 15 (up to 38 tris) | one mesh with a skin in the GLB; sockets `socket_fx_tip_r`, `socket_grip_off_r` |
| second test humanoid (2.0 m), crystal staff L, clips idle-loop + attack | 0.2 cm | 0.61 | 0 | 16 of 27 (up to 51 tris) | see the limitations |

Swapping a shield by socket: in the saved scene the kite shield is removed, and a round shield is placed only by
aligning its `socket_grip_main` with `socket_mount_shield_l`: up·Z = 0.86, face·outward (+X) = 0.82, the same as the
fitted one. The test `test_attach_shield_mount_swap_by_socket_matching` checks that such a swap gives the same world
matrix (to 1e-4) as the step itself, that in glTF the socket is a child of the hand joint and the shield a child of
the socket, and that a rerun on `equipped.blend` with `replace` reuses the socket.

The step time with an attack clip is 2.7 s inside Blender; the `equipped.glb` of the first test character is 1.9 MB
(the character texture), of the second 0.8 MB.

## How to run

```
"C:/Program Files/Blender Foundation/Blender 5.0/blender.exe" -b --factory-startup ^
    --python scripts/blender/af_run.py -- item_kit request.json        (item_from_mesh / attach_prop likewise)
"C:/Program Files/Blender Foundation/Blender 5.0/blender.exe" -b --factory-startup ^
    --python scripts/blender/tests/run_tests.py -- items
```
The numbers of this document (≈50 s, the OS temp folder; an optional image directory as the second argument):
```
"C:/Program Files/Blender Foundation/Blender 5.0/blender.exe" -b --factory-startup --python-expr ^
  "import sys;sys.path[:0]=[r'<repo>/scripts/blender',r'<repo>/scripts/blender/tests'];import test_items as t;t.make_evidence(None, r'<image dir>')"
```
`make_evidence` prints the path to `evidence.json` with all the measurements.

Tests: all kit types build within budget, without non-manifold edges, and export with sockets; axes/frames;
determinism and seed spread; the batch (list outputs, sanitizing the name `../../escape`), the `size`/`preview_size`
regression; `materials_principled` catches a disconnected BSDF; `forward: +Y`; outline filling; recovery of a sword,
greatsword, axe, battle axe, halberd and shield from "generated" copies (second hand ≥ 9 cm lower), grips by
seed/style, budget cutting; a synthetic rig (`bone_child` / `skin_rigid`), `offset`, shield swap by socket, `-loop` clip
names / clean sockets / height at rest; real ones: a humanoid with sword+shield and an attack clip, a humanoid with a
`skin_rigid` staff, a second humanoid with a staff (idle + attack, and the sewn-in sword in the right hand), and a
staff cut out of a generated character. The real tests return `"skip: …"` if the file is missing.

## Honest limitations

- **Hands do not close.** For Meshy/Tripo characters without finger bones the hand is open (the claw-like hands of
  one test character), and the handle lies in the palm/fingers (grasp 0.19–0.39 instead of ≥ 0.5 for a fist).
  Closing a fist is the job of the humanoid module.
- **A rigid fit does not adapt to a clip.** In an A-pose a sword and staff point horizontally forward; in an attack
  the sword touches the body in 2 of 15 frames, the shield in 4 of 15, and the shield stands and looks outward in only
  47 % of frames (the clip swings the "claws" rather than holding a shield). Clips made for the item, or a per-frame
  hand correction, are needed.
- **Second test character:** the right hand holds a sewn-in sword and all clips hold it with two hands, so a staff in
  the left hand passes through the body in 16 of 27 frames. The fit is right; the conflict is in the clip content.
- **A shield in an open hand** is offset by 10.4 cm (by the canonical shield) so that the fingers do not pierce the
  board: the shield handle is not in the fist. A fit "on the forearm through the enarme" is not modeled; the fitted
  socket replaces it. A shield thicker than the canonical one can touch the hand, which the check
  `hand_through_board_rest` catches.
- The rig's `socket_mount_shield_<side>` is fitted **only on the rest pose** (`rig_humanoid` has no clips); if the
  shield must also stand in the frames of a specific clip, `refit_shield_mount: true` with those clips is needed.
  `offset` is not carried over when swapping by sockets.
- **The thumb side** is guessed from the character's "forward" (correct for Mixamo A/T-poses) or from a sewn-in
  item; for other poses `thumb_hint` is needed.
- **item_from_mesh guesses the grip geometrically.** A staff is placed by a fraction of the length (error 10.8 cm), the
  halberd's second hand is 4 cm off, a mace 1.6 cm, a shield 3.9 cm, a dark sword with a spike pommel up to 2.9 cm.
  Firearms, bows and off-axis items require `grip.point` / `edge`. Collapse decimation eats thin spikes. An item cut
  out of a character carries stubs of the hand and the whole texture atlas (a staff cut from a generated character
  is a 6.0 MB GLB).
- **Materials are PBR constants per role**, wear is not baked: there is an unwrap, but no texture. The Workbench
  preview softens metallic (viewport values only; the GLB values are exact) and does not show the texture of a staff
  cut from a generated mesh.
- Small geometry: on a one-handed axe the inner edge of the light rim is slightly jagged (an offset curve); on the
  battle axe tiny light bevel chips are visible at the eye corners; both are visible only in close-up.
- Blasters point their muzzle to −Y by default (the item frame); `forward: "+Y"` exists, but which variant a
  consuming project requires is that project's decision.
- A torch flame is a static emissive mesh; the potion glass is opaque.
