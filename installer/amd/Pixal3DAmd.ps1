# Pixal3D/TRELLIS.2 for AMD: Linux ROCm in the project-owned WSL distribution.
# Dot-sourced by Install-AMD.ps1 after Install.ps1 and AmdCore.ps1.
. (Join-Path $PSScriptRoot '..\pixal3d\pixal3d.ps1')

$AmdPixalDistro = 'AstraForge-AMD'
$AmdPixalDir = Join-Path $Rt 'ComfyUI-Pixal3D-AMD'

function ConvertTo-AmdWslPath([string]$value) {
  if ($value -notmatch '^([A-Za-z]):[\\/](.*)$') { throw "An absolute Windows path is required: $value" }
  return '/mnt/' + $Matches[1].ToLower() + '/' + ($Matches[2] -replace '\\', '/')
}

function Assert-AmdPixalEngine($dir) {
  if (!(Test-Path -LiteralPath (Join-Path $dir 'wsl-ready.json'))) {
    throw 'The ROCm WSL engine is not verified. Run the pixal3d component before downloading weights.'
  }
}

function Copy-AmdPixalTools($dir) {
  foreach ($name in 'tools', 'logs', 'cache\torch\hub\checkpoints') {
    New-Item -ItemType Directory -Force -Path (Join-Path $dir $name) | Out-Null
  }
  foreach ($name in 'dinov3_timm_to_hf.py', 'smoke_test.py', 'normalize_seed_mesh.py') {
    Copy-Item -LiteralPath (Join-Path $P3Src $name) -Destination (Join-Path $dir ('tools\' + $name)) -Force
  }
}

function InstallAmdPixal3d() {
  $dir = $AmdPixalDir
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  $script = ConvertTo-AmdWslPath (Join-Path $Root 'scripts\wsl\install-pixal3d-amd.sh')
  Run 'wsl.exe' @('-d', $AmdPixalDistro, '--', 'env', 'MAX_JOBS=8', 'bash', $script) 'Pixal3D Linux ROCm engine and native extensions'
  Assert-AmdPixalEngine $dir
  Copy-AmdPixalTools $dir
  $Cfg.pixal3dDir = $dir
  $Cfg.pixal3dBackend = 'wsl'
  $Cfg.pixal3dWslDistro = $AmdPixalDistro
  SaveCfg
}

function InstallAmdPixal3dWeights() {
  $dir = $AmdPixalDir
  Assert-AmdPixalEngine $dir
  Copy-AmdPixalTools $dir
  $m = $P3Man
  $md = Join-Path $dir 'ComfyUI\models'
  $live = @{}
  foreach ($repo in $m.pixal3d.repo, $m.moge.repo) { $live[$repo] = HfTree $repo }
  Say '  Pixal3D single/multi-view weights and MoGe-2; resumable downloads with SHA-256 checks'
  foreach ($it in (P3WeightItems $dir)) {
    $sha = $it.sha; $size = $it.size
    $listing = $live[$it.repo]
    if ($listing -and $listing.ContainsKey($it.rel)) {
      if ($listing[$it.rel].sha256) { $sha = $listing[$it.rel].sha256 }
      $size = [long]$listing[$it.rel].size
    }
    DownloadChecked $it.url $it.path $size $sha
  }
  foreach ($name in 'ss_dec_conv3d_16l8_fp16.json', 'ss_dec_conv3d_16l8_fp16.safetensors') {
    $src = Join-Path $md ('TencentARC\Pixal3D\ckpts\' + $name)
    $dst = Join-Path $md ('microsoft\TRELLIS-image-large\ckpts\' + $name)
    if (!(Test-Path -LiteralPath $dst)) {
      New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
      Copy-Item -LiteralPath $src -Destination $dst
    }
  }
  New-Item -ItemType Directory -Force -Path (Join-Path $md 'microsoft\TRELLIS.2-4B') | Out-Null

  $hub = Join-Path $dir 'cache\torch\hub'
  $naf = Join-Path $hub 'valeoai_NAF_main'
  if (!(Test-Path -LiteralPath (Join-Path $naf 'hubconf.py'))) {
    New-Item -ItemType Directory -Force -Path $hub | Out-Null
    P3Repo $m.naf.url ('NAF-' + $m.naf.commit.Substring(0, 8)) $naf
  }
  $nafPatch = ConvertTo-AmdWslPath (Join-Path $Root 'scripts\wsl\patch_trellis2_amd.py')
  Run 'wsl.exe' @('-d', $AmdPixalDistro, '--', '/opt/astraforge/pixal3d-venv/bin/python', $nafPatch, '--naf', (ConvertTo-AmdWslPath $naf)) 'NAF PyTorch attention for AMD ROCm'
  $ck = $m.naf.checkpoint
  DownloadChecked $ck.url (Join-Path $hub 'checkpoints\naf_release.pth') ([long]$ck.size) $ck.sha256

  $fb = Join-Path $md 'facebook\dinov3-vitl16-pretrain-lvd1689m'
  if (!(Test-Path -LiteralPath (Join-Path $fb 'model.safetensors'))) {
    Say '  DINOv3 official timm weights and conversion in Linux ROCm Python'
    $td = Join-Path $md 'timm\vit_large_patch16_dinov3.lvd1689m'
    $dtree = HfTree $m.dinov3.repo
    foreach ($f in $m.dinov3.files) {
      $sha = $f.sha256; $size = [long]$f.size
      if ($dtree -and $dtree.ContainsKey($f.path)) {
        if ($dtree[$f.path].sha256) { $sha = $dtree[$f.path].sha256 }
        $size = [long]$dtree[$f.path].size
      }
      DownloadChecked ('https://huggingface.co/' + $m.dinov3.repo + '/resolve/main/' + $f.path) (Join-Path $td $f.path) $size $sha
    }
    $converter = ConvertTo-AmdWslPath (Join-Path $dir 'tools\dinov3_timm_to_hf.py')
    $args = @('-d', $AmdPixalDistro, '--', 'env', 'HF_HUB_OFFLINE=1', '/opt/astraforge/pixal3d-venv/bin/python', '-I', $converter,
      '--timm-dir', (ConvertTo-AmdWslPath $td), '--out-dir', (ConvertTo-AmdWslPath $fb),
      '--report', (ConvertTo-AmdWslPath (Join-Path $dir 'logs\dinov3_parity.json')))
    Run 'wsl.exe' $args 'DINOv3 conversion and numerical parity check'
    if (!(Test-Path -LiteralPath (Join-Path $fb 'model.safetensors'))) { throw 'DINOv3 conversion did not create model.safetensors.' }
    Remove-Item -LiteralPath (Join-Path $td 'model.safetensors') -Force -ErrorAction SilentlyContinue
  }
  $Cfg.pixal3dDir = $dir
  $Cfg.pixal3dBackend = 'wsl'
  $Cfg.pixal3dWslDistro = $AmdPixalDistro
  SaveCfg
}

function TestAmdPixal3d() {
  $dir = $AmdPixalDir
  Assert-AmdPixalEngine $dir
  $runtime = Join-Path $Root 'data\runtime'
  New-Item -ItemType Directory -Force -Path $runtime | Out-Null
  $script = ConvertTo-AmdWslPath (Join-Path $Root 'scripts\wsl\start-pixal3d-amd.sh')
  $temp = ConvertTo-AmdWslPath (Join-Path $runtime 'pixal3d-temp')
  $db = ConvertTo-AmdWslPath (Join-Path $runtime 'pixal3d.db')
  $log = Join-Path $runtime 'pixal3d-installer.log'
  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = 'wsl.exe'
  $psi.Arguments = "-d $AmdPixalDistro -- bash `"$script`" --port 8191 --temp-directory `"$temp`" --database-url `"sqlite:///$db`""
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true
  $psi.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
  $psi.RedirectStandardOutput = $true
  $psi.RedirectStandardError = $true
  $process = New-Object System.Diagnostics.Process
  $process.StartInfo = $psi
  [void]$process.Start()
  $stdout = $process.StandardOutput.ReadToEndAsync()
  $stderr = $process.StandardError.ReadToEndAsync()
  try {
    $ready = $false
    for ($i = 0; $i -lt 90; $i++) {
      try { $null = Invoke-RestMethod -Uri 'http://127.0.0.1:8191/system_stats' -TimeoutSec 2; $ready = $true; break } catch { Start-Sleep -Seconds 2 }
      if ($process.HasExited) { break }
    }
    if (!$ready) { throw "Pixal3D WSL did not start. See $log" }
    $smoke = ConvertTo-AmdWslPath (Join-Path $dir 'tools\smoke_test.py')
    Run 'wsl.exe' @('-d', $AmdPixalDistro, '--', '/opt/astraforge/pixal3d-venv/bin/python', '-I', $smoke) 'Pixal3D node and VRAM smoke test'
  } finally {
    if (!$process.HasExited) {
      Native { & wsl.exe -d $AmdPixalDistro --terminate 2>&1 | Out-Null }
      $process.WaitForExit(15000) | Out-Null
    }
    if (!$process.HasExited) { $process.Kill() }
    [IO.File]::WriteAllText($log, $stdout.Result + $stderr.Result, [Text.Encoding]::UTF8)
    $process.Dispose()
  }
}
