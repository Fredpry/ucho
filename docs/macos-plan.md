# Ucho na macOS: plan wykonania

Stan: 2026-11-19, gałąź `macos/plan` (kopia `main` 944721f), issue #22. Plan opiera się na dwóch raportach rozpoznania (mapa zależności od Windowsa, technologie macOS). Kluczowe twierdzenia mapy sprawdziłem w kodzie (sekcja 0). Decyzje Fryderyka z 2026-11-19: (1) Mac z Apple Silicon do testów ręcznych na końcu, wszystko inne weryfikujemy na runnerach macOS w CI; (2) beta bez podpisu Apple Developer (podpis ad-hoc, instrukcja otwarcia na stronie), podpis i notaryzacja później; (3) przycisk na stronie jako „beta” dopiero po zielonym CI i testach użytkownika na runnerze.

## 0. Co sprawdziłem w kodzie (i co z tego wynika)

| Twierdzenie | Wynik | Skutek dla planu |
|---|---|---|
| `ucho/dictation.py` woła `ctypes.WinDLL` w ciele modułu | potwierdzone, l. 58-59 (`_user32`, `_kernel32`) | na macOS `import ucho.dictation` (a więc i `ucho.app`) się wywraca; M1a musi to usunąć jako pierwsze |
| `ucho/audio.py` importuje `pyaudiowpatch` na poziomie modułu | potwierdzone, l. 41 | M1c: import przez backend platformy; `tests/test_audio_devices.py` podstawia atrapę w `sys.modules`, więc nadal ma działać |
| Testy podmieniają prymitywy WinAPI przez `monkeypatch.setattr(dictation, ...)` | potwierdzone, `tests/test_dictation_clipboard.py` l. 150-167 (`wait_for_keys_released`, `foreground_window`, `window_is_ours`, `is_shell_window`, `window_exists`, `window_process`, `activate`, `set_clipboard`, `get_clipboard_text`, `press_ctrl_v`, `press_enter`, `type_text`, `DelayedClipboard`); `test_engine_dictation_pause.py` i `test_live.py` podmieniają `dictation_target`, `insert_text` | refaktor musi zostawić te nazwy jako atrybuty modułu `ucho.dictation`, wołane przez nazwę modułu w czasie wywołania (inaczej atrapy przestaną działać) |
| `hotkeys.py`, `instance.py`, `winshell.py` nie wywracają importu poza Windows | **nieprawda (poprawka recenzji).** Wywołania WinAPI są w funkcjach, ale wszystkie trzy (oraz `dictation.py` l. 38, `tools/benchmark_engine.py` l. 34) robią na poziomie modułu `from ctypes import wintypes`. W Pythonie 3.12 `ctypes.wintypes` definiuje `VARIANT_BOOL` z `_type_ = "v"` (sprawdzone w źródle 3.12.10), a kod typu `v` istnieje tylko w buildzie Windows, więc na macOS sam import kończy się `ValueError`. `winshell.py` ma też na poziomie modułu struktury z polami `wintypes` (l. 34-51) | `import ucho.app`, `ucho.screens` (importuje `hotkeys`), `ucho.settings_screen` (importuje `winshell`) i `tests/test_package.py`, `tests/test_hotkeys.py` padają na macOS do czasu M1b. M1b jest więc na ścieżce krytycznej i zależnością M3b; test AST w M1a/M1b pilnuje, że poza `ucho/platforms/windows/` nikt nie importuje `wintypes` na poziomie modułu |
| `dictation.DelayedClipboard` w teście na prawdziwym schowku | `tests/test_dictation_delayed_clipboard.py` (proces potomny, l. 86-171) woła `d.DelayedClipboard("dictated", os.getpid())`: drugi argument to **pid** odbiorcy, nie HWND | konstruktor `DelayedClipboard(text, consumer_pid)` zostaje bez zmian; w `Clipboard.offer_delayed` parametr to `consumer_pid: int | None` |
| Dyktowanie przy braku modelu | `DictationController.model_key` (l. 928-937) już dziś przechodzi z niepobranego `dictation_model` na `settings["model"]`; `MeetingController._start` (l. 430-439) nie ma żadnego zapasu | brama M0 musi liczyć model tak samo jak kontrolery (patrz M0a), inaczej zablokuje dyktowanie, które dziś działa |
| `.venv-dev` w worktree | brak: `.venv-dev` jest tylko w `C:/Users/HP/Documents/Repozytoria/ucho/` | polecenie testów wykonawcy musi używać bezwzględnej ścieżki do tego interpretera (p. 4.0) |
| Ścieżki `settings.json`, `ucho.log`, `models/` w `config.PROJECT_DIR` | potwierdzone, `ucho/config.py` l. 20-55 | M2 (`ucho/paths.py`) przed paczką `.app` |
| Pobieranie modelu bez postępu, nagrywanie bez blokady | potwierdzone: `wizard._download` l. 239, `settings_screen` l. 176 i 486 pokazują tylko „Downloading...”; `MeetingController.start` (l. 180) nagrywa bez modelu, a `_start` (l. 436) dopiero po nagraniu zgłasza „The model is not downloaded”; `DictationController.start` (l. 1040) startuje mikrofon bez sprawdzenia | M0a i M0b (prośba użytkownika, obie platformy) |
| Job `test` w CI nie instaluje `requirements.txt` | potwierdzone, `.github/workflows/ci.yml` (tylko pytest, ruff, numpy, soundfile) | testy muszą przechodzić bez PyAudioWPatch, OpenVINO i PyObjC; tak samo na macOS |
| `requirements.txt` bez markerów platformy | potwierdzone (`PyAudioWPatch==0.2.12.8` bez `sys_platform`) | M3b dodaje markery |
| `release.yml` to jeden job z `gh release create` | potwierdzone | M10 dodaje job `macos` z `needs` i `gh release upload` |
| Strona: przycisk „Pobierz dla Windows” | potwierdzone, `public/index.html` l. 130, `public/en/index.html` l. 130; FAQ „Wersji na macOS jeszcze nie ma” w l. 101 (JSON-LD) i l. 209 (widoczny tekst); `operatingSystem` l. 55; `tools/check.py` zabrania półpauz i pauz, inline stylów i skryptów oraz hostów spoza `ucho.app` i `github.com/Fredpry/ucho` | M12 |
| Runner `macos-14` | potwierdzone w changelogu GitHub (2026-10-01): wycofany 2026-11-02 | CI na `macos-15` i `macos-26` (arm64). Skutek: **żaden runner nie testuje macOS 14**, więc wsparcie 14.x w becie byłoby niesprawdzone (decyzja D1 w p. 8) |
| Koła arm64 dla Pythona 3.12 (PyPI, 2026-11-19) | są: `openvino` 2026.4.1, `openvino-genai` 2026.4.1.0 (cp312 macosx_11_0_arm64), `openvino-tokenizers` (py3 arm64), `pywhispercpp` 1.5.1 (cp312 arm64, w środku `libwhisper.1.8.5`, `libggml-metal`), `sherpa-onnx`/`-core` 2.0.1, `soxr` 1.1.0 (abi3), `soundfile` 0.14.0, `numpy` 2.5.4, `pillow` 13.0.0, `pyobjc-*` 13.0 (universal2), `sounddevice` 0.5.6 i `pyinstaller` 6.22.3 (universal2), `catap` 0.6.1 (`macosx_14_0_universal2`), czyste `customtkinter`, `pystray` (na darwin ciągnie `pyobjc-framework-Quartz`), `huggingface_hub` 1.30.0 (ciągnie natywne `hf-xet`) | zależności wykonalne; uwagi w p. 2 (pywhispercpp, catap, telemetria OpenVINO) |
| API `pywhispercpp` 1.5.1 (źródło z koła) | `Model(model, ...)`: gdy `model` nie jest istniejącym plikiem, `utils.resolve_model_path` **sam pobiera** model przez `requests` z gałęzi `main` (bez przypiętej rewizji); `auto_detect_language(audio)` zwraca krotkę `((lang, prob), {lang: prob})`, nie słownik | M8: wolno podać tylko ścieżkę do istniejącego pliku `.bin`; atrapa w testach zwraca krotkę |
| `catap` 0.6.1 | zależności tylko PyObjC, ale koło zawiera natywną `catap/native/libcatap_coreaudio.dylib`; API `AudioRecorder(on_buffer=...)`, `capture_failed`, `excluded=` istnieje | M10 musi dołączyć i podpisać tę bibliotekę |

## 1. Cel i zakres bety

**Cel:** Ucho na Macu z Apple Silicon z tymi samymi funkcjami co na Windows: nagrywanie spotkania na dwóch ścieżkach (mikrofon i dźwięk systemowy), dyktowanie globalnym skrótem z wklejaniem do aktywnego okna, transkrypcja offline i na żywo, podsumowania i tytuły lokalnym LLM, serwer MCP. Paczka `Ucho-macOS-arm64.dmg` budowana w GitHub Actions i publikowana w GitHub Release obok paczek Windows; na ucho.app przycisk „Pobierz na Maca (beta)”.

**W becie działa:**
- macOS 15 lub nowszy (propozycja recenzji, decyzja D1 w p. 8: CI nie ma już runnera z macOS 14, a osobna lista „Tylko nagrywanie dźwięku systemowego” w Ustawieniach jest dopiero w macOS 15; 14.4 zostaje technicznie możliwe, jeśli Mac Fryderyka ma 14.x), Apple Silicon (arm64), `.app` w `.dmg`, podpis ad-hoc;
- nagrywanie mikrofonu (sounddevice) i dźwięku systemowego (Core Audio Process Tap);
- transkrypcja whisper.cpp z Metal (`pywhispercpp`), modele OpenVINO na CPU jako zapas; Parakeet, jeśli `sherpa-onnx` jest w paczce (decyzja w M10 według rozmiaru);
- podsumowania i tytuły przez OpenVINO GenAI na CPU (bez zmian w `summary.py`, `titles.py`);
- dyktowanie: skrót globalny, wklejanie ⌘V i tryb pisania, przywracanie schowka, Esc;
- pasek menu (tray), jedna instancja, autostart (LaunchAgent), ustawienia, archiwum i wyszukiwanie, serwer MCP (`Ucho.app/Contents/MacOS/Ucho --mcp`);
- pasek postępu pobierania modelu i blokada nagrywania i dyktowania, dopóki żaden model mowy nie jest gotowy (M0a, M0b, także na Windows; prośba Fryderyka: „blokować nagrywanie, dopóki nie jest jakikolwiek model transkrypcyjny gotowy”);
- kreator pierwszego uruchomienia z krokiem uprawnień (Mikrofon, Dźwięk systemowy, Dostępność).

**Świadomie później (poza betą):**
- podpis Developer ID, notaryzacja, hardened runtime z entitlements (osobny krok po becie);
- Mac z procesorem Intel (x86_64) i paczka universal2;
- macOS starszy niż wersja minimalna z D1 (ScreenCaptureKit jako alternatywne źródło dźwięku);
- automatyczne aktualizacje (Sparkle itp.); na razie ręczne pobranie nowego `.dmg`;
- Core ML lub ANE dla Whispera, LLM z Metal (`llama-cpp-python`), MLX;
- menu Docka zamiast Jump Listy, postęp na ikonie w Docku;
- powiadomienia systemowe (zostają toasty w oknie, jak na Windows);
- sklep Mac App Store i sandbox.

## 2. Wybrane technologie

| Obszar | Wybór | Uzasadnienie |
|---|---|---|
| Dźwięk systemowy | Core Audio Process Tap przez `catap==0.6.1` (MIT, zależności tylko PyObjC, ale w kole jest natywna `libcatap_coreaudio.dylib`, którą M10 musi dołączyć); gdy catap zawiedzie, własny kod na `pyobjc-framework-CoreAudio` wzorem AudioCap (ok. 300 linii) | Łagodniejsze uprawnienie „Tylko nagrywanie dźwięku systemowego” zamiast „Nagrywania ekranu”, bez comiesięcznego przypomnienia; łapie miks niezależnie od wyjścia; można wykluczyć własny proces (piknięcia Ucho nie trafiają na ścieżkę „Meeting”). Wymaga macOS 14.2+; `LSMinimumSystemVersion` według D1 (propozycja 15.0, technicznie możliwe 14.4) |
| Mikrofon | `sounddevice==0.5.6` (koło universal2 z PortAudio) | PyAudioWPatch i PyAudio nie mają kół dla macOS; resampling (`soxr`), zapis FLAC (`soundfile`) i kontrola dryfu w `audio._Track` zostają wspólne |
| Transkrypcja | `pywhispercpp==1.5.1` (whisper.cpp 1.8.5 z Metal), nowy `ucho/whispercpp.py` z powierzchnią `transcribe.Engine` | Metal kilkukrotnie szybszy od CPU; `auto_detect_language()` zwraca prawdopodobieństwa języków, więc da się zachować zawężanie do `config.LANGUAGES` (poprawka 8/8 z `docs/architecture.md` p. 3); bez PyTorch. OpenVINO CPU zostaje jako zapas bez nowego kodu. Uwaga: pakiet ciągnie `requests`, `tqdm`, `platformdirs` i przy nazwie modelu zamiast ścieżki sam pobiera plik z gałęzi `main`; Ucho podaje wyłącznie ścieżkę do pliku pobranego przez `huggingface_hub` z przypiętą rewizją. Biblioteki `libggml*.dylib` leżą w kole także w katalogu głównym `site-packages` (M10) |
| Telemetria OpenVINO | `openvino` ciągnie `openvino-telemetry`; `install.ps1` (l. 301-307) zapisuje opt-out. Na Macu paczka robi to samo przy pierwszym starcie (M10) | Ta sama obietnica prywatności co na Windows |
| LLM | `openvino-genai==2026.4.1.0` na CPU, bez zmian w kodzie | Koło macOS arm64 ma `LLMPipeline`, `StructuredOutputConfig` i przerywanie streamerem; te same modele i walidacja GGUF; podsumowanie działa w tle, więc szybkość CPU wystarcza na betę |
| Skróty globalne | Carbon `RegisterEventHotKey` przez ctypes, z opcją `kEventHotKeyExclusive` | Nie wymaga uprawnień TCC; ta sama semantyka co `RegisterHotKey` (zajęty skrót jest raportowany; bez `kEventHotKeyExclusive` skrót zajęty przez inny proces nie daje błędu, sprawdza M3a p. 4); zdarzenia przez pętlę NSApp, którą obsługuje Tk |
| Wklejanie, schowek, fokus | PyObjC 13.0: `Quartz` (`CGEventPost` ⌘V, `CGEventKeyboardSetUnicodeString`), `Cocoa` (`NSPasteboard`, `NSWorkspace.frontmostApplication`), `ApplicationServices` (`AXIsProcessTrustedWithOptions`) | Jedyna droga do wklejania w innej aplikacji; wymaga Dostępności; bez niej tekst zostaje w schowku z komunikatem, jak dziś przy „stays on clipboard” |
| Pasek menu | obecny `pystray==0.19.5` (backend darwin) na wątku Tk | Zero nowego kodu; spike w M3a. Plan B (gdy spike lub smoke test padnie): własny `NSStatusItem` przez PyObjC za interfejsem `tray.TrayIcon` |
| Jedna instancja | `fcntl.flock` na pliku blokady + gniazdo Unix na komendy `show`/`dictate`/`record` | Proste, testowalne na runnerze; ścieżka gniazda krótsza niż 104 znaki |
| Autostart | LaunchAgent `~/Library/LaunchAgents/app.ucho.plist` (`/usr/bin/open -a <ścieżka .app> --args --minimized`, `RunAtLoad`) | Działa przy podpisie ad-hoc, plik da się sprawdzić `plutil -lint` w CI |
| Ścieżki | `~/Library/Application Support/Ucho/` (ustawienia, modele, blokada), `~/Library/Logs/Ucho/ucho.log`, nagrania jak na Windows w `~/Ucho/recordings` | Bundle `.app` jest tylko do odczytu (zapis psuje podpis, App Translocation); iCloud „Biurko i Dokumenty” nie synchronizuje katalogu domowego |
| Ikony interfejsu na Macu | podzbiór czcionki Fluent UI System Icons (MIT) w `design/icons/` | `ui.py` rysuje ikony z czcionki Segoe Fluent z `%WINDIR%\Fonts`; Fluent ma ten sam styl, licencja pozwala dołączyć plik; Windows zostaje przy Segoe |
| Czcionki | `theme.py`: na Macu `.AppleSystemUIFont`/„SF Pro Text”, „Helvetica Neue”, „SF Mono”/„Menlo” | Systemowe, bez dołączania plików |
| Dźwięki dyktowania | `NSSound` z dźwiękami systemowymi („Tink”, „Pop”, „Basso”) | `winsound.Beep` nie istnieje; trzy nazwane zdarzenia zamiast częstotliwości |
| Pakowanie | `pyinstaller==6.22.3` + `pyinstaller-hooks-contrib==2026.10`, onedir z `BUNDLE`, `target_arch="arm64"` | Najlepiej sprawdzony z tkinter i bibliotekami natywnymi; sam podpisuje ad-hoc binaria na arm64 |
| Podpis | ad-hoc (`codesign --force --deep -s -`), weryfikacja `codesign --verify --deep --strict`, bez hardened runtime | Decyzja Fryderyka nr 2. Skutek: po każdej aktualizacji trzeba ponownie nadać uprawnienia (sekcja 8) |
| Obraz dysku | `hdiutil create -format ULFO` z katalogu `Ucho.app` + dowiązanie `Applications` | Bez Findera i AppleScriptu, bez nowej zależności |
| CI | `macos-15` (wszystkie joby) i `macos-26` (testy), osobny plik `.github/workflows/macos.yml` | `macos-14` wycofany; Windowsowe `ci.yml` zostaje nietknięte |

Odrzucone: `mlx-whisper` (ciągnie `torch`, a AGENTS.md zabrania PyTorch; MLX nie działa w CI bez Metal), `pynput` (wymaga Monitorowania wprowadzania, zgłaszane crashe), ScreenCaptureKit jako domyślne źródło (gorsze uprawnienie), `create-dmg` (AppleScript w CI), `py2app` i `briefcase` (mniej hooków dla OpenVINO, niepewne Tk).

## 3. Warstwa platformy

### 3.1 Układ plików

```
ucho/paths.py                  ścieżki danych (M2); czyste funkcje, bez importów systemowych
ucho/platforms/__init__.py     family(), accessory, override() dla testów
ucho/platforms/base.py         Protocol-e i dataclassy wspólne
ucho/platforms/null.py         implementacje puste ("brak dostępu"), dla Linuksa i testów
ucho/platforms/windows/        clipboard.py, inserter.py, sounds.py      (M1a, kod z dictation.py)
                               hotkeys.py, instance.py, shell.py         (M1b, kod z hotkeys/instance/winshell/app)
                               audio.py, sysinfo.py, fonts.py            (M1c, kod z audio/hardware/i18n/theme/ui)
ucho/platforms/macos/          sysinfo.py, shell.py, sounds.py, fonts.py (M4)
                               audio.py (mikrofon, M7a), audio_tap.py (M7b)
                               instance.py, hotkeys.py                   (M5)
                               clipboard.py, inserter.py                 (M6)
                               permissions.py                            (M7b; interfejs i wersja null z M1a)
ucho/downloads.py              menedżer pobierania i brama modelu mowy (M0a)
ucho/download_ui.py            pasek pobierania i baner (M0b; osobny plik, żeby nie konfliktować z M1c w ui.py)
```

