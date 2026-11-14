# Privacy / Prywatność

## English

**Ucho has no server, no account and no telemetry of its own.** It never uploads audio, transcripts, settings or logs.

### What uses the network, and when

| When | What | Where |
|---|---|---|
| Installation | Python 3.12 (only if missing, via `winget`) | Microsoft / python.org |
| Installation | Python packages pinned in `requirements.txt` | PyPI |
| Installation, or *Download model* in Settings | Model files, pinned to a fixed revision in `ucho/config.py` | Hugging Face (`huggingface.co`) |

Nothing else. Recording, transcription, optional summaries and the MCP server work offline.

### Telemetry switched off

- Hugging Face: the app sets `HF_HUB_DISABLE_TELEMETRY=1` before any import of `huggingface_hub`.
- OpenVINO: the `openvino-telemetry` package is installed together with OpenVINO but is only used by model-conversion tools, never by Ucho. The installer still records an opt-out (`opt_in_out --opt_out`).

### Where your data lives

| What | Where |
|---|---|
| Recordings (`microphone.flac`, `meeting.flac`), transcripts, `meta.json` | `C:\Users\<you>\Ucho\recordings` (changeable in Settings; v0.1 used `Ucho\nagrania`) |
| Settings | `settings.json` in the program folder (v0.1: `ustawienia.json` and `dyktuj.json`; earlier v0.2 builds: `dictate.json`. They are imported once and kept) |
| Logs | `ucho.log` in the program folder. **It contains the dictated text** (used to restore the last text after a restart), like `dictate.log` of earlier versions. Delete them whenever you like. |
| Models | `models\` in the program folder |

The default recordings folder is deliberately **not** in Documents: on many company laptops Documents are redirected to OneDrive, which would upload recordings to the cloud.

### How long it is kept

Ucho keeps a recording until you delete it. **Delete recording** on the transcript screen (or the Delete key on a recording selected in the list) asks for confirmation and then removes the whole session folder: audio, transcript and summary. The files are deleted directly, not moved to the Recycle Bin. Copies made outside Ucho (backups, a synced folder) are not affected.

**Delete audio after** in Settings (Never, 7, 30 or 90 days; default Never) removes only the audio files of recordings that already have a transcript, at start and then once a day. Transcripts, summaries and `meta.json` stay, and `meta.json` notes that the audio was removed. The log records only how many files were removed.

### Dictation and the clipboard

Paste mode puts the dictated text on the clipboard for a moment and presses Ctrl+V. The text is marked so that Windows keeps it out of clipboard history (Win+V) and cloud clipboard sync, and the text you had copied before is put back right after the paste (only text: an image or copied files on the clipboard are not restored). Text Ucho copies on request (*Copy all*, *Copy*) is marked the same way. If the window you dictated into is closed or does not come back to the front, nothing is pasted: the text stays on the clipboard for you to paste with Ctrl+V.

### Check it yourself

While Ucho is recording or transcribing, list external connections of its Python processes (no admin rights needed):

```powershell
Get-Process | Where-Object { $_.ProcessName -like "python*" } | ForEach-Object {
  Get-NetTCPConnection -OwningProcess $_.Id -ErrorAction SilentlyContinue |
    Where-Object { $_.RemoteAddress -notin @('0.0.0.0','::','127.0.0.1','::1') }
}
```

An empty result means no external connections. For a hard guarantee, block the interpreter in Windows Firewall (admin; model downloads stop working until you remove the rule):

```powershell
New-NetFirewallRule -DisplayName "Ucho - block" -Direction Outbound `
  -Program "C:\Users\<you>\Ucho\app\.venv\Scripts\pythonw.exe" -Action Block
# remove: Remove-NetFirewallRule -DisplayName "Ucho - block"
```

Last measurement by the author: full recording, two-track transcription on the GPU and a summary with zero external connections (Nagrywacz, the predecessor of Ucho, 2026). This will be repeated on the v0.1.0 release build and the date added here.

### MCP and AI agents

The MCP server only reads files and talks to the agent that started it over stdio. **The agent** (for example Claude Code) sends the transcript fragments it reads to its model provider. If a meeting must not leave your computer, do not ask a cloud agent about it.

### Recording law

No bot in the call does not make recording legal. In the EU, recording people without their knowledge can violate the GDPR and the terms of Teams, Zoom or Meet. **Tell participants that you are recording.** Suggested message:

> I'm recording this meeting locally on my laptop to make notes. The recording does not leave my computer. Let me know if you'd rather I didn't.

## Polski

**Ucho nie ma serwera, konta ani własnej telemetrii.** Nigdy nie wysyła nagrań, transkryptów, ustawień ani logów.

### Co i kiedy łączy się z siecią

