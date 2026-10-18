# AstraForge: Pixal3D (TRELLIS.2). A separate portable ComfyUI on port 8191, shape only (no textures).
# The file is dot-sourced from installer\Install.ps1 and uses its helpers: Say/Ok/Miss/Opt, Download, Unzip,
# Run, Native, Gb, Mb, FileOk, DownloadChecked, JsonUrl, HfTree, FreeOn and the variables $Root, $Rt, $Dl, $Cfg.
# Reproduces a verified installation of ComfyUI-Pixal3D; all versions, links and checksums live
# in manifest.json, the PyPI packages are pinned in requirements-lock.txt.
# Official sources only: GitHub, Hugging Face, PyPI, download.pytorch.org, python.org.
# No third-party mirrors are used. DINOv3 is taken only from the official timm copy.

$P3Src = $PSScriptRoot
$P3Man = Get-Content -LiteralPath (Join-Path $P3Src 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
# estimates: download and disk space for the engine (Python, PyTorch, packages, ComfyUI, nodes)
$P3EngineDownload = [long]5.5e9
$P3EngineDisk = [long]12e9
$P3Port = 8191
$P3Required = @('torch-2.8.0+cu128.dist-info', 'flash_attn-2.8.3.dist-info', 'utils3d-1.7.dist-info', 'cumesh-*.dist-info',
  'flex_gemm-*.dist-info', 'o_voxel-*.dist-info', 'natten-*.dist-info', 'nvdiffrast-*.dist-info',
  'triton_windows-*.dist-info', 'timm-1.0.30.dist-info', 'transformers-4.57.6.dist-info')

# ---------------------------------------------------------------- finding and checking an existing installation
function P3Candidates() {
  # an explicitly given folder (environment variable) is the only candidate; otherwise: config\paths.json, the default folder, C:\AstraForge
  if ($env:ASTRA_PIXAL3D_DIR) { return @($env:ASTRA_PIXAL3D_DIR) }
  $c = @($Cfg.pixal3dDir, (Join-Path $Rt 'ComfyUI-Pixal3D'), 'C:\AstraForge\ComfyUI-Pixal3D') | Where-Object { $_ }
  return @($c)
}

function P3WeightItems($dir) {
  # weight files that must be in place: path, size, sha256 (if known), where to download from
  $md = Join-Path $dir 'ComfyUI\models'
  $items = @()
  foreach ($f in $P3Man.pixal3d.files) {
    $items += @{ path = (Join-Path $md ('TencentARC\Pixal3D\' + ($f.path -replace '/', '\'))); size = [long]$f.size; sha = $f.sha256
      url = ('https://huggingface.co/' + $P3Man.pixal3d.repo + '/resolve/main/' + $f.path); repo = $P3Man.pixal3d.repo; rel = $f.path }
  }
  foreach ($f in $P3Man.moge.files) {
    $items += @{ path = (Join-Path $md ('Ruicheng\moge-2-vitl\' + ($f.path -replace '/', '\'))); size = [long]$f.size; sha = $f.sha256
      url = ('https://huggingface.co/' + $P3Man.moge.repo + '/resolve/main/' + $f.path); repo = $P3Man.moge.repo; rel = $f.path }
  }
  return $items
}

function P3Probe($dir) {
  $r = @{ dir = $dir }
  $r.exists = Test-Path -LiteralPath $dir
  $r.python = Test-Path -LiteralPath (Join-Path $dir 'python_embeded\python.exe')
  $r.comfy = Test-Path -LiteralPath (Join-Path $dir 'ComfyUI\main.py')
  $nodes = Join-Path $dir 'ComfyUI\custom_nodes\ComfyUI-Trellis2\nodes.py'
  $r.node = Test-Path -LiteralPath $nodes
  $r.patched = $r.node -and [bool](Select-String -LiteralPath $nodes -SimpleMatch 'ASTRAFORGE PATCH' -Quiet)
  $sp = Join-Path $dir 'python_embeded\Lib\site-packages'
  $r.libs = $r.python
  if ($r.python) { foreach ($p in $P3Required) { if (!(Get-ChildItem -LiteralPath $sp -Filter $p -Directory -ErrorAction SilentlyContinue | Select-Object -First 1)) { $r.libs = $false; break } } }
  $r.engine = $r.python -and $r.comfy -and $r.node -and $r.patched -and $r.libs
  # weights: what is missing is counted in bytes (by the sizes from manifest.json; checksums are verified at download time)
  $missing = @(); $bytes = 0L; $total = 0L
  foreach ($it in (P3WeightItems $dir)) { $total += $it.size; if (!(FileOk $it.path $it.size $null)) { $missing += $it.rel; $bytes += $it.size } }
  $md = Join-Path $dir 'ComfyUI\models'
  $dino = $P3Man.dinov3.files | Where-Object { $_.path -eq 'model.safetensors' } | Select-Object -First 1
  $total += [long]$dino.size + [long]$P3Man.naf.checkpoint.size
  if (!(FileOk (Join-Path $md 'facebook\dinov3-vitl16-pretrain-lvd1689m\model.safetensors') 0 $null)) { $missing += 'DINOv3'; $bytes += [long]$dino.size }
  if (!(FileOk (Join-Path $dir 'cache\torch\hub\checkpoints\naf_release.pth') ([long]$P3Man.naf.checkpoint.size) $null)) { $missing += 'NAF'; $bytes += [long]$P3Man.naf.checkpoint.size }
  elseif (!(Get-ChildItem -LiteralPath (Join-Path $dir 'cache\torch\hub') -Filter 'valeoai_NAF_*' -Directory -ErrorAction SilentlyContinue | Select-Object -First 1)) { $missing += 'NAF'; $bytes += 3MB }
  $r.missing = $missing; $r.missingBytes = $bytes; $r.totalBytes = $total
  $r.weights = ($missing.Count -eq 0)
  return $r
}

function P3Scan($s) {
  # $s.gpu, $s.vram (MiB), $s.cc (compute capability, e.g. 8.9, or $null)
  $found = $null; $partial = $null
  $cands = @(P3Candidates)
  foreach ($c in $cands) {
    $p = P3Probe $c
    if ($p.engine -and $p.weights) { $found = $p; break }
    if (!$partial -and ($p.python -or $p.comfy -or $p.node)) { $partial = $p }
  }
  $p = $found
  if (!$p) { $p = $partial }
  if (!$p) { $p = P3Probe $cands[0] }
  $p.complete = [bool]($p.engine -and $p.weights)
  # whether the installation can be offered
  $p.offer = $true; $p.reason = $null; $p.warn = $null
  if (!$p.complete) {
    if (!$s.gpu) { $p.offer = $false; $p.reason = 'an NVIDIA RTX GPU is required' }
    elseif ($s.vram -lt 7900) { $p.offer = $false; $p.reason = ('video memory is ' + [math]::Round($s.vram / 1024) + ' GB, at least 8 GB is required (10+ is better)') }
    elseif ($s.cc -and ([double]::Parse($s.cc, [Globalization.CultureInfo]::InvariantCulture) -lt 8.0)) { $p.offer = $false; $p.reason = 'an NVIDIA RTX 30-series or newer GPU is required (Ampere and above)' }
    elseif (!$s.cc -and $s.gpu -notmatch 'RTX\s*(30|40|50|A\d|PRO)') { $p.offer = $false; $p.reason = 'an NVIDIA RTX 30/40/50 GPU is required' }
    elseif ($s.vram -lt 10000) { $p.warn = ('video memory is ' + [math]::Round($s.vram / 1024) + ' GB: with several views, 8 GB may not be enough, 10+ GB is recommended') }
    if ($p.offer) {
      $need = P3DiskNeed $p
      $free = FreeOn $p.dir
      # reserve: 2 GB plus 1.3 GB for the converted DINOv3 copy next to the original
      if ($null -ne $free -and $free -lt ($need + 3.3e9)) { $p.offer = $false; $p.reason = ('not enough disk space on ' + (Split-Path -Qualifier $p.dir) + ' (about ' + (Gb $need) + ' needed, ' + (Gb $free) + ' free)') }
    }
  }
  return $p
}

function P3DiskNeed($p) {
  $n = 0L
  if (!$p.engine) { $n += $P3EngineDisk }
  if (!$p.weights) { $n += $p.missingBytes }
  return [long]$n
}

# ---------------------------------------------------------------- installing the engine
$script:P3SavedEnv = $null
function P3EnvPush($dir) {
  # clean environment, as in the launch bat file; temporary files stay inside the installation
  $script:P3SavedEnv = @{}
  foreach ($k in 'CUDA_PATH', 'CUDA_HOME', 'CUDA_BIN_PATH', 'CUDNN_PATH', 'PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'CONDA_PREFIX', 'HF_HOME', 'TORCH_HOME', 'PYTHONUTF8', 'TMP', 'TEMP', 'PIP_DISABLE_PIP_VERSION_CHECK', 'PIP_NO_INPUT', 'HF_HUB_DISABLE_TELEMETRY', 'HF_HUB_DISABLE_SYMLINKS_WARNING') {
    $script:P3SavedEnv[$k] = [Environment]::GetEnvironmentVariable($k, 'Process')
  }
  $tmp = Join-Path $dir 'cache\tmp'
  New-Item -ItemType Directory -Force -Path $tmp | Out-Null
  foreach ($k in 'CUDA_PATH', 'CUDA_HOME', 'CUDA_BIN_PATH', 'CUDNN_PATH', 'PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'CONDA_PREFIX') { [Environment]::SetEnvironmentVariable($k, $null, 'Process') }
  $env:HF_HOME = Join-Path $dir 'cache\huggingface'
  $env:TORCH_HOME = Join-Path $dir 'cache\torch'
  $env:PYTHONUTF8 = '1'; $env:TMP = $tmp; $env:TEMP = $tmp
  $env:PIP_DISABLE_PIP_VERSION_CHECK = '1'; $env:PIP_NO_INPUT = '1'
  $env:HF_HUB_DISABLE_TELEMETRY = '1'; $env:HF_HUB_DISABLE_SYMLINKS_WARNING = '1'
}
function P3EnvPop() {
  if ($script:P3SavedEnv) { foreach ($k in $script:P3SavedEnv.Keys) { [Environment]::SetEnvironmentVariable($k, $script:P3SavedEnv[$k], 'Process') } }
  $script:P3SavedEnv = $null
}

function P3Repo($url, $name, $dest) {
  # GitHub archive -> folder $dest (the top-level directory of the archive is stripped)
  $zip = Join-Path $Dl ($name + '.zip')
  Download $url $zip
  $tmp = Join-Path (Split-Path $dest) ('_unpack_' + $name)
  if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
  try { Unzip $zip $tmp } catch { Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue; throw }
  $top = Get-ChildItem -LiteralPath $tmp -Directory | Select-Object -First 1
  if (!$top) { Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue; throw "The archive $name is empty or corrupt - deleted, run the installer again." }
  if (Test-Path -LiteralPath $dest) { Remove-Item -LiteralPath $dest -Recurse -Force }
  Move-Item -LiteralPath $top.FullName -Destination $dest
  Remove-Item -LiteralPath $tmp -Recurse -Force
}

function P3Python($dir) {
  $pyd = Join-Path $dir 'python_embeded'
  $py = Join-Path $pyd 'python.exe'
  $m = $P3Man.python
  if (!(Test-Path -LiteralPath $py)) {
    $zip = Join-Path $Dl ('python-' + $m.version + '-embed-amd64.zip')
    Download $m.url $zip
    Unzip $zip $pyd
    # python.exe signature: the publisher is the Python Software Foundation
    $sig = Get-AuthenticodeSignature -LiteralPath $py
    $subject = if ($sig.SignerCertificate) { $sig.SignerCertificate.Subject } else { '' }
    if (($sig.Status -in @('HashMismatch', 'NotSigned')) -or ($subject -notmatch [regex]::Escape($m.signer))) {
      Remove-Item -LiteralPath $pyd -Recurse -Force
      throw ('The python.exe signature failed verification (' + $sig.Status + ') - the archive was deleted.')
    }
    Say ("    python.exe signature verified: " + $m.signer)
  }
  # as in Easy-Install: ComfyUI on the search path, site-packages enabled
  $pth = Join-Path $pyd ('python' + ($m.version -replace '^(\d+)\.(\d+).*', '$1$2') + '._pth')
  $lines = @('../ComfyUI', ('python' + ($m.version -replace '^(\d+)\.(\d+).*', '$1$2') + '.zip'), '.', 'Lib/site-packages', 'Lib', 'Scripts', 'import site')
  [IO.File]::WriteAllText($pth, (($lines -join "`r`n") + "`r`n"), (New-Object Text.UTF8Encoding($false)))
  New-Item -ItemType Directory -Force -Path (Join-Path $pyd 'Lib\site-packages') | Out-Null
  # Python headers and libraries are needed by triton to build its own loader; taken from the official dev.msi from python.org
  if (!((Test-Path -LiteralPath (Join-Path $pyd 'include\Python.h')) -and (Test-Path -LiteralPath (Join-Path $pyd 'libs\python312.lib')))) {
    $msi = Join-Path $Dl ('python-' + $m.version + '-dev-amd64.msi')
    Download $m.devMsiUrl $msi
    $out = Join-Path $dir '_msi'
    if (Test-Path -LiteralPath $out) { Remove-Item -LiteralPath $out -Recurse -Force }
    $pr = Start-Process -FilePath 'msiexec.exe' -ArgumentList ('/a "' + $msi + '" /qn TARGETDIR="' + $out + '"') -Wait -PassThru
    if ($pr.ExitCode -ne 0 -or !(Test-Path -LiteralPath (Join-Path $out 'include\Python.h'))) { throw ('Could not unpack the Python headers from dev.msi (code ' + $pr.ExitCode + ').') }
    Copy-Item -LiteralPath (Join-Path $out 'include') -Destination $pyd -Recurse -Force
    Copy-Item -LiteralPath (Join-Path $out 'libs') -Destination $pyd -Recurse -Force
    Remove-Item -LiteralPath $out -Recurse -Force
  }
  # pip: a wheel from PyPI, the sha256 from the PyPI response is checked
  if (!(Test-Path -LiteralPath (Join-Path $pyd 'Lib\site-packages\pip'))) {
    $pv = $m.pip
    $j = JsonUrl "https://pypi.org/pypi/pip/$pv/json"
    $w = @($j.urls | Where-Object { $_.packagetype -eq 'bdist_wheel' })[0]
    $whl = Join-Path $Dl $w.filename
    DownloadChecked $w.url $whl ([long]$w.size) $w.digests.sha256
    Run $py @('-I', '-c', "import sys; sys.path.insert(0, r'$whl'); from pip._internal.cli.main import main; sys.exit(main(['install', '--no-index', '--find-links', r'$Dl', '--no-warn-script-location', 'pip==$pv']))") ('installing pip ' + $pv)
  }
}

function P3Pip($dir, [string[]]$argv, $what) {
  $py = Join-Path $dir 'python_embeded\python.exe'
  Run $py (@('-I', '-m', 'pip') + $argv + @('--no-warn-script-location', '--no-cache-dir')) $what
}

function P3Dist($dir, $pattern) {
  return [bool](Get-ChildItem -LiteralPath (Join-Path $dir 'python_embeded\Lib\site-packages') -Filter $pattern -Directory -ErrorAction SilentlyContinue | Select-Object -First 1)
}

function P3Natten($gpuCc) {
  # Blackwell GPUs (RTX 50, compute capability 12+) need a different natten wheel
  $bw = $false
  if ($gpuCc) { try { $bw = ([double]::Parse($gpuCc, [Globalization.CultureInfo]::InvariantCulture) -ge 12.0) } catch {} }
  return $bw
}

function P3InstallEngine($dir, $cc) {
  $m = $P3Man
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  P3EnvPush $dir
  try {
    $py = Join-Path $dir 'python_embeded\python.exe'
    Say '  Python 3.12 (portable) and pip'
    P3Python $dir
    Say '  ComfyUI (pinned version)'
    if (!(Test-Path -LiteralPath (Join-Path $dir 'ComfyUI\main.py'))) { P3Repo $m.comfy.url ('ComfyUI-' + $m.comfy.commit.Substring(0, 8)) (Join-Path $dir 'ComfyUI') }
    Say '  ComfyUI-Trellis2 nodes (visualbruno, MIT) together with prebuilt Windows wheels'
    $node = Join-Path $dir 'ComfyUI\custom_nodes\ComfyUI-Trellis2'
    if (!(Test-Path -LiteralPath (Join-Path $node 'nodes.py'))) { P3Repo $m.trellis2.url ('ComfyUI-Trellis2-' + $m.trellis2.commit.Substring(0, 8)) $node }
    if (!(P3Dist $dir 'torch-2.8.0+cu128.dist-info')) {
      P3Pip $dir (@('install') + @($m.torch.packages) + @('--index-url', $m.torch.index)) 'PyTorch 2.8.0 + CUDA 12.8 (download.pytorch.org, about 3.5 GB)'
    }
    P3Pip $dir @('install', '--no-deps', '-r', (Join-Path $P3Src 'requirements-lock.txt')) 'libraries from PyPI (pinned versions, about 1.3 GB)'
    # FlashAttention 2.8.3 (kingbri1 wheel on GitHub, sha256 is checked)
    if (!(P3Dist $dir 'flash_attn-2.8.3.dist-info')) {
      $fa = Join-Path $Dl $m.flashAttn.file
      DownloadChecked $m.flashAttn.url $fa ([long]$m.flashAttn.size) $m.flashAttn.sha256
      P3Pip $dir @('install', '--no-deps', $fa) 'FlashAttention 2.8.3'
    }
    # node wheels (cumesh, flex_gemm, o_voxel, natten, nvdiffrast, nvdiffrec_render); the sha256 of each is checked
    $wd = Join-Path $node ($m.trellis2.wheelDir -replace '/', '\')
    $list = @($m.trellis2.wheels | ForEach-Object { @{ file = $_.file; sha = $_.sha256; dir = $wd } })
    if (P3Natten $cc) { $list += @{ file = $m.trellis2.natten.file; sha = $m.trellis2.natten.blackwellSha256; dir = (Join-Path $wd $m.trellis2.natten.blackwellDir) } }
    else { $list += @{ file = $m.trellis2.natten.file; sha = $m.trellis2.natten.sha256; dir = $wd } }
    foreach ($w in $list) {
      $f = Join-Path $w.dir $w.file
      if (!(Test-Path -LiteralPath $f)) { throw ('The node archive has no wheel ' + $w.file) }
      if ((Get-FileHash -LiteralPath $f -Algorithm SHA256).Hash -ne $w.sha) { throw ('The checksum of the wheel ' + $w.file + ' does not match the verified one.') }
      $dn = ($w.file -split '-')[0]
      if (!(P3Dist $dir ($dn + '-*.dist-info'))) { P3Pip $dir @('install', '--no-deps', $f) ('wheel ' + $w.file) }
    }
    # utils3d from GitHub EasternJournalist/utils3d (v1.7), not the package of the same name on PyPI
    if (!(P3Dist $dir ('utils3d-' + $m.utils3d.version + '.dist-info'))) {
      $uz = Join-Path $Dl ('utils3d-' + $m.utils3d.commit.Substring(0, 8) + '.zip')
      Download $m.utils3d.url $uz
      # without build isolation: in portable Python the PYTHONPATH variable is ignored, the build takes setuptools from site-packages (already in the list above)
      P3Pip $dir @('install', '--no-deps', '--no-build-isolation', $uz) 'utils3d 1.7 (GitHub EasternJournalist/utils3d)'
    }
    Say '  node patch: automatic DINOv3 download from a third-party mirror disabled'
    Run $py @('-I', (Join-Path $P3Src 'patch_trellis2.py'), $node) 'patching nodes.py (ASTRAFORGE PATCH)'
    P3Extras $dir
    # utils3d is installed without dependencies (as in the verified installation), so pip check is expected to complain about moderngl: MoGe does not need it
    Native { & $py -I -m pip check 2>&1 | ForEach-Object { Say ('    pip check: ' + $_) } }
  } finally { P3EnvPop }
  $Cfg.pixal3dDir = $dir; SaveCfg
}

function P3Extras($dir) {
  # own helpers: video memory probe, tools, launch bat file, installation description
  foreach ($d in 'tools', 'logs', 'cache\huggingface', 'cache\torch\hub\checkpoints') { New-Item -ItemType Directory -Force -Path (Join-Path $dir $d) | Out-Null }
  New-Item -ItemType Directory -Force -Path (Join-Path $dir 'ComfyUI\custom_nodes') | Out-Null
  Copy-Item -LiteralPath (Join-Path $P3Src 'af_vram_probe.py') -Destination (Join-Path $dir 'ComfyUI\custom_nodes\af_vram_probe.py') -Force
  foreach ($f in 'dinov3_timm_to_hf.py', 'normalize_seed_mesh.py', 'smoke_test.py', 'patch_trellis2.py') { Copy-Item -LiteralPath (Join-Path $P3Src $f) -Destination (Join-Path $dir ('tools\' + $f)) -Force }
  Copy-Item -LiteralPath (Join-Path $P3Src 'Start ComfyUI (Pixal3D, 8191).bat') -Destination (Join-Path $dir 'Start ComfyUI (Pixal3D, 8191).bat') -Force
  # the node writes this json itself on first start, but does not create the folder
  $rd = Join-Path $dir 'ComfyUI\models\microsoft\TRELLIS.2-4B'
  New-Item -ItemType Directory -Force -Path $rd | Out-Null
  $rj = Join-Path $dir 'ComfyUI\custom_nodes\ComfyUI-Trellis2\reconviagen_pipeline.json'
  if ((Test-Path -LiteralPath $rj) -and !(Test-Path -LiteralPath (Join-Path $rd 'reconviagen_pipeline.json'))) { Copy-Item -LiteralPath $rj -Destination $rd }
  $txt = @'
# ComfyUI-Pixal3D for AstraForge

A separate portable ComfyUI with TRELLIS.2 + Pixal3D (shape only, no texturing), port 127.0.0.1:8191.
Installed by the AstraForge installer; the studio ComfyUI (port 8190) and the system Python are not touched.

Start: double-click `Start ComfyUI (Pixal3D, 8191).bat`. Stop: close the console window.
Check: `python_embeded\python.exe -I tools\smoke_test.py` (with ComfyUI running).

Versions: Python 3.12.10, ComfyUI 0.39.0 (@COMFY@), ComfyUI-Trellis2 1.0.27 (@NODE@), PyTorch 2.8.0+cu128,
FlashAttention 2.8.3, utils3d 1.7 (GitHub), other packages per the installer's `requirements-lock.txt`.
Weights: TencentARC/Pixal3D (shape: single- and multi-view), Ruicheng/moge-2-vitl, NAF (valeoai), DINOv3 ViT-L/16 from the
official timm copy (timm/vit_large_patch16_dinov3.lvd1689m), converted to the transformers format
(`tools\dinov3_timm_to_hf.py`, the comparison with the original model is recorded in `logs\dinov3_parity.json`).

Licenses: Pixal3D, TRELLIS.2 and the node wrapper, MoGe: MIT; NAF: Apache-2.0; FlashAttention: BSD-3; DINOv3: the DINOv3 license
(`ComfyUI\models\facebook\...\LICENSE.md`; royalty-free, commercial use allowed; military purposes and
violating trade restrictions are prohibited; when redistributing the weights the license text must be included).

IMPORTANT: nvdiffrast and nvdiffrec_render (NVIDIA) are distributed under a non-commercial license. They are installed only because
the Trellis2 node imports them on load; AstraForge does not call them. Do not use in production the nodes
`MeshTexturing*`, `TexSlatGenerator`, `RenderMultiViewNvdiffrast`, `UnWrapAndRasterizer`,
`PostProcessAndUnWrapAndRasterizer`, `Multi-View Texturing` and Trellis2/Pixal3D texturing.
The `meshlib` package (MeshLib, AMV Consulting; non-commercial license only) is not installed: the nodes are patched
(`patch_trellis2.py`), holes are filled by CuMesh (MIT). `pymeshlab` is GPL-3.0.

Node patch: `ComfyUI\custom_nodes\ComfyUI-Trellis2\nodes.py` is marked `ASTRAFORGE PATCH`: the automatic DINOv3 download from a
third-party mirror is replaced by an error. Peak video memory (RTX 4070, 12 GB): one view about 6.5 GB, four views about 9.7 GB.
'@
  $txt = $txt.Replace('@COMFY@', $P3Man.comfy.commit.Substring(0, 8)).Replace('@NODE@', $P3Man.trellis2.commit.Substring(0, 8))
  [IO.File]::WriteAllText((Join-Path $dir 'ASTRAFORGE_README.md'), $txt, (New-Object Text.UTF8Encoding($true)))
}

# ---------------------------------------------------------------- installing the weights
function P3InstallModels($dir) {
  $m = $P3Man
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  P3EnvPush $dir
  try {
    $py = Join-Path $dir 'python_embeded\python.exe'
    $md = Join-Path $dir 'ComfyUI\models'
    if (!(Test-Path -LiteralPath (Join-Path $dir 'tools\dinov3_timm_to_hf.py'))) { P3Extras $dir }
    $live = @{}
    foreach ($repo in $m.pixal3d.repo, $m.moge.repo) { $live[$repo] = HfTree $repo }
    Say '  Pixal3D weights (shape, single view and several views) and MoGe-2; checksums are verified against the Hugging Face file list'
    foreach ($it in (P3WeightItems $dir)) {
      $sha = $it.sha; $size = $it.size
      $lt = $live[$it.repo]
      if ($lt -and $lt.ContainsKey($it.rel)) {
        if ($lt[$it.rel].sha256) { if ($sha -and $sha -ne $lt[$it.rel].sha256) { Say ('    Hugging Face updated ' + $it.rel + ': using the current checksum') 'Yellow' }; $sha = $lt[$it.rel].sha256 }
        $size = [long]$lt[$it.rel].size
      }
      DownloadChecked $it.url $it.path $size $sha
    }
    # sparse structure decoder: the same file (same sha256) is in Pixal3D, the node expects it in microsoft\TRELLIS-image-large
    foreach ($n in 'ss_dec_conv3d_16l8_fp16.json', 'ss_dec_conv3d_16l8_fp16.safetensors') {
      $src = Join-Path $md ('TencentARC\Pixal3D\ckpts\' + $n)
      $dst = Join-Path $md ('microsoft\TRELLIS-image-large\ckpts\' + $n)
      if (!(Test-Path -LiteralPath $dst)) { New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null; Copy-Item -LiteralPath $src -Destination $dst }
    }
    New-Item -ItemType Directory -Force -Path (Join-Path $md 'microsoft\TRELLIS.2-4B') | Out-Null
    # NAF (feature upsampler): code from GitHub at the required commit + the release weights, torch looks for them in TORCH_HOME\hub
    $hub = Join-Path $dir 'cache\torch\hub'
    $naf = Join-Path $hub 'valeoai_NAF_main'
    if (!(Test-Path -LiteralPath (Join-Path $naf 'hubconf.py'))) { New-Item -ItemType Directory -Force -Path $hub | Out-Null; P3Repo $m.naf.url ('NAF-' + $m.naf.commit.Substring(0, 8)) $naf }
    $ck = $m.naf.checkpoint
    DownloadChecked $ck.url (Join-Path $hub 'checkpoints\naf_release.pth') ([long]$ck.size) $ck.sha256
    # DINOv3 ViT-L/16: official timm copy -> transformers format, parity check
    $fb = Join-Path $md 'facebook\dinov3-vitl16-pretrain-lvd1689m'
    if (!(Test-Path -LiteralPath (Join-Path $fb 'model.safetensors'))) {
      Say '  DINOv3 (official timm copy, DINOv3 license) and conversion to the transformers format'
      $td = Join-Path $md 'timm\vit_large_patch16_dinov3.lvd1689m'
      $dtree = HfTree $m.dinov3.repo
      foreach ($f in $m.dinov3.files) {
        $sha = $f.sha256; $size = [long]$f.size
        if ($dtree -and $dtree.ContainsKey($f.path)) { if ($dtree[$f.path].sha256) { $sha = $dtree[$f.path].sha256 }; $size = [long]$dtree[$f.path].size }
        DownloadChecked ('https://huggingface.co/' + $m.dinov3.repo + '/resolve/main/' + $f.path) (Join-Path $td $f.path) $size $sha
      }
      Run $py @('-I', (Join-Path $dir 'tools\dinov3_timm_to_hf.py'), '--timm-dir', $td, '--out-dir', $fb, '--report', (Join-Path $dir 'logs\dinov3_parity.json')) 'converting DINOv3 and checking parity (threshold 1e-3)'
      if (!(Test-Path -LiteralPath (Join-Path $fb 'model.safetensors'))) { throw 'The DINOv3 conversion did not create model.safetensors.' }
      # the original copy is no longer needed (the license and config stay next to it)
      Remove-Item -LiteralPath (Join-Path $td 'model.safetensors') -Force -ErrorAction SilentlyContinue
    }
  } finally { P3EnvPop }
  $Cfg.pixal3dDir = $dir; SaveCfg
}

# ---------------------------------------------------------------- smoke test
function P3Smoke($dir) {
  # starts ComfyUI on 8191, checks that the Trellis2 nodes loaded, and stops it
  $py = Join-Path $dir 'python_embeded\python.exe'
  $smoke = Join-Path $P3Src 'smoke_test.py'   # the node check does not depend on the installation folder, the current installer script is used
  $busy = $false
  try { Invoke-RestMethod ("http://127.0.0.1:$P3Port/system_stats") -TimeoutSec 3 | Out-Null; $busy = $true } catch {}
  if ($busy) {
    Say ("  port $P3Port is already taken by a running ComfyUI: checking it without stopping it")
    Run $py @('-I', $smoke, '--port', "$P3Port") 'checking the Trellis2 nodes'
    return
  }
  P3EnvPush $dir
  $proc = $null
  try {
    $outLog = Join-Path $dir 'logs\smoke_comfy.log'; $errLog = Join-Path $dir 'logs\smoke_comfy_err.log'
    New-Item -ItemType Directory -Force -Path (Split-Path $outLog) | Out-Null
    Say "  smoke start of ComfyUI on 127.0.0.1:$P3Port (the first start takes up to a couple of minutes)"
    $proc = Start-Process -FilePath $py -WorkingDirectory $dir -WindowStyle Hidden -PassThru -RedirectStandardOutput $outLog -RedirectStandardError $errLog `
      -ArgumentList @('-I', '-W', 'ignore::FutureWarning', 'ComfyUI\main.py', '--windows-standalone-build', '--listen', '127.0.0.1', '--port', "$P3Port", '--disable-auto-launch')
    $up = $false
    for ($i = 0; $i -lt 150; $i++) {
      if ($proc.HasExited) { break }
      try { Invoke-RestMethod ("http://127.0.0.1:$P3Port/system_stats") -TimeoutSec 3 | Out-Null; $up = $true; break } catch { Start-Sleep 2 }
    }
    if (!$up) {
      $tail = (Get-Content -LiteralPath $errLog -Tail 8 -ErrorAction SilentlyContinue) -join "`n"
      throw ("ComfyUI did not respond on port $P3Port. Log: $outLog, $errLog`n" + $tail)
    }
    Run $py @('-I', $smoke, '--port', "$P3Port") 'checking the Trellis2 nodes'
  } finally {
    if ($proc -and !$proc.HasExited) { Native { & taskkill.exe /PID $proc.Id /T /F 2>&1 | Out-Null } }
    P3EnvPop
  }
  Say '  smoke start stopped'
}