Nazwa `platforms` (liczba mnoga) celowo nie zasłania modułu biblioteki standardowej `platform`.

### 3.2 Zasada wyboru implementacji

- `family() -> Literal["windows", "macos", "null"]`: `"win32"` daje `windows`, `"darwin"` daje `macos`, reszta `null`.
- Każdy accessor ładuje leniwie moduł `ucho.platforms.<family>.<name>` i bierze z niego funkcję `create()`. Gdy moduł nie istnieje (np. jeszcze nienapisany dla macOS), bierze implementację z `null.py` i loguje raz `info`. Dzięki temu zadania dodają tylko nowe pliki i nie konfliktują na `__init__.py`.
- Moduły `macos/*`, `base.py`, `null.py` i `__init__.py` nie importują niczego systemowego w ciele modułu (PyObjC, sounddevice, catap, `fcntl` jest dozwolony): tylko w funkcjach lub w `create()`, więc importują się także na Windows (testy z `override("macos")`). Moduły `windows/*` mogą na poziomie modułu używać `ctypes.wintypes` i struktur WinAPI (przeniesiony kod zostaje 1:1), ale importuje je wyłącznie accessor przy prawdziwym `sys.platform == "win32"`. Test `tests/test_platforms.py` importuje `base`, `null`, `__init__` i `macos/*` na każdym systemie, `windows/*` tylko na Windows, oraz (test AST, na każdym systemie) sprawdza, że żaden plik `ucho/*.py`, `ucho_mcp/*.py` ani `ucho/platforms/{base,null,__init__}.py`, `ucho/platforms/macos/*.py` nie ma na poziomie modułu importu `ctypes.wintypes`, `winreg`, `winsound`, `msvcrt`, `pyaudiowpatch`, PyObjC ani wywołania `ctypes.WinDLL`/`ctypes.windll`.
- Wersje `null` mają semantykę „aplikacja działa bez funkcji systemowych”: `SingleInstance.acquire()` zwraca `True` (inaczej aplikacja uznałaby, że już działa), `signal()` zwraca `False`; `GlobalHotkeys.start` zwraca błąd dla każdego skrótu, a aplikacja loguje go na poziomie `warning` (smoke test pada na `ERROR`); `Shell.start_command()` zwraca `[sys.executable, "-m", "ucho"]`; `Fonts` zwraca krotki z rodzinami Tk (`"Helvetica"`, `"Courier"`) i `icon_font() = None` (UI ma już ścieżkę bez czcionki ikon).
- `override(family)` (menedżer kontekstu, tylko do testów) wymusza rodzinę i czyści pamięć podręczną accessorów.
- Accessory zwracają singletony (`clipboard()`, `inserter()`, `sounds()`, `shell()`, `audio()`, `sysinfo()`, `fonts()`, `permissions()`), a dla obiektów ze stanem fabryki (`hotkeys(action)`, `instance()`).

### 3.3 Interfejsy (`ucho/platforms/base.py`)

```python
from typing import Literal, Protocol
from collections.abc import Callable

Permission = Literal["granted", "denied", "unknown", "unsupported"]
WindowRef = int            # HWND na Windows, pid procesu na macOS; 0 = brak

class Clipboard(Protocol):
    def get_text(self) -> str | None: ...
    def set_text(self, text: str | None, private: bool = True) -> int: ...   # numer sekwencji
    def sequence(self) -> int: ...            # GetClipboardSequenceNumber / NSPasteboard.changeCount
    def offer_delayed(self, text: str, consumer_pid: int | None) -> "DelayedOffer": ...  # pid, jak dziś DelayedClipboard(text, pid)

class DelayedOffer(Protocol):                 # dzisiejszy dictation.DelayedClipboard
    rendered: bool
    lost: bool
    def pump(self, seconds: float, until: Callable[[], bool] | None = None) -> bool: ...
    def restore(self, previous: str | None) -> bool: ...
    def close(self) -> None: ...

class TextInserter(Protocol):
    def foreground(self) -> WindowRef | None: ...
    def process_of(self, window: WindowRef) -> int | None: ...
    def is_ours(self, window: WindowRef) -> bool: ...
    def is_shell(self, window: WindowRef) -> bool: ...    # pulpit, pasek zadań, Dock, Finder-pulpit
    def exists(self, window: WindowRef) -> bool: ...
    def activate(self, window: WindowRef) -> bool: ...
    def paste(self) -> None: ...                          # Ctrl+V / Cmd+V
    def press_enter(self) -> None: ...
    def type_text(self, text: str) -> None: ...
    def wait_keys_released(self, limit_s: float = 1.0) -> None: ...
    def paste_label(self) -> str: ...                     # "Ctrl+V" / "⌘V" do komunikatów
    def permission(self) -> Permission: ...               # Dostępność na macOS, "granted" na Windows
    def request_permission(self) -> None: ...

class Sounds(Protocol):
    def play(self, event: Literal["start", "stop", "cancel"]) -> None: ...

class GlobalHotkeys(Protocol):
    def start(self, bindings: dict[str, str]) -> dict[str, str]: ...   # nazwa -> komunikat błędu
    def escape(self, enable: bool) -> None: ...
    def stop(self) -> None: ...
# fabryka: hotkeys(action: Callable[[str], None]) -> GlobalHotkeys

class SingleInstance(Protocol):              # ten sam interfejs co dziś ucho/instance.py
    def acquire(self) -> bool: ...
    def listen(self, handler: Callable[[str], None]) -> None: ...
    def signal(self, command: str) -> bool: ...
    def close(self) -> None: ...

class ProgressBar(Protocol):
    def set(self, state: Literal["none", "normal", "indeterminate", "paused", "error"],
            value: float = 0.0) -> None: ...

class Shell(Protocol):
    def set_app_identity(self) -> None: ...                       # AppUserModelID; macOS no-op
    def window_handle(self, tk_window) -> int: ...
    def describe_window(self, tk_window) -> bool: ...
    def after_start(self, app_id: str, tasks: list[tuple[str, str]]) -> None: ...  # Jump List, skróty .lnk
    def progress(self, tk_window) -> ProgressBar | None: ...
    def start_command(self) -> list[str]: ...                     # jak uruchomić aplikację ponownie
    def worker_command(self, module: str, args: list[str]) -> list[str]: ...  # podproces (diarize, pobieranie)
    def autostart_get(self) -> str | None: ...
    def autostart_set(self, enabled: bool) -> bool: ...
    def open_path(self, path) -> None: ...
    def work_area(self) -> tuple[int, int, int, int] | None: ...
    def show_without_focus(self, tk_window) -> None: ...
    def set_window_icon(self, tk_window) -> None: ...             # .ico / iconphoto PNG
    def autostart_label(self) -> str: ...                         # "Start with Windows" / "Open at login"

class AudioBackend(Protocol):
    name: str
    def list_devices(self) -> dict: ...   # ten sam słownik co dziś audio.list_devices()
    def open_input(self, device: "DeviceInfo", channels: int, rate: int, frames: int,
                   on_data: Callable[[bytes], None]) -> "InputStream": ...  # int16, przeplot
    def permission(self, kind: Literal["microphone", "system_audio"]) -> Permission: ...
    def request_permission(self, kind: Literal["microphone", "system_audio"]) -> None: ...

class InputStream(Protocol):
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def close(self) -> None: ...

class SystemInfo(Protocol):
    def ram_gb(self) -> tuple[float, float, float | None]: ...   # total, available, installed
    def cpu_name(self) -> str: ...
    def physical_cores(self) -> int | None: ...
    def cpu_features(self) -> dict[str, bool]: ...                # avx2, avx512, arm64, ...
    def adapters(self) -> list["GraphicsAdapter"]: ...
    def ui_language(self) -> str | None: ...                     # "pl", "en", ...
    def reduce_motion(self) -> bool: ...

class Fonts(Protocol):
    sans: tuple[str, ...]; display: tuple[str, ...]; mono: tuple[str, ...]
    numeric: tuple[str, ...]; strong: dict[str, tuple[str, ...]]
    def icon_font(self) -> str | None: ...                        # ścieżka pliku czcionki ikon
    def glyphs(self) -> dict[str, str]: ...                       # nazwa ikony -> znak
    def ui_font_file(self, bold: bool) -> str | None: ...         # dla Pillow (ui._ui_font)

class Permissions(Protocol):                  # M1a: interfejs + null; M7b: macOS
    def state(self) -> dict[str, Permission]: ...                 # microphone, system_audio, accessibility
    def request(self, kind: str) -> None: ...
    def settings_url(self, kind: str) -> str | None: ...          # x-apple.systempreferences:...; None na Windows
```

`DeviceInfo` zostaje w `ucho/audio.py` (testy go używają), `base.py` importuje go leniwie. `GraphicsAdapter` zostaje w `hardware.py`.

Ścieżki to nie Protocol, tylko czyste funkcje w `ucho/paths.py` (M2): `app_dir()`, `data_dir()`, `models_dir()`, `settings_file()`, `log_file()`, `lock_dir()`, `recordings_dir()`, `cloud_roots()`. Każda przyjmuje opcjonalne `system`, `home`, `env` do testów.

## 4. Zadania

### 4.0 Zasady wspólne dla wszystkich zadań (wchodzą do każdego SPEC)

- Kod po angielsku, nagłówek SPDX w nowych plikach (`tools/set_copyright.py --check`), `from __future__ import annotations`, typy, `logging`, bez `print`.
- Każdy tekst dla użytkownika przez `t("...")` z wpisem w `ucho/locale/pl.json` (`tests/test_i18n.py`). Nowe wpisy dopisywać na końcu pliku, w jednym bloku, żeby scalanie równoległych gałęzi było proste.
- Zero nowych połączeń sieciowych poza pobieraniem modeli z Hugging Face; bez PyTorch; bez prawdziwych nagrań i nazwisk w testach.
- Istniejące testy zostają zielone na Windows i nie są zmieniane, z wyjątkiem jawnie wymienionych w zadaniu (najczęściej zdjęcie `skipif`, gdy test zaczyna działać wszędzie).
- Polecenie testów wykonawcy (Windows, jedno, uruchamiane w katalogu worktree zadania): `C:\Users\HP\Documents\Repozytoria\ucho\.venv-dev\Scripts\python.exe -m pytest -q tests` (worktree nie ma własnego `.venv-dev`; `tests/conftest.py` wstawia katalog worktree na początek `sys.path`, więc testowany jest kod zadania). Ukryte testy lidera zostają poza repo i lider uruchamia je sam, kopiując na chwilę do `tests/_hidden/` (żeby działał `conftest.py` z `english_ui`); nie wchodzą do commita. Potem lider: `ruff check .`, `python tools/set_copyright.py --check`, `python -m ucho --smoke-test 3`.
- Refaktory przenoszące kod (M1a, M1b, M1c): ukryty test lidera porównuje `ast.dump` każdej przeniesionej funkcji i klasy z wersją z `944721f` (`git show 944721f:ucho/<plik>`), z dopuszczalną zmianą wyłącznie nazw modułów (`_user32` itp.). Wykonawca dostaje w SPEC listę przenoszonych nazw i zakaz „ulepszania” ich treści.
- „Tylko macOS” w kryteriach oznacza testy z `pytest.mark.skipif(sys.platform != "darwin")`, uruchamiane przez lidera w jobie `macos-tests` (`macos.yml`).
- Delegowalne „tak”: wykonawca bez Maca da radę, bo wszystko sprawdzają testy na Windows (atrapy modułów w `sys.modules`, `platforms.override`). „Nie”: kod wymaga API Apple i iteracji na runnerze; robi lider.

### M0a. Menedżer pobierania i brama modelu mowy, bez UI (Windows i macOS)

- **Szacunek:** ok. 400 linii. **Delegowalne:** tak. **Zależności:** brak. **Fala:** 1.
- **Pliki:** nowy `ucho/downloads.py`, `ucho/meeting.py` (`MeetingController.start` l. 180, `_start` l. 430-439), `ucho/dictation.py` (tylko `DictationController.model_key` l. 928-937 i `DictationController.start` l. 1040-1075), `ucho/locale/pl.json`, testy `tests/test_downloads.py`, `tests/test_speech_gate.py`.
- **SPEC:**
  - `downloads.filter_files(files: list[tuple[str, int]], patterns: list[str] | None) -> list[tuple[str, int]]`: te same reguły co `allow_patterns` w `huggingface_hub` (fnmatch; `None` = wszystko).
  - `downloads.bytes_on_disk(hub_dir: Path, repo: str) -> int`: suma rozmiarów plików w `hub_dir/models--{org}--{name}/blobs`, łącznie z `*.incomplete`; brak katalogu = 0.
  - `downloads.Rate`: średnia prędkość z okna 5 s i ETA; czyste funkcje z jawnym czasem (`add(t, done)`, `speed()`, `eta(total)`).
  - `downloads.enough_space(total: int, free: int) -> bool`: `free >= 1.1 * total`.
  - `@dataclass(frozen=True) Progress(repo, state, done, total, speed, eta_s, error)`, `state ∈ {"starting","downloading","done","failed","cancelled"}`.
  - Proces roboczy `python -m ucho.downloads --worker <repo>` (wzorzec `diarize.worker_python()` i `_CREATION_FLAGS`; na tym etapie **bez** `platforms.shell()`, które powstaje dopiero w M1b): najpierw liczy rozmiar (`HfApi().model_info(repo, revision=config.REVISIONS[repo], files_metadata=True)` + `filter_files` z `config.DOWNLOAD_PATTERNS`) i wypisuje na stdout jedną linię JSON `{"total": N}` albo `{"total": null}`; sprawdza `shutil.disk_usage(config.MODELS_DIR)` i przy `not enough_space` wypisuje `{"error": "disk", "needed": N, "free": F}` i kończy kodem 4; potem woła `transcribe.model_path(repo, download=True)` i kończy kodem 0. Zapytanie sieciowe jest więc tylko w procesie roboczym, nigdy na wątku UI.
  - `downloads.DownloadManager(emit, hub_dir, command=None, poll_s=0.5)`: `start(repo) -> bool` (False, gdy to samo repo już się pobiera; kolejne repo czeka w kolejce FIFO, pobiera się jedno naraz), `cancel(repo)`, `progress(repo) -> Progress | None`, `busy() -> bool`, `shutdown()`. Wątek czytający stdout procesu bierze `total`; rodzic co `poll_s` liczy `bytes_on_disk` i wywołuje `emit(progress)` (aplikacja przekazuje tu `Emitter`/`app.post`, test zwykłą listę). `command(repo) -> list[str]` do testów.
  - Kod 4 daje `failed` z komunikatem „Not enough disk space: {needed} needed, {free} free.”; inny kod ≠ 0 daje `failed` z ostatnią linią stderr (bez ścieżek użytkownika), a gdy stderr wskazuje na brak połączenia: „Cannot reach Hugging Face. Check the connection and try again.”. Anulowanie zabija proces, zostawia `*.incomplete` (huggingface_hub wznawia pobieranie), stan `cancelled`; `shutdown()` anuluje wszystko.
  - **Brama (zgodnie z prośbą Fryderyka: „dopóki nie jest jakikolwiek model transkrypcyjny gotowy”).** `downloads.speech_state(selected: str, offered: list[str], downloaded: Callable[[str], bool], progress: Progress | None) -> GateState`, czysta funkcja. `GateState(ready: bool, reason: "ready"|"fallback"|"downloading"|"missing", model: str, effective: str | None, progress: Progress | None)`. `effective` = wybrany model, gdy pobrany; inaczej pierwszy pobrany z `offered`; `ready = effective is not None`. `reason`: `ready` (wybrany pobrany), `fallback` (używany jest inny pobrany model, np. w trakcie pobierania nowego), `downloading` (nic nie jest pobrane, trwa pobieranie), `missing` (nic nie pobrane i nic się nie pobiera).
  - `transcribe.model_downloaded` dotyka dysku, więc wynik dla każdego modelu jest buforowany do następnego zdarzenia pobierania lub zmiany ustawień (wzorzec `DictationController._model_downloaded`).
  - `MeetingController.start` i `DictationController.start`: gdy `getattr(self.app, "speech_gate", None)` istnieje i zwraca `ready == False`, nie otwierają urządzeń, zwracają `False` i emitują komunikat („The speech model is still downloading ({percent} %).” albo „No speech model is downloaded yet. Download one in Settings.”) oraz zdarzenie `"gate"`, na które aplikacja (M0b) pokazuje okno. Fałszywe `app` w obecnych testach (`FakeApp`, `SimpleNamespace`) nie mają `speech_gate`, więc ich zachowanie się nie zmienia. Zatrzymanie trwającego nagrania działa zawsze.
  - `MeetingController._start`: gdy `settings["model"]` nie jest pobrany, a jest inny pobrany (`speech_state(...).effective`), transkrybuje nim, zapisuje go w `meta["model"]` i emituje informację „Transcribed with {model}: {selected} is not downloaded yet.”; komunikat „The model is not downloaded — open Settings.” zostaje tylko dla przypadku „żaden model”. `DictationController.model_key`: `dictation_model`, gdy pobrany; inaczej `settings["model"]`, gdy pobrany; inaczej `effective`; logowanie bez zmian.
- **Kryteria akceptacji (ukryte testy, Windows, bez sieci i bez modeli):**
  1. `filter_files` z wzorcami z `config.DOWNLOAD_PATTERNS` zwraca dokładnie pliki pasujące; `None` zwraca wszystkie.
  2. `bytes_on_disk` na `tmp_path` z plikami `blobs/a`, `blobs/b.incomplete` zwraca sumę; brak katalogu daje 0.
  3. `Rate`: stałe tempo 10 MB/s przez 6 s daje `speed` 10 MB/s ±1 % i `eta(total)` zgodne z resztą; `enough_space` na granicy 1,1.
  4. `DownloadManager` z `command` uruchamiającym atrapę (skrypt Pythona w `tmp_path`, który wypisuje `{"total": N}`, zapisuje pliki do katalogu `blobs` i kończy kodem 0, 3 lub 4) emituje `starting → downloading… → done` z rosnącym `done`, albo `failed` (z tekstem o miejscu na dysku dla kodu 4); `start` tego samego repo w trakcie zwraca `False`; drugie repo czeka i startuje po pierwszym; `cancel` kończy proces w < 2 s i daje `cancelled`; `shutdown` nie zostawia żywych procesów.
  5. `speech_state`: tabela przypadków (wybrany pobrany / inny pobrany / nic i pobieranie trwa / nic) daje `ready`, `fallback`, `downloading`, `missing` z właściwym `effective`.
  6. `MeetingController.start` z atrapą `app.speech_gate` zwracającą `ready=False` zwraca `False` i nie tworzy `audio.Recorder` (atrapa liczy wywołania); z `ready=True` zachowuje się jak dziś. To samo dla `DictationController.start` i mikrofonu (atrapa `microphone.start` nie jest wołana).
  7. `MeetingController._start` z atrapą `transcribe.model_downloaded` (wybrany niepobrany, inny pobrany) transkrybuje innym modelem i zapisuje go w `meta["model"]`; gdy żaden nie jest pobrany, zachowanie jak dziś.
  8. Cały obecny zestaw testów zielony; wszystkie nowe teksty mają wpisy w `pl.json`.
