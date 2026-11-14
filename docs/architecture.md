# Architecture

How Ucho turns a call into a transcript on an ordinary laptop, and why each step looks the way it does. Most decisions below were measured on real Polish and English meetings.

## 1. Two tracks instead of diarization

Ucho records the microphone and the system audio (WASAPI loopback, i.e. whatever comes out of your speakers) into two separate FLAC files, 16 kHz mono, written incrementally so a crash keeps everything recorded so far.

Both tracks are transcribed separately and merged by timestamp. The microphone becomes "Me" and the system audio "Meeting". This gives a speaker split for free, without a diarization model, and without the classic diarization errors when two people have similar voices. The cost: everyone on the far end is one speaker. With speakers instead of headphones, the microphone hears the others too, so headphones are required for clean results.

## 2. Silence never reaches Whisper

Whisper invents text on silence and noise: Polish recordings produce "Napisy stworzone przez społeczność Amara.org", English ones "Thank you." or "Thanks for watching". Ucho detects silence by RMS and cuts it before the model sees it. Remaining known phantoms are removed by patterns in `merge.py` (and a test found that the Polish "Dziękuję za uwagę" pattern had never matched because of a missing "ę"; fixed in v0.1).

Dictation additionally shortens pauses longer than one second, because a long pause in the middle of a sentence is the main source of an invented "Dziękuję." at the end.

## 3. Language detection on short chunks, restricted to your languages

Whisper detects the language once per processed chunk. Feed it a whole bilingual meeting and the second language gets translated into the first or dropped. So in `Auto` mode the recording is cut at silences into ~12 s chunks, each with its own detection. With a forced language, chunks are up to 5 minutes long, which gives the model more context and slightly better results.

Short chunks have a price: on "mhm", a cough or noise the detector guesses among all ~100 languages Whisper knows, and Polish meetings got Korean or Icelandic insertions. Ucho gives the model a reduced `lang_to_id` map containing only the configured languages. Measured on eight bad chunks: restricting `lang_to_id` fixed 8 of 8; `suppress_tokens` fixed 1 of 8, because in OpenVINO GenAI language detection is a separate pass that runs before generation and ignores suppressed tokens.

## 4. OpenVINO on the integrated GPU

Whisper large-v3 INT8 runs through OpenVINO GenAI on the Intel Arc iGPU, with no PyTorch and no conversion on the user's machine (models are ready-made OpenVINO IR from Hugging Face, pinned to a fixed revision). Measured on a Core Ultra 7 258V: an 85 s recording in 5.7 s on the GPU and 35 s on the CPU. The first GPU load compiles the model (~21 s); the result is cached in `models/ovcache`. If the GPU fails, Ucho falls back to the CPU and records that in `meta.json`.

## 5. Optional summaries that cannot invent names or dates

Off by default. When enabled, a local Qwen model (same OpenVINO runtime, loaded only after Whisper is released) produces a summary as JSON constrained by a schema, so the folder name and summary never depend on whether a small model got the format right.

Two mechanical filters run on the result, because no prompt was enough:

- **Names** must appear in the transcript as a sufficiently similar whole word, with tolerance for Polish inflection ("Paweł" appears only as "Pawle" and "Pawła"). Whole words matter: "Teresa" hides in "zainteresowani" and "Robert" in "robimy". Threshold 0.8, measured: 6 of 6 present names kept, 0 of 8 absent names let through.
- **Dates** are cut back to what was said. A month nobody mentioned disappears, and a numeric date like `29.02` keeps only the day. Times such as `12.00` are safe because `00` is not a month.

Meetings up to ~40 minutes are summarized in one pass. Longer ones are split, but the threshold is high on purpose: map-reduce loses decisions that are renegotiated during a meeting (the model took the first proposed date and missed that it was moved half an hour later).

## 6. MCP: the archive as memory for your agent

`ucho_mcp/server.py` is a stdio MCP server built only on the Python standard library: JSON-RPC over stdin/stdout, no ports, no network, read-only. It reads `meta.json` segments through `ucho/session.py` (the same reader the app uses, which also understands the Polish file names and keys of v0.1 archives), builds an in-memory SQLite FTS5 index (diacritics-insensitive, prefix search) and rebuilds it when the archive changes. Meeting ids are folder names; anything containing a path separator or `..` is rejected.

Ucho deliberately has no chatbot. The agent you already use (Claude Code, Cursor, Codex) reads the transcripts and summarizes them. That keeps the app small and leaves the choice of model, and of whether text may leave the machine, to the user.

## 7. One app, one Whisper engine

Up to v0.1 meetings and dictation were two processes with two tray icons and two models in memory (Dictate kept large-v3 FP16, about 3 GB, loaded all day). Since v0.2 they are one process (`ucho/app.py`) with one engine (`ucho/engine.py`):

- **One worker thread with a priority queue.** An OpenVINO pipeline must not be called from two threads, so every model call runs on the engine thread. Jobs are ordered: dictation, then meeting transcription, summary and benchmark, then background preloading.
- **Transcription in steps.** A meeting transcription is a generator that yields after every chunk (`transcribe.iter_transcribe_session`). After each chunk the worker picks the most urgent job again, so a dictation waits for at most one chunk. In the default Auto mode a chunk is ~12 s of audio, about 1 s of work on an Arc GPU. With a forced language chunks are up to 5 minutes long (section 3), and a dictation can wait up to one of them; OpenVINO cannot interrupt a running chunk (its streamer works only for short audio without timestamps).
- **Models cached by (model, device).** When dictation uses the transcription model ("shared", the default with less than 16 GB RAM) both use one loaded pipeline. A separate FP16 dictation model is a setting. Before a summary every Whisper pipeline is released (`engine.exclusive`), because the language model runs on the same integrated GPU; the dictation model is loaded again afterwards.
- **One identity on the taskbar.** One AppUserModelID for the window, the shortcuts ("Ucho" and "Ucho Dictate") and the Jump List (*Record a meeting*, *Dictate*). A named mutex keeps one instance per Windows session; a second start sets a named event and exits, and the running app shows its window, opens dictation or starts recording.
- **Hotkeys through `RegisterHotKey`.** Ctrl+Alt+R and Ctrl+Shift+Space are owned by the app while it runs, Esc only while a dictation records. A combination held by another program is shown in the window instead of failing.

Measured on a Core Ultra 7 258V (Arc 140V, 32 GB), 6 minutes of synthetic Polish speech transcribed on the GPU while six 10 s dictations were submitted (`tools/benchmark_engine.py`): a dictation was answered in 1.2 to 1.7 s (median 1.3 s) with the shared INT8 model and 1.3 to 1.7 s (median 1.5 s) with a separate FP16 model; alone, 1.0 to 1.4 s. Details in [benchmarks.md](benchmarks.md#dictation-during-a-transcription-v02).

## Files per meeting

```
recordings\2026-11-14_10-00_Budget-Q4\
    microphone.flac    microphone track
    meeting.flac       system audio track
    transcript.md      transcript with speakers and timestamps
    transcript.srt     subtitles
    transcript.txt     plain text
    meta.json          session parameters + structured segments
    summary.md         only if summaries are enabled
```
