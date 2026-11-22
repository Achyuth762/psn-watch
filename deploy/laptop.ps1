<#
psn-watch laptop helper (Windows).

Watches only the stores that refuse traffic from cloud servers (Amazon, Croma, Play-Asia, MD Computers,
Sheenu, KRG Kart, E2Z, Stan Shop). The Azure VM covers every other store. Sends restock alerts only,
to the same ntfy topic as the VM, and follows the wishlist saved on achyuthus.dev/psn.

Install or update (PowerShell, no admin needed):
  & ([scriptblock]::Create((irm https://raw.githubusercontent.com/Achyuth762/psn-watch/main/deploy/laptop.ps1))) -Topic <your-ntfy-topic>
Update later: run the same line without -Topic.
Remove:       add -Uninstall.

Everything lives in %LOCALAPPDATA%\psn-watch (app, venv, data, log). A scheduled task starts it hidden
at every sign-in and restarts it if it stops. It only runs while the laptop is awake.
#>
param(
    [string]$Topic,
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"          # makes Invoke-WebRequest much faster
[Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor 3072   # TLS 1.2
$Base = Join-Path $env:LOCALAPPDATA "psn-watch"
$App, $Venv, $Data = (Join-Path $Base "app"), (Join-Path $Base "venv"), (Join-Path $Base "data")
$TaskName = "psn-watch laptop helper"
function Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }

function Stop-Helper {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    }
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*psn-watch*watch.py*--laptop*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

if ($Uninstall) {
    Step "Stopping and removing the scheduled task"
    Stop-Helper
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Remove-Item (Join-Path ([Environment]::GetFolderPath("Startup")) "psn-watch laptop helper.lnk") -ErrorAction SilentlyContinue
    Write-Host "Removed. Files are still in $Base; delete that folder if you don't need them." -ForegroundColor Green
    return
}

# ------------------------------------------------------------------ alert topic
$EnvFile = Join-Path $Data ".env"
New-Item -ItemType Directory -Force -Path $Data | Out-Null
if (-not $Topic -and (Test-Path $EnvFile)) {
    $line = Select-String -Path $EnvFile -Pattern '^NTFY_TOPIC=(.+)$' | Select-Object -First 1
    if ($line) { $Topic = $line.Matches[0].Groups[1].Value.Trim() }
}
if (-not $Topic) { throw "First install needs your ntfy topic: add -Topic <topic> to the command." }

# ------------------------------------------------------------------ Python 3.10+
function Find-Python {
    $cands = @(
        [pscustomobject]@{ Exe = "py"; Args = @("-3") },
        [pscustomobject]@{ Exe = "python"; Args = @() },
        [pscustomobject]@{ Exe = (Join-Path $env:LOCALAPPDATA "Programs\Python\Launcher\py.exe"); Args = @("-3") },
        [pscustomobject]@{ Exe = (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"); Args = @() },
        [pscustomobject]@{ Exe = (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe"); Args = @() }
    )
    foreach ($c in $cands) {
        try {
            $a = $c.Args
            $out = & $c.Exe @a -c "import sys; print(sys.version_info >= (3, 10), sys.executable)" 2>$null
            if ($LASTEXITCODE -eq 0 -and "$out" -match '^True (.+)$') { return $Matches[1].Trim() }
        } catch { }
    }
    return $null
}
Step "Looking for Python 3.10 or newer"
$Py = Find-Python
if (-not $Py) {
    Step "Installing Python 3.12 for your user (winget)"
    winget install -e --id Python.Python.3.12 --scope user --silent --accept-package-agreements --accept-source-agreements | Out-Host
    $Py = Find-Python
    if (-not $Py) { throw "Python didn't install. Install Python 3.12 from python.org (tick 'Add to PATH'), then run this again." }
}
Write-Host "    $Py"

# ------------------------------------------------------------------ code (fresh copy every run = update)
Stop-Helper
Step "Downloading the latest psn-watch from GitHub"
$Zip = Join-Path $env:TEMP "psn-watch.zip"
$Tmp = Join-Path $env:TEMP "psn-watch-src"
Invoke-WebRequest "https://codeload.github.com/Achyuth762/psn-watch/zip/refs/heads/main" -OutFile $Zip -UseBasicParsing
Remove-Item $Tmp -Recurse -Force -ErrorAction SilentlyContinue
Expand-Archive $Zip -DestinationPath $Tmp -Force
Remove-Item $App -Recurse -Force -ErrorAction SilentlyContinue
Move-Item (Get-ChildItem $Tmp -Directory | Select-Object -First 1).FullName $App
Remove-Item $Zip, $Tmp -Recurse -Force -ErrorAction SilentlyContinue

# ------------------------------------------------------------------ venv + packages + browser
if (-not (Test-Path (Join-Path $Venv "Scripts\python.exe"))) {
    Step "Creating a private Python environment"
    & $Py -m venv $Venv
    if ($LASTEXITCODE) { throw "venv failed" }
}
$VPy, $VPyw = (Join-Path $Venv "Scripts\python.exe"), (Join-Path $Venv "Scripts\pythonw.exe")
Step "Installing packages (first time takes a minute or two)"
& $VPy -m pip install --disable-pip-version-check -q --upgrade pip | Out-Null
& $VPy -m pip install --disable-pip-version-check -q -r (Join-Path $App "requirements.txt")
if ($LASTEXITCODE) { throw "pip install failed" }
Step "Installing the headless browser some stores need (about 150 MB, first time only)"
& $VPy -m playwright install chromium | Out-Host
if ($LASTEXITCODE) { throw "playwright install failed" }

# ------------------------------------------------------------------ settings
@"
# psn-watch laptop helper settings (kept across updates)
NTFY_TOPIC=$Topic
# follow the wishlist saved on the dashboard
PSN_PREFS_URL=https://achyuthus.dev/psn/api/status
"@ | Set-Content -Path $EnvFile -Encoding UTF8

# ------------------------------------------------------------------ one check now, so you see it works from home
Step "Checking the cloud-blocked stores once from this laptop (no alerts sent)"
Push-Location $App
& $VPy watch.py --laptop --once --dry-run --data $Data
Pop-Location

# ------------------------------------------------------------------ scheduled task: start at sign-in, hidden, auto-restart
Step "Setting it to start hidden whenever you sign in"
$Log = Join-Path $Data "psn-watch.log"
$action = New-ScheduledTaskAction -Execute $VPyw -WorkingDirectory $App `
    -Argument "`"$(Join-Path $App 'watch.py')`" --laptop --data `"$Data`" --log `"$Log`""
$trigger = @(New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME")
try {   # also every 5 min: starts it again if it ever stopped (an already-running copy is left alone)
    $trigger += New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 5)
} catch { }
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -StartWhenAvailable `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$Startup = Join-Path ([Environment]::GetFolderPath("Startup")) "psn-watch laptop helper.lnk"
try {
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
        -Description "Watches PSN India gift card stock at stores that block cloud servers; alerts via ntfy." -Force | Out-Null
    Remove-Item $Startup -ErrorAction SilentlyContinue
    Start-ScheduledTask -TaskName $TaskName
} catch {
    # Some PCs only let admins create sign-in tasks. Fall back to a Startup-folder shortcut
    # (starts at sign-in too, but isn't restarted automatically if it ever stops).
    Write-Host "    Scheduled task not allowed here ($($_.Exception.Message)); using a Startup shortcut instead." -ForegroundColor Yellow
    $sh = (New-Object -ComObject WScript.Shell).CreateShortcut($Startup)
    $sh.TargetPath = $VPyw
    $sh.WorkingDirectory = $App
    $sh.Arguments = "`"$(Join-Path $App 'watch.py')`" --laptop --data `"$Data`" --log `"$Log`""
    $sh.Save()
    Start-Process -FilePath $VPyw -WorkingDirectory $App -ArgumentList $sh.Arguments
}
Start-Sleep -Seconds 8
$running = Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" | Where-Object { $_.CommandLine -like "*watch.py*--laptop*" }

Write-Host ""
if ($running) {
    Write-Host "Done. The laptop helper is running in the background." -ForegroundColor Green
} else {
    Write-Host "Installed, but it isn't running yet. Look at the log: $Log" -ForegroundColor Yellow
}
Write-Host "  Log:     $Log"
Write-Host "  Update:  run the same command again (without -Topic)"
Write-Host "  Remove:  run it with -Uninstall"
