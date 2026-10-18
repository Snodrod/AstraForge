# AstraForge - VFX texture generators (the "VFX" tab).
# Downloads the free generators listed in server\vfx-tools-catalog.json from their official sites into the tools folder.
# The programs themselves are not part of AstraForge: each has its own license (see the catalog and THIRD_PARTY_NOTICES.md).
#   -Root <folder>  where to install; default is $env:ASTRA_VFX_TOOLS or C:\AstraForge\tools\vfx
#   -Only a,b       only these tools (ids from the catalog)
#   -DryRun         show the plan without downloading anything
# Installers (PixaFlux, KumoWorks, Slate) are only downloaded; the user runs them with the "Install" button in the tab.
# Anything already downloaded is skipped, so the script can be run again. No administrator rights needed.
param([string]$Root, [string[]]$Only, [switch]$DryRun)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$App = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (!$Root) { $Root = if ($env:ASTRA_VFX_TOOLS) { $env:ASTRA_VFX_TOOLS } else { 'C:\AstraForge\tools\vfx' } }
# when started through -File the list "a,b" arrives as a single string
$Only = @($Only | ForEach-Object { $_ -split ',' } | Where-Object { $_ })
$Catalog = Get-Content -LiteralPath (Join-Path $App 'server\vfx-tools-catalog.json') -Raw -Encoding UTF8 | ConvertFrom-Json
# the system curl and tar of Windows 10/11 (tar from Git or MSYS does not understand zip and paths with "C:")
$Curl = Join-Path $env:SystemRoot 'System32\curl.exe'
if (!(Test-Path -LiteralPath $Curl)) { $Curl = 'curl.exe' }
$Tar = Join-Path $env:SystemRoot 'System32\tar.exe'
if (!(Test-Path -LiteralPath $Tar)) { $Tar = $null }

