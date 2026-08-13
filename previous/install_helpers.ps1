param(
    [Parameter(Mandatory=$true)][ValidateSet('stop-old','copy-source','register-task')][string]$Action,
    [string]$Destination,
    [string]$TaskName = 'MESFlow QA Center'
)
$ErrorActionPreference = 'Stop'

switch ($Action) {
    'stop-old' {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
        $procs = Get-CimInstance Win32_Process | Where-Object {
            $_.CommandLine -and $_.CommandLine -like '*C:\MESFlowQACenter*agent.py*'
        }
        foreach ($p in $procs) {
            Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        }
        exit 0
    }

    'copy-source' {
        if ([string]::IsNullOrWhiteSpace($Destination)) { throw 'Destination required' }
        # Source is always the folder containing this helper. Never pass %~dp0 from CMD:
        # a trailing backslash in a quoted command-line argument can corrupt PowerShell parsing.
        $src = $PSScriptRoot.TrimEnd([char]92)
        if (-not (Test-Path -LiteralPath $src -PathType Container)) {
            throw "Source folder not found: [$src]"
        }

        New-Item -ItemType Directory -Force -Path $Destination | Out-Null
        $dstItem = Get-Item -LiteralPath $Destination -Force
        $dst = $dstItem.FullName.TrimEnd([char]92)

        if ([string]::Equals($src, $dst, [System.StringComparison]::OrdinalIgnoreCase)) {
            Write-Host 'Source already equals install directory; copy skipped.'
            exit 0
        }

        $exclude = @(
            '.venv','reports','backups','.pytest_cache','__pycache__',
            'config.json','qa-center.log','ms-playwright'
        )

        foreach ($item in (Get-ChildItem -LiteralPath $src -Force)) {
            if ($exclude -contains $item.Name) { continue }
            Copy-Item -LiteralPath $item.FullName -Destination $dst -Recurse -Force -ErrorAction Stop
        }

        foreach ($required in @('agent.py','requirements.txt','VERSION','install_helpers.ps1','start_qa.ps1')) {
            $requiredPath = Join-Path -Path $dst -ChildPath $required
            if (-not (Test-Path -LiteralPath $requiredPath)) {
                throw "Required file missing after copy: $requiredPath"
            }
        }
        Write-Host "Copy source OK: [$src] -> [$dst]"
        exit 0
    }

    'register-task' {
        $launcher = 'C:\MESFlowQACenter\run_qa.bat'
        if (-not (Test-Path -LiteralPath $launcher)) { throw "Launcher not found: $launcher" }
        $taskAction = New-ScheduledTaskAction -Execute 'cmd.exe' -Argument ('/d /c ""{0}""' -f $launcher)
        $trigger = New-ScheduledTaskTrigger -AtStartup
        $principal = New-ScheduledTaskPrincipal -UserId 'SYSTEM' -LogonType ServiceAccount -RunLevel Highest
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero)
        Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
        Start-ScheduledTask -TaskName $TaskName
        exit 0
    }
}
