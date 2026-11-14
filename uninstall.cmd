@echo off
rem SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
rem SPDX-License-Identifier: Apache-2.0
rem
rem Ucho uninstaller for a double click. Runs uninstall.ps1, which asks before
rem removing anything and deletes recordings only after a separate prompt.
rem Options are passed through, for example:
rem     uninstall.cmd -KeepEnvironment
rem ASCII only, CRLF line endings (.gitattributes).

setlocal
if not exist "%~dp0uninstall.ps1" (
    echo   uninstall.ps1 is missing next to uninstall.cmd.
    pause
    exit /b 1
)
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0uninstall.ps1" %*
set "code=%ERRORLEVEL%"
echo.
pause
exit /b %code%
