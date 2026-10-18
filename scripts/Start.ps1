param([switch]$NoBrowser)
$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent $PSScriptRoot
$taskUrl='http://127.0.0.1:3210'
try {$taskHealth=Invoke-RestMethod -Uri "$taskUrl/api/health" -TimeoutSec 2} catch {$taskHealth=$null}
if ($taskHealth.app -eq 'AstraForge') {if (!$NoBrowser) {Start-Process $taskUrl}; exit 0}
if ($taskHealth) {throw 'Port 3210 is in use by another application.'}
# paths written by the installer (installer/Install.ps1); environment variables set by the user win
$taskPaths=Join-Path $taskRoot 'config\paths.json'
$taskCfg=$null
if (Test-Path -LiteralPath $taskPaths) {$taskCfg=Get-Content -LiteralPath $taskPaths -Raw -Encoding UTF8 | ConvertFrom-Json}
foreach ($taskPair in @(@('comfyDir','ASTRA_COMFY_DIR'),@('blender','ASTRA_BLENDER'),@('godot','ASTRA_GODOT'),@('library','ASTRA_FORGE_LIBRARY'),@('forgeRuntime','ASTRA_FORGE_RUNTIME'),@('pixal3dDir','ASTRA_PIXAL3D_DIR'),@('vfxTools','ASTRA_VFX_TOOLS'))) {
  $taskValue=if ($taskCfg) {$taskCfg.($taskPair[0])} else {$null}
  if ($taskValue -and !(Test-Path "env:$($taskPair[1])")) {Set-Item "env:$($taskPair[1])" $taskValue}
}
$taskNode=if ($taskCfg -and $taskCfg.node -and (Test-Path -LiteralPath $taskCfg.node)) {$taskCfg.node} else {(Get-Command node.exe -ErrorAction SilentlyContinue).Source}
$taskSetup="Run 'Install AstraForge.cmd' in the application folder."
if (!$taskNode -or !(Test-Path -LiteralPath $taskNode)) {throw "Node.js was not found. $taskSetup"}
if (!(Test-Path -LiteralPath (Join-Path $taskRoot 'node_modules\express'))) {throw "Dependencies are missing. $taskSetup"}
if (!(Test-Path -LiteralPath (Join-Path $taskRoot 'dist\index.html'))) {throw "The interface has not been built. $taskSetup"}
$taskLogs=Join-Path $taskRoot 'data\runtime'
New-Item -ItemType Directory -Force -Path $taskLogs | Out-Null
$taskProcess=Start-Process -FilePath $taskNode -ArgumentList 'server/index.mjs' -WorkingDirectory $taskRoot -WindowStyle Hidden -RedirectStandardOutput (Join-Path $taskLogs 'server.log') -RedirectStandardError (Join-Path $taskLogs 'server-error.log') -PassThru
$taskProcess.Id | Set-Content -LiteralPath (Join-Path $taskLogs 'node.pid')
for ($taskAttempt=0;$taskAttempt -lt 40;$taskAttempt++){try{$taskHealth=Invoke-RestMethod -Uri "$taskUrl/api/health" -TimeoutSec 1;if($taskHealth.app -eq 'AstraForge'){if(!$NoBrowser){Start-Process $taskUrl};Write-Host 'AstraForge is ready: http://127.0.0.1:3210';exit 0}}catch{};Start-Sleep -Milliseconds 500}
throw 'AstraForge did not start. See data/runtime/server-error.log.'