- **Uwaga lidera przed zleceniem (10 min, Windows):** pobrać ręcznie mały przypięty model (np. Qwen2.5-1.5B z `config.REVISIONS`) z `huggingface_hub==1.30.0` i obserwować `blobs/`: czy przy pobieraniu przez `hf-xet` rozmiar `*.incomplete` rośnie stopniowo, czy plik jest od razu alokowany w pełnym rozmiarze. W drugim przypadku proces roboczy ustawia `HF_HUB_DISABLE_XET=1` (wpis do SPEC przed zleceniem).

### M0b. Pasek pobierania i baner bramy w interfejsie (Windows i macOS)

- **Szacunek:** ok. 350 linii. **Delegowalne:** tak (logika w M0a; UI sprawdza smoke test i zrzuty lidera). **Zależności:** M0a. **Fala:** 2 (scalana po M1b, M1c, M8; fragmenty rozłączne, patrz p. 5).
- **Pliki:** nowy `ucho/download_ui.py` (`DownloadBar`, `GateBanner`), `ucho/wizard.py` (`_download` l. 239-256, `_show_hardware` l. 202-209), `ucho/settings_screen.py` (tylko oba `_download`, l. 176-200 i 486-500, oraz miejsca, które pokazują stan modelu w l. 470-485), `ucho/screens.py` (konstruktory ekranów Meeting i Dictation: jedna linia dodająca `GateBanner`; stan przycisku nagrywania i dyktowania), `ucho/app.py` (`App.downloads`, `App.speech_gate()`, zamknięcie aplikacji woła `downloads.shutdown()`, obsługa zdarzenia `"gate"`: pokaż okno), `ucho/tray.py` (etykiety „Record”/„Dictate” ze stanem), `ucho/locale/pl.json`, testy `tests/test_download_ui.py`.
- **SPEC:**
  - `download_ui.DownloadBar` (CTkProgressBar, „{done} of {total} GB”, prędkość, ETA, przycisk „Cancel”; tryb nieokreślony, gdy `total is None`; po `failed` tekst błędu i przycisk „Try again”). Formatowanie tekstu w czystej funkcji `download_ui.describe(progress) -> str` (testowalnej bez Tk).
  - Ten sam widżet w kreatorze, w Ustawieniach (Whisper i LLM) i w `GateBanner` na ekranach Meeting i Dictation. Kreator pozwala przejść dalej w trakcie pobierania (pobieranie trwa w `App.downloads`).
  - `GateBanner`: `missing`: „No speech model yet. Recording starts after you download one.” + przycisk „Download ({size})” dla wybranego modelu; `downloading`: pasek; `fallback`: jednowierszowa informacja „Using {effective} until {model} is downloaded.” z paskiem; `ready`: baner ukryty.
  - Przycisk nagrywania i przycisk dyktowania są wyłączone, gdy brama zamknięta; skrót globalny, pozycja tray i `--record`/`--dictate` przy zamkniętej bramie pokazują okno z banerem zamiast milczeć. Pozycje tray „Record”/„Dictate” mają w etykiecie dopisek „(downloading model)” albo „(no model)”.
  - Pobieranie modeli mówców (`settings_screen._download_speakers`, l. 566) zostaje bez zmian (poza zakresem).
- **Kryteria akceptacji:**
  1. `describe()` dla `Progress` z `total` i bez `total`, dla `failed` i `cancelled` daje oczekiwane teksty (po angielsku, `english_ui`).
  2. Z atrapą `App` (bez Tk): `speech_gate()` przechodzi z `missing` do `ready` po zdarzeniu `done` (atrapa `model_downloaded`); zdarzenie `"gate"` woła `show()`.
  3. `--smoke-test 3` zielony na Windows (`gui-smoke`), także z atrapą `speech_gate` w stanie `downloading` (flaga środowiskowa tylko dla smoke testu, np. `UCHO_SMOKE_GATE=downloading`).
  4. Lider: zrzuty `tools/screenshots.py` ekranu Meeting w stanach `missing`, `downloading`, `fallback` i kreatora z paskiem, w obu motywach; `pl.json` kompletne.

### M1a. Szkielet `ucho/platforms/` i prymitywy dyktowania (refaktor bez zmiany zachowania)

- **Szacunek:** ok. 750 linii (głównie przeniesienie). **Delegowalne:** tak. **Zależności:** brak. **Fala:** 1.
- **Pliki:** nowe `ucho/platforms/{__init__,base,null}.py`, `ucho/platforms/windows/{__init__,clipboard,inserter,sounds}.py`, `ucho/dictation.py` (bez `DictationController.start` i `model_key`, które zmienia M0a w tej samej fali), nowe `tests/test_platforms.py`; `tests/test_dictation_delayed_clipboard.py` zostaje bez zmian i nadal przechodzi na Windows; zdjęcie `skipif` w `tests/test_dictation_clipboard.py` (cały plik), `tests/test_engine_dictation_pause.py` (4 testy), `tests/test_live.py` (2 testy), `tests/test_dictate_settings.py`, jeśli przejdą na atrapach (na Windows i tak się wykonują).
- **SPEC:**
  - `platforms/__init__.py`: `family()`, `override(family)`, accessory z p. 3.2 dla wszystkich interfejsów z p. 3.3 (także tych, które dopiero powstaną; brakujący moduł daje `null`).
  - `base.py`: wszystkie Protocol-e z p. 3.3 (także `Permissions`). `null.py`: implementacje, które nic nie robią i zwracają wartości „brak” (`None`, `False`, `{}`, `"unsupported"`), z wyjątkami z p. 3.2 (`SingleInstance.acquire() -> True`, `start_command`, czcionki Tk); `GlobalHotkeys.start` zwraca błąd dla każdego skrótu („Global hotkeys are not supported on this system.”). `Permissions.state()` w null i na Windows zwraca `"unsupported"` dla wszystkich trzech rodzajów (Windows nie ma tych zgód w TCC); `windows/permissions.py` nie powstaje, accessor bierze null.
  - Kod WinAPI z `dictation.py` l. 33-553 i `beep` (l. 679-691) przeniesiony bez zmiany logiki do `windows/clipboard.py` (schowek, formaty prywatne, `DelayedClipboard`), `windows/inserter.py` (SendInput, okna, `GetAsyncKeyState`), `windows/sounds.py` (`winsound.Beep` z dotychczasowymi częstotliwościami pod nazwami `start`, `stop`, `cancel`).
  - W `ucho/dictation.py` zostają atrybuty modułu o dotychczasowych nazwach: `set_clipboard`, `get_clipboard_text`, `clipboard_sequence`, `DelayedClipboard`, `press_ctrl_v`, `press_enter`, `type_text`, `wait_for_keys_released`, `foreground_window`, `window_process`, `window_is_ours`, `window_exists`, `is_shell_window`, `dictation_target`, `activate`, `beep`, jako cienkie funkcje delegujące do `platforms.clipboard()`, `platforms.inserter()`, `platforms.sounds()`. Orkiestracja (`paste_target`, `insert_text`, `_paste`, `DictationController`) woła je przez nazwy modułu, więc `monkeypatch.setattr(dictation, ...)` nadal działa.
  - `import ucho.dictation` nie importuje w ciele modułu `ctypes.wintypes`, `ctypes.WinDLL` ani `winsound` (sama linia `from ctypes import c_void_p, wintypes`, l. 38, wywraca import na macOS, patrz p. 0); `dictation.Microphone` importuje `pyaudiowpatch` jak dziś, leniwie (zmiana źródła mikrofonu to M1c).
  - `dictation.beep(frequency, ms=70)` zachowuje sygnaturę i wywołania (l. 1072, 1127, 1231 bez zmian, bo l. 1072 leży w `DictationController.start`, który zmienia M0a); mapuje częstotliwość na zdarzenie: 1046 → `start`, 659 → `stop`, 330 → `cancel`, inne → `start`. `windows/sounds.py` gra dla zdarzeń dokładnie dzisiejsze częstotliwości i czasy.
  - Komunikat l. 1201 „press Ctrl+V” dostaje parametr z `inserter().paste_label()` (tekst źródłowy „Text is on the clipboard, press {keys}.”; nowy wpis w `pl.json`).
- **Kryteria akceptacji:**
  1. Cały obecny zestaw testów zielony na Windows (lider sprawdza też liczbę wykonanych testów: nie mniejsza niż przed zmianą).
  2. `import ucho.dictation` w podprocesie, który przed importem ustawia `sys.platform = "linux"`, przechodzi, a `platforms.family()` daje `null` (sam `ucho.app` nie jest tu sprawdzany: importuje `customtkinter`, którego nie ma w jobie `test`, i `hotkeys`/`instance`/`winshell` z `wintypes`, co naprawia dopiero M1b).
  3. `tests/test_platforms.py` według p. 3.2: import `base`, `null`, `__init__` (i `macos/*`, gdy powstaną) na każdym systemie, `windows/*` tylko na Windows; test AST zakazanych importów na poziomie modułu, w M1a z wyjątkiem plików `hotkeys.py`, `instance.py`, `winshell.py` (lista wyjątków w teście, M1b ją opróżnia); `family()` mapuje `win32/darwin/linux`; brakujący moduł daje obiekt z `null.py`.
  4. Z `override("null")`: `dictation.insert_text("abc", ...)` zwraca `CLIPBOARD_ONLY` albo `NO_WINDOW` (bez wyjątku); `dictation.beep(1046)` nie robi nic; z atrapą `sounds` `beep(1046)`, `beep(659)`, `beep(330, 120)` dają `start`, `stop`, `cancel`.
  4a. Ukryty test lidera: `ast.dump` przeniesionych funkcji i klas równy wersji z `944721f` (p. 4.0).
  5. Test kontraktu: każda implementacja `windows.*` i `null.*` ma wszystkie metody z odpowiedniego Protocol-u (`inspect` po nazwach i liczbie parametrów).
  6. Tylko macOS (lider, po M3b): ten sam zestaw testów dyktowania przechodzi na runnerze.

### M1b. Skróty, jedna instancja i powłoka (refaktor bez zmiany zachowania)

- **Szacunek:** ok. 800 linii. **Delegowalne:** tak. **Zależności:** M1a, M0a (`downloads.py` używa `diarize.worker_python`). **Fala:** 2.
- **Pliki:** nowe `ucho/platforms/windows/{hotkeys,instance,shell}.py`; `ucho/hotkeys.py` (zostają `parse`, `label`, stałe `MOD_*`, `_KEYS`; klasa `Hotkeys` deleguje do `platforms.hotkeys`), `ucho/instance.py` (zostaje `SingleInstance` jako cienka fasada i `COMMANDS`), `ucho/winshell.py` (reeksport z `windows/shell.py`, na czas migracji), `ucho/app.py` (l. 193-232, 611-616, 752-762, 827-895), `ucho/screens.py` (`open_path` l. 66-70, `iconbitmap` l. 1158-1161), `ucho/settings_screen.py` (l. 18, 300, 903-909: `sync_autostart`, etykieta autostartu), `ucho/diarize.py` (`worker_python` przez `shell().worker_command`), `ucho/downloads.py` (polecenie procesu roboczego przez `shell().worker_command`), testy `tests/test_platform_shell.py`.
- **SPEC:**
  - `windows/hotkeys.py`: dzisiejsza pętla `RegisterHotKey`/`GetMessageW`, `escape`, `stop` bez zmian; `start` zwraca słownik błędów jak dziś.
  - `windows/instance.py`: mutex i zdarzenia jak dziś; `ucho.instance.SingleInstance` to fasada z tym samym publicznym API (`tests/test_instance.py` bez zmian).
  - `windows/shell.py`: logika `winshell.py` plus z `app.py` funkcje `_pythonw`, `start_command`, `_stamp_shortcuts`, `_work_area`, `_show_without_focus`, `_set_app_id`, `window_handle`, `describe_window`, Jump List i `TaskbarProgress` za `Shell.progress`. `open_path` = `os.startfile`. `autostart_label()` = „Start with Windows”. Klucz ustawień `start_with_windows` zostaje.
  - `app.py` woła wyłącznie `platforms.shell()`; nie ma już `ctypes.windll` ani `import winshell`. Żaden moduł w `ucho/` nie importuje `winshell` (zostaje tylko dla zgodności zewnętrznych skryptów), a `hotkeys.py` i `instance.py` nie importują `ctypes.wintypes` na poziomie modułu: test AST z M1a działa już bez listy wyjątków.
- **Kryteria akceptacji:**
  1. Obecny zestaw testów zielony na Windows bez zmian w testach.
  2. `grep -n "windll\|WinDLL\|winreg\|winsound\|wintypes" ucho/*.py` zwraca tylko `ucho/winshell.py` (reeksport), a test AST z p. 3.2 przechodzi bez wyjątków.
  2a. W podprocesie z `sys.platform = "darwin"` ustawionym przed importem: `import ucho.hotkeys, ucho.instance, ucho.screens` przechodzi (pomijany, gdy brak `customtkinter`).
  3. Z `override("null")`: `App` z `--smoke-test` nie woła żadnej metody Windows (atrapa `shell` liczy wywołania; test jednostkowy na `app._after_start` z fałszywym oknem).
  4. `hotkeys.parse`, `hotkeys.label` dają te same wyniki co przed zmianą dla wszystkich kombinacji z `tests/test_hotkeys.py` i dla `config.DEFAULTS`.
  5. `diarize.worker_python()` zwraca tę samą ścieżkę co dziś (test z `tests/test_diarize.py` l. 410-422 zielony).
  6. Ukryty test lidera: `ast.dump` przeniesionych funkcji równy wersji z `944721f` (p. 4.0).
  7. Lider: `--smoke-test 3` na Windows, ręcznie: skróty, druga instancja („show”), autostart włącz/wyłącz, Jump List; pełna lista dla Fryderyka w p. 7c.

### M1c. Audio, informacje o sprzęcie, czcionki i ikony (refaktor bez zmiany zachowania)

- **Szacunek:** ok. 600 linii. **Delegowalne:** tak. **Zależności:** M1a. **Fala:** 2 (równolegle z M1b).
- **Pliki:** nowe `ucho/platforms/windows/{audio,sysinfo,fonts}.py`; `ucho/audio.py` (`_input_api`, `microphones`, `_device_map`, `list_devices`, `_Track.start`/`_callback`, `Recorder.start` l. 503-562), `ucho/dictation.py` (tylko klasa `Microphone`, l. 694-893), `ucho/hardware.py` (`_ram`, `_cpu_name`, `_physical_cores`, `_cpu_feature`, `_adapters`, blok w `profile()`), `ucho/i18n.py` (`system_language`), `ucho/theme.py` (l. 163-173, 296-305), `ucho/ui.py` (l. 29-62 ikony, `_ui_font` l. 538-546, `scrollable` l. 206).
- **SPEC:**
  - `windows/audio.py`: `list_devices()` z dzisiejszą logiką WASAPI i loopback, `open_input()` przez PyAudioWPatch (`paInt16`, callback zwraca `paContinue`), blokada `PORTAUDIO_LOCK` bez zmian. W `ucho/audio.py` zostają funkcje `new_pyaudio`, `close_pyaudio`, `microphones(p)`, `list_devices`, `find_device`, `menu_choices`, `migrate_device_choices`, `DeviceInfo`, `Recorder`, `_Track` (testy je podmieniają); `list_devices` deleguje do `platforms.audio()`, a gdy rodzina to `windows`, dalej używa `new_pyaudio` (atrapa z testów działa).
  - `_Track` i `Microphone` otwierają strumień przez `AudioBackend.open_input`, dostają `bytes` int16; zapis, resampling, dryf i watchdog bez zmian.
  - `Recorder.start` przy braku źródła systemowego zgłasza błąd z tekstem zależnym od platformy (`"No loopback device found"` na Windows bez zmian).
  - `hardware.profile()` bierze dane z `platforms.sysinfo()`; reguły `recommend`/`recommend_llm` bez zmian. `i18n.system_language` = `sysinfo().ui_language()` z dotychczasowym fallbackiem `locale`. `theme` i `ui` biorą czcionki i glify z `platforms.fonts()`; Windows zwraca dokładnie dzisiejsze krotki i plik `SegoeIcons.ttf`/`segmdl2.ttf`.
- **Kryteria akceptacji:**
  1. Obecny zestaw testów zielony na Windows bez zmian w testach (w tym `test_audio_devices.py` z atrapą `pyaudiowpatch`, `test_hardware.py`, `test_theme.py`, `test_i18n.py`).
  2. `import ucho.audio` z `override("null")` i bez modułu `pyaudiowpatch` w `sys.modules` przechodzi; `audio.list_devices()` zwraca puste listy i `None`.
  3. `windows.fonts.create().sans == theme.SANS` sprzed zmiany (stałe porównane w teście).
  4. `_Track` z atrapą `AudioBackend` (strumień wywołujący `on_data` blokami 48 kHz stereo int16) zapisuje FLAC 16 kHz mono o długości zgodnej z liczbą próbek ±1 blok.
  5. Ukryty test lidera: `ast.dump` przeniesionych funkcji równy wersji z `944721f`; zmienione mogą być tylko `_Track.start`/`_callback` i `Microphone.start`/`_callback` (otwarcie strumienia), co lider czyta na diffie.
  6. Lider: `--smoke-test 3` i krótkie nagranie testowe na Windows (bez prawdziwej rozmowy: dźwięk z syntezatora mowy), obie ścieżki niepuste.

### M2. Ścieżki danych poza katalogiem kodu

- **Szacunek:** ok. 300 linii. **Delegowalne:** tak. **Zależności:** brak. **Fala:** 1.
- **Pliki:** nowy `ucho/paths.py`, `ucho/config.py` (l. 20-55, `onedrive_folder` l. 585-592), `ucho/dictate.py` (l. 25-26), `ucho/__main__.py` (l. 39), `ucho/app.py` (konfiguracja logowania, ok. l. 924), `ucho_mcp/server.py` (tylko jeśli używa `PROJECT_DIR`), `tests/test_paths.py`.
- **SPEC:**
  - `paths.app_dir()`: katalog z kodem i `design/tokens.json` (`sys._MEIPASS`, gdy `sys.frozen`, inaczej katalog nad `ucho/`).
  - `paths.data_dir(system=sys.platform, home=None, env=os.environ)`: `win32` = `app_dir()` (dziś `PROJECT_DIR`, bez zmian); `darwin` = `~/Library/Application Support/Ucho`; inne = `$XDG_DATA_HOME/ucho` lub `~/.local/share/ucho`. Zmienna `UCHO_DATA_DIR` nadpisuje (CI, testy). Katalog tworzony przy pierwszym zapisie, nie przy imporcie.
  - `paths.log_file()`: Windows `data_dir()/ucho.log`; macOS `~/Library/Logs/Ucho/ucho.log`.
  - `paths.models_dir()` = `data_dir()/models` z dzisiejszą migracją `modele` → `models` (tylko Windows ma stare foldery). `config.MODELS_DIR`, `SETTINGS_FILE`, `LOG_FILE`, `OV_CACHE_DIR` liczone z `paths`; `HF_HOME` dalej ustawiany w `config.py` przed importem `huggingface_hub`.
  - `config.PROJECT_DIR` zostaje jako alias `app_dir()` dla zgodności (`diarize.py` l. 268 używa go jako `cwd`).
  - `paths.cloud_roots(system, home, env)`: Windows jak dziś (`OneDrive*`); macOS `~/Library/CloudStorage/*`, `~/Library/Mobile Documents`, `~/Documents`, `~/Desktop`, `~/Dropbox`. `config.onedrive_folder(path)` (nazwa bez zmian) zwraca prawdę dla ścieżek pod którymkolwiek z nich; komunikat w UI mówi o „folderze synchronizowanym z chmurą”.
