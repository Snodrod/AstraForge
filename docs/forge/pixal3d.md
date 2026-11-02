# Pixal3D: the second 3D engine (more accurate than Hunyuan3D)

State as of 2026-10-14. Relates to `docs/forge/views.md` (views) and `docs/forge/API.md`.

## What it is and why

Pixal3D (Tencent ARC, built on TRELLIS.2) builds a shape from one or several views. The owner chose it after a comparison on a test humanoid character
(`pixal3d/compare.png` in the work directory, numbers in `compare.json`; silhouette IoU against the supplied views):

| Variant | front | left | back | right | time | VRAM (peak above baseline) |
|---|---|---|---|---|---|---|
| Hunyuan3D-2mv, 4 views | 0.936 | 0.847 | 0.924 | 0.843 | ~150 s | — |
| Pixal3D, 1 view | 0.898 | 0.709 | 0.869 | 0.736 | 355 s | 6.4 GB |
| **Pixal3D, 4 views (fov 0.1 rad)** | **0.971** | **0.937** | **0.940** | **0.938** | **232 s** | **9.5 GB** |

The face comes out noticeably crisper and more faceted. Pixal3D builds **the shape only by default**: textures are still produced by the studio pipeline
("paint_from_concept" from the front and back views), so the remaining steps (cleanup, rig, animation transfer) work with its mesh unchanged. On AMD, a verified single-view opt-in can instead write `COLOR_0` vertex colors without CUDA UV unwrapping or rasterisation.

```
view cut-outs (RGBA, norm/cut_<slot>.png) ──► ComfyUI-Pixal3D :8191 ──► GLB ──► normalisation ──► like a Hunyuan3D mesh
 front · left · back · right (any set,        shape-only graph or opt-in    (centre at zero, height 2,
 front is required) or a single view          AMD COLOR_0 graph             front faces -Y after import into Blender)
```

## How to use

**Studio.** The "Views" block and the "Geometry quality" section of the "Create" tab contain a **"3D engine"** switch: Pixal3D (more accurate, about 4 minutes,
~10 GB VRAM) or Hunyuan3D (faster). If Pixal3D is installed, it is selected by default (the choice is remembered in the browser); if not, the Pixal3D button
is inactive, and a note under the switch says what is missing. With Pixal3D the settings it does not need are hidden (shape generator, "Fast/Balanced/Detailed", surface level).
It is an ordinary generation job: queue, phase-by-phase progress, cancel, the "Results" history (prefixed "Pixal3D ·"). A single view is handled by the "Create 3D model" button
(~6 minutes), several views by "Create 3D from views" (~4 minutes). Errors are shown in the UI language.

**API.** `POST /api/jobs` with `"engine": "pixal3d"` (or `"auto"`); without the field Hunyuan3D works as before (`docs/forge/API.md`).

**Command line.**

```
node scripts/views/views_3d.mjs --views <view set folder> --out mesh.glb --engine pixal3d|hunyuan [--slots front,left,back,right]
        [--seed 1000] [--faces 700000]      # Pixal3D
        [--seed 703] [--quality high] [--level 0] [--swap-sides] [--comfy http://127.0.0.1:8190]   # Hunyuan3D
```

Without `--engine`, `auto` is used: Pixal3D if installed, otherwise Hunyuan3D. Any set of views that includes the front: `--slots front,back` builds the graph from two views.

## Settings and installation

The engine folder and address are set as follows (the first one found):

| What | Source |
|---|---|
| ComfyUI-Pixal3D folder | the `ASTRA_PIXAL3D_DIR` variable, then `config\paths.json`, key `pixal3dDir` (written by the installer; `Start.cmd` copies it into the variable), then `C:\AstraForge\ComfyUI-Pixal3D` |
| address | `ASTRA_PIXAL3D_URL` (or `pixal3dUrl` in `paths.json`), default `http://127.0.0.1:8191`; local http only |

"Installed" means: on disk there are a portable Python, ComfyUI, the ComfyUI-Trellis2 nodes, the Pixal3D weights (single- and multi-view), DINOv3 ViT-L/16 and MoGe-2 (the list is `PIXAL3D_REQUIREMENTS`
in `server/pixal3d.mjs`). If something is missing the studio does not crash: the switch is inactive, a job with `engine: "pixal3d"` is rejected at once with a reason, and `auto` and the CLI use Hunyuan3D.