function Say($t, $c = 'Gray') { Write-Host $t -ForegroundColor $c }
function Native([scriptblock]$b) { $ErrorActionPreference = 'Continue'; & $b }
function Inside($rel) {
  $full = [IO.Path]::GetFullPath((Join-Path $Root $rel))
  if (!$full.StartsWith([IO.Path]::GetFullPath($Root), [StringComparison]::OrdinalIgnoreCase)) { throw "Path outside the tools folder: $rel" }
  return $full
}
function Download($url, $dst) {
  New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
  if (Test-Path -LiteralPath $dst) { Say ('    already downloaded: ' + (Split-Path $dst -Leaf)); return }
  Say ('    downloading ' + $url)
  Native { & $Curl -L --fail --retry 5 --retry-delay 3 -C - -# -A 'Mozilla/5.0 AstraForge' -o ($dst + '.part') $url }
  if ($LASTEXITCODE -ne 0) { throw "Download failed: $url (code $LASTEXITCODE). Run the script again - the download will resume." }
  Move-Item -LiteralPath ($dst + '.part') -Destination $dst -Force
}
# unpack a zip into $dst; $strip is the top-level folder inside the archive, its contents are put directly into $dst
function Unzip($zip, $dst, $strip) {
  $tmp = Join-Path $Root ('_unzip_' + [guid]::NewGuid().ToString('N').Substring(0, 8))
  New-Item -ItemType Directory -Force -Path $tmp | Out-Null
  try {
    $done = $false
    if ($Tar) { Native { & $Tar -xf $zip -C $tmp }; $done = $LASTEXITCODE -eq 0 }
    if (!$done) { Expand-Archive -LiteralPath $zip -DestinationPath $tmp -Force }
    $src = if ($strip) { Join-Path $tmp $strip } else { $tmp }
    if (!(Test-Path -LiteralPath $src)) { throw "The archive has no folder $strip" }
    New-Item -ItemType Directory -Force -Path $dst | Out-Null
    Get-ChildItem -LiteralPath $src -Force | Move-Item -Destination $dst -Force
  } finally { Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue }
}
function Filled($dir) { (Test-Path -LiteralPath $dir) -and @(Get-ChildItem -LiteralPath $dir -Force -ErrorAction SilentlyContinue).Count -gt 0 }
# url + file | url + unzip (+ strip)
function Fetch($d) {
  if ($d.unzip) {
    $dst = Inside $d.unzip
    $leaf = ($d.url -split '/')[-1].Split('?')[0]
    if ($d.unzip -eq 'downloads') { $probe = Inside ('downloads\' + [IO.Path]::GetFileNameWithoutExtension($leaf) + '.exe'); if (Test-Path -LiteralPath $probe) { Say ('    already present: ' + (Split-Path $probe -Leaf)); return } }
    elseif (Filled $dst) { Say ('    already unpacked: ' + $d.unzip); return }
    $zip = Inside ('downloads\' + $leaf)
    Download $d.url $zip
    Unzip $zip $dst $d.strip
    Remove-Item -LiteralPath $zip -Force
  } else { Download $d.url (Inside $d.file) }
}
# sources from GitHub without git: the branch archive
function GitHub($d) {
  $dst = Inside $d.dir
  if (Filled $dst) { Say ('    already present: ' + $d.dir); return }
  $repo = ($d.git -replace '^https://github.com/', '').TrimEnd('/')
  $branch = if ($d.branch) { $d.branch } else { 'main' }
  $zip = Inside ('downloads\' + ($repo -replace '/', '_') + '.zip')
  Download "https://github.com/$repo/archive/refs/heads/$branch.zip" $zip
  Unzip $zip $dst ((($repo -split '/')[-1]) + '-' + $branch)
  Remove-Item -LiteralPath $zip -Force
}
# DisplacementX is built from source (Astro): needs Node.js with npm
function BuildAstro($d) {
  $out = Inside $d.out
  if (Test-Path -LiteralPath (Join-Path $out 'index.html')) { Say ('    already built: ' + $d.out); return }
  $npm = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
  if (!$npm) { Say '    npm not found - build skipped (install Node.js and run the script again).' 'Yellow'; return }
  $src = Inside $d.dir
  $cfg = Join-Path $src 'astro.config.mjs'
  $text = Get-Content -LiteralPath $cfg -Raw -Encoding UTF8
  if ($text -notmatch 'base:') { [IO.File]::WriteAllText($cfg, ($text -replace 'integrations:', ("base: '" + $d.base + "',`n  integrations:")), (New-Object Text.UTF8Encoding $false)) }
  Push-Location $src
  try {
    Native { & $npm ci --no-audit --no-fund }; if ($LASTEXITCODE -ne 0) { throw 'npm ci for DisplacementX failed' }
    Native { & $npm exec -- astro build }; if ($LASTEXITCODE -ne 0) { throw 'The DisplacementX build failed' }
  } finally { Pop-Location }
  if (Test-Path -LiteralPath $out) { Remove-Item -LiteralPath $out -Recurse -Force }
  Move-Item -LiteralPath (Join-Path $src 'dist') -Destination $out
  Remove-Item -LiteralPath (Join-Path $src 'node_modules') -Recurse -Force -ErrorAction SilentlyContinue
}
# a Material Maker preset from materialmaker.org (CC0): the API returns JSON, the graph itself is in the json field
function MaterialMaker($d) {
  $dst = Inside $d.file
  if (Test-Path -LiteralPath $dst) { Say ('    already downloaded: ' + (Split-Path $dst -Leaf)); return }
  $m = Invoke-RestMethod -Uri ('https://www.materialmaker.org/api/getMaterial?id=' + $d.materialMaker) -TimeoutSec 60
  $json = if ($m.json -is [string]) { $m.json } else { $m.json | ConvertTo-Json -Depth 100 -Compress }
  New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
  [IO.File]::WriteAllText($dst, $json, (New-Object Text.UTF8Encoding $false))
}

Say ("VFX texture generators -> $Root") 'Cyan'
New-Item -ItemType Directory -Force -Path (Join-Path $Root 'downloads'), (Join-Path $Root 'textures') | Out-Null
$failed = @()
foreach ($t in $Catalog.tools) {
  $d = $t.download
  if (!$d) { continue }
  if ($Only -and ($Only -notcontains $t.id)) { continue }
  Say ('  ' + $t.name + '  (' + $t.license + ')') 'White'
  if ($DryRun) { Say ('    plan: ' + $(if ($d.url) { $d.url } elseif ($d.git) { $d.git } elseif ($d.manual) { 'manual: ' + $d.manual } else { 'materialmaker.org #' + $d.materialMaker })); continue }
  try {
    if ($d.manual) {
      $dst = Inside $d.unzip
      $zip = Get-ChildItem -LiteralPath (Join-Path $Root 'downloads') -Filter '*Win*.zip' -ErrorAction SilentlyContinue | Where-Object { $_.Name -like '*Volume*' } | Select-Object -First 1
      if (Filled $dst) { Say ('    already unpacked: ' + $d.unzip) }
      elseif ($zip) { Unzip $zip.FullName $dst $d.strip; Say ('    unpacked from ' + $zip.Name) }
      else { Say ('    download manually: ' + $d.manual) 'Yellow'; Say ('    ' + $d.note) 'Yellow' }
    }
    elseif ($d.git) { GitHub $d; if ($d.build -eq 'astro') { BuildAstro $d } }
    elseif ($d.materialMaker) { MaterialMaker $d }
    else { Fetch $d }
    foreach ($x in @($d.extra)) { if ($x) { Fetch $x } }
  } catch { $failed += $t.id; Say ('    error: ' + $_.Exception.Message) 'Red' }
}
if ($failed.Count) { Say ('Failed: ' + ($failed -join ', ') + '. Run the script again with -Only ' + ($failed -join ',')) 'Yellow'; exit 1 }
Say 'Done. Open the studio, "VFX" tab.' 'Green'
