# af_status.ps1 - READ-ONLY AstraForge status check (PowerShell 5.1, UTF-8 with BOM).
# Starts nothing, downloads nothing, changes nothing: it only looks at files, ports (HTTP GET), nvidia-smi and `--version` of node/blender/godot.
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File af_status.ps1 [-App <app folder>] [-NoVersions] [-Json]
#
# App folder: -App, then $env:ASTRA_APP, then derived from this script's location (it lives at
# <app>\ai\skills\astraforge\scripts; when reached through a junction such as ~\.claude\skills\astraforge
# the junction target is resolved), then C:\AstraForge (if Start.cmd is there).
# Tool paths are resolved the same way as in the studio:
# ASTRA_* variable > config\paths.json > the default from the code (server\*.mjs, installer\Install.ps1).
# States: present / missing / off (a service that does not answer on its port - normal for the studio) / running.
# The exit code is always 0 (the script is a report, not a gate); missing required items show in the summary lines.
param(
  [string]$App,
  [switch]$NoVersions,
  [switch]$Json
)
$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

# ---------------------------------------------------------------- app folder
# Derive the app root from the script location: <app>\ai\skills\astraforge\scripts -> up 4 levels.
# If the skill folder is a junction/symlink, follow its target first.
function Resolve-AppFromScript {
  $skillDir = Split-Path -Parent $PSScriptRoot
  $real = $skillDir
  try {
    $item = Get-Item -LiteralPath $skillDir -Force -ErrorAction Stop
    if ($item.LinkType -and $item.Target) { $real = @($item.Target)[0] }
  } catch {}
  # $real = <app>\ai\skills\astraforge
  $cand = $real
  for ($i = 0; $i -lt 3; $i++) { $cand = Split-Path -Parent $cand; if (-not $cand) { return $null } }
  return $cand
}
if (-not $App) { $App = $env:ASTRA_APP }
if (-not $App) {
  $fromScript = Resolve-AppFromScript
  foreach ($c in @($fromScript, 'C:\AstraForge')) { if ($c -and (Test-Path -LiteralPath (Join-Path $c 'Start.cmd'))) { $App = $c; break } }
}
if (-not $App) { $App = 'C:\AstraForge' }

$rows = New-Object System.Collections.ArrayList
# Need: 'required' | 'optional' | 'info' (a missing 'info' item does not count). Hint is shown instead of Detail when the state is not present/running.
function Add-Row([string]$Group, [string]$Name, [string]$State, [string]$Detail, [string]$Need = 'required', [string]$Hint = '') {
  [void]$rows.Add([pscustomobject]@{ Group = $Group; Name = $Name; State = $State; Need = $Need; Detail = $Detail; Hint = $Hint })
}
function Has([string]$p) { if (-not $p) { return $false }; return (Test-Path -LiteralPath $p) }
function First-Existing([string[]]$paths) { foreach ($p in $paths) { if ($p -and (Test-Path -LiteralPath $p)) { return $p } }; return $null }
function Size-Gb([long]$b) { return ('{0:N1} GB' -f ($b / 1e9)) }
function Tcp-Open([int]$port) {
  $c = New-Object Net.Sockets.TcpClient
  try {
    $ar = $c.BeginConnect('127.0.0.1', $port, $null, $null)
    $ok = $ar.AsyncWaitHandle.WaitOne(700, $false)
    if ($ok -and $c.Connected) { return $true }
    return $false
  } catch { return $false } finally { $c.Close() }
}
function Http-Json([string]$url) {
  try { return Invoke-RestMethod -Uri $url -TimeoutSec 4 -ErrorAction Stop } catch { return $null }
}
function Version-Line([string]$exe, [string[]]$argv) {
  if ($NoVersions -or -not (Has $exe)) { return $null }
  try { $o = & $exe @argv 2>&1 | Select-Object -First 1; return ("$o").Trim() } catch { return $null }
}
function YN([bool]$b) { if ($b) { return 'present' } else { return 'missing' } }

