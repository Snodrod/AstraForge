# humanoid module: local auto-rig for Mixamo

Three Blender steps (contract: `docs/FORGE-CONTRACT.md`, §3–§7):

| Step | What it does |
|---|---|
| `rig_humanoid` | any humanoid mesh -> a 25-bone `mixamorig:*` skeleton (65 with fingers), weights, rigid props, sockets, checks, preview |
| `prepare_mixamo` | FBX for manual upload to mixamo.com (port of the reference pre-Mixamo conversion script); optionally an already skinned FBX so that Mixamo skips its own auto-rig |
| `import_mixamo_rig` | FBX downloaded from Mixamo (cm, `mixamorig:`) -> metres/height, original textures from the GLB, sockets, a clip named per contract §7, `rigged.glb/.blend` |

Code: `scripts/blender/steps/{rig_humanoid,prepare_mixamo,import_mixamo_rig,_humanoid_lib}.py`,
tests: `scripts/blender/tests/test_humanoid.py`.

## How rig_humanoid works

1. **Loading and cleanup.** All input meshes are merged into one `Body`; armatures, Multires/Subsurf and shape keys
   are discarded (with a warning), other modifiers are baked.
2. **Facing direction.** The wide axis at shoulder level is the lateral one; the sign comes from the feet, head and
   hands. Models from some generators (e.g. Tripo) face +X; the detector sees this. `params.front` overrides the
   auto-detection.
3. **Residual rotation (yaw).** After rotating by a multiple of 90 degrees, the tilt of the lateral axis is
   measured: PCA of the body points at shoulder level (XY) and the line between the feet (taken into account only
   if it agrees with the shoulders; in a stride the feet stand diagonally). Above 5 degrees (`max_yaw_deg`) the
   figure is turned to face -Y, with a warning (`fix_yaw: false` leaves it alone). The noise on 11 "straight"
   assets is <= 2.4 degrees. Inputs that were rotated by 11-21 degrees previously produced crooked arms.
4. **Normalization.** Feet on z = 0, height measured at the **crown** (a staff above the head does not reduce the
   height), x = 0 is the symmetry axis of head/torso, y = 0 is the middle of the torso. If the full detector
   (without shields and props) finds the axis away from zero, the body is shifted and the landmarks are
   recomputed (previously a shield displaced Hips by 10 cm).
