@echo off
rem Restarts the trade copier. Make a shortcut to this file for the desktop.
rem
rem NOT "run as administrator" by default, and it should not need to be:
rem restart-copier.ps1 stops whatever holds port 8010, and only asks for
rem elevation (a UAC prompt) if that process was itself started elevated
rem and refuses to die. Start the copier from here every time and that
rem case stops arising.
rem
rem The console window this opens is safe to close once it says the copier
rem is up: since 2026-11-18 the hub is started detached through WMI, so it
rem no longer dies with the window that launched it.
rem
rem %~dp0 is this file's own folder, so the shortcut works from anywhere.

setlocal
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\restart-copier.ps1" %*
set RC=%ERRORLEVEL%
echo.

rem THREE OUTCOMES, SAID APART. This used to print "the copier answered on
rem /health" for any exit code, including the one that means "a UAC window
rem was opened and nothing has been restarted yet" -- a green light over an
rem untouched copier, which is the single thing this launcher must never do.
if "%RC%"=="3" (
  echo Handed off to an ELEVATED window -- watch THAT window for the result.
  echo Nothing has been restarted yet.
) else if "%RC%"=="0" (
  echo The copier is up and detached. This window can be closed safely.
) else (
  echo FAILED ^(exit code %RC%^). The copier may be down.
  echo Check C:\GTC\logs\hub.log and C:\GTC\logs\console.err
)
echo.
pause
endlocal
