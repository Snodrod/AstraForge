---
name: astraforge-vfx
description: Visual effects in AstraForge — particle effects on a model (fire, smoke, sparks, aura, engine trail, magic, shockwave), VFX textures and flipbooks generated inside the studio, texture generators from Simon Schreibt's "Textures for VFX" database (Material Maker, PixaFlux, KumoWorks, Slate and others), and export of effects to Godot 4.5. Use when asked to make, add, tweak or export an effect for a model, unit or ship; to generate an effect texture, noise or flipbook; to download or add texture generators (including from the Notion database); or to improve the AstraForge "VFX" or "Effects" tabs. Triggers - «эффект», «VFX», «частицы», «партиклы», «текстура для эффекта», «флипбук», «шлейф», «аура», «вкладка Эффекты», «генераторы текстур», «Textures for VFX», «Simon Schreibt», effect, particles, flipbook, trail, aura.
---

# AstraForge: VFX textures and effects on a model

AstraForge is a local 3D studio (React + three.js + Express :3210). It has two tabs for effects:
**"Effects"** — particle emitters on a model with live preview, a texture generator and export to Godot;
**"VFX"** — a catalog of 31 external texture generators with a launcher. The effect format is shared with the Forge:
`astraforge-fx/1`. History and decisions are in `git log` and `docs/vfx-tools.md`; private per-machine notes live behind the links in
`%USERPROFILE%\.claude\astraforge-local\README.md`, if such a folder exists.

## Where things live