- **Kryteria akceptacji:**
  1. Na Windows wszystkie stałe `config.*` (ścieżki) mają te same wartości co przed zmianą (test porównuje z wartościami liczonymi „po staremu”).
  2. `data_dir(system="darwin", home=Path("/Users/x"), env={})` = `/Users/x/Library/Application Support/Ucho`; z `UCHO_DATA_DIR` = ta ścieżka; `log_file` dla `darwin` = `/Users/x/Library/Logs/Ucho/ucho.log` (`PurePosixPath` w teście, działa na Windows).
  3. `onedrive_folder` na danych z `tests/test_config.py` daje te same wyniki; dla `darwin`: `~/Library/Mobile Documents/com~apple~CloudDocs/x` i `~/Documents/x` dają prawdę, `~/Ucho/recordings` fałsz.
  4. Import `ucho.config` nie tworzy żadnego katalogu (test na `tmp_path` z `UCHO_DATA_DIR`).
  5. `paths.app_dir()` przy atrapie `sys.frozen=True`, `sys._MEIPASS=X` zwraca `X`.

### M3a. Spike CI macOS (zależności i ryzyka)

- **Szacunek:** ok. 150 linii YAML i skryptów. **Delegowalne:** nie (iteracje na runnerze). **Zależności:** brak. **Fala:** 1.
- **Pliki:** nowy `.github/workflows/macos.yml` (job `spike`, `workflow_dispatch`, `continue-on-error`), `tools/macos_spike.py`.
- **Cel:** odpowiedzi tak/nie, zanim ruszą zadania macOS: (1) instalacja kół na `macos-15` i `macos-26` z Pythonem 3.12: `openvino`, `openvino-genai`, `openvino-tokenizers`, `pywhispercpp`, `sounddevice`, `catap`, `pyobjc-framework-{Cocoa,Quartz,ApplicationServices,AVFoundation,CoreAudio}`, `sherpa-onnx`, `customtkinter`, `pystray`; (2) `tkinter.TkVersion` i okno Tk w sesji GUI runnera; (3) `pystray` `run_detached()` obok `mainloop` Tk przez 10 s bez crasha; (4) `RegisterEventHotKey` przez ctypes zwraca `noErr`; (5) `say` z głosem polskim (`say -v '?' | grep pl_PL`) i angielskim do WAV 16 kHz; (6) `pywhispercpp` z `ggml-base` transkrybuje WAV z `say`; (7) czy tap catap na runnerze zwraca dane czy ciszę (eksperyment z BlackHole i wpisem TCC, tylko informacyjnie).
- **Kryteria:** raport w podsumowaniu joba (`$GITHUB_STEP_SUMMARY`) z wynikiem każdego punktu; decyzje zapisane w tym pliku (sekcja 8). Brak wpływu na `ci.yml`.

### M3b. CI macOS: zależności per platforma, testy i smoke test

- **Szacunek:** ok. 300 linii. **Delegowalne:** nie. **Zależności:** M1a, M1b (import `hotkeys`/`instance`/`screens` na macOS), M1c, M2, M3a. **Fala:** 3.
- **Pliki:** `requirements.txt` (markery `; sys_platform == "win32"` dla `PyAudioWPatch`, linie `; sys_platform == "darwin"` dla `sounddevice`, `catap`, PyObjC, `pywhispercpp`), `requirements-parakeet.txt` (bez zmian, jeśli koła działają), `.github/workflows/macos.yml` (joby `macos-tests` na `macos-15` i `macos-26`, `macos-smoke` na `macos-15`), `tests/conftest.py` (wypisanie liczby pominiętych testów z powodem), `tests/test_package.py` (sprawdzenie markerów).
- **SPEC:** job `macos-tests` instaluje te same pakiety co Windowsowy job `test` (pytest, ruff, numpy, soundfile) i uruchamia `python -m pytest -q -rs`; job `macos-smoke` instaluje `requirements.txt` i uruchamia `python -m ucho --smoke-test 3` z `UCHO_DATA_DIR=$RUNNER_TEMP/ucho`. `install.ps1` musi dalej instalować to samo na Windows (pip respektuje markery).
- **Kryteria:** (1) oba joby zielone; (2) na macOS pominięte są tylko testy oznaczone jako „Windows only” z jawnym powodem, lista w podsumowaniu joba; (3) `ci.yml` nietknięty i zielony; (4) `tests/test_package.py` sprawdza, że linie `requirements.txt` bez markera albo z `sys_platform == "win32"` dają dokładnie dzisiejszy zestaw (lista wpisana w test), a linie macOS mają marker `sys_platform == "darwin"`; (5) job `macos-smoke` przechodzi także przed M4 i M5, bo wersje null mają semantykę z p. 3.2 (jeśli brak M4/M5 daje `ERROR` w logu, job ma `continue-on-error` do czasu M5, zapisane w YAML z komentarzem). Porównanie `pip freeze` na Windows w osobnym jobie nie jest potrzebne, a dopisanie go do `ci.yml` łamałoby kryterium 3.

### M4. macOS bez API Apple: informacje o systemie, powłoka, dźwięki, czcionki i ikony

- **Szacunek:** ok. 600 linii. **Delegowalne:** tak (wszystko przez `subprocess` i pliki, z atrapą wykonawcy poleceń). **Zależności:** M1b, M1c, M2 (`paths.app_dir`), M8 (identyfikatory modeli whisper.cpp w regułach). **Fala:** 3.
- **Pliki:** nowe `ucho/platforms/macos/{sysinfo,shell,sounds,fonts}.py`, `design/icons/FluentSystemIcons-Ucho.ttf` (podzbiór 19 glifów z `ui.GLYPHS`), `tools/subset_icons.py` (narzędzie dewelopera, `fonttools` tylko w `.venv-dev`), `ucho/hardware.py` (reguły dla Apple Silicon), `THIRD_PARTY.md`, testy `tests/test_macos_pure.py`.
- **SPEC:**
  - `sysinfo`: `ram_gb` z `sysctl -n hw.memsize` i `vm_stat` (dostępna = wolne + nieaktywne strony), `cpu_name` z `machdep.cpu.brand_string`, `physical_cores` z `hw.physicalcpu`, `cpu_features` z `hw.optional.arm64`, `adapters` = jedna pozycja „Apple GPU (Metal)”, `ui_language` z `defaults read -g AppleLanguages` (pierwszy element, np. `pl-PL` daje `pl`), `reduce_motion` z `defaults read com.apple.universalaccess reduceMotion`. Każde polecenie przez wstrzykiwalny `run(args) -> str`, timeout 2 s, błąd daje wartość „nieznana”, nigdy wyjątek.
  - `hardware.recommend`/`recommend_llm` dla `darwin`: model mowy whisper.cpp large-v3 (z M8) przy ≥ 16 GB, turbo przy < 16 GB; LLM: Qwen2.5-1.5B przy ≤ 8 GB, Qwen3-4B przy 8 < RAM ≤ 16 GB, Qwen3-8B przy > 16 GB (progi rozłączne). Urządzenie `CPU` dla OpenVINO, `METAL` dla whisper.cpp.
  - `shell`: `open_path` = `open <ścieżka>`; `start_command` = `["/usr/bin/open", "-a", <ścieżka .app>]` (gdy `sys.frozen`), inaczej `[sys.executable, "-m", "ucho"]`; `worker_command` w paczce = `[sys.executable, "--worker", module, *args]` (obsługę flagi dodaje M10), ze źródeł `[sys.executable, "-m", module, "--worker", *args]`; `autostart_set(True)` zapisuje plist `~/Library/LaunchAgents/app.ucho.plist` (`Label` = `app.ucho`, `ProgramArguments` z `start_command() + ["--args", "--minimized"]`, `RunAtLoad` = true), `False` usuwa plik; `autostart_get` zwraca polecenie z pliku albo `None`; `autostart_label()` = „Open at login”; reszta metod to no-op zwracające `None`/`False`; `set_window_icon` przez `iconphoto` z PNG rysowanego `ucho.icon.draw_mark`.
  - `sounds.play`: `NSSound.soundNamed_` przez PyObjC, a gdy go brak, `afplay /System/Library/Sounds/<nazwa>.aiff` w tle (`Tink` start, `Pop` stop, `Basso` anulowanie); błąd jest logowany i ignorowany.
  - `fonts`: `sans = (".AppleSystemUIFont", "SF Pro Text", "Helvetica Neue")`, `display = ("New York", "Georgia")`, `mono = ("SF Mono", "Menlo")`, `numeric = ("SF Pro Display", ".AppleSystemUIFont")`; `icon_font()` = `app_dir()/design/icons/FluentSystemIcons-Ucho.ttf`; `glyphs()` mapuje te same 19 nazw co `ui.GLYPHS`; `ui_font_file` = `/System/Library/Fonts/SFNS.ttf` lub `Helvetica.ttc`.
- **Kryteria akceptacji (Windows, z `override("macos")` i atrapą `run`):**
  1. `sysinfo` z zapisanymi wyjściami `sysctl` (M1 8 GB, M3 Pro 18 GB) zwraca poprawne liczby; wyjątek lub timeout w `run` daje wartości „nieznane” bez wyjątku.
  2. `recommend` dla profilu `darwin` z 8, 16 i 24 GB zwraca modele z tabeli powyżej.
  3. plist z `autostart_set(True)` (w `tmp_path` jako `home`) parsuje się `plistlib.loads` i ma klucze `Label`, `ProgramArguments`, `RunAtLoad`; `autostart_get` po zapisie zwraca polecenie, po `autostart_set(False)` `None`.
  4. Czcionka ikon zawiera glif dla każdej nazwy z `ui.GLYPHS` (sprawdzenie przez Pillow `getmask` niepusty) i waży < 100 KB.
  5. `sounds.play` z brakiem PyObjC i atrapą `subprocess.Popen` wywołuje `afplay` z właściwym plikiem.
  6. Tylko macOS (lider): `plutil -lint` na wygenerowanym pliku; `sysinfo` na runnerze zwraca RAM > 1 GB i nazwę procesora z „Apple”.

### M5. macOS: jedna instancja i skróty globalne

- **Szacunek:** ok. 500 linii. **Delegowalne:** nie (Carbon, `fcntl`, gniazda Unix tylko na runnerze). **Zależności:** M1b, M2, M3b. **Fala:** 4.
- **Pliki:** nowe `ucho/platforms/macos/{instance,hotkeys}.py`, `ucho/hotkeys.py` (`label` z symbolami ⌃⌥⇧⌘ na macOS), `ucho/config.py` (domyślne skróty per platforma, tylko jeśli M3a lub testy wykażą kolizję), `ucho/app.py` (obsługa `::tk::mac::ReopenApplication` → `show`), testy `tests/test_macos_instance.py`, `tests/test_macos_hotkeys.py`.
- **SPEC:**
  - `instance`: `acquire` = `fcntl.flock(LOCK_EX | LOCK_NB)` na `data_dir()/ucho.lock`; `listen` = wątek z serwerem `AF_UNIX` na `data_dir()/ucho.sock` (gdy ścieżka > 100 znaków, `$TMPDIR/ucho-<uid>.sock`), komenda to jedna linia ASCII z `COMMANDS`; `signal` łączy się, wysyła i zwraca `True`, gdy serwer potwierdził; stare gniazdo bez właściciela jest usuwane; `close` zamyka i usuwa.
  - `hotkeys`: `RegisterEventHotKey` przez ctypes z `Carbon.framework`; mapa `MOD_*` (Windows) na `cmdKey/optionKey/controlKey/shiftKey` i kodów VK z `hotkeys._KEYS` na `kVK_*`; rejestracja i wyrejestrowanie na wątku Tk (`app.after`); `escape(True)` rejestruje Esc, `escape(False)` go zwalnia; rejestracja z opcją `kEventHotKeyExclusive`, więc zajęty skrót (`eventHotKeyExistsErr`, także gdy trzyma go inny proces) trafia do słownika błędów z tekstem „Taken by another program”. Skrót kolidujący z systemowym (⌘Space, ⌃Space) jest raportowany tym samym tekstem, jeśli Carbon go odrzuci; inaczej lista znanych kolizji w `hotkeys.label` daje ostrzeżenie w Ustawieniach.
- **Kryteria (tylko macOS, job `macos-tests`):** (1) drugi `acquire` w podprocesie zwraca `False`; (2) `signal("show")` z podprocesu wywołuje handler w procesie głównym w < 1 s; (3) martwe gniazdo po `kill -9` nie blokuje nowego `acquire`; (4) `start({"record": "<ctrl>+<alt>+r"})` zwraca pusty słownik, a rejestracja tego samego skrótu przez drugi proces zwraca błąd (zależne od wyniku M3a p. 4; gdy Carbon nie zgłasza kolizji między procesami, kryterium zmienia się na „ten sam proces”, a w p. 8 trafia znane ograniczenie); (5) eksperymentalnie: syntetyczne zdarzenie klawiatury przez `CGEventPost` wywołuje akcję (`continue-on-error`). Na Windows: testy mapowania VK→kVK i etykiet ⌃⌥R (czyste funkcje) w zwykłym zestawie.

### M6. macOS: schowek, wklejanie i fokus

- **Szacunek:** ok. 550 linii. **Delegowalne:** nie. **Zależności:** M1a, M3b. **Fala:** 4.
- **Pliki:** nowe `ucho/platforms/macos/{clipboard,inserter}.py`, `ucho/screens.py` (małe okno dyktowania: `::tk::unsupported::MacWindowStyle style <okno> utility {noActivates}`), testy `tests/test_macos_clipboard.py`.
- **SPEC:**
  - `clipboard`: `NSPasteboard.generalPasteboard`; `set_text(private=True)` dodaje typy `org.nspasteboard.ConcealedType` i `org.nspasteboard.TransientType` (odpowiednik wykluczenia z historii Win+V); `sequence` = `changeCount`; `offer_delayed` zwraca obiekt, który od razu ustawia tekst, `pump` czeka `PASTE_SETTLE_S` (0,4 s) i ustawia `rendered=True`, `restore(previous)` przywraca poprzedni tekst tylko, gdy `changeCount` się nie zmienił od naszego zapisu, `lost` = ktoś inny zmienił schowek.
  - `inserter`: `foreground` = pid z `NSWorkspace.frontmostApplication()`; `is_ours` = pid == `os.getpid()`; `is_shell` = bundle id `com.apple.finder` bez okien albo `com.apple.dock`; `activate` = `NSRunningApplication.activateWithOptions_`; `paste` = `CGEventCreateKeyboardEvent` dla `kVK_ANSI_V` z `kCGEventFlagMaskCommand` i `CGEventPost(kCGHIDEventTap)`; `type_text` przez `CGEventKeyboardSetUnicodeString` porcjami po 20 znaków; `wait_keys_released` przez `CGEventSourceKeyState`; `permission` = `AXIsProcessTrusted()`; `request_permission` = `AXIsProcessTrustedWithOptions({prompt: True})`. Bez Dostępności `insert_text` zostawia tekst w schowku i pokazuje „Text is on the clipboard, press ⌘V.” plus przycisk „Open Accessibility settings” (`open x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility`).
- **SPEC (uzupełnienie):** bezpieczne wprowadzanie (pole hasła, niektóre terminale z „Secure Keyboard Entry”): `CGEventPost` jest wtedy ignorowany; `IsSecureEventInputEnabled()` (Carbon, ctypes) sprawdzane przed wklejeniem daje tekst w schowku i komunikat „Text is on the clipboard, press ⌘V.”.
- **Kryteria (tylko macOS):** (1) `set_text`/`get_text`/`sequence` w sesji GUI runnera; (2) `restore` po `offer_delayed` przywraca poprzednią treść, a gdy w międzyczasie inny proces (podproces `pbcopy`) zmienił schowek, nie nadpisuje go; (3) `foreground()` zwraca pid procesu z pierwszego planu, `is_ours` działa dla własnego pid (uwaga: `NSWorkspace.frontmostApplication` odświeża się tylko przy działającej pętli zdarzeń; test pompuje `NSRunLoop` albo używa `CGWindowListCopyWindowInfo`, inaczej wynik bywa nieaktualny); (4) eksperymentalnie (`continue-on-error`): `paste()` do okna TextEdit otwartego przez `open -a TextEdit` i odczyt treści przez AppleScript. Na Windows: testy orkiestracji `insert_text` z atrapami (już istnieją) muszą przejść także z `override("macos")` i atrapami prymitywów.

### M7a. macOS: mikrofon przez sounddevice

- **Szacunek:** ok. 400 linii. **Delegowalne:** tak (atrapa modułu `sounddevice` w `sys.modules`). **Zależności:** M1c. **Fala:** 3.
- **Pliki:** nowy `ucho/platforms/macos/audio.py` (`AudioBackend` bez źródeł systemowych; te dodaje M7b), testy `tests/test_macos_audio.py`.
- **SPEC:** `list_devices()` z `sounddevice.query_devices()` i `sounddevice.default.device`: mikrofony = urządzenia z `max_input_channels > 0`, bez urządzeń agregujących Ucho (nazwa zaczyna się od „Ucho”); `system` = pusta lista, dopóki M7b nie doda tapu; indeksy i nazwy jak w `DeviceInfo`. `open_input()` = `sounddevice.RawInputStream(dtype="int16", channels, samplerate, blocksize=frames, device=index, callback)`; callback przekazuje `bytes(indata)`; `status.input_overflow` jest logowany raz na 10 s. `permission("microphone")` przez `AVCaptureDevice.authorizationStatusForMediaType_` (PyObjC, import leniwy; brak PyObjC daje `"unknown"`). Brak urządzeń nie jest błędem (runner).
- **Kryteria (Windows, atrapa `sounddevice`):** (1) lista urządzeń z atrapy (2 mikrofony, 1 wyjście, 1 agregat Ucho) daje 2 mikrofony i właściwy domyślny; (2) `open_input` przekazuje do `on_data` bajty int16 z atrapy strumienia; (3) `query_devices` rzucające `PortAudioError` daje puste listy bez wyjątku; (4) `_Track` z tym backendem i atrapą zapisuje poprawny FLAC (jak M1c p. 4). Tylko macOS: `list_devices()` na runnerze nie rzuca (zero albo więcej urządzeń).

### M7b. macOS: dźwięk systemowy przez Core Audio Process Tap i uprawnienia audio

- **Szacunek:** ok. 500 linii. **Delegowalne:** nie (prototyp i test na runnerze, potem ręcznie na Macu). **Zależności:** M7a, M3a (wynik punktu 7). **Fala:** 4.
- **Pliki:** nowy `ucho/platforms/macos/audio_tap.py`, `ucho/platforms/macos/audio.py` (źródło „System audio” w `list_devices()["system"]`), nowy `ucho/platforms/macos/permissions.py` (implementacja interfejsu `Permissions` z M1a, z którego korzysta M9), `ucho/audio.py` (wykrywanie „cyfrowej ciszy” na ścieżce `meeting`), testy `tests/test_macos_tap.py`, `tests/test_silence_watch.py`.
- **SPEC:** jedno źródło systemowe „System audio (all apps)” (`DeviceInfo(index=-1, loopback=True)`); `open_input` dla niego tworzy tap catap z wykluczeniem własnego procesu, konwertuje float32 48 kHz stereo na int16 w przeplocie i oddaje przez `on_data`; po `capture_failed` (uśpienie, zmiana formatu) `Recorder._watchdog_loop` odtwarza strumień jak dziś. `audio.SilenceWatch` (czysta klasa): gdy ścieżka `meeting` ma przez 20 s same zera (nie „cicho”, tylko dokładnie 0), emituje ostrzeżenie „System audio is silent. Ucho may not have permission to record it.” z przyciskiem do ustawień prywatności. `permissions.state() -> dict[str, Permission]` dla `microphone`, `system_audio`, `accessibility`; `system_audio` po próbnym tapie 1 s: dane niezerowe = `granted`, same zera = `unknown`; próba tylko na żądanie (przycisk „Check” albo start kroku kreatora), wynik buforowany, nigdy w pętli odświeżania co 1 s.
- **Kryteria:** Windows (czyste funkcje): konwersja float32 → int16 z obcinaniem do zakresu; `SilenceWatch` na sztucznych blokach (zera 21 s → jedno ostrzeżenie; szum → brak). Tylko macOS: import catap i obecność `AudioHardwareCreateProcessTap`; eksperymentalnie (BlackHole + `say`) tap zwraca dane niezerowe. Ręcznie (Fryderyk): pkt 7b.

