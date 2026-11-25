# Pixal3D/TRELLIS.2 AMD WSL Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run Pixal3D/TRELLIS.2 on the Radeon RX 9070 XT through WSL2/ROCm and deliver visible, validated GLB results in AstraForge.

**Architecture:** Keep the Windows studio and existing ROCm applications unchanged. Add a project-owned WSL distribution and a Linux ComfyUI on port 8191, selected by `pixal3dBackend: "wsl"` in `config/paths.json`; keep the existing NVIDIA Windows backend unchanged.

**Tech Stack:** WSL2, Ubuntu, Python 3.12, ROCm PyTorch, ComfyUI, C++/HIP, PowerShell 5.1, Node.js 22, node:test.

## Global Constraints

- No automatic reboot: stop and warn the user if Windows requires one.
- Keep the distro VHDX, models, caches and generated artifacts inside the task workspace; never commit them.
- Do not install CUDA wheels or substitute CUDA DLLs in the AMD profile.
- Do not replace the working Windows PyTorch 2.9.1+rocm7.2.1 environment.
- Use the existing port 8191, GPU mutex, Pixal3D workflow and GLB normalizer.
- Stop every project-owned server after verification; do not stop unrelated WSL distributions.
- The existing NVIDIA path must retain its current defaults and behavior.

---

### Task 1: Enable and isolate WSL2

**Files:**
- Create: `scripts/wsl/README.md`
- Runtime only: `runtime/wsl/`, `installer/downloads/`

**Interfaces:**
- Produces: WSL distribution `AstraForge-AMD`, version 2, with its VHDX under `runtime/wsl/distro`.

- [ ] **Step 1: Record the current WSL state**

Run: `wsl.exe --status; wsl.exe --list --verbose`
Expected: currently WSL is absent; no project changes are made by this read-only check.

- [ ] **Step 2: Enable WSL without installing a default distro**

Run from an elevated PowerShell only after the already obtained user approval: `wsl.exe --install --no-distribution`
Expected: Windows enables the required features. If it reports that a restart is required, stop here and warn the user; never run `shutdown`, `Restart-Computer` or equivalent.

- [ ] **Step 3: Create Ubuntu and relocate it into the workspace**

Install Ubuntu without launching it, export its root filesystem, unregister only that newly created temporary distro, then run:

```powershell
wsl.exe --import AstraForge-AMD "$PWD\runtime\wsl\distro" "$PWD\installer\downloads\ubuntu-rootfs.tar" --version 2
wsl.exe -d AstraForge-AMD -- cat /etc/os-release
```

Expected: Ubuntu metadata and `wsl.exe -l -v` showing `AstraForge-AMD` at version 2. The VHDX must be under `runtime/wsl/distro`.

- [ ] **Step 4: Verify ROCm device visibility before downloading models**

Install the supported AMD WSL userspace packages inside `AstraForge-AMD`, then run `rocminfo` and a small ROCm PyTorch allocation.

```bash
python3 -c "import torch; assert torch.version.hip; assert torch.cuda.is_available(); print(torch.cuda.get_device_name(0)); print(torch.empty(1024, device='cuda').sum().item())"
```

Expected: `AMD Radeon RX 9070 XT` and `0.0`. If WSL cannot expose the GPU, stop before model downloads and report the driver/ROCm error.

- [ ] **Step 5: Document the exact runtime layout and commit**

```markdown
# Project-owned WSL runtime
Distro: AstraForge-AMD
VHDX: runtime/wsl/distro (ignored)
Engine: runtime/ComfyUI-Pixal3D-AMD (models ignored)
Port: 127.0.0.1:8191
Stop: Stop.cmd; never terminate another distro.
```

Run: `git add scripts/wsl/README.md && git commit -m "Document project-owned AMD WSL runtime"`

---

### Task 2: Add a reproducible Linux ROCm build

**Files:**
- Create: `scripts/wsl/install-pixal3d-amd.sh`
- Create: `scripts/wsl/verify_pixal3d_amd.py`
- Create: `scripts/wsl/start-pixal3d-amd.sh`
- Test: `tests/amd-wsl-scripts.test.mjs`

**Interfaces:**
- Produces: `runtime/ComfyUI-Pixal3D-AMD/wsl-ready.json`, Linux `.venv`, ComfyUI-Trellis2 and native ROCm extensions.
- Produces: start script accepting `--port`, `--temp-directory`, `--database-url` and passing them to ComfyUI.