# ---------------------------------------------------------------- 1. app folder
$G = 'Application'
$appOk = Has $App
Add-Row $G 'App folder' (YN $appOk) $App 'required' 'no such folder: use -App <path>, the ASTRA_APP variable, or get the code (gh repo clone Snodrod/AstraForge C:\AstraForge)'
$cfg = $null
$cfgPath = Join-Path $App 'config\paths.json'
if ($appOk) {
  $pkg = Join-Path $App 'package.json'
  if (Has $pkg) { try { $pj = Get-Content -LiteralPath $pkg -Raw -Encoding UTF8 | ConvertFrom-Json; Add-Row $G 'package.json' 'present' ('astraforge ' + $pj.version) } catch { Add-Row $G 'package.json' 'missing' '' 'required' 'cannot be read' } }
  else { Add-Row $G 'package.json' 'missing' '' 'required' 'is this an AstraForge folder?' }
  Add-Row $G 'Start.cmd / Stop.cmd' (YN ((Has (Join-Path $App 'Start.cmd')) -and (Has (Join-Path $App 'Stop.cmd')))) ''
  Add-Row $G 'node_modules (express)' (YN (Has (Join-Path $App 'node_modules\express'))) 'npm dependencies' 'required' 'npm component of the installer wizard (npm ci)'
  Add-Row $G 'dist (built UI)' (YN (Has (Join-Path $App 'dist\index.html'))) 'UI is built' 'required' 'needs npm run build (npm component of the installer wizard)'
  if (Has $cfgPath) {
    try { $cfg = Get-Content -LiteralPath $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json; Add-Row $G 'config\paths.json' 'present' ('keys: ' + (($cfg.PSObject.Properties.Name) -join ', ')) 'info' }
    catch { Add-Row $G 'config\paths.json' 'missing' '' 'info' 'the file exists but cannot be read' }
  } else { Add-Row $G 'config\paths.json' 'missing' '' 'info' 'not created by the wizard: paths come from ASTRA_* variables or the defaults' }
  Add-Row $G 'git repository' (YN (Has (Join-Path $App '.git'))) 'origin = Snodrod/AstraForge' 'info' 'no .git: a copy without history'
}
function Cfg([string]$key) { if ($cfg -and $cfg.PSObject.Properties[$key] -and $cfg.$key) { return [string]$cfg.$key }; return $null }
function Pick([string]$envName, [string]$cfgKey, [string]$default) {
  $e = [Environment]::GetEnvironmentVariable($envName, 'Process')
  if ($e) { return $e }
  $c = Cfg $cfgKey
  if ($c) { return $c }
  return $default
}

# ---------------------------------------------------------------- 2. services (ports)
$G = 'Services'
$health = Http-Json 'http://127.0.0.1:3210/api/health'
if ($health -and $health.app -eq 'AstraForge') { Add-Row $G 'Studio :3210' 'running' ('AstraForge ' + $health.version) 'required' }
elseif (Tcp-Open 3210) { Add-Row $G 'Studio :3210' 'missing' '' 'required' 'port is taken by another application (no /api/health from AstraForge)' }
else { Add-Row $G 'Studio :3210' 'off' '' 'required' 'start with Start.cmd (scripts\Start.ps1)' }

$st = Http-Json 'http://127.0.0.1:8190/system_stats'
if ($st) { Add-Row $G 'Studio ComfyUI :8190 (Hunyuan3D, SDXL, MV-Adapter)' 'running' ('ComfyUI ' + $st.system.comfyui_version) 'required' }
else { Add-Row $G 'Studio ComfyUI :8190 (Hunyuan3D, SDXL, MV-Adapter)' 'off' '' 'required' 'the studio starts it by itself at launch if the Hunyuan weights are present' }

