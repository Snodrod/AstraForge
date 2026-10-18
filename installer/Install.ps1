# AstraForge - setup wizard (Windows 10/11 x64).
# Run: double-click "Install AstraForge.cmd" in the application root.
#   -ScanOnly   only check the computer and show what is missing (downloads nothing)
#   -Yes        install everything required without asking (for reinstalls)
#   -DryRun     walk through the wizard and show the plan, without downloading or changing anything
#   -Only a,b   reinstall only these components (node, npm, blender, comfy, comfydeps, hy2, hymv, hy21,
#               sdxl, u2net, godot, clips, ollama, llm, vlm, mvadapter, pixal3d, pixal3dw, pixal3dtest, vfx), e.g. after a failure;
#               with -Only the questions about optional components are not asked
# Optional components (each with its own question, size and license; the wizard finds and skips ones already installed):
#   Pixal3D (TRELLIS.2, shape from 1-4 views, ComfyUI on port 8191, about 48 GB; folder runtime\ComfyUI-Pixal3D, key pixal3dDir in config\paths.json,
#   id pixal3d = engine, pixal3dw = weights, pixal3dtest = smoke test), MV-Adapter (views locally, id mvadapter),
#   qwen3-vl:8b (texture and animation check model via Ollama, id vlm).
# Everything is installed into the application folder (runtime\, data\models\), no administrator rights needed,
# except Ollama (its installer puts the program into the user profile). Log: installer\install.log.
param([switch]$ScanOnly, [switch]$Yes, [switch]$DryRun, [string[]]$Only)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$Root = Split-Path -Parent $PSScriptRoot
$Rt = Join-Path $Root 'runtime'
$Dl = Join-Path $PSScriptRoot 'downloads'
$Log = Join-Path $PSScriptRoot 'install.log'
$CfgFile = Join-Path $Root 'config\paths.json'
$NodeVersion = 'v22.23.3'
$ComfyTag = 'v0.33.2'
$BlenderZip = 'https://download.blender.org/release/Blender5.0/blender-5.0.1-windows-x64.zip'
$GodotZip = 'https://github.com/godotengine/godot/releases/download/4.5.1-stable/Godot_v4.5.1-stable_win64.exe.zip'
$UvZip = 'https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip'
# the system curl and tar of Windows 10/11 (tar from Git or MSYS does not understand zip and paths with "C:")
$Curl = Join-Path $env:SystemRoot 'System32\curl.exe'
if (!(Test-Path -LiteralPath $Curl)) { $Curl = 'curl.exe' }
$Tar = Join-Path $env:SystemRoot 'System32\tar.exe'
if (!(Test-Path -LiteralPath $Tar)) { $Tar = $null }

function Say($t, $c = 'Gray') { Write-Host $t -ForegroundColor $c; Add-Content -LiteralPath $Log -Value $t -Encoding UTF8 }
function Ok($t) { Say ("  [found]    " + $t) 'Green' }
function Miss($t) { Say ("  [missing]  " + $t) 'Yellow' }
function Opt($t) { Say ("  [optional] " + $t) 'DarkGray' }
function Title($t) { Write-Host ''; Say ('== ' + $t + ' ==') 'Cyan' }
function Ask($q, $def = $true, [switch]$Confirm) {
  if ($Yes) { return $def }
  if ($Only -and !$Confirm) { return $def }   # -Only: the component list is given explicitly, questions about optional ones are not needed
  $hint = if ($def) { '[Y/n]' } else { '[y/N]' }
  $a = Read-Host ("  " + $q + " " + $hint)
  if ([string]::IsNullOrWhiteSpace($a)) { return $def }
  return ($a.Trim().ToLower() -in @('y', 'yes', 'д', 'да', 'l'))   # the Russian д/да are still accepted for compatibility
}
function Gb($bytes) { '{0:N1} GB' -f ($bytes / 1e9) }
function Mb($bytes) { '{0:N0} MB' -f ($bytes / 1e6) }
# external programs write progress and warnings to stderr; in Windows PowerShell 5.1 with 'Stop' that aborts the
# script - inside Native stderr output is not fatal, the result is checked through $LASTEXITCODE
function Native([scriptblock]$b) { $ErrorActionPreference = 'Continue'; & $b }

