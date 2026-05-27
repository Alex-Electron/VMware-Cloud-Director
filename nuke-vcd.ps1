param (
    [Parameter(ValueFromRemainingArguments=$true)]$PassThruArgs
)

$ErrorActionPreference = "Stop"

$ScriptDir = $PSScriptRoot
Set-Location -Path $ScriptDir

$PyScript = Join-Path $ScriptDir "nuke_vcd_tenant.py"
$VenvDir = Join-Path $ScriptDir ".venv"
$ConfigFile = Join-Path $ScriptDir "nuke_vcd.conf"
$ConfigExample = Join-Path $ScriptDir "nuke_vcd.conf.example"

if (-Not (Test-Path $PyScript)) {
    Write-Error "`[X`] `$PyScript not found"
    exit 1
}

if (-Not (Test-Path $ConfigFile)) {
    if (Test-Path $ConfigExample) {
        Write-Host '[!] $ConfigFile is missing — copy the template and fill it in:' -ForegroundColor Yellow
        Write-Host '    Copy-Item ''$ConfigExample'' ''$ConfigFile''' -ForegroundColor Yellow
        Write-Host '    notepad ''$ConfigFile''' -ForegroundColor Yellow
    } else {
        Write-Error "`[X`] No $ConfigFile and no .example template either"
    }
    exit 2
}

$PythonBin = if ($env:PYTHON_BIN) { $env:PYTHON_BIN } else { "python" }

if (-Not (Get-Command $PythonBin -ErrorAction SilentlyContinue)) {
    Write-Error "`[X`] `$PythonBin not in PATH"
    exit 3
}

$UseVenv = if ($env:USE_VENV) { $env:USE_VENV } else { "auto" }

if ($UseVenv -eq "auto") {
    # Check if requests and urllib3 are installed globally
    & $PythonBin -c "import requests, urllib3" 2>$null
    if ($LASTEXITCODE -eq 0) {
        $UseVenv = "0"
    } else {
        $UseVenv = "1"
    }
}

if ($UseVenv -eq "1") {
    if (-Not (Test-Path $VenvDir)) {
        Write-Host ('[*] Creating venv in {0}...' -f $VenvDir)
        & $PythonBin -m venv $VenvDir
        $VenvPython = Join-Path $VenvDir 'Scripts\python.exe'
        & $VenvPython -m pip install --quiet --upgrade pip
        & $VenvPython -m pip install --quiet requests urllib3
    }
    $PythonBin = Join-Path $VenvDir 'Scripts\python.exe'
}

$env:VCD_CONFIG = $ConfigFile

# Execute the python script with the passed arguments
& $PythonBin $PyScript @PassThruArgs
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}
