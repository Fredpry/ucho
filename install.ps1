# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
<#
    Ucho - installer / instalator.

    Run / Uruchom: right click -> "Run with PowerShell"
    or / albo:  powershell -ExecutionPolicy Bypass -File .\install.ps1

    No admin rights needed; safe to run more than once.
    Nie wymaga uprawnien administratora i mozna go uruchamiac wielokrotnie.

    ASCII only: PowerShell 5.1 reads a file without a BOM as ANSI.
#>

[CmdletBinding()]
param(
    [switch]$SkipModel,            # skip the Whisper model / nie pobieraj modelu Whisper
    [switch]$WithSummary,          # also download the summary model / pobierz tez model podsumowan
    [switch]$WithParakeet,         # optional Parakeet backend (CPU) / opcjonalny silnik Parakeet (CPU)
    [switch]$ReinstallPackages     # force reinstall of packages / wymus ponowna instalacje
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $Root  # "-c from ucho..." imports need the program folder as working directory
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
function T($pl, $en) { if ($PL) { return $pl } else { return $en } }

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

New-Item -ItemType Directory -Force -Path $Tmp | Out-Null

Write-Host ''
Write-Host (T '  Ucho - lokalna transkrypcja spotkan i dyktowanie' '  Ucho - local meeting transcription and dictation') -ForegroundColor White
Write-Host (T '  Dziala offline. Audio i transkrypty nie opuszczaja tego komputera.' '  Works offline. Audio and transcripts never leave this computer.') -ForegroundColor DarkGray

if ($Root -match 'OneDrive') {
    Warn (T 'Program lezy w folderze OneDrive. Modele (kilka GB) i logi beda synchronizowane do chmury.' 'The program is inside a OneDrive folder. Models (several GB) and logs will sync to the cloud.')
    Warn (T 'Zalecane: rozpakuj Ucho np. do C:\Users\<Ty>\Ucho\app' 'Recommended: unpack Ucho to e.g. C:\Users\<you>\Ucho\app')
}

# --- 1. Python 3.12 -------------------------------------------------------

Step (T 'Sprawdzam Pythona 3.12' 'Checking Python 3.12')
$version = Test-Python312
if ($null -eq $version) {
    Warn (T 'Brak Pythona 3.12 - instaluje przez winget (kilka minut)' 'Python 3.12 not found - installing with winget (a few minutes)')
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Fail (T 'Brak winget. Zainstaluj Pythona 3.12 z https://www.python.org/downloads/' 'winget not found. Install Python 3.12 from https://www.python.org/downloads/')
        exit 1
    }
    & winget install --id Python.Python.3.12 -e --scope user --silent --accept-package-agreements --accept-source-agreements
    Update-Path
    Start-Sleep -Seconds 2
    $version = Test-Python312
    if ($null -eq $version) {
        Fail (T 'Instalacja Pythona nie powiodla sie. Otworz nowe okno i uruchom skrypt ponownie.' 'Python installation failed. Open a new window and run the script again.')
        exit 1
    }
}
Ok "Python $version"

# --- 2. Virtual environment -----------------------------------------------

Step (T 'Przygotowuje srodowisko wirtualne (.venv)' 'Preparing the virtual environment (.venv)')
if (-not (Test-Path $VenvPy)) {
    & py -3.12 -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Fail (T 'Nie udalo sie utworzyc .venv' 'Could not create .venv'); exit 1 }
    Ok (T 'Utworzono .venv' 'Created .venv')
} else {
    Ok (T '.venv juz istnieje' '.venv already exists')
}

# --- 3. Dependencies (pinned versions from requirements.txt) ---------------

Step (T 'Instaluje zaleznosci (za pierwszym razem ok. 400 MB)' 'Installing dependencies (about 400 MB the first time)')
& $VenvPy -m pip install --upgrade --quiet pip setuptools wheel
if ($LASTEXITCODE -ne 0) { Fail (T 'Aktualizacja pip nie powiodla sie' 'pip upgrade failed'); exit 1 }

