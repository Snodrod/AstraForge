# AstraForge AMD/ROCm Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a separate AMD installer and native ROCm/HIP support for the editor, Cycles, SDXL and Hunyuan3D on Radeon RX 9070 XT.

**Architecture:** Reuse the existing installer helpers by dot-sourcing `installer/Install.ps1`, while keeping AMD package selection and the AMD wizard in separate files. Keep NVIDIA installation intact, add robust NVIDIA CSV parsing, use official AMD ROCm 7.2.1 wheels, and add HIP to the existing Cycles backend selection.

**Tech Stack:** Windows PowerShell 5.1, CMD, Python 3.12, PyTorch 2.9.1+rocm7.2.1, Blender 5.0.1/Cycles HIP, Node.js `node:test`.

## Global Constraints

- Target OS is Windows 11 x64; target GPU is AMD Radeon RX 9070 XT (`gfx1201`, 16 GB VRAM).
- Pixal3D/TRELLIS.2 is excluded from this stage and must not be installed by the AMD wizard.
- Use native ROCm/HIP; do not use ZLUDA, DirectML or WSL.
- Use Python 3.12 and official AMD ROCm 7.2.1 packages from `repo.radeon.com`.
- Preserve the existing NVIDIA path and CPU fallback.
- Install application components inside the repository workspace.
- Do not run `git commit` or other Git-mutating commands without an explicit user request.
- Do not claim hardware validation unless the corresponding command has actually passed on the RX 9070 XT.

---

### Task 1: Robust GPU parsing and testable AMD package selection

**Files:**
- Modify: `installer/Install.ps1:53-60,141-153`
- Create: `installer/amd/AmdCore.ps1`
- Create: `tests/amd-installer.test.ps1`
- Create: `tests/amd-installer.test.mjs`

**Interfaces:**
- Produces: `ConvertFrom-NvidiaSmiCsv($line)` returns `@{gpu;vram}` or `$null`.
- Produces: `Select-AmdGpu([object[]]$Adapters)` returns the selected adapter object or `$null`.
- Produces: `Get-AmdRocmPackageUrls()` and `Get-AmdTorchPackageUrls()` return string arrays of official wheel URLs.
- Produces: `Assert-AmdTorch($Python, $ExpectedName)` throws on a non-ROCm build, unavailable GPU or wrong device.

- [ ] **Step 1: Write failing PowerShell tests and their Node wrapper**

Create `tests/amd-installer.test.ps1`:

```powershell
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\installer\Install.ps1')
. (Join-Path $PSScriptRoot '..\installer\amd\AmdCore.ps1')
function Assert($condition, $message) { if (!$condition) { throw $message } }
Assert ((ConvertFrom-NvidiaSmiCsv $null) -eq $null) 'null NVML output must be ignored'
Assert ((ConvertFrom-NvidiaSmiCsv 'Failed to initialize NVML') -eq $null) 'NVML error text must be ignored'
$nv = ConvertFrom-NvidiaSmiCsv 'NVIDIA GeForce RTX 4070, 12282'
Assert ($nv.gpu -eq 'NVIDIA GeForce RTX 4070' -and $nv.vram -eq 12282) 'valid NVIDIA CSV must parse'
$cards = @(
  [pscustomobject]@{ Name = 'AMD Radeon(TM) Graphics' },
  [pscustomobject]@{ Name = 'AMD Radeon RX 9070 XT' }
)
Assert ((Select-AmdGpu $cards).Name -eq 'AMD Radeon RX 9070 XT') 'discrete RX 9070 XT must beat the iGPU'
Assert ((Select-AmdGpu @($cards[0])) -eq $null) 'unsupported iGPU must not be selected'
Assert ((Get-AmdRocmPackageUrls).Count -eq 4) 'four ROCm runtime packages are required'
Assert ((Get-AmdTorchPackageUrls).Count -eq 3) 'three PyTorch packages are required'
Assert ((Get-AmdTorchPackageUrls() -notmatch 'rocm-rel-7.2.1').Count -eq 0) 'all wheels must be pinned to ROCm 7.2.1'
Assert ((Get-AmdTorchPackageUrls() -match 'cu128|download.pytorch.org').Count -eq 0) 'AMD profile must not contain CUDA wheels'
Write-Host 'AMD installer unit tests passed'
```

