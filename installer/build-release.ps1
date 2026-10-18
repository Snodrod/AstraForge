# Builds a single distributable file: AstraForge-Setup-<version>.exe (the application + the built interface + node_modules
# without dev packages). Models, Blender, ComfyUI and the rest are not included - the installer\Install.ps1 wizard downloads them.
#   powershell -ExecutionPolicy Bypass -File installer\build-release.ps1 [-Out <folder>]
# By default the result goes to ..\AstraForge-release next to the application folder.
param([string]$Out)
$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $PSScriptRoot
if (!$Out) { $Out = Join-Path (Split-Path -Parent $Root) 'AstraForge-release' }
$version = (Get-Content -LiteralPath (Join-Path $Root 'package.json') -Raw | ConvertFrom-Json).version
$stage = Join-Path $Out 'stage'
if (Test-Path -LiteralPath $stage) { Remove-Item -LiteralPath $stage -Recurse -Force }
New-Item -ItemType Directory -Force -Path $stage | Out-Null

function Copy-Tree($from, $to, [string[]]$xd, [string[]]$xf) {
  $a = @($from, $to, '/E', '/NFL', '/NDL', '/NJH', '/NJS', '/NP', '/R:1', '/W:1')
  if ($xd) { $a += '/XD'; $a += $xd }
  if ($xf) { $a += '/XF'; $a += $xf }
  & robocopy.exe @a | Out-Null
  if ($LASTEXITCODE -ge 8) { throw "robocopy ${from} -> ${to}: code $LASTEXITCODE" }
}

# 1. application files - without user data, models, downloads and machine-specific settings
$skipDirs = @('node_modules', '.git', '__pycache__', 'library.moved-*') + @(
  'runtime', 'library', 'config', 'installer\downloads', 'installer\setup\bin',
  'data\models', 'data\assets', 'data\jobs', 'data\projects', 'data\exports', 'data\tripo', 'data\python',
  'data\library', 'data\runtime', 'data\views', '.claude') | ForEach-Object { if ($_ -match '\\|^(runtime|library|config)$') { Join-Path $Root $_ } else { $_ } }
Copy-Tree $Root $stage $skipDirs @('*.glb', '*.zip', '*.pyc', '.env', 'vault.json', 'install.log', 'node.pid', '*.log', (Join-Path $Root 'data\config.json'))
# the ComfyUI-MVAdapter node is not put into the archive: the wizard installs it itself (the original from GitHub plus the AstraForge patches from installer\mvadapter\patch)
Copy-Tree (Join-Path $Root 'data\runtime\custom_nodes') (Join-Path $stage 'data\runtime\custom_nodes') @('__pycache__', 'ComfyUI-MVAdapter') @('*.pyc')
if (!(Test-Path -LiteralPath (Join-Path $stage 'dist\index.html'))) { throw 'The built interface is missing: run npm run build.' }

# the AstraForge patches to MV-Adapter are taken from the live copy of the node if there is one (so that fresh files go into the installer),
# otherwise the installer\mvadapter\patch snapshot from the application tree stays
$mvLive = Join-Path $Root 'data\runtime\custom_nodes\ComfyUI-MVAdapter'
$mvPatch = Join-Path $stage 'installer\mvadapter\patch'
if (Test-Path -LiteralPath $mvLive) {
  foreach ($f in (Get-ChildItem -LiteralPath $mvPatch -File)) {
    $live = Join-Path $mvLive $f.Name
    if (Test-Path -LiteralPath $live) { Copy-Item -LiteralPath $live -Destination $f.FullName -Force } else { Write-Warning ("$($f.Name) is missing in the live MV-Adapter node, the snapshot was kept") }
  }
}
foreach ($need in 'installer\pixal3d\manifest.json', 'installer\pixal3d\pixal3d.ps1', 'installer\pixal3d\requirements-lock.txt', 'installer\mvadapter\mvadapter.ps1', 'installer\mvadapter\patch\astra_views.py', 'scripts\views\make_sdxl_config.py') {
  if (!(Test-Path -LiteralPath (Join-Path $stage $need))) { throw "The archive is missing $need" }
}

# 2. node_modules without dev packages (vite etc.) - the user will not need npm or the internet for npm
Copy-Tree (Join-Path $Root 'node_modules') (Join-Path $stage 'node_modules') @() @()
$npm = (Get-Command npm.cmd -ErrorAction Stop).Source
Push-Location $stage
try {
  $ErrorActionPreference = 'Continue'
  & $npm prune --omit=dev --no-audit --no-fund 2>&1 | Out-Null
  $code = $LASTEXITCODE
  $ErrorActionPreference = 'Stop'
  if ($code -ne 0) { throw "npm prune: code ${code}" }
} finally { Pop-Location }

# 3. payload.zip and the exe
$files = (Get-ChildItem -LiteralPath $stage -Recurse -File | Measure-Object -Property Length -Sum)
Add-Type -AssemblyName System.IO.Compression.FileSystem
$payload = Join-Path $Out 'payload.zip'
if (Test-Path -LiteralPath $payload) { Remove-Item -LiteralPath $payload }
[IO.Compression.ZipFile]::CreateFromDirectory($stage, $payload, [IO.Compression.CompressionLevel]::Optimal, $false)
$csc = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$exe = Join-Path $Out "AstraForge-Setup-$version.exe"
$fw = Split-Path $csc
$cscArgs = @('/nologo', '/target:winexe', '/optimize+', '/codepage:65001',
  "/win32icon:$(Join-Path $Root 'assets\astraforge.ico')", "/resource:$payload,payload.zip",
  "/r:$fw\System.IO.Compression.dll", "/r:$fw\System.IO.Compression.FileSystem.dll",
  '/r:System.Windows.Forms.dll', '/r:System.Drawing.dll', "/out:$exe", (Join-Path $PSScriptRoot 'setup\Setup.cs'))
& $csc @cscArgs
if ($LASTEXITCODE -ne 0) { throw "csc: code ${LASTEXITCODE}" }
Remove-Item -LiteralPath $payload
Remove-Item -LiteralPath $stage -Recurse -Force
$hash = (Get-FileHash -LiteralPath $exe -Algorithm SHA256).Hash
"$hash  $(Split-Path $exe -Leaf)" | Set-Content -LiteralPath "$exe.sha256" -Encoding ASCII
Write-Host ("Done: {0}" -f $exe)
Write-Host ("  {0:N1} MB, {1} files inside ({2:N0} MB unpacked), SHA-256 {3}" -f ((Get-Item -LiteralPath $exe).Length / 1e6), $files.Count, ($files.Sum / 1e6), $hash)
