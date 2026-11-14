# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
<#
    Ucho - installer / instalator.

    Run / Uruchom: double-click install.cmd / kliknij dwukrotnie install.cmd
    or / albo:  powershell -ExecutionPolicy Bypass -File .\install.ps1

    No admin rights needed; safe to run more than once.
    Nie wymaga uprawnien administratora i mozna go uruchamiac wielokrotnie.
    The whole run is logged to install.log in the program folder.
    Przebieg instalacji trafia do install.log w folderze programu.

    ASCII only: PowerShell 5.1 reads a file without a BOM as ANSI.
#>

[CmdletBinding()]
param(
    [switch]$SkipModel,            # skip the Whisper model / nie pobieraj modelu Whisper
    [switch]$WithSummary,          # also download the summary model / pobierz tez model podsumowan
    [switch]$WithParakeet,         # optional Parakeet backend (CPU) / opcjonalny silnik Parakeet (CPU)
    [switch]$ReinstallPackages,    # force reinstall of packages / wymus ponowna instalacje
    [switch]$Force,                # install inside OneDrive without asking / instaluj w OneDrive bez pytania
    [switch]$Pause                 # wait for Enter before closing (install.cmd) / czekaj na Enter przed zamknieciem
)

# First thing: tell install.cmd that the script did start (it shows its own
# result and waits for Enter from here on). Without this file install.cmd
# knows that PowerShell could not run the script at all. A file in TEMP, not
# install.log: the log may be impossible to write in a read-only folder.
if ($env:UCHO_INSTALL_MARKER) {
    try { [System.IO.File]::WriteAllText($env:UCHO_INSTALL_MARKER, 'started') } catch { }
}

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $Root  # "-c from ucho..." imports need the program folder as working directory
$Log = Join-Path $Root 'install.log'
$Venv = Join-Path $Root '.venv'
$VenvPy = Join-Path $Venv 'Scripts\python.exe'
$VenvPyw = Join-Path $Venv 'Scripts\pythonw.exe'
$Icon = Join-Path $Root 'ucho\assets\icon.ico'
$DictateIcon = Join-Path $Root 'ucho\assets\dictate.ico'
$Settings = Join-Path $Root 'settings.json'
$LegacySettings = Join-Path $Root 'ustawienia.json'
$Requirements = Join-Path $Root 'requirements.txt'
$ParakeetRequirements = Join-Path $Root 'requirements-parakeet.txt'
$Recordings = Join-Path $env:USERPROFILE 'Ucho\recordings'
$DefaultModel = 'OpenVINO/whisper-large-v3-int8-ov'
$DefaultLlmModel = 'OpenVINO/Qwen3-8B-int4-ov'
$Tmp = Join-Path $env:TEMP 'ucho-installer'

$PL = (Get-UICulture).TwoLetterISOLanguageName -eq 'pl'
# Parameter names differ from $PL on purpose: PowerShell names are case-insensitive,
# so a parameter called $pl would hide the language flag (always Polish).
function T($textPl, $textEn) { if ($PL) { return $textPl } else { return $textEn } }

function Step($text) { Write-Host "`n==> $text" -ForegroundColor Cyan }
function Ok($text)   { Write-Host "    [OK] $text" -ForegroundColor Green }
function Warn($text) { Write-Host "    [!]  $text" -ForegroundColor Yellow }
function Fail($text) { Write-Host "    [X]  $text" -ForegroundColor Red }

function Update-Path {
    $machine = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $user = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$machine;$user"
}

function Test-Python312 {
    try {
        $result = & py -3.12 -c "import sys; print(sys.version.split()[0])"
        if ($LASTEXITCODE -eq 0 -and $result) { return $result.Trim() }
    } catch { }
    return $null
}

# False in CI, in scheduled tasks and under "powershell -NonInteractive":
# there the installer must never wait for a key.
function Test-Interactive {
    if (-not [Environment]::UserInteractive) { return $false }
    try { if ([Console]::IsInputRedirected) { return $false } } catch { }
    foreach ($arg in [Environment]::GetCommandLineArgs()) {
        if ($arg -like '-noni*') { return $false }
    }
    return $true
}

