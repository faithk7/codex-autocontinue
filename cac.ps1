# cac — short alias for codex-autocontinue (Windows).
# Forwards to codex-autocontinue.ps1 next to it; the repo dir is on the user
# PATH after install, so `cac <command>` works in a new PowerShell session.
$ErrorActionPreference = "Stop"
& (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) "codex-autocontinue.ps1") @args
exit $LASTEXITCODE
