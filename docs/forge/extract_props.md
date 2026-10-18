# The extract_props module — weapons, shields and banners separated from the body (2026-10-12)

A Hunyuan generation from a concept such as "a knight with a sword and shield" is **one fused mesh**: the sword, shield, banner, bow, staff
and book are melded into the hand, forearm and cloak. Skinning such a mesh bends the sword together with the arm, crumples the shield on the
forearm, cuts the blade with the body and stretches the plate armour like rubber. The `extract_props` step
cuts each item out of the body and turns it into an item per the contract (§6): the body is rigged separately, and the items
return to the hands rigidly — exactly where they were in the concept.

Files: `scripts/blender/steps/extract_props.py` (the step), `steps/_props_lib.py` (search, cut, frame, check,
kit replacement, in-clip check), `scripts/blender/tests/test_extract_props.py`, registration —
`server/forge-steps.mjs`, the `attach_prop` input — `server/forge-pipeline.mjs` (`state.extracted`). Related changes:
`rig_humanoid` (item sockets, grip by them, rigid plate armour — humanoid.md), `attach_prop` (`extracted`, `skin_separate` — items.md).

## Place in the pipeline

```
cleanup → paint_from_concept → extract_props → rig_humanoid (body, hard_surface auto) → retarget
        → attach_prop (extracted) → motion_style → render_sprites → godot_package
```

`attach_prop` comes **after** `retarget`: items ride the hand bones rigidly in any order, but the shield needs the real clip frames
for its socket (see "Back into the hands"). The order "rig → attach → retarget" also works
(the shield is then at its concept position, `extracted_shield_fit: placement`).

Input — `painted.blend` from painting (slots `blend`, `mesh`). Outputs: `body.glb/.blend` (the body without items + place sockets
`socket_prop_<hand>_<n>`), `props.json`, `items/<id>/item.glb|.blend|.json|preview.png`, `exploded.png`,
`detect.png`, `grips.png`. The chain carries `props.json` onward (`state.extracted`); `attach_prop` with
`extracted: true` receives it in `inputs.extracted` in the same run; the character for it is the `.blend` of the previous
step (rig or `retarget`; place sockets live on the hand bones and survive `retarget`).

## How items are found

1. **Normalisation as in the rig** (`prepare_body`): facing -Y, height by the top of the head, centred. New is
   `front_prior: "-Y"`: generations from frontal concepts face the camera, so the detector's answer of "back" (+Y)
   or an uncertain one (< 0.3) yields to -Y (a confident side answer of +-X stays — an archer standing sideways).
2. **The rig detector in "find only" mode** (`run_landmarks`): `find_props`, `find_blades`, `find_bows`,
   `find_plates`, a banner on a pole, `bind_props`, vertices — `prop_vertex_sets`. The rig verdict (`lm.fail`) does not
   stop the search: on a fused mesh the rig failed precisely because of these items.
3. **`roles.json` hints** (`params.hints`: `hand_weapon`, `hand` r|l|both, `shield`, `notes`): "banner + staff"
   → the banner in the main hand, the staff in the other; "two swords" → a sword in each; "... in the left hand" in `notes` is
   the second item. Hints name the type and enable searches the rig does not do:
   - **a pole the hand trace did not pick up** (the hand went into the banner cloth): a straight item on the hand side from the
     hint with a fist on the axis within the shoulder's reach — is held; the pole reaches along its axis through the cloth;
   - **banner cloth**: slice components on the outer side of the pole, connected in height to a piece touching
     the pole above the fist (cloth below the hand stays with the banner);
   - **a shield** the rig's "plate" search did not take (merged with the arm, pole and cloak): a RANSAC plane over the points of the
     shield side, disc R95 >= 0.11 H, not the chest itself;
   - **a short weapon with a pommel** (mace, axe 0.17-0.25 H) — including one hanging on the belt: it goes
     to `Hips`, not to the hand.
4. **Bow**: a limb pressed against the leg is lost by the rig detector; it is found by mirroring the found limb through the grip and
   by chains of thin side sections (does not always work — see limitations).

## How it is cut

- **A straight item** (sword, staff, pole) is cut by **planes across the axis at the edges of the fist**. The fist edge is taken from the
  profile of sections along the axis (slabs of 0.01 H, points within 0.04 H of the axis, spread across the minor axis): the fist is round and
  wide (~0.045 H each way), the guard is flat, the blade and pole are thin; the edge is no farther than 0.03 H from the fist centre
  (the wrist and bracer are "wide" too). A guard beyond the edge is collected by the connected 2D component of the section containing the axis.
  A grip at the end (sword): the blade side is taken, the pommel stays in the body's fist. A grip in the middle of a pole: both sides.
  **The fist stays with the body.**
- **Shield**: rig vertex marks + growth along the mesh within the **board surface**: a quadratic surface over the front
  face (fitted to the "outer" layer, so the arm and forearm behind the board drop out); behind the face — 3 cm at the
  centre (the grip is there) and up to 0.075 H at the edge (a convex board, a thick rim); never inside the forearm/upper-arm tube.
