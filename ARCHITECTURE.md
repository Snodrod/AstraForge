# AstraForge architecture and roadmap

## Implemented

React + Three.js → Express on 127.0.0.1:3210 → a separate ComfyUI on 127.0.0.1:8190. SDXL creates a concept from text, rembg/U2Net removes the background on the CPU and fits the object into a white square, and Hunyuan3D 2.0 builds a mesh from the image. An existing ComfyUI on 8188 is not reconfigured. The scene, materials, transforms, keyframes, ready-made clips and particles run in the browser. Saved meshes without normals get computed normals before display.

The LLM adapter supports Ollama, OpenAI Chat Completions and Anthropic Messages. The LLM returns JSON with a limited set of actions; Zod validates them before they are applied. It does not execute Blender Python, shell or arbitrary JS. The server stores keys in DPAPI; the client only receives a "connected" flag.

`data/assets` holds images and GLB; `data/jobs` holds the queue and completed jobs; `data/projects` holds scenes with geometry; `data/exports` holds exported files; `data/runtime` holds the separate ComfyUI directories and logs; `data/models` holds weights. Weights must stay out of Git and source-code archives.

## Generator choice

The reference setup is a 12 GB VRAM GPU with about 64 GB of RAM. Hunyuan3D 2.0 was chosen as the practical option: it is already supported by the installed ComfyUI and has official requirements of about 6 GB VRAM for geometry. [Hunyuan3D-2](https://github.com/Tencent-Hunyuan/Hunyuan3D-2#-models-zoo), [official ComfyUI workflow](https://docs.comfy.org/tutorials/3d/hunyuan3D-2).

TRELLIS.2 is a candidate for a more powerful GPU and detailed PBR geometry. The official implementation targets Linux and a minimum of 24 GB VRAM. Reduced modes in third-party builds have not been verified on a 12 GB GPU. [Microsoft TRELLIS.2](https://github.com/microsoft/TRELLIS.2#prerequisites).

Hunyuan3D 2.1 offers improved PBR textures, but the official requirements are 10 GB for shape and 21 GB for textures. Therefore the full standard pipeline cannot be promised on 12 GB. [Hunyuan3D-2.1](https://github.com/Tencent-Hunyuan/Hunyuan3D-2.1#-models-zoo).

TripoSR is a potential lightweight mode for quick drafts. It is not installed in the current version: the main pipeline already goes through Hunyuan3D without extra CUDA dependencies. [TripoSR](https://github.com/VAST-AI-Research/TripoSR).

## Next stages

1. **Blender bridge.** A separate local worker with a limited set of import/export, decimate, remesh, UV, bake and skeleton-preparation operations. Operations must support cancellation, version saving and result verification. Do not run arbitrary Python from an LLM.
2. **Creatures.** Anatomy and symmetry checks, mesh separation, manual skeleton editing, weights, auto-skinning and a motion library. Humanoid templates do not automatically solve rigging of dragons and multi-legged creatures. Different skeleton types and manual correction are needed.
3. **Items and ships.** Multi-view concept, separation of hull/wings/engines, symmetry, hard edges, separate pivots, animation of mechanisms. A generated mesh needs review and retopology before use in a game.
4. **Textures.** An isolated Hunyuan3D-Paint worker, sequential loading of geometry and textures, VRAM checks, UV and PBR packing. Quality and time must be compared separately on 12 GB; if memory is insufficient, provide for an external GPU only by explicit user choice.
5. **Effects.** Emitters attached to objects and bones; custom birth/life/speed parameters; smoke, fire, bloom; export of effect descriptions for game engines. GLB by itself does not carry arbitrary particles.
6. **Product packaging.** Installer, an independent managed runtime, model updates with SHA-256, disk-full handling, project recovery, LLM queues and cancellation, visual regression tests, license audit before distribution.

These are development directions, not features that already exist. The sources are split into `src/scene.js`, `src/animation.js`, `src/App.jsx` and the server adapters `server/jobs.mjs`, `server/llm.mjs`, `server/runtime.mjs`.

## Implemented in 0.3
Local UV: server/uv.mjs → scripts/unwrap_uv.py → xatlas, installed in data/python. The remap result preserves skin/morph/normal/colour. Textures: src/media-core.js, src/texture-bake.js, src/media-tools.js. Sprites: a separate WebGLRenderer with a deterministic AnimationMixer, PNG/JSON/.tres in a ZIP. Project storage: server/projects.mjs, gzip JSON and metadata; old JSON files are still readable.
Tripo: server/tripo-core.mjs checks operations and compatibility; server/tripo.mjs uses the official SDK API v3, a server-side DPAPI key, history, explicit submission and bounded result downloads. The browser does not receive the key. UI: src/TripoPanel.jsx. Generation remains an optional cloud feature; local tools do not require Tripo.

# Reference fidelity update — 2026-10-10

Reference RGB/alpha and native crop metadata now survive independently of the geometry encoder image. `reference-core.js` supplies calibrated cameras, silhouette/colour metrics, bounded landmark warps and contour suggestions; `reference-tools.js` performs actual preview rendering and geometry updates. `ReferenceCompare.jsx` presents identical views, overlay, automatic camera calibration, proposed contour points, quality-gated deformation and aligned UV projection. All scores explicitly describe a single view rather than anatomical correctness.

The isolated runtime whitelists only the application's `AstraForgeMesh` node while other custom nodes remain disabled. `marching_mesh.py` extracts a Lewiner surface without artificial padding and checks orientation against the exterior. The workflow converts native level 0 to normalized voxel level 0.5 because the installed Comfy VAE wrapper applies `(x+1)/2` and clamping. Runtime logging uses inherited file handles so the worker can survive a web-server restart; explicit Stop still terminates the owned worker.

`mesh-validation.mjs` enforces generated GLB byte/count bounds before import. The optimizer creates a working mesh before UV/rigging. Re-extraction reuses the stored input and available runtime cache, with recomputation when that cache has expired. Failed diagnostic geometry is not offered as a completed character.

Detailed part reconstruction, validated unseen views, full neural PBR on a 12 GB device and anatomical acceptance are still incomplete. Code/data-level validity must not be presented as exact visual reference fidelity.
