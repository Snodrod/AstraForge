# AstraForge: MV-Adapter (local views from a single image) in the studio's own ComfyUI.
# The file is dot-sourced from installer\Install.ps1 and uses its helpers: Say, Download, Unzip, Run, Native, Uv,
# FileOk, DownloadChecked, HfTree and the variables $Root, $Dl. What is installed and how: docs\forge\views_install.md.
#
#   node      huanngzh/ComfyUI-MVAdapter (Apache-2.0), pinned commit, plus the AstraForge patches from installer\mvadapter\patch
#   weights   huanngzh/mv-adapter -> mvadapter_i2mv_sdxl.safetensors (3.6 GB, Apache-2.0) -> data\models\mvadapter
#   Python packages the node lacks are installed INTO THE SAME environment WITHOUT upgrading the ones already there (constraints = the current freeze)
#   SDXL configs (without weights) are built by scripts\views\make_sdxl_config.py; SDXL itself (sd_xl_base_1.0.safetensors) is required.

$MvSrc = $PSScriptRoot
$MvMan = Get-Content -LiteralPath (Join-Path $MvSrc 'manifest.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$MvPkgBytes = [long]0.2e9   # estimate of the Python packages and node archive download

function MvPaths() {
  return @{
    node = (Join-Path $Root 'data\runtime\custom_nodes\ComfyUI-MVAdapter')
    weights = (Join-Path $Root ('data\models\mvadapter\' + $MvMan.files[0].path))
    config = (Join-Path $Root 'data\models\mvadapter\sdxl-config')
  }
}

function MvMissingModules($comfy) {
  # which of the Python modules the node needs are not found in the ComfyUI environment (checked without importing, fast)
  $py = Join-Path $comfy '.venv\Scripts\python.exe'
  if (!(Test-Path -LiteralPath $py)) { return @($MvMan.modules.PSObject.Properties.Name) }
  $names = ($MvMan.modules.PSObject.Properties.Name | ForEach-Object { "'" + $_ + "'" }) -join ', '
  $out = Native { & $py -c "import importlib.util as u; print(' '.join(m for m in [$names] if u.find_spec(m) is None))" 2>$null }
  if ($LASTEXITCODE -ne 0) { return @($MvMan.modules.PSObject.Properties.Name) }
  return @(("$out").Trim() -split '\s+' | Where-Object { $_ })
}

function MvScan($s) {
  $p = MvPaths
  $r = @{ nodeDir = $p.node }
  $r.node = (Test-Path -LiteralPath (Join-Path $p.node 'astra_views.py')) -and (Test-Path -LiteralPath (Join-Path $p.node 'utils.py')) -and [bool](Select-String -LiteralPath (Join-Path $p.node 'utils.py') -SimpleMatch 'local_sdxl_config' -Quiet)
  $r.weights = FileOk $p.weights ([long]$MvMan.files[0].size) $null
  $r.config = Test-Path -LiteralPath (Join-Path $p.config 'model_index.json')
  $r.missingModules = @()
  if ($s.comfy) { $r.missingModules = MvMissingModules $s.comfy }
  $r.pkgs = [bool]$s.comfy -and ($r.missingModules.Count -eq 0)
  $r.ready = $r.node -and $r.weights -and $r.config -and $r.pkgs
  $r.offer = $true; $r.reason = $null; $r.warn = $null
  if (!$r.ready) {
    if (!$s.gpu) { $r.offer = $false; $r.reason = 'an NVIDIA GPU is required' }
    elseif ($s.vram -lt 7900) { $r.offer = $false; $r.reason = ('video memory is ' + [math]::Round($s.vram / 1024) + ' GB, at least 8 GB is required') }
    elseif ($s.vram -lt 10000) { $r.warn = ('video memory is ' + [math]::Round($s.vram / 1024) + ' GB: with 8 GB the views are computed only at 512 px and memory may run out, 10 GB or more is comfortable') }
  }
  # how much is still to be downloaded
  $b = 0L
  if (!$r.weights) { $b += [long]$MvMan.files[0].size }
  if (!$r.node -or !$r.pkgs) { $b += $MvPkgBytes }
  $r.bytes = $b
  return $r
}

function MvInstallNode() {
  $p = MvPaths
  $n = $MvMan.node
  if ((Test-Path -LiteralPath $p.node) -and !(Test-Path -LiteralPath (Join-Path $p.node 'mvadapter'))) { Remove-Item -LiteralPath $p.node -Recurse -Force }   # incomplete folder
  if (!(Test-Path -LiteralPath (Join-Path $p.node 'mvadapter'))) {
    $zip = Join-Path $Dl ('ComfyUI-MVAdapter-' + $n.commit.Substring(0, 8) + '.zip')
    Download $n.url $zip
    $parent = Split-Path $p.node
    New-Item -ItemType Directory -Force -Path $parent | Out-Null
    $tmp = Join-Path $parent '_unpack_mvadapter'
    if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Recurse -Force }
    try { Unzip $zip $tmp } catch { Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue; throw }
    $top = Get-ChildItem -LiteralPath $tmp -Directory | Select-Object -First 1
    if (!$top) { Remove-Item -LiteralPath $zip -Force -ErrorAction SilentlyContinue; throw 'The MV-Adapter node archive is empty or corrupt - deleted, run the installer again.' }
    if (Test-Path -LiteralPath $p.node) { Remove-Item -LiteralPath $p.node -Recurse -Force }
    Move-Item -LiteralPath $top.FullName -Destination $p.node
    Remove-Item -LiteralPath $tmp -Recurse -Force
  }
  # AstraForge patches on top of the original (list in NOTICE-ASTRAFORGE.md): local SDXL configs, VAE slicing for diffusers 0.40, the AstraMVAdapterViews node
  foreach ($d in $n.removeDirs) { $x = Join-Path $p.node $d; if (Test-Path -LiteralPath $x) { Remove-Item -LiteralPath $x -Recurse -Force } }
  Copy-Item -Path (Join-Path (Join-Path $MvSrc $n.patchDir) '*') -Destination $p.node -Force
  if (!(Select-String -LiteralPath (Join-Path $p.node 'utils.py') -SimpleMatch 'local_sdxl_config' -Quiet)) { throw 'The AstraForge patches were not applied to the MV-Adapter node.' }
  Say ('    node ComfyUI-MVAdapter ' + $n.version + ' (' + $n.commit.Substring(0, 8) + ') + AstraForge patches: ' + $p.node)
}

function MvFreeze($uv, $py) {
  $lines = Native { & $uv pip freeze --python $py 2>$null }
  $h = @{}
  foreach ($l in @($lines)) { if ("$l" -match '^([A-Za-z0-9_.\-]+)==(\S+)$') { $h[($Matches[1].ToLower() -replace '[-_.]+', '-')] = $Matches[2] } }
  return $h
}

function MvInstallPackages($comfy) {
  $py = Join-Path $comfy '.venv\Scripts\python.exe'
  if (!(Test-Path -LiteralPath $py)) { throw 'The studio ComfyUI has no Python environment - install ComfyUI first.' }
  $missing = MvMissingModules $comfy
  if ($missing.Count -eq 0) { Say '    the required Python packages are already installed'; return }
  $uv = Uv
  $before = MvFreeze $uv $py
  $cons = Join-Path $Dl 'mvadapter-constraints.txt'
  New-Item -ItemType Directory -Force -Path $Dl | Out-Null
  (($before.GetEnumerator() | Sort-Object Name | ForEach-Object { $_.Name + '==' + $_.Value }) -join "`n") | Set-Content -LiteralPath $cons -Encoding ASCII
  $pinned = @($missing | ForEach-Object { $MvMan.modules.PSObject.Properties[$_].Value })
  $plain = @($pinned | ForEach-Object { ($_ -split '==')[0] })
  Say ('    missing packages: ' + ($pinned -join ', ') + ' (already installed ones will not be upgraded)')
  try { Run $uv (@('pip', 'install', '--python', $py, '-c', $cons) + $pinned) 'installing Python packages for MV-Adapter' }
  catch { Say '    the pinned versions did not fit, trying unpinned ones (still without upgrading installed packages)' 'Yellow'; Run $uv (@('pip', 'install', '--python', $py, '-c', $cons) + $plain) 'installing Python packages for MV-Adapter (unpinned versions)' }
  $after = MvFreeze $uv $py
  $changed = @($before.Keys | Where-Object { $after[$_] -ne $before[$_] })
  if ($changed.Count) { Say ('    WARNING: versions changed: ' + (($changed | ForEach-Object { $_ + ' ' + $before[$_] + ' -> ' + $after[$_] }) -join ', ')) 'Yellow' }
  else { Say ('    packages added: ' + ($after.Count - $before.Count) + ', versions of installed ones unchanged') }
  $still = MvMissingModules $comfy
  if ($still.Count) { throw ('After installation these modules are still not found: ' + ($still -join ', ')) }
}

function MvInstall($s) {
  $p = MvPaths
  Say '  MV-Adapter node'
  MvInstallNode
  Say '  Python packages in the studio ComfyUI environment'
  MvInstallPackages $s.comfy
  Say '  MV-Adapter weights (Hugging Face huanngzh/mv-adapter, Apache-2.0)'
  $f = $MvMan.files[0]
  $size = [long]$f.size; $sha = $f.sha256
  $t = HfTree $MvMan.repo
  if ($t -and $t.ContainsKey($f.path)) { $size = [long]$t[$f.path].size; if ($t[$f.path].sha256) { $sha = $t[$f.path].sha256 } }
  DownloadChecked ('https://huggingface.co/' + $MvMan.repo + '/resolve/main/' + $f.path) $p.weights $size $sha
  Say '  SDXL configs without weights (scripts\views\make_sdxl_config.py)'
  $py = Join-Path $s.comfy '.venv\Scripts\python.exe'
  Run $py @((Join-Path $Root 'scripts\views\make_sdxl_config.py'), $p.config, $s.comfy) 'building data\models\mvadapter\sdxl-config'
  if (!(Test-Path -LiteralPath (Join-Path $p.config 'model_index.json'))) { throw 'make_sdxl_config.py did not create sdxl-config.' }
  Say '    to make the views work, restart the studio (Stop.cmd, then Start.cmd)'
}
