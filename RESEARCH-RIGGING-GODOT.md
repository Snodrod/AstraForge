# Components for characters and Godot

Checked on 9 October 2026: README files, model cards, and the sources for rigging, mesh preparation and motion transfer. The neural models from these projects were not run on the reference setup. The research refers to UniMate `b78c7807df7cf4231d4e67f8d0ece29c5e11025b` and AssetFurnace `d16bcd7f771575977b548061d92402120914bf5f` (GitHub tree snapshots).

## Decision for AstraForge

Main path: **concept → mesh → game-mesh preparation → skeleton and weights → clips → verified GLB → Godot**. The bone and pose editor must work locally without neural auto-rigging. AI modules are attached as separate workers; the user can correct their result.

| Component | Role | Decision |
|---|---|---|
| UniMate | Text-driven motion for a ready skeleton | Candidate for an experimental mode; the current weights have an NC restriction |
| UniMate site | Result demo, mesh/skeleton display interface | An interaction reference, not a source of a model library |
| image-to-3dlab / AssetFurnace | Preparation, rig, motion, export | The most useful components for a game pipeline |
| SkinTokens / TokenRig | Skeleton and weight generation from a mesh | A separate future backend; 12 GB VRAM is below the official minimum |
| Blender/Rigify | Geometry preparation, manual editing and rigs | The basis for reliable heavy operations; isolated process |

## UniMate

