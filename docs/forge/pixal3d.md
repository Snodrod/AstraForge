# Pixal3D: the second 3D engine (more accurate than Hunyuan3D)

State as of 2026-11-03. Relates to `docs/forge/views.md` (views) and `docs/forge/API.md`.

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

**Studio.** The "Views" block and the "Geometry quality" section of the "Create" tab contain a **"3D engine"** switch: Pixal3D (more accurate) or Hunyuan3D (faster). If Pixal3D is installed, it is selected by default (the choice is remembered in the browser); if not, the Pixal3D button
is inactive, and a note under the switch says what is missing. Pixal3D offers draft 512 (four-step Euler, about 100k faces) and full 1024 (Heun cascade, about 700k faces); the unrelated shape-generator and surface-level settings are hidden.
It is an ordinary generation job: queue, phase-by-phase progress, cancel, the "Results" history (prefixed "Pixal3D ·"). A single view is handled by the "Create 3D model" button, several views by "Create 3D from views". AMD multi-view 1024 is disabled in the UI with a reason and a one-click draft 512 alternative. Errors are shown in the UI language.

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

**Generation parameters** (`pixal3dWorkflow` in `server/schema.mjs`): full 1024 uses Heun, 25 steps for the coarse shape and 12 steps each for 512/1024 with about 700 thousand faces by default; draft 512 uses four-step Euler and about 100 thousand faces.
For several views the FOV is fixed at 0.1 rad and azimuths are 0/90/180/270 (front/left/back/right); a single view uses the MoGe-2 camera. The seed comes from the job (1000 in the evaluation).
"Re-extract the surface" is unavailable for Pixal3D (the mesh is final, there is no surface level): a different result comes from a different seed.

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

The project-owned WSL2 runtime uses ROCm PyTorch and the bounded PyTorch NAF fallback; no NVIDIA NATTEN, CUDA wheels or `nvdiffrast` are required for shape generation, vertex colors or UV base color texturing. `ASTRA_AMD_NAF_ROW_BLOCK` controls the fallback's row block and defaults to `2`; the patch also upgrades existing installations that still contain the former fixed `row_block = 1` without reinstalling dependencies. For single-view texturing, `ASTRA_AMD_NAF_TILE` defaults to `2` and enables the upstream tiled NAF path. Padding is bounded to each row block instead of allocating a full padded copy. The AMD UV branch reuses ComfyUI's existing PyTorch/NumPy UV unwrap, tiled barycentric rasterizer and interpolation helpers instead of CuMesh UV unwrap and nvdiffrast.

### Compatibility matrix

| GPU / VRAM | Profile | Views | Texturing | State |
|---|---|---|---|---|
| NVIDIA RTX 4070 / 12 GB | full 1024 | single or multi | none (shape only) | verified |
| AMD RX 9070 XT / 16 GB | draft 512 | single | none | verified |
| AMD RX 9070 XT / 16 GB | draft 512 | multi | none | verified |
| AMD RX 9070 XT / 16 GB | full 1024 | single | none | verified |
| AMD RX 9070 XT / 16 GB | draft 512 | single | `COLOR_0` | verified |
| AMD RX 9070 XT / 16 GB | draft 512 | single | 512 UV base color | verified |
| AMD RX 9070 XT / 16 GB | full 1024 | single | `COLOR_0` or UV | unsupported: rejected before job creation |
| AMD RX 9070 XT / 16 GB | draft 512 | multi | `COLOR_0` or UV | unsupported: single-view only |
| AMD RX 9070 XT / 16 GB | full 1024 | multi | any | unsupported: rejected before job creation |
| AMD GPU / 24 GB | full 1024 | multi | any | not verified; currently rejected pending a hardware run |

The API and CLI apply the same compatibility checks. Before any 1024 run, the server reads `/system_stats` and requires at least 11,264 MiB free VRAM on one device. This is the measured additional requirement of the verified single-view run plus a safety margin, not its whole 15.3 GB peak. Low or missing telemetry produces a clear error recommending draft 512 before image upload, GPU locking or prompt submission.

### NAF parity and microbenchmark

Verified on 2026-11-02 with ROCm `7.2.26015-fc0010cf6a`: CPU reference maximum absolute error `2.22e-16`, GPU-versus-CPU maximum absolute error `4.77e-7`. The bounded microbenchmark used kernel 9, four heads, dimension 64 and value dimension 128. Every row block at every shape matched block 1 exactly in this run:

