# codex-autocontinue — developer control script (Windows).
# Same interface as the bash wrapper: install/uninstall/start/stop/status/logs.
# Service manager: Task Scheduler (logon trigger + restart on failure).
param([Parameter(Position=0)][string]$Command = "")

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $MyInvocation.MyCommand.Path
$Daemon = Join-Path $Repo "codex-autocontinue.py"
$Log = Join-Path $Repo "watcher.log"
$TaskName = "codex-autocontinue"

function Find-Python {
    $py = Get-Command python -ErrorAction SilentlyContinue
    if (-not $py) { $py = Get-Command py -ErrorAction SilentlyContinue }
    if (-not $py) { throw "python not found on PATH" }
    return $py.Source
}

function Add-RepoToPath {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -notlike "*$Repo*") {
        [Environment]::SetEnvironmentVariable("Path", "$userPath;$Repo", "User")
        Write-Host "added $Repo to user PATH (open a new shell to pick it up)"
    } else {
        Write-Host "repo already on user PATH"
    }
}

function Remove-RepoFromPath {
    $userPath = [Environment]::GetEnvironmentVariable("Path", "User")
    if ($userPath -like "*$Repo*") {
        $newPath = ($userPath -split ";" | Where-Object { $_ -ne $Repo }) -join ";"
        [Environment]::SetEnvironmentVariable("Path", $newPath, "User")
        Write-Host "removed $Repo from user PATH"
    }
}

switch ($Command) {
    "install" {
        $python = Find-Python
        $action = New-ScheduledTaskAction -Execute $python -Argument "`"$Daemon`"" -WorkingDirectory $Repo
        $trigger = New-ScheduledTaskTrigger -AtLogOn
        $settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Force | Out-Null
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "scheduled task installed and started: $TaskName"
        Add-RepoToPath
        $dry = $true
        try { $dry = (Get-Content (Join-Path $Repo "config.json") -Raw | ConvertFrom-Json).dry_run } catch { $dry = $true }
        if ($null -eq $dry) { $dry = $true }
        Write-Host ""
        Write-Host "dry_run is $dry (see $Repo\config.json)"
        if ($dry) {
            Write-Host "the watcher will detect events and log what it WOULD do, injecting nothing."
            Write-Host "set `"dry_run`": false in config.json and run: codex-autocontinue start"
        }
    }
    "uninstall" {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        Remove-RepoFromPath
        Write-Host "uninstalled: scheduled task removed, PATH entry removed."
        Write-Host "repo left in place at $Repo (delete manually if unwanted)."
    }
    "start" {
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "started"
    }
    "stop" {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Write-Host "stopped (still installed; will start again at logon)"
    }
    "status" {
        $task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        if ($task) { Write-Host "task state: $($task.State)" } else { Write-Host "not installed" }
        if (Test-Path $Log) {
            $n = (Select-String -Path $Log -Pattern "auto-continue injected").Count
            Write-Host "auto-continues so far: $n"
            Write-Host "last log lines:"
            Get-Content $Log -Tail 3 | ForEach-Object { "  $_" }
        }
    }
    "logs" {
        if (-not (Test-Path $Log)) { New-Item $Log | Out-Null }
        Get-Content $Log -Tail 50 -Wait
    }
    default {
        Write-Host "usage: codex-autocontinue.ps1 {install|uninstall|start|stop|status|logs}"
        exit 1
    }
}
