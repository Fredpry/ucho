# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
<#
    Ucho - uninstaller / deinstalator.

    Run / Uruchom: double-click uninstall.cmd / kliknij dwukrotnie uninstall.cmd
    or / albo:  powershell -ExecutionPolicy Bypass -File .\uninstall.ps1

    Removes what install.ps1 and the app created for THIS program folder:
      - the "Ucho" and "Ucho Dictate" shortcuts on the Desktop and in the
        Start menu (also the v0.1 "Ucho Dyktuj"), only when they point here;
      - the "start with Windows" entry (HKCU\...\Run, value "Ucho"), only when
        it points here;
      - after a question: .venv, the downloaded models, settings and logs
        (ucho.log holds dictated text).
    Recordings are deleted only after a separate prompt where you type a word.
    Never touched: the program files themselves (delete the folder afterwards)
    and Python 3.12 (remove it in Settings > Apps if nothing else needs it).

    No admin rights needed. ASCII only: PowerShell 5.1 reads a file without a
    BOM as ANSI.
#>

[CmdletBinding()]
param(
    [switch]$Yes,              # no questions for shortcuts and app data (never recordings) / bez pytan (nigdy o nagrania)
    [switch]$KeepEnvironment   # keep .venv, models, settings and logs / zostaw .venv, modele, ustawienia i logi
)