The site and the repository belong to one project. It accepts an already prepared rig and a motion description; creating a character from an image and initial skinning remain other stages. Useful features are transition generation, partial motion editing and joining actions. [Project](https://linzhanmou.com/unimate/).

`rig_preprocess` provides a canonical coordinate system, joint order and semantic labels. For external GLB/FBX it proposes checking names and direction before building the model input. The shared checkpoint has a limit of the order of 70 joints, so the face and detailed fingers need a separate solution. This preprocessing handles an existing rig and does not create one. [Guide](https://github.com/Friedrich-M/UniMate/blob/main/data_process/rig_preprocess/README.md).

The code is MIT, but the weights are CC BY-NC 4.0. They cannot be considered a ready commercial backend without separate permission. The model card also notes foot sliding, instability on rare motions and no guarantee of clean third-party rights. Judging by the network size, inference looks like a plausible candidate for a 12 GB consumer GPU, but this is an assumption: VRAM was not measured exactly and it was not run on Windows. Training from scratch is not part of the local studio plan. [Model card and license](https://huggingface.co/Linzhan/UniMate).

## What to take from AssetFurnace

The repository was renamed from image-to-3dlab to AssetFurnace; the code is Apache-2.0. There is no need to copy its UI and runtime wholesale: AstraForge already has a local interface, storage and verified generation. What is needed are the independent stages. The repository's general license does not replace the licenses of the models. [Repository](https://github.com/Bingeljell/image-to-3dlab).

1. **Finish:** target polygon budget, simplification, UV/atlas, bake and saving of intermediate results. This closes the current gap between a heavy neural mesh and a game asset. Repaint in the verified `finish_api.py` is limited to Apple Silicon, so the whole painting stage cannot be ported to Windows as a ready feature. Compression must be checked separately on the specific Godot version and in a viewer. [Finish](https://github.com/Bingeljell/image-to-3dlab/blob/main/viewer/finish_api.py).
2. **Rig and ready-made motions:** `autorig_api.py` separates SkinTokens and CPU retargeting. Motions are prepared in advance via Kimodo; applying them does not require downloading the large Kimodo model. This is a separate useful library of actions, not generation of arbitrary text at run time. The provenance and licenses of the source mesh, rigger and motion are preserved. [Autorig API](https://github.com/Bingeljell/image-to-3dlab/blob/main/viewer/autorig_api.py), [retargeting](https://github.com/Bingeljell/image-to-3dlab/blob/main/scripts/kimodo_retarget.py).
3. **Weight checks:** removal of influence from anatomically distant bones, normalization, test poses. Zero weight sums, wrong indices and "a finger bound to the leg" must be caught before export. Such checks do not replace visual inspection. [Weight cleanup](https://github.com/Bingeljell/image-to-3dlab/blob/main/scripts/clean_skin_weights.py).
4. **Quadrupeds:** a separate Rigify pipeline with metarig fitting, a proxy for weight solving, weight transfer, leg IK and gait setup. This is not a universal automatic rig for dragons and spiders; fitting is required. The verified upstream targets a specific Blender 5.2. [Documentation](https://github.com/Bingeljell/image-to-3dlab/blob/main/docs/quadruped-pipeline.md).
5. **Provenance:** generator version, source concept, seed, preparation stage, licenses and checksums next to the final GLB. This is a useful asset passport for a team and an asset store. [Implementation](https://github.com/Bingeljell/image-to-3dlab/blob/main/image_to_3dlab/provenance.py).

## SkinTokens and the reference setup

This is the most direct candidate for an AI skeleton and AI weights. The code and the published checkpoints are declared MIT, and the two checkpoints are about 1.6 GB. But the official minimum for NVIDIA is **14 GB VRAM**, while the reference GPU has 12 GB. The size of the weight files is not the same as inference memory. Therefore a separate, verifiable mode with memory measurement and quality checks is needed; reducing beam search/memory is an experiment, not a promise that it works. [Code and requirements](https://github.com/VAST-AI-Research/SkinTokens), [weights](https://huggingface.co/VAST-AI/SkinTokens).

The AssetFurnace installer pins revisions and uses portable attention, but its `.venv/bin/python` path shows that a Windows port needs verification. The general README note about Windows support does not prove that every backend works on Windows. [Bootstrap](https://github.com/Bingeljell/image-to-3dlab/blob/main/scripts/bootstrap_autorig.py).

## Godot contract

- GLB 2.0 with mesh, skin, inverse bind matrices, skeleton and named clips; OBJ is not suitable for an animated character. [Godot formats](https://docs.godotengine.org/en/stable/tutorials/assets_pipeline/importing_3d_scenes/available_formats.html).
- Rest pose and pose are edited separately. For humanoids: T-pose, clear English names, Root/Hips, correct axes and a BoneMap. Matching bone names alone does not ensure retargeting. [Godot retargeting](https://docs.godotengine.org/en/stable/tutorials/assets_pipeline/retargeting_3d_skeletons.html).
- Explicit in-place and root motion modes, looping Idle/Walk/Run and separate one-shot actions. They must be exported as separate clips of one skeleton.
- Checks: influence limit, weight sum, presence of UV/materials, positive scale, no NaN, a consistent hierarchy and stability of test poses.
- Humanoid/Quadruped/Custom profiles. Insects, winged creatures and mechanisms use their own schemes; one human rig for everything must not be promised.
- Mandatory import of the final GLB into a separate Godot test project before claiming "verified in Godot". A structural GLB test and a Three.js view are useful but do not replace this import.

## Integration order

First the local bone editor, rest/pose, skinning and weight control, animation clips and export. Then the Blender worker and the game Finish. After that SkinTokens, once it is proven to work within the memory limits, and a library of licensed motions. UniMate should be attached as a separate experimental text-to-motion backend with an explicit usage mode and a check of the weight terms.

## Implemented after the analysis

AstraForge 0.2 added its own basic bone editor, templates, Rest/Pose, initial geometric weights, a brush and named clips. The code of the studied projects and their neural weights were neither copied nor run for this. A real export of a test mannequin with Idle/Walk/ArmRaise was imported into Godot 4.7.2: 27 bones, 15 bound meshes; the two verified clips change the poses of the corresponding bones. Instructions: `GODOT-RIGGING.md`. AssetFurnace Finish/retargeting, SkinTokens and UniMate remain candidates for later integration.
