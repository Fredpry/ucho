# SPDX-FileCopyrightText: 2026 Fryderyk Pryjma
# SPDX-License-Identifier: Apache-2.0
<#
    Ucho - instalator / installer.

    Uruchom / Run: prawy przycisk -> "Uruchom w programie PowerShell"
    albo / or:  powershell -ExecutionPolicy Bypass -File .\instaluj.ps1

    Nie wymaga uprawnien administratora i mozna go uruchamiac wielokrotnie.
    No admin rights needed; safe to run more than once.

    Teksty bez polskich znakow: PowerShell 5.1 czyta plik bez BOM jako ANSI.
#>

[CmdletBinding()]
param(
    [switch]$PomijModel,            # nie pobieraj modelu Whisper / skip the Whisper model
    [switch]$ZPodsumowaniem,        # pobierz tez model podsumowan / also download the summary model
    [switch]$PrzeinstalujPakiety    # wymus ponowna instalacje / force reinstall of packages
)

$ErrorActionPreference = 'Stop'
$Root = Split-Path -Parent $MyInvocation.MyCommand.Definition
Set-Location $Root  # importy "-c from ucho..." wymagaja pracy w katalogu programu
$Venv = Join-Path $Root '.venv'
$VenvPy = Join-Path $Venv 'Scripts\python.exe'
$VenvPyw = Join-Path $Venv 'Scripts\pythonw.exe'
$Modele = Join-Path $Root 'modele'
$Ikona = Join-Path $Root 'ucho\assets\ikona.ico'
$Ustawienia = Join-Path $Root 'ustawienia.json'
$Wymagania = Join-Path $Root 'requirements.txt'
$Nagrania = Join-Path $env:USERPROFILE 'Ucho\nagrania'
$ModelDomyslny = 'OpenVINO/whisper-large-v3-int8-ov'
$ModelLLMDomyslny = 'OpenVINO/Qwen3-8B-int4-ov'
$Tmp = Join-Path $env:TEMP 'ucho-instalator'

$PL = (Get-UICulture).TwoLetterISOLanguageName -eq 'pl'
function T($pl, $en) { if ($PL) { return $pl } else { return $en } }

function Krok($tekst) { Write-Host "`n==> $tekst" -ForegroundColor Cyan }
function Ok($tekst)   { Write-Host "    [OK] $tekst" -ForegroundColor Green }
function Uwaga($t)    { Write-Host "    [!]  $t" -ForegroundColor Yellow }
function Blad($t)     { Write-Host "    [X]  $t" -ForegroundColor Red }

function Odswiez-Sciezke {
    $maszyna = [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $uzytkownik = [Environment]::GetEnvironmentVariable('Path', 'User')
    $env:Path = "$maszyna;$uzytkownik"
}

function Test-Python312 {
    try {
        $wynik = & py -3.12 -c "import sys; print(sys.version.split()[0])"
        if ($LASTEXITCODE -eq 0 -and $wynik) { return $wynik.Trim() }
    } catch { }
    return $null
}

New-Item -ItemType Directory -Force -Path $Tmp | Out-Null

Write-Host ''
Write-Host (T '  Ucho - lokalna transkrypcja spotkan i dyktowanie' '  Ucho - local meeting transcription and dictation') -ForegroundColor White
Write-Host (T '  Dziala offline. Audio i transkrypty nie opuszczaja tego komputera.' '  Works offline. Audio and transcripts never leave this computer.') -ForegroundColor DarkGray

if ($Root -match 'OneDrive') {
    Uwaga (T 'Program lezy w folderze OneDrive. Modele (kilka GB) i logi beda synchronizowane do chmury.' 'The program is inside a OneDrive folder. Models (several GB) and logs will sync to the cloud.')
    Uwaga (T 'Zalecane: rozpakuj Ucho np. do C:\Users\<Ty>\Ucho\app' 'Recommended: unpack Ucho to e.g. C:\Users\<you>\Ucho\app')
}

# --- 1. Python 3.12 -------------------------------------------------------

Krok (T 'Sprawdzam Pythona 3.12' 'Checking Python 3.12')
$wersja = Test-Python312
if ($null -eq $wersja) {
    Uwaga (T 'Brak Pythona 3.12 - instaluje przez winget (kilka minut)' 'Python 3.12 not found - installing with winget (a few minutes)')
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Blad (T 'Brak winget. Zainstaluj Pythona 3.12 z https://www.python.org/downloads/' 'winget not found. Install Python 3.12 from https://www.python.org/downloads/')
        exit 1
    }
    & winget install --id Python.Python.3.12 -e --scope user --silent --accept-package-agreements --accept-source-agreements
    Odswiez-Sciezke
    Start-Sleep -Seconds 2
    $wersja = Test-Python312
    if ($null -eq $wersja) {
        Blad (T 'Instalacja Pythona nie powiodla sie. Otworz nowe okno i uruchom skrypt ponownie.' 'Python installation failed. Open a new window and run the script again.')
        exit 1
    }
}
Ok "Python $wersja"

