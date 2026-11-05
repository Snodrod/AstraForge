$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot '..\installer\amd\AmdCore.ps1')
function Assert($condition, $message) { if (!$condition) { throw $message } }
$installer = Join-Path $PSScriptRoot '..\installer\amd\Pixal3DNative.ps1'
Assert (Test-Path -LiteralPath $installer) 'native installer must exist'
$root = Join-Path $PSScriptRoot '_native-installer-test'
$report = Join-Path $PSScriptRoot '_native-probe-test.json'
Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $report -Force -ErrorAction SilentlyContinue
try {
  . $installer -RuntimeRoot $root -ProbeOnly -ReportPath $report
  Assert (Test-Path -LiteralPath $report) 'probe-only must write a structured report without a runtime'
  $json = Get-Content -LiteralPath $report -Raw | ConvertFrom-Json
  Assert ($json.schemaVersion -eq 1) 'probe schema version'
  Assert ($json.platform -eq 'Windows') 'probe platform'
  Assert (!$json.nativeReady) 'missing Python cannot be native-ready'
  Assert (@($json.checks | Where-Object { $_.name -eq 'python_import' -and $_.status -eq 'fail' }).Count -eq 1) 'missing Python must be a visible failure'
  Assert (!(Test-Path -LiteralPath (Join-Path $root 'python_embeded'))) 'probe-only must not install Python'

  Assert-NativeRuntimePath $root
  foreach ($bad in 'relative\px', '/mnt/c/px', 'C:\AstraForge\ComfyUI-Pixal3D-AMD', 'C:\AstraForge\ComfyUI-Pixal3D') {
    $rejected = $false
    try { Assert-NativeRuntimePath $bad } catch { $rejected = $true }
    Assert $rejected "unsafe native path must be rejected: $bad"
  }
  $wheels = @(Get-AmdNativeWheelPaths)
  Assert ($wheels.Count -eq 6) 'three ROCm and three PyTorch Windows wheels are required'
  foreach ($wheel in $wheels) { Assert (Test-Path -LiteralPath $wheel) "local wheel missing: $wheel" }
  Assert (($wheels -notmatch 'cu128|cuda').Count -eq 6) 'native AMD must not select CUDA wheels'
  $meta = Get-AmdNativeMetaPackage
  Assert ($meta.EndsWith('rocm-7.2.1.tar.gz')) 'ROCm meta package providing rocm_sdk is required'
  Assert (Test-Path -LiteralPath $meta) 'official ROCm meta package must be local'

  $dryRoot = Join-Path $PSScriptRoot '_native-dry-run'
  Remove-Item -LiteralPath $dryRoot -Recurse -Force -ErrorAction SilentlyContinue
  . $installer -RuntimeRoot $dryRoot -DryRun
  Assert (!(Test-Path -LiteralPath $dryRoot)) 'dry run must not create or modify the runtime'

  $text = Get-Content -LiteralPath $installer -Raw
  Assert ($text.Contains('native-base-ready.json')) 'base probe has a separate readiness marker'
  Assert ($text.Contains("'--no-build-isolation', (Get-AmdNativeMetaPackage)")) 'offline source meta package must use the installed pinned setuptools'
  Assert (!$text.Contains('wsl.exe')) 'native installer must not call WSL'
  Assert (!$text.Contains('Restart-Computer')) 'native installer must not reboot'
  Assert (!$text.Contains('Install-AmdDriver')) 'native installer must not install drivers'

  $manifestPath = Join-Path $PSScriptRoot '..\installer\amd\native-wheels.json'
  Assert (Test-Path -LiteralPath $manifestPath) 'native wheel manifest must exist'
  $manifest = Get-Content -LiteralPath $manifestPath -Raw | ConvertFrom-Json
  Assert ($manifest.schemaVersion -eq 1) 'native wheel manifest schema'
  Assert (@($manifest.official).Count -eq 7) 'seven official ROCm packages are required'
  Assert (@($manifest.extensions).Count -eq 3) 'three bundled HIP wheels are required'
  foreach ($entry in @($manifest.official) + @($manifest.extensions)) {
    Assert ($entry.file -and $entry.size -gt 0) 'wheel file and positive size are required'
    Assert ($entry.sha256 -match '^[0-9a-f]{64}$') "wheel SHA-256 is invalid: $($entry.file)"
    Assert ($entry.file -notmatch 'cu128|cuda') "CUDA package is forbidden: $($entry.file)"
  }
  foreach ($entry in $manifest.official) {
    Assert ($entry.url -match '^https://repo\.radeon\.com/rocm/windows/rocm-rel-7\.2\.1/') "official URL must be pinned: $($entry.file)"
  }
  $extensionWheels = @(Get-AmdExtensionWheelPaths)
  Assert ($extensionWheels.Count -eq 3) 'three bundled HIP wheel paths are required'
  foreach ($wheel in $extensionWheels) { Assert (Test-Path -LiteralPath $wheel) "bundled HIP wheel missing: $wheel" }

  $cache = Join-Path $PSScriptRoot '_native-download-test'
  $source = Join-Path $PSScriptRoot '_native-download-source.bin'
  $wrong = Join-Path $PSScriptRoot '_native-download-wrong.bin'
  $oldWheelDir = $NativeWheelDir
  [IO.File]::WriteAllBytes($source, [Text.Encoding]::UTF8.GetBytes('verified-wheel'))
  [IO.File]::WriteAllBytes($wrong, [Text.Encoding]::UTF8.GetBytes('wrong-wheel'))
  $entry = @{
    file = 'test-wheel.whl'; size = (Get-Item -LiteralPath $source).Length
    sha256 = (Get-FileHash -LiteralPath $source -Algorithm SHA256).Hash.ToLowerInvariant()
    url = 'https://example.invalid/test-wheel.whl'
  }
  $script:mockSource = $source
  $script:downloadCalls = 0
  function Invoke-WebRequest {
    param($Uri, $OutFile)
    $script:downloadCalls++
    Copy-Item -LiteralPath $script:mockSource -Destination $OutFile -Force
  }
  try {
    $NativeWheelDir = $cache
    $destination = Join-Path $cache $entry.file
    New-Item -ItemType Directory -Force -Path $cache | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination -Force
    Assert ((Save-AmdOfficialPackage $entry) -eq $destination) 'valid cache path must be returned'
    Assert ($script:downloadCalls -eq 0) 'valid cache must not be downloaded again'

    Set-Content -LiteralPath $destination -Value 'corrupt' -Encoding ASCII
    $null = Save-AmdOfficialPackage $entry
    Assert ($script:downloadCalls -eq 1) 'corrupt cache must be downloaded once'
    Assert (Test-AmdPackageFile $entry $destination) 'downloaded replacement must match the manifest'

    Remove-Item -LiteralPath $destination -Force
    $script:mockSource = $wrong
    $rejected = $false
    try { $null = Save-AmdOfficialPackage $entry } catch { $rejected = $true }
    Assert $rejected 'hash mismatch after download must fail'
    Assert (!(Test-Path -LiteralPath $destination)) 'bad downloaded package must not become a valid cache file'
  } finally {
    $NativeWheelDir = $oldWheelDir
    Remove-Item -LiteralPath $cache -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $source, $wrong -Force -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath Function:\Invoke-WebRequest -Force -ErrorAction SilentlyContinue
  }
  Write-Host 'Native AMD installer unit tests passed'
} finally {
  Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $report -Force -ErrorAction SilentlyContinue
}
