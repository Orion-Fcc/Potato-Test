@echo off
chcp 65001 >nul
REM ============================================================
REM  Potato Test pre-run RAM check - run before starting a batch
REM  Rule: free RAM < 3.5GB => do NOT start a run.
REM        Measured on this 16GB soldered board: at 4.25GB free, Chrome
REM        failed to launch 3x in a row ("exited before CDP became
REM        available"). At 5.5GB it launches fine.
REM
REM  Three hard-won environment rules (do not "clean these up"):
REM   1. wmic is GONE on Win11 -> read RAM via Get-CimInstance.
REM   2. Never use "find" in a pipe here: under Git Bash / PATH
REM      shadowing it resolves to the Unix find, which silently returns
REM      0 matches and makes every count wrong.
REM   3. The PowerShell block MUST stay on ONE physical line. Using
REM      cmd's "^" continuation across lines breaks on the parentheses
REM      and pipes: cmd truncates the command at ")" and every line
REM      after it dies with "'6' is not recognized". Symptom: the
REM      [1] Free RAM line vanishes and the LSS check passes wrongly.
REM      Long line is ugly but it works - keep it that way.
REM
REM   Edge is NOT the whole story: msedgewebview2 (embedded web UI of
REM   DingTalk / WorkBuddy) measured 677-751MB, MORE than Edge, and it
REM   survives closing Edge. Both are reported.
REM ============================================================

echo.
echo === Potato Test pre-run RAM check ===
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command "$os=Get-CimInstance Win32_OperatingSystem; $free=[math]::Round($os.FreePhysicalMemory/1MB,2); $edge=@(Get-Process msedge -ErrorAction SilentlyContinue); $wv=@(Get-Process msedgewebview2 -ErrorAction SilentlyContinue); $edgeMB=[math]::Round((($edge|Measure-Object WorkingSet64 -Sum).Sum)/1MB,0); $wvMB=[math]::Round((($wv|Measure-Object WorkingSet64 -Sum).Sum)/1MB,0); $svc='stopped'; try{$r=Invoke-WebRequest -UseBasicParsing -TimeoutSec 5 http://127.0.0.1:18081/api/projects; if($r.StatusCode -eq 200){$svc='running'}}catch{$svc='stopped'}; Write-Output ('FREE_GB=' + $free); Write-Output ('EDGE=' + $edge.Count); Write-Output ('EDGE_MB=' + $edgeMB); Write-Output ('WEBVIEW=' + $wv.Count); Write-Output ('WEBVIEW_MB=' + $wvMB); Write-Output ('SVC=' + $svc)" > "%TEMP%\_pt_health.txt" 2>nul

set "FREEGB=0"
set "EDGECOUNT=0"
set "EDGEMB=0"
set "WEBVIEW=0"
set "WEBVIEWMB=0"
set "SVC=unknown"
for /f "usebackq tokens=1,2 delims==" %%a in ("%TEMP%\_pt_health.txt") do (
  if "%%a"=="FREE_GB"    set "FREEGB=%%b"
  if "%%a"=="EDGE"       set "EDGECOUNT=%%b"
  if "%%a"=="EDGE_MB"    set "EDGEMB=%%b"
  if "%%a"=="WEBVIEW"    set "WEBVIEW=%%b"
  if "%%a"=="WEBVIEW_MB" set "WEBVIEWMB=%%b"
  if "%%a"=="SVC"        if "%%b"=="running" (set "SVC=running 18081") else (set "SVC=stopped")
)

echo [1] Free RAM      : %FREEGB% GB   (need ^>= 3.5)
echo [2] Edge          : %EDGECOUNT% procs / %EDGEMB% MB
echo [3] WebView2      : %WEBVIEW% procs / %WEBVIEWMB% MB   ^(DingTalk^/WorkBuddy embedded UI^)
echo [4] Service       : %SVC%
echo.

if %FREEGB% LSS 3.5 (
  echo [DO NOT RUN] Free RAM %FREEGB% GB is under 3.5GB - Chrome failed to launch at 4.25GB.
  echo Quit DingTalk and other heavy apps, then check again.
) else (
  if %EDGECOUNT% GTR 0 (
    echo [OK TO RUN] RAM %FREEGB% GB is fine. Edge is still up ^(%EDGECOUNT% procs / %EDGEMB% MB^) - close it for headroom.
  ) else (
    echo [OK TO RUN] RAM %FREEGB% GB, Edge closed - Chrome should start reliably.
  )
)
if %WEBVIEWMB% GTR 400 echo [TIP] WebView2 is holding %WEBVIEWMB% MB. Quitting DingTalk frees the most.

del "%TEMP%\_pt_health.txt" >nul 2>nul
echo.
pause