### M8. Backend whisper.cpp

- **Szacunek:** ok. 550 linii. **Delegowalne:** tak (atrapa `pywhispercpp.model.Model`). **Zależności:** M0a (`downloads.py` musi rozumieć nowy format klucza modelu). **Fala:** 2.
- **Pliki:** nowy `ucho/whispercpp.py`, `ucho/transcribe.py` (`create_engine`, `offered_models`, `available_devices`), `ucho/config.py` (`MODELS` z `"backend": "whispercpp"` i `"platforms": ["darwin"]`, `DOWNLOAD_PATTERNS` z jednym plikiem `.bin`; `REVISIONS` uzupełnia lider po sprawdzeniu repo), `ucho/downloads.py` (klucz `repo:plik` w `bytes_on_disk` i procesie roboczym), `ucho/settings_screen.py` (tylko `short_model`, l. 36-37: czytelna nazwa dla klucza `repo:plik`), `tools/benchmark_wer.py` (opcja `--backend whispercpp`), `tools/benchmark_engine.py` (import `ctypes.wintypes` przeniesiony do funkcji, bo p. 7b-13 uruchamia go na Macu), testy `tests/test_whispercpp.py`. `THIRD_PARTY.md` dopisuje M13 (unika konfliktu z M4).
- **SPEC:**
  - `whispercpp.Engine(repo_id, device)` z tą samą powierzchnią co `parakeet.Engine`: `load()`, `release()`, `generate(audio: np.ndarray, language: str | None, prompt: str | None) -> Result`, `device`, `requested_device`. `load` importuje `pywhispercpp.model` leniwie i otwiera plik `ggml-*.bin` z `transcribe.model_path(repo)`. Do `Model(...)` trafia wyłącznie bezwzględna ścieżka istniejącego pliku; gdy plik nie istnieje, `load` rzuca wyjątek **przed** utworzeniem `Model` (inaczej `pywhispercpp.utils.resolve_model_path` pobrałby model sam, przez `requests`, bez przypiętej rewizji). Logi whisper.cpp przekierowane do `logging` (`redirect_whispercpp_logs_to`).
  - Język: gdy `language` jest `None` (Auto), `auto_detect_language(audio)` zwraca krotkę `((lang, prob), {lang: prob})` (API 1.5.1) i bierzemy słownik; wybór = argmax w obrębie `config.LANGUAGES` (zawężenie jak `lang_to_id` w `transcribe.Engine._narrowed_languages`); potem transkrypcja z wymuszonym językiem. `Result.languages` = lista wybranych języków w formacie oczekiwanym przez `transcribe._detected_language`.
  - Prompt: `initial_prompt`. Znaczniki czasu z segmentów (`t0`, `t1` w jednostkach 10 ms) na sekundy, wynik w kształcie OpenVINO (`texts`, `chunks` z `start_ts`/`end_ts`, `text`), z ponownym użyciem `parakeet.Chunk`/`Result`.
  - `device`: `"METAL"`, gdy biblioteka zgłosi Metal, inaczej `"CPU"` (log `info` przy fallbacku, np. na runnerze).
  - `offered_models()` pomija modele z `platforms`, które nie zawierają `sys.platform`; na Windows lista jest identyczna jak dziś.
  - Wpisy modeli: `ggerganov/whisper.cpp` z plikami `ggml-large-v3-q5_0.bin` (1,08 GB; domyślny na Macu po pomiarze WER) i `ggml-large-v3-turbo-q5_0.bin` (0,57 GB) (pliki i rozmiary sprawdzone w API Hugging Face 2026-11-19, `sha` repo `5359861c739e955e79d9a303bcbc70fb988958b1` jako kandydat do `REVISIONS`); oraz modele tylko do CI `ggml-tiny-q5_1.bin` (32 MB) i `ggml-base-q5_1.bin` (60 MB) z flagą `"hidden": True` (nie ma ich w `offered_models()`, używa ich `macos-asr` i 7a). Klucz modelu w `config.MODELS` rozróżnia pliki (`ggerganov/whisper.cpp:ggml-large-v3-q5_0.bin`); jedna funkcja `config.split_model_id(key) -> (repo, filename | None)` jest jedynym miejscem parsowania, a używają jej `transcribe.model_path` (z `allow_patterns=[filename]`), `downloads` i `REVISIONS` (klucz po repo). Wszystkie modele whisper.cpp dzielą jeden katalog repo w `hub/`, więc `bytes_on_disk` dla nich liczy tylko blob danego pliku (rozmiar z `{"total"}` procesu roboczego, postęp z `*.incomplete`).
- **Kryteria akceptacji:**
  1. Z atrapą `pywhispercpp` (`auto_detect_language` zwraca `(("ko", 0.5), {"ko": 0.5, "pl": 0.4, "en": 0.1})`) i `config.LANGUAGES = ["pl", "en"]` silnik wybiera `pl` i woła transkrypcję z `language="pl"`.
  2. Segmenty atrapy `t0=150, t1=420` dają chunk 1,5 → 4,2 s; `texts` łączy teksty; puste audio daje pusty wynik bez wywołania modelu.
  3. `transcribe.create_engine("ggerganov/whisper.cpp:ggml-large-v3-q5_0.bin")` zwraca `whispercpp.Engine`; dla modeli OpenVINO dalej `transcribe.Engine`.
  4. `offered_models()` na `win32` równa się liście sprzed zmiany; na `darwin` (atrapa `sys.platform`) zawiera modele whisper.cpp.
  5. `iter_transcribe_session` z atrapą silnika przechodzi cały przebieg (te same testy co dla Parakeet, sparametryzowane).
  5a. `load` dla nieistniejącej ścieżki rzuca wyjątek i nie tworzy `Model` ani nie woła `pywhispercpp.utils.download_model` (atrapy liczą wywołania); `split_model_id` dla kluczy OpenVINO zwraca `(repo, None)` i nie zmienia dzisiejszych ścieżek.
  6. Lider, tylko macOS: job `macos-asr` transkrybuje WAV z `say` modelem `ggml-base` i zawiera oczekiwane słowa; `tools/benchmark_wer.py` na zestawie TTS z `docs/benchmarks.md` porównuje OpenVINO CPU int8 z whisper.cpp q5_0 (wynik do `docs/benchmarks.md`, warunek ustawienia domyślnego modelu na Macu).

### M9. Kreator uprawnień i teksty zależne od platformy

- **Szacunek:** ok. 500 linii. **Delegowalne:** tak (atrapa `permissions.state`, UI sprawdzane smoke testem). **Zależności:** M1b, M4 (etykiety), M0b (te same pliki UI, wcześniejsza fala), interfejs `Permissions` z M1a (accessor `platforms.permissions()`; M9 nie tworzy żadnego pliku w `platforms/`, więc nie konfliktuje z M7b). **Fala:** 4.
- **Pliki:** `ucho/wizard.py` (nowy krok „Permissions” tylko na macOS), `ucho/settings_screen.py` (sekcja „Permissions” na macOS, etykieta autostartu z `shell().autostart_label()`, „Same as Windows” → „Same as the system”), `ucho/screens.py` (komunikaty z `paste_label()`), `ucho/locale/pl.json`, `ucho/i18n.py` nie zmienia się, testy `tests/test_permissions_ui.py`.
- **SPEC:** krok pokazuje trzy wiersze: Mikrofon, Dźwięk systemowy, Dostępność; każdy ze stanem (`granted` zielony, inne z przyciskiem „Allow” wołającym `permissions().request(kind)` albo „Open settings” z adresem z `permissions().settings_url(kind)`). Krok można pominąć („Later”), a stan widać potem w Ustawieniach. Odświeżanie stanu co 1 s, gdy krok jest widoczny. Na Windows krok nie istnieje, liczba kroków kreatora bez zmian. Teksty z „Windows” w interfejsie (`grep -n "Windows" ucho/locale/pl.json`) są przeredagowane na neutralne albo wybierane według platformy.
- **Kryteria:** (1) na Windows `--smoke-test 3` przechodzi, a kreator ma tyle kroków co dziś; (2) z `override("macos")` i atrapą `permissions.state` kreator ma krok uprawnień; `granted` dla wszystkich ukrywa przyciski; (3) przycisk „Open settings” dla Dostępności wywołuje `open` z adresem zawierającym `Privacy_Accessibility` (atrapa `shell.open_path`); (4) żaden widoczny tekst na Windows nie zmienił się poza świadomie wymienionymi (test porównuje listę kluczy `t()` używanych w kreatorze na Windows przed i po); (5) `pl.json` kompletne.

### M10. Paczka `.app` i `.dmg`, publikacja w GitHub Release

- **Szacunek:** ok. 550 linii. **Delegowalne:** nie. **Zależności:** M0b, M2, M3b, M4, M5, M6, M7b, M8, M9 (do bety wszystkie; pierwszą paczkę testową można zbudować wcześniej z tym, co jest), decyzje D1 i D2 z p. 8. **Fala:** 5.
- **Pliki:** nowe `packaging/macos/ucho.spec` (PyInstaller), `packaging/macos/entitlements.plist` (pusty, na później), `tools/build_macos.py`, `ucho/__main__.py` (flagi `--mcp` i `--worker <module> ...` w zamrożonej paczce), `.github/workflows/macos.yml` (job `macos-package`), `.github/workflows/release.yml` (job `macos` z `needs: release`).
- **SPEC:**
  - PyInstaller onedir + `BUNDLE`, `target_arch="arm64"`, `name="Ucho"`, ikona `.icns` rysowana z `ucho.icon` (`iconutil`), dane: `design/tokens.json`, `design/icons/*.ttf`, `ucho/locale/*.json`; zebrane dynamiczne biblioteki i dane `openvino`, `openvino_genai`, `openvino_tokenizers` (w tym `plugins.xml`), `pywhispercpp` (także `libwhisper*.dylib`/`libggml*.dylib`, które koło kładzie w katalogu głównym `site-packages`, z `libggml-metal`), `sounddevice` (`_sounddevice_data`), `catap` (z `catap/native/libcatap_coreaudio.dylib`), `hf_xet` (natywne). Wszystkie te biblioteki są podpisane przez `codesign` (sprawdza kryterium 1).
  - Info.plist: `CFBundleIdentifier=app.ucho`, `CFBundleName=Ucho`, `CFBundleShortVersionString=config.VERSION`, `LSMinimumSystemVersion` według D1 (propozycja `15.0`), `NSMicrophoneUsageDescription`, `NSAudioCaptureUsageDescription` (po polsku i angielsku przez `InfoPlist.strings`), `LSApplicationCategoryType=public.app-category.productivity`, `NSHighResolutionCapable=true`. Bez `NSCameraUsageDescription`, bez kluczy nagrywania ekranu.
  - Podpis: `codesign --force --deep -s - dist/Ucho.app`, potem `codesign --verify --deep --strict`. Bez `--options runtime`.
  - DMG: katalog z `Ucho.app` i dowiązaniem `Applications`, `hdiutil create -volname Ucho -srcfolder <katalog> -format ULFO dist/Ucho-macOS-arm64.dmg` z 3 próbami co 10 s (na runnerach macOS `hdiutil create` bywa przerywany „Resource busy”); `SHA256SUMS-macos.txt`.
  - Pierwszy start paczki: zapis opt-out telemetrii OpenVINO (odpowiednik `install.ps1` l. 301-307, przez moduł `openvino_telemetry` w procesie, bez sieci) i flaga w `settings.json`, żeby nie powtarzać.
  - App Translocation: gdy ścieżka aplikacji zawiera `/AppTranslocation/` albo zaczyna się od `/Volumes/`, okno pokazuje komunikat „Move Ucho to the Applications folder and open it again.” i nie zapisuje autostartu (LaunchAgent wskazywałby na ścieżkę tymczasową).
  - `ucho/__main__.py`: w paczce `Ucho --mcp` uruchamia `ucho_mcp.server.main()`, a `Ucho --worker ucho.diarize ...`, `--worker ucho.downloads ...` i `--worker ucho.cli_transcribe ...` (M11) uruchamiają `main()` wskazanego modułu (tylko z listy dozwolonych modułów). Rozpoznanie `--mcp`/`--worker` następuje przed utworzeniem okna, blokadą jednej instancji i konfiguracją logowania do pliku aplikacji, a w trybie `--mcp` nic poza JSON-RPC nie trafia na stdout. Ze źródeł zachowanie bez zmian.
  - `release.yml`: dotychczasowy job (Windows) bez zmian kroków; nowy job `macos` (`runs-on: macos-15`, `needs: release`) buduje paczkę, uruchamia testy z p. 7a (1-3) i dodaje pliki `gh release upload "$TAG" dist/Ucho-macOS-arm64.dmg dist/SHA256SUMS-macos.txt`. Błąd joba macOS nie cofa wydania Windows, ale wtedy link `releases/latest/download/Ucho-macOS-arm64.dmg` ze strony daje 404; dlatego ostatni job `assets` (`needs: [release, macos]`, `if: always()`) sprawdza `gh release view "$TAG" --json assets`, że są wszystkie pięć plików, i pada głośno, a strona (M12) ma obok przycisku link zapasowy do `github.com/Fredpry/ucho/releases`.
- **Kryteria (tylko macOS, job `macos-package`):** (1) `codesign --verify --deep --strict` przechodzi; (2) `plutil -extract` zwraca wszystkie klucze Info.plist z listy i nie ma kluczy spoza listy uprawnień; (3) `Ucho.app/Contents/MacOS/Ucho --smoke-test 3` z `UCHO_DATA_DIR` w katalogu tymczasowym kończy się kodem 0 i nic nie zapisuje do bundla (suma kontrolna drzewa `.app` przed i po); (4) DMG: `hdiutil verify`, `hdiutil attach`, smoke test z zamontowanej kopii; (5) rozmiar DMG < 200 MB (próg do korekty po pierwszym buildzie, ale z twardym limitem w jobie); (6) informacyjnie (`continue-on-error`): `spctl -a -vv -t exec` daje „rejected” (podpis ad-hoc; wynik nie zależy od kwarantanny i nie zastępuje ręcznego p. 7b-2); (7) w `release.yml` po tagu testowym na forku obie paczki trafiają do jednego wydania.

### M11. Scenariusze testów użytkownika na runnerze (job `macos-e2e`)

- **Szacunek:** ok. 350 linii (skrypty w `tools/e2e_macos/`, mały moduł produktu `ucho/cli_transcribe.py` i job YAML). **Delegowalne:** nie. **Zależności:** M10. **Fala:** 5.
- **Zakres:** automatyzacja scenariuszy z p. 7a na zbudowanej paczce, raport w podsumowaniu joba i jako artefakt. Zielony job `macos-e2e` jest warunkiem M12. `ucho/cli_transcribe.py` (transkrypcja folderu sesji bez okna, `main(argv)`; wchodzi do paczki, bo `tools/` nie jest w bundlu) jest jedynym nowym modułem na liście `--worker`; jego czysta część ma test na Windows.

### M12. Strona ucho.app: przycisk „Pobierz na Maca (beta)” i instrukcja instalacji

- **Szacunek:** ok. 250 linii HTML/CSS. **Delegowalne:** tak (test: `python tools/check.py`). **Zależności:** wydanie z `Ucho-macOS-arm64.dmg` (M10), zielony M11, testy Fryderyka (p. 7b), D1 (wersja macOS w tekstach). **Fala:** 6. Przed zleceniem lider uruchamia `delegate --check` w repo `ucho-strona` (osobne repo prywatne).
- **Pliki (repo `C:/Users/HP/Documents/Repozytoria/ucho-strona`):** `public/index.html`, `public/en/index.html`, `public/instalacja/index.html`, `public/en/install/index.html`, `public/assets/css/site.css` (tylko jeśli potrzebny styl etykiety „beta”), `public/llms.txt`, `public/llms-full.txt`, `README.md` (zdanie „for Windows”).
- **SPEC:**
  - Strona główna PL i EN: obok „Pobierz dla Windows” drugi przycisk `btn btn-secondary` „Pobierz na Maca (beta)” / „Download for Mac (beta)”, prowadzący do `instalacja/#macos` / `install/#macos` (nie wprost do pliku, bo użytkownik musi przeczytać instrukcję Gatekeepera). Linia wymagań: „Windows 10 lub 11 · macOS 15+ na Apple Silicon (beta) · 16 GB RAM …” (wersja według D1). Pod przyciskiem drobny link „Wszystkie pliki wydania” do `https://github.com/Fredpry/ucho/releases` (zapas, gdy paczka macOS danego wydania się nie zbudowała).
  - FAQ: odpowiedź „Wersji na macOS jeszcze nie ma” zmieniona w widocznym `<dd>` i w JSON-LD `FAQPage` na ten sam tekst; `operatingSystem` w JSON-LD: „Windows 10, Windows 11 (x64), macOS 15+ (Apple Silicon, beta)” (wersja według D1).
  - `/instalacja/` i `/en/install/`: nowa sekcja `id="macos"` z krokami z p. 7d (tekst instrukcji Gatekeepera jest w tym planie, nie w raporcie zewnętrznym). Polskie nazwy przycisków i okien potwierdzone zrzutami Fryderyka (p. 7b-2, 7b-3) przed scaleniem.
  - Przycisk i sekcja nie zawierają żadnych obrazków ani czcionek z zewnątrz; zrzuty okien macOS (jeśli będą) jako lokalne pliki w `public/assets/`, bez danych osobowych na zrzutach (nazwa użytkownika, ścieżki).
  - Zasady strony: zero JS, zero zewnętrznych zapytań, bez inline stylów, bez półpauz i pauz w tekście, linki tylko do `ucho.app` i `github.com/Fredpry/ucho`.
- **Kryteria:** (1) `python tools/check.py` drukuje „All checks passed.”; (2) pytania FAQ w JSON-LD równe widocznym; (3) `grep` nie znajduje „Wersji na macOS jeszcze nie ma” ani „There is no macOS version yet” (dokładny tekst z `public/en/index.html` l. 101 i 212); (4) zrzuty `tools/screens.py / /en/ /instalacja/ /en/install/` w szerokościach 1440, 390, 360 bez poziomego przewijania (sprawdza lider).

### M13. README, README.pl, CHANGELOG i dokumentacja