5. **Auto landmarks** (`Landmarks`). The surface is sampled to 200,000 points; horizontal slices every 0.5 % of
   the height; within a slice there are connected components, neighbouring slices are linked. From this:
   - **props**: chains of thin components with a straight axis, height at the ends (pommel, guard/grip).
     **Straightness is robust to mesh decimation** (`_chain_axis`): one cross-section per slice, the largest
     component (small fragments closer than 0.03 H, such as fingertips at the grip or a section torn by large
     triangles, do not count); the line is fitted discarding up to 15 % of slices (`PROP_TRIM`); the rms
     threshold of 0.006 H is unchanged. If more than 25 % of the chain's slices have a second genuine component
     (a bow limb and its string, `PROP_SPLIT_MAX`), it is not a single shaft: it is checked strictly, over all
     centroids, as before (so an archer's bow is not taken for a "staff"). Reason (smoke run 2026-10-10): after
     `cleanup` to 40,000 triangles the centroid rms of a staff grew from 0.005 to 0.007, the staff was lost, the
     arms "diverged" by 0.231 H and the rig was not built.
     **Grip** is the body mass sticking to the shaft; beyond the end of the straight part (a fist on a hilt above
     the guard) a grip counts only if it is the bottom of a free arm chain. **No found item is left on bone
     heat**: it goes to the holding hand; otherwise to the hand if the axis passes closer than 0.08 H to it;
     otherwise to the nearest torso bone (Hips/Spine), with a warning. Check `props_rigid`;
   - **shields**: "plates" (RANSAC plane, disc) at the hand. A plane **behind the back or above the shoulders**
     (wings, cloak) does not count as a shield. The hand behind a shield is placed behind the shield centre
     (forearm along the back of the shield, length by proportion);
   - **legs/crotch**: components rising from the floor; the merge of the legs is the crotch. If above the merge
     the legs are still separated by a thin bridge and then the section jumps to a full ring, this is a **coat
     or tunic hem**: the crotch is hidden and is placed by proportion at 0.46 H (previously the crotch of
     coated characters landed on the hem, leg length 0.30 H);
   - **neck**: the minimum of the section under the crown;
   - **shoulder joint (head of Arm) from the arm itself**: height = top of the free arm chain (armpit) + an
     offset that grows the closer the arm is to the body (0.035 H at abduction >= 43 degrees, up to 0.11 H for a
     hanging arm); in X, the side edge of the deltoid at that height minus the arm radius, within 0.09-0.15 H.
     Calibration: a reference Mixamo-compatible character rig, the joints of Human Base Mesh, a Mixamo auto-rig
     of an archer test character, a mannequin. If the arm is hidden (sleeve, staff), a proportion under the neck
     is used; a shoulder measured on one side is mirrored to the other;
   - **elbow**: the kink of the arm centreline, searched only where upper arm : forearm is about 0.8-1.5 (the
     flare of a gauntlet at the wrist no longer becomes an elbow); in a T-pose, slices along X;
   - **knees/ankles/feet**: from the leg tracks.
6. **Gross landmark errors stop the step** (`ok:false`, no `rigged.glb`, `markers.json/png` are written,
   confidence <= 0.25): upper arm : forearm outside 0.6-2.0, forearm < 0.09 H, leg < 0.33 H, mirrored
   ForeArm/Hand further than 0.10 H apart if neither hand holds a found prop/bow/shield (checks
   `landmark_proportions`, `arm_symmetry`); also no support (a ghost) and non-human proportions.
7. **Skeleton**, symmetric shoulders and hip joints.
8. **Weights**: bone heat on a welded/decimated copy (fallbacks 1/2, 1/4, voxel remesh), Data Transfer to the
   full mesh, suppression of arms outside the "tube", arm/leg kept apart, robe (Hips + thighs with a smooth
   transition), rigid props 100 % on a bone (vertices inside the shaft capsule are added even if long blade
   triangles have no samples, otherwise a sword tip stayed on the floor as a strip), at most 4 influences.
9. **Sockets** §6 (9 of them, including shield sockets), checks, preview, export.

### Weights on cut clothing and small islands (2026-10-10, evening)

Found while rebuilding a robed test character (v2). Everything works by default, the QA thresholds were not
changed.

1. **Small islands do not enter the heat map.** A bone_kit hand (27 separate bones), clasps, beads, eye
   spheres: pieces smaller than `island_max_frac` (1 %) of the vertices. The Blender solver failed on the
   **whole** mesh (100 % of vertices without weights on all fallback proxies). Such pieces are removed from the
   proxy; pieces closer than 0.006 H to each other are merged into a cluster (a hand is one cluster). After the
   weight transfer every cluster is bound rigidly to the bone nearest to its centre: a hand to `Hand`, a clasp
   to `Spine*`, an eye to `Head`. Report: `stats.skin.small_islands`.
2. **Proxy repair.** Edges with three or more faces and degenerate faces (welding of AI cloth layers, ends of
   cuts) also crashed the solver. `repair_for_heat` deletes the faces around such edges (up to 3 passes),
   dissolves degenerate and isolated vertices. The proxy is used only for the weight transfer. Report:
   `attempts[].repair`.
3. **Surface-aware weight cleanup** (`surface_aware`, true by default). A sleeve cut off the robe (`separate`
   `slit`) hangs next to the thigh but is connected to it over the surface only through the shoulder.
   `limb_surface_distances` computes geodesic distances (Dijkstra over the welded vertex graph) to each arm,
   the torso and the legs. Seeds are the vertices that explicitly belong to a chain: closer than 0.6 x the
   distance to any other chain and no further than 0.15 H. The arm tube trimming keeps the arm weight if the
   vertex is closer to the arm than to the torso over the surface by `surface_margin_h` (0.05 H): this is a bell
   sleeve. The arm weight is cut if the vertex is noticeably closer to the torso over the surface: the side of
   a robe next to a hidden arm. The arm/leg separation compares geodesic distances instead of straight ones.
   For fused meshes the surface paths are short and everything works as before.
4. **Suppression of arm bleed into the torso** (`arm_bleed`, [0.10; 0.35]). The residual arm weight `a` on
   vertices where the torso weight is larger is multiplied by `smoothstep(0.10; 0.35; a)`. The shoulder
   transition (a > 0.35) is untouched, the ramp is continuous, the removed weight goes to the torso bones on
   normalization.

Effect on the robed test character, "arms forward / up / squat / stride", % torn:

| input | v1 | without items 1-4 | with items 1-4 |
|---|---|---|---|
| fused sleeves (v1) | 14.3 / 19.7 / 0.90 / 1.21 | - | - |
| sleeves cut, ngon caps, hem open | - | 14.3 / 19.9 / 0.66 / 0.83 | 1.64 / 5.3 / 0.22 / 0.73 |
| same, caps as a strip along the height (final v2) | - | - | **0.88 / 5.4 / 0.21 / 0.74** |

No regression was found on the asset set used for testing, see the table "No regression after the weight
cleanup".

### Input sockets, `grip_dirs` (2026-10-10, evening)

- **Input sockets are kept** (`keep_input_sockets`, true by default). Empties `socket_*` / with `game_socket` are
  recorded before cleanup (`load_body`) and go through the same input -> rig frame matrix as the body
  (`prep["matrix"]`, with re-centring). A socket whose name is built by the rig itself (hands, hips, back, head,
  chest) takes its frame and extras from the input instead of being recomputed: for a bone_kit hand placed by
  `place_parts` this is the axis of its grip. The others become children of the bone whose weight dominates at
  the 8 nearest vertices: `socket_fx_eye_l/r` -> `Head`. `params.socket_bones {name: bone}` overrides the
  choice, `replace_sockets [names]` rebuilds a socket. Report: `stats.input_sockets`,
  `stats.sockets_from_input`, `hand_socket_frame.*.source = "input"`.
- **`grip_dirs`**: `{"l"|"r": [x, y, z] | "up" | "down" | "forward" | "back" | "out" | "in"}` in the rig frame.
  +Z of the rebuilt `socket_mount_hand_<side>` goes exactly along this direction. The same
  `_items_lib.hand_socket_frame` (`grip_dir`) is used, there is one implementation. An item that is later swapped
  by aligning `socket_grip_main` faces the right way: a staff held vertically gets `"up"`.
- Tests: `test_input_sockets_survive_rig_and_grip_dirs`,
  `test_small_islands_kept_out_of_bone_heat_and_bound_rigidly`, `test_repair_for_heat_drops_nonmanifold_faces`.

### Bows and curved props in the hand (2026-10-11)

A bow (or a curved blade) held in one hand used to break the rig: the bow limb became a continuation of the
arm, the wrist was placed on the bow limb, and the rig was rejected for arm asymmetry (0.26-0.28 H on the test
archers). It is now found by `Landmarks.find_bows` (right after the torso, before the shields, so a bow can no
longer become a "plate" and pull the sleeve along):

1. **Candidates** are thin components of horizontal slices (section < `BOW_THIN` 0.042 H) **beside the body**:
   the centre is further than 0.12 H from the axis, no point closer than 0.10 H; **not behind the back** (a
   quiver on the back, a cloak): the centre is in front of the back surface of the torso (below the torso
   profile, in front of Y0 + 0.05 H).
2. Candidates are linked over neighbouring slices into chains (a fist on the grip splits the bow into an upper
   and a lower limb). On each side, chains that do not lie in one plane with the rest are discarded one by one.
3. **It is a curved item** if: the vertical extent is >= 0.35 H, the plane is almost vertical (|n.Z| < 0.5) and
   holds the centres (rms <= 0.012 H), and it is curved: a line through the main sections of the levels gives
   rms > 0.008 H **or** at >= 30 % of the levels near the limb there is a string.
4. **It is held**: the fist is the densest 0.08 H stretch along the arc where body mass below the shoulder
   sticks to it (an arc passing by the shoulder does not count as a grip), above the knees, to the side of the
   torso; >= 25 sample points and within reach of the shoulder (<= 0.42 H). Otherwise (the bow stands beside
   the character) nothing changes.
5. The bow points are removed from the landmarks (the arm is found by the arm), the hand gets a "staff-like"
   grip along the bow chord; the whole bow is rigid on the bone of this hand (`rigid_bow_<hand>`,
   `props_rigid`); +Z of the hand socket goes up along the riser (`bow_riser_up`; for a curved blade held by
   its end, to the far end); the arm symmetry check knows that this arm holds an item. The symmetry thresholds
   were not changed.

`markers.json` -> `bows` (kind, hand, ends, length, sag, plane, grip), `detector.bows` (why a candidate was
accepted or not), `stats.bows`, `stats.rigid` (`kind: "bow"`).

Observed effect on test characters: bows held vertically beside the body went from rejections (wrist on the
bow limb, arm symmetry 0.26-0.28 H) to accepted rigs (symmetry 0.017-0.032 H, bow rigid on the left hand,
torn area 0-0.75 % in the pose check). Cases that remain unchanged by design: a bow that stands beside the
character with the fist out of reach is not taken; a bow raised horizontally (a shot) is not a vertical plane
and is outside `find_bows`; a short crossbow (0.13 H) is not taken by `find_bows`, its stock with an arc is
caught by `find_plates` as a rigid "plate" of the hand. A bow whose lower limb is pressed to the leg is
treated as a straight item, both parts rigid on the hand, and may fail the squat check (the lower limb fused
with the foot).

Tests: `test_bow_held_beside_body_rigid_quiver_ignored` (a mannequin with a 1.3 m bow and a quiver on the
back; mutation: without `find_bows` the rig is rejected for asymmetry).

### Swords, maces and shafts at an angle (2026-10-11, night: `find_blades`)

On a batch of generated concept characters, swordsmen did not pass the auto-rig: a sword held forward,
diagonally or down-and-aside crosses the horizontal and vertical `find_props` slices obliquely, where it is
wider than their threshold (0.045 H in Z, 0.032 H in X/Y) and becomes part of the arm: an "arm" of 0.6-0.95 H,
rejection. Now:

1. **Diagonal slices.** `find_props` additionally cuts the cloud across 11 directions (`OBLIQUE_DIRS`: 6 face
   diagonals, 4 cube diagonals, so any line is no further than ~27 degrees from one of them, and the forward axis
   -Y for maces/swords stretched straight forward; the X axis is not used, those are arms in a T-pose). A thin
   component of a slice: section < 0.055 H or flat (< 0.08 x < 0.03 H, a two-handed sword 0.06 H wide).
   Straight chains (`_chain_axis`, rms <= 0.006 H) no further than 37 degrees from the slice normal are
   candidates. A candidate that coincides with an already found item is discarded: the behaviour of the earlier
   axes is unchanged.
2. **It is an item in the hand** (`Landmarks.find_blades`, after the torso, before bows and shields) if: length
   >= 0.25 H (the chain is first extended along its axis while points lie on the axis and nothing surrounds
   them, because a blade fuller or a fragment at the guard broke it); not behind the back; a **fist** sits on
   it: the densest 0.08 H stretch of body mass at the axis (>= 30 points), above 0.28 H, within reach of the
   shoulder (<= 0.47 H) and either on the free arm chain or to the side of/in front of the torso core (an arm
   pressed to a cloak merges with it in the slices); the end farthest from the fist is a **free tip** (0.03-0.09 H
   of emptiness behind it) or a **pommel/head** (a compact lump <= 0.16 H within 0.2 H beyond the end, not at
   the floor, entirely out of reach of the arm: a mace ball, an axe, a banner finial are included in the item),
   not closer than 0.2 H to the fist and **farther than a bare arm reaches** (0.45 H from the shoulder:
   shoulder + forearm + hand). A forearm that merged with the blade into one line through the fist is cut off:
   the part on the far side of the fist that does not reach the shoulder or is noticeably thicker than the
   blade is the arm.
3. The item's points are removed from the landmarks (the wrist on the fist, not on the tip), the grip is at the
   fist (`end` at the hilt, `shaft` in the middle of the shaft), the whole item is rigid on the hand bone
   (`props_rigid`), +Z of the hand socket points to the far end (`prop_grip_dir`), the arm symmetry check knows
   about the item. `markers.json` -> `props[].oblique`, `detector.blades` (why a candidate was accepted or not:
   length, fist, tip, reach).

Also found on the same generations and fixed:
- **Body axis after a shield.** A large shield at the torso pulled the median of the waist and the body axis by
  0.09-0.1 H (the spine went along the edge of the shield, the head to the side). If after a shield/bow/blade
  the axis without them shifts by >= 0.03 H, it is recomputed (small shifts are left alone, they oscillated
  between centring passes); the centring in `run_landmarks` takes up to three passes.
- **Rotation about Z.** A weak shoulder axis (PCA < 1.6) contradicted by the feet (> 20 degrees) is no longer
  "corrected": a tower shield gave a warrior 38.7 degrees with the feet at -1.5 degrees.
- **"Plates" that are not shields.** A flat skirt of a cloak/tabard in the frontal plane beside the body
  (|n.Y| > 0.8, not in front of the torso) and a plate further than 0.15 H from the forearm/hand of its side do
  not count as a shield (rigid on the hand, it tore at the shoulder in every clip: 12-21 % of the area on a
  skeleton swordsman). The exception is a banner on a shaft: a plate at the upper half of a vertical item is
  rigid on the hand that holds the shaft.
- **Minimum leg 0.31 H** (was 0.33): stocky plate-armoured generations measure 0.325-0.33 H; the "crotch on the
  hem" error gives ~0.30 H and is still caught.
- `params.deform_gate` (`error` | `warning`): in a production batch the pose check is only a report, the real
  clips decide (`motion_style`, see motion_style.md).

Results of that batch, `rig_humanoid` with defaults on meshes after `cleanup` to 40,000 triangles: of the
characters that previously failed with an "arm" of 0.62-0.97 H, most now pass the landmark checks with swords
or maces found and bound to the correct hand, and their pose-check tear is in the usual range (a few percent,
more where a cloak is involved). Remaining failures from that batch, kept as honest limits:
- a cloak billowing sideways breaks face detection (front +X instead of -Y), and even with
  `front: -Y, fix_yaw: false` the cloak is taken for the left arm;
- a tower shield that stands on the floor counts as a leg (body axis on the shield, shoulders 0.66 H);
- a two-handed sword held straight forward with face detection backwards (+Y: a cloak behind pulls the
  head/hands over); with `front: -Y` the sword is found (1.3 m -> RightHand) but the legs are 0.30 H < 0.31;
- a bow held horizontally/diagonally: an item outside a vertical plane is not taken by `find_bows`;
- a mace held forward from a robed character: it is bound rigidly, but the fist is merged with the robe and the
  rigid mace pulls the robe sheet, so the pose numbers get worse than with the bone-heat weights.

No regression: earlier test assets give the same ok/rejection, the same props and shields, joints in place to
0.0 cm, except one (0.39 cm at the shoulder; arms up 0.46 -> 0.34 %).

Tests: `test_oblique_sword_held_forward_rigid_to_hand` (a mannequin with a flat 8 cm blade forward-down-aside;
mutation: without `find_blades` the blade is not an item and the wrist leaves the fist; on a mannequin without
a sword no blade at all).

### Props extracted before the rig, rigid plate armour (2026-10-12)

With the batch of 2026-10-12, items of generated characters are cut out by the `extract_props` step
(docs/forge/extract_props.md) before the rig: the rig gets a body without sword, shield and banner and
**placement sockets** `socket_prop_<hand>_<n>` (extras `af_bone`, `af_hand`, `af_item_kind`, `af_grip_mode`).
What changes in `rig_humanoid`:

1. **The placement socket is a child of its bone.** `af_bone` from the extras takes precedence over the choice
   by weights (`params.socket_bones` takes precedence over it). `attach_prop` later moves the hand or shield
   socket onto it.
2. **The hand is solved on the grip of the extracted item** (`use_placement_grips`, true by default): the
   placement sockets of weapons, staff, bow, banner give `Landmarks` a "known grip" (`params.known_grips_in` ->
   in the rig frame on each centring pass), and the arm goes into `grip` mode as if the item were in place.
   Without this, a fist raised forward empty was traced into a book on the belt (a mage: confidence 0.33, the
   rig was not built) or into the hem. The shield is only a fallback: if the landmarks did not converge without
   it, the pass is repeated with the shield grip (`stats.shield_grip_retry`); on one knight the shield grip
   immediately worsened the poses (0.12 -> 3.7 % in "arms forward").
3. **The holding arm** (`held_hands` from the sockets) is not an arm symmetry error: the pose of the arm that
   held a sword stays "holding".
4. **`front_prior`** (pipeline: -Y): a facing answer of "back" (+Y) or an uncertain one (< 0.3) yields to the
   prior; generations from frontal concepts face the camera (`stats.facing.overridden`).
5. **Shoulders versus a "solid" crown.** A crown, a nimbus of spikes, a crest or horns above the head enter the
   crown height, and the shoulders came out below 0.70 H (a crusader: 0.67-0.69 -> "not humanoid"). If the
   shoulders are outside 0.70-0.88, they are also measured from the upper slice where the head width is
   >= 0.045 H (`info.solid_head_top`); the scale is not changed.
6. **`arm_clamp_h`** (0.47 in the pipeline after `extract_props`, off by default): if the arm chain went past
   the hand into a cloak, a hem or a merged item and is longer than this, the wrist is placed at the typical arm
   length in its direction, confidence 0.3, a warning. This is a guess instead of a rejection.
7. **Short weapon with a head** (`short_headed_min_len`, `belt_items`, enabled only by `extract_props`): a mace
   or axe with a 0.17-0.25 H shaft whose head is beyond arm reach is also an item; if the "fist" on it is a
   thigh, the item hangs on the belt and is bound to `Hips`.

**Rigid plate armour** (`hard_surface`: `auto` | `on` | `off`; `build` from `roles.json`). The metal mask comes
from the colour map of `paint_from_concept`: low saturation (< 0.28) at value 0.22-0.97 (steel, silver, and also
skeleton bone). `auto` is enabled for `armored` / `heavy`, or if metal is > 35 % of the area (`robed` / `light`:
> 50 %). A plate is a connected region of metal (the welded vertex graph) where one bone from `HARD_BONES`
(head, Spine...Spine2, Hips, shoulder, forearm, hand, thigh, shin, foot) dominates with weight >= 0.78: every
plate of >= 30 vertices goes entirely to this bone. Joints (metal up to 8 rings from the plates) are relaxed
harmonically with the plates pinned, without a weight jump at the edge; cloth, leather and the rest keep the
earlier smooth weights; rigid props are untouched. Report `stats.hard_surface`: share of metal, plates per
bone, share of rigid area, and the pose check **twice**: with soft weights (`pose_test_soft`) and with plates
(`pose_test_hard`): torn, stretched (> 1.8x) and **plate "rubber"**: the share of the area of rigid plates
whose triangles changed by more than 25 %.

Pilot of 2026-10-12 on real clips of the character (motion_style, worst frame of the clip; idle / walk /
attack|cast / hit / death), "soft" being the same rig with `hard_surface: off`, "plates" being the batch result,
on five armoured characters (plates 9-16, rigid area 7-25 %) and one robed character where it stays off
(`robed`, metal 40 %):

- plate "rubber" in clips dropped from 2-28 % of the plate area to <= 0.26 % in all clips (0 on most);
- the total torn share barely changes: it falls for some characters (-0.4...-3 points) and rises by 0.2-0.6
  points for others, because the stress moves from the plate into the joint (the relaxation strip), and the
  joint stretches more;
- in the pose check (arms forward / up / squat / stride) the torn area stays at the same level
  (e.g. 0.12 / 3.27 / 0.03 / 0.03 -> 0.23 / 3.67 / 0.19 / 0.07 on one knight, "rubber" 2.9-3.3 -> 0);
- the large percentages of two of the characters are not plates but the cloak, the hem and the legs under it
  (robe 25 %, "thighs"): that is a cloth problem, not a rigidity problem.

Conclusion, honestly: **plates no longer stretch**.

### Bone axes (roll)

Local **+Y** goes from the bone head to its tail. **+Z** is forward of the character (-Y) for spine, neck, head,
clavicles, arms, hands and legs (`GLOBAL_NEG_Y`); for `Foot/ToeBase/Toe_End` **+Z is up**. This is the
convention of the reference Mixamo-compatible rig. The native Mixamo skeleton has the arms rotated differently
(X backwards); clips are transferred by the retarget algorithm of the reference script (world increments + a
rest-pose correction). `import_mixamo_rig` does not change the Mixamo roll.

### Sockets and grip

All 9 human sockets of §6: `socket_mount_hand_r/l` and `socket_mount_shield_r/l` (hand bone),
`socket_mount_back`, `socket_mount_hip_l/r`, `socket_fx_head`, `socket_fx_chest`; extras `game_socket`, `kind`.
The same set is created by `import_mixamo_rig` (the shared `add_human_sockets`).

**The hand frame is one for the whole forge**: `_items_lib.hand_socket_frame`, the same function with which
`attach_prop` builds a socket if the rig has none. The centre is on the palm side of the hand axis, 0.42 of the
hand length from the wrist (palm thickness + 1.5 cm), **+Z** is the thumb side = "forward" of the character,
perpendicular to the hand axis (the bone roll does not matter), **-Y** is along the knuckles, X = Y x Z.
`attach_prop` takes such sockets as they are. Verified on a test character
(real-asset test): the rig sockets and the frame that `attach_prop` would
compute match to 1e-4 both in `rigged.blend` and after glTF (`rigged.glb`); on a male base mesh and on another
test character's right hand also 0.0 cm / 0.0 degrees. A sword from the kit on this rig
(real-asset test): `attach_prop` does not create a socket, the
grip is 0.8 cm from the hand surface and inside its bounds, the tip lies exactly on the +Z axis (cosine > 0.995),
with no intersections with the arm and body at rest.

For a hand with a found item `rig_humanoid` passes to the same function `grip_dir` (+Z is set directly from the
item, independent of the roll) and the mask of the item's vertices (`exclude`: they do not count for the hand
length and palm thickness):
- a grip within the extreme 35 % of an item shorter than 0.75 H (sword, axe, mace by the handle) points **to the
  far end**: for a sword blade down +Z points down;
