@echo off
rem SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
rem SPDX-License-Identifier: Apache-2.0
rem
rem Ucho installer for a double click. Runs install.ps1 in Windows PowerShell
rem without changing the system execution policy and keeps the window open at
rem the end (-Pause). Installer options are passed through, for example:
rem     install.cmd -WithSummary
rem ASCII only, CRLF line endings (.gitattributes).

setlocal
rem Double-clicked inside the zip: Explorer copies only this file to a
rem temporary folder, so the rest of Ucho is not next to it.
if not exist "%~dp0install.ps1" goto missing
if not exist "%~dp0ucho\__init__.py" goto missing

rem install.ps1 writes a new install.log as soon as it starts. If there is no
rem log after a failure, PowerShell could not start the script at all (e.g. a
rem company policy), and the window must stay open to show why.
del /q "%~dp0install.log" >nul 2>&1
"%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -Pause %*
set "code=%ERRORLEVEL%"
if "%code%"=="0" exit /b 0
if exist "%~dp0install.log" exit /b %code%
echo.
echo   install.ps1 could not be started. See the message above.
echo   Nie udalo sie uruchomic install.ps1. Przyczyna jest powyzej.
echo.
pause
exit /b %code%

:missing
echo.
echo   Unpack the whole zip first (right click - Extract All),
echo   then run install.cmd from the unpacked folder.
echo.
echo   Najpierw rozpakuj cale archiwum (prawy przycisk - Wyodrebnij wszystkie),
echo   potem uruchom install.cmd z rozpakowanego folderu.
echo.
pause
exit /b 1
