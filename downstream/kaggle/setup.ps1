param(
    [Parameter(Mandatory=$true)][string]$DataDir,
    [string]$ImportFrom,
    [string]$Uv='uv'
)
$ErrorActionPreference='Stop'
$pluginHome=Join-Path ([IO.Path]::GetFullPath($DataDir)) 'kaggle'
New-Item -ItemType Directory -Force -Path $pluginHome | Out-Null
$env:UV_PYTHON_INSTALL_DIR=Join-Path $pluginHome 'python'
$env:UV_CACHE_DIR=Join-Path $pluginHome 'installer-cache'
$env:TEMP=Join-Path $pluginHome 'tmp'
$env:TMP=$env:TEMP
$env:PYTHONUTF8='1'
New-Item -ItemType Directory -Force -Path $env:TEMP | Out-Null
& $Uv python install 3.12.13 --no-bin
if($LASTEXITCODE){throw 'Kaggle Python installation failed'}
& $Uv venv (Join-Path $pluginHome 'runtime') --python 3.12.13 --managed-python --allow-existing
if($LASTEXITCODE){throw 'Kaggle environment creation failed'}
$pluginPython=Join-Path $pluginHome 'runtime/Scripts/python.exe'
& $Uv pip sync --python $pluginPython (Join-Path $PSScriptRoot 'requirements.txt')
if($LASTEXITCODE){throw 'Kaggle dependencies installation failed'}
if($ImportFrom){
    & $pluginPython (Join-Path $PSScriptRoot 'import_profiles.py') $ImportFrom $pluginHome
    if($LASTEXITCODE){throw 'Kaggle profile import failed'}
}
$currentSid=[Security.Principal.WindowsIdentity]::GetCurrent().User.Value
& icacls.exe $pluginHome /inheritance:r /grant:r "*$($currentSid):(OI)(CI)F" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /Q | Out-Null
if($LASTEXITCODE){throw 'Could not restrict Kaggle private storage permissions'}
& icacls.exe (Join-Path $pluginHome '*') /reset /T /Q | Out-Null
if($LASTEXITCODE){throw 'Could not inherit Kaggle private storage permissions'}
Write-Output 'Kaggle runtime ready. Open Compute settings and enable accounts in the pool.'
