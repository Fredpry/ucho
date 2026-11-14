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
