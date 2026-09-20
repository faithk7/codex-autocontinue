# codex-autocontinue — thin shim (Windows).
# All commands are implemented in Python: codex-autocontinue.py dispatches
# subcommands to cli.py. This script only locates python and forwards args.
$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$Daemon = Join-Path $Repo "codex-autocontinue.py"

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) { $python = Get-Command py -ErrorAction SilentlyContinue }
if (-not $python) { Write-Host "python not found on PATH"; exit 1 }

& $python.Source $Daemon @args
exit $LASTEXITCODE
