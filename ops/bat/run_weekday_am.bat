@echo off
setlocal enabledelayedexpansion

:: Repository root comes from this script's own location, not from the current
:: directory, the drive, the user name or a hardcoded path.
call "%~dp0_repo_root.cmd" || exit /b 1

set "SCHEDULE_FILE=%REPO_ROOT%\ops\schedules$slot.json"
if not exist "%SCHEDULE_FILE%" (
    echo [weekday-am] missing schedule file "%SCHEDULE_FILE%". 1>&2
    exit /b 1
)

:: pwsh is resolved through PATH with a Windows PowerShell fallback; its install
:: location is not fixed and hardcoding it broke on every machine but one.
set "PWSH_EXE=pwsh.exe"
where pwsh.exe >nul 2>nul || set "PWSH_EXE=powershell.exe"

:: Results go under the user's local app data by default. Writing them into the
:: repository would dirty the checkout; a hardcoded drive root assumes a layout
:: nobody else has. Override with XRML_SCHEDULE_RESULT_ROOT.
set "RESULT_ROOT=%XRML_SCHEDULE_RESULT_ROOT%"
if not defined RESULT_ROOT set "RESULT_ROOT=%LOCALAPPDATA%\XRMLData\schedule-results"
set "SUMMARY_DIR=%RESULT_ROOT%$slot"
if not exist "%SUMMARY_DIR%" mkdir "%SUMMARY_DIR%" || exit /b 1
set "SUMMARY_FILE=%SUMMARY_DIR%\latest-summary.json"

pushd "%REPO_ROOT%" || exit /b 1
"%PWSH_EXE%" -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%REPO_ROOT%\scripts\run_gemini_schedule.ps1" -ScheduleFile "%SCHEDULE_FILE%" -JsonOut "%SUMMARY_FILE%"
set "EXIT_CODE=!ERRORLEVEL!"
popd
exit /b %EXIT_CODE%
