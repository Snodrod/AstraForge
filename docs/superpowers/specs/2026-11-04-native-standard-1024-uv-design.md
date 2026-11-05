# Native AMD Standard 1024 + UV 1024 design

**Goal:** verify single-view Standard 1024 geometry with a 1024 × 1024 UV atlas on the RX 9070 XT without opening unrelated workflows.

**Decision:** add the fail-closed native capability `uvTextureStandard1024`, default `false`. Permit the combination only for `windows-rocm`, single view, `quality: standard`, `uvTexture: true`, and `vertexColors: false` when both `single1024` and `uvTextureStandard1024` are explicitly true. Keep the existing draft-only texturing rule for every other backend and combination.

**Experiment:** a dedicated runner temporarily enables only `uvTextureStandard1024`, restores `native-ready.json` in `finally`, and runs the existing native E2E once. It writes `1024-single-uv1024.json` and a GLB whose name contains `single-standard-uv1024`, so the verified draft artifact is not overwritten.

**Safety:** no driver, network, VPN, WSL, NVIDIA, multi-view, UV 512, or vertex-color changes. No CPU/WSL/model fallback. GPU jobs run sequentially. Permanent readiness changes only after technical and visual acceptance.

**Acceptance:** focused tests pass; the real job completes with an accepted GLB, finite in-range UVs, a non-uniform 1024 × 1024 PNG atlas, and `fallbackEvents: []`; a rendered preview is visually inspected. On OOM, preserve diagnostics and stop rather than lowering quality.
