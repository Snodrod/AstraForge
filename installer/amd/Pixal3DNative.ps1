[CmdletBinding()]
param(
  [string]$RuntimeRoot = '',
  [switch]$ProbeOnly,
  [switch]$SkipModels,
  [switch]$ForceProbe,
  [switch]$DryRun,
  [string]$ReportPath = ''
)
$ErrorActionPreference = 'Stop'
$ProjectRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
if (!$RuntimeRoot) { $RuntimeRoot = Join-Path $ProjectRoot 'runtime\ComfyUI-Pixal3D-AMD-Native' }
if (!$ReportPath) { $ReportPath = Join-Path $ProjectRoot 'data\runtime\diagnostics\native-amd\base-probe.json' }
$NativeManifest = Get-Content -LiteralPath (Join-Path $ProjectRoot 'installer\pixal3d\manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$NativeWheelManifest = Get-Content -LiteralPath (Join-Path $ProjectRoot 'installer\amd\native-wheels.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$NativeWheelDir = Join-Path $ProjectRoot 'installer\downloads\amd'
$NativeExtensionWheelDir = Join-Path $ProjectRoot 'installer\amd\wheels'
$NativeToolsDir = Join-Path $ProjectRoot 'scripts\native'

function Assert-NativeRuntimePath([string]$Path) {
  if (!$Path -or ![IO.Path]::IsPathRooted($Path) -or $Path -match '^/mnt/[a-z](/|$)' -or $Path -match '(?i)wsl\.exe') {
    throw "Native AMD runtime must be an absolute native Windows path: $Path"
  }
  $leaf = (Split-Path -Leaf $Path).ToLowerInvariant()
  if ($leaf -in @('comfyui-pixal3d', 'comfyui-pixal3d-amd')) {
    throw "Refusing to overwrite an existing NVIDIA or Linux runtime: $Path"
  }
}

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

function Get-AmdNativeWheelPaths() {
  $paths = @($NativeWheelManifest.official | Where-Object { $_.file.EndsWith('.whl') } | ForEach-Object {
    if ($_.file -match '(?i)cu128|cuda') { throw "A CUDA wheel is not valid for native AMD: $($_.file)" }
    Save-AmdOfficialPackage $_
  })
  if ($paths.Count -ne 6) { throw 'Exactly six official ROCm/PyTorch wheels are required.' }
  return $paths
}

function Get-AmdNativeMetaPackage() {
  $entry = @($NativeWheelManifest.official | Where-Object { $_.file -eq 'rocm-7.2.1.tar.gz' })
  if ($entry.Count -ne 1) { throw 'The ROCm meta package must occur exactly once in the manifest.' }
  return Save-AmdOfficialPackage $entry[0]
}

function Get-AmdExtensionWheelPaths() {
  $paths = @($NativeWheelManifest.extensions | ForEach-Object {
    if ($_.file -match '(?i)cu128|cuda') { throw "A CUDA wheel is not valid for native AMD: $($_.file)" }
    $path = Join-Path $NativeExtensionWheelDir $_.file
    if (!(Test-AmdPackageFile $_ $path)) { throw "Bundled HIP wheel failed size or SHA-256 validation: $path" }
    $path
  })
  if ($paths.Count -ne 3) { throw 'Exactly three bundled HIP wheels are required.' }
  return $paths
}

function Write-NativeProbeReport($Report) {
  $parent = Split-Path -Parent $ReportPath
  New-Item -ItemType Directory -Force -Path $parent | Out-Null
  [IO.File]::WriteAllText($ReportPath, (($Report | ConvertTo-Json -Depth 8) + "`n"), (New-Object Text.UTF8Encoding($false)))
}

function Invoke-NativeProbe {
  $python = Join-Path $RuntimeRoot 'python_embeded\python.exe'
  if (!(Test-Path -LiteralPath $python)) {
    Write-NativeProbeReport @{
      schemaVersion = 1
      platform = 'Windows'
      gpu = @{ name = ''; architecture = 'unknown'; vramBytes = 0 }
      torch = @{ version = ''; hip = '' }
      checks = @(@{ name = 'python_import'; status = 'fail'; details = "Native Python not found: $python" })
      nativeReady = $false
    }
    return
  }
  $verify = Join-Path $NativeToolsDir 'verify_pixal3d_amd.py'
  & $python -I $verify --json $ReportPath
  if ($LASTEXITCODE -ne 0) { throw "Native AMD probe exited with code $LASTEXITCODE" }
}

function Install-NativePython {
  $pythonDir = Join-Path $RuntimeRoot 'python_embeded'
  $python = Join-Path $pythonDir 'python.exe'
  $downloads = Join-Path $RuntimeRoot 'cache\downloads'
  New-Item -ItemType Directory -Force -Path $downloads | Out-Null
  $info = $NativeManifest.python
  if (!(Test-Path -LiteralPath $python)) {
    $archive = Join-Path $downloads ("python-$($info.version)-embed-amd64.zip")
    if (!(Test-Path -LiteralPath $archive)) { Invoke-WebRequest -Uri $info.url -OutFile $archive }
    Expand-Archive -LiteralPath $archive -DestinationPath $pythonDir -Force
    $signature = Get-AuthenticodeSignature -LiteralPath $python
    $subject = if ($signature.SignerCertificate) { $signature.SignerCertificate.Subject } else { '' }
    if ($signature.Status -in @('HashMismatch', 'NotSigned') -or $subject -notmatch [regex]::Escape($info.signer)) {
      throw "Portable Python signature verification failed: $($signature.Status)"
    }
  }
  $short = $info.version -replace '^(\d+)\.(\d+).*', '$1$2'
  $pth = Join-Path $pythonDir "python$short._pth"
  $lines = @('../ComfyUI', "python$short.zip", '.', 'Lib/site-packages', 'Lib', 'Scripts', 'import site')
  [IO.File]::WriteAllText($pth, (($lines -join "`r`n") + "`r`n"), (New-Object Text.UTF8Encoding($false)))
  New-Item -ItemType Directory -Force -Path (Join-Path $pythonDir 'Lib\site-packages') | Out-Null
  if (!(Test-Path -LiteralPath (Join-Path $pythonDir 'Lib\site-packages\pip'))) {
    $metadata = Invoke-RestMethod -Uri "https://pypi.org/pypi/pip/$($info.pip)/json"
    $wheel = @($metadata.urls | Where-Object { $_.packagetype -eq 'bdist_wheel' })[0]
    if (!$wheel) { throw 'PyPI did not return a pip wheel.' }
    $wheelPath = Join-Path $downloads $wheel.filename
    if (!(Test-Path -LiteralPath $wheelPath)) { Invoke-WebRequest -Uri $wheel.url -OutFile $wheelPath }
    $actualHash = (Get-FileHash -LiteralPath $wheelPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actualHash -ne $wheel.digests.sha256.ToLowerInvariant()) { throw 'pip wheel checksum mismatch.' }
    $bootstrap = "import sys; sys.path.insert(0, r'$wheelPath'); from pip._internal.cli.main import main; sys.exit(main(['install', '--no-index', '--find-links', r'$downloads', '--no-warn-script-location', 'pip==$($info.pip)']))"
    & $python -I -c $bootstrap
    if ($LASTEXITCODE -ne 0) { throw 'pip installation failed.' }
  }
}

function Invoke-NativePip([string[]]$Arguments) {
  $python = Join-Path $RuntimeRoot 'python_embeded\python.exe'
  & $python -I -m pip @Arguments --no-warn-script-location --no-cache-dir
  if ($LASTEXITCODE -ne 0) { throw "Native pip failed: $($Arguments -join ' ')" }
}

Assert-NativeRuntimePath $RuntimeRoot
$RuntimeRoot = [IO.Path]::GetFullPath($RuntimeRoot)
$baseMarker = Join-Path $RuntimeRoot 'native-base-ready.json'
if ($ProbeOnly) {
  Invoke-NativeProbe
  return
}
if ($DryRun) {
  Write-Host "Native AMD base runtime would be installed into $RuntimeRoot"
  Write-Host 'No driver, model, system Python, or existing runtime changes.'
  return
}
if ((Test-Path -LiteralPath $baseMarker) -and !$ForceProbe) {
  Invoke-NativeProbe
  return
}

New-Item -ItemType Directory -Force -Path $RuntimeRoot | Out-Null
$env:HF_HOME = Join-Path $RuntimeRoot 'cache\huggingface'
$env:TORCH_HOME = Join-Path $RuntimeRoot 'cache\torch'
$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'
$env:PIP_NO_INPUT = '1'
Install-NativePython
Invoke-NativePip @('install', '-r', (Join-Path $NativeToolsDir 'requirements-base.txt'))
$python = Join-Path $RuntimeRoot 'python_embeded\python.exe'
& $python -I (Join-Path $NativeToolsDir 'install_pixal3d_amd.py') --runtime $RuntimeRoot --wheel-dir $NativeWheelDir --check
if ($LASTEXITCODE -ne 0) { throw 'Native runtime or wheel validation failed.' }
$wheels = @(Get-AmdNativeWheelPaths)
Invoke-NativePip @('install', '--no-index', '--no-deps', '--no-build-isolation', (Get-AmdNativeMetaPackage))
Invoke-NativePip (@('install', '--no-index', '--no-deps') + $wheels)
Invoke-NativeProbe
$report = Get-Content -LiteralPath $ReportPath -Raw -Encoding UTF8 | ConvertFrom-Json
if (!$report.nativeReady) {
  Remove-Item -LiteralPath $baseMarker -Force -ErrorAction SilentlyContinue
  throw "Native AMD base probe failed. See $ReportPath"
}
$marker = @{
  schemaVersion = 1
  runtime = $RuntimeRoot
  report = $ReportPath
  reportSha256 = (Get-FileHash -LiteralPath $ReportPath -Algorithm SHA256).Hash.ToLowerInvariant()
  gpu = $report.gpu
  torch = $report.torch
  capabilities = @{ single512 = $false; single1024 = $false; multi512 = $false; multi1024 = $false; uvTexture512 = $false; uvTexture1024 = $false }
  createdAt = [DateTime]::UtcNow.ToString('o')
}
[IO.File]::WriteAllText($baseMarker, (($marker | ConvertTo-Json -Depth 6) + "`n"), (New-Object Text.UTF8Encoding($false)))
Write-Host "Native AMD base runtime is ready. Pixal3D capabilities remain locked pending extension and E2E probes: $baseMarker"