- [ ] **Step 1: Write failing static contract tests**

```js
test('AMD WSL scripts exclude CUDA wheels and verify the GPU', () => {
  const install = readFileSync('scripts/wsl/install-pixal3d-amd.sh', 'utf8');
  assert.doesNotMatch(install, /download\.pytorch\.org\/whl\/cu|flash_attn-.*win_amd64/);
  assert.match(install, /torch\.version\.hip/);
  assert.match(install, /9070 XT/);
  const verify = readFileSync('scripts/wsl/verify_pixal3d_amd.py', 'utf8');
  for (const name of ['cumesh', 'flex_gemm', 'o_voxel']) assert.match(verify, new RegExp(name));
});
```

Run: `node --test tests/amd-wsl-scripts.test.mjs`
Expected: FAIL because the scripts do not exist.

- [ ] **Step 2: Implement the idempotent build script**

The script must use `set -euo pipefail`, resolve the engine from its own location, create `.venv`, install ROCm Linux PyTorch and pinned ComfyUI requirements, clone pinned ComfyUI/Trellis2/CuMesh/FlexGEMM/O-Voxel source revisions, apply only reviewed ROCm patches, and build each native extension against the same Python and `torch.version.hip`.

```bash
export PYTORCH_ROCM_ARCH=gfx1201
export FORCE_CUDA=1
export CC=hipcc CXX=hip++
"$VENV/bin/python" -c "import torch; assert torch.version.hip; assert '9070 XT' in torch.cuda.get_device_name(0)"
"$VENV/bin/python" -m pip install --no-build-isolation "$SOURCE_DIR"
```

Use PyTorch SDPA instead of the CUDA FlashAttention wheel. Build or select ROCm NATTEN only if the chosen Trellis2 revision actually imports it. Record package versions and source commits in `wsl-ready.json` only after all import/GPU probes pass.

- [ ] **Step 3: Implement native GPU probes and launch script**

```python
import torch
assert torch.version.hip, 'ROCm PyTorch required'
assert torch.cuda.is_available(), 'AMD GPU unavailable'
assert '9070 XT' in torch.cuda.get_device_name(0)
import cumesh, flex_gemm, o_voxel
x = torch.randn((256, 256), device='cuda')
assert torch.isfinite(x @ x).all().item()
print({'gpu': torch.cuda.get_device_name(0), 'hip': torch.version.hip})
```

The launch script must forward arguments rather than embed a Windows path:

```bash
exec "$ENGINE/.venv/bin/python" "$ENGINE/ComfyUI/main.py" \
  --listen 127.0.0.1 --disable-auto-launch "$@"
```

- [ ] **Step 4: Run contract tests and the real build**

Run: `node --test tests/amd-wsl-scripts.test.mjs`
Expected: PASS.
Run: `wsl.exe -d AstraForge-AMD -- bash <workspace-wsl-path>/scripts/wsl/install-pixal3d-amd.sh`
Expected: all source builds finish and `wsl-ready.json` exists. On a compiler/kernel error, fix the named extension and rerun; do not proceed to weights.

- [ ] **Step 5: Commit the build and probes**

Run: `git add scripts/wsl tests/amd-wsl-scripts.test.mjs && git commit -m "Add ROCm WSL build for Pixal3D"`

---

### Task 3: Select and control the WSL backend from AstraForge

**Files:**
- Create: `server/pixal3d-wsl.mjs`
- Modify: `server/pixal3d.mjs`
- Test: `tests/pixal3d-wsl.test.mjs`
- Modify: `tests/pixal3d.test.mjs` if its config fixtures require the new default field

**Interfaces:**
- `pixal3dConfig()` additionally returns `{backend: 'windows'|'wsl', wslDistro: string}`; default remains `windows`.
- `toWslPath(absoluteWindowsPath)` returns `/mnt/<drive>/<path>` and rejects relative paths.
- `pixal3dWslArgs(cfg, runtimeDir)` returns an argument array for `wsl.exe`, without shell interpolation.
- `startPixal3dRuntime()` and `stopOwnedPixal3d()` use the selected backend and preserve their existing return shape.

- [ ] **Step 1: Write failing backend tests**

