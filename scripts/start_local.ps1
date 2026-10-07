param([string]$RuntimeDir = 'data\native_runtime', [switch]$BackendOnly)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$runtimePath = [System.IO.Path]::GetFullPath((Join-Path $projectRoot $RuntimeDir))
$logsPath = Join-Path $projectRoot 'data\local_services'
New-Item -ItemType Directory -Force -Path $logsPath | Out-Null
$env:DATA_DIR = $runtimePath
$env:CATALOG_SOURCE = 'cj'
$env:CJ_CATALOG_PATH = Join-Path $projectRoot 'data\cj_published\cj_catalog.sqlite3'
$env:QUEUE_ENABLED = '0'
$env:REDIS_URL = ''
$env:SEMANTIC_CACHE_ENABLED = '0'
$env:PYTHONIOENCODING = 'utf-8'
$env:PYTHONUNBUFFERED = '1'
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
$backend = Start-Process -FilePath $pythonPath -ArgumentList @('-m','uvicorn','app.presentation.server:app','--host','127.0.0.1','--port','8000') `
    -WorkingDirectory $projectRoot -WindowStyle Hidden -PassThru `
    -RedirectStandardOutput (Join-Path $logsPath 'backend.stdout.log') -RedirectStandardError (Join-Path $logsPath 'backend.stderr.log')
if (-not $BackendOnly) {
    $nodePath = (Get-Command node.exe).Source
    $vitePath = Join-Path $projectRoot 'frontend\node_modules\vite\bin\vite.js'
    $frontend = Start-Process -FilePath $nodePath -ArgumentList @(('"' + $vitePath + '"'),'--host','127.0.0.1','--port','5173','--strictPort') `
        -WorkingDirectory (Join-Path $projectRoot 'frontend') -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput (Join-Path $logsPath 'frontend.stdout.log') -RedirectStandardError (Join-Path $logsPath 'frontend.stderr.log')
    $frontendPid = $frontend.Id
} else {
    $previous = Get-Content -LiteralPath (Join-Path $logsPath 'processes.json') | ConvertFrom-Json
    # Only reuse the recorded pid while it is still alive, otherwise a stale id
    # makes the summary claim a frontend that no longer exists.
    $frontendPid = 0
    if ($previous.frontendPid -and (Get-Process -Id $previous.frontendPid -ErrorAction SilentlyContinue)) {
        $frontendPid = $previous.frontendPid
    }
}
@{ backendLauncherPid = $backend.Id; frontendPid = $frontendPid; runtimeDir = $runtimePath } | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $logsPath 'processes.json')
Write-Output ('Native services started: backend=' + $backend.Id + ', frontend=' + $frontendPid)