**On-demand start.** No separate button is needed: the first job starts the engine itself in a hidden window with the same parameters as `Start ComfyUI (Pixal3D, 8191).bat`
(its own caches in the install folder, system CUDA and Python variables cleared), waits for readiness (~6 s on a warm machine, 1-2 minutes on a cold start) and writes a log to
`data\runtime\pixal3d.log`. In advance: `POST /api/runtime/pixal3d/start`. An already running instance (for example one started by hand with the batch file) is used as is by the studio and **is not stopped**.
An instance started by the studio gets its own database (`--database-url`, otherwise two ComfyUI instances of the same folder fight over `ComfyUI\user\comfyui.db`) and a temporary folder `data\runtime\pixal3d-temp`:
by this marker `Stop.cmd` finds and stops exactly that instance, and on a normal studio exit it is removed together with it.

**Fixed generation parameters** (those chosen in the evaluation; `pixal3dWorkflow` in `server/schema.mjs`): heun sampler, 25 steps for the coarse shape and 12 steps each for 512/1024, ~700 thousand faces
(`--faces`), for several views a fixed FOV of 0.1 rad and azimuths 0/90/180/270 (front/left/back/right), for a single view the MoGe-2 camera. The seed comes from the job (1000 in the evaluation).
There is no quality switch; "Re-extract the surface" is unavailable for Pixal3D (the mesh is final, there is no surface level): a different result comes from a different seed.

**Mesh normalisation.** The Pixal3D output is already in the glTF convention (+Y up, front toward +Z), so only a move to the origin and uniform scaling to height 2 are done
(`server/glb-normalize.mjs`; on the test character it matches vertex for vertex the result of the Python normaliser `normalize_seed_mesh.py`, which tried 24 rotations; the identity rotation won for a single view too).

## GPU: the two engines do not run at the same time

Pixal3D takes about 9.5 GB of 12, and Hunyuan keeps its models in memory, so they cannot be in VRAM together. Rules (`server/gpu-handover.mjs`):

1. All jobs still take the shared GPU mutex (the default name is set in `server/gpu-lock.mjs` and can be overridden with `ASTRA_GPU_LOCK_NAME`; the Blender steps take the same one), and the studio's job queue runs one at a time: the two engines do not generate simultaneously.
2. Under the lock, **before** a job on engine X, the studio checks the other engine's queue (`GET /queue`). While something there is running or waiting, the studio **waits** (the job shows "Waiting for ... to finish its job"), and the running job **is not interrupted**.
3. When the other engine is idle, `POST /free {"unload_models": true, "free_memory": true}` is sent to it, and the studio waits until its memory is actually freed (no longer than 8 s).
4. After a Pixal3D job the studio itself unloads its models (if the queue is empty), so that the memory goes to the next consumer.
5. An unresponsive engine (not started, not installed) is skipped. This applies to Hunyuan3D from views, from a single image, an SDXL concept, and MV-Adapter.

If another program sends jobs directly to port 8191, they join ComfyUI's own queue before or after the studio's job; the studio does not interrupt them and simply waits for its own result.

## Time and memory (RTX 4070, 12 GB)

Checked on 2026-10-14 through the studio API (`POST /api/jobs` jobs, test character, seed 1000, `views/codex` views; VRAM — peak above baseline per the engine's `/af/vram` and per `nvidia-smi`):

| Run | Engine time | VRAM (peak above baseline) | Triangles | Silhouette IoU front / left / back / right |
|---|---|---|---|---|
| 4 views, GPU free | 239.7 s (job 243 s) | 10.3 GB per `nvidia-smi`, 10.8 GB per the engine probe (the card is completely full) | 653 thousand | 0.971 / 0.937 / 0.940 / 0.938 |
| 4 views, right after Hunyuan (GPU handover) | 220.4 s | 10.8 GB | — | — |
| 2 views (front + back), CLI | 241.1 s | 9.4 GB | 660 thousand | 0.970 / 0.848 / 0.936 / 0.862 |
| 1 view ("Create 3D model" button) | 262.4 s (job 266 s) | 6.4 GB | 684 thousand | 0.892 / 0.711 / 0.861 / 0.734 |

- Agreement with the evaluation: the result's dimensions via the API are +-0.9423 x +-1 x +-0.1944, the same as the evaluated `mv\mesh_mv_pixal3d.glb`; IoU to `ref_front.png` 0.9695 against 0.9697 (back 0.955
  against 0.952), i.e. the orientation and scale are the same. The triangle count floats by a few percent between runs (653-694 thousand), the shape is the same.