Create `tests/amd-installer.test.mjs`:

```js
import {test} from 'node:test';
import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {resolve} from 'node:path';

test('AMD installer PowerShell unit tests', () => {
  const script = resolve('tests/amd-installer.test.ps1');
  const run = spawnSync('powershell.exe', ['-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', script], {encoding: 'utf8'});
  assert.equal(run.status, 0, `${run.stdout}\n${run.stderr}`);
  assert.match(run.stdout, /AMD installer unit tests passed/);
});
```

- [ ] **Step 2: Run the tests and verify failure**

Run: `node --test tests/amd-installer.test.mjs`
Expected: FAIL because `ConvertFrom-NvidiaSmiCsv` and `AmdCore.ps1` do not exist.

- [ ] **Step 3: Implement NVIDIA parsing and AMD core functions**

Add to `installer/Install.ps1` before `Scan()`:

```powershell
function ConvertFrom-NvidiaSmiCsv($line) {
  if ([string]::IsNullOrWhiteSpace($line)) { return $null }
  $p = ([string]$line).Split(',')
  if ($p.Count -lt 2) { return $null }
  $vram = 0
  if (![int]::TryParse($p[1].Trim(), [ref]$vram)) { return $null }
  return @{ gpu = $p[0].Trim(); vram = $vram }
}
```

Replace the fragile line in `Scan()` with:

```powershell
$line = Native { & $smi.Source --query-gpu=name,memory.total --format=csv,noheader,nounits 2>$null | Select-Object -First 1 }
$nv = ConvertFrom-NvidiaSmiCsv $line
if ($nv) { $s.gpu = $nv.gpu; $s.vram = $nv.vram }
```

Create `installer/amd/AmdCore.ps1`:

```powershell
$script:AmdRocmBase = 'https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1'
function Select-AmdGpu([object[]]$Adapters) {
  $pattern = 'AMD Radeon RX (9070 XT|9070|9060 XT|7900 XTX|7700)|AMD Radeon AI PRO R9700|AMD Radeon PRO W7900'
  return $Adapters | Where-Object { $_.Name -match $pattern } | Sort-Object { if ($_.Name -match '9070 XT') { 0 } else { 1 } } | Select-Object -First 1
}
function Get-AmdRocmPackageUrls() {
  return @(
    "$script:AmdRocmBase/rocm_sdk_core-7.2.1-py3-none-win_amd64.whl",
    "$script:AmdRocmBase/rocm_sdk_devel-7.2.1-py3-none-win_amd64.whl",
    "$script:AmdRocmBase/rocm_sdk_libraries_custom-7.2.1-py3-none-win_amd64.whl",
    "$script:AmdRocmBase/rocm-7.2.1.tar.gz"
  )
}
function Get-AmdTorchPackageUrls() {
  return @(
    "$script:AmdRocmBase/torch-2.9.1%2Brocm7.2.1-cp312-cp312-win_amd64.whl",
    "$script:AmdRocmBase/torchaudio-2.9.1%2Brocm7.2.1-cp312-cp312-win_amd64.whl",
    "$script:AmdRocmBase/torchvision-0.24.1%2Brocm7.2.1-cp312-cp312-win_amd64.whl"
  )
}
function Assert-AmdTorch($Python, $ExpectedName) {
  $code = "import torch; assert torch.version.hip, 'ROCm/HIP build required'; assert torch.cuda.is_available(), 'AMD GPU unavailable'; name=torch.cuda.get_device_name(0); print(name); assert '9070 XT' in name, 'wrong GPU: '+name"
  & $Python -c $code
  if ($LASTEXITCODE -ne 0) { throw "ROCm PyTorch validation failed for $ExpectedName" }
}
```

- [ ] **Step 4: Run focused and regression tests**

Run: `node --test tests/amd-installer.test.mjs`
Expected: PASS with `AMD installer unit tests passed`.
Run: `powershell -NoProfile -ExecutionPolicy Bypass -File installer/Install.ps1 -ScanOnly`
Expected: exit 0; broken NVML is reported as unavailable instead of crashing.

---

### Task 2: HIP backend for Cycles export

**Files:**
- Modify: `scripts/blender/steps/_export_lib.py:221-268`
- Modify: `scripts/blender/tests/test_export.py`