```js
assert.equal(toWslPath('C:\\work\\Astra Forge'), '/mnt/c/work/Astra Forge');
assert.throws(() => toWslPath('relative/path'));
assert.equal(pixal3dConfig({env: {}, appRoot}).backend, 'windows');
const cfg = {dir: 'C:\\work\\engine', port: 8191, wslDistro: 'AstraForge-AMD'};
const args = pixal3dWslArgs(cfg, 'C:\\work\\data\\runtime');
assert.deepEqual(args.slice(0, 3), ['-d', 'AstraForge-AMD', '--']);
assert.ok(args.includes('--port') && args.includes('8191'));
assert.ok(args.every((arg) => !arg.includes('C:\\')));
```

Also test that `startPixal3dRuntime` spawns `wsl.exe` for `backend: 'wsl'`, reuses a healthy server without spawning, and that WSL stop invokes `wsl.exe -d AstraForge-AMD --terminate`, never a global WSL shutdown.

- [ ] **Step 2: Run the tests and confirm the missing implementation**

Run: `node --test tests/pixal3d-wsl.test.mjs`
Expected: FAIL on the missing module/backend.

- [ ] **Step 3: Implement path conversion and backend selection**

```js
export function toWslPath(value) {
  const match = /^([A-Za-z]):[\\/](.*)$/.exec(value);
  if (!match) throw new Error('An absolute Windows path is required');
  return `/mnt/${match[1].toLowerCase()}/${match[2].replaceAll('\\', '/')}`;
}
```

Read `ASTRA_PIXAL3D_BACKEND`, then `paths.pixal3dBackend`, defaulting to `windows`. Accept only `windows` and `wsl`. Default the distro to `AstraForge-AMD`; allow `ASTRA_PIXAL3D_WSL_DISTRO` and `paths.pixal3dWslDistro` to override it.

For WSL installation state require `wsl-ready.json`, `ComfyUI/main.py`, Trellis2 nodes and the same model files; do not require `python_embeded/python.exe`.

- [ ] **Step 4: Implement lifecycle delegation**

Spawn `wsl.exe` with an argument array ending in the project start script and explicit `--port`, `--temp-directory`, `--database-url`. Keep logs at `data/runtime/pixal3d.log`. On stop, terminate only the configured project distro; do not run `wsl --shutdown`.

- [ ] **Step 5: Run regression tests and commit**

Run: `node --test tests/pixal3d*.test.mjs`
Expected: all PASS.
Run: `git add server/pixal3d.mjs server/pixal3d-wsl.mjs tests/pixal3d*.test.mjs && git commit -m "Add WSL Pixal3D runtime backend"`

---

### Task 4: Add AMD installer components and resumable weights

**Files:**
- Create: `installer/amd/Pixal3DAmd.ps1`
- Modify: `installer/Install-AMD.ps1`
- Modify: `tests/amd-installer.test.ps1`
- Runtime only: `runtime/ComfyUI-Pixal3D-AMD/ComfyUI/models/`

**Interfaces:**
- Adds `-Only pixal3d,pixal3dw,pixal3dtest` to the AMD installer.
- `InstallAmdPixal3d` configures `pixal3dBackend='wsl'`, `pixal3dDir`, and `pixal3dWslDistro` without touching the main ComfyUI config.

- [ ] **Step 1: Add failing installer assertions**

```powershell
Assert ($amdInstaller.Contains("id='pixal3d'")) 'AMD Pixal3D engine component is required'
Assert ($amdInstaller.Contains("id='pixal3dw'")) 'AMD Pixal3D weights component is required'
Assert ($amdInstaller.Contains('Pixal3DAmd.ps1')) 'AMD WSL implementation must be separate from NVIDIA installer'
```

Run: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/amd-installer.test.ps1`
Expected: FAIL.

- [ ] **Step 2: Implement engine and model installation**

Reuse `P3WeightItems`, `DownloadChecked`, the existing manifest hashes, DINO conversion and NAF installation where backend-independent. Do not call `P3InstallEngine`, `P3Python`, CUDA FlashAttention or Windows native wheels. Call the reviewed WSL build script for `pixal3d`; install weights only after `wsl-ready.json` and the ROCm probe exist.

- [ ] **Step 3: Verify installer and resume behavior**

Run: `npm test`
Expected: 0 failures.
Run twice: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File installer/Install-AMD.ps1 -Yes -Only pixal3dw`
Expected: second run reports existing files and performs no large downloads. Verify total model sizes and SHA-256 against the manifest/Hugging Face metadata.

