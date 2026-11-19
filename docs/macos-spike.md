# Spike CI macOS (M3a): wyniki i decyzje

Stan: 2026-11-19, gałąź `macos/m3a` (baza `5c95b97` z `macos/dev`), issue #22. Zadanie M3a z `docs/macos-plan.md`.

Spike to tymczasowy workflow `.github/workflows/macos-spike.yml` i skrypty `tools/macos_spike/` (do usunięcia, gdy M3b doda `macos.yml`). Każda próba działa w osobnym procesie z limitem czasu, więc crash albo zawieszenie jest wynikiem, a nie końcem przebiegu. Wyniki szczegółowe (JSON na próbę, logi, zrzuty ekranu, syntetyczne WAV z `say`) są w artefaktach `spike-macos-15` i `spike-macos-26` przebiegów Actions na tej gałęzi. Powtórzenie: `gh workflow run macos-spike.yml --ref macos/m3a` (albo push na tę gałąź). Liczby poniżej pochodzą z 6 przebiegów (runy 44602465046, 44604336958, 44605955787, 44607193643, 44608511074, 44610531762); były powtarzalne na obu runnerach, różnice podaję jako zakres.

## Środowisko runnerów

| | `macos-15` | `macos-26` |
|---|---|---|
| System | macOS 15.8.2 | macOS 26.7.2 (25G332) |
| Sprzęt | Apple M1 (Virtual), arm64, 3 rdzenie, 7 GB RAM | to samo |
| Sesja GUI | Aqua, zalogowany użytkownik `runner` | to samo |
| GPU | „Apple Paravirtual device”, Metal działa (whisper.cpp wybiera backend `MTL0`) | to samo |
| Python | 3.12 z `actions/setup-python` (build python.org, Tk 8.6.16) | to samo |
| Dźwięk | „Apple Virtual Sound Device” (wejście i wyjście), „Null Audio Device”; wejście daje same zera | to samo |
| Uprawnienia TCC | Mikrofon `authorized`, Dostępność (`AXIsProcessTrusted`) `True`, nagrywanie ekranu i `CGEventPost` dozwolone (obraz runnera wpisuje je do TCC.db); dźwięk systemowy (`kTCCServiceAudioCapture`) „nieustalony” | to samo |

Poprawka planu: p. 6 planu zakłada, że runner nie ma Metal. Ma: whisper.cpp ładuje model na `MTL0` (urządzenie paravirtual), więc job `macos-asr` sprawdzi ścieżkę Metal, choć czasy nie odpowiadają prawdziwemu Macowi.

## Wyniki punkt po punkcie