- **Szacunek:** ok. 300 linii. **Delegowalne:** tak (lider czyta całość). **Zależności:** M10 (nazwy plików, wymagania). **Fala:** 5 (równolegle z M10, finalizacja po M11).
- **Pliki:** `README.md`, `README.pl.md` (sekcja „macOS (beta)”: wymagania, instalacja z `.dmg`, uprawnienia, ograniczenia bety, komenda MCP `/Applications/Ucho.app/Contents/MacOS/Ucho --mcp`), `CHANGELOG.md` (pod „Unreleased”: Added „macOS beta (Apple Silicon)”, „download progress and speech-model gate” z M0a/M0b), `docs/architecture.md` (nowa sekcja o warstwie platformy i backendzie whisper.cpp), `docs/hardware.md` (reguły Apple Silicon), `AGENTS.md` (mapa: `ucho/platforms/`, `ucho/paths.py`, `ucho/whispercpp.py`, `ucho/downloads.py`; komendy macOS), `PRIVACY.md` (uprawnienia macOS i do czego służą), `THIRD_PARTY.md` (pywhispercpp z zależnościami `requests`, `tqdm`, `platformdirs`, whisper.cpp i modele ggml, catap z natywną biblioteką, sounddevice/PortAudio, PyObjC, Fluent UI System Icons, PyInstaller). Sekcja „Odinstalowanie na Macu”: przeciągnij Ucho do Kosza, usuń `~/Library/Application Support/Ucho`, `~/Library/Logs/Ucho`, `~/Library/LaunchAgents/app.ucho.plist`; nagrania w `~/Ucho/recordings` zostają, dopóki ich nie usuniesz.
- **Kryteria:** (1) testy dokumentacji (`tests/test_package.py`, jeśli sprawdzają README/CHANGELOG) zielone; (2) każda nowa zależność z `requirements.txt` jest w `THIRD_PARTY.md` (nowy test porównujący listy); (3) w README nie ma obietnic spoza zakresu bety (Intel, notaryzacja, autoaktualizacja).

### Podsumowanie zadań

| Id | Tytuł | Linie | Delegowalne | Zależności | Fala |
|---|---|---|---|---|---|
| M0a | Menedżer pobierania i brama modelu mowy, bez UI (oba systemy) | 400 | tak | brak | 1 |
| M1a | Szkielet `platforms/` + prymitywy dyktowania (refaktor) | 750 | tak | brak | 1 |
| M2 | Ścieżki danych (`paths.py`) | 300 | tak | brak | 1 |
| M3a | Spike CI macOS | 150 | nie | brak | 1 |
| M1b | Skróty, instancja, powłoka (refaktor, usuwa `wintypes` z importu) | 800 | tak | M1a, M0a | 2 |
| M1c | Audio, sprzęt, czcionki, ikony (refaktor) | 600 | tak | M1a | 2 |
| M8 | Backend whisper.cpp | 550 | tak | M0a | 2 |
| M0b | Pasek pobierania i baner bramy w UI (oba systemy) | 350 | tak | M0a | 2 |
| M3b | CI macOS: markery, testy, smoke | 300 | nie | M1a, M1b, M1c, M2, M3a | 3 |
| M4 | macOS bez API Apple: sysinfo, powłoka, dźwięki, czcionki, ikony | 600 | tak | M1b, M1c, M2, M8 | 3 |
| M7a | macOS: mikrofon (sounddevice) | 400 | tak | M1c | 3 |
| M5 | macOS: instancja i skróty Carbon | 500 | nie | M1b, M2, M3b | 4 |
| M6 | macOS: schowek, wklejanie, fokus | 550 | nie | M1a, M3b | 4 |
| M7b | macOS: Process Tap, uprawnienia audio, cisza | 500 | nie | M7a, M3a | 4 |
| M9 | Kreator uprawnień, teksty platformowe | 500 | tak | M1a, M1b, M4, M0b | 4 |
| M10 | `.app`, `.dmg`, GitHub Release | 550 | nie | M0b, M2-M9, D1, D2 | 5 |
| M13 | README, CHANGELOG, docs | 300 | tak | M10 | 5 |
| M11 | Scenariusze e2e na runnerze | 350 | nie | M10 | 5 |
| M12 | Strona: przycisk beta i instrukcja | 250 | tak | M10, M11, p. 7b, D1 | 6 |

Razem ok. 8 700 linii, z czego ok. 5 800 delegowalnych (M0a, M0b, M1a, M1b, M1c, M2, M4, M7a, M8, M9, M12, M13). Największe zadania delegowane to refaktory M1b (800) i M1a (750); jeśli pierwsze zlecenie M1a wymaga więcej niż jednej rundy poprawek, M1b dzieli się na M1b-1 (skróty i instancja, ok. 300 linii) i M1b-2 (powłoka, ok. 500 linii).

## 5. Fale równoległości

Każde zadanie w osobnym worktree z gałęzi `macos/<id>` od aktualnego `macos/integration`; lider scala po kolei w podanym porządku i po każdym scaleniu uruchamia pełny zestaw testów na Windows (i od fali 3 job `macos-tests`).

| Fala | Zadania równolegle | Wspólne pliki i jak ich uniknąć |
|---|---|---|
| 1 | M0a, M1a, M2, M3a | `ucho/dictation.py`: M0a zmienia tylko `DictationController.model_key` i `start`, M1a blok l. 33-553 i `beep` (sygnatura i wywołania bez zmian). `ucho/app.py`: tylko M2 (konfiguracja logowania); M0a nie dotyka `app.py`. `ucho/meeting.py`: tylko M0a. `pl.json`: wpisy na końcu pliku, konflikty rozwiązuje lider. M3a dotyka tylko nowego `macos.yml` i `tools/macos_spike.py`. Kolejność scalania: M1a, M2, M0a |
| 2 | M1b, M1c, M8, M0b | `settings_screen.py`: M1b l. 18, 300, 903-909; M8 tylko `short_model` l. 36-37; M0b oba `_download` (l. 176-200, 486-500) i stan modelu (l. 470-485). `app.py`: M1b l. 193-232, 611-616, 752-762, 827-895; M0b konstruktor `App` (nowe atrybuty), `toggle_recording` (l. 429) i zamykanie. `screens.py`: M1b `open_path` l. 66-70 i `iconbitmap` l. 1158-1161; M0b konstruktory ekranów Meeting i Dictation. `downloads.py`: M1b polecenie procesu roboczego, M8 format klucza. M1c sam w `audio.py`, `hardware.py`, `theme.py`, `ui.py`, `i18n.py`, `Microphone`; M0b ma widżety w nowym `download_ui.py`, nie w `ui.py`. Kolejność scalania: M1b, M1c, M8, M0b; przed zleceniem każdego zadania lider wkleja do SPEC numery linii z aktualnego `macos/integration` |
| 3 | M3b, M4, M7a | M3b: `requirements.txt`, `macos.yml`, `conftest.py`, `test_package.py`. M4: nowe pliki w `platforms/macos/`, `hardware.py`, `design/icons/`, `THIRD_PARTY.md` (tylko czcionka ikon). M7a: nowy `platforms/macos/audio.py`. Brak wspólnych plików |
| 4 | M5, M6, M7b, M9 | `platforms/macos/permissions.py` tworzy tylko M7b; M9 używa accessora z M1a i nie tworzy plików w `platforms/`. `screens.py`: M6 (okno dyktowania `noActivates`) i M9 (komunikaty, ostrzeżenie `SilenceWatch` z M7b: M7b tylko emituje zdarzenie, M9 je wyświetla): rozłączne fragmenty. `config.py` i `app.py`: tylko M5. `audio.py`: tylko M7b |
| 5 | M10, M13, potem M11 | M10: `__main__.py`, `packaging/`, `tools/build_macos.py`, workflowy. M11: `tools/e2e_macos/`, `ucho/cli_transcribe.py`, job w `macos.yml` (po M10). M13: tylko dokumentacja |
| 6 | M12 | osobne repo `ucho-strona` |

Ścieżka krytyczna: M1a → M1b → M3b → M5/M6/M7b → M10 → M11 → p. 7b → M12. M0a/M0b i M8 idą obok. Jeśli Fryderyk chce pasek pobierania i blokadę na Windows przed betą macOS (decyzja D3), M0a i M0b robi się od `main`, wydaje dla Windows i scala `main` do `macos/integration`; wtedy M0b przechodzi do fali 1 (po M0a), a fala 2 rusza po jego scaleniu.

## 6. Weryfikacja

**Testy automatyczne:**
- Windows (`ci.yml`, bez zmian): `test`, `gui-smoke`, `installer` na każdym PR. Po każdym zadaniu cały zestaw zielony, liczba wykonanych testów nie maleje.
- macOS (`macos.yml`): `macos-tests` (macos-15, macos-26), `macos-smoke`, `macos-asr` (whisper.cpp `ggml-base-q5_1` i OpenVINO CPU `whisper-large-v3-turbo-int4-ov` na WAV z `say`, LLM `Qwen2.5-1.5B-Instruct-int4-ov` ze schematem), `macos-package`, `macos-e2e`; modele w `actions/cache` z kluczem z `config.REVISIONS`, tylko małe modele. Runner `macos-15` ma 3 rdzenie i ok. 7 GB RAM, bez Metal (maszyna wirtualna): whisper.cpp działa tam na CPU, co M8 obsługuje fallbackiem.
- Ukryte testy akceptacyjne lidera do każdego zadania delegowanego, trzymane poza repo (scratchpad), zgodnie z `model-lab/docs/chudy-lider.md`; uruchamiane według p. 4.0.

**Review (lider, na diffie):** brak zmian zachowania w refaktorach M1a-M1c (ukryty test `ast.dump`, porównanie wywołań WinAPI 1:1), poprawność wątków (Carbon, NSPasteboard, Tk tylko na wątku głównym), brak importów systemowych w ciele modułów (test AST z p. 3.2), `t()` i `pl.json`, nagłówki SPDX.

**Bezpieczeństwo i prywatność (kontrole przed każdym scaleniem i przed wydaniem):**
- `grep -rnE "urllib|requests|http\.client|socket\.(create_connection|connect)|websocket" ucho ucho_mcp` pokazuje tylko istniejące miejsca i gniazdo Unix z M5; żadnych nowych połączeń poza `huggingface_hub` w procesie roboczym `downloads.py`/`transcribe.model_path`. `pywhispercpp` ma własne pobieranie przez `requests`; test M8 5a pilnuje, że Ucho go nie wywołuje.
- `HF_HUB_DISABLE_TELEMETRY` i `DO_NOT_TRACK` ustawione także w paczce: test, że w zamrożonej aplikacji `huggingface_hub` nie jest importowany przed `ucho.config` (sprawdzenie `sys.modules` w `--smoke-test`); opt-out telemetrii OpenVINO zapisany przy pierwszym starcie (M10).
- Info.plist zawiera tylko `NSMicrophoneUsageDescription` i `NSAudioCaptureUsageDescription`; brak kamery, nagrywania ekranu, kontaktów, lokalizacji; Dostępność prosi tylko przy pierwszym dyktowaniu lub w kreatorze. Brak entitlements w becie.
- DMG nie zawiera modeli, `settings.json`, logów, nagrań ani `.venv`; zbudowana `.app` nic nie zapisuje do bundla (M10 p. 3).
- Logi bez nazw spotkań i ścieżek użytkownika (obecne zasady), także w nowych modułach (`downloads.py` loguje tylko id repo i rozmiary).
- LaunchAgent powstaje tylko po włączeniu autostartu przez użytkownika i znika po wyłączeniu; nie powstaje, gdy aplikacja działa z ścieżki tymczasowej (App Translocation).
- Gniazdo instancji z uprawnieniami 0600, przyjmuje tylko komendy z `COMMANDS`.
- Pobieranie zawsze z przypiętą rewizją (`config.REVISIONS`), także dla modeli ggml.

## 7. Testy użytkownika

### 7a. Scenariusze wykonywane przez agenta na runnerze macOS (job `macos-e2e`, M11)

1. **Uruchomienie paczki.** Zamontuj `Ucho-macOS-arm64.dmg`, skopiuj `Ucho.app` do `$RUNNER_TEMP/Applications`, uruchom `Ucho.app/Contents/MacOS/Ucho --smoke-test 3` z `UCHO_DATA_DIR`. Oczekiwane: kod 0, w logu brak `ERROR`, okna kreatora, ekranów i obu motywów otwarte, `settings.json` w `UCHO_DATA_DIR`, nic nie zapisane w `.app`.
2. **Transkrypcja syntetycznego nagrania.** `say` tworzy dwa pliki (PL i EN), skrypt składa z nich sesję (`microphone.flac`, `meeting.flac`) i uruchamia transkrypcję w zamrożonej paczce przez `Ucho --worker ucho.cli_transcribe <folder>` (moduł z M11, na liście dozwolonych modułów `--worker`, wołający `transcribe.iter_transcribe_session`) z modelem `ggml-base-q5_1` (ukryty model CI z M8). Oczekiwane: `transcript.md`, `.srt`, `.txt`, `meta.json`; słowa kluczowe z obu nagrań; język PL dla pierwszej ścieżki i EN dla drugiej; `meta.json` ma `model` i `device`. Jeśli runner nie ma polskiego głosu (`say -v '?'`, wynik M3a p. 5), ścieżka PL jest pomijana z adnotacją w raporcie.
3. **Serwer MCP.** Wyślij do `Ucho.app/Contents/MacOS/Ucho --mcp` `initialize`, `tools/list`, `list_meetings`, `search_transcripts` z frazą z p. 2, `get_transcript`. Oczekiwane: poprawne odpowiedzi JSON-RPC, na stdout nic poza JSON-RPC, spotkanie z p. 2 na liście, fraza znaleziona, identyfikator z `..` odrzucony; drugi proces `--mcp` działa równolegle z otwartą aplikacją (brak blokady jednej instancji).
4. **Info.plist, podpis i rozmiar.** `plutil -extract` dla kluczy z M10, `codesign --verify --deep --strict` (także każdej `*.dylib` i `*.so` w bundlu), informacyjnie `spctl`, `du -sh Ucho.app`, rozmiar DMG. Oczekiwane: komplet kluczy, poprawny podpis ad-hoc, rozmiary pod progami, raport w podsumowaniu joba.
5. **Pobieranie modelu z paskiem i blokada.** Pusty `UCHO_DATA_DIR`, uruchomienie z flagą testową, która woła `App.toggle_recording()` i `dictation.toggle()` (smoke test rozszerzony w M0b). Oczekiwane: nagrywanie i dyktowanie odmówione z komunikatem, okno pokazane z banerem `missing`; po `DownloadManager.start("ggerganov/whisper.cpp:ggml-tiny-q5_1.bin")` zdarzenia postępu rosną monotonicznie do 100 %; potem brama otwarta; anulowanie w połowie i ponowny start kończą się `done`.
6. **Podsumowanie LLM.** Na sesji z p. 2 uruchom podsumowanie modelem `Qwen2.5-1.5B-Instruct-int4-ov` przez OpenVINO CPU w paczce. Oczekiwane: `summary.md` zgodny ze schematem, filtr nazwisk nie przepuszcza imion spoza transkryptu (tekst syntetyczny z jednym imieniem).
7. **Jedna instancja w paczce.** Uruchom paczkę zminimalizowaną, potem drugi raz z `--dictate`. Oczekiwane: drugi proces kończy się w < 2 s, pierwszy loguje komendę `dictate`; po `kill -9` pierwszego nowe uruchomienie startuje (martwe gniazdo usunięte).
8. **Cisza na ścieżce systemowej bez uprawnienia.** Nagranie 25 s w paczce na runnerze (bez zgody na dźwięk systemowy tap daje zera). Oczekiwane: w logu ostrzeżenie `SilenceWatch` dokładnie raz, nagranie i plik `meeting.flac` poprawne.
9. **Autostart.** Włączenie autostartu w paczce (flaga testowa) tworzy `~/Library/LaunchAgents/app.ucho.plist` w podstawionym `HOME`, `plutil -lint` przechodzi, `ProgramArguments` wskazuje na skopiowaną `.app`; wyłączenie usuwa plik; uruchomienie z zamontowanego DMG nie tworzy pliku i loguje komunikat o przeniesieniu do Aplikacji.
10. **Zamykanie.** `SIGTERM` w trakcie pobierania modelu i w trakcie transkrypcji. Oczekiwane: brak osieroconych procesów `--worker` (sprawdzenie `pgrep -f "Ucho --worker"` po 5 s).

### 7b. Lista kontrolna dla Fryderyka (prawdziwy Mac z Apple Silicon)

Na początku zapisz wersję systemu (menu Apple → Ten Mac), bo od niej zależy D1 i tekst na stronie.

| # | Krok | Oczekiwany wynik |
|---|---|---|
| 1 | Pobierz `Ucho-macOS-arm64.dmg` z wydania, sprawdź `shasum -a 256` z `SHA256SUMS-macos.txt`, otwórz, przeciągnij Ucho do Aplikacji | Suma zgodna; DMG montuje się bez błędu, w oknie są Ucho i skrót Aplikacje |
| 2 | Otwórz Ucho z Aplikacji | Okno o nieotwarciu aplikacji (Apple nie mogło jej sprawdzić) z przyciskami „Gotowe” i „Przenieś do Kosza”; zrób zrzut (dokładne polskie teksty do strony). Jeśli zamiast tego macOS pisze, że aplikacja „jest uszkodzona”, to błąd podpisu w M10 (zgłoś, nie obchodź) |
| 3 | Ustawienia systemowe → Prywatność i ochrona → sekcja bezpieczeństwa na dole → „Otwórz mimo to”, w kolejnym oknie znowu „Otwórz mimo to”, hasło lub Touch ID | Ucho startuje, pokazuje kreator; kolejne uruchomienia bez pytań (zrzuty polskich nazw sekcji i przycisków) |
| 4 | W kreatorze pobierz zalecany model | Pasek z procentami, „x z y GB”, prędkością i czasem do końca; przycisk nagrywania wyłączony z komunikatem do końca pobierania; Anuluj i ponowny start wznawiają pobieranie (nie od zera) |
| 5 | W trakcie pobierania przejdź dalej w kreatorze i na ekran Dyktowanie, potem wróć | Pasek nadal widoczny w banerze, pobieranie trwa |
| 6 | Wyłącz Wi-Fi i zacznij pobierać inny model | Czytelny komunikat o braku połączenia i przycisk „Spróbuj ponownie”; po włączeniu Wi-Fi pobieranie dochodzi do końca |
| 7 | Gdy jeden model jest już pobrany, wybierz w Ustawieniach większy i zacznij go pobierać, a w tym czasie nagraj 1 min | Nagrywanie działa (prośba „jakikolwiek model gotowy”); baner „Używam {model}, dopóki {nowy} się nie pobierze”; transkrypt zrobiony starszym modelem, zapisanym w `meta.json` |
| 8 | Krok uprawnień: Mikrofon, Dźwięk systemowy | Systemowe okna zgody; po zgodzie stany zielone; zrzut sekcji z dźwiękiem systemowym w Ustawieniach (polska nazwa do strony) |
| 9 | Odmów zgody na mikrofon, spróbuj nagrać; potem włącz zgodę w Ustawieniach | Czytelny komunikat z przyciskiem do ustawień; po zgodzie nagrywanie działa bez restartu albo komunikat mówi, że trzeba uruchomić ponownie |
| 10 | Nagraj 3-5 min spotkania (Zoom, Meet lub Teams w przeglądarce) w słuchawkach przewodowych | Dwie ścieżki, obie z dźwiękiem; podczas nagrania transkrypt na żywo; po stopie transkrypt z podziałem „Ja”/„Spotkanie”; piknięcia Ucho nie są na ścieżce „Spotkanie” |
| 11 | Krótkie nagranie z mikrofonem słuchawek Bluetooth (np. AirPods) | Nagranie działa (mikrofon 16/24 kHz jest przepróbkowany), w logu brak błędów formatu |
| 12 | Dyktowanie skrótem ⌃⇧Space do Notatek, Safari, Slacka i VS Code | Prośba o Dostępność przy pierwszym razie; tekst wklejony w miejscu kursora; poprzedni schowek przywrócony; Esc anuluje; okienko dyktowania nie zabiera fokusu |
| 13 | Dyktowanie do pola hasła w Safari i do Terminala z włączonym „Secure Keyboard Entry” | Tekst nie jest wpisywany, zostaje w schowku z komunikatem „naciśnij ⌘V” |
| 14 | Dyktowanie, gdy okno Ucho jest na innym biurku (Spaces) albo na drugim monitorze | Tekst trafia do aplikacji z pierwszego planu, okno Ucho nie przeskakuje |
| 15 | Podsumowanie spotkania z p. 10 (Qwen3-4B lub 8B) | Podsumowanie powstaje bez zamrożenia UI; czas zapisz do `docs/benchmarks.md` |
| 16 | Pasek menu, zamknięcie okna, ponowne otwarcie z Docka, drugie uruchomienie z Aplikacji, wyjście ⌘Q i z paska menu | Jedna instancja; okno wraca; menu ma „Nagraj” i „Dyktuj”; po wyjściu w Monitorze aktywności nie ma procesów Ucho |
| 17 | Włącz „Otwieraj przy logowaniu”, wyloguj i zaloguj | Ucho startuje zminimalizowane; macOS pokazuje powiadomienie o dodaniu rzeczy w tle |
| 18 | Uśpij Maca w trakcie nagrania, wybudź | Nagranie trwa albo kończy się czytelnym komunikatem; plik nie jest uszkodzony |
| 19 | Język systemu polski, potem angielski; tryb ciemny i jasny | Interfejs po polsku/angielsku zgodnie z systemem; motyw zmienia się z systemem |
| 20 | Serwer MCP w Claude Desktop (`/Applications/Ucho.app/Contents/MacOS/Ucho --mcp` w konfiguracji) | Claude widzi spotkania z p. 10 i znajduje frazę |
| 21 | Zainstaluj nowszy build na starszy (aktualizacja) | Instrukcja z p. 7d (krok 7) wystarcza; ustawienia, modele i nagrania zostają |
| 22 | Odinstalowanie według README (M13) | Po usunięciu nic nie startuje przy logowaniu; nagrania zostają w `~/Ucho/recordings` |
| 23 | Pomiar: `tools/benchmark_engine.py` i `tools/benchmark_wer.py` na Metal | Opóźnienie dyktowania i WER zapisane; decyzja o domyślnym modelu na Macu |

