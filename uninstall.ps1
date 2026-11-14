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
    Recordings are deleted only after a separate prompt where you type a word,
    only session folders the app created (listed by name first), and never
    when the recordings folder is the profile, Documents, Desktop, Downloads,
    a OneDrive root or a drive root.
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
# Parameter names differ from $PL on purpose: PowerShell names are case-insensitive,
# so a parameter called $pl would hide the language flag (always Polish).
function T($textPl, $textEn) { if ($PL) { return $textPl } else { return $textEn } }

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

$ReparsePoint = [System.IO.FileAttributes]::ReparsePoint
$ReadOnly = [System.IO.FileAttributes]::ReadOnly

# A junction or a symbolic link (to a folder or a file). OneDrive's cloud
# files are reparse points too, but they are the data itself, not a link to
# somewhere else; any other reparse point with a target counts as a link.
function Test-Link([System.IO.FileSystemInfo]$Item) {
    if (($Item.Attributes -band $ReparsePoint) -eq 0) { return $false }
    try {
        if ([string]$Item.LinkType -in @('Junction', 'SymbolicLink')) { return $true }
        return (@($Item.Target | Where-Object { $_ }).Count -gt 0)
    } catch { return $true }
}

# Deletes a file or a folder tree without following junctions or symbolic
# links: a link is removed as a link and its target is never touched.
# Remove-Item -Recurse in Windows PowerShell 5.1 can follow a junction into
# its target, and "cmd /c rmdir" would expand %NAME% inside a path, so the
# tree is walked here. Errors are skipped; Remove-Folder checks the result.
function Remove-Tree([System.IO.FileSystemInfo]$Item) {
    $link = Test-Link $Item
    try {
        if ($Item -is [System.IO.DirectoryInfo]) {
            if (-not $link) {
                foreach ($child in $Item.GetFileSystemInfos()) { Remove-Tree $child }
                if (($Item.Attributes -band $ReadOnly) -ne 0) { $Item.Attributes = $Item.Attributes -bxor $ReadOnly }
            }
            # Not recursive: on a junction this removes only the junction.
            [System.IO.Directory]::Delete($Item.FullName, $false)
        } else {
            if (-not $link -and (($Item.Attributes -band $ReadOnly) -ne 0)) { $Item.Attributes = $Item.Attributes -bxor $ReadOnly }
            [System.IO.File]::Delete($Item.FullName)
        }
    } catch { }
}

function Remove-Folder([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $false }
    Remove-Tree (New-Object System.IO.DirectoryInfo $Path)
    if (Test-Path -LiteralPath $Path) {
        Warn ((T 'Nie udalo sie usunac w calosci: ' 'Could not remove completely: ') + $Path)
        return $false
    }
    return $true
}

# Size without following links: Get-ChildItem -Recurse in PowerShell 5.1
# walks into junctions and would count their targets too.
function Get-FolderSizeGb([string[]]$Paths) {
    [long]$bytes = 0
    $pending = New-Object System.Collections.Generic.Stack[System.IO.DirectoryInfo]
    foreach ($path in $Paths) {
        if (Test-Path -LiteralPath $path -PathType Container) {
            $dir = New-Object System.IO.DirectoryInfo $path
            if (-not (Test-Link $dir)) { $pending.Push($dir) }
        }
    }
    while ($pending.Count -gt 0) {
        $dir = $pending.Pop()
        try { $children = $dir.GetFileSystemInfos() } catch { continue }
        foreach ($child in $children) {
            if ((($child.Attributes -band $ReparsePoint) -ne 0) -and (Test-Link $child)) { continue }
            if ($child -is [System.IO.DirectoryInfo]) { $pending.Push($child) } else { $bytes += $child.Length }
        }
    }
    return [math]::Round($bytes / 1GB, 1)
}