function Download($url, $dst) {
  New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
  if (Test-Path -LiteralPath $dst) { Say ("    already downloaded: " + (Split-Path $dst -Leaf)); return }
  Say ("    downloading " + $url)
  Native { & $Curl -L --fail --retry 5 --retry-delay 3 -C - -# -o ($dst + '.part') $url }
  if ($LASTEXITCODE -ne 0) { throw "Download failed: $url (code $LASTEXITCODE). Run the installer again - the download will resume." }
  Move-Item -LiteralPath ($dst + '.part') -Destination $dst -Force
}
function Unzip($zip, $dst) {
  New-Item -ItemType Directory -Force -Path $dst | Out-Null
  if ($Tar) { Native { & $Tar -xf $zip -C $dst }; if ($LASTEXITCODE -eq 0) { return } }
  try { Expand-Archive -LiteralPath $zip -DestinationPath $dst -Force } catch { throw "Could not unpack $zip" }
}
function Run($exe, [string[]]$argv, $what) {
  Say ("    " + $what)
  Native { & $exe @argv }
  if ($LASTEXITCODE -ne 0) { throw "$what - failed (code $LASTEXITCODE)" }
}

# --- verified downloads of large files (Hugging Face etc.): size and sha256 are checked, a corrupt file is deleted ---
function FileOk($path, [long]$size = 0, $sha = $null) {
  # is there a file of the expected size (size 0: any non-empty file) and, if sha256 is given, does the hash match
  if (!(Test-Path -LiteralPath $path -PathType Leaf)) { return $false }
  $len = (Get-Item -LiteralPath $path).Length
  if ($size -gt 0) { if ($len -ne $size) { return $false } } elseif ($len -le 0) { return $false }
  if ($sha) { return ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -eq $sha) }
  return $true
}
function DownloadChecked($url, $dst, [long]$size = 0, $sha = $null) {
  $leaf = Split-Path $dst -Leaf
  if (FileOk $dst $size $null) {
    if (!$sha) { Say ("    already in place: " + $leaf); return }
    Say ("    checking checksum: " + $leaf)
    if (FileOk $dst $size $sha) { Say "    matches, no download needed"; return }
    Say ("    file $leaf does not match the checksum - downloading again") 'Yellow'
  }
  if (Test-Path -LiteralPath $dst) { Remove-Item -LiteralPath $dst -Force }
  Download $url $dst
  if ($sha) { Say ("    checking checksum: " + $leaf) }
  if (!(FileOk $dst $size $sha)) {
    Remove-Item -LiteralPath $dst -Force -ErrorAction SilentlyContinue
    throw "File $leaf was downloaded incorrectly (size or sha256 mismatch). The file was deleted - run the installer again."
  }
}
function JsonUrl($url) {
  $txt = Native { & $Curl -s -L --fail --retry 3 --max-time 60 $url 2>$null }
  if ($LASTEXITCODE -ne 0 -or !$txt) { throw "Could not fetch $url" }
  return (($txt -join "`n") | ConvertFrom-Json)
}
function HfTree($repo) {
  # file list of a Hugging Face repository: path -> size and sha256 (lfs.oid). If unavailable, returns $null and the values from manifest.json are used
  try {
    $t = Native { & $Curl -s -L --fail --retry 2 --max-time 60 ("https://huggingface.co/api/models/$repo/tree/main?recursive=true") 2>$null }
    if ($LASTEXITCODE -ne 0 -or !$t) { return $null }
    $h = @{}
    foreach ($e in (($t -join "`n") | ConvertFrom-Json)) { if ($e.type -eq 'file') { $h[$e.path] = @{ size = [long]$e.size; sha256 = $(if ($e.lfs) { $e.lfs.oid } else { $null }) } } }
    if ($h.Count -eq 0) { return $null }
    return $h
  } catch { return $null }
}
function DriveLetterOf($path, $fallback) {
  # drive letter of the drive the path lives on (a fallback for network paths)
  try { $q = Split-Path -Qualifier $path; if ($q) { return $q.TrimEnd(':') } } catch {}
  return $fallback
}
function FreeOn($path) {
  # free space on the drive the path lives on (or $null if it cannot be determined)
  try { return [long]([IO.DriveInfo]([IO.Path]::GetPathRoot([IO.Path]::GetFullPath($path)))).AvailableFreeSpace } catch { return $null }
}

# ---------------------------------------------------------------- configuration
$Cfg = @{}
if (Test-Path -LiteralPath $CfgFile) {
  (Get-Content -LiteralPath $CfgFile -Raw -Encoding UTF8 | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $Cfg[$_.Name] = $_.Value }
}
function SaveCfg() {
  New-Item -ItemType Directory -Force -Path (Split-Path $CfgFile) | Out-Null
  ($Cfg | ConvertTo-Json) | Set-Content -LiteralPath $CfgFile -Encoding UTF8
}
function FirstExisting([string[]]$paths) { foreach ($p in $paths) { if ($p -and (Test-Path -LiteralPath $p)) { return (Resolve-Path -LiteralPath $p).Path } }; return $null }

# optional components: Pixal3D (engine on port 8191) and MV-Adapter (views); the files live next to this one, in installer\pixal3d and installer\mvadapter
. (Join-Path $PSScriptRoot 'pixal3d\pixal3d.ps1')
. (Join-Path $PSScriptRoot 'mvadapter\mvadapter.ps1')
$VlmName = 'qwen3-vl:8b'
$VlmBytes = [long]6.1e9