- Phases of four views (for the progress bar): loading the model and DINOv3 ~8 s, coarse shape 68 s, 512 shape 46 s, 1024 shape 86 s, decoding 6 s, surface rebuild 15 s, holes and simplification 8 s.
- GPU handover: after a Hunyuan job (6.7 GB in memory) Pixal3D unloaded it in 2 s (in the measurements 8190 held 96 MiB), then Hunyuan "Fast" on 4 views took 26-28 s including the Pixal3D unload.
  Two jobs submitted at once (Pixal3D and Hunyuan) ran in turn: of 259 queue samples of both engines, in none were both executing a job at the same time.
- On-demand start: a second instance on port 8199 came up in 6 s (warm caches), found the Pixal3D nodes, and a repeated start reused it; the `Stop` rule found and stopped only it (8190 and 8191 stayed).
- The GPU lock works across processes: a CLI started during a studio job printed "Waiting for the GPU to be released" and started after it.

## AMD ROCm verification (RX 9070 XT, 16 GB)

The project-owned WSL2 runtime uses ROCm PyTorch and the bounded PyTorch NAF fallback; no NVIDIA NATTEN, CUDA wheels or `nvdiffrast` are required for shape generation or the verified vertex-color path. `ASTRA_AMD_NAF_ROW_BLOCK` controls the fallback's row block and defaults to `2`; the patch also upgrades existing installations that still contain the former fixed `row_block = 1` without reinstalling dependencies. For single-view texturing, `ASTRA_AMD_NAF_TILE` defaults to `2` and enables the upstream tiled NAF path. Padding is bounded to each row block instead of allocating a full padded copy.

Verified on 2026-11-02 with ROCm `7.2.26015-fc0010cf6a`: CPU reference maximum absolute error `2.22e-16`, GPU-versus-CPU maximum absolute error `4.77e-7`. The bounded microbenchmark used kernel 9, four heads, dimension 64 and value dimension 128:

| Shape | Row block | Mean call | Peak allocation | Parity vs block 1 |
|---|---:|---:|---:|---:|
| 1×32×32×4×64 | 1 | 8.41 ms | 103,490,048 B | exact |
| 1×32×32×4×64 | 2 | 5.93 ms | 117,654,528 B | exact |
| 1×32×32×4×64 | 4 | 4.39 ms | 143,427,584 B | exact |
| 1×32×32×4×64 | 8 | 12.03 ms | 196,677,632 B | exact |
| 1×64×64×4×64 | 1 | 22.24 ms | 148,259,840 B | exact |
| 1×64×64×4×64 | 2 | 16.68 ms | 174,884,864 B | exact |
| 1×64×64×4×64 | 4 | 46.89 ms | 228,134,912 B | exact |
| 1×64×64×4×64 | 8 | 47.24 ms | 336,732,160 B | exact |

Block 2 was selected as the consistent compromise: about 25% faster than block 1 at 64×64 with bounded memory. Draft 512 was also verified end to end: single-view job `de105220-ccbc-4789-a7ca-ae565323e9ff` took 186.3 s with a 10,048.65 MB VRAM peak; four-view job `9bcda0be-ca33-4227-959b-e217544179fd` took 184.0 s with a 10,175.95 MB peak. Both normalized GLB files passed technical mesh validation.

The full single-view 1024/Heun profile then completed successfully with block 2: job `7edaa31c-8155-4d7c-977d-dc5839510761`, 842.0 s, 15,301.30 MB VRAM peak (4,583.57 MB baseline), 697,848 triangles and 349,109 vertices. The 12,564,220-byte normalized GLB passed validation and is saved as `data/runtime/amd-pixal3d-single-standard.glb`. The existing 60-minute deadline was sufficient and remains unchanged. Multi-view 1024 was not run: the single-view peak leaves only about 1 GB of headroom on the 16 GB card.

