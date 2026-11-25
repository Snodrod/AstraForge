# Local A-pose normalization for coherent AMD multi-view generation

## Goal

Improve the experimental orc multi-view result on the verified RX 9070 XT by replacing the dynamic source pose with a coherent full-body A-pose before generating four views. Keep inference local after an explicitly authorized model download, preserve the existing native AMD runtime, and rerun only the established Draft 512 + light UV 1024 path.

This is an experiment, not a new production capability or a universal AMD backend.

## Observed problem

The current source image has a dynamic stance, bent arms, asymmetric equipment, and one oversized spiked pauldron. The existing view prompts assume a strict T-pose. Local MV-Adapter receives only `high quality, best quality`, so it invents side equipment and produces incompatible side silhouettes.

MV-Adapter also generates a front image in the same batch as the other views, but the current experiment discards it and sends the original front to Pixal3D. This mixes two inconsistent view sets. The previous light run completed technically, but visual inspection found coarse anatomy, merged arms/equipment, and torn clothing/base regions.

## Chosen approach

Use `Qwen/Qwen-Image-Edit-2509` for one local instruction-based pose-normalization pass, then generate all four views with the existing local MV-Adapter pipeline.

Qwen-Image-Edit-2509 is suitable because its model card explicitly covers person identity preservation, pose transformations, and keypoint conditioning. Use the community `Qwen-Image-Edit-2509-Q4_K_M.gguf` conversion through ComfyUI-GGUF because the full BF16/FP8 variants are too demanding for this 16 GB card. The initial Q4_K_M file is exactly 13,065,746,976 bytes. The FP8 Qwen2.5-VL text encoder is approximately 9.38 GB and the VAE approximately 254 MB; expected downloads total approximately 22.7 GB before any optional accelerator LoRA.

Use Q4_K_M as explicitly selected by the user. Do not silently substitute Q3 or another model. At the time of design, Windows reported 31.1 GiB total RAM, 8.5 GiB immediately free RAM, and approximately 218 GB free disk space. Offloading may be slow; completion and artistic quality are not guaranteed before measurement.

## Isolation and safety

- Reuse the isolated `runtime/ComfyUI-Pixal3D-AMD-Native` environment and its existing Torch/ROCm installation.
- Do not install, replace, or configure GPU drivers; do not change WSL, networking, VPN, global Python, or reboot settings.
- Download only the explicitly selected Qwen model components and the small ComfyUI-GGUF source/dependency required to load them.
- Keep model weights and generated images in Git-ignored runtime/data directories. Track manifests, workflows, prompts, tests, and reports as source where appropriate.
- Run all GPU work sequentially. Stop only processes started by the experiment and unload models after each stage.
- Preserve the previous `orc` and `orc-light` outputs. Write new outputs under `data/runtime/orc-apose/` and separate diagnostics.
- Do not enable a persistent production profile or alter `native-ready.json`.
- Do not commit or push without a separate user request.

## Stage 1: local A-pose front

Build a narrow ComfyUI workflow using the existing Qwen edit nodes plus the GGUF diffusion-model loader. Start with one 1024 × 1024 output, seed 42, 40 inference steps, `true_cfg_scale` equivalent 4.0, and guidance 1.0, following the official model-card starting point. Do not add a Lightning LoRA in the correctness baseline.

Initial prompt:

```text
Edit the supplied image into a clean full-body FRONT character-design view of exactly the same fantasy orc. Preserve his face, tusks, topknot, green skin, scars, body proportions, painted game-concept-art style, clothing, boots, belts, pouches, fur, and all existing colors. Keep exactly one oversized spiked metal pauldron on the character's own RIGHT shoulder, which appears on the LEFT side of the image in this front view; the other shoulder remains bare. Change only the body pose and framing: standing upright in a relaxed neutral A-pose, head facing forward, arms lowered straight and held slightly away from the torso, hands open and empty, legs straight and parallel, both feet flat and fully visible. Show the entire character from hair to soles, centered, with neutral chest-height orthographic framing and a plain neutral gray studio background. Do not add weapons, shields, jewelry, armor, props, scenery, text, or a second character.
```

Initial negative prompt:

```text
different character, changed face, changed outfit, extra arms, extra legs, extra fingers, fused fingers, missing hands, missing feet, duplicate pauldron, second pauldron, symmetrical shoulder armor, weapon, sword, axe, shield, held prop, cropped feet, cropped hands, dynamic pose, T-pose, three-quarter view, side view, back view, collage, text, watermark, blurry, noisy, deformed anatomy
```

Save the prompt, negative prompt, seed, dimensions, model filenames, file hashes, workflow, runtime versions, elapsed time, and output path in a JSON report. The original source remains unchanged.

### Front acceptance gate

Inspect the generated front before MV-Adapter:

- one complete upright orc, front-facing, with both hands and both feet visible;
- arms lowered away from the torso and legs approximately parallel;
- exactly one large pauldron on the character's right/image left;
- no new weapon, shield, duplicate limb, duplicate equipment, or second figure;
- recognizable face, clothing, colors, and asymmetric details;
- no collage, labels, or cropped silhouette.

