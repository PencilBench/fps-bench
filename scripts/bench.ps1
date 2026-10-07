# Windows entry point: locate an interpreter that has fpsbench installed and
# hand the arguments over unchanged.
#
# Usage: scripts\bench.ps1 doctor | record ... | bench ... | pick-region | dry-run
[CmdletBinding()]
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Args
)

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)

$venvPython = Join-Path $root '.venv\Scripts\python.exe'
if (Test-Path $venvPython) {
    $python = $venvPython
} else {
    $python = (Get-Command py -ErrorAction SilentlyContinue)
    if ($python) {
        & $python.Source -3 -m fpsbench @Args
        exit $LASTEXITCODE
    }
    $python = (Get-Command python -ErrorAction SilentlyContinue)
    if (-not $python) {
        Write-Error 'no Python found: install Python 3.10+ and run: py -m pip install -e ".[windows]"'
        exit 1
    }
    $python = $python.Source
}

& $python -m fpsbench @Args
exit $LASTEXITCODE
