# Native AMD sparse texture 1536 and UV 2048 design

Date: 2026-11-04

## Goal

Improve actual Pixal3D texture detail on the verified RX 9070 XT without regenerating geometry, changing drivers, using WSL, or silently falling back to a lower texture resolution. The experimental target is a 1536 texture latent/denoiser grid with a 2048 × 2048 base-color UV atlas. The existing native Standard 1024 + UV 1024 profile remains unchanged until the experiment passes numerical, hardware, and visual validation.

## Bottleneck and chosen approach

The single-view projected-conditioning path evaluates a dense `R³` feature grid and then selects only the sparse surface coordinates used by the denoiser. At 1536, `R=96`; the dense grid and NAF intermediates produce an approximately 32 GB peak in the existing implementation. The machine has 31.1 GiB RAM and 16 GB VRAM, so an unmodified dense 1536 run is not a useful target.

Compute the same DINOv3/NAF projections directly at the sparse surface coordinates. Preserve the existing coordinate rotation, camera projection, border sampling, NAF tiling, global conditioning, and denoiser weights. Use NAF tile factor 4 for the experiment to bound temporary image-space feature maps. Do not reduce texture steps, alter the seed, cap tokens, or reduce the requested resolution.

Two alternatives are rejected: UV-only upscaling increases atlas dimensions without adding denoiser detail, and dense 1536 relies excessively on shared/paged memory.

## Stage 1: numerical equivalence

Add an explicit sparse-projection path beside the unchanged dense path. Convert sparse integer coordinates to the same rotated continuous points as `ProjGrid`, evaluate low- and high-resolution image features only at those points, and construct the existing sparse conditioning tensors directly.

Compare sparse output against selection from dense output at grid 64 and, where memory permits, grid 96. Cover asymmetric coordinate subsets, border/out-of-image points, tiled NAF, global conditioning, and negative conditioning. Require exact or tightly bounded numerical agreement appropriate to the existing tiled-NAF path; any known tiled/un-tiled difference must be measured separately rather than attributed to sparse selection.

Run focused Python tests sequentially. A parity failure blocks the hardware texture run.

## Stage 2: isolated texture-only hardware run

Reuse the accepted baton GLB and its original 1024 × 1024 input image. Load the existing geometry through a dedicated experimental ComfyUI workflow, preserving its orientation and camera configuration. Run only the Pixal3D texture stage with:

- texture latent/denoiser resolution 1536;
- UV base-color atlas 2048 × 2048;
- sparse projected conditioning;
- NAF tile factor 4;
- the existing seed, sampler, texture steps, and no fallback.

Use separate local ports, logs, output files, and a dedicated report. Do not overwrite the accepted baton GLB, atlas, report, or readiness capabilities. Run no other GPU task in parallel. Drivers, network configuration, Windows priorities, and the stable production workflow remain unchanged.

Record wall time, phase durations, available VRAM/RAM telemetry, requested and actual texture resolution, atlas dimensions, and fallback events. Stop on OOM, driver reset, invalid output, or a numerical/graph mismatch. Do not retry blindly or unlock the mode.

## Acceptance

Technical acceptance requires a completed native AMD run, an accepted GLB with valid UVs, an embedded base-color PNG exactly 2048 × 2048, non-uniform texture content, and no fallback events. Confirm that the texture stage actually ran at 1536; atlas dimensions alone are insufficient.

Visual acceptance requires four-view comparison against the existing 1024 + UV 1024 baton using the same preview method. Inspect crust/scoring detail, ends, backside, stretching, color shift, and UV seams. Higher resolution is not automatically better quality; regressions must be reported honestly.

Only after numerical, technical, and visual acceptance should a separate change expose a persistent experimental capability. This design does not enable 1536 by default and does not claim universal AMD support.

## Verification and delivery

Run focused Python parity tests, relevant Node workflow/validation tests, source/patch reproducibility checks, and `git diff --check`. Preserve raw logs and reports. Deliver the experimental GLB, 2048 atlas, four-view preview, measured memory/time results, and remaining quality limitations. Commits and pushes beyond the approved design checkpoint require a separate user request.