- [ ] **Step 4: Commit installer changes**

Run: `git add installer/Install-AMD.ps1 installer/amd/Pixal3DAmd.ps1 tests/amd-installer.test.ps1 && git commit -m "Add AMD Pixal3D installer components"`

---

### Task 5: Add resumable end-to-end verification

**Files:**
- Create: `scripts/verify_amd_pixal3d_e2e.mjs`
- Test: `tests/amd-pixal3d-e2e.test.mjs`
- Runtime only: `data/runtime/amd-pixal3d-single.glb`, `data/runtime/amd-pixal3d-multi.glb`

**Interfaces:**
- Accepts `AMD_PIXAL3D_JOB_ID` to resume polling instead of creating a duplicate generation.
- Accepts `AMD_PIXAL3D_VIEWS=single|multi`, default `single`.
- Prints JSON with job id, engine, byte counts, geometry statistics and validation result.

- [ ] **Step 1: Write failing option tests**

```js
assert.equal(parseOptions({AMD_PIXAL3D_VIEWS: 'multi'}).views, 'multi');
assert.throws(() => parseOptions({AMD_PIXAL3D_VIEWS: 'unknown'}));
assert.equal(parseOptions({AMD_PIXAL3D_JOB_ID: 'abc'}).jobId, 'abc');
```

Run: `node --test tests/amd-pixal3d-e2e.test.mjs`
Expected: FAIL because the parser does not exist.

- [ ] **Step 2: Implement the API check**

Start the studio, call `/api/runtime/pixal3d/start`, create a job with `engine: 'pixal3d'` and the studio header, poll by id, validate the GLB, download it to `data/runtime`, and assert the `glTF` magic plus existing mesh validation. For multi-view, create four deterministic concept views and pass their asset ids through the existing view-set API; do not invent a parallel job schema.

- [ ] **Step 3: Run unit and full regression tests**

Run: `node --test tests/amd-pixal3d-e2e.test.mjs && npm test`
Expected: all PASS.

- [ ] **Step 4: Commit verification tooling**

Run: `git add scripts/verify_amd_pixal3d_e2e.mjs tests/amd-pixal3d-e2e.test.mjs && git commit -m "Add AMD Pixal3D end-to-end verification"`

---

### Task 6: Run real generation and present the result

**Files:**
- Modify: `docs/superpowers/specs/2026-11-02-astraforge-pixal3d-amd-wsl-design.md`
- Runtime only: PNG, GLB and logs under `data/runtime/`

**Interfaces:**
- Produces validated one-view GLB and, if supported by the tested ROCm stack, four-view GLB.

- [ ] **Step 1: Start the studio and run one-view generation**

Run: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/Start.ps1 -NoBrowser`
Run: `node scripts/verify_amd_pixal3d_e2e.mjs`
Expected: JSON with `engine: pixal3d`, nonzero GLB bytes and `accepted: true`. If the command times out, inspect `/api/jobs` and resume using `AMD_PIXAL3D_JOB_ID`; never create a blind duplicate.

- [ ] **Step 2: Inspect logs and the mesh**

Check `data/runtime/pixal3d.log` for ROCm/HIP errors, CUDA DLL imports and CPU fallback. Parse the GLB through the existing validator and record vertices/triangles. Open the generated asset in AstraForge and capture a useful preview for the user.

- [ ] **Step 3: Run four-view generation**

Run: `AMD_PIXAL3D_VIEWS=multi node scripts/verify_amd_pixal3d_e2e.mjs`
Expected: validated GLB within 16 GB VRAM. If an upstream ROCm limitation prevents it, preserve the exact log and report the limitation separately from the successful one-view result.

- [ ] **Step 4: Stop servers and run final checks**

Run: `powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts/Stop.ps1`
Run: `npm test` and the focused Blender HIP test.
Expected: 0 failures and no project-owned server left running.

- [ ] **Step 5: Record observed versions/results and commit**

Append actual package commits, ROCm version, one-/multi-view timing, VRAM, byte counts, triangles and any limitation to the design document. Run `git diff --check`, review `git status --short`, then commit only source/tests/docs. Update the existing PR with the verified scope; do not merge it without separate user approval.