# ---------------------------------------------------------------- computer scan
function Scan() {
  $s = @{}
  $s.gpu = $null; $s.vram = 0; $s.cc = $null
  $smi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
  if ($smi) {
    $line = Native { & $smi.Source --query-gpu=name,memory.total --format=csv,noheader,nounits 2>$null | Select-Object -First 1 }
    if ($line) { $p = $line.Split(','); $s.gpu = $p[0].Trim(); $s.vram = [int]$p[1].Trim() }
    # compute capability (8.9 on an RTX 4070): needed by Pixal3D; old drivers do not know this field - then it stays $null
    if ($s.gpu) {
      $cc = Native { & $smi.Source --query-gpu=compute_cap --format=csv,noheader 2>$null | Select-Object -First 1 }
      if ($LASTEXITCODE -eq 0 -and "$cc".Trim() -match '^\d+\.\d+$') { $s.cc = "$cc".Trim() }
    }
  }
  # Node.js >= 22.12
  $s.node = $null
  $cands = @($Cfg.node, (Join-Path $Rt 'node\node.exe'))
  $sys = Get-Command node.exe -ErrorAction SilentlyContinue
  if ($sys) { $cands += $sys.Source }
  foreach ($n in $cands) {
    if ($n -and (Test-Path -LiteralPath $n)) {
      $v = (Native { & $n -v 2>$null }) -replace 'v', ''
      $pv = $v.Split('.')
      if ([int]$pv[0] -gt 22 -or ([int]$pv[0] -eq 22 -and [int]$pv[1] -ge 12)) { $s.node = $n; $s.nodeVer = $v; break }
    }
  }
  $s.npmDeps = Test-Path -LiteralPath (Join-Path $Root 'node_modules\express')
  $s.ui = Test-Path -LiteralPath (Join-Path $Root 'dist\index.html')
  # Blender 5.x
  $bl = @($Cfg.blender, $env:ASTRA_BLENDER)
  $bl += (Get-ChildItem -Path (Join-Path $Rt 'blender') -Filter blender.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
  $bl += (Get-ChildItem -Path 'C:\Program Files\Blender Foundation' -Directory -Filter 'Blender 5*' -ErrorAction SilentlyContinue | ForEach-Object { Join-Path $_.FullName 'blender.exe' })
  $s.blender = FirstExisting $bl
  # ComfyUI (the application's own copy: .venv + main.py + Hunyuan3D nodes)
  $s.comfy = $null
  foreach ($c in @($Cfg.comfyDir, $env:ASTRA_COMFY_DIR, (Join-Path $Rt 'ComfyUI'), 'D:\ComfyUI', 'C:\ComfyUI', (Join-Path $HOME 'ComfyUI'))) {
    if ($c -and (Test-Path -LiteralPath (Join-Path $c '.venv\Scripts\python.exe')) -and (Test-Path -LiteralPath (Join-Path $c 'main.py')) -and (Test-Path -LiteralPath (Join-Path $c 'comfy_extras\nodes_hunyuan3d.py'))) { $s.comfy = (Resolve-Path -LiteralPath $c).Path; break }
  }
  $s.comfyDeps = $false
  if ($s.comfy) {
    $py = Join-Path $s.comfy '.venv\Scripts\python.exe'
    $vendored = Join-Path $Root 'data\python'   # xatlas may be shipped as a ready module next to the application
    Native { & $py -c "import sys; sys.path.insert(0, r'$vendored'); import torch, rembg, skimage, numba, onnxruntime, xatlas" 2>$null | Out-Null }
    $s.comfyDeps = ($LASTEXITCODE -eq 0)
  }
  # Godot 4.5 (optional)
  $gd = @($Cfg.godot, $env:ASTRA_GODOT)
  foreach ($d in @((Join-Path $Rt 'godot'), 'C:\Program Files\Godot', 'C:\Godot', (Join-Path $env:LOCALAPPDATA 'Programs\Godot'))) {
    $gd += (Get-ChildItem -Path $d -Filter 'Godot_v4.5*console.exe' -ErrorAction SilentlyContinue | Select-Object -ExpandProperty FullName)
  }
  $s.godot = FirstExisting $gd
  # models
  # models - in the application folder or in models\checkpoints of an existing ComfyUI (the application uses both)
  $cks = @((Join-Path $Root 'data\models\checkpoints'))
  if ($s.comfy) { $cks += Join-Path $s.comfy 'models\checkpoints' }
  $has = { param($n) [bool](FirstExisting @($cks | ForEach-Object { Join-Path $_ $n })) }
  $s.hy2 = & $has 'hunyuan3d-dit-v2.safetensors'
  $s.hy21 = & $has 'hunyuan_3d_v2.1.safetensors'
  $s.hymv = & $has 'hunyuan3d-dit-v2-mv.safetensors'
  $s.sdxl = & $has 'sd_xl_base_1.0.safetensors'
  $s.u2net = [bool](Get-ChildItem -Path (Join-Path $Root 'data\models\background') -Filter u2net.onnx -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1)
  # Ollama
  $s.ollama = FirstExisting @($Cfg.ollama, (Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'), $(if (Get-Command ollama.exe -ErrorAction SilentlyContinue) { (Get-Command ollama.exe).Source }))
  $s.llms = @()
  if ($s.ollama) { $s.llms = @(Native { & $s.ollama list 2>$null } | Select-Object -Skip 1 | ForEach-Object { ($_ -split '\s+')[0] } | Where-Object { $_ }) }
  # `ollama list` prints nothing until the Ollama server is running: additionally read the model manifests from disk
  $s.llms = @($s.llms + (OllamaModelsOnDisk) | Where-Object { $_ } | Select-Object -Unique)
  $s.vlm = $s.llms -contains $VlmName
  # Pixal3D and MV-Adapter
  $s.p3 = P3Scan $s
  $s.mv = MvScan $s
  # clip library (Quaternius / ActionForge animations, CC0)
  $s.library = FirstExisting @($Cfg.library, $env:ASTRA_FORGE_LIBRARY, 'C:\AstraForge\library')
  if (!$s.library) { $s.library = Join-Path $Root 'library' }
  $s.clips = (Get-ChildItem -Path (Join-Path $s.library 'animations\quaternius') -Filter *.fbx -ErrorAction SilentlyContinue | Measure-Object).Count
  return $s
}

function Show($s) {
  Title 'What is already on this computer'
  if ($s.gpu) { Ok ("GPU: " + $s.gpu + " (" + [math]::Round($s.vram / 1024) + " GB)") } else { Miss 'NVIDIA GPU not found - 3D generation from an image will be unavailable (the interface and Blender still work)' }
  if ($s.node) { Ok ("Node.js " + $s.nodeVer) } else { Miss 'Node.js 22.12+' }
  if ($s.npmDeps -and $s.ui) { Ok 'Application dependencies and interface' } else { Miss 'Dependencies / interface build (npm)' }
  if ($s.blender) { Ok ("Blender 5: " + $s.blender) } else { Miss 'Blender 5.0' }
  if ($s.comfy) { if ($s.comfyDeps) { Ok ("ComfyUI: " + $s.comfy) } else { Miss ("ComfyUI is present (" + $s.comfy + "), but Python libraries are missing") } } else { Miss 'ComfyUI (3D generation)' }
  if ($s.hy2) { Ok 'Hunyuan3D-2 model' } else { Miss 'Hunyuan3D-2 model (required, 4.9 GB)' }
  if ($s.u2net) { Ok 'u2net background-removal model' } else { Miss 'u2net background-removal model (176 MB)' }
  if ($s.hymv) { Ok 'Hunyuan3D-2mv model (from two views)' } else { Opt 'Hunyuan3D-2mv model (from two views, 4.9 GB)' }
  if ($s.hy21) { Ok 'Hunyuan3D-2.1 model' } else { Opt 'Hunyuan3D-2.1 model (7.4 GB)' }
  if ($s.sdxl) { Ok 'SDXL (images)' } else { Opt 'SDXL for image generation (6.9 GB)' }
  if ($s.godot) { Ok ("Godot 4.5: " + $s.godot) } else { Opt 'Godot 4.5 (validation of game packages, 77 MB)' }
  if ($s.ollama) { Ok ("Ollama, models: " + ($(if ($s.llms.Count) { $s.llms -join ', ' } else { 'none' }))) } else { Opt 'Ollama - local AI assistant (optional)' }
  if ($s.clips -gt 0) { Ok ("Quaternius animations: " + $s.clips + " clips") } else { Opt 'Quaternius / ActionForge animations, 86 clips (CC0)' }
  # MV-Adapter: views from a single image right in the studio
  $mv = $s.mv
  if ($mv.ready) { Ok 'MV-Adapter: views locally (node, weights, packages, SDXL configs)' }
  elseif ($mv.node -or $mv.weights -or $mv.config) {
    $lack = @(); if (!$mv.node) { $lack += 'node' }; if (!$mv.weights) { $lack += 'weights' }; if (!$mv.config) { $lack += 'SDXL configs' }; if (!$mv.pkgs) { $lack += 'Python packages' }
    Miss ('MV-Adapter is not fully installed, missing: ' + ($lack -join ', '))
  }
  elseif ($mv.offer) { Opt ('MV-Adapter - views from a single image, locally (3.6 GB of weights; needs SDXL)') }
  else { Opt ('MV-Adapter is unavailable: ' + $mv.reason) }
  # local model for texture and animation checks
  if ($s.vlm) { Ok ('Check model ' + $VlmName + ' (textures and animation)') } else { Opt ('Check model ' + $VlmName + ' via Ollama (' + (Gb $VlmBytes) + ', textures and animation)') }
  # Pixal3D: a separate ComfyUI on port 8191
  $p3 = $s.p3
  if ($p3.complete) { Ok ('Pixal3D (3D from 1-4 views, shape only): ' + $p3.dir) }
  elseif ($p3.exists -and ($p3.python -or $p3.comfy -or $p3.node -or $p3.missingBytes -lt $p3.totalBytes)) {
    $lack = @(); if (!$p3.engine) { $lack += 'engine' }; if (!$p3.weights) { $lack += ('weights, ' + (Gb $p3.missingBytes) + ' more') }
    Miss ('Pixal3D is not fully installed (' + $p3.dir + '), missing: ' + ($lack -join ', '))
  }
  elseif ($p3.offer) { Opt ('Pixal3D - 3D from 1-4 views, shape only (ComfyUI on port 8191), about ' + (Gb (P3DiskNeed $p3)) + ' on disk') }
  else { Opt ('Pixal3D is unavailable: ' + $p3.reason) }
}

function OllamaModelsOnDisk() {
  # Ollama model manifests: <models>\manifests\registry.ollama.ai\<namespace>\<name>\<tag>; the library namespace yields "name:tag"
  $base = if ($env:OLLAMA_MODELS) { $env:OLLAMA_MODELS } else { Join-Path $HOME '.ollama\models' }
  $mroot = Join-Path $base 'manifests\registry.ollama.ai'
  $found = @()
  if (Test-Path -LiteralPath $mroot) {
    foreach ($f in (Get-ChildItem -LiteralPath $mroot -Recurse -File -ErrorAction SilentlyContinue)) {
      $parts = $f.FullName.Substring($mroot.Length).TrimStart('\') -split '\\'
      if ($parts.Count -eq 3 -and $parts[2] -notmatch '^[0-9a-f]{64}$') { if ($parts[0] -eq 'library') { $found += ($parts[1] + ':' + $parts[2]) } else { $found += ($parts[0] + '/' + $parts[1] + ':' + $parts[2]) } }
    }
  }
  return $found
}

function RecommendLlm($vram) {
  if ($vram -ge 16000) { return @{ name = 'qwen3:14b'; bytes = 9300MB } }
  if ($vram -ge 8000) { return @{ name = 'qwen3:8b'; bytes = 5200MB } }
  return @{ name = 'qwen3:4b'; bytes = 2500MB }
}

# ---------------------------------------------------------------- installing components
function InstallNode() {
  $zip = Join-Path $Dl "node-$NodeVersion-win-x64.zip"
  Download "https://nodejs.org/dist/$NodeVersion/node-$NodeVersion-win-x64.zip" $zip
  if (Test-Path -LiteralPath (Join-Path $Rt 'node')) { Remove-Item -Recurse -Force (Join-Path $Rt 'node') }
  Unzip $zip $Rt
  Rename-Item -LiteralPath (Join-Path $Rt "node-$NodeVersion-win-x64") -NewName 'node'
  $Cfg.node = Join-Path $Rt 'node\node.exe'; SaveCfg
}
function InstallNpm($node) {
  $npm = Join-Path (Split-Path $node) 'npm.cmd'
  if (!(Test-Path -LiteralPath $npm)) { $npm = 'npm.cmd' }
  $env:PATH = (Split-Path $node) + ';' + $env:PATH
  Push-Location $Root
  try { Run $npm @('ci', '--no-audit', '--no-fund') 'installing dependencies (npm ci)'; Run $npm @('run', 'build') 'building the interface (npm run build)' } finally { Pop-Location }
}
function InstallBlender() {
  $zip = Join-Path $Dl 'blender-5.0.1-windows-x64.zip'
  Download $BlenderZip $zip
  Unzip $zip (Join-Path $Rt 'blender')
  $Cfg.blender = (Get-ChildItem -Path (Join-Path $Rt 'blender') -Filter blender.exe -Recurse | Select-Object -First 1).FullName; SaveCfg
}
function InstallGodot() {
  $zip = Join-Path $Dl 'Godot_v4.5.1-stable_win64.exe.zip'
  Download $GodotZip $zip
  Unzip $zip (Join-Path $Rt 'godot')
  $Cfg.godot = (Get-ChildItem -Path (Join-Path $Rt 'godot') -Filter '*console.exe' -Recurse | Select-Object -First 1).FullName; SaveCfg
}
function Uv() {
  $uv = Join-Path $Rt 'uv\uv.exe'
  if (!(Test-Path -LiteralPath $uv)) { $zip = Join-Path $Dl 'uv.zip'; Download $UvZip $zip; Unzip $zip (Join-Path $Rt 'uv') }
  $env:UV_PYTHON_INSTALL_DIR = Join-Path $Rt 'python'
  $env:UV_CACHE_DIR = Join-Path $Rt 'uv-cache'
  return $uv
}
function InstallComfyDeps($dir, $hasGpu) {
  $uv = Uv
  $py = Join-Path $dir '.venv\Scripts\python.exe'
  if (!(Test-Path -LiteralPath $py)) { Push-Location $dir; try { Run $uv @('venv', '--python', '3.12', '.venv') 'Python 3.12 for ComfyUI' } finally { Pop-Location } }
  $torchIndex = if ($hasGpu) { 'https://download.pytorch.org/whl/cu128' } else { 'https://download.pytorch.org/whl/cpu' }
  Native { & $py -c "import torch" 2>$null | Out-Null }
  if ($LASTEXITCODE -ne 0) { Run $uv @('pip', 'install', '--python', $py, 'torch', 'torchvision', 'torchaudio', '--index-url', $torchIndex) 'PyTorch' }
  Run $uv @('pip', 'install', '--python', $py, '-r', (Join-Path $dir 'requirements.txt')) 'ComfyUI libraries'
  Run $uv @('pip', 'install', '--python', $py, 'rembg', 'onnxruntime', 'numba', 'scikit-image', 'xatlas') 'AstraForge libraries (background removal, meshing, unwrapping)'
}
function InstallComfy($hasGpu) {
  $zip = Join-Path $Dl "ComfyUI-$ComfyTag.zip"
  Download "https://github.com/comfyanonymous/ComfyUI/archive/refs/tags/$ComfyTag.zip" $zip
  $dir = Join-Path $Rt 'ComfyUI'
  if (!(Test-Path -LiteralPath (Join-Path $dir 'main.py'))) {
    Unzip $zip $Rt
    $src = Get-ChildItem -Path $Rt -Directory -Filter 'ComfyUI-*' | Select-Object -First 1
    Rename-Item -LiteralPath $src.FullName -NewName 'ComfyUI'
  }
  InstallComfyDeps $dir $hasGpu
  $Cfg.comfyDir = $dir; SaveCfg
}
function Model($url, $name) { Download $url (Join-Path $Root ('data\models\checkpoints\' + $name)) }
function InstallOllama() {
  $exe = Join-Path $Dl 'OllamaSetup.exe'
  Download 'https://ollama.com/download/OllamaSetup.exe' $exe
  Say '    installing Ollama (its installer will open, this takes a minute)'
  Start-Process -FilePath $exe -ArgumentList '/VERYSILENT', '/NORESTART', '/SUPPRESSMSGBOXES' -Wait
  $o = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
  if (!(Test-Path -LiteralPath $o)) { throw 'Ollama was not installed - install it manually from https://ollama.com and run the installer again.' }
  $Cfg.ollama = $o; SaveCfg
  return $o
}
function EnsureOllama($ollama) {
  for ($i = 0; $i -lt 30; $i++) { try { Invoke-RestMethod 'http://127.0.0.1:11434/api/version' -TimeoutSec 2 | Out-Null; break } catch { if ($i -eq 0) { Start-Process -FilePath $ollama -ArgumentList 'serve' -WindowStyle Hidden }; Start-Sleep 2 } }
}
# helper model that does not change the assistant provider (nothing is written to data\config.json): the texture and animation check model
function PullModel($ollama, $model) {
  EnsureOllama $ollama
  Run $ollama @('pull', $model) ("downloading model " + $model)
}
function PullLlm($ollama, $model) {
  EnsureOllama $ollama
  Run $ollama @('pull', $model) ("downloading model " + $model)
  $cfgPath = Join-Path $Root 'data\config.json'
  $c = @{}
  if (Test-Path -LiteralPath $cfgPath) { (Get-Content -LiteralPath $cfgPath -Raw | ConvertFrom-Json).PSObject.Properties | ForEach-Object { $c[$_.Name] = $_.Value } }
  $c.provider = 'ollama'; $c.ollamaModel = $model; $c.ollamaUrl = 'http://127.0.0.1:11434'
  New-Item -ItemType Directory -Force -Path (Split-Path $cfgPath) | Out-Null
  ($c | ConvertTo-Json) | Set-Content -LiteralPath $cfgPath -Encoding UTF8
}
function InstallClips($blender, $library) {
  $src = Join-Path $Dl 'actionforge'
  $base = 'https://actionforge.app/assets/animation-library'
  Download "$base/manifests/library.json" (Join-Path $src 'manifests\library.json')
  $man = Get-Content -LiteralPath (Join-Path $src 'manifests\library.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  foreach ($p in $man.clips.PSObject.Properties) { $f = $p.Value.file; Download "$base/$f" (Join-Path $src ($f.Split('?')[0] -replace '/', '\')) }
  $fbx = Join-Path $Dl 'actionforge_fbx'
  Run $blender @('-b', '--factory-startup', '--python', (Join-Path $PSScriptRoot 'af_clips_to_fbx.py'), '--', $src, $fbx, '--library') 'converting clips to FBX (Blender)'
  $files = Get-Content -LiteralPath (Join-Path $fbx 'files.json') -Raw -Encoding UTF8 | ConvertFrom-Json
  $req = @{ step = 'clip_library'; inputs = @{}; params = @{ action = 'import'; files = $files; library = (Join-Path $library 'animations') }; out_dir = (Join-Path $fbx '_import') }
  New-Item -ItemType Directory -Force -Path (Join-Path $fbx '_import') | Out-Null
  ($req | ConvertTo-Json -Depth 6) | Set-Content -LiteralPath (Join-Path $fbx '_import\request.json') -Encoding UTF8
  Run $blender @('-b', '--factory-startup', '--python', (Join-Path $Root 'scripts\blender\af_run.py'), '--', 'clip_library', (Join-Path $fbx '_import\request.json')) 'importing clips into the AstraForge library'
}
function Shortcut() {
  $lnk = Join-Path ([Environment]::GetFolderPath('Desktop')) 'AstraForge.lnk'
  $s = (New-Object -ComObject WScript.Shell).CreateShortcut($lnk)
  $s.TargetPath = Join-Path $Root 'Start.cmd'; $s.WorkingDirectory = $Root
  $s.IconLocation = (Join-Path $Root 'assets\astraforge.ico') + ',0'; $s.Description = 'AstraForge - 3D character studio'; $s.WindowStyle = 7
  $s.Save()
  Say ("  desktop shortcut: " + $lnk) 'Green'
}

# ---------------------------------------------------------------- wizard
if ($MyInvocation.InvocationName -eq '.') { return }
Clear-Host
Say 'AstraForge - setup' 'White'
Say ("Application folder: " + $Root)
$drive = (Get-Item -LiteralPath $Root).PSDrive
Say ("Free space on drive " + $drive.Name + ": " + (Gb $drive.Free))
$s = Scan
Show $s
if ($ScanOnly) { exit 0 }

Title 'What needs to be installed'
$plan = New-Object System.Collections.ArrayList
# size = how much to download, extraDisk = how much more it takes on disk beyond the download (unpacking), driveLetter = the drive it is installed to
function Add($id, $what, $size, $lic, $extraDisk = 0, $driveLetter = $null) {
  if (!$driveLetter) { $driveLetter = $drive.Name }
  [void]$plan.Add(@{ id = $id; what = $what; size = [long]$size; lic = $lic; disk = ([long]$size + [long]$extraDisk); drive = $driveLetter })
}
$LicP3Engine = 'MIT (Pixal3D, TRELLIS.2); BSD-3 (PyTorch, FlashAttention)'
$LicP3Weights = 'MIT (Pixal3D, MoGe); Apache-2.0 (NAF); DINOv3 license'
# plan entries for new components named in -Only (no questions are asked then, so they are not in the plan yet)
function NewEntry($id) {
  $letter = DriveLetterOf $p3.dir $drive.Name
  switch ($id) {
    'pixal3d' { return @{ id = $id; what = 'Pixal3D: engine (Python, PyTorch, ComfyUI, Trellis2 nodes)'; size = [long]$P3EngineDownload; lic = $LicP3Engine; disk = [long]$P3EngineDisk; drive = $letter } }
    'pixal3dw' { return @{ id = $id; what = 'Pixal3D: weights (shape, MoGe-2, NAF, DINOv3)'; size = [long]$p3.missingBytes; lic = $LicP3Weights; disk = [long]($p3.missingBytes + 1.3e9); drive = $letter } }
    'mvadapter' { return @{ id = $id; what = 'MV-Adapter: node, weights, Python packages'; size = [long]$s.mv.bytes; lic = 'Apache-2.0'; disk = [long]$s.mv.bytes; drive = $drive.Name } }
    'vlm' { return @{ id = $id; what = ('model ' + $VlmName + ' (texture and animation check)'); size = [long]$VlmBytes; lic = 'Apache 2.0 (Qwen3-VL)'; disk = [long]$VlmBytes; drive = $drive.Name } }
    'vfx' { return @{ id = $id; what = 'VFX texture generators'; size = [long]330MB; lic = 'MIT / BSD-3-Clause / GPL-3.0 / free programs'; disk = [long]500MB; drive = $drive.Name } }
  }
  return $null
}
if (!$s.node) { Add 'node' "Node.js $NodeVersion (into the application folder)" 32MB 'MIT' }
if (!$s.npmDeps -or !$s.ui) { Add 'npm' 'dependencies and interface build' 250MB 'open-source licenses of npm packages' }
if (!$s.blender) { Add 'blender' 'Blender 5.0.1 (into the application folder)' 401MB 'GPL' }
if (!$s.comfy) { Add 'comfy' "ComfyUI $ComfyTag + Python 3.12 + PyTorch" 6GB 'GPL / BSD' }
elseif (!$s.comfyDeps) { Add 'comfydeps' 'missing Python libraries for ComfyUI' 1GB 'open-source licenses' }
if (!$s.hy2) { Add 'hy2' 'Hunyuan3D-2 model (3D from an image)' 4928151562 'Tencent Hunyuan Community License' }
if (!$s.u2net) { Add 'u2net' 'u2net background-removal model' 175997641 'Apache-2.0' }
foreach ($p in $plan) { Say ("  • " + $p.what + "  — " + $(if ($p.size -ge 1GB) { Gb $p.size } else { Mb $p.size })) }
if ($plan.Count -eq 0) { Say '  everything required is already installed' 'Green' }
$p3 = $s.p3

$mandatoryCount = $plan.Count
Title 'Optional'
if (!$s.hymv -and (Ask 'Hunyuan3D-2mv model - 3D from a front and a back view, 4.9 GB. Download?' $true)) { Add 'hymv' 'Hunyuan3D-2mv model' 4928151562 'Tencent Hunyuan Community License' }
if (!$s.hy21 -and (Ask 'Hunyuan3D-2.1 model - a second shape model, 7.4 GB. Download?' $false)) { Add 'hy21' 'Hunyuan3D-2.1 model' 7365943290 'Tencent Hunyuan Community License' }
if (!$s.sdxl -and (Ask 'SDXL - generate concept images right in the application, 6.9 GB. Download?' $false)) { Add 'sdxl' 'SDXL model' 6938078334 'CreativeML Open RAIL++-M' }
if (!$s.godot -and (Ask 'Godot 4.5.1 - validation of finished game packages, 77 MB. Download?' $true)) { Add 'godot' 'Godot 4.5.1' 76685794 'MIT' }
if ($s.clips -eq 0 -and (Ask 'Quaternius animations (ActionForge): 86 clips - running, swords, shields, magic, 10 MB. Download?' $true)) { Add 'clips' 'Quaternius / ActionForge animations (CC0)' 10MB 'CC0' }
# VFX texture generators for the "VFX" tab (scripts\vfx\Install-VfxTools.ps1, list in server\vfx-tools-catalog.json)
$VfxDir = if ($Cfg.vfxTools) { $Cfg.vfxTools } else { Join-Path $Rt 'vfx' }
if (!(Test-Path -LiteralPath (Join-Path $VfxDir 'apps\material-maker\material_maker.exe')) -and (Ask 'VFX texture generators: Material Maker, PixaFlux, KumoWorks, Slate, FlowMap Painter and 4 web generators, about 330 MB. Download?' $false)) { Add 'vfx' 'VFX texture generators' 330MB 'MIT / BSD-3-Clause / GPL-3.0 / free programs' }
# MV-Adapter: left, back and right views from a single front image, locally; runs on SDXL
if (!$s.mv.ready -and $s.mv.offer) {
  $needSdxl = !$s.sdxl -and !($plan | Where-Object { $_.id -eq 'sdxl' })
  Say '  MV-Adapter completes the left, back and right views from a single front image right in the studio, offline.' 'White'
  if ($s.mv.warn) { Say ('  Warning: ' + $s.mv.warn) 'Yellow' }
  $mvq = 'MV-Adapter - views locally, ' + (Gb $s.mv.bytes) + $(if ($needSdxl) { ' (SDXL, 6.9 GB, is needed too - downloaded together)' } else { '' }) + '. Install?'
  if (Ask $mvq ($s.sdxl -and !$s.mv.warn)) {
    if ($needSdxl) { Add 'sdxl' 'SDXL model (needed for MV-Adapter)' 6938078334 'CreativeML Open RAIL++-M' }
    Add 'mvadapter' 'MV-Adapter: node, weights, Python packages' $s.mv.bytes 'Apache-2.0'
  }
}
$llm = RecommendLlm $s.vram
$wantLlm = $null
if ($s.llms -notcontains $llm.name) {
  Say ("  Local AI assistant (descriptions, prompts, task breakdown) - free, works offline.") 'White'
  Say ("  The best model for your GPU is " + $llm.name + " (" + (Gb $llm.bytes) + ") via Ollama.")
  # the user already has their own models - do not push by default
  if ($s.llms.Count) { Say ("  You already have: " + ($s.llms -join ', ')) }
  if (Ask ("Install " + $(if ($s.ollama) { '' } else { 'Ollama and ' }) + "model " + $llm.name + "?") ($s.llms.Count -eq 0)) {
    if (!$s.ollama) { Add 'ollama' 'Ollama' 1578217944 'MIT' }
    Add 'llm' ("model " + $llm.name) $llm.bytes 'Apache 2.0 (Qwen3)'
    $wantLlm = $llm.name
  }
}
# local vision model for texture and animation checks (after the Ollama question: it needs the same Ollama)
if (!$s.vlm) {
  Say ('  Local check model ' + $VlmName + ' (understands images): checks textures and animation, works offline.') 'White'
  if ($s.vram -gt 0 -and $s.vram -lt 8000) { Say '  Warning: less than 8 GB of video memory, the model will run slowly.' 'Yellow' }
  $needOllama = !$s.ollama -and !($plan | Where-Object { $_.id -eq 'ollama' })
  # default is "yes" only if Ollama is already present or is being installed in this run for the assistant, and there is enough video memory
  $vlmDefault = ($s.vram -ge 8000) -and ([bool]$s.ollama -or [bool]$wantLlm)
  if (Ask ('Install ' + $(if ($needOllama) { 'Ollama and ' } else { '' }) + 'model ' + $VlmName + ' (' + (Gb $VlmBytes) + ')?') $vlmDefault) {
    if ($needOllama) { Add 'ollama' 'Ollama' 1578217944 'MIT' }
    Add 'vlm' ('model ' + $VlmName + ' (texture and animation check)') $VlmBytes 'Apache 2.0 (Qwen3-VL)'
  }
}
# Pixal3D (TRELLIS.2): the heaviest, so it comes last and defaults to "no"
if (!$p3.complete -and $p3.offer) {
  $p3Letter = DriveLetterOf $p3.dir $drive.Name
  Say '  Pixal3D (TRELLIS.2) builds the character shape from 1-4 views: geometry only, no textures. It is a separate ComfyUI on port 8191.' 'White'
  Say ('  Needs an NVIDIA RTX 30/40/50 GPU, preferably 10 GB of video memory or more. Folder: ' + $p3.dir)
  if ($p3.warn) { Say ('  Warning: ' + $p3.warn) 'Yellow' }
  if ($p3.dir.Length -gt 90) { Say '  Warning: the path is long, Windows may fail to create deeply nested files (260-character limit). Better set a short path: the ASTRA_PIXAL3D_DIR variable, for example C:\AstraForge\ComfyUI-Pixal3D.' 'Yellow' }
  $p3n = P3DiskNeed $p3
  $p3dl = $(if ($p3.engine) { 0 } else { $P3EngineDownload }) + $(if ($p3.weights) { 0 } else { $p3.missingBytes })
  if (Ask ('Install Pixal3D? Download about ' + (Gb $p3dl) + ', ' + (Gb $p3n) + ' on disk.') $false) {
    if (!$p3.engine) { Add 'pixal3d' 'Pixal3D: engine (Python, PyTorch, ComfyUI, Trellis2 nodes)' $P3EngineDownload $LicP3Engine ($P3EngineDisk - $P3EngineDownload) $p3Letter }
    if (!$p3.weights) { Add 'pixal3dw' 'Pixal3D: weights (shape, MoGe-2, NAF, DINOv3)' $p3.missingBytes $LicP3Weights 1.3e9 $p3Letter }
  }
}

if (!$Only -and $plan.Count -gt $mandatoryCount) {
  Title 'Selected optional components'
  for ($i = $mandatoryCount; $i -lt $plan.Count; $i++) { $q = $plan[$i]; Say ('  • ' + $q.what + '  — ' + $(if ($q.size -ge 1GB) { Gb $q.size } else { Mb $q.size }) + '  [' + $q.lic + ']') }
}
if ($Only) {
  $ids = $Only | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() }
  if ($ids -contains 'llm' -and !$wantLlm) { $wantLlm = $llm.name }
  $plan = @($ids | ForEach-Object { $id = $_; $hit = $plan | Where-Object { $_.id -eq $id } | Select-Object -First 1; if ($hit) { $hit } else { $e = NewEntry $id; if ($e) { $e } else { @{ id = $id; what = $id; size = [long]0; lic = ''; disk = [long]0; drive = $drive.Name } } } })
}
Title 'Total'
$dlSum = 0L; $diskBy = @{}
foreach ($q in $plan) { $dlSum += [long]$q.size; $dk = $q.drive; if (!$diskBy.ContainsKey($dk)) { $diskBy[$dk] = 0L }; $diskBy[$dk] += [long]$q.disk }
Say ("  components: " + $plan.Count + ", download about " + (Gb $dlSum))
$noSpace = $false
foreach ($dk in $diskBy.Keys) {
  $free = (Get-PSDrive -Name $dk).Free
  Say ("  drive " + $dk + ": about " + (Gb $diskBy[$dk]) + " needed, " + (Gb $free) + " free")
  if ($diskBy[$dk] -gt $free) { $noSpace = $true }
}
if ($noSpace) { Say '  Not enough disk space - free some space and run again.' 'Red'; exit 1 }
$lics = ($plan | ForEach-Object { $_.lic } | Where-Object { $_ } | Sort-Object -Unique) -join '; '
if ($plan.Count) { Say ("  licenses: " + $lics) }
if ($plan | Where-Object { $_.id -like 'pixal3d*' }) {
  Say '  Pixal3D: DINOv3 is taken from the official timm copy under the DINOv3 license (royalty-free, commercial use allowed;' 'Yellow'
  Say '  military purposes and violating trade restrictions are prohibited). The Trellis2 nodes pull in libraries with special licenses:' 'Yellow'
  Say '  nvdiffrast and nvdiffrec_render (NVIDIA) - non-commercial and research use only, PyMeshLab - GPL-3.0.' 'Yellow'
  Say '  AstraForge does not call nvdiffrast, nvdiffrec_render or Trellis2 texturing; holes are filled by CuMesh (MIT).' 'Yellow'
  Say '  The meshlib library (non-commercial license only) is not installed: the nodes are patched to work without it.' 'Yellow'
}
if ($plan | Where-Object { $_.lic -like 'Tencent*' }) {
  Say '  The Hunyuan3D models are distributed under the Tencent Hunyuan Community License: use is prohibited in the territory of' 'Yellow'
  Say '  the European Union, the United Kingdom and South Korea. Text: https://github.com/Tencent-Hunyuan/Hunyuan3D-2/blob/main/LICENSE' 'Yellow'
}
if ($plan.Count -eq 0) {
  # a ready Pixal3D that was found is remembered in config\paths.json (key pixal3dDir) so the studio knows where it is
  if (!$DryRun -and $p3.complete -and !$Cfg.pixal3dDir) { $Cfg.pixal3dDir = $p3.dir; SaveCfg }
  if (!$DryRun) { Shortcut }
  Say 'Done - launch the AstraForge shortcut.' 'Green'; exit 0
}
if (!(Ask 'Start the installation?' $true -Confirm)) { Say 'Cancelled.'; exit 0 }

$hasGpu = [bool]$s.gpu
foreach ($p in $plan) {
  Title $p.what
  if ($DryRun) { Say '  dry run - skipped'; continue }
  try {
    switch ($p.id) {
      'node' { InstallNode; $s.node = $Cfg.node }
      'npm' { InstallNpm $s.node }
      'blender' { InstallBlender; $s.blender = $Cfg.blender }
      'comfy' { InstallComfy $hasGpu; $s.comfy = $Cfg.comfyDir }
      'comfydeps' { InstallComfyDeps $s.comfy $hasGpu; $Cfg.comfyDir = $s.comfy; SaveCfg }
      'hy2' { Model 'https://huggingface.co/tencent/Hunyuan3D-2/resolve/main/hunyuan3d-dit-v2-0/model.fp16.safetensors' 'hunyuan3d-dit-v2.safetensors' }
      'hymv' { Model 'https://huggingface.co/tencent/Hunyuan3D-2mv/resolve/main/hunyuan3d-dit-v2-mv/model.fp16.safetensors' 'hunyuan3d-dit-v2-mv.safetensors' }
      'hy21' { Model 'https://huggingface.co/Comfy-Org/hunyuan3D_2.1_repackaged/resolve/main/hunyuan_3d_v2.1.safetensors' 'hunyuan_3d_v2.1.safetensors' }
      'sdxl' { Model 'https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors' 'sd_xl_base_1.0.safetensors' }
      'u2net' { Download 'https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx' (Join-Path $Root 'data\models\background\models\u2net\u2net.onnx') }
      'godot' { InstallGodot }
      'clips' { if (!$s.blender) { throw 'Blender is required - install it in the same run.' }; $Cfg.library = $s.library; SaveCfg; InstallClips $s.blender $s.library }
      'vfx' { $Cfg.vfxTools = $VfxDir; SaveCfg; & (Join-Path $Root 'scripts\vfx\Install-VfxTools.ps1') -Root $VfxDir; if ($LASTEXITCODE) { throw 'Not all VFX texture generators were downloaded (retry: -Only vfx)' } }
      'ollama' { $s.ollama = InstallOllama }
      'llm' { PullLlm $s.ollama $wantLlm }
      'vlm' { PullModel $s.ollama $VlmName }
      'mvadapter' {
        $sdxlFiles = @((Join-Path $Root 'data\models\checkpoints\sd_xl_base_1.0.safetensors')); if ($s.comfy) { $sdxlFiles += Join-Path $s.comfy 'models\checkpoints\sd_xl_base_1.0.safetensors' }
        if (!(FirstExisting $sdxlFiles)) { Say '  SDXL is not downloaded yet: MV-Adapter will not work without it (install it: -Only sdxl).' 'Yellow' }
        MvInstall $s
      }
      'pixal3d' { P3InstallEngine $p3.dir $s.cc; P3Smoke $p3.dir }
      'pixal3dw' { P3InstallModels $p3.dir }
      'pixal3dtest' { P3Smoke $p3.dir }
    }
    Say '  done' 'Green'
  } catch {
    Say ("  ERROR: " + $_.Exception.Message) 'Red'
    if (!(Ask 'Continue with the remaining components?' $true -Confirm)) { exit 1 }
  }
}
if ($DryRun) { exit 0 }
if (!$Cfg.library) { $Cfg.library = $s.library }
if (!$Cfg.forgeRuntime -and !$env:ASTRA_FORGE_RUNTIME -and (!(Test-Path -LiteralPath 'C:\AstraForge\runtime') -or $Root.TrimEnd('\') -eq 'C:\AstraForge')) { $Cfg.forgeRuntime = Join-Path $Root 'data\runtime\forge' }
if ($s.comfy -and !$Cfg.comfyDir) { $Cfg.comfyDir = $s.comfy }
if ($s.blender -and !$Cfg.blender) { $Cfg.blender = $s.blender }
if ($s.godot -and !$Cfg.godot) { $Cfg.godot = $s.godot }
if ($s.node -and !$Cfg.node) { $Cfg.node = $s.node }
if ($p3.complete -and !$Cfg.pixal3dDir) { $Cfg.pixal3dDir = $p3.dir }
SaveCfg
Title 'Post-install check'
Show (Scan)
Shortcut
if (Ask 'Start AstraForge now?' $true -Confirm) { Start-Process (Join-Path $Root 'Start.cmd') }