# --- 2. Srodowisko wirtualne ---------------------------------------------

Krok (T 'Przygotowuje srodowisko wirtualne (.venv)' 'Preparing the virtual environment (.venv)')
if (-not (Test-Path $VenvPy)) {
    & py -3.12 -m venv $Venv
    if ($LASTEXITCODE -ne 0) { Blad (T 'Nie udalo sie utworzyc .venv' 'Could not create .venv'); exit 1 }
    Ok (T 'Utworzono .venv' 'Created .venv')
} else {
    Ok (T '.venv juz istnieje' '.venv already exists')
}

# --- 3. Zaleznosci (przypiete wersje z requirements.txt) -----------------

Krok (T 'Instaluje zaleznosci (za pierwszym razem ok. 400 MB)' 'Installing dependencies (about 400 MB the first time)')
& $VenvPy -m pip install --upgrade --quiet pip setuptools wheel
if ($LASTEXITCODE -ne 0) { Blad (T 'Aktualizacja pip nie powiodla sie' 'pip upgrade failed'); exit 1 }

$argi = @('-m', 'pip', 'install', '-r', $Wymagania)
if ($PrzeinstalujPakiety) { $argi += '--force-reinstall' }
& $VenvPy @argi
if ($LASTEXITCODE -ne 0) { Blad (T 'Instalacja zaleznosci nie powiodla sie' 'Dependency installation failed'); exit 1 }
Ok (T 'Zaleznosci zainstalowane' 'Dependencies installed')

# Pakiet openvino-telemetry przychodzi razem z OpenVINO. Aplikacja go nie uzywa
# (korzystaja z niego tylko narzedzia do konwersji modeli), ale zapisujemy
# odmowe zgody, zeby nic nie moglo wyslac danych nawet przy recznym uzyciu narzedzi.
$optOut = Join-Path $Venv 'Scripts\opt_in_out.exe'
if (Test-Path $optOut) {
    & $optOut --opt_out | Out-Null
    Ok (T 'Telemetria OpenVINO wylaczona' 'OpenVINO telemetry disabled')
}

# --- 4. Ikona -------------------------------------------------------------

Krok (T 'Generuje ikone aplikacji' 'Generating the app icon')
& $VenvPy -c "from ucho.ikona import zapisz_ico; from pathlib import Path; zapisz_ico(Path(r'$Ikona'))"
if ($LASTEXITCODE -eq 0) { Ok $Ikona } else { Uwaga (T 'Nie udalo sie wygenerowac ikony (aplikacja zadziala bez niej)' 'Could not generate the icon (the app works without it)') }

# --- 5. Ustawienia poczatkowe --------------------------------------------