- Small body pieces completely enclosed by an item, and islands hanging nearby (the banner finial) go to the item.
- Both sides of each cut are closed with caps (`holes_fill`), a cap takes the UV and material of the neighbouring edge —
  **the paint stays on both parts**; the face winding of the cap is made consistent with its neighbour (volume > 0 test).
- **Handle through the fist** (`bridge`): a cylinder along the axis in place of the cut-out fist, section colours — for a sword from the kit
  or, with replacement, the item is whole; on the body the fist hides it.

## The item: frame, sockets, place

- Grip frame (§6): `socket_grip_main` at the fist centre on the axis, **+Z** — by the rig rule (`prop_grip_dir`: blade —
  toward the far end, staff/pole — up), **-Y** — the blade edge (the wide side of the blade) or forward of the character;
  shield: +Z along the long axis of the board upward, -Y — the face (away from the body), the grip 3.5 cm behind the centre of the back side — the same
  frame as `socket_mount_shield_*`; bow: +Z up along the grip, -Y toward the back of the bow (string on +Y, as in the kit).
- `socket_grip_off` — the second hand on the item axis (if the other hand's wrist is near the axis) or the rule for long
  items (staff/spear/banner > 1.15 m: 0.14 of the length below; two-handed sword: 11 cm below). `socket_fx_tip` — on staves,
  wands, torches, lanterns at the far end.
- The item stands at z = 0, centred in X/Y, `af_item_type` in extras (as in `item_kit`).
- **Place on the body**: an empty `socket_prop_<hand>_<n>` (extras `af_bone`, `af_hand`, `af_item`, `af_item_kind`,
  `af_grip_mode`) in the world grip frame. `rig_humanoid` keeps the input sockets and makes each a child of `af_bone`;
  `attach_prop` moves the hand socket onto it (`refit_socket`) or the shield socket, and deletes it.

## Quality check and kit replacement

Gate for every cut item (`gate`): triangles >= 120; open edges <= 2 % (banner — cloth, <= 15 %);
**not shreds**: the largest piece >= 75 % of the area (for a grip in the middle of a pole and for a bow — the two largest: an item of two
halves by construction); pieces over 1 % of the area <= 6; size by type (sword 0.4-1.45 m, staff 0.8-3 m, shield
0.25-1.7 m and thickness < 0.3 of the size, ...), width (sword <= 0.42 m — otherwise the body was grabbed).

`replace`: **auto** — an item that fails is built by `item_kit` of the same kind (sword → sword/greatsword with the blade length =
grip → tip - 3 cm and the blade width; staff, spear → length; shield → kite / tower / round / buckler by proportions
and size; bow → length; book), style by side (undead — `dark`), ornament `kit_ornament`, **colours —
from the cut item**: area-weighted k-means of texture colours by faces → palette roles (light grey — `metal`, dark warm
— `grip`/`wood`, gold — `trim`, saturated — `accent`). The kit item takes the same place (its
`socket_grip_main` onto the grip frame of the cut item). **never** — always the cut item; **always** — always the kit. For a banner,
skull and lantern the kit has no counterpart — the cut item stays (a warning).

## Back into the hands: attach_prop extracted

Weapon, staff, bow, banner: `socket_mount_hand_<hand>` is moved onto the item's `socket_grip_main`
(`refit_socket`, replacement by socket will repeat the grip). An item on the belt — rigidly on `Hips`.

**Shield** (`extracted_shield_fit`, default `auto`): at the concept position (`placement`) it ends up **edge-on in the clips
and cuts the arm** — the concept holds the shield in front of the chest, while Mixamo clips lower and rotate the hand (in a test run
the shield was upright and facing outward in 0 of 43 frames, and intersected the arm/body in 43 frames; depth up to 10 cm at the shoulder).
So on a character that already has clips (`attach_prop` after `retarget`), `auto` = **`clips`**:
`socket_mount_shield_<hand>` is the canonical shield socket (as for kit shields), fitted to the clip frames:
the shield stands, the face outward **and forward** (`shield_front_bias` 1: with a lowered arm, strictly "away from the chest" means edge-on to the
camera from the front), death does not take part in the fit (lying down, the shield cannot stand), idle/walk cycles weigh double,
the concept rest pose — 5 %. In that test run the shield stood and faced outward in 67 % of frames (more without the death frames),
intersections in 3 frames of 43; body gaps unchanged. The grip frame of the cut shield is the same as that of the socket — the shield
sits on the socket without correction; the place socket `socket_prop_l_*` is deleted. At rest the shield now stands not where it was in the
concept (the rest pose is not visible in sprites). The mode (`extracted_mode`):
`bone_child` (the step default) or **`skin_separate`**: a separate mesh skinned 100 % to the hand
bone. Both are rigid; `skin_separate` is the one to use when `motion_style` (a separate module) looks for items
in the hand by weights >= 0.99: that way a staff stays vertical in the step, a sword tip does not go through the floor, the item lies down
in death. It does not see an object under a socket (`bone_child`). The hand socket is moved onto the grip anyway — in Godot the item
is swapped by aligning sockets.

`rig_humanoid` by place sockets: the hand that held a cut item is solved **at its grip** (otherwise a fist
raised forward without a sword was traced into a book on the belt or the hem of a cloak); the shield — only if the landmarks did not
agree without it; arm asymmetry in the holding hand is not an error.

## Check in clips

After `motion_style`, `_props_lib.clip_props_qa` runs on `styled.blend`: on 12 frames of each clip,
the item's shift from where the hand bone carries its rest position (a rigid item — 0), and **how much deeper
than at rest** the item's vertices go into the body (the item stands where it was in the concept, touching the fist and forearm;
what the animation added is counted; threshold 2 cm). "Inside" = behind the nearest body face **and** surrounded by the body: rays
in 8 directions hit the body within 0.6 m at least 7 times (a sword behind an open cloak or hem is not "in the body").
Depth is counted up to 10 cm (beyond that — "10 cm"). In state: `props_qa`, in the report — the "Check" column.

