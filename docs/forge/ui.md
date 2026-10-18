# Forge — the wizard in the studio interface (the "ui" module)

The **"Forge"** tab in the studio's left panel (second after "Create"). The wizard leads an asset
through the steps of the Blender pipeline from `docs/FORGE-CONTRACT.md` and works through
`docs/forge/API.md` (and the extensions from `docs/forge/server.md` listed below). While the forge is
open, the left column widens to 400 px (350 px on narrow screens); other tabs keep their width.

## Files

| Path | What |
|---|---|
| `src/forge/ForgeTab.jsx` | the tab entry for `App.jsx`: the icon and `React.lazy` — the wizard itself loads as a separate chunk on first open |
| `src/forge/forge-tabs.css` | loaded with the studio: the tab-block grid (the ninth tab) and the "Opening the forge…" placeholder |
| `src/forge/ForgePanel.jsx` | the wizard root: server check, step and preset registry, passport, job polling, step strip, the "Wizard / Jobs / Batch" sections |
| `src/forge/Step*.jsx` | wizard steps: `StepWhat`, `StepPrepare` (cleanup or generator + "6 more variants"), `StepSkeleton`, `StepMotion`, `StepItems`, `StepFx`, `StepExport` |
| `src/forge/MarkerEditor.jsx` | draggable joint points over `markers.png` |
| `src/forge/StickFigure.jsx` | stick-figure clip preview on a canvas |
| `src/forge/ForgeJobs.jsx`, `ForgeBatch.jsx` | jobs and batches |
| `src/forge/ForgeUi.jsx` | shared elements: context, a parameter field from the registry, a numeric field, the "to trash" button, checks, result previews |
| `src/forge/forge-api.js` | **all** `/api/forge/*` paths in one place; header `x-astra-client: studio` |
| `src/forge/forge-core.js` | pure functions (tests: `tests/forge-ui-core.test.mjs`) |
| `src/forge/forge.css` | wizard styles on the tokens of `style.css` (loaded together with the lazy chunk) |

`src/App.jsx` has three lines: the import of `ForgePanel,{ForgeIcon}` from `./forge/ForgeTab.jsx`, the
tab `['forge','Forge',ForgeIcon]` and `<ForgePanel studio={studio} snapshot={snapshot}
notify={notify} onEdit={()=>setDirty(true)}/>`. The tab block is five columns (two rows of 5
and 4) so the ninth tab does not wrap onto a third row.

## Steps

| Type | Steps |
|---|---|
| Human | What we create → Prepare → Skeleton → Motion → Items → Effects → Export |
| Creature | What we create → Prepare → Skeleton → Motion → Effects → Export |
| Item, ship, turret | What we create → Prepare/Generator → Effects → Export |

1. **What we create.** Type cards (turret — `kind: prop`) and the source:
   - a model from the scene — the selected object is exported to GLB **in its own units**:
     a copy (SkeletonUtils.clone) is exported without the studio's display wrapper (the 3/max scale and
     the offset from `StudioScene.importAsset`); rotation and scale the user added on top of the import
     are preserved. The GLB goes as multipart straight to `POST /api/forge/assets`;
   - a generation result (`GET /api/jobs` → `source:{type:'job'}`);
   - a file: GLB, FBX and OBJ — multipart straight to `POST /api/forge/assets` (up to 300 MB, without a
     copy in `data/assets`); the forge does not accept STL, which goes through the studio's
     `POST /api/assets` (100 MB) and `source:{type:'asset'}`;
   - parameters (generators).

   At the bottom is "Continue working" over the list of passports; each row has a "to trash" button
   (`DELETE /assets/:id`, double click: the first arms "Sure?", the second confirms).
2. **Prepare.** `cleanup`: a logarithmic budget slider (defaults 40k human, 30k creature, 8k item,
   40k ship; the "Simplify" checkbox can be cleared), height, splitting into parts (`separate`); the
   remaining parameters are under "All step parameters". The form opens with the values of the previous
   cleanup run; the default height is the passport's `profile.height_m` (1.8 m for a human). After the
   run, the chosen height of a human or creature is written to `profile.height_m` (`PATCH`). For ships
   and items made from a model, `ship_from_mesh` / `item_from_mesh` follows. For generators — the
   `ship_gen` / `turret_gen` / `item_kit` form, opened with the parameters of the **last generator run**
   (otherwise `source.params`), and "6 more variants" (a batch of 6 new passports with neighboring seeds,
   a preview grid, "Choose"; extra variants are removed one by one with "to trash" or all at once with
   "Remove unchosen").