# Runs a program and copies its output into install.log as well. Windows
# PowerShell's transcript does not record what a native program writes
# straight to the console, and errors (pip, winget, a failed model download)
# are what a bug report needs. Every native program the installer depends on
# runs through here. pip shows no progress bar when its output is piped, only
# the package names; the model download turns its bars off (model.py below).
# A progress line: bar characters (U+2580..U+259F), "10 MB / 25 MB" or "42%".
$ProgressLine = '[\u2580-\u259F]|\d\s*[KMGT]?i?B\s*/\s*[\d.,]+\s*[KMGT]?i?B|^\s*\d{1,3}([.,]\d+)?\s*%\s*$'
function Invoke-Logged([string]$Exe, [string[]]$Arguments) {
    $previous = $ErrorActionPreference
    # With 'Stop' the first line on stderr (e.g. a pip warning) would end the script.
    $ErrorActionPreference = 'Continue'
    # Piped output is decoded with the console code page: Python writes UTF-8
    # (PYTHONIOENCODING) and the console decodes UTF-8 for the duration, so
    # Polish text from "ucho.hardware" reaches the screen and the log intact.
    $previousIo = $env:PYTHONIOENCODING
    $env:PYTHONIOENCODING = 'utf-8'
    $previousEncoding = $null
    try { $previousEncoding = [Console]::OutputEncoding; [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }
    # Progress bars and spinners (winget) redraw a line with carriage returns,
    # which PowerShell reads as separate lines. Spinner frames are dropped and
    # a progress line is shown at most every 5 s (and its last state), so the
    # window still moves and the log stays short.
    $progress = @{ Pending = $null; Shown = [datetime]::MinValue }
    try {
        & $Exe @Arguments 2>&1 | ForEach-Object -Process {
            # A stderr line arrives as an ErrorRecord; an empty one would print
            # as "System.Management.Automation.RemoteException".
            if ($_ -is [System.Management.Automation.ErrorRecord]) { $line = [string]$_.TargetObject } else { $line = [string]$_ }
            if ($line -notmatch '\w') { return }
            if ($line -match $ProgressLine) {
                if (((Get-Date) - $progress.Shown).TotalSeconds -ge 5) {
                    Write-Host "    $line"
                    $progress.Shown = Get-Date
                    $progress.Pending = $null
                } else {
                    $progress.Pending = $line
                }
                return
            }
            if ($progress.Pending) { Write-Host "    $($progress.Pending)"; $progress.Pending = $null }
            Write-Host "    $line"
        } -End {
            if ($progress.Pending) { Write-Host "    $($progress.Pending)" }
        }
    } finally {
        $ErrorActionPreference = $previous
        $env:PYTHONIOENCODING = $previousIo
        if ($previousEncoding) { try { [Console]::OutputEncoding = $previousEncoding } catch { } }
    }
    return $LASTEXITCODE
}

$script:Logging = $false
function Stop-Log {
    if ($script:Logging) {
        try { Stop-Transcript | Out-Null } catch { }
        $script:Logging = $false
    }
}

# install.cmd passes -Pause: a double-clicked window would otherwise close
# before anyone can read the result or the error.
function Wait-Close {
    if ($Pause -and (Test-Interactive)) {
        Write-Host ''
        try { [void](Read-Host (T 'Nacisnij Enter, aby zamknac to okno' 'Press Enter to close this window')) } catch { }
    }
}

# Every exit goes through here: the log is closed, the window waits (-Pause).
function Exit-Installer([int]$Code, [switch]$Cancelled) {
    Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue
    if ($Code -ne 0 -and -not $Cancelled) {
        Write-Host ''
        Fail (T 'Instalacja nie zostala ukonczona.' 'Installation did not finish.')
        if ($script:Logging) {
            Write-Host ((T '     Szczegoly sa w logu: ' '     Details are in the log: ') + $Log) -ForegroundColor Yellow
            Write-Host (T '     Zgloszenie bledu: dolacz ten plik (zawiera nazwe uzytkownika i sciezki).' '     Bug report: attach this file (it contains your user name and paths).') -ForegroundColor DarkGray
        }
    }
    Stop-Log
    Wait-Close
    exit $Code
}

# Any unexpected terminating error ends here instead of closing the window.
trap {
    Fail ((T 'Nieoczekiwany blad: ' 'Unexpected error: ') + $_.Exception.Message)
    if ($_.InvocationInfo) { Write-Host $_.InvocationInfo.PositionMessage -ForegroundColor DarkGray }
    Exit-Installer 1
}

try {
    Start-Transcript -Path $Log -Force | Out-Null
    $script:Logging = $true
} catch {
    Warn (T 'Nie moge zapisac install.log - instaluje dalej bez logu' 'Could not write install.log - continuing without a log')
}

New-Item -ItemType Directory -Force -Path $Tmp | Out-Null

Write-Host ''
Write-Host (T '  Ucho - lokalna transkrypcja spotkan i dyktowanie' '  Ucho - local meeting transcription and dictation') -ForegroundColor White
Write-Host (T '  Dziala offline. Audio i transkrypty nie opuszczaja tego komputera.' '  Works offline. Audio and transcripts never leave this computer.') -ForegroundColor DarkGray
Write-Host ((T '  Folder programu: ' '  Program folder: ') + $Root) -ForegroundColor DarkGray

# Started from inside the zip (Explorer copies only the clicked file to a
# temporary folder) or from an incomplete copy: nothing below could work.
if (-not ((Test-Path (Join-Path $Root 'ucho\__init__.py')) -and (Test-Path $Requirements))) {
    Fail (T 'Brakuje plikow programu obok install.ps1.' 'Program files are missing next to install.ps1.')
    Warn (T 'Najpierw rozpakuj cale archiwum (prawy przycisk > Wyodrebnij wszystkie), potem uruchom install.cmd z rozpakowanego folderu.' 'Unpack the whole zip first (right click > Extract All), then run install.cmd from the unpacked folder.')
    Exit-Installer 1 -Cancelled
}

# Models (several GB), settings and ucho.log (which holds dictated text) live
# in the program folder, so a OneDrive folder would upload them. Stop by
# default; -Force or a non-interactive run (automation) only warns.
$inOneDrive = $Root -match 'OneDrive'
foreach ($folder in @($env:OneDrive, $env:OneDriveCommercial, $env:OneDriveConsumer)) {
    if ($folder -and $Root.StartsWith($folder.TrimEnd('\'), [StringComparison]::OrdinalIgnoreCase)) { $inOneDrive = $true }
}
if ($inOneDrive) {
    $bar = '  ' + ('!' * 72)
    Write-Host ''
    Write-Host $bar -ForegroundColor Red
    Write-Host (T '  UWAGA: Ucho jest w folderze synchronizowanym z OneDrive:' '  WARNING: Ucho is in a folder that OneDrive syncs to the cloud:') -ForegroundColor Red
    Write-Host "    $Root" -ForegroundColor Yellow
    Write-Host (T '  Modele (kilka GB), ustawienia i log z podyktowanym tekstem trafilyby do chmury.' '  Models (several GB), settings and the log with your dictated text would be uploaded.') -ForegroundColor Yellow
    Write-Host (T '  Zalecane: rozpakuj Ucho-Windows.zip do C:\Users\<Ty> (program trafi do C:\Users\<Ty>\Ucho\app).' '  Recommended: extract Ucho-Windows.zip to C:\Users\<you> (the app goes to C:\Users\<you>\Ucho\app).') -ForegroundColor Yellow
    Write-Host $bar -ForegroundColor Red
    if ($Force) {
        Warn (T 'Podano -Force - instaluje mimo to.' '-Force given - installing anyway.')
    } elseif (-not (Test-Interactive)) {
        Warn (T 'Tryb nieinteraktywny - instaluje mimo to (-Force wylacza to pytanie).' 'Non-interactive run - installing anyway (-Force skips this question).')
    } else {
        $answer = Read-Host (T '  Mimo to instalowac tutaj? [t/N]' '  Install here anyway? [y/N]')
        if ($answer -notmatch '^\s*(y|yes|t|tak)\s*$') {
            Write-Host ''
            Write-Host (T '  Przerwano. Przenies folder poza OneDrive i uruchom install.cmd ponownie.' '  Stopped. Move the folder out of OneDrive and run install.cmd again.') -ForegroundColor White
            Exit-Installer 1 -Cancelled
        }
    }
}

# --- 1. Python 3.12 -------------------------------------------------------

Step (T 'Sprawdzam Pythona 3.12' 'Checking Python 3.12')
$version = Test-Python312
if ($null -eq $version) {
    Warn (T 'Brak Pythona 3.12 - instaluje przez winget (kilka minut)' 'Python 3.12 not found - installing with winget (a few minutes)')
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Fail (T 'Brak winget. Zainstaluj Pythona 3.12 z https://www.python.org/downloads/' 'winget not found. Install Python 3.12 from https://www.python.org/downloads/')
        Exit-Installer 1
    }
    $code = Invoke-Logged 'winget' @('install', '--id', 'Python.Python.3.12', '-e', '--scope', 'user', '--silent',
                                     '--accept-package-agreements', '--accept-source-agreements')
    if ($code -ne 0) { Warn ((T 'winget zakonczyl sie kodem ' 'winget ended with code ') + $code) }
    Update-Path
    Start-Sleep -Seconds 2
    $version = Test-Python312
    if ($null -eq $version) {
        Fail (T 'Instalacja Pythona nie powiodla sie. Otworz nowe okno i uruchom skrypt ponownie.' 'Python installation failed. Open a new window and run the script again.')
        Exit-Installer 1
    }
}
Ok "Python $version"

# --- 2. Virtual environment -----------------------------------------------

Step (T 'Przygotowuje srodowisko wirtualne (.venv)' 'Preparing the virtual environment (.venv)')
if (-not (Test-Path $VenvPy)) {
    $code = Invoke-Logged 'py' @('-3.12', '-m', 'venv', $Venv)
    if ($code -ne 0) { Fail (T 'Nie udalo sie utworzyc .venv' 'Could not create .venv'); Exit-Installer 1 }
    Ok (T 'Utworzono .venv' 'Created .venv')
} else {
    Ok (T '.venv juz istnieje' '.venv already exists')
}

# --- 3. Dependencies (pinned versions from requirements.txt) ---------------

Step (T 'Instaluje zaleznosci (za pierwszym razem ok. 400 MB)' 'Installing dependencies (about 400 MB the first time)')
$code = Invoke-Logged $VenvPy @('-m', 'pip', 'install', '--upgrade', '--quiet', 'pip', 'setuptools', 'wheel')
if ($code -ne 0) { Fail (T 'Aktualizacja pip nie powiodla sie' 'pip upgrade failed'); Exit-Installer 1 }

$pipArgs = @('-m', 'pip', 'install', '-r', $Requirements)
if ($ReinstallPackages) { $pipArgs += '--force-reinstall' }
$code = Invoke-Logged $VenvPy $pipArgs
if ($code -ne 0) { Fail (T 'Instalacja zaleznosci nie powiodla sie' 'Dependency installation failed'); Exit-Installer 1 }
Ok (T 'Zaleznosci zainstalowane' 'Dependencies installed')

# Optional second speech-to-text backend (NVIDIA Parakeet via sherpa-onnx,
# CPU only). Not installed by default; Whisper stays the default model. The
# model itself (~2.5 GB) is downloaded later from Settings -> Hardware.
if ($WithParakeet) {
    $pipArgs = @('-m', 'pip', 'install', '-r', $ParakeetRequirements)
    if ($ReinstallPackages) { $pipArgs += '--force-reinstall' }
    $code = Invoke-Logged $VenvPy $pipArgs
    if ($code -ne 0) {
        Warn (T 'Instalacja Parakeet nie powiodla sie - Whisper dziala bez zmian' 'Parakeet installation failed - Whisper works as before')
    } else {
        Ok (T 'Parakeet zainstalowany - wybierz go w Ustawieniach > Sprzet' 'Parakeet installed - choose it in Settings > Hardware')
    }
}

# The openvino-telemetry package comes with OpenVINO. The app does not use it
# (only the model conversion tools do), but we record the opt-out anyway, so
# nothing can send data even when someone runs those tools by hand.
$optOut = Join-Path $Venv 'Scripts\opt_in_out.exe'
if (Test-Path $optOut) {
    & $optOut --opt_out | Out-Null
    Ok (T 'Telemetria OpenVINO wylaczona' 'OpenVINO telemetry disabled')
}

# --- 4. Icon --------------------------------------------------------------

Step (T 'Generuje ikone aplikacji' 'Generating the app icon')
$code = Invoke-Logged $VenvPy @('-c', "from ucho.icon import save_ico, draw_dictate; from pathlib import Path; save_ico(Path(r'$Icon')); save_ico(Path(r'$DictateIcon'), draw_dictate)")
if ($code -eq 0) { Ok $Icon } else { Warn (T 'Nie udalo sie wygenerowac ikony (aplikacja zadziala bez niej)' 'Could not generate the icon (the app works without it)') }

# --- 5. Initial settings --------------------------------------------------

# Update from v0.1: the app code itself (config.Settings) migrates
# ustawienia.json to settings.json, so the user's choices and recordings folder
# survive. The old file is kept.
if ((-not (Test-Path $Settings)) -and (Test-Path $LegacySettings)) {
    $null = Invoke-Logged $VenvPy @('-c', 'from ucho import config; config.Settings()')
    if (Test-Path $Settings) {
        Ok (T 'Przeniesiono ustawienia z ustawienia.json do settings.json' 'Migrated ustawienia.json to settings.json')
    }
}

# First the file with defaults (keys as in config.DEFAULTS), then step 6 writes
# the hardware recommendation into it. The other order would overwrite the
# recommendation with the defaults.
Step (T 'Zapisuje ustawienia poczatkowe' 'Writing initial settings')
$newSettings = -not (Test-Path $Settings)
if ($newSettings) {
    $configuration = [ordered]@{
        recordings_dir          = $Recordings
        model                   = $DefaultModel
        device                  = 'GPU'
        language                = 'auto'
        ui_language             = 'auto'
        tracks                  = 'both'
        microphone_index        = $null
        system_index            = $null
        hotkey                  = '<ctrl>+<alt>+r'
        tray                    = $true
        transcribe_after_stop   = $true
        summary_enabled         = [bool]$WithSummary
        summary_model           = $DefaultLlmModel
        rename_folder           = [bool]$WithSummary
        dictation_model         = 'OpenVINO/whisper-large-v3-fp16-ov'
        hardware_profile        = $null
    }
    $configuration | ConvertTo-Json | Out-File -FilePath $Settings -Encoding utf8
    Ok $Settings
} else {
    Ok (T 'settings.json juz istnieje - zostawiam wybory uzytkownika' 'settings.json already exists - keeping the user choices')
}
# Existing settings keep their own recordings folder (e.g. ...\Ucho\nagrania).
try {
    $stored = Get-Content -Raw -Encoding UTF8 -Path $Settings | ConvertFrom-Json
    if ($stored.recordings_dir) { $Recordings = [string]$stored.recordings_dir }
} catch { }
New-Item -ItemType Directory -Force -Path $Recordings | Out-Null
Ok ((T 'Nagrania: ' 'Recordings: ') + $Recordings)

# --- 6. Hardware and model choice ------------------------------------------

# ucho.hardware reads RAM, CPU and OpenVINO devices, picks the model and the
# device from the rule table (docs/hardware.md) and writes them to the settings.
# On a re-install --keep-choice overwrites only what the user did not change
# by hand (e.g. after a GPU driver update it switches to the GPU).
Step (T 'Sprawdzam sprzet i dobieram model' 'Checking the hardware and choosing the model')
$hardwareArgs = @('-m', 'ucho.hardware', '--save')
if (-not $newSettings) { $hardwareArgs += '--keep-choice' }
$code = Invoke-Logged $VenvPy $hardwareArgs
if ($code -ne 0) {
    Warn (T 'Nie udalo sie sprawdzic sprzetu - zostaja ustawienia domyslne' 'Hardware check failed - keeping the default settings')
}

$Model = $DefaultModel
$device = 'GPU'
try {
    $stored = Get-Content -Raw -Encoding UTF8 -Path $Settings | ConvertFrom-Json
    if ($stored.model) { $Model = [string]$stored.model }
    if ($stored.device) { $device = [string]$stored.device }
} catch {
    Warn (T 'Nie udalo sie odczytac ustawien - pobiore model domyslny' 'Could not read the settings - downloading the default model')
}
if ($device -eq 'CPU') {
    Warn (T 'Jesli masz Intel Arc lub Iris Xe, zaktualizuj sterownik graficzny i uruchom instalator ponownie' 'If you have Intel Arc or Iris Xe, update the graphics driver and run the installer again')
}

# --- 7. Models ------------------------------------------------------------

# Download through the app code: the same pinned revision as in config.REVISIONS.
# huggingface_hub's progress bars are off: piped into install.log, every redraw
# of a bar would be a line. Instead the script prints how much of the model is
# on the disk every 15 s, so the window shows that the download is moving, and
# a failure ends with the traceback in the log.
$modelScript = Join-Path $Tmp 'model.py'
@'
import os
import stat
import sys
import threading

os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
sys.path.insert(0, sys.argv[1])
from ucho import config  # sets HF_HOME before huggingface_hub is imported
from ucho.transcribe import model_path
from huggingface_hub import constants

repo, message = sys.argv[2], sys.argv[3]
folder = os.path.join(constants.HF_HUB_CACHE, "models--" + repo.replace("/", "--"))


def on_disk():
    total = 0
    for parent, _dirs, files in os.walk(folder):
        for name in files:
            try:
                info = os.lstat(os.path.join(parent, name))
            except OSError:
                continue
            if not stat.S_ISLNK(info.st_mode):  # snapshots may link to blobs
                total += info.st_size
    return total


def report(done):
    last = None
    while not done.wait(15):
        size = on_disk() // 2**20
        if size != last:
            print(message.format(size), flush=True)
            last = size


done = threading.Event()
threading.Thread(target=report, args=(done,), daemon=True).start()
try:
    print(model_path(repo, download=True), flush=True)
finally:
    done.set()
'@ | Out-File -FilePath $modelScript -Encoding utf8
$progressText = T 'pobrano {0} MB' 'downloaded {0} MB'

if (-not $SkipModel) {
    $size = & $VenvPy -c "from ucho import config; print(config.MODELS.get(r'$Model', {}).get('size', '?'))"
    if ($LASTEXITCODE -ne 0 -or -not $size) { $size = '?' }
    $size = "$size".Trim()
    if ($PL) { $size = $size -replace '\.', ',' }
    Step (T "Pobieram model $Model ($size, tylko raz)" "Downloading model $Model ($size, only once)")
    $code = Invoke-Logged $VenvPy @($modelScript, $Root, $Model, $progressText)
    if ($code -ne 0) {
        Warn (T 'Pobieranie nie powiodlo sie - mozesz je powtorzyc w Ustawieniach aplikacji' 'Download failed - you can retry from the app Settings')
    } else {
        Ok (T 'Model gotowy' 'Model ready')
    }
} else {
    Warn (T 'Pominieto pobieranie modelu (-SkipModel)' 'Skipped model download (-SkipModel)')
}

# Summaries are optional and off by default: transcription is the core of Ucho.
if ($WithSummary) {
    Step (T "Pobieram model podsumowan $DefaultLlmModel (~5 GB, tylko raz)" "Downloading summary model $DefaultLlmModel (~5 GB, only once)")
    $code = Invoke-Logged $VenvPy @($modelScript, $Root, $DefaultLlmModel, $progressText)
    if ($code -ne 0) {
        Warn (T 'Pobieranie nie powiodlo sie - transkrypcja dziala, tylko bez podsumowan' 'Download failed - transcription works, just without summaries')
    } else {
        Ok (T 'Model podsumowan gotowy' 'Summary model ready')
    }
}

# --- 8. Shortcuts ---------------------------------------------------------

Step (T 'Tworze skroty' 'Creating shortcuts')
# One app since v0.2: "Ucho" and "Ucho Dictate" start the same program
# (ucho.pyw); the second one opens dictation. A second start while Ucho
# already runs only brings its window forward. Both carry the same
# AppUserModelID, so a pinned tile, the running window and the Jump List
# are one taskbar button.
$shell = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$startMenu = Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs'
$starter = Join-Path $Root 'ucho.pyw'
$shortcuts = @(
    @{ Name = 'Ucho.lnk'; Args = "`"$starter`""; Icon = $Icon;
       Text = (T 'Lokalne nagrywanie i transkrypcja spotkan' 'Local meeting recording and transcription') },
    @{ Name = 'Ucho Dictate.lnk'; Args = "`"$starter`" --dictate"; Icon = $DictateIcon;
       Text = (T 'Dyktowanie do dowolnego okna (lokalny Whisper)' 'Voice dictation into any window (local Whisper)') }
)
foreach ($folder in @($desktop, $startMenu)) {
    foreach ($item in $shortcuts) {
        $target = Join-Path $folder $item.Name
        try {
            if (-not (Test-Path $folder)) { New-Item -ItemType Directory -Force -Path $folder | Out-Null }
            $shortcut = $shell.CreateShortcut($target)
            $shortcut.TargetPath = $VenvPyw
            # ucho.pyw instead of "-m ucho": works also when Windows starts the
            # shortcut without a working directory (e.g. from a pinned tile).
            $shortcut.Arguments = $item.Args
            $shortcut.WorkingDirectory = $Root
            $shortcut.Description = $item.Text
            if (Test-Path $item.Icon) { $shortcut.IconLocation = $item.Icon }
            $shortcut.Save()
            $null = Invoke-Logged $VenvPy @('-c', "from pathlib import Path; from ucho import config, winshell; winshell.set_shortcut_app_id(Path(r'$target'), config.APP_ID)")
            Ok $target
        } catch {
            Warn ((T 'Nie udalo sie utworzyc skrotu: ' 'Could not create shortcut: ') + $target)
        }
    }
}

# Console variant - useful when something does not work and you want to see the error.
$cmd = Join-Path $Root 'Ucho (diagnostics).cmd'
@"
@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m ucho
echo.
pause
"@ | Out-File -FilePath $cmd -Encoding ascii
Ok $cmd

# Leftovers of v0.1: the diagnostics file and the Dictate shortcuts had Polish
# names and point to a start script that no longer exists. The separate
# dictation log of v0.1 and of early v0.2 builds is no longer written (one
# log, ucho.log); it is kept, because it may hold the last dictated text.
$legacy = @(
    (Join-Path $Root 'Ucho (diagnostyka).cmd'),
    (Join-Path $desktop 'Ucho Dyktuj.lnk'),
    (Join-Path $startMenu 'Ucho Dyktuj.lnk')
)
foreach ($old in $legacy) {
    if (Test-Path $old) {
        Remove-Item -Force $old -ErrorAction SilentlyContinue
        Ok ((T 'Usunieto stary skrot: ' 'Removed old shortcut: ') + $old)
    }
}

# --- 9. Done ----------------------------------------------------------------

# The MCP command with the real paths of this installation, ready to paste.
# "-s user" registers the server for every project, not only the current folder.
$McpServer = Join-Path $Root 'mcp_server.py'
$McpCommand = "claude mcp add -s user ucho -- `"$VenvPy`" `"$McpServer`""
$bar = '  ' + ('=' * 72)

Write-Host ''
Write-Host $bar -ForegroundColor Green
Write-Host (T '  Gotowe. Ucho jest zainstalowane.' '  Done. Ucho is installed.') -ForegroundColor Green
Write-Host $bar -ForegroundColor Green
Write-Host ''
Write-Host (T "  Uruchom: Menu Start > Ucho (albo skrot 'Ucho' na Pulpicie)." "  Start it: Start menu > Ucho (or the 'Ucho' shortcut on the Desktop).") -ForegroundColor White
Write-Host (T '  Nagrywanie spotkania: Ctrl+Alt+R   Dyktowanie: Ctrl+Shift+Spacja' '  Record a meeting: Ctrl+Alt+R   Dictate: Ctrl+Shift+Space') -ForegroundColor DarkGray
Write-Host ((T '  Transkrypcja liczona na: ' '  Transcription runs on: ') + $device) -ForegroundColor DarkGray
Write-Host ''
Write-Host (T '  Agent AI (MCP), np. Claude Code - wklej to polecenie w terminalu:' '  AI agent (MCP), e.g. Claude Code - paste this command into a terminal:') -ForegroundColor White
Write-Host ''
Write-Host "  $McpCommand" -ForegroundColor Cyan
Write-Host ''
Write-Host (T '  Cursor, Claude Desktop, Codex: ta sama sciezka, przyklady w README.pl.md (sekcja MCP).' '  Cursor, Claude Desktop, Codex: same paths, examples in README.md (section MCP).') -ForegroundColor DarkGray
Write-Host ((T '  Folder programu: ' '  Program folder: ') + $Root) -ForegroundColor DarkGray
Write-Host ((T '  Nagrania:        ' '  Recordings:     ') + $Recordings) -ForegroundColor DarkGray
if ($script:Logging) { Write-Host ((T '  Log instalacji:  ' '  Install log:    ') + $Log) -ForegroundColor DarkGray }
Write-Host ''
Write-Host (T '  Pamietaj o poinformowaniu uczestnikow, ze spotkanie jest nagrywane.' '  Remember to tell participants that the meeting is being recorded.') -ForegroundColor Yellow
Write-Host ''
Exit-Installer 0