| Shape | Row block | Mean call | Peak allocation | Parity vs block 1 |
|---|---:|---:|---:|---:|
| 1×32×32×4×64 | 1 | 8.02 ms | 103,490,048 B | exact |
| 1×32×32×4×64 | 2 | 5.26 ms | 117,654,528 B | exact |
| 1×32×32×4×64 | 4 | 4.07 ms | 143,427,584 B | exact |
| 1×32×32×4×64 | 8 | 11.87 ms | 196,677,632 B | exact |
| 1×64×64×4×64 | 1 | 21.82 ms | 148,259,840 B | exact |
| 1×64×64×4×64 | 2 | 16.46 ms | 174,884,864 B | exact |
| 1×64×64×4×64 | 4 | 47.40 ms | 228,134,912 B | exact |
| 1×64×64×4×64 | 8 | 47.25 ms | 336,732,160 B | exact |
| 1×128×128×4×64 | 1 | 66.88 ms | 313,034,752 B | exact |
| 1×128×128×4×64 | 2 | 190.17 ms | 353,964,032 B | exact |
| 1×128×128×4×64 | 4 | 187.68 ms | 462,561,280 B | exact |
| 1×128×128×4×64 | 8 | 454.35 ms | 676,052,992 B | exact |
| 1×256×256×4×64 | 1 | 773.14 ms | 949,030,912 B | exact |
| 1×256×256×4×64 | 2 | 761.50 ms | 1,014,112,256 B | exact |
| 1×256×256×4×64 | 4 | 2,160.56 ms | 1,177,550,848 B | exact |
| 1×256×256×4×64 | 8 | 3,078.36 ms | 1,602,781,184 B | exact |

Block 2 remains the default: it was faster than block 1 at 64×64 and slightly faster at 256×256 with bounded additional memory. It was slower at 128×128 in this run, so the default is not claimed to be universally fastest.

### End-to-end runs

Draft 512 was verified end to end: single-view job `de105220-ccbc-4789-a7ca-ae565323e9ff` took 186.3 s with a 10,048.65 MB VRAM peak; four-view job `9bcda0be-ca33-4227-959b-e217544179fd` took 184.0 s with a 10,175.95 MB peak. Both normalized GLB files passed technical mesh validation. The four-view draft can be slightly faster than the single-view draft because the single-view workflow additionally uses MoGe-2 camera estimation.

The full single-view 1024/Heun profile completed successfully with block 2: job `7edaa31c-8155-4d7c-977d-dc5839510761`, 842.0 s, 15,301.30 MB VRAM peak (4,583.57 MB baseline), 697,848 triangles and 349,109 vertices. The 12,564,220-byte normalized GLB passed validation and is saved as `data/runtime/amd-pixal3d-single-standard.glb`. The existing 60-minute deadline was sufficient and remains unchanged. Multi-view 1024 was deliberately not run on the 16 GB card and is now rejected explicitly; a 24 GB run remains unverified.

Opt-in AMD single-view `COLOR_0` vertex painting was verified with the draft 512 profile: job `4c3eeb6f-3d07-4a21-9baf-2af318f5fe42`, 321.3 s, 14,752.64 MB VRAM peak (3,267.55 MB baseline), 98,615 triangles and 49,865 vertices. The 1,982,160-byte normalized GLB passed validation with `COLOR_0` required and is saved as `data/runtime/amd-pixal3d-single-colors.glb`. Its external four-view render found 2,947 distinct rounded RGB values, all finite. This verifies finite, varied colors, not color fidelity against a reference. Shape-only generation remains the default; multi-view vertex colors and 1024 geometry with vertex colors are rejected as unsupported.

Opt-in AMD single-view UV base color texturing was also verified with draft 512 geometry, texture latent 1024 and a 512 × 512 atlas: job `bc208684-a3dc-4b7f-9a2d-4a612af740f7`, 389.1 s, 14,731.38 MB VRAM peak (4,450.46 MB baseline), 98,525 triangles and 60,117 vertices after UV vertex splitting. The 2,735,876-byte normalized GLB contains `TEXCOORD_0` and an embedded PNG `baseColorTexture`; strict validation passed before and after normalization. It is saved as `data/runtime/amd-pixal3d-single-uv.glb`. The extracted atlas has 7,181 distinct rounded RGB values, all finite, and the external four-view renderer displayed the actual PBR base color. Use `"uvTexture": true` with `"quality": "draft"` in `POST /api/jobs`, or `AMD_PIXAL3D_UV_TEXTURE=1 AMD_PIXAL3D_QUALITY=draft node scripts/verify_amd_pixal3d_e2e.mjs` for the hardware runner. This option requires the AMD WSL single-view draft workflow and cannot be combined with `vertexColors`, multi-view input or 1024 geometry.

## Native Windows AMD backend

An explicit `windows-rocm` backend exists beside the unchanged NVIDIA `windows` and AMD `wsl` backends. Its isolated runtime is `runtime/ComfyUI-Pixal3D-AMD-Native` (or the absolute `ASTRA_PIXAL3D_NATIVE_DIR` override), with native portable Python, ROCm PyTorch and SDPA process construction. It does not call WSL or use `/mnt/c` paths. Backend selection is explicit, and failed native readiness never silently launches WSL.

On 2026-11-03 native single-view draft 512 generation passed on RX 9070 XT / `gfx1201` with PyTorch `2.9.1+rocm7.2.1` and HIP `7.2.53211-158bd99533`. Job `9a99b3dd-f598-4474-b6ff-6b0fef843fe5` completed in 161.4 seconds with no fallback events; the validated 1,825,416-byte GLB contains 53,824 vertices and 98,233 triangles. Reports are `data/runtime/diagnostics/native-amd/512-single.json` and `data/runtime/diagnostics/native-amd/cumesh-probe.log`.