- a staff or spear (grip in the middle or a long shaft) points up along the shaft; horizontal points to the
  long part.

`stats.grip_socket_vs_prop_axis_deg` measures the angle of the socket's +Z to the geometry (grip -> far end or
the axis up), not to the same hint; `stats.grip_socket_rule` is the rule and the vectors;
`stats.hand_socket_frame` is the source of +Z (`character_forward` / `grip_dir`), the number of hand vertices
and of excluded item vertices. For such hands the rig socket differs from what `attach_prop` would guess from
scratch for a "baked-in" item (differences of 2.7 degrees up to 180 degrees on the test characters: the
`attach_prop` heuristic "long part forward" would point +Z down a staff), but `attach_prop` does not recompute
the rig socket. The origin of the socket on a hand with an item lies 3.4-9.1 cm from the item axis: a new weapon
will stand next to the baked-in one, not exactly in its place.

**Shield sockets** `socket_mount_shield_r/l` (`shield_mounts: true`) are a fit of `attach_prop` for the
canonical shield (§6: +Z up, -Y face, grip at the handle in the centre of the back side) **in the rest pose**:
`_items_lib.rest_shield_mounts` -> `fit_shield_mount`, one implementation for both steps. The socket is a child
of the hand bone, the frame = the hand socket x the fitted rotation and offset (the matrix is in the extras
`socket_space_matrix` and `shield_mount_matrix` of the hand socket, in `stats.shield_mounts`). `attach_prop`
takes it unchanged; on the test character the world matrices of the shield from the rig socket and from the
socket that `attach_prop` builds itself with `fit: rest` match to 1e-4. It is cheap and stable on all checked
assets: fitting time 0.11-0.67 s (the slowest on a 120k-triangle mesh), offset 8-14 cm, alignment of the
canonical shield at rest (up.Z / face.outward) >= 0.99 on both sides.