# An absolute path without a trailing backslash, except for a drive root:
# "C:" alone would mean the current folder on drive C. $null for a relative
# or drive-relative path ("C:foo"), which this script never guesses at.
function Get-FullPath([string]$Path) {
    if ($Path -notmatch '^([A-Za-z]:\\|\\\\)') { return $null }
    try { $full = [System.IO.Path]::GetFullPath($Path) } catch { return $null }
    if ($full.Length -gt [System.IO.Path]::GetPathRoot($full).Length) { $full = $full.TrimEnd('\') }
    return $full
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
$recordingFolders = @($recordingFolders | ForEach-Object { Get-FullPath $_ } | Where-Object { $_ } |
                      Sort-Object -Unique | Where-Object { Test-Path -LiteralPath $_ -PathType Container })

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

# Only folders that are clearly Ucho sessions are deleted, never the whole
# recordings folder: it may have been set to a folder with other files. A
# session folder is named "<yyyy-MM-dd>_<HH-mm>[_<name>]" (meeting.py; a
# summary keeps the time stamp when it renames the folder) and holds an audio
# track under a name the app uses or a meta.json the app wrote.
$SessionName = '^\d{4}-\d{2}-\d{2}_\d{2}-\d{2}(_|$)'
$AudioTracks = @('microphone.flac', 'meeting.flac', 'mikrofon.flac', 'spotkanie.flac')
# meta.json keys written by meeting.py (and their v0.1 names): a date stamp
# plus at least one of the others.
$MetaDateKeys = @('date', 'data')
$MetaKeys = @('name', 'nazwa', 'duration_s', 'trwanie_s', 'segments', 'segmenty', 'model')

function Test-UchoMeta([string]$File) {
    try {
        $info = New-Object System.IO.FileInfo $File
        if (-not $info.Exists -or (Test-Link $info) -or $info.Length -gt 64MB) { return $false }
        $meta = [System.IO.File]::ReadAllText($File, [System.Text.Encoding]::UTF8) | ConvertFrom-Json
    } catch { return $false }
    if ($meta -isnot [System.Management.Automation.PSCustomObject]) { return $false }
    $keys = @($meta.PSObject.Properties | ForEach-Object { $_.Name })
    $dated = $false
    foreach ($key in $MetaDateKeys) {
        if ($keys -contains $key) {
            $value = $meta.$key
            if ($value -is [datetime] -or "$value" -match '^\d{4}-\d{2}-\d{2}') { $dated = $true }
        }
    }
    return ($dated -and @($keys | Where-Object { $MetaKeys -contains $_ }).Count -gt 0)
}

function Test-Session([System.IO.DirectoryInfo]$Dir) {
    if ((Test-Link $Dir) -or ($Dir.Name -notmatch $SessionName)) { return $false }
    foreach ($name in $AudioTracks) {
        $track = New-Object System.IO.FileInfo (Join-Path $Dir.FullName $name)
        if ($track.Exists -and -not (Test-Link $track)) { return $true }
    }
    return (Test-UchoMeta (Join-Path $Dir.FullName 'meta.json'))
}

function Get-Sessions([string]$Folder) {
    $dir = New-Object System.IO.DirectoryInfo $Folder
    try { $children = @($dir.GetDirectories()) } catch { return @() }
    return @($children | Where-Object { Test-Session $_ } | Sort-Object Name)
}

# Folders that hold far more than Ucho recordings. If the recordings folder is
# one of them (or a drive root, or contains the user profile), the uninstaller
# deletes nothing there: one typed word must never be able to empty Documents.
function Get-UserProfiles {
    return @(@($env:USERPROFILE, [Environment]::GetFolderPath('UserProfile')) |
             Where-Object { $_ } | ForEach-Object { Get-FullPath $_ } | Where-Object { $_ } | Sort-Object -Unique)
}

function Get-ProtectedFolders {
    $list = New-Object System.Collections.Generic.List[string]
    foreach ($name in @('Desktop', 'DesktopDirectory', 'MyDocuments', 'MyMusic', 'MyPictures', 'MyVideos')) {
        try { $list.Add([Environment]::GetFolderPath($name)) } catch { }
    }
    foreach ($value in @($env:OneDrive, $env:OneDriveCommercial, $env:OneDriveConsumer)) {
        if ($value) { $list.Add($value) }
    }
    # Explorer's own resolved paths: also Downloads (no .NET name for it) and
    # folders moved to another drive or into OneDrive.
    try {
        $shellFolders = Get-ItemProperty -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders' -ErrorAction Stop
        foreach ($name in @('Desktop', 'Personal', 'My Music', 'My Pictures', 'My Video', '{374DE290-123F-4565-9164-39C4925E467B}')) {
            $value = $shellFolders.$name
            if ($value) { $list.Add([string]$value) }
        }
    } catch { }
    foreach ($userDir in Get-UserProfiles) {
        $list.Add($userDir)
        foreach ($name in @('Desktop', 'Documents', 'Downloads', 'Music', 'Pictures', 'Videos', 'OneDrive')) {
            $list.Add((Join-Path $userDir $name))
        }
    }
    return @($list | Where-Object { $_ } | ForEach-Object { Get-FullPath $_ } | Where-Object { $_ } | Sort-Object -Unique)
}

# Returns the reason to keep everything in $Folder, or $null when its Ucho
# sessions may be deleted. A recordings folder that is itself a link (moved to
# another drive) is checked by its target as well.
function Get-ProtectedReason([string]$Folder) {
    $full = Get-FullPath $Folder
    if (-not $full) { return (T 'nie da sie ustalic jego sciezki' 'its path cannot be resolved') }
    $paths = @($full)
    if ([System.IO.Path]::GetPathRoot($full) -ine $full) {
        try {
            $item = Get-Item -LiteralPath $full -Force -ErrorAction Stop
            if (Test-Link $item) {
                $target = [string](@($item.Target) | Select-Object -First 1)
                if ($target -and $target -notmatch '^([A-Za-z]:\\|\\\\)') {
                    $target = Join-Path (Split-Path -Parent $full) $target  # relative symbolic link
                }
                $resolved = $null
                if ($target) { $resolved = Get-FullPath $target }
                if (-not $resolved) { return (T 'to link, ktorego celu nie da sie ustalic' 'it is a link whose target cannot be read') }
                $paths += $resolved
            }
        } catch { return (T 'nie da sie go odczytac' 'it cannot be read') }
    }
    $protected = Get-ProtectedFolders
    $profiles = Get-UserProfiles
    foreach ($path in $paths) {
        if ([System.IO.Path]::GetPathRoot($path) -ieq $path) {
            return (T 'to katalog glowny dysku' 'it is the root of a drive')
        }
        foreach ($folder in $protected) {
            if ($path -ieq $folder) { return (T 'to folder osobisty (profil uzytkownika, Dokumenty, Pulpit, Pobrane, OneDrive itp.)' 'it is a personal folder (user profile, Documents, Desktop, Downloads, OneDrive and the like)') }
        }
        foreach ($userDir in $profiles) {
            if ($userDir.StartsWith($path + '\', [StringComparison]::OrdinalIgnoreCase)) {
                return (T 'zawiera folder uzytkownika' 'it contains the user profile')
            }
        }
    }
    return $null
}

$ListLimit = 20
$keptRecordings = @()
foreach ($folder in $recordingFolders) {
    Step ((T 'Nagrania: ' 'Recordings: ') + $folder)
    $reason = Get-ProtectedReason $folder
    if ($reason) {
        Warn ((T 'Nie usuwam stad niczego: ' 'Not deleting anything here: ') + $reason + '.')
        Warn (T 'Nagrania Ucho (foldery z data, np. 2026-01-31_10-00_...) usun samodzielnie, jesli chcesz.' 'Delete the Ucho recordings (folders named by date, e.g. 2026-01-31_10-00_...) yourself if you want.')
        $keptRecordings += $folder
        continue
    }
    $sessions = @(Get-Sessions $folder)
    if ($sessions.Count -eq 0) {
        Ok (T 'Brak nagran Ucho w tym folderze - nic nie usuwam' 'No Ucho recordings in this folder - nothing to delete')
        continue
    }
    Write-Host ((T '    Nagrania Ucho w tym folderze: ' '    Ucho recordings in this folder: ') + $sessions.Count) -ForegroundColor White
    foreach ($session in ($sessions | Select-Object -First $ListLimit)) { Write-Host "      $($session.Name)" }
    if ($sessions.Count -gt $ListLimit) {
        $more = $sessions.Count - $ListLimit
        Write-Host (T "      ... i jeszcze $more" "      ... and $more more")
    }
    $deleted = $false
    if (Test-Interactive) {
        Write-Host (T '    Nagrania ZOSTAJA na dysku. Aby trwale usunac te foldery (bez Kosza), wpisz USUN i nacisnij Enter.' '    Recordings STAY on the disk. To delete these folders for good (no Recycle Bin), type DELETE and press Enter.') -ForegroundColor Yellow
        Write-Host (T '    Inne pliki i foldery w tym folderze zostaja.' '    Other files and folders in this folder stay.') -ForegroundColor Yellow
        $answer = Read-Host (T '    Cokolwiek innego je zostawia' '    Anything else keeps them')
        if ("$answer".Trim() -in @('USUN', 'DELETE')) {
            foreach ($session in $sessions) { [void](Remove-Folder $session.FullName) }
            $left = @(Get-ChildItem -LiteralPath $folder -Force -ErrorAction SilentlyContinue)
            if ($left.Count -eq 0) {
                # Not recursive: an empty folder, or only the link if it is one.
                try { [System.IO.Directory]::Delete($folder, $false) } catch { }
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