Opt-in AMD single-view `COLOR_0` vertex painting was verified with the draft 512 profile: job `4c3eeb6f-3d07-4a21-9baf-2af318f5fe42`, 321.3 s, 14,752.64 MB VRAM peak (3,267.55 MB baseline), 98,615 triangles and 49,865 vertices. The 1,982,160-byte normalized GLB passed validation with `COLOR_0` required and is saved as `data/runtime/amd-pixal3d-single-colors.glb`. Its external four-view render found 2,947 distinct rounded RGB values, all finite. Shape-only generation remains the default; multi-view vertex colors and the standard 1024 vertex-color combination have not been verified.

## Licences

- Pixal3D (code and weights, Tencent ARC) — MIT; TRELLIS.2 and the ComfyUI-Trellis2 wrapper — MIT; MoGe — MIT; NAF — Apache-2.0.
- DINOv3 — the DINOv3 licence (commercial use is allowed; when redistributing weights its text must be included); the weights come from the official copy `timm/vit_large_patch16_dinov3.lvd1689m`
  (converted to the transformers format by a converter, the licence text lies alongside).
- **nvdiffrast (NVIDIA) is not used by the AMD path**: it has a non-commercial licence. The AMD `COLOR_0` graph sets `bake_on_vertices: true`; CUDA UV unwrapping, UV/PBR baking and rasterisation are disabled. The test `tests/pixal3d.test.mjs` prevents UV unwrap, rasterizer, renderer and nvdiffrast nodes from appearing in either graph.

## Limitations

- VRAM: draft shape generation needs about 10 GB; the verified full 1024 shape profile peaked at 15.3 GB and draft vertex coloring at 14.8 GB. If another program holds memory (Blender, a browser with WebGL, a game), the job can end with an "out of video memory" error.
- Progress goes by phases with a timer: the engine itself does not report steps inside phases, so the bar moves in stages (the percentages are an estimate, not exact progress).
- Cancel interrupts the job at a ComfyUI node boundary: it can take up to a minute for the engine to stop; the next job on the other engine waits for it.
- One view is worse than four (0.90/0.71/0.87/0.74): the side and back are completed by inference. For a final character use views.
- Checked on a humanoid in a T-pose (the test character). Four-legged creatures and objects were not checked.
- Shape-only output remains the default and uses the existing studio painting steps. AMD `COLOR_0` is an opt-in single-view alternative, not a UV/PBR texture pipeline; multi-view coloring is rejected and the standard 1024 coloring combination is unverified.

## Tests

`tests/pixal3d.test.mjs` covers the shape-only and AMD `COLOR_0` graphs, the ban on CUDA UV/rasterizer nodes, required `COLOR_0` validation before and after GLB normalization, settings and installation lookup, engine start with a fake process, and generation against a fake ComfyUI with a websocket. `tests/amd-pixal3d-e2e.test.mjs` covers the hardware runner options and output naming; `tests/amd-wsl-scripts.test.mjs` guards the bounded NAF and vertex-color patches. `tests/gpu-handover.test.mjs` covers waiting for a busy engine without interruption, unloading, skipping a non-running one, the lock around the handover, and two jobs on different engines never running together.

## Files

`server/pixal3d.mjs` (settings, installation check, engine start and stop), `server/pixal3d-run.mjs` (one generation: view upload, lock, graph, progress, GLB),
`server/gpu-handover.mjs` (GPU handover), `server/glb-normalize.mjs` (normalisation), `server/schema.mjs` (`pixal3dWorkflow`, the `engine` field),
`server/jobs.mjs` (the `runPixal3d` branch), `server/index.mjs` (`engine` in jobs, `pixal3d` in `/api/status`), `server/views.mjs` (`forJob` also returns the `cutFiles` cut-outs),
`scripts/views/views_3d.mjs` (CLI), `scripts/Start.ps1` and `scripts/Stop.ps1`, `src/ViewsPanel.jsx` (`EngineSelect`), `src/App.jsx`, `src/views.css`.
The engine itself and its description are in `<ComfyUI-Pixal3D dir>\ASTRAFORGE_README.md`.