If the fit failed, the rig stays valid without shield sockets (warning `shield mounts not built`). The real
teardrop shield of the kit on a test character at rest touches 2 triangles of the open hand (it is thicker than
the canonical one); in `attach_prop` this is the warning `hand_through_board_rest`.

**Projection for the marker editor.** `markers.json` carries a `projection` block (read by
`src/forge/forge-core.js` -> `markerProjection`): `image {width, height}` is the whole `markers.png`
(1280 x 640), `center [x, z]` is metres at the centre of the front tile, `meters_per_px`, `center_px [u, v]` is
this point in pixels of the PNG (origin at the top-left, u to the right, v down), `front_tile`, the formula. The
numbers come from the same bounds and `margin` 1.08 with which `af.render_views` frames the front tile. Check
(real-asset test): each of the 27 joints of a test character, projected by the
block, against the centre of its ball in control renders of the same scene (only a group of widely spaced
balls is visible, the same frame, the centre from alpha): **maximum 0.19 px, mean 0.16 px** (threshold 2 px).

## Parameters

**rig_humanoid**: `inputs.mesh` (glb/gltf/fbx/obj/stl/blend; for .blend `params.objects`) or `inputs.blend`.

| Parameter | Default | |
|---|---|---|
| `height_m` / `height_ref` | 1.8 / `head` | height to the crown or `bbox` |
| `front` | `auto` | `-Y` `+Y` `+X` `-X` |
| `fix_yaw` / `max_yaw_deg` | true / 5 | turn a tilted input |
| `markers` | - | `chin, groin, l/r_wrist, l/r_elbow, l/r_knee` as `[x, z]` or `[x, y, z]` (m, after normalization); `joints: {Bone: [x,y,z]}` |
| `rigid` | - | rules of the reference pre-Mixamo script (`axis`+`r` or box, `merge`) |
| `auto_rigid` | true | items and shields rigid on a bone |
| `robe` / `robe_feet` | `auto` / false | |
| `max_influences` / `proxy_tris` | 4 / 60000 | |
| `fingers` | false | 3+1 bones per finger, if the fingers are separable |
| `sockets` / `deform_test` / `intersections` | true | |
| `deform_gate` | `error` | `warning`: the pose check is only in the report (in a production batch the real clips in `motion_style` decide) |
| `shield_mounts` | true | `socket_mount_shield_l/r` fitted by the rest pose |
| `grip_dirs` | - | `{"l"\|"r": [x,y,z] \| up/down/forward/back/out/in}`: +Z of the hand socket by direction |
| `keep_input_sockets` / `socket_bones` / `replace_sockets` | true / - / - | input sockets (see "Input sockets") |
| `island_max_frac` | 0.01 | small islands outside the heat map, rigid on the nearest bone |
| `surface_aware` / `surface_margin_h` | true / 0.05 | arm weight cleanup by geodesic distances |
| `arm_bleed` | [0.10, 0.35] | suppression of residual arm weight on the torso; null = off |
| `min_confidence` | 0.35 | |
| `front_prior` | - | `-Y` for generations: an answer of "back" or an uncertain one yields to it |
| `use_placement_grips` | true | hand on the grip of an extracted item (sockets `socket_prop_*`) |
| `hard_surface` / `build` | auto / - | rigid plates by the metal paint |
| `arm_clamp_h` | - | an arm chain longer than this -> wrist at the typical length (pipeline: 0.47) |