**Interfaces:**
- Produces: `cycles_device_kinds()` returning `("OPTIX", "CUDA", "HIP")`.
- Consumes: existing Blender `set_engine(scene, name, samples)` return contract `(engine_id, device_note)`.

- [ ] **Step 1: Add a failing Blender unit test**

Add to `scripts/blender/tests/test_export.py`:

```python
def test_cycles_device_backends_include_amd_hip():
    assert xl.cycles_device_kinds() == ("OPTIX", "CUDA", "HIP")
```

- [ ] **Step 2: Verify the test fails before implementation**

Run after Blender is installed:

```powershell
$env:ASTRA_BLENDER='<absolute path to blender.exe>'
$env:FORGE_TEST_MODULE='export'
$env:FORGE_TEST_FILTER='cycles_device_backends_include_amd_hip'
node --test tests/integration/blender.test.mjs
```

Expected: FAIL with `AttributeError` for `cycles_device_kinds`.

- [ ] **Step 3: Implement HIP selection without changing NVIDIA priority**

Add immediately before `set_engine` in `_export_lib.py`:

```python
def cycles_device_kinds():
    return ("OPTIX", "CUDA", "HIP")
```

In the Cycles branch replace `for kind in ("OPTIX", "CUDA"):` with:

```python
for kind in cycles_device_kinds():
```

- [ ] **Step 4: Run the focused test and later a real HIP render**

Expected: the unit test PASSes. On the target machine `set_engine(bpy.context.scene, "CYCLES", 1)` must return device note `hip`; otherwise record the Blender/HIP error and do not claim GPU rendering works.

---

### Task 3: Separate AMD installer and CMD entry point

**Files:**
- Create: `installer/Install-AMD.ps1`
- Create: `Install AstraForge AMD.cmd`
- Modify: `src/i18n/en.js`, `src/i18n/ru.js`

**Interfaces:**
- Consumes: existing `Download`, `Run`, `Uv`, `InstallNode`, `InstallNpm`, `InstallBlender`, `Model`, `SaveCfg`, `Shortcut`, `Scan` and `FirstExisting` helpers.
- Consumes: `Select-AmdGpu`, ROCm URL providers and `Assert-AmdTorch` from Task 1.
- Produces: AMD wizard supporting `-ScanOnly`, `-Yes`, `-DryRun`, `-Only` with component IDs `node,npm,blender,comfy,comfydeps,hy2,sdxl,u2net`.

- [ ] **Step 1: Add failing installer contract assertions**

Append to `tests/amd-installer.test.ps1` before its final success output:

```powershell
$amdInstaller = Get-Content (Join-Path $PSScriptRoot '..\installer\Install-AMD.ps1') -Raw
Assert ($amdInstaller.Contains('InstallAmdComfyDeps')) 'AMD ComfyUI installer must exist'
Assert ($amdInstaller.Contains('Get-AmdRocmPackageUrls')) 'ROCm runtime must be installed'
Assert ($amdInstaller.Contains('Assert-AmdTorch')) 'ROCm must be validated before model downloads'
Assert (!$amdInstaller.Contains('pixal3d.ps1')) 'AMD wizard must not install Pixal3D directly'
Assert (Test-Path (Join-Path $PSScriptRoot '..\Install AstraForge AMD.cmd')) 'AMD CMD entry point is required'
```

Run `node --test tests/amd-installer.test.mjs`; expected FAIL because the AMD wizard is absent.

- [ ] **Step 2: Implement the AMD dependency installer**

Create `installer/Install-AMD.ps1` with parameters, shared imports and these component functions:

