<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="design/assets/ucho-hero-dark.svg">
    <img src="design/assets/ucho-hero-light.svg" alt="Ucho: private transcripts of your meetings and dictation, offline" width="880">
  </picture>
</p>

<p align="center"><b>Accurate meeting and dictation transcripts in your language. On your laptop. Nothing leaves it.</b></p>

<p align="center">
  <a href="https://ucho.app">ucho.app</a> · <a href="README.pl.md">Polski</a> · <a href="PRIVACY.md">Privacy</a> · <a href="design/README.md">Design system</a> · <a href="LICENSE">Apache-2.0</a>
</p>


Ucho (pronounced *oo-ho*, Polish for "ear") records your Teams, Zoom or Meet calls and your dictation on a Windows laptop and turns them into text locally, with Whisper large-v3 running on OpenVINO. No bot joins the call, no account, no cloud. It is built for Polish first, and handles English and meetings that mix both.

[Polska wersja: README.pl.md](README.pl.md) · [ucho.app](https://ucho.app)

> Status: v0.1, Windows only, maintained by one person. Expect rough edges.

## What it does

- **Meetings.** Records two separate tracks: your microphone ("Me") and system audio ("Meeting"). You get a transcript split by speaker without any diarization model, because the sources are already separate.
- **Dictation.** *Ucho Dyktuj* (Dictate): press Ctrl+Shift+Space, speak, press again, and the text is pasted where your cursor is (terminal, browser, Teams).
- **Archive for AI agents.** A small read-only MCP server lets Claude Code, Claude Desktop, Cursor, Codex or any MCP client list, search and read your transcripts. Ucho does not ship its own chatbot; your agent does the summarizing.
- **Optional local summaries.** Off by default. If you turn them on, a local Qwen model writes a short summary and checks it mechanically against the transcript, so it cannot invent names or dates that were never said.

## Why another transcription app

| | Ucho |
|---|---|
| Runs offline | Yes. Network is used once, to download models from Hugging Face. Verified by monitoring the process's TCP connections. |
| Ordinary laptops | Tuned for Intel Core Ultra with Arc graphics (OpenVINO on the integrated GPU), falls back to the CPU anywhere else. |
| Polish and English | Language detection runs on ~12 s chunks and is restricted to the languages you use, so you don't get random Korean or Icelandic in a Polish meeting. |
| Whisper hallucinations | Silence is cut before it reaches the model, and known subtitle-style phantoms ("Subtitles by…", "Thanks for watching") are filtered out. |
| Your files | Plain `transkrypt.md`, `.srt`, `.txt` and `meta.json` in a folder per meeting. |

## Performance (Core Ultra 7 258V, Arc 140V)

| | GPU (Arc 140V) | CPU |
|---|---|---|
| 85 s recording, forced language | 5.7 s (15x faster than real time) | 35 s (2.4x) |
| 51 s recording, PL/EN auto-detect | 3.7 s (14x) | 47 s (1.1x) |
| First model load | ~21 s (compilation, then ~2 s from cache) | ~6 s |

A one-hour meeting transcribes in about 4 minutes on the Arc GPU.

## Requirements

- Windows 10 or 11, x64.
- 16 GB RAM recommended (8 GB works with the smaller Turbo models).
- Best on Intel Core Ultra (Arc graphics). Other x86 CPUs work on the CPU path, more slowly.
- About 3 GB of disk for the app and the default model.

## Install

1. Download `Ucho-Windows.zip` from [Releases](../../releases) and unpack it, for example to `C:\Users\<you>\Ucho\app`. **Do not unpack into OneDrive** (Desktop and Documents are often synced): models and logs would be uploaded.
2. Right-click `instaluj.ps1` → *Run with PowerShell*, or:
   ```powershell
   powershell -ExecutionPolicy Bypass -File .\instaluj.ps1
   ```
   No admin rights needed. It installs Python 3.12 (via winget if missing), a virtual environment with pinned dependencies, downloads the Whisper model (~1.6 GB, pinned revision) and creates shortcuts.
3. Start **Ucho** or **Ucho Dyktuj** from the Start menu.

Optional: `-ZPodsumowaniem` also downloads the summary model (~5 GB) and turns summaries on.

Recordings are saved in `C:\Users\<you>\Ucho\nagrania`, outside OneDrive on purpose. You can change it in Settings.

## Use

- Type a meeting name, press **REC**, or use **Ctrl+Alt+R** anywhere (also when the window is hidden in the tray).
- **STOP** starts transcription. You can stop it; finished parts are kept.
- **Use headphones.** On speakers the microphone also picks up the other people and you get everything twice.
- **Tell participants you are recording.** Ucho has no bot and is technically invisible; that does not make recording legal. See [PRIVACY.md](PRIVACY.md).

The interface is in Polish or English (Settings → Interface language, or automatically from Windows).

## Connect your AI agent (MCP)

The MCP server is read-only, uses only the Python standard library, talks over stdio and opens no ports. Tools: `list_meetings`, `get_transcript`, `get_summary`, `search_transcripts`.

Claude Code:

```powershell
claude mcp add ucho -- "C:\Users\<you>\Ucho\app\.venv\Scripts\python.exe" "C:\Users\<you>\Ucho\app\mcp_serwer.py"
```

Claude Desktop, Cursor and others: add a stdio server with the same command and argument. Then ask, for example: *"What did we agree with the steel supplier last week?"*

Your agent sends the transcript text it reads to its model provider (for Claude Code, Anthropic). Ucho itself sends nothing; what your agent does with the text is up to you and your agent's settings.

## Privacy

Short version: audio, transcripts and settings stay on your disk. Telemetry of Hugging Face and OpenVINO is disabled. Details and a one-line check you can run yourself: [PRIVACY.md](PRIVACY.md).

## Limitations (v0.1)

- Windows only. macOS comes in v0.2.
- Everyone on the far end of a call is labeled "Meeting"; individual remote speakers are not separated.
- No live transcript while recording.
- The app is not code-signed yet, so Windows may show a warning.

## Development

```powershell
py -3.12 -m venv .venv-dev
.\.venv-dev\Scripts\python.exe -m pip install pytest ruff numpy soundfile
.\.venv-dev\Scripts\python.exe -m pytest
.\.venv-dev\Scripts\python.exe -m ruff check .
```

Architecture notes: [docs/architecture.md](docs/architecture.md). Contributing: [CONTRIBUTING.md](CONTRIBUTING.md).

## License

Apache-2.0, see [LICENSE](LICENSE) and [NOTICE](NOTICE). Models and libraries keep their own licenses: [THIRD_PARTY.md](THIRD_PARTY.md).

Made by Fryderyk Pryjma.
