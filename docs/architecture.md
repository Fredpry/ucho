# Architecture

How Ucho turns a call into a transcript on an ordinary laptop, and why each step looks the way it does. Most decisions below were measured on real Polish and English meetings.

## 1. Two tracks instead of diarization

Ucho records the microphone and the system audio (WASAPI loopback, i.e. whatever comes out of your speakers) into two separate FLAC files, 16 kHz mono, written incrementally so a crash keeps everything recorded so far.

Both tracks are transcribed separately and merged by timestamp. The microphone becomes "Me" and the system audio "Meeting". This gives a speaker split for free, without a diarization model, and without the classic diarization errors when two people have similar voices. The cost: everyone on the far end is one speaker. With speakers instead of headphones, the microphone hears the others too, so headphones are required for clean results.

Optional speaker detection inside a track (`ucho/diarize.py`, experimental, off by default) can split "Meeting" (and, when several people share a room microphone, "Me") into numbered voices. It runs only after the transcription as one extra pass per track in a worker process: every Whisper segment takes the voice with the largest time overlap (segments without overlap keep the track speaker), giving codes `meeting:1`, `meeting:2`, … or `me:1`, `me:2`, …, while segment text and timestamps never change. User-given speaker names are stored separately in a `speaker_names` mapping. The two tracks remain the base because separating "Me" from "Meeting" by hardware never confuses similar voices.

## 2. Silence never reaches Whisper

Whisper invents text on silence and noise: Polish recordings produce "Napisy stworzone przez społeczność Amara.org", English ones "Thank you." or "Thanks for watching". Ucho detects silence by RMS and cuts it before the model sees it. Remaining known phantoms are removed by patterns in `merge.py` (and a test found that the Polish "Dziękuję za uwagę" pattern had never matched because of a missing "ę"; fixed in v0.1).

Dictation additionally shortens pauses longer than one second, because a long pause in the middle of a sentence is the main source of an invented "Dziękuję." at the end.

Live dictation (`ucho/live.py`, on by default) recognizes a dictation while it is spoken. Every 250 ms the new microphone audio goes to a cutter that ends a piece at a 0.6 s pause (pieces of at least 2 s, so a lone "Tak." stays with the next sentence; at most 20 s, then the cut goes to the quietest spot). Each piece is a short dictation-priority job on the engine, so it also fits between meeting chunks. The language detected on the first piece is kept for the rest, and the end of the text so far goes to Whisper as the prompt (`initial_prompt`). After the hotkey only the last piece is left to recognize. The closing-line cut and the hallucination filter run once, on the joined text: per piece they would drop a real "Tak." or the end of a piece. If a piece fails, the whole recording is recognized in one call as before.

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