| What | Where |
|---|---|
| Studio, git | the clone folder (APP below, environment variable `ASTRA_APP`) → GitHub `Snodrod/AstraForge`, branch `master` |
| Start / stop | `APP\Start.cmd` / `Stop.cmd` → http://127.0.0.1:3210 |
| Human-readable description | `APP\docs\vfx-tools.md` |
| External generators | `<APP>\runtime\vfx` or the path in `ASTRA_VFX_TOOLS` (`vfxTools` in `config\paths.json`): `apps\`, `downloads\` (installers), `web\`, `textures\` (ready PNGs, visible in both tabs), `_catalog\` (database export) |
| Generator catalog | `APP\server\vfx-tools-catalog.json` |
| "VFX" tab | `server\vfx-tools.mjs` (API `/api/vfx-tools`, web generators on a separate port 3211), `src\VfxPanel.jsx` |
| Effects: core | `src\fx-core.js` — presets, normalization, analytic particles, built-in textures, `.tscn`, ZIP (`buildFxPackage`), `fxAutoPlacement` |
| Effects: rendering and scene | `src\fx-runtime.js` (patch of the `StudioScene` prototype: `addFx`, `updateFx`, `fxList`, `triggerFx`, `fxAutoPlace`), `src\FxPanel.jsx` |
| Texture generator | `src\fx-textures.js` (+ `fx-textures.worker.js`), `src\FxTextureLab.jsx` |
| Presets (source) | `APP\scripts\blender\data\fx_presets.json`; browser copy `src\fx-data.js` |
| Godot reference | `APP\scripts\blender\steps\_export_lib.py`: `make_fx_texture`, `fx_scene_text`; mounting — `scripts\blender\godot\af_fx_mount.gd` |
| Generator installer | `APP\scripts\vfx\Install-VfxTools.ps1` (`-DryRun`, `-Only a,b`); in the setup wizard it is item `vfx` |
| Scripts of this skill | `scripts\notion_db_export.py`, `scripts\check_server.mjs`, `scripts\godot-check\` |

## Scenario 1. Make an effect for a model (working in the studio)

1. The owner starts the studio (`Start.cmd`); do not start `server/index.mjs` yourself without need — it brings up ComfyUI on the GPU.
2. Select a part or bone → "Effects" tab → a preset (12 Forge presets + "Empty"). The effect is placed under the part and
   scales itself to the model; engine effects (`engine_trail`, `thruster_glow`) go on the rear face of the part, exhaust pointing backwards.
3. Edit parameters (emission, shape, motion, appearance, texture, flipbook, light), move the gizmo marker with W/E/R.
4. Texture: built-in (soft, spark, star, smoke, ring), "From the VFX folder", "From disk" or "Create a texture in the generator"
   (10 types, 20 recipes, flipbooks 2×2/4×4/8×8) → "Use in effect".
5. Save the project; "ZIP for Godot 4.5" → `fx/*.tscn`, `fx/textures/`, `fx_bindings.json`, `af_fx_mount.gd`, README.

When choosing parameters for the user, stick to the `_fields` of `fx_presets.json`: only they are carried into Godot.

## Scenario 2. Add or update generators from the Notion database

1. Export the database: `py -3 -I scripts/notion_db_export.py <notion.site page url> <folder> --filter Type=generator`
   (no authentication, via `loadCachedPageChunkV2` → `queryCollection`; the spaceId is taken from the page block). Simon Schreibt's database:
   `https://simonschreibt.notion.site/Textures-for-VFX-Database-2c72eccccfa84a0eae927d778ad746cc`, 264 records, 31 generators (as of 2026-10-16).
2. For each link determine the kind: `desktop` (portable exe), `installer`, `web-local` (an open-source site, hosted locally),
   `file`, `web-online`, `store` (free, but via an order with an e-mail), `paid`, `guide`. Check the license (GitHub API `license.spdx_id`).
3. **Before downloading**, show the user the list: file, source, size — and wait for a "yes". Download only from official sites.
   Do not run installers: the user runs them with the "Install" button. Orders that need an e-mail (Gumroad $0, stores) are done by the user.
4. Add an entry to `vfx-tools-catalog.json` (fields: `id, name, author, kind, license, url, tags, ru, local, download`) and check with
   `Install-VfxTools.ps1 -DryRun`, then `-Only <id>` on a temporary folder (`-Root`).
5. itch.io "name your own price": in the browser "Download Now" → "No thanks, just take me to the downloads", then from the page
   `POST /<game>/file/<upload_id>?source=game_download&after_download_lightbox=1&as_props=1` (body `csrf_token`) — the response contains a `url`
   that lives 60 s, `curl` it at once. Put the zip into `downloads` and run `Install-VfxTools.ps1 -Only <id>`.

## Scenario 3. Improve the tabs' code

- First read the files you need from the table; code style is dense one-line JSX/JS, with UI strings in the i18n dictionaries.
- Change presets in `scripts/blender/data/fx_presets.json`, then sync the copy: `FX_SYNC=1 node --test tests/fx-core.test.mjs`
  (Vite does not serve `scripts/**` to the browser — hence the copy).
- The `.tscn` must stay byte-for-byte equal to `fx_scene_text` from `_export_lib.py` — the test checks it; change one, change both.
- Check without ComfyUI: `npm run build`, then `node scripts/check_server.mjs` from the skill folder (`ASTRA_APP` — studio path,
  `CHECK_PORT` — port, default 3299; PID in `%TEMP%\astraforge-check.pid`) → open http://127.0.0.1:3299 in the built-in browser.
  It serves the real `/api/vfx-tools` and `/api/assets`: the "Run"/"Install" buttons really launch programs — do not press them unless asked.
- Godot check: `node scripts/godot-check/gen.mjs <folder>` → `godot --headless --path <folder> --import` →
  `godot --headless --path <folder> --script check.gd` → a line `FXCHECK {...}` with `"errors":[]`, exit code 0.
  Godot: `ASTRA_GODOT` or the `godot` key in `APP\config\paths.json` (4.5.1 only, console build).
- Tests: `npm test` in APP. Hand large changes to subagents with a strict split of files.

## Rules and pitfalls

- **Web generator isolation.** Third-party pages are served only by the server at 127.0.0.1:3211 (`ASTRA_VFX_WEB_PORT`), never from the
  studio origin: otherwise their JS would get the studio API, including launching programs.
- **Metric presets.** Without `fxAutoPlacement` an effect on a studio model is almost invisible; emitter scale = particle size.
- **Emitter** is an `Object3D` in `content` with plain JSON in `userData.fx`; particles, marker and light live in an overlay outside `content`
  (they do not end up in the GLB or `toJSON`).
- **Licenses.** External programs are not part of the build. Neon Flames without a license — personal use only.
  Fateloom and the Flipped Normals links were dead as of 2026-10-16.
- **PowerShell scripts** of the studio are UTF-8 with BOM; take curl and tar from System32; in Git Bash use `py` instead of `python`.
- **Git.** If the folder was created by another Windows user (a sandbox), add it to the global `safe.directory`. Work via `cd`, without `git -c ...`/`git -C ...`. Commit after a green `npm test`; push to `origin master`
  only with the owner's consent.
- After the work, append the changes to `docs/vfx-tools.md` (or to the owner's notes, if `astraforge-local` points at them).
