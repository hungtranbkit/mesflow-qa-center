param([switch]$Foreground)
$ErrorActionPreference = 'Stop'
$app = 'C:\MESFlowQACenter'
$python = Join-Path $app '.venv\Scripts\python.exe'
$agent = Join-Path $app 'agent.py'
$log = Join-Path $app 'qa-center.log'
$pidFile = Join-Path $app 'qa-center.pid'
$env:PYTHONUTF8='1'
$env:PYTHONIOENCODING='utf-8'
$env:PLAYWRIGHT_BROWSERS_PATH = Join-Path $app 'ms-playwright'
$env:MESFLOW_QA_HOST='127.0.0.1'
$env:MESFLOW_QA_PORT='8095'
if (!(Test-Path -LiteralPath $python)) { throw "Missing Python: $python" }
if (!(Test-Path -LiteralPath $agent)) { throw "Missing agent.py: $agent" }
Set-Location $app
# Stop stale instance started from this install dir.
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and $_.CommandLine -like '*C:\MESFlowQACenter*agent.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Milliseconds 300
if ($Foreground) {
  & $python -u $agent 2>&1 | Tee-Object -FilePath $log -Append
  exit $LASTEXITCODE
}
$p = Start-Process -FilePath $python -ArgumentList @('-u', $agent) -WorkingDirectory $app -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError (Join-Path $app 'qa-center-error.log') -PassThru
Set-Content -LiteralPath $pidFile -Value $p.Id -Encoding ascii
Start-Sleep -Seconds 2
if ($p.HasExited) {
  Write-Host "QA process exited immediately. ExitCode=$($p.ExitCode)"
  if (Test-Path (Join-Path $app 'qa-center-error.log')) { Get-Content (Join-Path $app 'qa-center-error.log') -Tail 80 }
  exit 2
}
try {
  $r = Invoke-RestMethod -TimeoutSec 4 'http://127.0.0.1:8095/api/version'
  Write-Host ("ONLINE - version " + $r.version + " PID=" + $p.Id)
  exit 0
} catch {
  Write-Host ("PROCESS RUNNING PID=" + $p.Id + " but API not ready yet")
  exit 0
}