# Najpierw plik z wartosciami domyslnymi (klucze jak w config.DOMYSLNE), potem
# krok 6 wpisuje do niego rekomendacje sprzetowa. Odwrotna kolejnosc nadpisalaby
# rekomendacje wartosciami domyslnymi.
Krok (T 'Zapisuje ustawienia poczatkowe' 'Writing initial settings')
$noweUstawienia = -not (Test-Path $Ustawienia)
if ($noweUstawienia) {
    $konfiguracja = [ordered]@{
        katalog_nagran          = $Nagrania
        model                   = $ModelDomyslny
        device                  = 'GPU'
        jezyk                   = 'auto'
        jezyk_interfejsu        = 'auto'
        sciezki                 = 'obie'
        mikrofon_index          = $null
        system_index            = $null
        hotkey                  = '<ctrl>+<alt>+r'
        tray                    = $true
        transkrybuj_po_stopie   = $true
        podsumowanie            = [bool]$ZPodsumowaniem
        model_llm               = $ModelLLMDomyslny
        zmiana_nazwy            = [bool]$ZPodsumowaniem
        dictation_model         = 'OpenVINO/whisper-large-v3-fp16-ov'
        hardware_profile        = $null
    }
    $konfiguracja | ConvertTo-Json | Out-File -FilePath $Ustawienia -Encoding utf8
    Ok $Ustawienia
} else {
    Ok (T 'ustawienia.json juz istnieje - zostawiam wybory uzytkownika' 'ustawienia.json already exists - keeping the user choices')
}
New-Item -ItemType Directory -Force -Path $Nagrania | Out-Null
Ok ((T 'Nagrania: ' 'Recordings: ') + $Nagrania)

# --- 6. Sprzet i dobor modelu --------------------------------------------

# ucho.hardware czyta RAM, procesor i urzadzenia OpenVINO, dobiera model
# i urzadzenie z tabeli regul (docs/hardware.md) i zapisuje je w ustawieniach.
# Przy ponownej instalacji --keep-choice nadpisuje tylko to, czego uzytkownik
# nie zmienil recznie (np. po aktualizacji sterownika GPU wejdzie na GPU).
Krok (T 'Sprawdzam sprzet i dobieram model' 'Checking the hardware and choosing the model')
$argiSprzetu = @('-m', 'ucho.hardware', '--save')
if (-not $noweUstawienia) { $argiSprzetu += '--keep-choice' }
& $VenvPy @argiSprzetu
if ($LASTEXITCODE -ne 0) {
    Uwaga (T 'Nie udalo sie sprawdzic sprzetu - zostaja ustawienia domyslne' 'Hardware check failed - keeping the default settings')
}

$Model = $ModelDomyslny
$device = 'GPU'
try {
    $zapisane = Get-Content -Raw -Encoding UTF8 -Path $Ustawienia | ConvertFrom-Json
    if ($zapisane.model) { $Model = [string]$zapisane.model }
    if ($zapisane.device) { $device = [string]$zapisane.device }
} catch {
    Uwaga (T 'Nie udalo sie odczytac ustawien - pobiore model domyslny' 'Could not read the settings - downloading the default model')
}
if ($device -eq 'CPU') {
    Uwaga (T 'Jesli masz Intel Arc lub Iris Xe, zaktualizuj sterownik graficzny i uruchom instalator ponownie' 'If you have Intel Arc or Iris Xe, update the graphics driver and run the installer again')
}

# --- 7. Modele ------------------------------------------------------------

# Pobieranie przez aplikacje: ta sama przypieta rewizja co w config.REWIZJE.
$skryptModelu = Join-Path $Tmp 'model.py'
@'
import sys
sys.path.insert(0, sys.argv[1])
from ucho.transcribe import sciezka_modelu
print(sciezka_modelu(sys.argv[2], pobierz=True))
'@ | Out-File -FilePath $skryptModelu -Encoding utf8

if (-not $PomijModel) {
    $rozmiar = & $VenvPy -c "from ucho import config; print(config.MODELE.get(r'$Model', {}).get('rozmiar', '?'))"
    if ($LASTEXITCODE -ne 0 -or -not $rozmiar) { $rozmiar = '?' }
    $rozmiar = "$rozmiar".Trim()
    if (-not $PL) { $rozmiar = $rozmiar -replace ',', '.' }
    Krok (T "Pobieram model $Model ($rozmiar, tylko raz)" "Downloading model $Model ($rozmiar, only once)")
    & $VenvPy $skryptModelu $Root $Model
    if ($LASTEXITCODE -ne 0) {
        Uwaga (T 'Pobieranie nie powiodlo sie - mozesz je powtorzyc w Ustawieniach aplikacji' 'Download failed - you can retry from the app Settings')
    } else {
        Ok (T 'Model gotowy' 'Model ready')
    }
} else {
    Uwaga (T 'Pominieto pobieranie modelu (-PomijModel)' 'Skipped model download (-PomijModel)')
}