A technically valid PNG is not sufficient. If the pose or identity is wrong, make at most one bounded prompt/seed revision in this experiment. On an out-of-memory failure, stop and report; do not silently download a smaller model or launch another GPU job. A later quantization or resolution change requires an explicit decision.

## Stage 2: coherent four-view set

Use the accepted A-pose front as the MV-Adapter reference. Generate front, left, back, and right together at 512 × 512 with seed 21 and 30 steps so the views attend to one another.

Initial MV-Adapter positive prompt:

```text
full-body fantasy orc character design, neutral A-pose, consistent identity and outfit across orthographic front, side and back views, green skin, tusks, topknot, exactly one oversized spiked metal pauldron on the character's right shoulder, opposite shoulder bare, empty open hands, straight parallel legs, both feet visible, painted game concept art, plain neutral gray studio background, high quality, best quality
```

Initial MV-Adapter negative prompt:

```text
watermark, text, collage, multiple characters, ugly, deformed, noisy, blurry, low contrast, extra arms, extra legs, extra fingers, missing hands, missing feet, cropped figure, duplicate pauldron, second pauldron, symmetrical shoulder armor, weapon, sword, axe, shield, held prop, dynamic pose, T-pose, three-quarter view, perspective tilt
```

Retain the accepted A-pose source separately. For the derived four-view set, use the MV-Adapter-generated front from the same batch instead of mixing in its input front. Record that handoff explicitly in `views.json`; do not overwrite the source.

Normalize all four outputs together, create a labeled contact sheet, and inspect scale, orientation, pose, equipment asymmetry, and segmentation holes before shape generation. Automated checks cover file count, dimensions, non-empty alpha/content, report integrity, and correct front handoff; artistic acceptance remains a visual gate.

## Stage 3: existing native AMD light cycle

Only after accepted views:

1. Generate Draft 512 multi-view shape with the existing native AMD workflow.
2. Validate the GLB and preserve it.
3. Run the light texture stage: CPU decimation to at most 120,000 triangles, four texture steps, denoiser resolution 1024, final UV atlas exactly 1024 × 1024.
4. Keep the NAF tiling fix enabled in both shape and texturing paths.
5. Validate accepted geometry, finite in-range UVs, embedded material/image, exact atlas dimensions, and `fallbackEvents: []`.
6. Render front/back/side/three-quarter previews and inspect the atlas. Compare against `orc-light` without claiming that texture resolution alone caused any visual change.

Do not run Standard 1024, UV 2048, UV 4096, or concurrent GPU jobs in this experiment.

## Documentation to update

After measurement, update:

- `docs/forge/views.md` with the A-pose-first rule, coherent-front handoff, prompts, negative prompts, and view-rejection checklist;
- `docs/forge/pixal3d-native-amd.md` with the exact experimental scope, evidence, limitations, and observed timings;
- `scripts/native/README.md` with reproducible commands, required local assets, cleanup, and the distinction between verified single-view capabilities and this experiment.

Document actual results, not planned successes. If Qwen or MV-Adapter fails, record the failing stage and do not document the full cycle as verified.

## Tests and evidence

Add focused unit tests for workflow construction, prompt propagation, bounded options, same-batch front handoff, output naming, and report fields. Run the existing native multi-view, UV atlas, backend, and native-tool test groups after changes.

Evidence consists of:

- local asset manifest and hashes;
- A-pose workflow/report and source/result preview;
- four-view `views.json` and contact sheet;
- shape and texture JSON reports;
- final GLB, atlas, and rendered comparison previews;
- server/runner logs with real errors retained.

## Completion criteria

The experiment is technically complete only when the accepted A-pose front and coherent four-view set pass visual inspection, the light full cycle finishes sequentially, the final GLB passes UV/material validation, and the result is visually inspected. It is artistically successful only if the new model reduces the observed merged equipment/anatomy artifacts relative to `orc-light`; larger atlases or higher polygon counts are not completion criteria.

## Implementation status on 2026-11-04

The exact Q4_K_M, text-encoder and VAE downloads passed size and SHA-256 verification. ComfyUI-GGUF commit `6ea2651e7df66d7585f6ffee804b20e92fb38b8a` loaded successfully in the native runtime.

The first Qwen A-pose subsequently completed in 1865.4 seconds, and MV-Adapter generated four views in 46 seconds. Visual inspection rejected both stages: facial identity changed, asymmetric equipment was replaced by symmetric items, and side views invented a long facial wedge. No shape from this set was accepted.

The original inline prompts in this document are retained only as historical initial prompts. They are superseded by `2026-11-04-astraforge-amd-reusable-identity-lock-design.md`: resolved identity-lock/preset/negative JSON, immutable attempts, SHA-256 manifests and explicit human approval now govern the pipeline. The revised no-GPU contracts are implemented; a new real generation and the revised light full cycle remain unverified. This experiment must not be described as artistically complete.