**Live meeting transcription** (`ucho/live_meeting.py`, issue #16). While a meeting records, the recorder's write threads pass every block they write to `LiveMeeting`, which rounds it exactly as the 16-bit FLAC file stores it. A `TrackChunker` per track cuts the growing track with the same functions as the transcription after the stop and hands over a chunk only when it is final: with a forced language a 5-minute cut depends only on the 30 s around it, so every chunk but the last is final once 15 s more audio exists; in Auto mode the cut points depend on the loudest frame of the whole track, so a chunk counts as final under the current level and may still turn out different. Final chunks with speech go to the engine one at a time as normal-priority jobs (dictation goes first, and no new chunk starts while a dictation is being spoken), and each result is kept in a `ChunkCache` with a hash of its samples. After the stop the normal step transcription runs over the files as before and takes a chunk from the cache only when its range and samples are bit-identical to the chunk it cut, so the transcript is always the one the old way would give; anything else (the last chunk, Auto chunks cut differently) is recognized then. The text so far is shown on the Meeting screen and saved in the session folder after every chunk (`transcript_partial` in `meta.json`), so a crash keeps it; the transcription after the stop replaces it. When the live work falls behind the recording by more than two minutes plus one chunk, or fails three times, it stops for that recording and everything left is done after the stop. A recording stopped while another one is being transcribed waits in the queue with its cache. Live transcription runs only when the recording is transcribed after the stop anyway; the automatic setting turns it on in Auto language mode on a GPU or NPU. Measured on 8 minutes of synthetic speech: the transcript was ready 4.1 s after the stop instead of 90 to 138 s (Auto) and after 23 s instead of 54 to 56 s (Polish), identical in both cases ([benchmarks.md](benchmarks.md#live-meeting-transcription-16)).

## 8. Optional second backend: Parakeet

NVIDIA Parakeet TDT 0.6B v3 runs through sherpa-onnx (ONNX Runtime) on the CPU only; the PyPI wheel has no GPU execution provider. It is installed only with `install.ps1 -WithParakeet` and appears in the model list only when `sherpa_onnx` is importable. `transcribe.create_engine` returns a `parakeet.Engine` for its model id; that class has the same surface as the Whisper engine and returns results shaped like OpenVINO's (`.texts`, `.chunks` with timestamps, one chunk per sentence), so the engine thread, silence gate, chunking, hallucination filter, `merge_tracks` and the MCP server are shared. Two things differ: the model has no language setting (it picks among 25 European languages itself), and long audio has to be cut into pieces of at most 20 s, so the 5-minute chunks of forced-language mode are split again. Measured on synthetic speech it is close to Whisper on Polish and much faster on the CPU, but weak with English phrases inside Polish sentences, so it is never chosen automatically ([benchmarks.md](benchmarks.md#parakeet-tdt-06b-v3-as-an-optional-backend-v02)).

## 9. Speaker detection and names

Optional speaker detection (`ucho/diarize.py`, issue #15) tells voices apart inside a track with `sherpa-onnx` on the CPU, using two small ONNX models (~33 MB in all, pinned in `config.REVISIONS` and downloaded only when the feature is turned on in Settings → Transcription → *Speakers*): `pyannote/segmentation-3.0` (~6 MB, MIT, CNRS) and the 3D-Speaker project's `ERes2Net` trained on VoxCeleb (~27 MB, Apache-2.0; only the single ONNX file is downloaded from `csukuangfj/speaker-embedding-models` via `config.DOWNLOAD_PATTERNS`).

- **Worker process because of the GIL.** `sherpa-onnx` holds the Python GIL for seconds at a time during diarization (up to 3.2 s even with a progress callback, or the whole run without one), which would freeze the UI and delay dictation. Ucho runs each track pass in a short-lived subprocess (`python -m ucho.diarize --worker <track>`) at below-normal priority without a console window. Meanwhile `iter_diarize_track` yields on the engine thread every 0.1 s, so a dictation can overtake it between steps. Cancelling the transcription kills the worker immediately; if the worker fails, the segments keep their plain track speaker (`me` or `meeting`) and the transcript is still saved.
- **Text and timestamps never change.** `assign_speakers` only relabels each already-recognized Whisper segment to the diarized speaker turn with the largest summed time overlap (ordered 1, 2, … by first appearance on that track: `meeting:1`, `meeting:2` rendered as "Speaker 1", "Speaker 2", or `me:1`, `me:2` rendered as "Microphone 1", "Microphone 2"). If a track has fewer than two assigned speakers, its segments keep the plain track code.
- **Speaker names (`speaker_names`).** Clicking a speaker button or label on the transcript screen saves a `{speaker_code: name}` entry in `meta.json` and rewrites `transcript.md`, `transcript.srt`, `transcript.txt` and `meta.json` without touching the segments themselves. Two speaker numbers can be given the same name, so one person split into two voices reads as one person (the lines are not joined). *Transcribe again* drops names of numbered speakers (`meeting:N`, `me:N`) because a new diarization pass may number voices differently, and keeps names given to plain `me` or `meeting`.
- **Summaries, search and MCP.** When a summary runs, user-given names of speakers present in the segments are passed to the prompt and prepended to `participants` as known participants, while the mechanical name filters (`_names`, `_name_in_text`) still reject any extra names the LLM invents that were not said in the transcript. Full-text search (`ucho/archive.py`) indexes speaker names alongside titles and participants, and the MCP server (`ucho_mcp/server.py`) shows them in `list_meetings`, `get_transcript` and `search_transcripts`.
- **No voice profiles.** Voice embeddings exist only in the worker's memory during a single pass over one track and are never written to disk or reused across meetings. Benchmarks and model comparisons are in [benchmarks.md](benchmarks.md#speaker-detection-15).

## Files per meeting

```
recordings\2026-11-14_10-00_Budget-Q4\
    microphone.flac    microphone track
    meeting.flac       system audio track
    transcript.md      transcript with speakers and timestamps
    transcript.srt     subtitles
    transcript.txt     plain text
    meta.json          session parameters, structured segments + optional speaker_names
    summary.md         only if summaries are enabled
```
