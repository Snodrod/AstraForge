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
Assert (((Get-AmdTorchPackageUrls) -notmatch 'rocm-rel-7.2.1').Count -eq 0) 'all wheels must be pinned to ROCm 7.2.1'
Assert (((Get-AmdTorchPackageUrls) -match 'cu128|download.pytorch.org').Count -eq 0) 'AMD profile must not contain CUDA wheels'
. (Join-Path $PSScriptRoot '..\installer\Install-AMD.ps1') -ScanOnly -Yes -DryRun -Only node,npm
Assert ($ScanOnly -and $Yes -and $DryRun) 'shared installer import must preserve AMD switches'
Assert ($Only.Count -eq 2 -and $Only[0] -eq 'node' -and $Only[1] -eq 'npm') 'shared installer import must preserve component selection'
$amdInstaller = Get-Content (Join-Path $PSScriptRoot '..\installer\Install-AMD.ps1') -Raw
Assert ($amdInstaller.Contains('InstallAmdComfyDeps')) 'AMD ComfyUI installer must exist'
Assert ($amdInstaller.Contains('Get-AmdRocmPackageUrls')) 'ROCm runtime must be installed'
Assert ($amdInstaller.Contains('Assert-AmdTorch')) 'ROCm must be validated before model downloads'
Assert ($amdInstaller.Contains('!$amdTorchReady')) 'complete NVIDIA Python libraries must not skip ROCm replacement'
Assert ($amdInstaller.Contains("id='pixal3d'")) 'AMD Pixal3D engine component is required'
Assert ($amdInstaller.Contains("id='pixal3dw'")) 'AMD Pixal3D weights component is required'
Assert ($amdInstaller.Contains("id='pixal3dtest'")) 'AMD Pixal3D verification component is required'
Assert ($amdInstaller.Contains('Pixal3DAmd.ps1')) 'AMD WSL implementation must be separate from NVIDIA installer'
$amdPixal = Get-Content (Join-Path $PSScriptRoot '..\installer\amd\Pixal3DAmd.ps1') -Raw
Assert ($amdPixal.Contains('install-pixal3d-amd.sh')) 'AMD Pixal3D must run the reviewed WSL build'
Assert ($amdPixal.Contains('wsl-ready.json')) 'weights must require the ROCm probe'
Assert (!$amdPixal.Contains('P3InstallEngine')) 'AMD must not install the Windows CUDA engine'
Assert (!$amdPixal.Contains('P3Python')) 'AMD must not install Windows Python'
Assert (Test-Path (Join-Path $PSScriptRoot '..\Install AstraForge AMD.cmd')) 'AMD CMD entry point is required'
Write-Host 'AMD installer unit tests passed'
