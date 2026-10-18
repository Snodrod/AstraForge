# Forge API (AstraForge server, 2026-10-10)

Complements `docs/FORGE-CONTRACT.md`. All routes live under `/api/forge`. Mutating requests go through the
existing `server/index.mjs` protection: loopback Host/Origin and the `x-astra-client: studio` header.
Responses are JSON; errors are `{error: "message text", code}` with HTTP 4xx/5xx.

## State and reference data

| Method | Path | Response |
|---|---|---|
| GET | `/status` | `{blender:{path,version,ok}, godot:{path,version,ok}, library:{clips}, queue:{running,queued}, gpuLock:{name,held}}` |
| GET | `/steps` | step registry: `[{step, title, kinds, inputs, params:{<name>:{type,default,min,max,enum,title,help}}, gpu: true|false|'conditional', timeoutSec}]` (`'conditional'` applies to `cleanup`: the GPU is needed only when baking on the GPU) |
| GET | `/presets` | ready-made pipelines: `[{id, kind, title, steps:[{step, params}]}]` |

## Assets (passports)

| Method | Path | Body / response |
|---|---|---|
| GET | `/assets` | short list of passports `[{id,name,kind,updated,thumbnail,lastStatus}]` |
| POST | `/assets` | JSON `{name, kind, source:{type:'job'|'asset'|'upload'|'params', jobId?, assetId?, params?}, profile?}` or multipart: `file` (GLB/FBX/OBJ by signature, up to 300 MB) + fields `name`, `kind`, `source` (JSON), `profile` (JSON) -> passport |
| GET | `/assets/:id` | the complete `asset.json` passport |
| PATCH | `/assets/:id` | `{name?, profile?, clips?, props?, fx?, qa?}`; `qa.reviewed` changes only through an explicit field. Review decision: `qa:{reviewed:true, decision:'approved'|'rework'|'rejected', comment?, game_unit?}` (the server sets `qa.at` itself; without `reviewed` the flag turns on by itself); `qa:{reviewed:false, decision:null}` resets the decision; `comment` and `game_unit` separately change an already stored decision. See `docs/forge/REVIEW.md` |
| DELETE | `/assets/:id` | moved to `<library>/trash/` (not deleted) |
| POST | `/assets/:id/run` | `{steps:[{step, params}]}` or `{preset}`; `from`: `'source'` (default) | `'latest'` | `'v003'` (result of a completed version) -> `{jobId, version, warnings}` |
| POST | `/assets/:id/export` | `{profile:'godot'|'iso8'|'custom', params}` -> `{jobId}` |
| GET | `/assets/:id/files/<version>/<stepDir>/<file>` | a result file (extension whitelist, `..` protection) |

## Jobs

| Method | Path | Response |
|---|---|---|
| GET | `/jobs` | recent jobs |
| GET | `/jobs/:jobId` | `{id, assetId, version, status:'queued'|'running'|'completed'|'failed'|'cancelled', steps:[{step,status,progress,startedAt,finishedAt,result,logTail}], createdAt}` |
| POST | `/jobs/:jobId/cancel` | cancel; the Blender process is killed together with its children |

Progress: poll `GET /jobs/:jobId` once per second (WebSocket is optional, if a shared channel already exists).

## Clip library

| Method | Path | Body / response |
|---|---|---|
| GET | `/clips` | `<library>/animations/index.json` |
| POST | `/clips` | multipart `files[]` (.fbx, signature check "Kaydara FBX Binary" or ASCII FBX) -> import job |
| GET | `/clips/:clipId/poses` | `<id>.poses.json` for the stick-figure preview |
| DELETE | `/clips/:clipId` | remove from the library |

## Batches

| Method | Path | Body / response |
|---|---|---|
| POST | `/batch` | JSON `{items:[{name, kind, source, preset?|steps?}]}` or multipart: `items` (JSON) + `files[]`, where an item has `source:{type:'upload', fileIndex}` -> `{batchId, items:[{assetId, jobId}]}` |
| GET | `/batch/:batchId` | per-item summary |

## Review

The "Review" tab in the forge panel reads and writes only these routes and `PATCH /assets/:id`. Details are in `docs/forge/REVIEW.md`.

| Method | Path | Body / response |
|---|---|---|
| GET | `/review?batch=<name>` | `{batch:{name, source:'state'|'library', stateFile, stateUpdated, decisionsFile, generatedAt}, counts:{total, reviewable, built, failed_qa, failed_forge, skipped, other, approved, rework, rejected, notReviewed}, items:[...]}`. A row is `{assetId, name, kind, file, role, faction, handWeapon, notes, status:'built'|'failed_qa'|'failed_forge'|'incomplete'|'running'|'new'|'skipped'|'missing', label, tone:'good'|'warn'|'bad'|'busy'|'muted', reason, failedStep, failedStepTitle, error, tear:{gate, ok, failedClips, worst:{clip, tornPct, robePct, limitPct}, worstRobe, clips:[{clip, tornPct, robePct, limitPct, ok, frame, overFrames, frames, bones}]}|null, urls:{concept, compare, motion, sprites, markers, poses, glb}, glbStep, version, decision:{decision, comment, game_unit, at}|null, reviewable, canApprove}`. Without `batch` the default batch is used |
| POST | `/review/export` | body `{batch?}`; writes `review_decisions.json` next to the batch state file -> `{ok, path, count, batch, counts}` |

