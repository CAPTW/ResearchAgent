@echo off
setlocal enabledelayedexpansion

rem One-shot pipeline runner (Windows).
rem
rem This used to call "python tools\pipeline.py" relative to the current
rem directory, so it only worked when invoked from the repository root and failed
rem silently anywhere else. The root now comes from this script's own location
rem through the shared helper, and the pipeline's exit code is preserved instead
rem of being swallowed by a pause.
rem
rem Pass --check-root to validate resolution without running the pipeline.

call "%~dp0..\ops\bat\_repo_root.cmd" || exit /b 1

if /i "%~1"=="--check-root" (
    echo %REPO_ROOT%
    exit /b 0
)

if not exist "%REPO_ROOT%\tools\pipeline.py" (
    echo [run_all] missing "%REPO_ROOT%\tools\pipeline.py". 1>&2
    exit /b 1
)

pushd "%REPO_ROOT%" || exit /b 1
python "%REPO_ROOT%\tools\pipeline.py" --top 3 --weekly-top 15 --experiment --rank 1
set "EXIT_CODE=!ERRORLEVEL!"
popd
exit /b %EXIT_CODE%
