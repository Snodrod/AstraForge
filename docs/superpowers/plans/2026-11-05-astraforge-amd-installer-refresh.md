# Native AMD Installer Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a clean-machine native AMD installer with verified ROCm downloads, bundled HIP wheels, hardware probes, and a tested `0.3.0-amd.1` Setup package.

**Architecture:** A versioned JSON manifest describes the seven official ROCm/PyTorch packages; the three unchanged HIP wheels live beside their hashes and license notices in the repository. `Pixal3DNative.ps1` verifies/downloads official packages, verifies bundled wheels, installs both groups into the isolated Python, and runs separate base and extension probes. The release builder excludes recursive installers and review artifacts.

**Tech Stack:** Windows PowerShell 5.1, Python 3.12 embedded, PyTorch 2.9.1+rocm7.2.1, ROCm 7.2.1, Node.js built-in tests, existing C#/.NET Setup builder.

## Global Constraints

- Modify only `AstraForge-amd`; do not touch the unrelated dirty file in `AstraForge`.
- Native runtime only: `runtime/ComfyUI-Pixal3D-AMD-Native`; no WSL, CUDA, driver changes, network reconfiguration, or reboot.
- Do not modify `native-ready.json` or unlock production generation capabilities.
- Preserve the three HIP wheels byte-for-byte with their established SHA-256 values.
- Official packages use exact sizes and SHA-256 from the reviewed manifest; no unverified external wheel URL.
- Keep full ComfyUI/TRELLIS.2 sources and model weights outside this stage.
- Run GPU probes sequentially; no parallel generation jobs.
- Put generated deliverables under the current workspace in `delivery/astraforge-amd-native-2026-11-05`.
- Do not add `AstraForge-Setup-0.5.1.exe` or `verification/` images to commits or Setup payload.

---

### Task 1: Verified Official Downloads and Bundled HIP Wheels

**Files:**
- Create: `installer/amd/native-wheels.json`
- Create: `installer/amd/wheels/cumesh-1.0-cp312-cp312-win_amd64.whl`
- Create: `installer/amd/wheels/flex_gemm-1.0.0-cp312-cp312-win_amd64.whl`
- Create: `installer/amd/wheels/o_voxel-0.0.1-cp312-cp312-win_amd64.whl`
- Create: `installer/amd/wheels/wheels.json`
- Create: `installer/amd/licenses/*`
- Modify: `installer/amd/Pixal3DNative.ps1`
- Test: `tests/pixal3d-native-installer.test.ps1`

**Interfaces:**
- Consumes: existing `Get-AmdNativeWheelPaths()` and `Get-AmdNativeMetaPackage()` callers.
- Produces: `Test-AmdPackageFile($Entry, $Path) -> bool`, `Save-AmdOfficialPackage($Entry) -> string`, `Get-AmdExtensionWheelPaths() -> string[]`; all three verify size and SHA-256.

- [ ] **Step 1: Add failing manifest, cache, and bundle tests**

Append tests to `tests/pixal3d-native-installer.test.ps1` that load `installer/amd/native-wheels.json`, require seven official entries and three extension entries, and validate 64-character lowercase SHA-256 values. Test the bundle with:

```powershell
$extensionWheels = @(Get-AmdExtensionWheelPaths)
Assert ($extensionWheels.Count -eq 3) 'three bundled HIP wheels are required'
foreach ($wheel in $extensionWheels) { Assert (Test-Path -LiteralPath $wheel) "bundled HIP wheel missing: $wheel" }
```

For cache behavior, copy one official entry to a temporary manifest entry, compute the hash of a small local source file, and override `Invoke-WebRequest` in the test process to copy that source to `$OutFile`. Assert that `Save-AmdOfficialPackage` replaces a corrupt destination, accepts a valid destination without calling the mock, and throws when downloaded bytes do not match the manifest.