### 7c. Regresja Windows dla Fryderyka (po scaleniu M0a, M0b, M1a, M1b, M1c, przed scaleniem `macos/integration` do `main`)

Refaktory przenoszą kod WinAPI, którego testy automatyczne nie sprawdzają do końca (SendInput, prawdziwy schowek, Jump List), więc przed scaleniem do `main` potrzebny jest krótki przebieg na Windows 11 (ok. 20 min):

| # | Krok | Oczekiwany wynik |
|---|---|---|
| 1 | Czysta instalacja przez `Ucho-Setup.exe` z gałęzi (build z CI), pierwszy start | Kreator; pobieranie zalecanego modelu z paskiem; Meeting i Dictation pokazują baner, przyciski nagrywania i dyktowania wyłączone do końca pobierania |
| 2 | Skrót dyktowania i pozycja „Dyktuj” w tray, gdy model się pobiera | Okno pokazuje się z banerem, zamiast milczeć |
| 3 | Dyktowanie do Notatnika, Worda, przeglądarki, Teams | Tekst wklejony, schowek przywrócony, dyktat nie ląduje w historii Win+V, Esc anuluje, piknięcia jak dawniej |
| 4 | Nagranie spotkania 2 min (mikrofon i dźwięk systemowy z odtwarzanego filmu) | Obie ścieżki z dźwiękiem, transkrypt na żywo i po stopie |
| 5 | Skróty globalne, druga instancja (show), autostart włącz i wyłącz, Jump List, postęp na pasku zadań | Jak w wersji z `main` |
| 6 | Zmiana modelu na większy i nagranie w trakcie jego pobierania | Nagrywanie działa starszym modelem, baner informuje |
| 7 | (M1c, backendy audio, sprzętu i czcionek) Ustawienia → lista mikrofonów i wyjść; nagranie 1 min z mikrofonem i dźwiękiem systemowym (film), w trakcie odłącz i podłącz słuchawki USB lub Bluetooth; potem zmień domyślny mikrofon w Windows i podyktuj zdanie; kreator: rekomendacja modelu | Listy urządzeń jak w wersji z `main`; obie ścieżki (`microphone.flac`, `meeting.flac`) niepuste; po odłączeniu nagranie wznawia się na nowym urządzeniu (w logu „Track ... resumed on”); dyktowanie używa nowego domyślnego mikrofonu; ikony, czcionki, język interfejsu i rekomendacja modelu (RAM, procesor, karta) jak dawniej |
| 8 | (M8, modele) Ustawienia → lista modeli mowy; pobranie i wybór modelu; test fallbacku | Dokładnie 4 modele OpenVINO (+ Parakeet, gdy zainstalowany), bez wpisów whisper.cpp; pobieranie i wybór działają jak dotąd; fallback nie podsuwa modeli whisper.cpp |
| 9 | (M1b, szczegóły kroku 5) Skróty Ctrl+Alt+R i Ctrl+Shift+Space, Esc anuluje dyktowanie, zajęty skrót daje komunikat; druga instancja (skrót Ucho pokazuje okno, Ucho Dictate otwiera dyktowanie); Jump List „Nagraj spotkanie” i „Dyktuj” przy działającym Ucho, jeden przycisk na pasku (kafelek + okno), ikona `dictate.ico`; autostart: wpis HKCU Run z `--minimized`, toast przy błędzie, start zminimalizowany po restarcie; postęp na pasku (czerwony przy nagrywaniu, pasek przy transkrypcji i pobieraniu, ruchomy przy podsumowaniu); „Otwórz folder”; układ tray: dyktowanie bez zabierania fokusu; aktualizacja instalatorem wykrywa działające Ucho (AppMutex), `install.ps1` ustawia AppUserModelID | Jak w wersji z `main` |

### 7d. Instrukcja instalacji na Macu (źródło tekstu dla M12 i README)

Teksty w cudzysłowach do potwierdzenia zrzutami z p. 7b-2, 7b-3 i 7b-8 przed publikacją.

1. Pobierz `Ucho-macOS-arm64.dmg` z `https://github.com/Fredpry/ucho/releases/latest/download/Ucho-macOS-arm64.dmg`. Opcjonalnie sprawdź sumę: `shasum -a 256 ~/Downloads/Ucho-macOS-arm64.dmg` i porównaj z plikiem `SHA256SUMS-macos.txt` z tego samego wydania.
2. Otwórz plik `.dmg` i przeciągnij Ucho do folderu Aplikacje. Nie uruchamiaj Ucho z okna obrazu dysku (macOS uruchomiłby kopię z ukrytej, tymczasowej ścieżki).
3. Otwórz Ucho z folderu Aplikacje. macOS pokaże okno, że nie otworzył aplikacji, bo Apple nie mogło jej sprawdzić (beta nie jest jeszcze podpisana certyfikatem Apple). Kliknij „Gotowe”, nie „Przenieś do Kosza”.
4. Otwórz Ustawienia systemowe → Prywatność i ochrona i przewiń na dół do sekcji bezpieczeństwa. Przy informacji o zablokowaniu Ucho kliknij „Otwórz mimo to”, w następnym oknie jeszcze raz „Otwórz mimo to” i potwierdź hasłem lub Touch ID. Przycisk jest widoczny mniej więcej przez godzinę od próby otwarcia; jeśli go nie ma, powtórz krok 3. (W macOS 15 i nowszych nie działa już dawny sposób z kliknięciem z klawiszem Control i „Otwórz”; dotyczy to tylko macOS 14, jeśli D1 go obejmie.)
5. Przy pierwszym starcie kreator pobiera model mowy (pasek pobierania; nagrywanie ruszy, gdy model będzie gotowy) i prosi o uprawnienia: Mikrofon, Dźwięk systemowy (w Ustawieniach: Prywatność i ochrona → nagrywanie ekranu i dźwięku systemowego, lista „tylko dźwięk systemowy”) i Dostępność (potrzebna tylko do wklejania dyktowanego tekstu).
6. Jeśli macOS pisze, że aplikacja jest uszkodzona, albo krok 4 nie pomaga: upewnij się, że plik pochodzi z `github.com/Fredpry/ucho` i suma się zgadza, a potem w Terminalu wpisz `xattr -dr com.apple.quarantine /Applications/Ucho.app` i otwórz Ucho jeszcze raz.
7. Aktualizacja: zamknij Ucho, przeciągnij nową wersję do Aplikacji (zastąp starą) i powtórz krok 4. Beta jest podpisana tylko lokalnie, więc macOS traktuje każdą wersję jak nową aplikację: w Ustawieniach → Prywatność i ochrona → Dostępność usuń Ucho przyciskiem „−” i dodaj ponownie (albo w Terminalu `tccutil reset Accessibility app.ucho`); o mikrofon i dźwięk systemowy macOS zapyta ponownie (gdy nie pyta: `tccutil reset Microphone app.ucho`). Ustawienia, modele i nagrania zostają.

## 8. Ryzyka i otwarte pytania (tylko blokujące)

1. **Pasek menu (pystray) obok Tk.** Jeśli spike M3a lub `macos-smoke` pokaże crash albo zawieszenie, potrzebny jest własny `NSStatusItem` (ok. 150 linii, robi lider, przed M10). Rozstrzyga M3a.
2. **Process Tap na runnerze i u użytkownika.** Bez zgody tap daje same zera, a prompt bywa kapryśny. W CI nie sprawdzimy prawdziwego dźwięku (7a-8 sprawdza tylko ścieżkę „cisza”); jedyny pewny test to p. 7b-10. Jeśli catap zawiedzie, plan B to własny kod na `pyobjc-framework-CoreAudio` (ok. 300 linii, dodatkowe 1-2 dni). Blokuje M10.
3. **PyInstaller i OpenVINO/whisper.cpp/catap.** Pluginy OpenVINO i `plugins.xml`, biblioteki ggml z katalogu głównego `site-packages` oraz `libcatap_coreaudio.dylib` mogą nie trafić do bundla; błąd widać dopiero w runtime. Dlatego M11 p. 2, 6 i 8 ładują prawdziwe modele i tap z paczki. Blokuje wydanie.
4. **D2: reset uprawnień po każdej aktualizacji przy podpisie ad-hoc.** Dotyczy wszystkich zgód TCC (Dostępność wygląda na włączoną, ale nie działa; mikrofon i dźwięk systemowy pytają ponownie). Pytanie do Fryderyka: czy w becie wystarczy instrukcja (p. 7d krok 7), czy od razu stały samopodpisany certyfikat w sekrecie CI (ok. 30 linii w M10, Gatekeeper działa tak samo jak przy ad-hoc). Decyzja przed M10.
5. **Domyślny model mowy na Macu.** AGENTS.md wymaga pomiaru jakości przed zmianą domyślnego modelu; WER whisper.cpp q5_0 względem OpenVINO int8 jest nieznany. Do czasu pomiaru (M8 w CI na TTS, p. 7b-23 na Macu) beta proponuje large-v3 q5_0 jako zalecany, nie wymuszony.
6. **Brama nagrywania (rozstrzygnięte w recenzji).** Prośba Fryderyka brzmi „blokować nagrywanie, dopóki nie jest jakikolwiek model transkrypcyjny gotowy”, więc M0a otwiera bramę, gdy pobrany jest dowolny oferowany model, i używa go do czasu pobrania wybranego (z banerem i zapisem w `meta.json`). Blokada dotyczy tylko sytuacji, gdy nie ma żadnego modelu. Dzięki temu zmiana modelu na większy nie wyłącza nagrywania na czas pobierania.
7. **D1: minimalna wersja macOS.** Runner `macos-14` wycofano 2026-11-02, więc macOS 14 nie jest testowany w CI, a osobna lista zgód „tylko dźwięk systemowy” jest w Ustawieniach dopiero od macOS 15 (na 14.x zgodę da się tylko przyznać przy pierwszym pytaniu albo zresetować `tccutil`). Propozycja: `LSMinimumSystemVersion` 15.0 w becie; 14.4, tylko jeśli Mac Fryderyka ma 14.x (wtedy p. 7b na nim jest jedynym testem 14.x). Decyzja przed M10 i M12.
8. **D3: wcześniejsze wydanie M0 dla Windows.** Prośba o pasek pobierania dotyczy obecnych użytkowników Windows. Domyślnie M0 trafia do `main` razem z betą macOS; jeśli ma wyjść wcześniej, M0a i M0b robi się od `main` (p. 5). Decyzja przed zleceniem M0a.
9. **Pobieranie przez `hf-xet`.** `huggingface_hub` 1.30 pobiera przez natywny `hf-xet`; jeśli ten od razu alokuje pełny plik, liczenie postępu z `*.incomplete` pokaże 100 % na starcie. Rozstrzyga 10-minutowy pomiar lidera przed zleceniem M0a (opisany w M0a); zapas: `HF_HUB_DISABLE_XET=1` w procesie roboczym.
10. **Skróty Carbon między procesami.** Bez `kEventHotKeyExclusive` macOS nie zgłasza skrótu zajętego przez inną aplikację; z tą opcją zachowanie trzeba potwierdzić w M3a p. 4. Gdy się nie da, beta ma znane ograniczenie: kolizja skrótu z inną aplikacją nie jest raportowana.

## Recenzja

Recenzja planu z 2026-11-19 (Claude, bez delegowania). Twierdzenia o kodzie sprawdzone w plikach worktree (`944721f`), o pakietach w PyPI (JSON API) i w źródłach kół `pywhispercpp` 1.5.1 i `catap` 0.6.1, o modelach ggml w API Hugging Face, o runnerze w changelogu GitHub.

**Werdykt: gotowy po poprawkach.** Fala 1 (M0a, M1a, M2, M3a) może ruszyć od razu. Przed M10 potrzebne są decyzje D1 (minimalny macOS) i D2 (certyfikat), a przed M0a decyzja D3 (wcześniejsze wydanie M0 na Windows; domyślnie „nie”, więc też nie blokuje); żadna z nich nie blokuje fal 1-4.

**Problemy, które blokowały plan (poprawione):**
1. Fałszywe twierdzenie w p. 0: `hotkeys.py`, `instance.py`, `winshell.py`, `dictation.py` i `tools/benchmark_engine.py` importują `ctypes.wintypes` na poziomie modułu, a w Pythonie 3.12 ten import na macOS rzuca `ValueError` (`VARIANT_BOOL` z typem `v`). Skutki: M3b nie mógłby przejść bez M1b (dodana zależność), zasada „każdy plik `platforms/**` importuje się wszędzie” była niewykonalna dla przeniesionego kodu WinAPI (zmieniona w p. 3.2), dodany test AST zakazanych importów.
2. Brama M0 była sprzeczna z prośbą Fryderyka („jakikolwiek model gotowy”) i zablokowałaby dyktowanie, które dziś działa dzięki zapasowi w `DictationController.model_key`. Nowa semantyka: `ready`/`fallback`/`downloading`/`missing`, transkrypcja zapasowym modelem z informacją.
3. M0 w fali 1 miało używać `platforms.shell().worker_command`, które powstaje dopiero w M1b; teraz `diarize.worker_python()`, a M1b przepina oba miejsca.
4. `expected_size` wołało Hugging Face z procesu aplikacji (ryzyko zamrożenia UI); rozmiar i kontrola miejsca na dysku przeszły do procesu roboczego.
5. M8: `auto_detect_language` w `pywhispercpp` 1.5.1 zwraca krotkę, nie słownik (kryterium 1 poprawione); `Model("nazwa")` sam pobiera model przez `requests` bez przypiętej rewizji (dodany zakaz i test 5a).
6. Wersje null: `SingleInstance.acquire()` zwracające `False` (zgodnie z ogólną zasadą „brak”) kazałoby aplikacji na macOS zakończyć się jako „druga instancja”, więc `macos-smoke` z M3b by nie przeszedł; semantyka null opisana w p. 3.2.
7. Polecenie testów wykonawcy wskazywało `.venv-dev` w worktree, którego tam nie ma.
8. M3b: kryterium 4 (porównanie `pip freeze` w jobie Windows) przeczyło kryterium 3 (`ci.yml` nietknięty); zastąpione testem w `tests/test_package.py`.

**Pozostałe zmiany:**
- p. 0: nowe wiersze (wintypes, `DelayedClipboard(text, pid)`, zapas modelu w dyktowaniu, `.venv-dev`, koła arm64 z PyPI, API `pywhispercpp` i `catap`, potwierdzone wycofanie `macos-14`).
- p. 1 i 2: propozycja macOS 15+ (D1), natywna biblioteka `catap`, zależności i katalog bibliotek `pywhispercpp`, telemetria OpenVINO, `kEventHotKeyExclusive`.
- p. 3: interfejs `Permissions` z wersją null w M1a (M9 nie tworzy już pliku w `platforms/`, więc nie konfliktuje z M7b); `offer_delayed(text, consumer_pid)`; nowe pliki `downloads.py` i `download_ui.py`.
- p. 4.0: uruchamianie ukrytych testów, test `ast.dump` przeniesionego kodu w refaktorach.
- M0 podzielone na M0a (logika, 400 linii, fala 1) i M0b (UI, 350 linii, fala 2), z dokładnymi zakresami linii; widżety w nowym `download_ui.py`, żeby nie konfliktować z M1c w `ui.py`; skrót, tray i `--record`/`--dictate` przy zamkniętej bramie pokazują okno.
- M1a: `beep` zachowuje sygnaturę i wywołania (brak konfliktu z M0a w `DictationController.start`), kryterium 2 nie importuje już `ucho.app` (w jobie `test` nie ma `customtkinter`), `test_dictation_delayed_clipboard.py` bez zmian.
- M1b: zależność od M0a, `downloads.py` w plikach, zakaz importu `winshell` w `ucho/`, test importu pod `sys.platform = "darwin"`.
- M4: zależności M2 i M8, rozłączne progi RAM dla LLM.
- M5: `kEventHotKeyExclusive`, kryterium 4 zależne od M3a. M6: bezpieczne wprowadzanie (`IsSecureEventInputEnabled`), uwaga o nieaktualnym `frontmostApplication` w testach.
- M7b: próbny tap tylko na żądanie, nie co 1 s.
- M8: format klucza `repo:plik` w jednej funkcji `config.split_model_id`, ukryte modele CI (`ggml-tiny-q5_1`, `ggml-base-q5_1`), rozmiary i `sha` repo z API HF, `downloads.py`, `short_model`, `tools/benchmark_engine.py`; `THIRD_PARTY.md` przeniesiony do M13.
- M10: zależności M0b i M9, biblioteki natywne do zebrania i podpisania, ponawianie `hdiutil`, opt-out telemetrii OpenVINO, wykrywanie App Translocation, `--mcp`/`--worker` przed blokadą instancji, job `assets` w `release.yml` (bez niego link „latest” na stronie dawał 404 po nieudanym buildzie macOS), `spctl` tylko informacyjnie.
- M11: moduł `ucho/cli_transcribe.py` zamiast `tools.e2e_transcribe` (katalogu `tools/` nie ma w paczce), 350 linii.
- M12: instrukcja Gatekeepera wpisana do planu (p. 7d) zamiast odwołania do zewnętrznego raportu; poprawiony szukany tekst EN; link zapasowy do listy wydań; `delegate --check` w repo strony; bez obrazków i danych osobowych z zewnątrz. Zasady strony (zero JS, zero zewnętrznych zapytań, dozwolone hosty z `tools/check.py`) nie były łamane i zostają.
- M13: zależności `pywhispercpp`, natywna biblioteka `catap`, sekcja odinstalowania na Macu.
- p. 5: nowe fale z listą rozłącznych fragmentów w fali 2, ścieżka krytyczna przez M1b, wariant D3.
- p. 7: 7a uzupełnione o scenariusze 7-10 (jedna instancja w paczce, cisza bez uprawnienia, autostart i App Translocation, zamykanie bez osieroconych procesów) i poprawione identyfikatory modeli; 7b rozszerzone z 13 do 23 kroków (suma kontrolna, brak sieci, model zapasowy w trakcie pobierania, odmowa uprawnień, Bluetooth, pole hasła, Spaces, język i motyw, MCP w Claude Desktop, odinstalowanie, wyjście bez osieroconych procesów); nowe 7c (regresja Windows po refaktorach) i 7d (tekst instrukcji instalacji).
- p. 8: ryzyko 6 rozstrzygnięte; nowe D1, D3, `hf-xet`, skróty Carbon.