Outputs: `rigged.glb`, `rigged.blend`, `markers.json` (the format of `params.markers` + all joints,
confidences, `props` with `bound_to`/`bind_mode`, `plates`, `arm_symmetry`, `yaw`, `gross_failures`, `fail`,
`projection`: pixels of the front tile of `markers.png`; written on rejection too), `markers.png`,
`poses.png` (rest + 4 poses, front and 3/4).

**prepare_mixamo**: `inputs.mesh`, `inputs.rigged`. `lite` (<= 12,000 triangles, 256 px), `tris`/`tex`, `weld`,
`center`, `prerigged`, `rig_params`, `force_prerigged` (false). The skinned FBX is written **only if the own
rig passed the checks**; otherwise the error `prerigged_rig_ok` and a warning (the plain FBX for upload is
always written).

**import_mixamo_rig**: `inputs.fbx` ("With Skin"). `height_m`, `texture_source`, `retex`, `keep_animation`,
`sockets`, **`clip`** (by default the file name) and **`loop`** (`auto`: idle/walk/run/loop/breath in the name).
The Mixamo action `Armature|mixamo.com|Layer0` is renamed to `<clip>` or `<clip>-loop` (contract §7).

## Running

```
"C:/Program Files/Blender Foundation/Blender 5.0/blender.exe" -b --factory-startup ^
  --python scripts/blender/af_run.py -- rig_humanoid request.json
```
```json
{"step": "rig_humanoid", "inputs": {"mesh": "C:/.../character.glb"},
 "params": {"height_m": 1.8}, "out_dir": "C:/.../out"}
```
Tests: `blender -b --factory-startup --python scripts/blender/tests/run_tests.py -- humanoid`. The suite contains
synthetic tests (mannequins) and tests on real assets that run only when the meshes are available on disk.
Earlier additions to the suite: the sockets-and-grip integration round (hand frame of the rig = `attach_prop`
frame, kit sword and shield on a rig without refit, `markers.json` projection against `markers.png`; an
end-to-end mannequin test checks 9 sockets and the stance of the shield sockets), `test_prop_chain_axis_decimation_fragments_vs_bow`
(a shaft with fragments at the grip is straight, a bow with its string and an arc are not), a decimation test
(`cleanup` to 40,000 and 20,000 triangles -> staff found -> LeftHand, only `deform_torn_area*` fail).