$pipArgs = @('-m', 'pip', 'install', '-r', $Requirements)
if ($ReinstallPackages) { $pipArgs += '--force-reinstall' }
& $VenvPy @pipArgs
if ($LASTEXITCODE -ne 0) { Fail (T 'Instalacja zaleznosci nie powiodla sie' 'Dependency installation failed'); exit 1 }
Ok (T 'Zaleznosci zainstalowane' 'Dependencies installed')

# Optional second speech-to-text backend (NVIDIA Parakeet via sherpa-onnx,
# CPU only). Not installed by default; Whisper stays the default model. The
# model itself (~2.5 GB) is downloaded later from Settings -> Hardware.
if ($WithParakeet) {
    $pipArgs = @('-m', 'pip', 'install', '-r', $ParakeetRequirements)
    if ($ReinstallPackages) { $pipArgs += '--force-reinstall' }
    & $VenvPy @pipArgs
    if ($LASTEXITCODE -ne 0) {
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
& $VenvPy -c "from ucho.icon import save_ico, draw_dictate; from pathlib import Path; save_ico(Path(r'$Icon')); save_ico(Path(r'$DictateIcon'), draw_dictate)"
if ($LASTEXITCODE -eq 0) { Ok $Icon } else { Warn (T 'Nie udalo sie wygenerowac ikony (aplikacja zadziala bez niej)' 'Could not generate the icon (the app works without it)') }

# --- 5. Initial settings --------------------------------------------------

# Update from v0.1: the app code itself (config.Settings) migrates
# ustawienia.json to settings.json, so the user's choices and recordings folder
# survive. The old file is kept.
if ((-not (Test-Path $Settings)) -and (Test-Path $LegacySettings)) {
    & $VenvPy -c "from ucho import config; config.Settings()"
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
& $VenvPy @hardwareArgs
if ($LASTEXITCODE -ne 0) {
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
$modelScript = Join-Path $Tmp 'model.py'
@'
import sys
sys.path.insert(0, sys.argv[1])
from ucho.transcribe import model_path
print(model_path(sys.argv[2], download=True))
'@ | Out-File -FilePath $modelScript -Encoding utf8

if (-not $SkipModel) {
    $size = & $VenvPy -c "from ucho import config; print(config.MODELS.get(r'$Model', {}).get('size', '?'))"
    if ($LASTEXITCODE -ne 0 -or -not $size) { $size = '?' }
    $size = "$size".Trim()
    if ($PL) { $size = $size -replace '\.', ',' }
    Step (T "Pobieram model $Model ($size, tylko raz)" "Downloading model $Model ($size, only once)")
    & $VenvPy $modelScript $Root $Model
    if ($LASTEXITCODE -ne 0) {
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
    & $VenvPy $modelScript $Root $DefaultLlmModel
    if ($LASTEXITCODE -ne 0) {
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
            & $VenvPy -c "from pathlib import Path; from ucho import config, winshell; winshell.set_shortcut_app_id(Path(r'$target'), config.APP_ID)"
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

Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue

Write-Host ''
Write-Host (T '  Gotowe.' '  Done.') -ForegroundColor Green
Write-Host (T "  Uruchom skrotem 'Ucho' z Pulpitu lub Menu Start." "  Start it with the 'Ucho' shortcut on the Desktop or in the Start menu.") -ForegroundColor White
Write-Host (T '  Nagrywanie spotkania: Ctrl+Alt+R   Dyktowanie: Ctrl+Shift+Spacja' '  Record a meeting: Ctrl+Alt+R   Dictate: Ctrl+Shift+Space') -ForegroundColor DarkGray
Write-Host ((T '  Transkrypcja liczona na: ' '  Transcription runs on: ') + $device) -ForegroundColor DarkGray
Write-Host ''
Write-Host (T '  Pamietaj o poinformowaniu uczestnikow, ze spotkanie jest nagrywane.' '  Remember to tell participants that the meeting is being recorded.') -ForegroundColor Yellow
Write-Host ''
