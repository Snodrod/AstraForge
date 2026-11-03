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
  Write-Host 'Native AMD installer unit tests passed'
} finally {
  Remove-Item -LiteralPath $root -Recurse -Force -ErrorAction SilentlyContinue
  Remove-Item -LiteralPath $report -Force -ErrorAction SilentlyContinue
}