```powershell
param([switch]$ScanOnly, [switch]$Yes, [switch]$DryRun, [string[]]$Only)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Install.ps1')
. (Join-Path $PSScriptRoot 'amd\AmdCore.ps1')

function InstallAmdComfyDeps($dir) {
  $uv = Uv
  $py = Join-Path $dir '.venv\Scripts\python.exe'
  if (!(Test-Path -LiteralPath $py)) {
    Push-Location $dir
    try { Run $uv @('venv', '--python', '3.12', '.venv') 'Python 3.12 for AMD ComfyUI' } finally { Pop-Location }
  }
  $hip = Native { & $py -c "import torch; print(torch.version.hip or '')" 2>$null }
  if ($LASTEXITCODE -ne 0 -or !$hip) {
    Run $uv @('pip', 'uninstall', '--python', $py, 'torch', 'torchvision', 'torchaudio') 'removing non-ROCm PyTorch'
    Run $uv (@('pip', 'install', '--python', $py, '--no-cache-dir') + (Get-AmdRocmPackageUrls)) 'AMD ROCm 7.2.1 runtime'
    Run $uv (@('pip', 'install', '--python', $py, '--no-cache-dir') + (Get-AmdTorchPackageUrls)) 'AMD ROCm PyTorch 2.9.1'
  }
  Assert-AmdTorch $py 'AMD Radeon RX 9070 XT'
  Run $uv @('pip', 'install', '--python', $py, '-r', (Join-Path $dir 'requirements.txt')) 'ComfyUI libraries'
  Run $uv @('pip', 'install', '--python', $py, 'rembg', 'onnxruntime', 'numba', 'scikit-image', 'xatlas') 'AstraForge libraries'
  Assert-AmdTorch $py 'AMD Radeon RX 9070 XT'
}

function InstallAmdComfy() {
  $zip = Join-Path $Dl "ComfyUI-$ComfyTag.zip"
  Download "https://github.com/comfyanonymous/ComfyUI/archive/refs/tags/$ComfyTag.zip" $zip
  $dir = Join-Path $Rt 'ComfyUI'
  if (!(Test-Path -LiteralPath (Join-Path $dir 'main.py'))) {
    Unzip $zip $Rt
    $src = Get-ChildItem -Path $Rt -Directory -Filter 'ComfyUI-*' | Select-Object -First 1
    Rename-Item -LiteralPath $src.FullName -NewName 'ComfyUI'
  }
  InstallAmdComfyDeps $dir
  $Cfg.comfyDir = $dir
  SaveCfg
}
```

- [ ] **Step 3: Add the AMD wizard without Pixal3D**

Append to `installer/Install-AMD.ps1`:

```powershell
if ($MyInvocation.InvocationName -eq '.') { return }
Clear-Host
Say 'AstraForge - AMD ROCm setup' 'White'
$gpu = Select-AmdGpu @(Get-CimInstance Win32_VideoController)
$s = Scan
if (!$gpu) { Say 'Supported discrete AMD GPU not found (RX 9070 XT is the target).' 'Red'; exit 1 }
Ok ('AMD GPU: ' + $gpu.Name)
Say ('Application folder: ' + $Root)
if ($ScanOnly) { Show $s; exit 0 }
$items = @(
  @{ id='node'; missing=!$s.node; run={ InstallNode; $s.node=$Cfg.node } },
  @{ id='npm'; missing=(!$s.npmDeps -or !$s.ui); run={ InstallNpm $s.node } },
  @{ id='blender'; missing=!$s.blender; run={ InstallBlender; $s.blender=$Cfg.blender } },
  @{ id='comfy'; missing=!$s.comfy; run={ InstallAmdComfy; $s.comfy=$Cfg.comfyDir } },
  @{ id='comfydeps'; missing=($s.comfy -and !$s.comfyDeps); run={ InstallAmdComfyDeps $s.comfy } },
  @{ id='hy2'; missing=!$s.hy2; run={ Model 'https://huggingface.co/tencent/Hunyuan3D-2/resolve/main/hunyuan3d-dit-v2-0/model.fp16.safetensors' 'hunyuan3d-dit-v2.safetensors' } },
  @{ id='sdxl'; missing=!$s.sdxl; run={ Model 'https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors' 'sd_xl_base_1.0.safetensors' } },
  @{ id='u2net'; missing=!$s.u2net; run={ Download 'https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx' (Join-Path $Root 'data\models\background\models\u2net\u2net.onnx') } }
)
$ids = if ($Only) { @($Only | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() }) } else { @() }
$plan = @($items | Where-Object { ($Only -and $ids -contains $_.id) -or (!$Only -and $_.missing) })
foreach ($item in $plan) {
  Title ('AMD component: ' + $item.id)
  if ($DryRun) { Say 'dry run - skipped'; continue }
  & $item.run
}
if ($s.comfy -and !$Cfg.comfyDir) { $Cfg.comfyDir=$s.comfy }
if ($s.blender -and !$Cfg.blender) { $Cfg.blender=$s.blender }
if ($s.node -and !$Cfg.node) { $Cfg.node=$s.node }
SaveCfg
if (!$DryRun) { Shortcut; Say 'AMD installation finished.' 'Green' }
```