$sp = Http-Json 'http://127.0.0.1:8191/system_stats'
if ($sp) { Add-Row $G 'ComfyUI-Pixal3D :8191' 'running' ('ComfyUI ' + $sp.system.comfyui_version) 'optional' }
else { Add-Row $G 'ComfyUI-Pixal3D :8191' 'off' '' 'optional' 'starts on the first job or manually: Start ComfyUI (Pixal3D, 8191).bat' }

if (Tcp-Open 3211) { Add-Row $G 'VFX web generators :3211' 'running' 'separate server, started on the first GET /api/vfx-tools' 'optional' }
else { Add-Row $G 'VFX web generators :3211' 'off' '' 'optional' 'normal: starts when the VFX tab is opened' }

$tags = Http-Json 'http://127.0.0.1:11434/api/tags'
if ($tags) { Add-Row $G 'Ollama :11434' 'running' ('models: ' + @($tags.models).Count) 'optional' }
else { Add-Row $G 'Ollama :11434' 'off' '' 'optional' 'the studio LLM assistant and the qwen3-vl check are unavailable (ollama serve)' }

# ---------------------------------------------------------------- 3. programs
$G = 'Programs'
$nodeCands = @((Cfg 'node'), (Join-Path $App 'runtime\node\node.exe'))
$sysNode = Get-Command node.exe -ErrorAction SilentlyContinue
if ($sysNode) { $nodeCands += $sysNode.Source }
$node = First-Existing $nodeCands
$nv = Version-Line $node @('-v')
Add-Row $G 'Node.js (22.12+ required)' (YN ([bool]$node)) ("$node  $nv") 'required' 'node component of the installer wizard (v22.23.3)'

$blCands = @($env:ASTRA_BLENDER, (Cfg 'blender'), 'C:\Program Files\Blender Foundation\Blender 5.0\blender.exe')
if (Has (Join-Path $App 'runtime\blender')) { $blCands += (Get-ChildItem -Path (Join-Path $App 'runtime\blender') -Filter blender.exe -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName) }
if (Has 'C:\Program Files\Blender Foundation') { $blCands += (Get-ChildItem -Path 'C:\Program Files\Blender Foundation' -Directory -Filter 'Blender 5*' -ErrorAction SilentlyContinue | ForEach-Object { Join-Path $_.FullName 'blender.exe' }) }
$blender = First-Existing $blCands
$bv = Version-Line $blender @('--version')
Add-Row $G 'Blender 5.x (5.0.1 required)' (YN ([bool]$blender)) ("$blender  $bv") 'required' 'blender component of the installer wizard (5.0.1, 401 MB)'

$gdCands = @($env:ASTRA_GODOT, (Cfg 'godot'))
foreach ($d in @((Join-Path $App 'runtime\godot'), 'C:\Program Files\Godot', 'C:\Godot', $(if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'Programs\Godot' }))) {
  if ($d -and (Has $d)) { $gdCands += (Get-ChildItem -Path $d -Filter 'Godot_v4.5*console.exe' -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName) }
}
$godot = First-Existing $gdCands
$gv = Version-Line $godot @('--version')
Add-Row $G 'Godot 4.5.1 (package check; 4.5.1 only)' (YN ([bool]$godot)) ("$godot  $gv") 'optional' 'godot component of the installer wizard; without it godot_package is not validated'

