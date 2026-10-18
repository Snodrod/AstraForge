# Review — viewing and deciding on characters (2026-10-11)

The **"Review"** tab in the "Forge" panel (next to "Wizard", "Jobs", "Batch"). It shows all
characters of a batch next to their concepts, lets you approve each one or send it back for rework,
and saves the decisions where scripts read them. Routes: `docs/forge/API.md`, "Review" section.

## How to open

1. The studio must be running (`Start.cmd` or `scripts/Start.ps1`), address `http://127.0.0.1:3210`.
2. Left column → **"Forge"** tab → the **"Review"** tab at the bottom of the section row.
3. While the tab is open, the left column widens (up to 46% of the window width) so the cards fit
   into a grid. Other sections and studio tabs return to the previous width.
4. If the tab says "The studio server does not know the review route yet", the studio was started
   before the update: restart it (`Stop.cmd`, then `Start.cmd`).

Where the list comes from: the batch state file `C:\AstraForge\incoming\build\build.state.json`
(written by the batch builder; the path is changed with the `ASTRA_FORGE_REVIEW_STATE` variable).
It supplies the **batch composition**, role, faction and concept. Everything else — status, previews,
mesh-tear numbers, the final model — is read from the asset passports
(`<library>/assets/<id>/asset.json`) on every list refresh, so re-running a step (rework) is visible
immediately even if the state file is stale. If there is no state file and no batch is named, all
humanoids and creatures of the library that were built from generations are shown. Concept notes
(`notes`) come from `roles.json` in the concepts folder (`ASTRA_FORGE_REVIEW_ROLES`).

## How to look

At the top: four counters — **approved / rework / reject / not reviewed** (they also work as
filters: click a counter, click again to clear the filter) and a "reviewed out of all" bar.
Below are the filters:

| Filter | What it shows |
|---|---|
| All | the whole batch, including skipped characters |
| Built | the pipeline ran to the end (including "Tearing") |
| Tearing | built, but the animation check found mesh tears |
| Failed | the build stopped at one of the steps |
| faction, role, search | by faction ("Order", "Undead"), role, and a substring of the name, unit or comment |

Build filters and decision filters (the counters) combine: for example "Failed" and "not reviewed".
The selected filters are remembered in the browser.

### Card

- On the left is the concept (cropped around where the character stands); on the right is a strip of
  previews: **paint** (`compare.png` of the `paint_from_concept` step: the concept and a view of the
  model side by side), **motion** (`preview.png` of the `motion_style` step) and **sprites**
  (`preview.png` of the `render_sprites` step). If the skeleton was not built, the motion and sprites
  are replaced by the detected joint points (`markers.png`) and a deformation test.
  An empty dashed frame means there is no image.
- Name, role, faction, version, a **colored state label** and an explanatory text.
- For characters with measured animations — a "Mesh tear: N%" line (worst clip, tolerance) and a table
  of all clips (expandable): the share of tearing surface, cloth or cloak, the number of frames over
  tolerance.
- **"Show in scene"** loads the final model (the last GLB of the chain, not the Godot package) into the
  viewer. If there is no skeleton, this is the model after "Paint from concept".
- **"Open large"** (or Enter) — a window: the concept and paint large, the motion below, the sprites on
  the right; arrows switch between characters, Esc closes; the same decision buttons are in the window.

### What the state label means

| Label | Status | What happened |
|---|---|---|
| Built | `built` | all steps passed, mesh tear within tolerance |
| Mesh tearing | `failed_qa` | built completely (sprites exist), but the animation measurement found mesh tears over tolerance; can be approved |
| Stopped: <step> | `failed_forge` | the first failed step and the reason (e.g. "Skeleton not built: the model does not look like a human…", "Sprites failed the checks…", "The step exceeded the time limit"). Cannot be approved |
| Incomplete | `incomplete` | no errors, but the pipeline did not reach sprites |
| Building | `running` | a job is running on the asset |
| Not built | `new` | the passport has no steps |
| Skipped | `skipped` | the batch builder did not take the character (role `skip`, for example a ghost) |
| No passport / No asset | `missing` | the passport was moved to the trash or was not created |

Skipped characters and those without a passport are shown, but no decisions can be set for them and
they are not counted in the counters.

## How to decide

Buttons on the card (and in the large window):