$ErrorActionPreference = 'Stop'
$Root = (Split-Path -Parent $MyInvocation.MyCommand.Definition).TrimEnd('\')
# With the trailing backslash "...\Ucho\app" does not match "...\Ucho\app2".
$RootPrefix = $Root + '\'
$Venv = Join-Path $Root '.venv'
$Desktop = [Environment]::GetFolderPath('Desktop')
$StartMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$RunKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Run'
$Removed = New-Object System.Collections.Generic.List[string]

$PL = (Get-UICulture).TwoLetterISOLanguageName -eq 'pl'
function T($pl, $en) { if ($PL) { return $pl } else { return $en } }

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Ok($text)   { Write-Host "    [OK] $text" -ForegroundColor Green }
function Warn($text) { Write-Host "    [!]  $text" -ForegroundColor Yellow }
function Fail($text) { Write-Host "    [X]  $text" -ForegroundColor Red }
function Removed($text) { $Removed.Add($text); Ok ((T 'Usunieto: ' 'Removed: ') + $text) }

# False in CI, in scheduled tasks and under "powershell -NonInteractive".
function Test-Interactive {
    if (-not [Environment]::UserInteractive) { return $false }
    try { if ([Console]::IsInputRedirected) { return $false } } catch { }
    foreach ($arg in [Environment]::GetCommandLineArgs()) {
        if ($arg -like '-noni*') { return $false }
    }
    return $true
}

# A yes/no question. -Yes answers yes; a non-interactive run without -Yes
# answers no, so nothing is removed by accident.
function Confirm-Step([string]$Question, [bool]$DefaultYes) {
    if ($Yes) { return $true }
    if (-not (Test-Interactive)) { return $false }
    if ($DefaultYes) { $hint = T '[T/n]' '[Y/n]' } else { $hint = T '[t/N]' '[y/N]' }
    $answer = Read-Host "  $Question $hint"
    if ([string]::IsNullOrWhiteSpace($answer)) { return $DefaultYes }
    return ($answer -match '^\s*(y|yes|t|tak)\s*$')
}

function Test-PointsHere([string]$Text) {
    return ($Text -and $Text.IndexOf($RootPrefix, [StringComparison]::OrdinalIgnoreCase) -ge 0)
}

# "rmdir /s /q" removes junctions and symbolic links without following them;
# Remove-Item -Recurse in Windows PowerShell 5.1 can follow them into the
# target folder.
function Remove-Folder([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & cmd.exe /d /c "rmdir /s /q `"$Path`"" 2>&1 | Out-Null } finally { $ErrorActionPreference = $previous }
    if (Test-Path -LiteralPath $Path) {
        Warn ((T 'Nie udalo sie usunac w calosci: ' 'Could not remove completely: ') + $Path)
        return $false
    }
    return $true
}

function Get-FolderSizeGb([string[]]$Paths) {
    $bytes = 0
    foreach ($path in $Paths) {
        if (Test-Path -LiteralPath $path) {
            $sum = (Get-ChildItem -LiteralPath $path -Recurse -File -Force -ErrorAction SilentlyContinue |
                    Measure-Object -Property Length -Sum).Sum
            if ($sum) { $bytes += $sum }
        }
    }
    return [math]::Round($bytes / 1GB, 1)
}

trap {
    Fail ((T 'Nieoczekiwany blad: ' 'Unexpected error: ') + $_.Exception.Message)
    if ($_.InvocationInfo) { Write-Host $_.InvocationInfo.PositionMessage -ForegroundColor DarkGray }
    exit 1
}

Write-Host ''
Write-Host (T '  Ucho - odinstalowanie' '  Ucho - uninstall') -ForegroundColor White
Write-Host ((T '  Folder programu: ' '  Program folder: ') + $Root) -ForegroundColor DarkGray

# Only ever work on a real Ucho folder: this script could be copied anywhere.
if (-not (Test-Path -LiteralPath (Join-Path $Root 'ucho\config.py'))) {
    Fail (T 'To nie jest folder programu Ucho (brak ucho\config.py). Nic nie zmieniono.' 'This is not an Ucho program folder (ucho\config.py is missing). Nothing was changed.')
    exit 1
}

# --- 0. Ucho must not be running ----------------------------------------------

# The venv's python.exe is a launcher that starts the base interpreter, so the
# command line (which names ucho.pyw or mcp_server.py here) is checked too.
$running = @()
try {
    $running = @(Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" -ErrorAction Stop |
                 Where-Object { (Test-PointsHere $_.ExecutablePath) -or (Test-PointsHere $_.CommandLine) })
} catch { }
if ($running.Count -gt 0) {
    Fail (T 'Ucho dziala (albo jego serwer MCP).' 'Ucho is running (or its MCP server is).')
    Warn (T 'Zamknij Ucho (ikona w zasobniku > Zakoncz) i agentow AI, ktorzy go uzywaja, potem uruchom deinstalacje ponownie.' 'Quit Ucho (tray icon > Quit) and AI agents that use it, then run the uninstaller again.')
    exit 1
}

# --- 1. Where are the recordings (read before the settings are removed) ------

$recordingFolders = New-Object System.Collections.Generic.List[string]
foreach ($file in @('settings.json', 'ustawienia.json')) {
    $path = Join-Path $Root $file
    if (Test-Path -LiteralPath $path) {
        try {
            $stored = Get-Content -Raw -Encoding UTF8 -LiteralPath $path | ConvertFrom-Json
            foreach ($value in @($stored.recordings_dir, $stored.katalog_nagran)) {
                if ($value) { $recordingFolders.Add([string]$value) }
            }
        } catch { }
    }
}
# Default folders of v0.2 and v0.1.
$recordingFolders.Add((Join-Path $env:USERPROFILE 'Ucho\recordings'))
$recordingFolders.Add((Join-Path $env:USERPROFILE 'Ucho\nagrania'))
$recordingFolders = @($recordingFolders | ForEach-Object { $_.TrimEnd('\') } | Sort-Object -Unique |
                      Where-Object { Test-Path -LiteralPath $_ -PathType Container })

# --- 2. What will happen --------------------------------------------------------

Write-Host ''
Write-Host (T '  Zostana usuniete skroty Ucho (Pulpit, Menu Start) i wpis autostartu, jesli wskazuja na ten folder.' '  This removes the Ucho shortcuts (Desktop, Start menu) and the autostart entry if they point to this folder.') -ForegroundColor White
Write-Host (T '  Nagrania zostaja, chyba ze osobno potwierdzisz ich usuniecie na koncu.' '  Your recordings stay unless you confirm deleting them in a separate step at the end.') -ForegroundColor White
Write-Host ''
if (-not (Confirm-Step (T 'Odinstalowac Ucho?' 'Uninstall Ucho?') $false)) {
    if (-not $Yes -and -not (Test-Interactive)) {
        Warn (T 'Tryb nieinteraktywny bez -Yes - nic nie zmieniono.' 'Non-interactive run without -Yes - nothing was changed.')
    } else {
        Write-Host (T '  Przerwano. Nic nie zmieniono.' '  Cancelled. Nothing was changed.')
    }
    exit 1
}

# --- 3. Shortcuts -----------------------------------------------------------------

Step (T 'Usuwam skroty' 'Removing shortcuts')
$shell = New-Object -ComObject WScript.Shell
foreach ($folder in @($Desktop, $StartMenu) | Where-Object { $_ }) {
    foreach ($name in @('Ucho.lnk', 'Ucho Dictate.lnk', 'Ucho Dyktuj.lnk')) {
        $path = Join-Path $folder $name
        if (-not (Test-Path -LiteralPath $path)) { continue }
        $link = $shell.CreateShortcut($path)
        if ((Test-PointsHere $link.TargetPath) -or (Test-PointsHere $link.Arguments) -or (Test-PointsHere $link.WorkingDirectory)) {
            Remove-Item -LiteralPath $path -Force
            Removed $path
        } else {
            # Another Ucho installation, or a shortcut made by hand.
            Warn ((T 'Zostawiam (wskazuje na inny folder): ' 'Kept (points to another folder): ') + $path)
        }
    }
}

# --- 4. Start with Windows --------------------------------------------------------

# Written by the app (Settings > Start Ucho when I sign in), not the installer.
Step (T 'Sprawdzam autostart' 'Checking autostart')
$entry = $null
try { $entry = [string](Get-ItemProperty -Path $RunKey -Name 'Ucho' -ErrorAction Stop).Ucho } catch { }
if (-not $entry) {
    Ok (T 'Brak wpisu autostartu' 'No autostart entry')
} elseif (Test-PointsHere $entry) {
    Remove-ItemProperty -Path $RunKey -Name 'Ucho'
    Removed ($RunKey + '\Ucho')
} else {
    Warn ((T 'Zostawiam wpis autostartu wskazujacy na inny folder: ' 'Kept an autostart entry that points elsewhere: ') + $entry)
}

# --- 5. Python environment, models, settings, logs --------------------------------

# Everything here can be recreated by install.ps1 (the models are downloaded
# again). The program files themselves are not removed: this script is one of
# them, and the user may want to reinstall.
$dataFolders = @('.venv', 'models', 'modele') | ForEach-Object { Join-Path $Root $_ }
$dataFiles = @('settings.json', 'ustawienia.json', 'dictate.json', 'dyktuj.json',
               'ucho.log', 'dictate.log', 'dyktuj.log', 'install.log',
               'Ucho (diagnostics).cmd', 'Ucho (diagnostyka).cmd') | ForEach-Object { Join-Path $Root $_ }

if ($KeepEnvironment) {
    Step (T 'Zostawiam .venv, modele, ustawienia i logi (-KeepEnvironment)' 'Keeping .venv, models, settings and logs (-KeepEnvironment)')
} else {
    Step (T 'Srodowisko Pythona, modele, ustawienia i logi' 'Python environment, models, settings and logs')
    $size = Get-FolderSizeGb $dataFolders
    $sizeText = "$size GB"
    if ($PL) { $sizeText = $sizeText -replace '\.', ',' }
    $question = T "Usunac .venv, pobrane modele, ustawienia i logi (ok. $sizeText; ucho.log zawiera podyktowany tekst)?" "Remove .venv, the downloaded models, settings and logs (about $sizeText; ucho.log holds dictated text)?"
    if (Confirm-Step $question $true) {
        foreach ($folder in $dataFolders) {
            if (Remove-Folder $folder) { Removed $folder }
        }
        foreach ($file in $dataFiles) {
            if (Test-Path -LiteralPath $file) {
                try { Remove-Item -LiteralPath $file -Force; Removed $file }
                catch { Warn ((T 'Nie udalo sie usunac: ' 'Could not remove: ') + $file) }
            }
        }
    } else {
        Ok (T 'Zostawiono' 'Kept')
    }
}

# --- 6. Recordings: only after typing a word -------------------------------------

# Only folders that look like Ucho sessions are deleted, never the whole
# recordings folder blindly: it may have been set to a folder with other files.
$markers = @('meta.json', 'microphone.flac', 'meeting.flac', 'transcript.md',
             'mikrofon.flac', 'spotkanie.flac', 'transkrypt.md')
function Get-Sessions([string]$Folder) {
    return @(Get-ChildItem -LiteralPath $Folder -Directory -Force -ErrorAction SilentlyContinue | Where-Object {
        $dir = $_.FullName
        @($markers | Where-Object { Test-Path -LiteralPath (Join-Path $dir $_) }).Count -gt 0
    })
}

$keptRecordings = @()
foreach ($folder in $recordingFolders) {
    $sessions = Get-Sessions $folder
    Step ((T 'Nagrania: ' 'Recordings: ') + $folder)
    if ($sessions.Count -eq 0) {
        Ok (T 'Brak nagran Ucho w tym folderze - nic nie usuwam' 'No Ucho recordings in this folder - nothing to delete')
        continue
    }
    Write-Host ((T '    Liczba nagran: ' '    Recordings: ') + $sessions.Count) -ForegroundColor White
    $deleted = $false
    if (Test-Interactive) {
        Write-Host (T '    Nagrania ZOSTAJA na dysku. Aby je trwale usunac (bez Kosza), wpisz USUN i nacisnij Enter.' '    Recordings STAY on the disk. To delete them for good (no Recycle Bin), type DELETE and press Enter.') -ForegroundColor Yellow
        $answer = Read-Host (T '    Cokolwiek innego je zostawia' '    Anything else keeps them')
        if ("$answer".Trim() -in @('USUN', 'DELETE')) {
            foreach ($session in $sessions) { [void](Remove-Folder $session.FullName) }
            $left = @(Get-ChildItem -LiteralPath $folder -Force -ErrorAction SilentlyContinue)
            if ($left.Count -eq 0) {
                Remove-Item -LiteralPath $folder -Force -ErrorAction SilentlyContinue
                Removed $folder
            } else {
                Removed ((T 'nagrania z ' 'recordings in ') + $folder)
                Warn ((T 'W folderze zostaly inne pliki: ' 'Other items left in the folder: ') + $left.Count)
            }
            $deleted = $true
        }
    }
    if (-not $deleted) {
        Ok (T 'Nagrania zostaja' 'Recordings kept')
        $keptRecordings += $folder
    }
}

# --- 7. Summary --------------------------------------------------------------------

$bar = '  ' + ('=' * 72)
Write-Host ''
Write-Host $bar -ForegroundColor Green
Write-Host (T '  Gotowe. Usunieto:' '  Done. Removed:') -ForegroundColor Green
if ($Removed.Count -eq 0) { Write-Host (T '    nic' '    nothing') }
foreach ($item in $Removed) { Write-Host "    $item" }
Write-Host ''
Write-Host (T '  Zostalo (usun samodzielnie, jesli chcesz):' '  Left in place (remove yourself if you want):') -ForegroundColor White
Write-Host ((T '    Pliki programu: ' '    Program files: ') + $Root + (T ' (zamknij to okno i usun folder)' ' (close this window, then delete the folder)'))
foreach ($folder in $keptRecordings) { Write-Host ((T '    Nagrania: ' '    Recordings: ') + $folder) }
Write-Host (T '    Python 3.12: Ustawienia > Aplikacje, jesli nic innego go nie uzywa' '    Python 3.12: Settings > Apps, if nothing else uses it')
Write-Host (T '    Przypiete Ucho na pasku zadan: prawy przycisk > Odepnij' '    Ucho pinned to the taskbar: right click > Unpin')
Write-Host (T '    Serwer MCP w agencie AI: claude mcp remove -s user ucho' '    MCP server in your AI agent: claude mcp remove -s user ucho')
Write-Host $bar -ForegroundColor Green
Write-Host ''
exit 0