| # | Pytanie | Wynik |
|---|---|---|
| 1 | Koła cp312 arm64 | **Tak.** `pip install --only-binary=:all:` z wersjami z planu (`openvino` 2026.4.1, `openvino-genai`/`-tokenizers` 2026.4.1.0, `huggingface_hub` 1.30.0, `sounddevice` 0.5.6, `pywhispercpp` 1.5.1, `catap` 0.6.1, `pyobjc-framework-{Cocoa,Quartz,ApplicationServices,AVFoundation,CoreAudio}` 13.0, `sherpa-onnx`/`-core` 2.0.1, `soxr` 1.1.0, `soundfile` 0.14.0, `numpy` 2.5.4, `customtkinter` 6.0.0, `pillow` 13.0.0, `pystray` 0.19.5, `pyinstaller` 6.22.3, `pyinstaller-hooks-contrib` 2026.10): 50 dystrybucji, zero sdistów, 13-15 s, ok. 345 MB w `site-packages` (OpenVINO 144 MB, `sherpa-onnx-core` 32 MB, numpy 31 MB, PyObjC razem 33 MB, `pywhispercpp` 12,5 MB, `openvino-genai` 11 MB, `hf-xet` 7 MB). Wszystkie importy działają (najwolniejszy `catap` 0,6 s). OpenVINO widzi tylko `CPU` („Apple M1 (Virtual)”); `openvino_genai` ma `LLMPipeline`, `WhisperPipeline`, `StructuredOutputConfig`, `StreamingStatus` |
| 2 | Tk i customtkinter | **Tak.** Tk 8.6.16, `windowingsystem = aqua`, okno zmapowane, pętla `mainloop` działa (`after` co 100 ms). customtkinter 6.0.0 rysuje okno, motyw systemu `Light`. Czcionki: `TkDefaultFont` to `.AppleSystemUIFont` 13 pt (działa po nazwie, choć nie ma jej w `font.families()`); w `families()` są „Helvetica Neue”, „Helvetica”, „Menlo”, „Monaco”; **nie ma** „SF Pro”, „SF Pro Text”, „SF Mono” |
| 3 | pystray obok Tk | **Tak, z dwiema poprawkami.** `Icon(...).run_detached()` wywołane po utworzeniu okna Tk używa `NSApp` Tk (`TKApplication`), ikona pojawia się w pasku menu (zrzut w artefakcie), akcja menu (`performActionForItemAtIndex_`) wywołuje callback na wątku głównym, zmiana ikony, tytułu i `update_menu()` z wątku Tk działają, pętla Tk nie jest blokowana (ok. 83-86 ticków na 10 s, jak bez ikony). Problemy: (a) `stop()` w trybie detached nie usuwa ikony (`isVisible` dalej `True`), bo pystray zdejmuje `NSStatusItem` tylko w `_run`; (b) domyślny `run_detached()` ustawia `visible = True` z wątku pomocniczego (AppKit poza wątkiem głównym; w CI bez crasha, ale to niezdefiniowane zachowanie). `icon.run()` w osobnym wątku: crash procesu (SIGTRAP/SIGABRT w 0,5-1,3 s) albo zawieszenie wątku; nigdy tak. Własny `NSStatusItem` przez PyObjC na wątku Tk działa tak samo dobrze (plan B gotowy, ok. 60 linii) |
| 4 | Skrót Carbon | **Tak.** `RegisterEventHotKey` przez ctypes zwraca `noErr` dla ⌃⌥R i ⌃⇧Space (domyślne skróty Ucho) oraz Esc; `UnregisterEventHotKey` `noErr`. Drugi raz ten sam skrót w tym samym procesie: `-9878 eventHotKeyExistsErr` (z `kEventHotKeyExclusive` i bez). Skrót trzymany przez **inny proces** z `kEventHotKeyExclusive`: nasza rejestracja z tą opcją daje `-9878`, bez niej `noErr` (kolizja niewidoczna). ⌘Space (Spotlight) i ⌃Space (źródło wprowadzania) dają `noErr`: Carbon **nie zgłasza** kolizji ze skrótami systemowymi. Zdarzenie `kEventHotKeyPressed` dochodzi do handlera zainstalowanego na `GetApplicationEventTarget()` na wątku głównym w pętli Tk (syntetyczne ⌃⌥R przez `CGEventPost`, zarówno `kCGHIDEventTap`, jak i `kCGSessionEventTap`) |
| 5 | `say` do WAV 16 kHz | **Tak.** 69 głosów, polski „Zosia” (`pl_PL`) jest na obu runnerach; angielski np. „Samantha”, „Rocko”. `say -v <głos> -o x.wav --file-format=WAVE --data-format=LEI16@16000 "<tekst>"` daje mono 16 kHz w 1,5-2 s |
| 6 | whisper.cpp na WAV z `say` | **Tak, Metal działa.** `hf_hub_download("ggerganov/whisper.cpp", ..., revision="5359861c739e955e79d9a303bcbc70fb988958b1")`: `ggml-tiny-q5_1.bin` 30,7 MB, `ggml-base-q5_1.bin` 56,9 MB, po 1-2 s. Pierwsze załadowanie modelu w nowym procesie trwa ok. 20 s (inicjalizacja Metal), kolejne w tym samym procesie 0,1-0,3 s. Zdanie EN 6 s: base 1,7-3,5 s, 5/5 słów kluczowych (także tiny); zdanie PL 5,3 s: base 5,1-5,2 s, 4/5 („Spodka nie” zamiast „Spotkanie”). `auto_detect_language` zwraca krotkę `((lang, prob), {lang: prob})` (zgodnie z planem): EN 0,91, PL 0,99 dla base. Segmenty `t0`/`t1` w jednostkach 10 ms |
| 7 | Process Tap (catap) bez uprawnień | **Nie da się sprawdzić danych w CI.** `import catap`, `list_audio_devices()`, `list_audio_processes()`, `find_process_by_pid(os.getpid())` (proces `org.python.python`) i `RecordingSession.from_system_audio(...)` działają; `CATapDescription` i `AudioHardwareCreateProcessTap` są w systemie. Natomiast `session.start()` **blokuje się bez końca** w `AudioDeviceCreateIOProcID` (stos z `faulthandler`: `catap/_capture_engine.py` `open_tap_capture`), bez wyjątku i bez błędu uprawnień. Tak samo: z wpisem `kTCCServiceAudioCapture` w TCC.db (preflight TCC zmienia się z 2 „nieustalone” na 0 „przyznane”), z zamrożonej `.app` z `NSAudioCaptureUsageDescription` uruchomionej wprost i przez `open` (LaunchServices), także po wpisie TCC dla jej bundle id. Wynik eksperymentu z pętlą zdarzeń na wątku głównym i z tapem jednego procesu: sekcja „Process Tap: ostatni eksperyment” |
| 8 | PyInstaller z pywhispercpp | **Tak.** Opis niżej |