**Ostateczna lista zadań:** jak w tabeli „Podsumowanie zadań” (19 zadań, ok. 8 700 linii, ok. 5 800 delegowalnych).

## Postęp: fala 3

Baza: ab5f603. Scalone do `macos/dev` (`--no-ff`, w kolejności): M3b, M4, M7a. Wynik integracji: pytest 696 passed, 9 skipped (jedna niestabilna `test_live::test_controller_stop_submits_only_the_tail[True]` przeszła po osobnym powtórzeniu); ruff czysto; `set_copyright --check` 0 plików; `--smoke-test 3` OK. Konflikt tylko w `tests/test_platforms.py` (test zapasowych modułów null): zachowana wersja M7a, która sprawdza istnienie pliku `macos/<akcesor>.py`. `ucho/platforms/macos/__init__.py` identyczny z `windows/__init__.py`. CI PR #30 (commit 1a47fcc): wszystkie checki zielone (test, installer, gui-smoke na Windows; macos-tests na macos-15 i macos-26: 684 passed, 21 skipped, wszystkie z powodem "Windows only"; macos-smoke; macos-asr). Pierwszy run (f7d989b) padł na dwóch testach `test_hardware` (`test_profile_without_openvino`, `test_profile_survives_failing_probes` zakładały reguły Windows na hoście macOS: teraz `dataclasses.replace(p, system="win32")`) i na pominięciu testu M7a przez brak `sounddevice` (dopisany do kroku instalacji w `macos.yml`). Kryteria tylko-macOS z M4 i M7a (plutil, sysinfo, MacAudio na prawdziwym sounddevice) uruchomiły się w macos-tests i przeszły; zostają do p. 7b rzeczy wymagające prawdziwego urządzenia (uprawnienie mikrofonu, słuchawki, dźwięki, Tk z czcionkami).

**Zrobione**
- M3b: `.github/workflows/macos.yml` (macos-tests na macos-15 i macos-26, macos-smoke, macos-asr z whisper.cpp na Metal), `requirements.txt` z markerami platform, `tools/macos_asr_check.py`, `UCHO_ONLY_WINDOWS_SKIPS=1` (pominięcie bez powodu "Windows only: ..." wywala job macOS). Poprawki: `initial_prompt` w pywhispercpp tylko jako str, `diagnostics.Redactor` zachowuje wiodący `/` ścieżek POSIX.
- M4: `platforms/macos/{sysinfo,shell,sounds,fonts}.py`, reguły Apple Silicon w `hardware.py` (mac_large, mac_turbo, `HardwareProfile.system`, `recommend_llm` dla darwin), podzbiór Fluent UI System Icons (`design/icons/FluentSystemIcons-Ucho.ttf`, `tools/subset_icons.py`, `THIRD_PARTY.md`).
- M7a: `platforms/macos/audio.py` (MacAudio, sounddevice/CoreAudio, mikrofon przez AVFoundation z fallbackiem `unknown`), `system=[]` do czasu ScreenCaptureKit.

**Odchylenia od planu**
- `MacShell.worker_command` ze źródeł: `[sys.executable, '-m', module, *args]` (diarize samo dodaje `--worker` w args, wersja z planu podwoiłaby flagę). W paczce `.app`: `Ucho --worker ucho.diarize --worker ...`; M10 musi zdjąć pierwsze `--worker <moduł>` i przekazać resztę do `main()` modułu.
- Stosy czcionek macOS wg spike'a: sans `.AppleSystemUIFont, Helvetica Neue, Helvetica`, mono `Menlo, Monaco, Courier`, display `New York, Georgia, Times`, bez SF Pro/SF Mono, strong/semibold puste. `theme.Fonts.resolve` przyjmuje rodziny zaczynające się od `.`.
- Ikony rysuje Pillow z pliku (`ui._font_file()` -> `icon_font()`), tak samo jak na Windows; rejestracja TTF w Tk (Core Text) NIE jest potrzebna. Kryterium "czcionka ikon w Tk" zastąpić sprawdzeniem przez Pillow (`ImageFont.truetype`).
- `set_window_icon` używa `icon.draw/draw_dictate`; `hardware.benchmark` idzie przez `transcribe.create_engine` (dla whisper.cpp `_forced_kwargs` nie działa, benchmark mierzy dekodowanie swobodne).
- `autostart_set(True)` odmawia z `/Volumes/` i z App Translocation (M9 powinno to pokazać użytkownikowi). Start z bundla: `open -a <.app>`.
- Test `test_track_with_mac_audio...` sprawdza 16 kHz stereo bez resamplingu (M1c zakłada 48 kHz); bez skutków.

**Uwagi dla fali 4 (M5, M6, M7b, M9)**
- Nowe testy wymagające PyObjC, sounddevice, catap lub pywhispercpp muszą albo dopisać paczki do kroku "Install test dependencies" w `macos.yml`, albo używać atrap (pominięcie importorskip czerwieni job macOS).
- Backendy: `create()` bez argumentów, bez wołania akcesorów `platforms.*` przy imporcie (deadlock pod `_cache_lock`). `test_platforms` wymaga, by nazwa modułu = nazwa akcesora.
- Smoke na runnerze bez mikrofonu nie może logować ERROR (brak TCC/urządzeń: WARNING i powrót do null).
- PortAudio zapamiętuje listę urządzeń przy pierwszym imporcie sounddevice: mikrofon podłączony po starcie (AirPods) nie pojawi się, a `Recorder._restart_streams` może otworzyć nieaktualny indeks. `sd._terminate()/_initialize()` unieważnia strumienie. Rozwiązać w M7b albo ręcznie w p. 7b.
- `request_permission('microphone')` bez `NSMicrophoneUsageDescription` w Info.plist zabija TCC: M10/M11 dodać klucz, zanim M9 wywoła tę funkcję.
- M10: plik `design/icons/FluentSystemIcons-Ucho.ttf` musi być w bundlu (`icon_font()` zwraca None bez niego).

**Nowe kroki do 7b/7c (na Macu)**
- Kryteria tylko-macOS: `plutil -lint` na plist z `MacShell(home=tmp).autostart_set(True)`; `MacSystemInfo` (RAM > 1, CPU zawiera Apple, arm64, GPU Metal); `hardware.recommend` na darwin daje klucz whisper.cpp large-v3[-turbo]-q5_0 i METAL; `Fonts.resolve()` w prawdziwym Tk (sans `.AppleSystemUIFont`, mono Menlo); MacAudio: `list_devices()` bez wyjątku, open_input/start/stop/close na domyślnym urządzeniu, `permission('microphone')` zwraca wartość Permission.
- Na ucho: czy `NSSound.soundNamed_` nie gubi szybko następujących po sobie dźwięków start/stop/cancel.
- Podłączenie i odłączenie słuchawek w trakcie pracy.
- Benchmark WER whisper.cpp (ggml-large/turbo, Metal) vs OpenVINO: `tools/benchmark_wer.py --backend whispercpp` na Macu Fryderyka. CI (base-q5_1) sprawdza tylko działanie potoku (PL słowa kluczowe 4/5, EN 5/5, Metal MTL0).

## Postęp: fala 2

Baza: 00afdcf. Scalone do `macos/dev` (`--no-ff`): M1b, M1c, M8. Wynik integracji: pytest 675 passed, 6 skipped; ruff czysto; `set_copyright --check` 0 plików; `--smoke-test 3` OK. Konflikty: `tests/test_platforms.py` (zbiory wyjątków M1b i M1c, oba teraz puste) i `ucho/locale/pl.json` (wpisy M1c i M8 zachowane). `downloads.py` i `settings_screen.py` scaliły się bez konfliktów.

**Zrobione**
- M1b: skróty, jedna instancja i powłoka za `ucho/platforms/` (`windows/hotkeys.py`, `windows/instance.py`, `windows/shell.py`); `ucho/hotkeys.py` i `ucho/instance.py` to fasady; `ucho/winshell.py` zostaje reeksportem dla `install.ps1`. `diarize` buduje polecenie przez `shell().worker_command`.
- M1c: audio (`windows/audio.py`), informacje o sprzęcie (`windows/sysinfo.py`), czcionki i ikony (`windows/fonts.py`); `audio`, `hardware`, `i18n`, `theme`, `ui` delegują do backendów. Test AST zakazanych importów działa już bez wyjątków.
- M8: `ucho/whispercpp.py` (Engine na pywhispercpp, Metal), `config.split_model_id`, `config.model_supported`, 4 modele ggml w `MODELS` (platforma `darwin`, tiny i base ukryte), `REVISIONS['ggerganov/whisper.cpp']`, trasowanie w `transcribe`/`downloads`, `--backend whispercpp` w `tools/benchmark_wer.py`.

**Odchylenia od planu**
- Protokół `base.Shell` różni się od p. 3.3: `work_area()` zwraca `(szer., wys.)`; `after_start(app_id, tasks)` przyjmuje trójki `(argument, title, description)` i działa na wątku `taskbar`; `describe_window` i `set_window_icon` mają parametr `dictation`; doszło `autostart_sync(enabled)`; `ProgressBar.set(state, value: int | None)` (stany none, normal, indeterminate, paused, error; `error` oznacza nagrywanie); `SingleInstance.listen(on_command)`. `MUTEX_NAME` i `COMMANDS` zostają w `ucho/instance.py` (test_package czyta `AppMutex` z `installer/ucho.iss`).
- `AudioBackend.open_input(device, channels, rate, frames, on_data, host=None)`; nie startuje strumienia; `on_data` dostaje surowe bytes int16 z przeplotem. `audio.new_pyaudio()` zwraca `None` poza Windows. `Recorder` ma flagę `_running`, a `_restart_streams` działa przy `_pa=None`.
- `theme.SANS/DISPLAY/MONO/...` i `ui.GLYPHS` liczone raz przy imporcie z `platforms.fonts()`; macOS `Fonts.glyphs()` musi mieć te same 19 kluczy co `windows/fonts.GLYPHS`.
- M1b nie ruszał `downloads.py` (z #28 nie ma procesu roboczego). M8 nie zmienił `transcribe.available_devices()`.
- Opisy commitów M1b i M8 zawierały atrybucję narzędzi; w M1b poprawiono (e7a0c60), w M8 (fab67e7) i M1c (8b38d6f) zostaje w historii; przy ewentualnym squashu do `main` pominąć.
- Kryterium 6 z M8 (job `macos-asr`, benchmark WER q5_0 vs OpenVINO int8) przesunięte do fali 3.
- Kryterium 2 (grep `windll`/`wintypes` w `ucho/*.py`) nadal ma trafienia w `diagnostics.py` (importy w funkcjach, osłonięte `sys.platform`); test AST na nie nie reaguje.

**Uwagi dla fali 3 (M3b, M4, M7a)**
- M3b: testy `test_audio_devices.py` dla `dictation.Microphone` i `test_recorder_start_releases_started_tracks...` zakładają backend Windows z atrapą PyAudio; na macOS dać `skipif(sys.platform != "win32")` albo `override('windows')`. Testy z `importorskip` (customtkinter, soxr) pomijają się w jobie `test`. Job `macos-asr`: modele tiny/base przez `transcribe.model_path(key, download=True)`, format logu Metal sprawdzić na prawdziwej bibliotece (wykrywanie po linii `using MTL0 backend`). `tests/test_dictation_delayed_clipboard.py` i `test_live.py` bywają niestabilne pod obciążeniem.
- M4: zaimplementować `base.Shell` z powyższymi sygnaturami (test kontraktu sprawdza nazwy parametrów i wartości domyślne); `SystemInfo` może zwracać dodatkowe klucze, ale nigdy wyjątek; reguły `hardware.recommend` dla darwin mogą używać kluczy whisper.cpp (muszą być w `config.MODELS`). `worker_command` dla `.app` (M10): `[sys.executable, '--worker', module, *args]`.
- M5: `platforms.instance()` woła fabrykę bez argumentów (macOS `create` ma przyjmować opcjonalne `name`); `label` w `ucho/hotkeys.py` zmienić na symbole, mapę VK na kVK dodać w `macos/hotkeys.py`. `NullHotkeys.start` loguje WARNING.
- M7a: backend dokładnie z sygnaturą `open_input` jak wyżej; `Recorder.start` bez źródła systemowego ostrzega tekstem „No system audio source found”.
- M9: `screens.py` i `dictation.py` skracają nazwę modelu przez `split('/')[-1]`, więc klucze `repo:plik` pokażą się brzydko; użyć `downloads.model_name` albo `settings_screen.short_model`. UI może oferować urządzenia OpenVINO dla whisper.cpp (silnik je ignoruje).
- M10: log `whispercpp.log` (obok `LOG_FILE`) traktować jak zwykły log przy paczce diagnostycznej; wpis pl.json i `THIRD_PARTY.md` (whisper.cpp MIT, modele ggml) dopisuje M13.
- Pułapka platforms: moduły importowane przez accessory pod blokadą `_cache_lock` nie mogą przy imporcie wołać accessorów `platforms.*` (zakleszczenie).
- Kroki ręczne do 7c dopisane: wiersz 7 (M1c), 8 (M8) i 9 (szczegóły M1b do kroku 5).

## Postęp: fala 1

Baza: 5c95b97 (main + #27, #28, #29, plan). Scalone do `macos/dev`: M1a, M2, M3a (M3b nie było w fali 1).

**Zrobione**
- M0a i M0b: spełnione już przez #28 (`ucho/downloads.py`: `Downloads`, `ByteCounter`, `speech_readiness`, `fallback_speech_model`, `recording_blocked`; brama w `app.py`, `meeting.py`, `dictation.py`; brak `download_ui.py`). Odwołania do M0a/M0b w planie czytaj jako te nazwy.
- M1a: `ucho/platforms/` (`base.py`, `null.py`, loader `_load`, accessory singletonowe), kod WinAPI dyktowania przeniesiony 1:1 do `platforms/windows/` (`winapi`, `clipboard`, `inserter`, `sounds`), `dictation.py` jako cienkie delegatory. Testy `tests/test_platforms.py` (w tym test AST zakazanych importów).
- M2: `ucho/paths.py` (czysty stdlib), `config.DATA_DIR`; `PROJECT_DIR` to teraz kod/zasoby, nie dane. Na Windows wartości bez zmian (jest na to test).
- M3a: spike CI na macos-15 i macos-26, wyniki w `docs/macos-spike.md`. Tymczasowy workflow spike'u usunięty z `macos/dev`; `tools/macos_spike/` zostaje jako materiał dla M10 (np. `macho_strip_signature`).
- Decyzje: D1 minimalny macOS 15, D2 podpis ad-hoc, D3 nieaktualne.

**Odchylenia od planu**
- `base.py`: `DelayedOffer` ma `confirmed` i `offer()`; `Clipboard.offer_delayed` zwraca ofertę jeszcze niewystawioną; `TextInserter.wait_keys_released` ma `limit_s=1.5`; `process_of`, `is_ours`, `is_shell` przyjmują też `None`. Implementacje macOS muszą trzymać ten kształt (test kontraktu).
- M1a sparametryzował też komunikat `NOT_TAKEN` (pl.json). Zdjęto skipify „uses WinAPI” także w `test_downloads.py`; `test_i18n` skanuje `ucho/**` (rglob).
- M2 nie dodał `lock_dir()` ani `recordings_dir()` z p. 3 (M5 i ewentualna zmiana domyślnego katalogu nagrań muszą je dodać).
- Konflikt reguł: AGENTS.md zakazuje `Co-Authored-By`, zlecenia fali 1 je dodają. Do ujednolicenia przy scalaniu do main.

**Uwagi dla fali 2**
- Loader: kto tworzy `ucho/platforms/macos/`, musi dodać `__init__.py`; do tego czasu na macOS działa `null`. `create()` ma być lekkie (accessory trzymają wspólną blokadę podczas importu i `create()`). `windows/instance.create` i `windows/hotkeys.create` mają przyjmować argumenty dzisiejszych `SingleInstance` i `Hotkeys`.
- M1b usuwa z listy wyjątków testu AST `hotkeys.py`, `instance.py`, `winshell.py`; M1c usuwa `audio.py`. Test importu pod fałszywym `sys.platform` wymaga wcześniejszego importu numpy, soundfile (i customtkinter).
- M3b: `tests/test_config.py::test_onedrive_folder` potrzebuje `skipif(sys.platform != "win32")`; test `test_live` bywa flaky pod obciążeniem. `test_dictation_delayed_clipboard.py` zostaje tylko dla Windows.
- M9: teksty „synced by OneDrive” (settings_screen, wizard) uogólnić na folder synchronizowany z chmurą; na macOS ostrzeżenie pokaże się też dla ~/Documents i ~/Desktop; stare ~/OneDrive - Firma nie jest wykrywane. `diagnostics.py` „App folder” w paczce .app lepiej z `DATA_DIR`.
- Wyniki spike'u (szczegóły w `docs/macos-spike.md`): pasek menu zostaje pystray (ikona po oknie Tk, `run_detached`, ręczne `removeStatusItem_` w `stop()`); runner ma TCC przyznane i Metal; Carbon `RegisterEventHotKey` z flagą exclusive działa bez uprawnień; sounddevice daje ciszę na runnerze; catap `session.start()` blokuje się na CI, więc start/stop tapu w osobnym wątku z limitem ok. 10 s (prawdziwy test dopiero na Macu, p. 7b-10); pierwsze ładowanie modelu whisper.cpp ok. 20 s; przed PyInstallerem zdjąć podpis Intela z 4 bibliotek openvino/libs; `collect_all('openvino')` z wykluczeniami i `collect_dynamic_libs('catap')`.
- Sandbox agy nie widzi interpretera `.venv-dev` spoza worktree, więc wykonawca nie uruchomi testów; uruchamia je lider.
- CI (`ci.yml`) działa tylko na push do main i na pull requesty, więc push `macos/dev` nie uruchomił żadnego przebiegu. Wynik CI da dopiero PR.