# Podsumowania sa opcjonalne i domyslnie wylaczone: rdzeniem Ucho jest transkrypcja.
if ($ZPodsumowaniem) {
    Krok (T "Pobieram model podsumowan $ModelLLMDomyslny (~5 GB, tylko raz)" "Downloading summary model $ModelLLMDomyslny (~5 GB, only once)")
    & $VenvPy $skryptModelu $Root $ModelLLMDomyslny
    if ($LASTEXITCODE -ne 0) {
        Uwaga (T 'Pobieranie nie powiodlo sie - transkrypcja dziala, tylko bez podsumowan' 'Download failed - transcription works, just without summaries')
    } else {
        Ok (T 'Model podsumowan gotowy' 'Summary model ready')
    }
}

# --- 8. Skroty ------------------------------------------------------------

Krok (T 'Tworze skroty' 'Creating shortcuts')
$shell = New-Object -ComObject WScript.Shell
$cele = @(
    (Join-Path ([Environment]::GetFolderPath('Desktop')) 'Ucho.lnk'),
    (Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\Ucho.lnk')
)
foreach ($cel in $cele) {
    try {
        $katalogSkrotu = Split-Path -Parent $cel
        if (-not (Test-Path $katalogSkrotu)) { New-Item -ItemType Directory -Force -Path $katalogSkrotu | Out-Null }
        $skrot = $shell.CreateShortcut($cel)
        $skrot.TargetPath = $VenvPyw
        # uruchom.pyw zamiast "-m ucho": dziala takze wtedy, gdy Windows
        # wystartuje skrot bez katalogu roboczego (np. z przypietego kafelka).
        $skrot.Arguments = "`"$(Join-Path $Root 'uruchom.pyw')`""
        $skrot.WorkingDirectory = $Root
        $skrot.Description = (T 'Lokalne nagrywanie i transkrypcja spotkan' 'Local meeting recording and transcription')
        if (Test-Path $Ikona) { $skrot.IconLocation = $Ikona }
        $skrot.Save()
        # AppUserModelID w skrocie - przypiety kafelek sklei sie z dzialajacym
        # oknem zamiast tworzyc drugi przycisk na pasku.
        & $VenvPy -c "from pathlib import Path; from ucho import config, winshell; winshell.ustaw_id_skrotu(Path(r'$cel'), config.ID_APLIKACJI)"
        Ok $cel
    } catch {
        Uwaga ((T 'Nie udalo sie utworzyc skrotu: ' 'Could not create shortcut: ') + $cel)
    }
}

# Wariant z konsola - przydatny, gdy cos nie dziala i chcesz zobaczyc blad.
$cmd = Join-Path $Root 'Ucho (diagnostyka).cmd'
@"
@echo off
cd /d "%~dp0"
".venv\Scripts\python.exe" -m ucho
echo.
pause
"@ | Out-File -FilePath $cmd -Encoding ascii
Ok $cmd

# Dyktuj - osobne okno do dyktowania na tym samym modelu Whisper.
try {
    & $VenvPy (Join-Path $Root 'dyktuj.pyw') --skroty | ForEach-Object { Ok $_ }
} catch {
    Uwaga (T 'Nie udalo sie utworzyc skrotow Dyktuj' 'Could not create the Dictate shortcuts')
}

Remove-Item -Recurse -Force $Tmp -ErrorAction SilentlyContinue

Write-Host ''
Write-Host (T '  Gotowe.' '  Done.') -ForegroundColor Green
Write-Host (T "  Uruchom skrotem 'Ucho' z Pulpitu lub Menu Start." "  Start it with the 'Ucho' shortcut on the Desktop or in the Start menu.") -ForegroundColor White
Write-Host (T '  Skrot globalny start/stop: Ctrl+Alt+R' '  Global start/stop shortcut: Ctrl+Alt+R') -ForegroundColor DarkGray
Write-Host ((T '  Transkrypcja liczona na: ' '  Transcription runs on: ') + $device) -ForegroundColor DarkGray
Write-Host ''
Write-Host (T '  Pamietaj o poinformowaniu uczestnikow, ze spotkanie jest nagrywane.' '  Remember to tell participants that the meeting is being recorded.') -ForegroundColor Yellow
Write-Host ''