## PyInstaller

**Lekka paczka (Tk + pywhispercpp, `--windowed`, onedir z `BUNDLE`):** build 18-38 s bez żadnych hooków i opcji; `.app` 44-46 MB; biblioteki `libwhisper`/`libggml*` z katalogu głównego `site-packages` trafiają do `Contents/Frameworks` same (PyInstaller idzie po zależnościach Mach-O), także `libggml-metal`; podpis ad-hoc PyInstallera przechodzi `codesign --verify --deep --strict`; zamrożony proces ładuje model na Metal i transkrybuje (5/5 słów); Tk 8.6.16 działa w paczce. `spctl -a -t exec` daje `rejected` (oczekiwane przy ad-hoc). `hdiutil create -format ULFO`: 19-21 MB, za pierwszym razem, 5-18 s.

**Pełna paczka (to, czego potrzebuje Ucho: + OpenVINO, `openvino_genai`, catap, sounddevice, soundfile, pystray):**
- **Blokada: podpis bibliotek Intela.** Cztery biblioteki z `openvino/libs` (`libtbb.12.dylib`, `libtbbmalloc.2.dylib`, `libtbbbind_2_5.3.dylib`, `libhwloc.dylib`, podpisane Developer ID Intel Corporation) mają 16 bajtów za blokiem podpisu. `codesign` kończy się na nich „internal error in Code Signing subsystem” przy podpisie i nawet przy `--remove-signature`, więc PyInstaller przerywa build. Pozostałe 88 plików natywnych z badanych pakietów podpisuje się bez problemu.
- **Obejście (sprawdzone):** przed buildem usunąć `LC_CODE_SIGNATURE` i obciąć plik do początku podpisu (poprawiając `filesize` segmentu `__LINKEDIT`); to ok. 40 linii Pythona (`macho_strip_signature` w `tools/macos_spike/spike.py`). Kopia bez podpisu podpisuje się ad-hoc i ładuje (`ctypes.CDLL`); paczka buduje się w 24-63 s, przechodzi `codesign --verify --deep --strict` i działa.
- **OpenVINO bez urządzeń:** domyślnie zamrożona aplikacja widzi `available_devices == []` (wtyczki `libopenvino_*_plugin.so` nie trafiają do paczki). Z `--collect-all openvino` jest `['CPU']`, a `openvino_genai` 2026.4.1.0 ma `LLMPipeline`.
- **catap bez natywnej biblioteki:** `catap/native/libcatap_coreaudio.dylib` nie trafia do paczki sama (catap ładuje ją przez ścieżkę, nie przez linkowanie). Z `--collect-binaries catap` jest.
- sounddevice (PortAudio V19.7.0), soundfile (libsndfile 1.2.2) i backend `pystray._darwin` działają w paczce bez opcji.
- Rozmiar: `.app` 101 MB bez `--collect-all openvino`, 213-214 MB z nim (`--collect-all` dokłada nagłówki `include/` i frontendy JAX, PyTorch, TensorFlow; do przycięcia w specu). Biblioteki whisper.cpp leżą w paczce dwa razy (`Contents/Frameworks/` i `Contents/Frameworks/pywhispercpp/__dot__dylibs/`), co dokłada kilka MB.
- Zamrożony proces ładuje model whisper.cpp 12-25 s (pierwsza inicjalizacja Metal w nowym procesie), transkrypcja zdania 2,6-5 s.

## Process Tap: ostatni eksperyment

Hipoteza „Core Audio potrzebuje pętli zdarzeń na wątku głównym” odpada: (A) `start()` w wątku roboczym, gdy wątek główny kręci `NSRunLoop` (jak Tk), też wisi po 25 s; (C) `start()` na wątku głównym wisi w tym samym miejscu (`AudioDeviceCreateIOProcID`) i proces trzeba zabić. (B) Tapu jednego procesu nie udało się sprawdzić: `afplay` nie pojawił się jako nazwany proces audio (`list_audio_processes()` na runnerze zwraca głównie „Unknown”). Preflight TCC w tej próbie był 0 (przyznane, po wpisie do TCC.db).