Native `single512`, `single1024`, `uvTexture1024` (draft geometry), and `uvTextureStandard1024` are enabled by their own hardware evidence. Multi-view and other unverified native workflows remain locked behind `pixal3d_native_not_ready`. See `docs/forge/pixal3d-native-amd.md` and `scripts/native/README.md` for selection, rollback, installer and probe commands.

A separate 2026-11-04 texture-only experiment reused an accepted Standard 1024 baton and kept denoiser resolution 1024, seed 42, and 12 Euler steps. Native AMD produced exact 2048 × 2048 and 4096 × 4096 embedded base-color PNGs in 499.0 s and 382.4 s respectively. The accepted GLBs are 22,138,764 and 37,962,708 bytes, each with 357,819 vertices, 662,201 triangles, and no fallback events. The visual difference between 2048 and 4096 was small; 2048 is the experimental size/quality balance and 4096 is the largest atlas verified for this object. VRAM telemetry was unavailable, and neither result unlocks a persistent production profile.

Job results now also retain the selected backend, phase timings, fallback events and output geometry when supplied by the runner. The hardware E2E script accepts `AMD_PIXAL3D_BACKEND=wsl|windows-rocm`, `AMD_PIXAL3D_OUTPUT_DIR` and `AMD_PIXAL3D_JSON`; its default remains `wsl`.

## Licences

- Pixal3D (code and weights, Tencent ARC) — MIT; TRELLIS.2 and the ComfyUI-Trellis2 wrapper — MIT; MoGe — MIT; NAF — Apache-2.0.
- DINOv3 — the DINOv3 licence (commercial use is allowed; when redistributing weights its text must be included); the weights come from the official copy `timm/vit_large_patch16_dinov3.lvd1689m`
  (converted to the transformers format by a converter, the licence text lies alongside).
- **nvdiffrast (NVIDIA) is not used by the AMD path**: it has a non-commercial licence. AMD `COLOR_0` sets `bake_on_vertices: true`; AMD UV texturing sets it to `false` and uses ComfyUI's existing CPU unwrap and PyTorch/NumPy rasterization helpers. The NVIDIA CuMesh/nvdiffrast branch remains unchanged for non-AMD runtimes.

## Limitations

- VRAM: draft shape generation needs about 10 GB; the verified full 1024 shape profile peaked at 15.3 GB, draft vertex coloring at 14.8 GB and draft UV texturing at 14.7 GB. The 1024 preflight rejects less than 11,264 MiB free, but another program can still allocate memory after the check and cause an out-of-memory error.
- Progress goes by phases with a timer: the engine itself does not report steps inside phases, so the bar moves in stages (the percentages are an estimate, not exact progress).
- Cancel interrupts the job at a ComfyUI node boundary: it can take up to a minute for the engine to stop; the next job on the other engine waits for it.
- One view is worse than four (0.90/0.71/0.87/0.74): the side and back are completed by inference. For a final character use views.
- Checked on a humanoid in a T-pose (the test character) and on the deterministic RGBA test object used by the AMD hardware runner. Four-legged creatures were not checked.
- Shape-only output remains the default and uses the existing studio painting steps. Native AMD has separately verified single-view UV 1024 with draft and standard geometry. A standalone baton experiment also verified UV 2048 and UV 4096, but these larger sizes are not persistent production options. Multi-view native texturing remains locked. Normal/AO texture generation is not implemented as a verified feature.

## Tests

`tests/pixal3d.test.mjs` covers shape-only, AMD `COLOR_0` and AMD UV graphs, request compatibility rejection, 1024 VRAM preflight before upload/GPU acquisition, required vertex-color or UV/material/embedded-PNG validation before and after GLB normalization, settings and installation lookup, engine start with a fake process, and generation against a fake ComfyUI with a websocket. `tests/amd-pixal3d-e2e.test.mjs` covers the hardware runner options, including rejection of multi-view 1024 and 1024 texturing; `tests/amd-wsl-scripts.test.mjs` guards the bounded NAF benchmark through 256×256, vertex-color and AMD UV unwrap/rasterizer patches. `tests/gpu-handover.test.mjs` covers waiting for a busy engine without interruption, unloading, skipping a non-running one, the lock around the handover, and two jobs on different engines never running together.

## Files

`server/pixal3d.mjs` (settings, installation check, engine start and stop), `server/pixal3d-run.mjs` (one generation: view upload, lock, graph, progress, GLB),
`server/gpu-handover.mjs` (GPU handover), `server/glb-normalize.mjs` (normalisation), `server/schema.mjs` (`pixal3dWorkflow`, the `engine` field),
`server/jobs.mjs` (the `runPixal3d` branch), `server/index.mjs` (`engine` in jobs, `pixal3d` in `/api/status`), `server/views.mjs` (`forJob` also returns the `cutFiles` cut-outs),
`scripts/views/views_3d.mjs` (CLI), `scripts/Start.ps1` and `scripts/Stop.ps1`, `src/ViewsPanel.jsx` (`EngineSelect`), `src/App.jsx`, `src/views.css`.
The engine itself and its description are in `<ComfyUI-Pixal3D dir>\ASTRAFORGE_README.md`.