- [ ] **Step 2: Run the tests and confirm the new contract is absent**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/pixal3d-native-installer.test.ps1
```

Expected: FAIL because `native-wheels.json` and `Get-AmdExtensionWheelPaths` do not exist.

- [ ] **Step 3: Add the exact package manifest and preserved wheels**

Create `installer/amd/native-wheels.json` with `schemaVersion: 1`, an `official` array, and an `extensions` array. Official URLs use `https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/<filename>` with `+` encoded as `%2B` in torch URLs. Use these exact records:

```json
{
  "schemaVersion": 1,
  "official": [
    {"file":"rocm_sdk_core-7.2.1-py3-none-win_amd64.whl","size":644793492,"sha256":"f68989d48df71cbfc3cb68bf705dc37c0f56e9666feddb59a1a0f5ff7539fe1c"},
    {"file":"rocm_sdk_devel-7.2.1-py3-none-win_amd64.whl","size":232840013,"sha256":"19e6ee67e13432b7c1e8a4077df795dcb1239546ae314700c4b4d97e5b4b8f63"},
    {"file":"rocm_sdk_libraries_custom-7.2.1-py3-none-win_amd64.whl","size":489964648,"sha256":"c7fe0b0731af8896093ff69e11496830d3cb6a4aed73e895c60b7cbdc200be92"},
    {"file":"rocm-7.2.1.tar.gz","size":15940,"sha256":"9084902eaa69213a00a90784ad89e6e5fe73c702df0cc6cc3a70d777c7a6142b"},
    {"file":"torch-2.9.1+rocm7.2.1-cp312-cp312-win_amd64.whl","size":821065907,"sha256":"e88bf270163b48f7f27f7ea3db5ffb3be4ba107301933022bcb3c6ddedfeeabb"},
    {"file":"torchaudio-2.9.1+rocm7.2.1-cp312-cp312-win_amd64.whl","size":514708,"sha256":"62835605995c7812a3224733abf08e222afddb7a487c493521e613ca46e578ab"},
    {"file":"torchvision-0.24.1+rocm7.2.1-cp312-cp312-win_amd64.whl","size":1863312,"sha256":"b5c34d76a2f01fb97e1e0f224bd97fc3a58ec1ee1ef2376af2d72086e9aca34c"}
  ],
  "extensions": [
    {"file":"cumesh-1.0-cp312-cp312-win_amd64.whl","size":5649177,"sha256":"27f4600536972e51c6f3967fa370132b7d0781f1a64be3c67f91ae0af8f1614b"},
    {"file":"flex_gemm-1.0.0-cp312-cp312-win_amd64.whl","size":294337,"sha256":"ef65af07c3f34c767ba111d79407b9de4ba1d6137669fc0cd9482bce62c1d4c8"},
    {"file":"o_voxel-0.0.1-cp312-cp312-win_amd64.whl","size":267263,"sha256":"7a5a36de76e121082dbf3661e6fa3852da5ac6f1a56ce89d8efe2c04884fa0ae"}
  ]
}
```

Immediately after writing the JSON, add and persist a direct `url` property for every official record with this exact PowerShell loop; the installer consumes only these stored URLs:

```powershell
$manifestPath = 'installer/amd/native-wheels.json'
$manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
$base = 'https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1/'
foreach ($entry in $manifest.official) {
  $url = $base + $entry.file.Replace('+', '%2B')
  $entry | Add-Member -NotePropertyName url -NotePropertyValue $url
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $manifestPath -Encoding UTF8
```

Copy the three wheels from `afamd/wheels`, sanitize `afamd/wheels/wheels.json` by removing the machine-local `rocm` path, and copy the license texts from `delivery/astraforge-pixal3d-amd-hip-2026-11-04/licenses` to `installer/amd/licenses`.

- [ ] **Step 4: Implement verified cache and bundled-wheel functions**

In `Pixal3DNative.ps1`, load the new manifest and set `$NativeExtensionWheelDir = Join-Path $ProjectRoot 'installer\amd\wheels'`. Implement:

```powershell
function Test-AmdPackageFile($Entry, [string]$Path) {
  if (!(Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
  $item = Get-Item -LiteralPath $Path
  if ($item.Length -ne [long]$Entry.size) { return $false }
  return ((Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLowerInvariant() -eq $Entry.sha256.ToLowerInvariant())
}

function Save-AmdOfficialPackage($Entry) {
  New-Item -ItemType Directory -Force -Path $NativeWheelDir | Out-Null
  $destination = Join-Path $NativeWheelDir $Entry.file
  if (Test-AmdPackageFile $Entry $destination) { return $destination }
  $partial = $destination + '.part'
  Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
  Invoke-WebRequest -Uri $Entry.url -OutFile $partial
  if (!(Test-AmdPackageFile $Entry $partial)) {
    Remove-Item -LiteralPath $partial -Force -ErrorAction SilentlyContinue
    throw "Downloaded AMD package failed size or SHA-256 validation: $($Entry.file)"
  }
  Move-Item -LiteralPath $partial -Destination $destination -Force
  return $destination
}

function Get-AmdExtensionWheelPaths() {
  $paths = @($NativeWheelManifest.extensions | ForEach-Object {
    $path = Join-Path $NativeExtensionWheelDir $_.file
    if (!(Test-AmdPackageFile $_ $path)) { throw "Bundled HIP wheel failed size or SHA-256 validation: $path" }
    $path
  })
  if ($paths.Count -ne 3) { throw 'Exactly three bundled HIP wheels are required.' }
  return $paths
}
```

Make `Get-AmdNativeWheelPaths` call `Save-AmdOfficialPackage` for the six `.whl` official entries and `Get-AmdNativeMetaPackage` call it for `rocm-7.2.1.tar.gz`. Reject names containing `cu128` or `cuda` as before.

- [ ] **Step 5: Verify hashes and run focused tests**

Run:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File tests/pixal3d-native-installer.test.ps1
node --test tests/pixal3d-native-installer.test.mjs
```

Expected: `Native AMD installer unit tests passed` and one Node test passing.

- [ ] **Step 6: Commit the verified package source**

```bash
git add installer/amd/native-wheels.json installer/amd/wheels installer/amd/licenses installer/amd/Pixal3DNative.ps1 tests/pixal3d-native-installer.test.ps1 docs/superpowers/plans/2026-11-05-astraforge-amd-installer-refresh.md
git commit -m "Add verified native AMD wheel installation"
```

---

### Task 2: Install and Probe HIP Extensions Without Unlocking Generation

**Files:**
- Modify: `installer/amd/Pixal3DNative.ps1`
- Modify: `tests/pixal3d-native-installer.test.ps1`
- Modify: `scripts/native/README.md`

**Interfaces:**
- Consumes: `Get-AmdExtensionWheelPaths()` from Task 1 and existing `scripts/native/probe_trellis2_extensions.py --output <json>`.
- Produces: `Invoke-NativeExtensionProbe() -> void` and `native-extensions-ready.json` with report path/hash; no changes to `native-ready.json`.

- [ ] **Step 1: Add failing installer-flow assertions**

Add assertions that the installer installs `@('install', '--no-index', '--no-deps') + $extensionWheels`, invokes `probe_trellis2_extensions.py` with `--output`, writes `native-extensions-ready.json`, checks `shapeReady`, and never writes `native-ready.json`. Assert that the existing base early-return no longer skips extension installation.

- [ ] **Step 2: Run the focused test and confirm failure**

Run the PowerShell test from Task 1 Step 2. Expected: FAIL on the missing extension probe/marker flow.

- [ ] **Step 3: Implement the extension probe and marker**

Add:

```powershell
$ExtensionReportPath = Join-Path $ProjectRoot 'data\runtime\diagnostics\native-amd\extensions-installer.json'
function Invoke-NativeExtensionProbe {
  $python = Join-Path $RuntimeRoot 'python_embeded\python.exe'
  $probe = Join-Path $NativeToolsDir 'probe_trellis2_extensions.py'
  & $python -I $probe --output $ExtensionReportPath
  if ($LASTEXITCODE -ne 0) { throw "Native AMD extension probe exited with code $LASTEXITCODE" }
  $report = Get-Content -LiteralPath $ExtensionReportPath -Raw -Encoding UTF8 | ConvertFrom-Json
  if (!$report.shapeReady) { throw "Native AMD extensions are not ready. See $ExtensionReportPath" }
}
```

After base installation/probe succeeds, install the three bundled wheels with `--no-index --no-deps`, call `Invoke-NativeExtensionProbe`, then write `native-extensions-ready.json` containing `schemaVersion`, `runtime`, `report`, `reportSha256`, `shapeReady: true`, and UTC `createdAt`. Remove the early `return` that currently skips this stage when `native-base-ready.json` already exists. Keep `ProbeOnly` and `DryRun` non-mutating.

- [ ] **Step 4: Run installer and regression tests**

Run:

```powershell
node --test tests/pixal3d-native-installer.test.mjs tests/amd-installer.test.mjs
runtime/ComfyUI-Pixal3D-AMD-Native/python_embeded/python.exe -I scripts/native/test_native_tools.py
```

Expected: all selected tests pass; no runtime generation flags are changed.

- [ ] **Step 5: Document and commit**

Update `scripts/native/README.md` with official download behavior, bundled wheels, both report paths, and the fact that full sources/weights and `native-ready.json` remain separate. Commit only the installer, tests, and README:

```bash
git add installer/amd/Pixal3DNative.ps1 tests/pixal3d-native-installer.test.ps1 scripts/native/README.md
git commit -m "Probe bundled native AMD extensions"
```

---

### Task 3: Build a Clean `0.3.0-amd.1` Setup and Wheel Archive

**Files:**
- Modify: `package.json`
- Modify: `package-lock.json`
- Modify: `installer/build-release.ps1`
- Test: `tests/pixal3d-native-installer.test.ps1`
- Create: `delivery/astraforge-amd-native-2026-11-05/AstraForge-Setup-0.3.0-amd.1.exe`
- Create: `delivery/astraforge-amd-native-2026-11-05/AstraForge-Pixal3D-AMD-HIP-Wheels.zip`
- Create: `delivery/astraforge-amd-native-2026-11-05/SHA256SUMS.txt`

**Interfaces:**
- Consumes: verified files from Tasks 1–2 and the existing C# Setup builder.
- Produces: installable Setup and redistributable wheel archive with matching SHA-256 records.

- [ ] **Step 1: Add failing release-layout assertions**

Assert that `build-release.ps1` excludes `AstraForge-Setup-*.exe` and `verification`, and that `package.json` plus the root package lock version are `0.3.0-amd.1`.

- [ ] **Step 2: Confirm the version/layout tests fail**

Run the PowerShell installer test. Expected: FAIL on the old `0.3.0` version or missing exclusion.

- [ ] **Step 3: Update version and exclusions**

Run `npm version 0.3.0-amd.1 --no-git-tag-version`. In `build-release.ps1`, add the root `verification` directory to `$skipDirs` and add `AstraForge-Setup-*.exe` to the `Copy-Tree` file exclusions. Keep `installer/amd/wheels` and `installer/amd/licenses` included.

- [ ] **Step 4: Build and inspect the application**

Run:

```powershell
npm run build
powershell.exe -NoProfile -ExecutionPolicy Bypass -File installer/build-release.ps1 -Out ../delivery/astraforge-amd-native-2026-11-05
./../delivery/astraforge-amd-native-2026-11-05/AstraForge-Setup-0.3.0-amd.1.exe /extract ../delivery/astraforge-amd-native-2026-11-05/extracted
```

Expected: build exits 0 and Setup extracts successfully. Verify the extracted package contains `installer/amd/wheels`, current LSCM/MV-Adapter patches, `dist/index.html`, and no `verification/` or `AstraForge-Setup-0.5.1.exe`.

- [ ] **Step 5: Create the wheel archive and checksums**

Copy `installer/amd/wheels` and `installer/amd/licenses` into a staging folder under `delivery/astraforge-amd-native-2026-11-05`, compress them as `AstraForge-Pixal3D-AMD-HIP-Wheels.zip`, remove the staging folder, and generate `SHA256SUMS.txt` for Setup and ZIP. Verify the three archived wheel hashes against `native-wheels.json`.

- [ ] **Step 6: Run full non-GPU tests and commit**

Run `npm test` and the focused Python tests from `scripts/native/README.md`. Expected: zero failures. Commit only version/build source and tests, not the generated delivery folder:

```bash
git add package.json package-lock.json installer/build-release.ps1 tests/pixal3d-native-installer.test.ps1
git commit -m "Build clean native AMD release package"
```

---

### Task 4: RX 9070 XT Acceptance and Final Documentation

**Files:**
- Modify: `docs/forge/pixal3d-native-amd.md`
- Modify: `scripts/native/README.md`
- Create: `delivery/astraforge-amd-native-2026-11-05/VERIFICATION.md`

**Interfaces:**
- Consumes: Tasks 1–3 artifacts and existing native runtime on RX 9070 XT.
- Produces: measured base/extension readiness evidence and final documented limitations.

- [ ] **Step 1: Run the native installer and hardware probes**

Run sequentially:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File installer/amd/Pixal3DNative.ps1 -ForceProbe
```

Expected: exit 0; `base-probe.json` has `nativeReady: true`; `extensions-installer.json` has `shapeReady: true`; both marker files exist. Inspect every mandatory check. If any check fails, stop and report the exact operation; do not retry generation or change drivers.

- [ ] **Step 2: Verify packaged artifact hashes and unchanged production gates**

Run `Get-FileHash` for the three source wheels and their extracted Setup copies, compare with `native-wheels.json`, and confirm with `git status` and the runtime files that `native-ready.json` was not changed by the installer.

- [ ] **Step 3: Write measured documentation**

Record actual package size, SHA-256, probe results, GPU/Torch/HIP versions, and remaining limits in `docs/forge/pixal3d-native-amd.md`, `scripts/native/README.md`, and `delivery/astraforge-amd-native-2026-11-05/VERIFICATION.md`. State explicitly that this is installation/extension readiness, not a new generation capability.

- [ ] **Step 4: Final verification, commit, and push**

Run `git diff --check`, confirm no delivery binaries, old Setup, runtime files, or review images are staged, then commit and push:

```bash
git add docs/forge/pixal3d-native-amd.md scripts/native/README.md
git commit -m "Document verified native AMD installer"
git push fork amd-rocm
```

Verify the remote `amd-rocm` hash equals local `HEAD` and report the Setup/ZIP paths and hashes.