## Results (Blender 5.0.1, 2026-10-10)

"Torn" is the share of the **pose area** occupied by triangles that have grown more than 3 times (floor stubs
and degenerates are excluded). The earlier metric weighted by rest area and understated thin fused strips that
became a sheet by roughly 8-10 times. Thresholds (error): **> 1 %** in "arms forward / squat / stride",
**> 8 %** with arms up (2-5 % at the armpit is normal for linear skinning), robe in the stride **> 4 %** of its
own area. On clean meshes all three main poses are <= 0.4 %.

**The pose check is a proxy, not a verdict (2026-10-11, night).** The poses are abstract: arms straight up and
forward without an elbow bend, a deep squat, a long stride. Production clips have almost no such poses, but they
have what the poses lack: elbow bends, torso and head turns, deaths. Therefore in a production batch the step
runs with `deform_gate: warning`: the pose numbers go into the report but do not stop the run, and the decision
is made by the same measure on the character's real final clips (`motion_style`, `deform_qa`, the same
thresholds; see motion_style.md). This is not a relaxation: in the pilot the clips turned out stricter than the
poses for merged shields (one character: poses 0.56 / 1.68 / 0.73 / 0.15 %, idle 2.8 %, a shield at the
shoulder tears when the elbow bends).

Representative results on assets that can be described neutrally:

| Asset | Triangles | Input | Confidence | Time | Torn, % (forward / up / squat / stride) | Items | Result |
|---|---|---|---|---|---|---|---|
| Male base mesh (Human Base Mesh) | 21,160 | -Y | 0.82 | 6 s | 0 / 4.07 / 0 / 0 | 65 bones (fingers) | ok |
| Female base mesh (Human Base Mesh) | 21,160 | -Y | 0.82 | 6 s | 0 / 2.75 / 0 / 0 | - | ok |
| Clean generated humanoid, +X facing | ~17,000 | +X | 0.75 | 5 s | 0 / 0.14 / 0.01 / 0 | - | ok |
| Same, rotated by 30 degrees | ~17,000 | +X, yaw 29.9 degrees -> fixed | 0.75 | 5 s | 0 / 0.14 / 0.01 / 0 | - | ok, joints <= 2 cm from the original |
| Humanoid with spear and shield | ~17,000 | +X | 0.76 | 6 s | 0 / 0.37 / 0 / 0 | spear 2.07 m -> RightHand (0.0 degrees), shield -> LeftHand | ok |
| Humanoid with sword and shield | ~18,600 | -Y | 0.62 | 6 s | 0 / 0.10 / 0.03 / 0 | sword 0.99 m -> RightHand, grip at the hilt, +Z down (0.1 degrees); shield -> LeftHand | ok |
| Robed humanoid with staff, 120k triangles | 119,997 | -Y, robe | 0.55 | 14 s | **11.6 / 15.6** / 1.1 / 1.9; robe in the stride 3.0 | staff 1.88 m -> LeftHand (11,802 vert., 0.0 degrees) | **error**: sleeve fused with the robe |
| Same after `cleanup` 40,000, 1.8 m | 39,919 | -Y, robe | 0.55 | 7 s | **10.0 / 13.3**; arm symmetry 0.118 H (staff in the hand) | staff -> LeftHand (2,613 vert.) | **error**, same (before the 2026-10-10 fix: staff not found, rejection on landmarks) |
| Same after `cleanup` 20,000, 1.8 m | 19,959 | -Y, robe | 0.55 | 6 s | **10.2 / 14.4**; symmetry 0.112 H | staff -> LeftHand (1,261 vert.) | **error**, same |
| Robed humanoid, staff not in the hand | ~21,400 | +X, yaw -21.5 degrees -> fixed, robe | 0.62 | 7 s | 0.10 / 0.03 / **3.50 / 2.55**; robe 3.98 | staff not in the hand -> Hips | **error**: the robe box crumples in the squat |
| Winged humanoid | ~18,500 | -Y | 0.20 | 6 s | - | - | **rejection**: arms went into the wings (0.61 H) |
| Ghost | ~19,200 | +X | 0.15 | 4 s | - | - | **rejection**: no support |

**No regression after `_chain_axis` (2026-10-10, evening).** The same `rig_humanoid` with the old (HEAD) and the
new code on the test assets (clean humanoids, characters with weapons and shields, robed characters, the
120k-triangle mesh): `ok`, confidence, arm symmetry, torn area, rigid props (bone and vertex count) and the
angle of the grip socket match; joints moved by no more than **0.06 cm** (a hand tip). The ghost still gets a
rejection (`test_real_ghost_fails`).

**No regression after the weight cleanup (2026-10-10, night).** The earlier numbers (HEAD code) were compared
with the current code: small islands, proxy repair, geodesic cleanup, bleed suppression (only for hidden arms).
Torn, % (forward / up / squat / stride):