3. **Skeleton.** Human: `rig_humanoid`. The "Height, m" field is filled from the "Prepare" step (the
   cleanup height if cleanup ran after the previous rig, otherwise the previous rig's height, otherwise
   `profile.height_m`): the autorig scales the body to `height_m` itself; without it a 1.2 m dwarf would
   again become 1.8 m. The other parameters of the previous rig (fingers, robe…) are preserved; markers
   are sent only by the point editor. The front view from `markers.png` with points for the chin, groin,
   wrists, elbows and knees (the side view is a button away). Points are dragged with the mouse (and
   arrows, Shift — 10 px steps); symmetry turns on by itself only if the detected pose is symmetric.
   "Rebuild from points" runs `rig_humanoid` with `params.markers = {name: [x, z]}` in meters **and**
   `height_m`: `markers.json` is written at the scale of its own `height_m`, so for a different height
   the form recomputes the points to it. Below are `poses.png`, the numbers and the checks. An FBX with a
   Mixamo rig — the `import_mixamo_rig` button; the "Rig manually on mixamo.com" block prepares the FBX
   with the `prepare_mixamo` step. Creature: the template
   `auto/quadruped/winged_quadruped/bird/hexapod/octopod/serpent/custom` and `rig_creature`.
4. **Motion.** Human: the `GET /clips` library — categories, search (including by the Russian category
   names), selection with "loop" and "in place" checkboxes, a pose preview (`GET /clips/:id/poses`), FBX
   upload by drag and drop (`POST /clips`, field `files[]`), "Apply" = `PATCH clips` + `retarget` with
   `params.clips=[{source:'library:<id>', name, loop, in_place?}]`. **"In place" is on by default only for
   locomotion clips** (`isLocomotionClip`: the `locomotion` flag of the library entry, otherwise the
   `locomotion` tag, otherwise the walk/run category — the same rule as in the `retarget` step, contract
   §7): `retargetClips` sends them `in_place: true` (a cleared checkbox — `false`); standing clips (idle,
   attack, death) get no key, and the step's `root_motion` decides — the hip travel is preserved, the
   feet do not slide. A checkbox set by hand on a standing clip is sent as `true`. Previously
   `in_place: true` went to every clip (the smoke run of 2026-10-10). Passports saved by the old wizard
   carry `in_place: true` on all clips — on standing clips the "in place" checkbox is on and must be
   cleared once. A hint on how to download clips from mixamo.com (FBX Binary, Without Skin, 30 fps).
   Creature: checkboxes of procedural clips from the registry (`idle-loop … death`); flight and gliding
   are only for winged templates; `anim_creature`.
5. **Items.** The `item_kit` type gallery (style, seed, ornament) creates a separate item passport;
   "Put in hand" = `PATCH props` + `attach_prop` with `hand` and `prop_asset`.
6. **Effects.** Presets on sockets (`PATCH fx`). The preset list is for now defined in the client
   (`FX_PRESETS` in `forge-core.js`): engine_trail, thruster_glow, aura, magic_cast, fire, smoke,
   sparks, ice, poison, heal, muzzle_flash, nav_light. Sockets come from the step results
   (`stats.sockets`) plus the contract §6 list.