Create `Install AstraForge AMD.cmd`:

```cmd
@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0installer\Install-AMD.ps1" %*
pause
```

Change the hardware connection copy in both locales to `Runs locally on your GPU` / `Работает локально на вашей видеокарте`.

- [ ] **Step 4: Verify installer contracts and dry-run**

Run: `node --test tests/amd-installer.test.mjs`
Expected: PASS.
Run: `powershell -NoProfile -ExecutionPolicy Bypass -File installer/Install-AMD.ps1 -ScanOnly`
Expected: exit 0 and `AMD GPU: AMD Radeon RX 9070 XT`.
Run: `powershell -NoProfile -ExecutionPolicy Bypass -File installer/Install-AMD.ps1 -DryRun -Yes`
Expected: exit 0, no downloads, no Pixal3D entries.

---

### Task 4: Install application, ROCm and Blender; validate hardware backends

**Files:**
- Create by installer: `runtime/`, `config/paths.json`, `dist/`, `node_modules/`
- Create: `scripts/verify_amd.py`

**Interfaces:**
- Produces: machine-readable JSON containing `torch_version`, `hip_version`, `gpu_name`, `cycles_device`.
- Consumes: installed `runtime/ComfyUI/.venv` and Blender from Task 3.

- [ ] **Step 1: Install Node dependencies and build before large AI downloads**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File installer/Install-AMD.ps1 -Yes -Only node,npm,blender`
Expected: exit 0; `dist/index.html` and Blender exist.

- [ ] **Step 2: Install ComfyUI and ROCm before models**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File installer/Install-AMD.ps1 -Yes -Only comfy`
Expected: exit 0; PyTorch prints `AMD Radeon RX 9070 XT` and ROCm validation passes. On failure, capture `installer/install.log` and stop before model downloads.

- [ ] **Step 3: Add and run the hardware probe**

Create `scripts/verify_amd.py`:

```python
import json
import torch

result = {
    "torch_version": torch.__version__,
    "hip_version": torch.version.hip,
    "gpu_name": torch.cuda.get_device_name(0) if torch.cuda.is_available() else None,
}
assert result["hip_version"], "ROCm/HIP PyTorch is required"
assert result["gpu_name"] and "9070 XT" in result["gpu_name"], result
print(json.dumps(result, ensure_ascii=False))
```

Run: `runtime/ComfyUI/.venv/Scripts/python.exe scripts/verify_amd.py`
Expected: JSON with non-empty `hip_version` and `gpu_name` containing `9070 XT`.

- [ ] **Step 4: Validate Cycles HIP with a small real render**

Create `scripts/blender/tests/verify_amd_hip.py`:

```python
import json
import os
import sys
import bpy

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from steps import _export_lib as xl

scene = bpy.context.scene
engine, device = xl.set_engine(scene, "CYCLES", 1)
assert device == "hip", f"HIP was not selected: {device}"
scene.render.resolution_x = 64
scene.render.resolution_y = 64
scene.render.resolution_percentage = 100
out = os.environ["ASTRA_AMD_HIP_OUT"]
scene.render.filepath = out
bpy.ops.render.render(write_still=True)
assert os.path.getsize(out) > 0, "empty HIP render"
print("AMD_HIP_OK " + json.dumps({"engine": engine, "device": device, "output": out}))
```

Run:

```powershell
$env:ASTRA_AMD_HIP_OUT='<absolute project path>\data\runtime\amd-hip-smoke.png'
& $env:ASTRA_BLENDER -b --factory-startup --python scripts/blender/tests/verify_amd_hip.py
```

Expected: Blender exit 0, `AMD_HIP_OK` and a non-empty PNG. If HIP is unavailable, leave the installation marked unverified and report the exact backend error.

---

### Task 5: Models, end-to-end generation and full regression

**Files:**
- Create by installer: `data/models/checkpoints/*.safetensors`, `data/models/background/models/u2net/u2net.onnx`
- Create: `scripts/verify_amd_e2e.mjs`
- Modify: `docs/superpowers/specs/2026-11-01-astraforge-amd-rocm-design.md` only to record verification results, not to expand scope.

