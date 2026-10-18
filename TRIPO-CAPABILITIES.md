# Tripo and AstraForge 0.3

Checked: 9 October 2026. Tripo is a reference for working operations and an additional cloud provider. Its closed generative models are not installed locally. The integration uses the official `@vastai/tripo-sdk` 0.3.0 and API v3.

| Capability | Locally in AstraForge | Through the Tripo connection |
|---|---|---|
| Image → 3D | Hunyuan3D 2.0 | H3.1 / H3.0 / P1 / P2 preview |
| Text → 3D | SDXL → Hunyuan3D | Direct generation |
| Several views → 3D | Not implemented | 2–4 images, the front one is required |
| Create / edit views | Not implemented | Image to Multiview / Edit Multiview |
| Higher detail | 45 steps, 512³ grid | H3 Ultra; result quality needs evaluation |
| UV | CPU xatlas, skin weights preserved | UV generation / packing |
| Materials / textures | Procedural PBR, map loading, brush, reference projection | AI Texture / PBR |
| Semantic parts | Objects of an imported composite model | Segmentation, Generate Parts, Completion |
| Retopology / mesh budget | No local remesher | Smart Low Poly, Quad, Face Limit |
| Bones and weights | Manual skeleton, templates, binding, weight brush | Rig Check, Auto Rig |
| Motions | Bone keys, Idle/Walk/Run drafts, imported clips | Retarget; up to five selected actions per task |
| Geometry stylization | Not implemented | Voxel / Lego / Voronoi / Minecraft |
| Animation baking | All clips into GLB at a given FPS | Bake Animation |
| Isometric sprites | 8 directions by default, PNG RGBA, JSON, Godot SpriteFrames | The GLB result can be processed locally |
| Effects | Sparks, engine, energy; included in sprites / video | Custom VFX remain local |
| Export | GLB, OBJ, PNG, WebM, JSON, ZIP | GLB, GLTF, FBX, OBJ, STL, USDZ, 3MF |
| Batch processing | Local queue of up to four jobs | Separate cloud tasks and history; batch import of concepts is not yet available |

The list of API operations and the current model generations were checked against the [official SDK](https://github.com/VAST-AI-Research/tripo-js-sdk). [Importing external models](https://developers.tripo3d.ai/en/docs/models-import) allows a local GLB to be processed further in Tripo. In the interface the upload stays local until you click "Run in Tripo · paid".

## How to connect

1. Open the **Tripo** tab and add an API key from your [API account](https://platform.tripo3d.ai). It is encrypted with Windows DPAPI; `TRIPO_API_KEY` on the server is also supported.
2. Select an operation, an image / views or a source successful task. For a local model use "Local model → Tripo".
3. Run the paid task. This is an explicit sending of data to the cloud. "Balance" can request the remaining credits.
4. The history shows state, progress, credit usage and results. GLB is loaded into the scene; other formats are downloaded as a file.
5. Fix materials, weights and clips locally; export GLB or sprites for Godot.

Production sequence: generation / import → split / completion → Low Poly → textures → rig check → auto rig → motions → local editing → baking / Godot. According to the [Tripo documentation](https://docs.tripo3d.ai/animation/rig-v2-5-20260210.html), remesh, segmentation and part completion remove the original skeleton data, so they must be done before rigging.

Quad generation produces FBX; this format is downloaded but cannot be imported directly into our viewer. For local viewing use GLB. External GLTF resources are blocked; Meshopt decoding of an embedded GLB is supported.

## Verification and limitations

Verified: the local interface and server, strict parameter validation, the front/left/back/right order, prefixed task IDs of API v3, several motions, rejection of unsafe result URLs, and no retry of an uncertain paid submission. The build and automated checks pass. Live Tripo generations were not run: no API key was provided. Therefore the quality and compatibility of real cloud results are not yet confirmed.

A full local reproduction of Tripo Studio has not been achieved: there are no custom AI textures for all sides, no semantic neural segmentation, no trained auto-rig, no AI retopology, no batch import of concepts, no sculpt and no IK. Local analogues and API calls are marked separately so that a procedural or manual operation is not passed off as a neural one.