| asset | before | after |
|---|---|---|
| clean humanoid (1.7 m) | 0 / 0.14 / 0.01 / 0 | 0 / 0.14 / 0.01 / 0 |
| humanoid with spear and shield | 0 / 0.37 / 0 / 0 | 0 / 0.46 / 0.01 / 0 |
| humanoid with sword and shield | 0 / 0.10 / 0.03 / 0 | 0 / 0.10 / 0.03 / 0 |
| humanoid with sword (12 small islands) | 0 / 0.64 / 0 / 0 | 0 / 0.03 / 0 / 0 (the islands are now rigid) |
| male base mesh (fingers) | 0 / 4.07 / 0 / 0 | 0 / 3.78 / 0 / 0 |
| female base mesh | 0 / 2.75 / 0 / 0 | 0 / 2.32 / 0 / 0 |
| robed 120k humanoid, sleeve fused | 11.6 / 15.6 / 1.1 / 1.9 | 6.56 / 10.26 / 1.70 / 2.28 (still an error: weights do not cure it without a cut) |

In its first variant the bleed suppression also worked on visible arms. On one test character "arms up" then
grew from 0.14 to 0.96 %, so it is now enabled only for arms without a visible shoulder.

Rejections and errors are an honest result: the rig is either not built (markers for correction are written), or
built but the step returns `ok:false` with an indication of where it tears (`deform_*` with `torn_by_bone_m2`,
`torn_rest_centroid` in the statistics).

**Shoulders.** Against the joints of a reference rig (height 1.7 m, front view), a test character: **3.2 cm** on
each side (before the fix ~13.5 cm closer to the neck and 10 cm higher). Male base mesh against reference
joints: shoulder 1.2 cm, elbow 1.3, wrist 0.8, knee 1.3, ankle 3.8, hip 6.6, Hips 7.1 (our pelvis is lower: the
crotch of the mesh is by the meeting of the thighs). Upper arm : forearm: male base mesh 1.17/1.36, female base
mesh 0.99/0.85 (earlier 1.9-2.8 on generated characters). With arms up the armpit of a test character tears by
0.14 % (earlier 1.65 % by the old metric).

**A test character against hand-placed reference markers** (cm): wrists 0.4/0.9, elbows 2.2/1.8, knees 0.6/0.8.

**prepare_mixamo**: a test character lite + prerigged: 12,000 triangles, 563 KB, skinned 762 KB (25 bones, the
rig passed the checks). A robed character lite + prerigged: the skinned FBX is **not written**: the rig did not
pass `deform_torn_area`, error `prerigged_rig_ok`; the plain FBX (785 KB) exists. **import_mixamo_rig**: an
`idle.fbx` of an archer test character: 1.8 m, -Y, clip `idle-loop` (from `Armature|mixamo.com|Layer0`,
frames 1-154), original material from `model.glb`, 9 sockets (including shield sockets, the same
`add_human_sockets`), 2.3 s; a `walk.fbx` without skin gives a clear error "no skinned mesh... (download 'With
Skin')".

## Honest limitations

- The landmarks are an **analysis of silhouette and cross-sections, not anatomy**. Under a robe, a coat hem or
  a wide sleeve, the crotch, knees, elbows and shoulders are placed by proportion (confidence 0.3-0.4).
- T, A and "arms down" poses of a standing figure are supported; rotation about Z is fixed, forward/sideways
  tilts are not. Arms forward/up, seated poses, chibi: rejection or low confidence.
- A **straight item at an angle** (sword, mace, spear, banner shaft) is recognized if a fist sits on it and
  its far end goes further than the arm reaches (section "Swords ... at an angle"). Not taken: a dagger shorter
  than 0.25 H, a blade wider than 0.08 H, an item whose fist is merged with a cloak and lies on the torso core.
  The face of a model with a cloak billowing sideways or backwards may be detected wrongly; then use
  `params.front` (in a batch, `roles.json` -> `rig`). A tower shield standing on the floor counts as a leg.
- A **bow in the hand** is recognized if it stands almost vertically beside the body (section "Bows..."). A
  bow raised horizontally for a shot, a crossbow, a whip, wings, tails, cloaks are not: the rig is rejected for
  arm asymmetry or length. For such cases use `params.rigid` or `markers`. A bow limb fused with the foot or
  leg tears in the squat (weights do not cure it, a cut is needed).
- An item that is not held (a staff standing beside a character) is rigidly attached to Hips: it does not bend,
  but in a squat it goes into the floor with the pelvis.
- A two-handed grip is not distinguished; a sword longer than 0.75 H taken at the very end is considered a
  shaft (+Z up).
- Fused AI meshes tear where they are fused: a sleeve grown into a robe turns in "arms forward" into a sheet of
  0.72 m2 (11.6 % of the pose). Weights do not cure this, a cut before the rig is needed: `separate` with
  `kind: "slit"` (a slab with caps on the sides, see `core.md`), which is how the v2 of the robed test
  character passes the check. The closed "bottom" of a robe is visible in the squat. Robes are not simulated.
- The weights are bone heat + heuristics, linear blending, without corrective shapes and twist bones. The
  Blender solver fails on some assets: for a robed one only the voxel proxy worked, for others the 1/4 proxy
  left 5-6 % of vertices without weights (filled from neighbours); this is in the step warnings.
- In the test poses the hands are not turned separately but keep the rest angle to the forearm (otherwise a
  fist on a hilt would break by 90 degrees); the wrist is not checked by the tests.
- Fingers only for separable relaxed fingers (base meshes), bending about the local Z.
- `prepare_mixamo` does not upload to the site and does not check whether Mixamo accepts the file;
  `import_mixamo_rig` was tested on "with skin" clips and on a synthetic FBX in centimetres.
