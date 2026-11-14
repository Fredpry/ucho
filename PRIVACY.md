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
| Recordings (`mikrofon.flac`, `spotkanie.flac`), transcripts, `meta.json` | `C:\Users\<you>\Ucho\nagrania` (changeable in Settings) |
| Settings | `ustawienia.json`, `dyktuj.json` in the program folder |
| Logs | `ucho.log`, `dyktuj.log` in the program folder. **`dyktuj.log` contains the dictated text** (it is used to restore the last text after a restart). Delete it whenever you like. |
| Models | `modele\` in the program folder |

The default recordings folder is deliberately **not** in Documents: on many company laptops Documents are redirected to OneDrive, which would upload recordings to the cloud.

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
| Nagrania, transkrypty, `meta.json` | `C:\Users\<Ty>\Ucho\nagrania` (do zmiany w Ustawieniach) |
| Ustawienia | `ustawienia.json`, `dyktuj.json` w folderze programu |
| Logi | `ucho.log`, `dyktuj.log` w folderze programu. **`dyktuj.log` zawiera podyktowany tekst** (służy do odtworzenia ostatniego tekstu po restarcie). Możesz go w każdej chwili usunąć. |
| Modele | `modele\` w folderze programu |

Domyślny folder nagrań celowo **nie** leży w Dokumentach: na wielu firmowych laptopach Dokumenty są przekierowane do OneDrive, więc nagrania trafiłyby do chmury.

### Sprawdź sam

Polecenie i regułę zapory znajdziesz w części angielskiej powyżej; działają tak samo.

### MCP i agenci AI

Serwer MCP tylko czyta pliki i rozmawia przez stdio z agentem, który go uruchomił. **Agent** (np. Claude Code) wysyła przeczytane fragmenty transkryptu do swojego dostawcy modelu. Jeśli spotkanie nie może opuścić komputera, nie pytaj o nie agenta działającego w chmurze.

### Prawo do nagrywania

Brak bota nie oznacza, że nagrywanie jest legalne. W UE nagrywanie osób bez ich wiedzy może naruszać RODO oraz regulaminy Teams, Zooma czy Meet. **Poinformuj uczestników, że nagrywasz.** Przykładowa wiadomość:

> Nagrywam to spotkanie lokalnie na swoim laptopie, żeby zrobić notatki. Nagranie nie opuszcza mojego komputera. Daj znać, jeśli wolisz, żebym nie nagrywał.
