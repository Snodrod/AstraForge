$ErrorActionPreference='Stop'
$taskRoot=Split-Path -Parent $PSScriptRoot
try {$taskHealth=Invoke-RestMethod -Uri 'http://127.0.0.1:3210/api/health' -TimeoutSec 2} catch {$taskHealth=$null}
if($taskHealth.app -eq 'AstraForge'){Invoke-RestMethod -Uri 'http://127.0.0.1:3210/api/shutdown' -Method Post -Headers @{'x-astra-client'='studio'} | Out-Null;Start-Sleep -Seconds 1}
# A runtime can outlive a previous studio restart. Stop only the process whose command
# explicitly identifies this application's dedicated input folder.
$taskInput=Join-Path $taskRoot 'data\runtime\input'
$taskOwned=Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | Where-Object {$_.CommandLine -and $_.CommandLine.Contains($taskInput) -and $_.CommandLine.Contains('--port 8190')}
foreach($taskProcess in $taskOwned){taskkill.exe /PID $taskProcess.ProcessId /T /F | Out-Null}
# The Pixal3D engine (ComfyUI-Pixal3D, port 8191) the studio started itself carries data\runtime\pixal3d-temp in its command line;
# a Pixal3D started by hand (its own bat file) is not ours and is left running.
$taskPixalTemp=Join-Path $taskRoot 'data\runtime\pixal3d-temp'
$taskPixal=Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue | Where-Object {$_.CommandLine -and $_.CommandLine.Contains($taskPixalTemp)}
foreach($taskProcess in $taskPixal){taskkill.exe /PID $taskProcess.ProcessId /T /F | Out-Null}
Write-Host 'AstraForge stopped. The existing ComfyUI on 8188 and Ollama were left running.'
