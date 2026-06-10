# keep_alive.ps1 — Keeps the ISO 20022 backend running.
# Checks every 60 seconds; restarts the server if it is not running.

$PythonExe  = "C:\Users\HP\AppData\Local\Programs\Python\Python314\python.exe"
$RunScript  = "C:\Users\HP\Documents\ISO20022 Validator new\iso20022generatorbackend\run.py"
$WorkingDir = "C:\Users\HP\Documents\ISO20022 Validator new\iso20022generatorbackend"
$LogFile    = "$WorkingDir\keep_alive.log"
$Port       = 8001

function Write-Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    "$ts  $msg" | Tee-Object -FilePath $LogFile -Append | Write-Host
}

Write-Log "Keep-alive watcher started."

while ($true) {
    # Check if server is responding on the configured port
    $alive = $false
    try {
        $resp = Invoke-WebRequest -Uri "http://localhost:$Port/health" `
                                  -UseBasicParsing -TimeoutSec 5 -ErrorAction Stop
        if ($resp.StatusCode -lt 500) { $alive = $true }
    } catch {
        # /health may not exist — fall back to checking the process
    }

    if (-not $alive) {
        # Check if any python process is running run.py
        $procs = Get-Process python* -ErrorAction SilentlyContinue |
                 Where-Object { $_.CommandLine -like "*run.py*" }
        if ($procs) { $alive = $true }
    }

    if (-not $alive) {
        Write-Log "Server not detected — starting..."
        Start-Process -FilePath $PythonExe `
                      -ArgumentList "`"$RunScript`"" `
                      -WorkingDirectory $WorkingDir `
                      -WindowStyle Hidden `
                      -RedirectStandardOutput "$WorkingDir\server_stdout.log" `
                      -RedirectStandardError  "$WorkingDir\server_stderr.log"
        Write-Log "Server process launched."
    } else {
        Write-Log "Server is running — OK."
    }

    Start-Sleep -Seconds 60
}
