$script:AmdRocmBase = 'https://repo.radeon.com/rocm/windows/rocm-rel-7.2.1'
function Select-AmdGpu([object[]]$Adapters) {
  $pattern = 'AMD Radeon RX (9070 XT|9070|9060 XT|7900 XTX|7700)|AMD Radeon AI PRO R9700|AMD Radeon PRO W7900'
  return $Adapters | Where-Object { $_.Name -match $pattern } | Sort-Object { if ($_.Name -match '9070 XT') { 0 } else { 1 } } | Select-Object -First 1
}
function Get-AmdRocmPackageUrls() {
  return @(
    "$script:AmdRocmBase/rocm_sdk_core-7.2.1-py3-none-win_amd64.whl",
    "$script:AmdRocmBase/rocm_sdk_devel-7.2.1-py3-none-win_amd64.whl",
    "$script:AmdRocmBase/rocm_sdk_libraries_custom-7.2.1-py3-none-win_amd64.whl",
    "$script:AmdRocmBase/rocm-7.2.1.tar.gz"
  )
}
function Get-AmdTorchPackageUrls() {
  return @(
    "$script:AmdRocmBase/torch-2.9.1%2Brocm7.2.1-cp312-cp312-win_amd64.whl",
    "$script:AmdRocmBase/torchaudio-2.9.1%2Brocm7.2.1-cp312-cp312-win_amd64.whl",
    "$script:AmdRocmBase/torchvision-0.24.1%2Brocm7.2.1-cp312-cp312-win_amd64.whl"
  )
}
function Assert-AmdTorch($Python, $ExpectedName) {
  $code = "import torch; assert torch.version.hip, 'ROCm/HIP build required'; assert torch.cuda.is_available(), 'AMD GPU unavailable'; name=torch.cuda.get_device_name(0); print(name); assert '9070 XT' in name, 'wrong GPU: '+name"
  & $Python -c $code
  if ($LASTEXITCODE -ne 0) { throw "ROCm PyTorch validation failed for $ExpectedName" }
}
