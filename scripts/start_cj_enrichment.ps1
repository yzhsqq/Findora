param([int]$Limit = 0)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$scriptPath = Join-Path $PSScriptRoot 'enrich_cj_catalog.py'
$reportDir = Join-Path $projectRoot 'data\cj_enrichment'
New-Item -ItemType Directory -Force -Path $reportDir | Out-Null
$stopFile = Join-Path $reportDir 'STOP'
if (Test-Path -LiteralPath $stopFile) {
    throw 'STOP file exists. Remove it before restarting the collector.'
}
$workerArguments = @('-u', ('"' + $scriptPath + '"'), '--follow', '--verify-pages', '--refresh-observed')
if ($Limit -gt 0) { $workerArguments += @('--limit', $Limit.ToString()) }
$worker = Start-Process -FilePath $pythonPath -ArgumentList $workerArguments -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $reportDir 'worker.stdout.log') -RedirectStandardError (Join-Path $reportDir 'worker.stderr.log')
$worker.Id | Set-Content -LiteralPath (Join-Path $reportDir 'worker.pid')
Write-Output ('CJ enrichment started; process=' + $worker.Id + '; progress=data/cj_enrichment/progress.json')
