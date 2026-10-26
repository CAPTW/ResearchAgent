@echo off
rem Resolve the repository root from this helper's own location.
rem
rem The previous scripts hardcoded an absolute repository root that did not exist
rem even on the machine that wrote them, so they failed for everyone including
rem their author. Deriving the root from the script location makes them work from
rem any current directory, on any drive, under any user, and inside an extracted
rem source archive.
rem
rem Call as:  call "PATH_TO_THIS\_repo_root.cmd" ^|^| exit /b 1
rem Sets:     REPO_ROOT  (absolute, no trailing backslash)
rem
rem The caller must check the exit code. Continuing past a failed resolution would
rem run commands against whatever directory happened to be current.
rem
rem Note: comments here avoid the percent character entirely. cmd.exe parses a
rem tilde expansion inside a comment as batch-parameter substitution and aborts
rem the whole script, which is how the first draft of this file broke.

setlocal
rem The for-variable full-path modifier normalizes the parent references and
rem strips the trailing backslash that the script-directory expansion always
rem carries; a trailing backslash immediately before a closing quote escapes the
rem quote and corrupts every later expansion.
for %%I in ("%~dp0..\..") do set "RESOLVED=%%~fI"

if not defined RESOLVED (
    echo [repo-root] could not resolve a repository root from this script location. 1>&2
    endlocal & exit /b 1
)

rem Markers, not a guess. If these are absent the directory is not this project,
rem and running a scheduled job against it would be worse than stopping.
if not exist "%RESOLVED%\pyproject.toml" goto :missing
if not exist "%RESOLVED%\app" goto :missing
if not exist "%RESOLVED%\scripts" goto :missing

endlocal & set "REPO_ROOT=%RESOLVED%" & exit /b 0

:missing
echo [repo-root] repository markers not found ^(pyproject.toml, app, scripts^). 1>&2
endlocal & exit /b 1