7. **Export.** The Godot / iso8 sprites profiles
   (`POST /assets/:id/export`; for Godot the passport's effects go in `params.fx`).
   The server exports the last completed version, so above the button there is a "What goes into the
   package" block: the number of that version and its steps. If a human or creature has no skeleton or
   motion in it while an older version had them (after a return to an early step the late steps are
   reset), a warning with a link to the needed step is shown. Below are the result folder, ZIP download
   from `asset.exports`, the result checks and the Godot JSON check as green and red rows.

Any result with a GLB can be opened with the **"Show in scene"** button: the file is taken from
`GET /api/forge/assets/:id/files/…` and loaded through `StudioScene.importAsset`.

## Versions, `from` and step repeats

`from: 'vNNN'` on the server is the **final** state of a version. Presets and batches put several
wizard steps into one version (`cleanup → rig_humanoid`; `humanoid_from_mesh` also has `retarget` and
`godot_package`), and a version can fail after the first steps. So the wizard plans the run itself
(`runPlan` / `planSteps` in `forge-core.js`):

- "Prepare" is always `from: 'source'`;
- for later steps the last successful step of the nearest earlier wizard group is taken (for "Skeleton"
  — prepare, for "Motion" — skeleton, for "Items" — motion or skeleton);
- if its version is complete and has no other model-changing steps after it (export does not count),
  `from` = that version;
- otherwise the version's own steps up to it are repeated at the start of the new run with the same
  parameters, from the base of that version (`versions[].base` or the source). Example: a batch asset
  with `[cleanup, rig_humanoid]` in `v001` → "Rebuild from points" sends
  `steps: [cleanup(previous params), rig_humanoid(markers, height_m)]`, `from: 'source'`;
  the notification appends "(repeating first: Cleanup and simplification)";
- nothing was done earlier — `from: 'source'`.

This way a repeated autorig does not land on the old rig, retargeting does not land on old clips, and
after a failed batch rig the skeleton is built on the cleaned and scaled model rather than on the raw
source. Returning to an early step drops the late steps of the current recipe — this is expected, and
the "What goes into the package" block on export shows it. A repeat could be made cheaper if the server
starts to understand a step-level base (for example `from: 'v001/01_cleanup'`).

`from: 'source'` and multipart forms (`POST /assets` with a file, `POST /batch` with `files[]` and
`source:{type:'upload', fileIndex}`) are described in `docs/forge/server.md`; they should be added to
`API.md`.

## Numeric fields

A number is not clamped to `min`/`max` on every keystroke (previously "1", "5" in the "Length, m" field
with a minimum of 2 produced 25): while the field is focused, the value is passed as typed, and it is
rounded and clamped when the field loses focus (or on Enter). A comma is accepted as the decimal
separator. An empty field of a "empty — by class" parameter is sent as an explicit `null`: with
`from: 'source'` the server mixes `source.params` under the step parameters, and without `null` the old
value would silently come back (the server itself drops `null` before Blender).

## Jobs and batches

Polling (`GET /jobs/:id` once a second) runs only for running jobs and jobs started from this window;
queued jobs coming from `GET /jobs` (for example after a page reload in the middle of a 200-asset
batch) are refreshed by a single `GET /jobs` every 3 seconds. Job records are merged by step: the items
of the `GET /jobs` list carry only `{step, status, progress}`, so already loaded step results, the log
tail and errors do not disappear; a job that finished "via the list" is fetched in full once. The "Jobs"
section additionally refreshes the list every 5 seconds. A job card: steps with progress, the log tail,
the error, cancel.

Errors: the server's messages ("The GPU has been busy with another process for more than 3600 s.", "A
job is running on the asset…", "The asset files are locked by another program…") are shown as they are;
English Blender/Python/Node text is translated by exact patterns (`humanizeError`), and the
`checks failed:` marker becomes "Quality checks failed: …".

"Batch": seeds from…to for generators, files or a whole folder of GLBs (multipart straight to
`POST /batch`, in parts of 20 files, each part its own card), checked generation results; a preset from
`GET /presets` or the base steps by type. Batches are tracked through `GET /batch/:id` every 2 seconds;
the batch list is remembered in the browser (localStorage).

## Step parameters

Forms are built from `GET /steps`. If a step is in the registry, only the parameters it declares are
sent, and only those the user changed (defaults stay with the module), plus the contract parameters
(`markers`, `clips`, `hand`, `prop_asset`, `height_m` when rebuilding from points). For generators the
default values are passed explicitly so the recipe rebuilds the same asset. While the registry is
unavailable, the fallback descriptions from `FALLBACK_PARAMS` are shown.

## Without the forge server

If `/api/forge` does not answer with JSON (the module is not connected — Express serves `index.html` or
an HTML 404), the wizard shows a calm message "The "Forge" module is not connected on the server yet…",
rechecks the server by itself every 20 seconds, and the run buttons are disabled.

## Verification

- `node --test tests/*.test.mjs` — 29 tests in `tests/forge-ui-core.test.mjs` (in the clip library test —
  the `in_place` rule for locomotion only, an explicit choice, `'strict'`, `isLocomotionClip`): point
  projection (a projection block on a two-tile sheet, only the front tile, a downscaled image; the
  fallback projection by dimensions and joints; fitting by the joint markers), the run plan (a
  single-step version, a `[cleanup, rig_humanoid]` version, a four-step preset, a failed batch version,
  the base of a failed version), rig height and point recomputation, numeric fields, the server's
  error messages, job merging and the polling plan, the generator form, the export plan, file batches,
  as well as the clip filter, presets, file paths, Godot checks, clip_library poses and the budget.
- `npm run build` — the build passes. The main chunk is about 1.33 MB (as before the forge; the size
  warning existed earlier), the wizard is a separate chunk of about 148 KB + 19 KB CSS.
- A live run of the first round (2026-10-10, port 3299, Blender 5.0.1, Godot 4.5.1): a ship, variants, an
  effect, a Godot export with the Godot check, a human from a generation with `cleanup` and
  `rig_humanoid`, point dragging. The test passports were moved to `data/library/trash/`.

## Limitations

- The `markers.json` of the `rig_humanoid` step contains a `projection` block (`image`, `center`,
  `meters_per_px`, `center_px`, `front_tile`); the wizard takes the projection from it (checked: 0 px
  mismatch on the test character). The estimate from `joints` + `height_m` and the fit by joint markers
  remain as a fallback for old files.
- The FX preset list is hardcoded in the client until a server-side catalog appears.
- Export always goes from the last completed version (that is how `POST /export` works); the wizard
  shows which version it is but cannot choose another.
- Repeating early steps (see "Versions") costs Blender time: when rebuilding the skeleton of a batch
  asset, cleanup is run again.
- The "before/after" preview shows the images the step returned: `cleanup` writes a single preview.