## Parameters

| Parameter | Default | |
|---|---|---|
| `height_m`, `front`, `front_prior`, `fix_yaw`, `markers` | 1.8, auto, —, true, — | normalisation as in `rig_humanoid` |
| `hints`, `use_hints` | —, true | `roles.json` entry |
| `replace` | auto | auto / never / always |
| `gate` | — | `{open_edge_frac, main_part_share, parts_over_1pct, min_tris}` |
| `bridge` | true | handle through the fist |
| `kit_style`, `kit_ornament`, `seed` | auto, 1, — | kit for replacement |
| `short_weapons` | true | short weapon with a pommel, and the belt |
| `min_item_vertices` | 60 | smaller finds stay with the body |
| `preview`, `preview_size` | true, 320 | `exploded.png`, `detect.png`, `grips.png`, item previews |

`props.json`: per item — `id` (`r_sword`, `l_shield`, `hip_mace`), `type`, `type_from` (hint / geometry /
plate on pole / bow_detector), `kind`, `hand`, `source` (extracted | kit), `grip_mode`, `fist` (edges, profile),
`gate` (numbers and reasons), `dims`, `colours`, `kit` (replacement parameters), `bridge`, `files`, `placement_matrix`,
`placement_socket`, `grip_m`, axes; `detector` (what the rig found, hints, pole extension, bow limb, shield by
hint, absorbed pieces), `cut`, `prepare`.

## Honest limitations

- **What is found is what the rig detector sees, plus hints.** A pole passing diagonally behind the back
  (a banner), a bow raised horizontally for a shot, a crossbow at the chest, a book on the belt (not in the hand)
  stay on the body — with the same artefacts as before. An item with neither a hint nor a fist on it is not searched for.
- **The fist edge comes from the section profile, no more than 0.03 H from the fist centre.** A thick guard may partly stay on the body,
  and the fingertips may go away with the item; both ride the hand bone, invisible in the animation, but the item from the
  file carries a "stump".
- **The handle in the fist is a conventional cylinder** (`bridge`), not the real generated handle (there is none inside the fist).
- **Shield**: the strip of board right against the forearm (a 0.03 H tube around the bones) stays with the body and rides with the arm; a shield
  thicker than 0.3 of its size (cloth or arm grabbed) goes to kit replacement — and then the pieces of cloth cut out with it
  vanish together with it.
- **Kit replacement** takes the colours but not the emblem or the silhouette of the concept: a kite shield may become round
  (proportions < 1.25), the emblem becomes the kit's emblem. For a banner, skull, lantern the kit has no counterpart.
- **The banner is rigid** — the cloth is not simulated, it only waves together with the hand.
- **A two-handed grip is not solved**: the item is on one hand bone, `socket_grip_off` is only a marker; there is no IK of the second hand
  in the pipeline.
- **The shield rides the hand bone** (contract §6), not the forearm: when the wrist bends it rotates around the
  wrist. There is one shield socket per character: one rotation correction for all clips; in "hit to the head" (`hit`) and in
  a swing the shield does not always stand. A real "shield and sword" pose is the Mixamo sword-and-shield clips, and the library has almost
  none of them (except idle angel); `params.shield_bone forearm` was tried — not better.
- **Place = the concept pose** for weapons, staves, banners: the item stands where it was on the fused mesh, so
  at rest it may touch the forearm; the in-clip check counts only the penetration added by the animation.
- **`front_prior` -Y** is correct for generations from frontal concepts; a model that is truly turned away
  it will turn incorrectly.
- **A flat slab under the feet** (Hunyuan attaches it to the feet) is not an item, it stays on the
  body and tilts with the feet; that is the job of `cleanup`.
- **Bow**: a lower limb pressed against the leg is searched for by mirroring the upper one and tracing sections — it is found on a test
  archer, on others the limb may stay with the body.