The batch state file is `ASTRA_FORGE_REVIEW_STATE` (default `C:\AstraForge\incoming\build\build.state.json`); another batch `?batch=pilot2` is taken from `<incoming folder>/pilot2/pilot2.state.json`. The decisions file is `ASTRA_FORGE_REVIEW_DECISIONS` (by default next to the state file). Concept notes are `ASTRA_FORGE_REVIEW_ROLES` (by default `roles.json` in the concepts folder). If there is no state file and no batch is named, all humanoids and creatures of the library built from generations (`source.type:'job'`) are used.

## Views (3D from multiple views)

Not under `/api/forge` but under `/api/views` (the studio server, `server/views.mjs`); the same protection applies: loopback and the `x-astra-client: studio` header
for mutating requests. Details are in `docs/forge/views.md`. Slots: `front`, `left`, `back`, `right` (+ `frontLeft`, `frontRight`, which do not affect the 3D result).

| Method | Path | Body / response |
|---|---|---|
| GET | `/api/views/status` | `{codex:{available,message,version}, mvadapter:{ready,reasons[]}, slots, shapeSlots}` (cached for 20 s) |
| POST | `/api/views` | `{imageId}` (a resource from `/api/assets`) -> 201 set; the same concept returns the same set |
| GET | `/api/views/:id` | `{id, frontAssetId, slots:{<slot>:{filled, source:'input'|'manual'|'codex'|'mvadapter', seconds, thumb, full, ...}}, normalized:{at,stale}, task:{status:'running'|'completed'|'failed'|'cancelled', provider, slots, message}, extraViews, canBuild, lastRun}` |
| GET | `/api/views/:id/image/:slot?kind=thumb|norm|raw` | image (`norm` is the 1536² shape-generator input, `raw` is as received) |
| PUT | `/api/views/:id/:slot` | multipart `file` (PNG/JPG/WEBP, up to 40 MB) -> set; uploading your own view while generation is running returns 409 |
| DELETE | `/api/views/:id/:slot` | remove a view (the front view cannot be removed) |
| POST | `/api/views/:id/generate` | `{provider:'codex'|'mvadapter', slots?:['left','back','right'], overwrite?:false, seed?, size?:512..768, steps?:8..60, note?}` -> 202 set with `task`; only one generation runs at a time (otherwise 409); only empty slots are filled unless `overwrite` is set |
| POST | `/api/views/:id/cancel` | cancel generation |

"3D from views" is an ordinary generation job: `POST /api/jobs` with `{imageId, viewSetId, quality, seed, meshLevel, ...}`. The job waits for the GPU lock,
takes the normalized views of the set, uploads them to ComfyUI and builds the `hunyuanMultiViewWorkflow` graph (Hunyuan3D-2mv); the job record carries `viewSetId`, `viewSlots`.

### 3D engine: Pixal3D (`engine`)

`POST /api/jobs` accepts `engine`: `"hunyuan"` (default, API behavior unchanged), `"pixal3d"` or `"auto"` (Pixal3D if installed, otherwise Hunyuan; the job
record always holds a concrete value). With `pixal3d` the fields `quality`, `shapeModel`, `meshLevel` are not used; if it is not installed the response is 400 with the reason.
It works both with `viewSetId` (up to four views, `pixal3dWorkflow` graph) and with a single image. The job record holds `engine`, `engineUrl`, `modelCheckpoint`,
`pixal3d:{seconds, slots, targetFaces, vramPeakMb, vramBaselineMb, scale}`. `POST /api/jobs/:id/reextract` returns an error for such jobs (Pixal3D has no "surface level").
`GET /api/status` contains `pixal3d:{installed, running, url, dir, missing[], message, estimate}`; `POST /api/runtime/pixal3d/start` starts the engine ahead of time (otherwise it starts by itself
on the first generation). Details: `docs/forge/pixal3d.md`.

## Error codes

`not_found`, `bad_id`, `bad_path`, `busy`, `validation`, `no_local_paths` (a local path cannot be passed in
parameters over HTTP; files are uploaded instead), `fx_socket_missing` (the effect mask found no sockets on the
model), `bad_glb`, `bad_json` (malformed request body; too large returns 413), `bad_state` (the batch state file cannot be read, 503). The error text is always in English.
Implementation details: `docs/forge/server.md`.

## Versions and input resolution

The library `<library>` is `C:\AstraForge\library` (or `ASTRA_FORGE_LIBRARY`). Each pipeline run creates a new version `<library>/assets/<id>/v00N/`; step `k` writes to
`v00N/<kk>_<step>/` (`out_dir`). A step's input is taken from the previous outputs: `outputs.blend` ->
`inputs.blend`, otherwise `outputs.glb` -> `inputs.mesh`. For the first step it is the passport source
(the generation's GLB, an uploaded file) or nothing (the `item_kit`, `ship_gen`, `turret_gen` generators).
Export (`godot_package`, `render_sprites`) does not alter the chain: its outputs are written separately, and the next model operation receives the previous `.blend`.
Special inputs: `attach_prop` takes `inputs.character` = the current result, `inputs.item` = the latest GLB
of the asset from `props[].asset`; `render_sprites`, `godot_package` take the current `.blend`/`.glb`.

## Command line

`node scripts/forge.mjs <command>`: `status`, `assets`, `create --kind ship --name X [--from-glb path] [--preset id]`,
`run <assetId> --steps file.json | --preset id`, `batch file.json`, `clips import <folder>`,
`export <assetId> --profile godot --out <folder>`. It works through a running server
(the `x-astra-client: studio` header) or directly without a server (`--direct`), using the same pipeline
module.