- **Approve** (green, key **A**) — unavailable for characters whose build stopped.
- **Rework** (amber, key **R**) — opens a "What is wrong?" field: ready-made reasons
  "cloth/cloak tears", "weapon not in hand", "does not match concept", "bad animation", "no skeleton"
  (click toggles each on and off) and a free-text line; `Ctrl+Enter` submits, `Esc` closes.
  Reasons are joined with ";". An empty comment is allowed.
- **Reject** (red) — the character is not usable at all.
- The "reset" icon returns the character to "not reviewed".
- **Game unit** — an optional field: which unit of your game the character goes to (for example
  `paladin`). It is pre-filled with a suggestion from the name (the words up to the first verb or
  preposition, up to three words, "#2" → `_2`: "Forest archer holding longbow #2" → `forest_archer_2`).
  Change or clear it before deciding; for an already decided character the edit is saved when you
  leave the field.

Keyboard (works while the tab is open and the cursor is not in an input field): **arrows** — choose a
card (up and down move by a row), **A** — approve the selected one, **R** — rework, **Enter** — open
large, **Esc** — close the window. After a decision the selection moves to the next card on its own.
While "Review" is open, the R and arrow keys belong to it rather than to the studio viewer (where R is
zoom).

## Where decisions go

1. **Into the asset passport — immediately, on every click.** `PATCH /api/forge/assets/:id`:

   ```json
   { "qa": { "reviewed": true, "decision": "approved", "comment": "", "game_unit": "paladin" } }
   ```

   In `asset.json` the `qa` block receives `reviewed`, `reviewedAt`, `decision`
   (`approved` | `rework` | `rejected`), `comment`, `game_unit` and `at` (the time is set by the server).
   Reset: `{"qa":{"reviewed":false,"decision":null}}` — the decision fields are removed. The other `qa`
   data (`checks`, `notes`) is not touched.
2. **Into a file — with the "Save decisions to file" button** (`POST /api/forge/review/export`).
   The file `C:\AstraForge\incoming\build\review_decisions.json` (next to the state file; the path is
   changed with the `ASTRA_FORGE_REVIEW_DECISIONS` variable) is a JSON list of decided characters only:

   ```json
   [
     {
       "assetId": "2e7b691e-dcb8-41fe-8d90-decada973c53",
       "name": "Acolyte holding staff and book",
       "decision": "rework",
       "comment": "cloth/cloak tears; weapon not in hand",
       "game_unit": "acolyte",
       "file": "Acolyte_holding_staff_and_book.jpg",
       "role": "caster", "faction": "order",
       "status": "failed_qa", "failedStep": null,
       "reason": "Built completely, but the mesh tears: …",
       "at": "2026-10-11T16:11:13.743Z"
     }
   ]
   ```

   The first five fields are required for the following steps (a rework agent, game integration); the
   rest are for reference. The file is written whole and atomically on every button click, so before a
   script reads it, click the button again if decisions were made since the last save.

A script that does not want the file can simply read the passports: `qa.decision === 'approved'` and
`qa.game_unit`. The batch list with statuses and file links is `GET /api/forge/review`.

## Configuration

| Variable | Default | What |
|---|---|---|
| `ASTRA_FORGE_REVIEW_STATE` | `C:\AstraForge\incoming\build\build.state.json` | batch state file |
| `ASTRA_FORGE_REVIEW_DECISIONS` | `review_decisions.json` next to the state file | where "Save decisions" writes |
| `ASTRA_FORGE_REVIEW_ROLES` | `roles.json` in the concepts folder | concept notes |

Another batch: `GET /api/forge/review?batch=pilot2` reads `<incoming folder>/pilot2/pilot2.state.json`
(the name is Latin letters, digits, `_`, `-`).

## Limitations

- The list is refreshed when the tab is opened and with the "refresh" button; it does not poll the
  server by itself. If a rework is running in parallel, click "refresh" after it ends.
- Preview size is the original (500–600 KB per image, files are served without cache); lazy loading
  fetches only the visible cards.
- The state file must be readable: if the batch builder is rewriting it at that moment, the list
  answers "The batch state file cannot be read" — retry in a second.
- The "Approve" decision does not export anything into a game by itself: a separate step does that,
  working from `review_decisions.json` or from the passports.
- Browser verification: `tests/forge-review.test.mjs` covers the server and the pure view functions;
  the interface itself was checked by hand in a live studio (cards, filters, keys, the large window,
  the "decision → passport → file → reset" loop).