**Interfaces:**
- Consumes: existing local API on `127.0.0.1:3210`, ComfyUI on `127.0.0.1:8190`, job schema and GLB validator.
- Produces: one verified SDXL PNG and one validated Hunyuan3D GLB.

- [ ] **Step 1: Download required models and verify idempotence**

Run: `powershell -NoProfile -ExecutionPolicy Bypass -File installer/Install-AMD.ps1 -Yes -Only hy2,sdxl,u2net`
Expected: all three files pass the existing non-empty download checks.
Run the same command again.
Expected: `already downloaded` for each file and no duplicate downloads.

- [ ] **Step 2: Run the complete existing automated tests**

Run: `npm test`
Expected: all existing tests plus `amd-installer.test.mjs` PASS.
Run the focused Blender export test from Task 2.
Expected: HIP backend test PASS and no NVIDIA regression.

- [ ] **Step 3: Create the end-to-end API verification script**

Create `scripts/verify_amd_e2e.mjs`:

```js
import {writeFileSync, mkdirSync} from 'node:fs';
import {resolve} from 'node:path';

const base = 'http://127.0.0.1:3210';
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
async function request(path, options = {}) {
  const response = await fetch(base + path, options);
  const body = await response.json();
  if (!response.ok) throw new Error(body.error || `${path}: ${response.status}`);
  return body;
}
async function download(path, destination) {
  const response = await fetch(base + path);
  if (!response.ok) throw new Error(`${path}: ${response.status}`);
  const bytes = Buffer.from(await response.arrayBuffer());
  writeFileSync(destination, bytes);
  return bytes;
}

for (let i = 0; i < 100; i += 1) {
  try { if ((await request('/api/health')).app === 'AstraForge') break; } catch {}
  if (i === 99) throw new Error('AstraForge did not start');
  await sleep(1000);
}
await request('/api/runtime/start', {method: 'POST'});
for (let i = 0; i < 200; i += 1) {
  const status = await request('/api/status');
  if (status.comfy.ready) break;
  if (i === 199) throw new Error('ComfyUI is not ready');
  await sleep(3000);
}
const job = await request('/api/jobs', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({
    prompt: 'a single simple red cube on a plain white background',
    quality: 'draft', shapeModel: 'default', meshLevel: 0,
    backgroundMode: 'auto', hazeFilter: 'auto', seed: 42, engine: 'hunyuan'
  })
});
let result;
for (let i = 0; i < 1200; i += 1) {
  result = (await request('/api/jobs')).find((item) => item.id === job.id);
  if (result.status === 'failed' || result.status === 'cancelled') throw new Error(result.message);
  if (result.status === 'completed') break;
  if (i === 1199) throw new Error('Generation timed out');
  await sleep(3000);
}
const validation = await request(`/api/jobs/${job.id}/validate`, {method: 'POST'});
if (!validation.accepted) throw new Error('GLB validation failed');
const out = resolve('data/runtime');
mkdirSync(out, {recursive: true});
const png = await download(`/api/assets/${result.concept.id}`, resolve(out, 'amd-e2e-concept.png'));
const glb = await download(`/api/assets/${result.asset.id}`, resolve(out, 'amd-e2e-mesh.glb'));
if (png.length < 1000 || png.subarray(1, 4).toString() !== 'PNG') throw new Error('Invalid SDXL PNG');
if (glb.length < 1000 || glb.subarray(0, 4).toString() !== 'glTF') throw new Error('Invalid Hunyuan3D GLB');
console.log(JSON.stringify({job: job.id, pngBytes: png.length, glbBytes: glb.length, validation}));
```

- [ ] **Step 4: Run SDXL → Hunyuan3D → GLB on the RX 9070 XT**

Start the application using `Start.cmd`, then run `node scripts/verify_amd_e2e.mjs`.
Expected: JSON containing the completed job ID, non-empty PNG and GLB byte counts and `accepted: true`. Inspect `data/runtime/comfy.log` for HIP/ROCm errors and do not claim success if generation fell back to CPU or produced an invalid asset.

- [ ] **Step 5: Stop test-owned processes and record evidence**

Run `Stop.cmd` so test-owned Node and ComfyUI processes do not remain running. Append to the design document the actual commands, GPU name, ROCm version, HIP render result, SDXL result, GLB validation and any unresolved external blocker. Do not mark the stage complete if any required hardware check failed.