Wniosek: na runnerach GitHub (maszyna wirtualna z paravirtual audio) Process Tap nie startuje w żadnym wariancie, więc prawdziwy dźwięk z tapu sprawdzi dopiero Mac (p. 7b-10 planu), a CI sprawdza tylko, że zawieszony start nie blokuje aplikacji. Czy na prawdziwym Macu start bez zgody też wisi (zamiast dawać ciszę, jak zakłada plan), jest nieznane; kod M7b musi działać w obu przypadkach.

## Decyzje dla dalszych zadań

**Pasek menu (ryzyko 8.1): zostaje pystray, bez własnego `NSStatusItem` na betę.** pystray działa obok Tk w jednym procesie bez blokady wątku głównego, pod warunkiem dwóch drobnych zmian w `ucho/tray.py` na macOS (wchodzą do M10 albo do zadania, które pierwsze uruchomi tray na Macu): `run_detached(setup=...)` z pustym `setup` i ustawienie `visible = True` przez `app.after` na wątku Tk; w `stop()` dodatkowo `NSStatusBar.systemStatusBar().removeStatusItem_(icon._status_item)`. Ikona musi powstać po utworzeniu okna Tk (wtedy `NSApp` to `TKApplication`). Nigdy `icon.run()` w wątku. Własny `NSStatusItem` (sprawdzony w spike'u) zostaje planem B, gdyby `macos-smoke` lub prawdziwy Mac pokazał problem.

**M3b (CI macOS):**
- `macos-15` i `macos-26` działają, Python 3.12 z `actions/setup-python@v7` ma Tk; wystarczy `pip install` bez kompilacji (wszystko z kół). Instalacja pełnego `requirements.txt` z markerami zajmie ok. 15 s.
- Runner ma sesję Aqua, więc `macos-smoke` (`python -m ucho --smoke-test 3`) może otwierać prawdziwe okna; `screencapture -x` działa do zrzutów.
- Runner ma przyznane Mikrofon, Dostępność, nagrywanie ekranu i wysyłanie zdarzeń: testy „tylko macOS” nie sprawdzą ścieżek „brak uprawnienia” (muszą używać atrap), za to mogą sprawdzić prawdziwe wklejanie (M6 kryterium 4) i skróty.
- Wersje do markerów `sys_platform == "darwin"`: dokładnie te z punktu 1 (wszystkie się instalują).

**M4 (czcionki):** sans: `.AppleSystemUIFont` (to też `TkDefaultFont`), zapas „Helvetica Neue”; mono: „Menlo” (nie „SF Mono”, Tk go nie widzi); „SF Pro” w Tk nie istnieje, nie wpisywać jej jako pierwszej rodziny.

**M5 (skróty):** Carbon `RegisterEventHotKey` z `kEventHotKeyExclusive` i handlerem na `GetApplicationEventTarget()`; zdarzenia przychodzą na wątku Tk, bez uprawnień i bez osobnej pętli. Kryterium 4 M5 zostaje w wersji „między procesami”, ale tylko wobec procesu, który też rejestruje z `kEventHotKeyExclusive` (np. druga kopia Ucho). Skrótów systemowych Carbon nie odrzuca (⌘Space, ⌃Space dają `noErr`), więc potrzebna jest lista znanych kolizji w `hotkeys.label`/Ustawieniach (co najmniej ⌘Space, ⌃Space, ⌃⌘Space, ⌘Tab); domyślne ⌃⌥R i ⌃⇧Space nie kolidują na czystym systemie. Kryterium 5 (syntetyczne zdarzenie przez `CGEventPost` wywołuje akcję) przechodzi na runnerze, może być zwykłym testem zamiast `continue-on-error`.

**M7a (mikrofon):** `sounddevice.RawInputStream(dtype="int16")` otwiera się na runnerze („Apple Virtual Sound Device”, 48 kHz) i oddaje bloki z zerami; testy „tylko macOS” mogą otwierać strumień, ale nie oczekiwać sygnału.

**M7b (dźwięk systemowy, ryzyko 8.2):**
- `session.start()` catap może zablokować wątek na zawsze (w CI zawsze tak robił, w `AudioDeviceCreateIOProcID`), więc w Ucho start i stop tapu muszą działać w osobnym wątku z limitem czasu (np. 10 s); po przekroczeniu nagranie idzie dalej z samym mikrofonem i komunikatem, a wątek zostaje porzucony (nie da się go przerwać). Nigdy na wątku Tk.
- Stan uprawnienia: publiczne API go nie podaje. Prywatne `TCCAccessPreflight(kTCCServiceAudioCapture)` działa (0/2 na runnerze), ale to prywatne SPI: najwyżej jako podpowiedź w kreatorze, z obsługą braku symbolu; decyzja o „granted” nadal z próbnego tapu (dane niezerowe), jak w planie.
- Prawdziwy test danych z tapu jest tylko na Macu Fryderyka (p. 7b-10); w CI zostaje test „start nie blokuje UI” i `SilenceWatch`.
- Pętla zdarzeń na wątku głównym nie odblokowuje startu (sekcja wyżej), więc nie ma prostego obejścia po stronie Ucho; plan B z własnym kodem na `pyobjc-framework-CoreAudio` wywołuje to samo `AudioDeviceCreateIOProcID` i na runnerze zachowa się tak samo. Wybór catap czy własny kod rozstrzyga dopiero test na Macu.
- Paczka (M10) musi zawierać `catap/native/libcatap_coreaudio.dylib` i `NSAudioCaptureUsageDescription`; oba sprawdzone w spike'u.

**M8 (whisper.cpp):** API z planu potwierdzone (`auto_detect_language` zwraca krotkę, `t0`/`t1` w 10 ms, `redirect_whispercpp_logs_to=<ścieżka>` działa). Na runnerze jest Metal, więc `device` = `METAL` także w CI (fallback na CPU sprawdzić atrapą). Pierwsze załadowanie modelu w procesie trwa ok. 20 s, więc silnik musi ładować model raz i trzymać go (już tak działa `engine.py`), a UI pokazywać stan „ładowanie modelu”. Ukryte modele CI `ggml-tiny-q5_1.bin` i `ggml-base-q5_1.bin` z rewizją `5359861c739e955e79d9a303bcbc70fb988958b1` pobierają się przez `huggingface_hub`; base daje 5/5 słów EN i 4/5 PL na `say`, tiny wystarcza do testu „działa”.

**M10 (paczka):**
- PyInstaller 6.22.3 + hooks-contrib 2026.10 nadaje się; podpis ad-hoc robi sam PyInstaller, `codesign --verify --deep --strict` przechodzi.
- Krok obowiązkowy przed buildem: zdjąć podpis Intela z bibliotek OpenVINO, które odrzuca `codesign` (dziś 4 pliki z `openvino/libs`), na **kopii** `site-packages` w katalogu builda (nie w środowisku użytkownika), funkcją typu `macho_strip_signature`; build ma sprawdzać wszystkie `*.dylib`/`*.so` próbnym `codesign` i padać z listą, gdy pojawi się nowy odrzucony plik.
- W specu: `collect_all("openvino")` (bez tego brak urządzenia CPU) z wykluczeniem `openvino/include` i frontendów `jax`, `pytorch`, `tensorflow`, `paddle` (Ucho czyta tylko IR); `collect_dynamic_libs("catap")` (natywna biblioteka tapu); sprawdzić `openvino_genai` i `openvino_tokenizers` tak samo (w spike'u `openvino_genai` importował się i miał `LLMPipeline`, ale LLM nie był ładowany).
- Rozmiary orientacyjne: `.app` z OpenVINO ok. 100 MB po przycięciu (213 MB z pełnym `--collect-all`), DMG ULFO ok. 45 % rozmiaru `.app` (lekka paczka: 46 MB → 21 MB); próg 200 MB dla DMG z planu jest bezpieczny. `hdiutil create` przeszedł za pierwszym razem we wszystkich przebiegach (ponawianie i tak zostaje).
- Info.plist: `plutil -insert` po buildzie i ponowny `codesign --force --deep -s -` działają (sprawdzone z `NSAudioCaptureUsageDescription` i `NSMicrophoneUsageDescription`); lepiej jednak wpisać klucze przez `info_plist` w `BUNDLE`.
- Pierwsze uruchomienie paczki ładuje model 12-25 s (Metal), co trzeba uwzględnić w limicie czasu smoke testu z modelem (M11).

**D1, D2:** spike nie zmienia decyzji (macOS 15+, ad-hoc). Oba runnery to macOS 15 i 26, więc minimalna wersja 15 jest pokryta testami.
