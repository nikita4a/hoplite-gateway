@echo off
:: Handoff current directory's OMP/Claude/Codex session to Hoplite cloud agent.
:: Usage: copy this file into your project dir and run it, or:
::        handoff.bat "C:\path\to\project" [extra prompt]
setlocal
set "DIR=%~1"
if "%DIR%"=="" set "DIR=%CD%"
set "PROMPT=%~2"

where hoplite >nul 2>&1
if errorlevel 1 set "PATH=%PATH%;C:\Users\User\AppData\Local\Hoplite\bin"

echo Handing off: %DIR%
if "%PROMPT%"=="" (
    hoplite handoff --cwd "%DIR%" --autopush --harness opencode
) else (
    hoplite handoff --cwd "%DIR%" --autopush --harness opencode --prompt "%PROMPT%"
)
pause