| Kiedy | Co | Dokąd |
|---|---|---|
| Instalacja | Python 3.12 (tylko gdy go brakuje, przez `winget`) | Microsoft / python.org |
| Instalacja | Pakiety Pythona przypięte w `requirements.txt` | PyPI |
| Instalacja albo *Pobierz model* w Ustawieniach | Pliki modelu w przypiętej wersji z `ucho/config.py` | Hugging Face (`huggingface.co`) |

Nic więcej. Nagrywanie, transkrypcja, opcjonalne podsumowania i serwer MCP działają offline.

### Wyłączona telemetria

- Hugging Face: aplikacja ustawia `HF_HUB_DISABLE_TELEMETRY=1`, zanim cokolwiek zaimportuje `huggingface_hub`.
- OpenVINO: pakiet `openvino-telemetry` instaluje się razem z OpenVINO, ale korzystają z niego tylko narzędzia do konwersji modeli, nigdy Ucho. Instalator i tak zapisuje odmowę zgody (`opt_in_out --opt_out`).

### Gdzie są Twoje dane

| Co | Gdzie |
|---|---|
| Nagrania, transkrypty, `meta.json` | `C:\Users\<Ty>\Ucho\recordings` (do zmiany w Ustawieniach; w v0.1 `Ucho\nagrania`) |
| Ustawienia | `settings.json` w folderze programu (w v0.1 `ustawienia.json` i `dyktuj.json`, we wcześniejszych wersjach v0.2 `dictate.json`; są wczytywane raz i zostają na dysku) |
| Logi | `ucho.log` w folderze programu. **Zawiera podyktowany tekst** (służy do odtworzenia ostatniego tekstu po restarcie), tak jak `dictate.log` wcześniejszych wersji. Możesz je w każdej chwili usunąć. |
| Modele | `models\` w folderze programu |

Domyślny folder nagrań celowo **nie** leży w Dokumentach: na wielu firmowych laptopach Dokumenty są przekierowane do OneDrive, więc nagrania trafiłyby do chmury.

### Jak długo są przechowywane

Ucho przechowuje nagranie, dopóki go nie usuniesz. **Usuń nagranie** na ekranie transkryptu (albo klawisz Delete na nagraniu zaznaczonym na liście) po potwierdzeniu usuwa cały folder sesji: dźwięk, transkrypt i podsumowanie. Pliki są kasowane od razu, nie trafiają do Kosza. Kopie zrobione poza Ucho (kopie zapasowe, folder synchronizowany) zostają bez zmian.

**Usuwaj nagrania audio po** w Ustawieniach (Nigdy, 7, 30 albo 90 dniach; domyślnie Nigdy) usuwa przy starcie, a potem raz dziennie, tylko pliki audio nagrań, które mają już transkrypt. Transkrypty, podsumowania i `meta.json` zostają, a w `meta.json` zapisuje się informacja o usunięciu dźwięku. Log odnotowuje tylko liczbę usuniętych plików.

### Dyktowanie a schowek

W trybie wklejania podyktowany tekst na chwilę trafia do schowka, a Ucho naciska Ctrl+V. Tekst jest oznaczony tak, żeby Windows nie zapisał go w historii schowka (Win+V) ani nie zsynchronizował przez chmurę, a tekst skopiowany wcześniej wraca do schowka zaraz po wklejeniu (tylko tekst: obrazu ani skopiowanych plików Ucho nie przywraca). Tak samo oznaczony jest tekst kopiowany na żądanie (*Kopiuj całość*, *Kopiuj*). Jeśli okno, w którym dyktujesz, zostało zamknięte albo nie wraca na wierzch, Ucho niczego nie wkleja: tekst zostaje w schowku i możesz go wkleić Ctrl+V.

### Sprawdź sam

Polecenie i regułę zapory znajdziesz w części angielskiej powyżej; działają tak samo.

### MCP i agenci AI

Serwer MCP tylko czyta pliki i rozmawia przez stdio z agentem, który go uruchomił. **Agent** (np. Claude Code) wysyła przeczytane fragmenty transkryptu do swojego dostawcy modelu. Jeśli spotkanie nie może opuścić komputera, nie pytaj o nie agenta działającego w chmurze.

### Prawo do nagrywania

Brak bota nie oznacza, że nagrywanie jest legalne. W UE nagrywanie osób bez ich wiedzy może naruszać RODO oraz regulaminy Teams, Zooma czy Meet. **Poinformuj uczestników, że nagrywasz.** Przykładowa wiadomość:

> Nagrywam to spotkanie lokalnie na swoim laptopie, żeby zrobić notatki. Nagranie nie opuszcza mojego komputera. Daj znać, jeśli wolisz, żebym nie nagrywał.
