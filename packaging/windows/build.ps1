param([switch]$Official, [switch]$CheckConfiguration)
$ErrorActionPreference = 'Stop'
$Root = (Resolve-Path "$PSScriptRoot\..\..").Path
Set-Location $Root
$Python = if ($env:P011_BUILD_PYTHON) { $env:P011_BUILD_PYTHON } else { 'python' }
$ConfigurationArgs = @('packaging/check_release_configuration.py')
if ($Official) { $ConfigurationArgs += '--official' }
& $Python @ConfigurationArgs
if ($LASTEXITCODE -ne 0) { throw 'Release service configuration failed validation' }
if ($CheckConfiguration) { exit 0 }
foreach ($name in @('P011_FFMPEG','P011_FFPROBE','PLAYWRIGHT_BROWSERS_PATH','P011_WHISPER_DIR','P011_VC_RUNTIME_DIR')) {
  $value = [Environment]::GetEnvironmentVariable($name)
  if (-not $value -or -not (Test-Path $value)) { throw "Build dependency missing: $name" }
}
& $Python packaging/recognition_bundle.py prepare --platform windows_x64 --source-root $env:P011_WHISPER_DIR --vc-root $env:P011_VC_RUNTIME_DIR --output-root packaging/resources
if ($LASTEXITCODE -ne 0) { throw 'Recognition input validation failed' }
& $Python -m PyInstaller --noconfirm --clean packaging/windows/yijianjifa.spec
if ($LASTEXITCODE -ne 0) { throw 'Windows build failed' }
& $Python @ConfigurationArgs --frozen-root 'dist/一剪即发/_internal'
if ($LASTEXITCODE -ne 0) { throw 'Frozen service configuration does not match this build' }
& $Python packaging/recognition_bundle.py finalize --platform windows_x64 --output-root packaging/resources --frozen-root 'dist/一剪即发/_internal'
if ($LASTEXITCODE -ne 0) { throw 'Frozen recognition inventory failed' }
& $Python packaging/windows/trim_bundle.py --root 'dist/一剪即发' --report 'dist/一剪即发/trim-report.json'
if ($LASTEXITCODE -ne 0) { throw 'Reviewed Windows slimming failed; no package will be delivered' }
Write-Host 'Built dist/一剪即发/一剪即发.exe; Windows device acceptance remains separate'
