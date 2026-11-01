param([switch]$ScanOnly, [switch]$Yes, [switch]$DryRun, [string[]]$Only)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'Install.ps1') -ScanOnly:$ScanOnly -Yes:$Yes -DryRun:$DryRun -Only:$Only
. (Join-Path $PSScriptRoot 'amd\AmdCore.ps1')

function InstallAmdComfyDeps($dir) {
  $uv = Uv
  $py = Join-Path $dir '.venv\Scripts\python.exe'
  if (!(Test-Path -LiteralPath $py)) {
    Push-Location $dir
    try { Run $uv @('venv', '--python', '3.12', '.venv') 'Python 3.12 for AMD ComfyUI' } finally { Pop-Location }
  }
  $hip = Native { & $py -c "import torch; print(torch.version.hip or '')" 2>$null }
  if ($LASTEXITCODE -ne 0 -or !$hip) {
    Run $uv @('pip', 'uninstall', '--python', $py, 'torch', 'torchvision', 'torchaudio') 'removing non-ROCm PyTorch'
    Run $uv (@('pip', 'install', '--python', $py, '--no-cache-dir') + (Get-AmdRocmPackageUrls)) 'AMD ROCm 7.2.1 runtime'
    Run $uv (@('pip', 'install', '--python', $py, '--no-cache-dir') + (Get-AmdTorchPackageUrls)) 'AMD ROCm PyTorch 2.9.1'
  }
  Assert-AmdTorch $py 'AMD Radeon RX 9070 XT'
  Run $uv @('pip', 'install', '--python', $py, '-r', (Join-Path $dir 'requirements.txt')) 'ComfyUI libraries'
  Run $uv @('pip', 'install', '--python', $py, 'rembg', 'onnxruntime', 'numba', 'scikit-image', 'xatlas') 'AstraForge libraries'
  Assert-AmdTorch $py 'AMD Radeon RX 9070 XT'
}

function InstallAmdComfy() {
  $zip = Join-Path $Dl "ComfyUI-$ComfyTag.zip"
  Download "https://github.com/comfyanonymous/ComfyUI/archive/refs/tags/$ComfyTag.zip" $zip
  $dir = Join-Path $Rt 'ComfyUI'
  if (!(Test-Path -LiteralPath (Join-Path $dir 'main.py'))) {
    Unzip $zip $Rt
    $src = Get-ChildItem -Path $Rt -Directory -Filter 'ComfyUI-*' | Select-Object -First 1
    Rename-Item -LiteralPath $src.FullName -NewName 'ComfyUI'
  }
  InstallAmdComfyDeps $dir
  $Cfg.comfyDir = $dir
  SaveCfg
}

if ($MyInvocation.InvocationName -eq '.') { return }
Clear-Host
Say 'AstraForge - AMD ROCm setup' 'White'
$gpu = Select-AmdGpu @(Get-CimInstance Win32_VideoController)
$s = Scan
if (!$gpu) { Say 'Supported discrete AMD GPU not found (RX 9070 XT is the target).' 'Red'; exit 1 }
Ok ('AMD GPU: ' + $gpu.Name)
Say ('Application folder: ' + $Root)
if ($ScanOnly) { Show $s; exit 0 }
$amdTorchReady = $false
if ($s.comfy) {
  $py = Join-Path $s.comfy '.venv\Scripts\python.exe'
  Native { & $py -c "import torch; assert torch.version.hip and torch.cuda.is_available(); assert '9070 XT' in torch.cuda.get_device_name(0)" 2>$null | Out-Null }
  $amdTorchReady = ($LASTEXITCODE -eq 0)
}
$items = @(
  @{ id='node'; missing=!$s.node; run={ InstallNode; $s.node=$Cfg.node } },
  @{ id='npm'; missing=(!$s.npmDeps -or !$s.ui); run={ InstallNpm $s.node } },
  @{ id='blender'; missing=!$s.blender; run={ InstallBlender; $s.blender=$Cfg.blender } },
  @{ id='comfy'; missing=!$s.comfy; run={ InstallAmdComfy; $s.comfy=$Cfg.comfyDir } },
  @{ id='comfydeps'; missing=($s.comfy -and (!$s.comfyDeps -or !$amdTorchReady)); run={ InstallAmdComfyDeps $s.comfy } },
  @{ id='hy2'; missing=!$s.hy2; run={ Model 'https://huggingface.co/tencent/Hunyuan3D-2/resolve/main/hunyuan3d-dit-v2-0/model.fp16.safetensors' 'hunyuan3d-dit-v2.safetensors' } },
  @{ id='sdxl'; missing=!$s.sdxl; run={ Model 'https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0/resolve/main/sd_xl_base_1.0.safetensors' 'sd_xl_base_1.0.safetensors' } },
  @{ id='u2net'; missing=!$s.u2net; run={ Download 'https://github.com/danielgatis/rembg/releases/download/v0.0.0/u2net.onnx' (Join-Path $Root 'data\models\background\models\u2net\u2net.onnx') } }
)
$ids = if ($Only) { @($Only | ForEach-Object { $_ -split ',' } | ForEach-Object { $_.Trim() }) } else { @() }
$plan = @($items | Where-Object { ($Only -and $ids -contains $_.id) -or (!$Only -and $_.missing) })
foreach ($item in $plan) {
  Title ('AMD component: ' + $item.id)
  if ($DryRun) { Say 'dry run - skipped'; continue }
  & $item.run
}
if (!$DryRun) {
  if ($s.comfy -and !$Cfg.comfyDir) { $Cfg.comfyDir=$s.comfy }
  if ($s.blender -and !$Cfg.blender) { $Cfg.blender=$s.blender }
  if ($s.node -and !$Cfg.node) { $Cfg.node=$s.node }
  SaveCfg
  Shortcut
  Say 'AMD installation finished.' 'Green'
}
