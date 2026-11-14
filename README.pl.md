# Ucho

**Dokładna transkrypcja spotkań i dyktowania po polsku. Na Twoim laptopie, nic z niego nie wychodzi.**

Ucho nagrywa rozmowy w Teams, Zoomie czy Meet oraz dyktowanie na laptopie z Windowsem i zamienia je w tekst na miejscu, modelem Whisper large-v3 uruchomionym przez OpenVINO. Żaden bot nie dołącza do spotkania, nie trzeba zakładać konta, nic nie trafia do chmury. Powstało z myślą o polskim, radzi sobie też z angielskim i ze spotkaniami, w których oba języki się mieszają.

[English version: README.md](README.md) · [ucho.app](https://ucho.app)

> Stan: v0.1, tylko Windows, utrzymuje jedna osoba. Mogą zdarzać się niedoróbki.

## Co robi

- **Spotkania.** Nagrywa dwie osobne ścieżki: Twój mikrofon („Ja”) i dźwięk systemowy („Spotkanie”). Transkrypt jest podzielony na mówców bez modelu diaryzacji, bo źródła są rozdzielone od początku.
- **Dyktowanie.** *Ucho Dyktuj*: Ctrl+Shift+Spacja, mówisz, drugie wciśnięcie, a tekst ląduje tam, gdzie stoi kursor (terminal, przeglądarka, Teams).
- **Archiwum dla agentów AI.** Mały serwer MCP tylko do odczytu pozwala Claude Code, Claude Desktop, Cursorowi, Codexowi i innym klientom MCP przeglądać, przeszukiwać i czytać transkrypty. Ucho nie ma własnego czatu: streszcza Twój agent.
- **Opcjonalne podsumowania lokalne.** Domyślnie wyłączone. Po włączeniu lokalny model Qwen pisze krótkie podsumowanie, a mechaniczne filtry sprawdzają je z transkryptem, żeby nie pojawiły się imiona ani daty, które nie padły.

## Czym się różni

| | Ucho |
|---|---|
| Działa offline | Tak. Sieć jest potrzebna raz, do pobrania modeli z Hugging Face. Sprawdzone pomiarem połączeń TCP procesu. |
| Zwykłe laptopy | Dostrojone do Intel Core Ultra z grafiką Arc (OpenVINO na zintegrowanej karcie), na innych komputerach liczy na procesorze. |
| Polski i angielski | Język jest wykrywany co ok. 12 s i tylko spośród języków, których używasz, więc w polskim spotkaniu nie pojawiają się wtrącenia po koreańsku czy islandzku. |
| Zmyślenia Whispera | Cisza jest wycinana, zanim trafi do modelu, a znane formułki z napisów filmowych („Napisy stworzone przez…”) są odsiewane. |
| Twoje pliki | Zwykłe `transkrypt.md`, `.srt`, `.txt` i `meta.json` w osobnym folderze dla każdego spotkania. |

## Wydajność (Core Ultra 7 258V, Arc 140V)

| | GPU (Arc 140V) | CPU |
|---|---|---|
| Nagranie 85 s, język wymuszony | 5,7 s (15× szybciej niż czas nagrania) | 35 s (2,4×) |
| Nagranie 51 s, autodetekcja PL/EN | 3,7 s (14×) | 47 s (1,1×) |
| Pierwsze załadowanie modelu | ok. 21 s (kompilacja, potem ok. 2 s z pamięci podręcznej) | ok. 6 s |

Godzinne spotkanie transkrybuje się na karcie Arc w ok. 4 minuty.

## Wymagania

- Windows 10 lub 11, 64-bitowy.
- Zalecane 16 GB RAM (przy 8 GB działają mniejsze modele Turbo).
- Najlepiej Intel Core Ultra z grafiką Arc. Na innych procesorach x86 transkrypcja idzie na CPU, wolniej.
- Ok. 3 GB miejsca na program i domyślny model.

## Instalacja

1. Pobierz `Ucho-Windows.zip` z zakładki [Releases](../../releases) i rozpakuj, np. do `C:\Users\<Ty>\Ucho\app`. **Nie rozpakowuj do OneDrive** (Pulpit i Dokumenty często są synchronizowane), bo modele i logi trafiłyby do chmury.
2. Prawy przycisk na `instaluj.ps1` → *Uruchom w programie PowerShell* albo:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\instaluj.ps1
   ```
   Nie trzeba uprawnień administratora. Instalator doinstaluje Pythona 3.12 (przez winget, jeśli go nie ma), środowisko z przypiętymi wersjami pakietów, pobierze model Whisper (ok. 1,6 GB, w przypiętej wersji) i utworzy skróty.
3. Uruchom **Ucho** albo **Ucho Dyktuj** z Menu Start.

Opcjonalnie `-ZPodsumowaniem` pobiera też model podsumowań (ok. 5 GB) i włącza podsumowania.

Nagrania trafiają do `C:\Users\<Ty>\Ucho\nagrania`, celowo poza OneDrive. Katalog zmienisz w Ustawieniach.

## Użycie

- Wpisz nazwę spotkania i naciśnij **REC** albo użyj **Ctrl+Alt+R** w dowolnym programie (działa też, gdy okno jest schowane w zasobniku).
- **STOP** uruchamia transkrypcję. Możesz ją przerwać; gotowe fragmenty zostaną.
- **Używaj słuchawek.** Na głośnikach mikrofon łapie też rozmówców i wszystko jest w transkrypcie dwa razy.
- **Poinformuj uczestników o nagrywaniu.** Brak bota nie oznacza, że nagrywanie jest legalne. Zobacz [PRIVACY.md](PRIVACY.md).

Interfejs jest po polsku albo po angielsku (Ustawienia → Język interfejsu albo automatycznie według Windowsa).

## Podłączenie agenta AI (MCP)

Serwer MCP działa tylko do odczytu, korzysta wyłącznie z biblioteki standardowej Pythona, komunikuje się przez stdio i nie otwiera żadnych portów. Narzędzia: `list_meetings`, `get_transcript`, `get_summary`, `search_transcripts`.

Claude Code:

```powershell
claude mcp add ucho -- "C:\Users\<Ty>\Ucho\app\.venv\Scripts\python.exe" "C:\Users\<Ty>\Ucho\app\mcp_serwer.py"
```

Claude Desktop, Cursor i inne: dodaj serwer stdio z tym samym poleceniem i argumentem. Potem zapytaj np.: *„Co ustaliliśmy w zeszłym tygodniu z dostawcą stali?”*

Agent wysyła przeczytany fragment transkryptu do swojego dostawcy modelu (w przypadku Claude Code do Anthropic). Samo Ucho niczego nie wysyła; co agent zrobi z tekstem, zależy od Ciebie i jego ustawień.

## Prywatność

W skrócie: nagrania, transkrypty i ustawienia zostają na dysku. Telemetria Hugging Face i OpenVINO jest wyłączona. Szczegóły i polecenie, którym sprawdzisz to sam: [PRIVACY.md](PRIVACY.md).

## Ograniczenia (v0.1)

- Tylko Windows. macOS w v0.2.
- Wszyscy po drugiej stronie rozmowy mają etykietę „Spotkanie”; poszczególni rozmówcy zdalni nie są rozróżniani.
- Brak transkrypcji na żywo w trakcie nagrania.
- Aplikacja nie jest jeszcze podpisana cyfrowo, więc Windows może wyświetlić ostrzeżenie.

## Rozwój

```powershell
py -3.12 -m venv .venv-dev
.\.venv-dev\Scripts\python.exe -m pip install pytest ruff numpy soundfile
.\.venv-dev\Scripts\python.exe -m pytest
.\.venv-dev\Scripts\python.exe -m ruff check .
```

Opis architektury: [docs/architecture.md](docs/architecture.md). Jak pomóc: [CONTRIBUTING.md](CONTRIBUTING.md).

## Licencja

Apache-2.0, zobacz [LICENSE](LICENSE) i [NOTICE](NOTICE). Modele i biblioteki mają własne licencje: [THIRD_PARTY.md](THIRD_PARTY.md).

Autor: Fryderyk Pryjma.