$ollamaExe = First-Existing @((Cfg 'ollama'), $(if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe' }))
if (-not $ollamaExe) { $oc = Get-Command ollama.exe -ErrorAction SilentlyContinue; if ($oc) { $ollamaExe = $oc.Source } }
Add-Row $G 'Ollama (program)' (YN ([bool]$ollamaExe)) $ollamaExe 'optional' 'ollama component of the installer wizard'

$smi = Get-Command nvidia-smi.exe -ErrorAction SilentlyContinue
if ($smi) {
  $line = & $smi.Source --query-gpu=name,memory.total,memory.used,compute_cap --format=csv,noheader,nounits 2>$null | Select-Object -First 1
  Add-Row $G 'NVIDIA GPU' 'present' ("$line   (name, total MiB, used MiB, compute_cap)")
} else { Add-Row $G 'NVIDIA GPU' 'missing' '' 'required' 'nvidia-smi not found: image-to-3D is unavailable (the UI and Blender still work)' }

foreach ($t in @(@('git', 'git.exe'), @('gh (GitHub CLI)', 'gh.exe'), @('ffmpeg', 'ffmpeg.exe'))) {
  $cmd = Get-Command $t[1] -ErrorAction SilentlyContinue
  $src = ''; if ($cmd) { $src = $cmd.Source }
  Add-Row $G $t[0] (YN ([bool]$cmd)) $src 'info' 'not needed by the studio; needed for cloning the repository (git, gh) and video editing (ffmpeg)'
}

# ---------------------------------------------------------------- 4. studio ComfyUI, weights, Ollama models
$G = 'Studio ComfyUI, weights, models'
$comfyDir = Pick 'ASTRA_COMFY_DIR' 'comfyDir' (Join-Path $App 'runtime\ComfyUI')
$comfyOk = (Has (Join-Path $comfyDir '.venv\Scripts\python.exe')) -and (Has (Join-Path $comfyDir 'main.py'))
$hyNode = Has (Join-Path $comfyDir 'comfy_extras\nodes_hunyuan3d.py')
Add-Row $G 'Studio ComfyUI (folder with .venv)' (YN ($comfyOk -and $hyNode)) $comfyDir 'required' ($comfyDir + ': missing .venv\Scripts\python.exe, main.py or comfy_extras\nodes_hunyuan3d.py; comfy component of the installer wizard (v0.33.2)')
$ckDirs = @((Join-Path $App 'data\models\checkpoints'), (Join-Path $comfyDir 'models\checkpoints'))
function Weight([string]$name, [string]$label, [string]$need, [string]$hint) {
  $f = First-Existing ($ckDirs | ForEach-Object { Join-Path $_ $name })
  $d = ''; if ($f) { $d = $f + '  ' + (Size-Gb (Get-Item -LiteralPath $f).Length) }
  Add-Row $G $label (YN ([bool]$f)) $d $need $hint
}
Weight 'hunyuan3d-dit-v2.safetensors' 'Hunyuan3D-2 (shape from an image)' 'required' 'hy2 component of the installer wizard (4.9 GB)'
Weight 'hunyuan3d-dit-v2-mv.safetensors' 'Hunyuan3D-2mv (from several views)' 'optional' 'hymv component (4.9 GB)'
Weight 'hunyuan_3d_v2.1.safetensors' 'Hunyuan3D-2.1' 'optional' 'hy21 component (7.4 GB)'
Weight 'sd_xl_base_1.0.safetensors' 'SDXL base 1.0 (text-to-concept, MV-Adapter)' 'optional' 'sdxl component (6.9 GB)'
$u2 = Get-ChildItem -Path (Join-Path $App 'data\models\background') -Filter u2net.onnx -Recurse -ErrorAction SilentlyContinue | Select-Object -First 1
$u2p = ''; if ($u2) { $u2p = $u2.FullName }
Add-Row $G 'u2net.onnx (background removal)' (YN ([bool]$u2)) $u2p 'required' 'u2net component of the installer wizard (176 MB)'
$mvW = Join-Path $App 'data\models\mvadapter\mvadapter_i2mv_sdxl.safetensors'
$mvC = Join-Path $App 'data\models\mvadapter\sdxl-config\model_index.json'
$mvN = Join-Path $App 'data\runtime\custom_nodes\ComfyUI-MVAdapter\astra_views.py'
Add-Row $G 'MV-Adapter: weights' (YN (Has $mvW)) 'mvadapter_i2mv_sdxl.safetensors' 'optional' 'mvadapter component of the installer wizard (3.6 GB; needs SDXL)'
Add-Row $G 'MV-Adapter: node and sdxl-config' (YN ((Has $mvC) -and (Has $mvN))) 'data\runtime\custom_nodes\ComfyUI-MVAdapter + data\models\mvadapter\sdxl-config' 'optional' 'mvadapter component of the installer wizard'
$xat = (Get-ChildItem -Path (Join-Path $App 'data\python') -Filter 'xatlas*' -ErrorAction SilentlyContinue | Select-Object -First 1)
Add-Row $G 'xatlas (UV unwrap, data\python)' (YN ([bool]$xat)) 'or installed in the ComfyUI .venv' 'info' 'not in data\python: check the ComfyUI .venv (comfydeps)'

# Ollama models: via the API, or, when the server is off, from the manifests on disk (as in installer\Install.ps1)
$ollamaModels = @()
if ($tags) { $ollamaModels = @($tags.models | ForEach-Object { $_.name }) }
else {
  $base = $env:OLLAMA_MODELS
  if (-not $base) { $base = [Environment]::GetEnvironmentVariable('OLLAMA_MODELS', 'User') }
  if (-not $base) { $base = Join-Path $HOME '.ollama\models' }
  $mroot = Join-Path $base 'manifests\registry.ollama.ai'
  if (Has $mroot) {
    foreach ($f in (Get-ChildItem -LiteralPath $mroot -Recurse -File -ErrorAction SilentlyContinue)) {
      $parts = $f.FullName.Substring($mroot.Length).TrimStart('\') -split '\\'
      if ($parts.Count -eq 3) { if ($parts[0] -eq 'library') { $ollamaModels += ($parts[1] + ':' + $parts[2]) } else { $ollamaModels += ($parts[0] + '/' + $parts[1] + ':' + $parts[2]) } }
    }
  }
}
Add-Row $G 'Ollama: qwen3-vl:8b (texture and animation check)' (YN ($ollamaModels -contains 'qwen3-vl:8b')) ('models: ' + ($ollamaModels -join ', ')) 'optional' 'vlm component of the installer wizard (6.1 GB)'
$llmAny = @($ollamaModels | Where-Object { $_ -like 'qwen3*' -and $_ -notlike '*vl*' }).Count -gt 0
Add-Row $G 'Ollama: LLM assistant (any qwen3*)' (YN $llmAny) 'any qwen3*; the name is set in "Connections"' 'optional' 'llm component of the installer wizard (qwen3:4b/8b/14b depending on VRAM)'

# ---------------------------------------------------------------- 5. Pixal3D
$G = 'Pixal3D'
$p3Dir = Pick 'ASTRA_PIXAL3D_DIR' 'pixal3dDir' 'C:\AstraForge\ComfyUI-Pixal3D'
$p3Req = @(
  @('python_embeded\python.exe', 'Python'), @('ComfyUI\main.py', 'ComfyUI'), @('ComfyUI\custom_nodes\ComfyUI-Trellis2\nodes.py', 'Trellis2 nodes'),
  @('ComfyUI\models\TencentARC\Pixal3D\ckpts\ss_flow_img_dit_1_3B_64_bf16.safetensors', 'weight: coarse shape, 1 view'),
  @('ComfyUI\models\TencentARC\Pixal3D\ckpts\ss_flow_img_dit_1_3B_64_bf16_mv.safetensors', 'weight: coarse shape, N views'),
  @('ComfyUI\models\TencentARC\Pixal3D\ckpts\slat_flow_img2shape_dit_1_3B_512_bf16.safetensors', 'weight: shape 512'),
  @('ComfyUI\models\TencentARC\Pixal3D\ckpts\slat_flow_img2shape_dit_1_3B_512_bf16_mv.safetensors', 'weight: shape 512 mv'),
  @('ComfyUI\models\TencentARC\Pixal3D\ckpts\slat_flow_img2shape_dit_1_3B_1024_bf16.safetensors', 'weight: shape 1024'),
  @('ComfyUI\models\TencentARC\Pixal3D\ckpts\slat_flow_img2shape_dit_1_3B_1024_bf16_mv.safetensors', 'weight: shape 1024 mv'),
  @('ComfyUI\models\TencentARC\Pixal3D\ckpts\shape_dec_next_dc_f16c32_fp16.safetensors', 'weight: shape decoder'),
  @('ComfyUI\models\facebook\dinov3-vitl16-pretrain-lvd1689m\model.safetensors', 'DINOv3 ViT-L/16'),
  @('ComfyUI\models\Ruicheng\moge-2-vitl\model.pt', 'MoGe-2')
)
$p3Missing = @()
if (Has $p3Dir) { foreach ($r in $p3Req) { if (-not (Has (Join-Path $p3Dir $r[0]))) { $p3Missing += $r[1] } } } else { $p3Missing = @('folder not found') }
$p3Hint = ''; if ($p3Missing.Count) { $p3Hint = 'missing: ' + ($p3Missing -join '; ') + '. pixal3d and pixal3dw components of the installer wizard (~46 GB on disk)' }
Add-Row $G ('ComfyUI-Pixal3D (' + $p3Dir + ')') (YN ($p3Missing.Count -eq 0)) 'engine and all 12 required files in place' 'optional' $p3Hint
$p3Patched = $false
$nodesPy = Join-Path $p3Dir 'ComfyUI\custom_nodes\ComfyUI-Trellis2\nodes.py'
if (Has $nodesPy) { $p3Patched = [bool](Select-String -LiteralPath $nodesPy -SimpleMatch 'ASTRAFORGE PATCH' -Quiet) }
Add-Row $G 'Trellis2 node patch (ASTRAFORGE PATCH)' (YN $p3Patched) 'DINOv3 is taken only from the official timm copy' 'optional' 'without the patch the node may pull DINOv3 from a third-party mirror'

# ---------------------------------------------------------------- 6. library, clips, VFX
$G = 'Library and data'
$lib = Pick 'ASTRA_FORGE_LIBRARY' 'library' 'C:\AstraForge\library'
Add-Row $G ('Forge library (' + $lib + ')') (YN (Has $lib)) 'asset passports, clips, kits; paths in passports are absolute' 'required' 'created on first run; clips come from the clips component of the installer wizard'
if (Has $lib) {
  $na = @(Get-ChildItem -LiteralPath (Join-Path $lib 'assets') -Directory -ErrorAction SilentlyContinue).Count
  Add-Row $G 'Forge assets (passports)' (YN ($na -gt 0)) ("$na total") 'info' 'no assets yet'
  $mix = @(Get-ChildItem -LiteralPath (Join-Path $lib 'animations\mixamo') -Filter *.fbx -ErrorAction SilentlyContinue).Count
  $qua = @(Get-ChildItem -LiteralPath (Join-Path $lib 'animations\quaternius') -Filter *.fbx -ErrorAction SilentlyContinue).Count
  Add-Row $G 'Quaternius/ActionForge clips (CC0)' (YN ($qua -gt 0)) ("$qua clips") 'optional' 'clips component of the installer wizard (86 clips, 10 MB)'
  Add-Row $G 'Mixamo clips (downloaded by the user)' (YN ($mix -gt 0)) ("$mix clips") 'info' 'manual download only from mixamo.com (FBX, Without Skin, 30 fps) -> clips import'
  Add-Row $G 'Quaternius kits (base characters, outfits)' (YN (Has (Join-Path $lib 'kits\quaternius'))) 'kits\quaternius' 'info' 'the Universal Base Characters / Modular Outfits Fantasy archives are supplied by the user'
}
$vfx = Pick 'ASTRA_VFX_TOOLS' 'vfxTools' 'C:\AstraForge\tools\vfx'
Add-Row $G ('VFX generators (' + $vfx + ')') (YN (Has (Join-Path $vfx 'apps\material-maker\material_maker.exe'))) 'Material Maker and others; see the astraforge-vfx skill' 'optional' 'vfx component of the installer wizard or scripts\vfx\Install-VfxTools.ps1'
Add-Row $G 'Studio data\ (projects, jobs, assets)' (YN (Has (Join-Path $App 'data'))) 'do not delete: projects, models, connections' 'required' 'created on the first studio run'
Add-Row $G 'data\vault.json (API keys, DPAPI)' (YN (Has (Join-Path $App 'data\vault.json'))) 'bound to the Windows user; never copy or publish' 'info' 'no cloud service keys (not needed for local work)'

# ---------------------------------------------------------------- output
if ($Json) { $rows | ConvertTo-Json -Depth 4; return }

Write-Host ''
Write-Host ('AstraForge - status (read-only)   ' + (Get-Date -Format 'yyyy-MM-dd HH:mm')) -ForegroundColor Cyan
Write-Host ('App: ' + $App) -ForegroundColor DarkGray
$prev = ''
foreach ($r in $rows) {
  if ($r.Group -ne $prev) { Write-Host ''; Write-Host ('== ' + $r.Group + ' ==') -ForegroundColor Cyan; $prev = $r.Group }
  $color = 'Gray'
  switch ($r.State) {
    'present' { $color = 'Green' }
    'running' { $color = 'Green' }
    'missing' { if ($r.Need -eq 'required') { $color = 'Red' } else { $color = 'Yellow' } }
    'off' { $color = 'DarkYellow' }
  }
  $text = $r.Detail
  if (($r.State -eq 'missing' -or $r.State -eq 'off') -and $r.Hint) { $text = $r.Hint }
  Write-Host ('  [' + $r.State.PadRight(9) + '] ') -NoNewline -ForegroundColor $color
  Write-Host $r.Name -NoNewline
  if ($text) { Write-Host ('  - ' + $text) -ForegroundColor DarkGray } else { Write-Host '' }
}

$have = @($rows | Where-Object { $_.State -in @('present', 'running') }).Count
$off = @($rows | Where-Object { $_.State -eq 'off' }).Count
$missReq = @($rows | Where-Object { $_.State -eq 'missing' -and $_.Need -eq 'required' })
$missOpt = @($rows | Where-Object { $_.State -eq 'missing' -and $_.Need -eq 'optional' })
Write-Host ''
Write-Host ('Total: present/running ' + $have + ', off ' + $off + ', missing required ' + $missReq.Count + ', missing optional ' + $missOpt.Count) -ForegroundColor Cyan
if ($missReq.Count) { Write-Host ('Missing required: ' + (($missReq | ForEach-Object { $_.Name }) -join '; ')) -ForegroundColor Red }
$studioUp = [bool]($health -and $health.app -eq 'AstraForge')
$baseReady = $appOk -and $node -and (Has (Join-Path $App 'node_modules\express')) -and (Has (Join-Path $App 'dist\index.html'))
$okHy = $comfyOk -and (First-Existing ($ckDirs | ForEach-Object { Join-Path $_ 'hunyuan3d-dit-v2.safetensors' })) -and $u2
Write-Host ('Studio can start: ' + $(if ($baseReady) { 'yes (Start.cmd)' } else { 'no - see the red lines; wizard: Install AstraForge.cmd' }))
Write-Host ('Forge (Blender + Godot): ' + $(if ($blender -and $godot) { 'ready' } elseif ($blender) { 'Blender present, Godot missing - packages are not validated' } else { 'not ready' }))
Write-Host ('Image-to-3D: Hunyuan ' + $(if ($okHy) { 'ready' } else { 'not ready' }) + '; Pixal3D ' + $(if ($p3Missing.Count -eq 0) { 'ready' } else { 'not ready' }))
if (-not $studioUp) { Write-Host 'The studio is off right now - that is normal; start it with Start.cmd (or scripts\Start.ps1).' -ForegroundColor DarkYellow }
exit 0
