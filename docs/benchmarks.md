# Benchmarks

How fast and how accurately Ucho transcribes on different laptops, and how to measure it again before a release. Two parts:

1. **Hardware matrix**: does the installer work and how fast is each model on each device, on at least three kinds of laptop.
2. **Transcription quality regression**: word error rate (WER) on public Polish speech with `tools/benchmark_wer.py`.

Both are run by hand. Neither runs in CI (no models, no GPU, no audio there).

## Known numbers (v0.1)

Measured on the development laptop, Intel Core Ultra 7 258V with Arc 140V, 32 GB RAM, Whisper large-v3 INT8:

| | GPU (Arc 140V) | CPU |
|---|---|---|
| 85 s recording, forced language | 5.7 s (15x faster than real time) | 35 s (2.4x) |
| 51 s recording, PL/EN auto-detect | 3.7 s (14x) | 47 s (1.1x) |
| First model load | ~21 s (compilation, then ~2 s from cache) | ~6 s |

A one-hour meeting transcribes in about 4 to 7 minutes on the Arc GPU: 15x is this short clip with a forced language, 8.9x a 5-minute synthetic meeting with automatic language detection ([Parakeet section](#parakeet-tdt-06b-v3-as-an-optional-backend-v02)). Auto-detect is slower on the CPU because the recording is cut into ~12 s chunks, each with its own language detection (see `docs/architecture.md`, section 3).

Everything below fills in the other rows.

## Dictation during a transcription (v0.2)

Plan v0.2, W3: one process and one Whisper engine for meetings and dictation; a 10 s dictation must be answered in under 3 s while a meeting is being transcribed. Measured with `tools/benchmark_engine.py` on synthetic Polish speech from the offline Windows voice *Microsoft Paulina* (no recording of anyone): a 6-minute "meeting" transcribed on the GPU with large-v3 INT8 in Auto mode, and six 10 s dictations submitted 3 s apart while it ran. Core Ultra 7 258V, Arc 140V, 32 GB, OpenVINO GenAI 2026.3.1, 14.11.2026.

| Dictation model | Models in memory (private bytes) | Dictation alone | Dictation during transcription | 6 min meeting transcribed in |
|---|---|---|---|---|
| shared large-v3 INT8 | 2.1 GB | 1.0-1.5 s | 1.2-1.7 s (median 1.3 s) | 50 s |
| separate large-v3 FP16 | 5.2 GB | 1.4-2.3 s | 1.3-1.7 s (median 1.5 s) | 54 s |

The first value of "alone" includes the warm-up of the first call. The meeting gets slower by the time the dictations take, nothing more.

With a forced language (`--meeting-language pl`) the meeting is cut into chunks of up to 5 minutes, and a dictation waits for the chunk that is running: 24.0 s and 5.8 s measured for two dictations. OpenVINO cannot interrupt a running chunk. Keeping the 5-minute chunks was a quality decision (`transcribe.py`, section 3 of [architecture.md](architecture.md)); shorter chunks for forced languages need a WER check on real meetings first.

For comparison, v0.1 ran Dictate as a second process that kept FP16 loaded (about 3 GB) next to the meeting app.

```powershell
$env:HF_HOME = "C:\path\to\models"   # where the models are downloaded
.\.venv\Scripts\python.exe tools\benchmark_engine.py --only shared
.\.venv\Scripts\python.exe tools\benchmark_engine.py --only separate
.\.venv\Scripts\python.exe tools\benchmark_engine.py --only shared --meeting-language pl
```

Run each scenario in its own process (`--only`), otherwise the memory numbers of the second include the first.

### Dictation first while speaking (#13)

Where a dictation's time goes during a meeting transcription, from the engine's own job times: a 28.1 s live dictation (6 sentences by *Microsoft Paulina*, separate FP16 model, both models loaded) during a 10-minute synthetic meeting (INT8), Arc 140V, 16.11.2026. Running a piece between transcription chunks costs nothing extra: a piece took 0.8-1.0 s during the transcription and 0.8-0.9 s alone, so there is no model-switch penalty. All the delay is waiting for a running chunk. In Auto mode a step takes 0.7 s (median, at most 1.8 s), so pieces waited at most 1.1 s and the text came 1.0-1.5 s after the stop (alone 0.95 s). With Polish forced the chunks took 29.5-31.3 s, pieces waited up to 24.4 s and the text came 4.7 and 6.5 s after the stop. Reloading the meeting model from the OpenVINO cache took under 2 s. The 15.5 s from the issue did not appear with both models loaded on an otherwise idle GPU.

Since #13 the engine starts no new transcription step while the dictation hotkey records (`Engine.pause_for_dictation`). The same day other benchmarks loaded the CPU and GPU, so wall-clock runs of `--dictation-priority` varied 3x between rounds (the transcripts were identical in every round). The ordering was therefore replayed with the real `Engine` and sleeps of the measured lengths (30 trials per row, random start of the dictation relative to the chunks; pieces every 5.5 s of speech, 0.9 s each, final 1.0 s). Time from the stop to the text:

| Meeting language | Dictation | Before: median / max | With the pause: median / max | Meeting time lost per dictation |
|---|---|---|---|---|
| Polish (30 s chunks) | 10 s | 15.2 s / 29.1 s | 4.8 s / 21.9 s | 1.6 s |
| Polish (30 s chunks) | 28 s | 13.4 s / 31.3 s | 1.4 s / 7.0 s | 6.9 s |
| Auto (0.5-1.8 s steps) | 10 s | 1.7 s / 2.5 s | 1.1 s / 1.1 s | 8.5 s |
| Auto (0.5-1.8 s steps) | 28 s | 2.0 s / 3.0 s | 1.4 s / 1.7 s | 23.2 s |

With the pause no transcription step starts while you speak, so a dictation waits only for the chunk that was already running when it started, and that wait overlaps with the speaking. A short dictation started right after a 5-minute chunk began still waits for most of it (#12 stays open for forced languages). The price is meeting time: the GPU idles while you speak once the running chunk is done. Repeat on a quiet machine with `tools\benchmark_engine.py --dictation-priority --meeting-language pl --rounds 3` (the arms alternate, so a changing load hits both).

## INT8 vs FP16 for dictation (v0.2)

Plan v0.2, W3: "measure the quality difference on 20 dictations before deciding". 20 short Polish dictation sentences (made up: e-mails, tasks, terminal requests) spoken by *Microsoft Paulina*, `tools/benchmark_engine.py --make-wer-set`, then `tools/benchmark_wer.py --language pl --device GPU`:

| Model | WER | CER | Processing (95.5 s of audio) | Load (cached) |
|---|---|---|---|---|
| large-v3 INT8 | 3.2 % | 3.3 % | 11.4 s (8.4x) | 2.0 s |
| large-v3 FP16 | 3.2 % | 3.3 % | 15.7 s (6.1x) | 4.3 s |

Both models wrote exactly the same text for all 20 sentences. Every error is a number written as digits ("dwadzieścia trzy" became "23", "o dziesiątej" became "o 10.00"), which the WER normalization counts as wrong (see "Reading the numbers" below).

Limits: a synthetic voice is clean, steady and has no accent, background noise or hesitations, so this shows only that INT8 is not worse on clear speech. Real dictations (a laptop microphone, an office) may separate the models; the founder decides after a check on a few real dictations.

```powershell
.\.venv\Scripts\python.exe tools\benchmark_engine.py --make-wer-set C:\bench\tts-pl
.\.venv\Scripts\python.exe tools\benchmark_wer.py C:\bench\tts-pl --language pl --model OpenVINO/whisper-large-v3-int8-ov
.\.venv\Scripts\python.exe tools\benchmark_wer.py C:\bench\tts-pl --language pl --model OpenVINO/whisper-large-v3-fp16-ov
```

## OpenVINO 2026.3.1 → 2026.4.1 (v0.2)

Checked before accepting the Dependabot update, same laptop, GPU, same 20 synthetic sentences:

| | OpenVINO 2026.3.1 | OpenVINO 2026.4.1 |
|---|---|---|
| WER / CER (large-v3 INT8) | 3.2 % / 3.3 % | 3.2 % / 3.3 % |
| Processing, 95.5 s of audio | 12.2 s (7.8x) | 12.1 s (7.9x) |
| Dictation during a 3-minute meeting, max / median | | 2.3 s / 1.8 s |
| `python -m ucho --smoke-test` | OK | OK |

The text is identical, so the update was accepted (with numpy 2.5.4).

## Live dictation (#19)

Recognizing a dictation in pieces while it is spoken (`ucho/live.py`), compared with the old way (the whole recording in one call after the hotkey). `tools/benchmark_engine.py --live` on synthetic Polish speech from *Microsoft Paulina*: the 20 dictation sentences as one 62.8 s dictation (13 sentences) and as four dictations of 5 sentences, with the voice's 0.7 s pause between sentences. Language "auto", as in the app. Core Ultra 7 258V, Arc 140V GPU, OpenVINO GenAI 2026.4.1, 16.11.2026.

| Model | WER whole (old) | WER live | WER live without prompt | Pieces (62.8 s) |
|---|---|---|---|---|
| large-v3 FP16 | 2.7 % | 1.9 % | 1.9 % | 13 + empty tail |
| large-v3 INT8 | 2.7 % | 1.9 % | 1.9 % | 13 + empty tail |

262 reference words in all. The only difference: "o dziesiątej" stays words in a piece and becomes "o 10" in the whole recording; the rest of the text is identical. Each sentence alone (the 20-sentence set) is shorter than the first cut, so live and whole give the same text there by construction. The prompt (`initial_prompt`, the end of the text so far) changed nothing on this clean speech; it stays on for real speech, where a cut can fall inside a sentence.

Time from the stop to the text for the 62.8 s dictation, stopped 0.25 s after the last word (3 runs, the model loaded):

| Model | Old (whole after the stop) | Live |
|---|---|---|
| large-v3 FP16 | 7.9 s median, 8.6 s max | 0.83 s median, 1.20 s max |
| large-v3 INT8 | 8.4 s median, 10.2 s max | 0.83 s median, 0.84 s max |

After the stop only the last sentence is left (about 1 s of work). With a meeting being transcribed at the same time (8 minutes of synthetic speech, INT8, Auto mode), the live dictation was ready 1.03 s after the stop, and the meeting took 87.3 s instead of 81.3 s: it waits only for the pieces, each about 1 s of work. A first run on the same day measured 18 to 25 s for the old way on 62.8 s of audio (live 0.4 to 1.7 s); the GPU was probably busy with something else then, and the numbers above are from a repeat.

Limits: the synthetic voice pauses clearly between sentences, so every cut fell between sentences. Real speech has shorter or no pauses (then a piece is cut at 20 s, at the quietest spot), hesitations and background noise. Check a few real dictations before relying on these numbers.

```powershell
.\.venv\Scripts\python.exe tools\benchmark_engine.py --live --live-during-meeting --meeting-min 8
.\.venv\Scripts\python.exe tools\benchmark_engine.py --live --live-model OpenVINO/whisper-large-v3-int8-ov
```

## Live meeting transcription (#16)

A meeting transcribed while it is recorded (`ucho/live_meeting.py`), compared with today's transcription after the stop. `tools/benchmark_engine.py --live-meeting` builds an 8-minute synthetic meeting (system track: the made-up monologue by *Microsoft Paulina*; microphone track: one short sentence about once a minute over a faint noise floor), feeds both tracks in 128 ms blocks in real time to `LiveMeeting` as the recorder would, stops, and runs the normal transcription with the live cache. Meanwhile a 10 s dictation (separate FP16 model) is spoken every 50 s; no live chunk starts while it is spoken, as in the app. Transcription model large-v3 INT8, Core Ultra 7 258V, Arc 140V GPU, OpenVINO GenAI 2026.4.1, 16.11.2026. "After the stop only" has the model already loaded (the best case for it).

| Language | After the stop only | Live: stop to transcript | Chunks from the live cache | Final transcript |
|---|---|---|---|---|
| Auto (12 s chunks) | 89.5 s, 137.9 s | 4.1 s | 77 of 82 | identical (60 utterances) |
| Polish (chunks up to 5 min) | 56.0 s, 53.9 s | 23.2 s | 2 of 4 | identical (54 utterances) |
| Auto, system track 1.6x louder from minute 4 (4x real time) | 67.2 s | 27.4 s | 49 of 83 | identical (60 utterances) |

"Identical" compares start, end, speaker and text of every utterance with each run after the stop only (those runs were identical to each other too). In Auto mode only the last chunks are left after the stop; at the stop the live work was 4.9 s behind the recording. With a forced language the last chunk of each track (here about 3 minutes) can only be cut once the recording has ended, so it is always transcribed after the stop: up to 5 minutes 15 s of audio per track. The louder variant raises the silence threshold of Auto cutting in the middle of the recording, so some cuts made live no longer match the cuts of the whole track; those chunks are recognized again after the stop (with the 21.7 s the live work was behind at 4x speed, 34 chunks in all) and the transcript stays identical.

Dictation while the live transcription runs: 1.4 to 2.1 s in Auto mode (9 dictations; alone 1.4 to 4.4 s in that run), 1.4 to 1.7 s with Polish (9 dictations; alone 1.3 to 1.4 s). One live chunk took at most 3.2 s of work in Auto mode, but 25.8 s with Polish (a 5-minute chunk). A dictation spoken while such a chunk is being recognized waits for it; none of the 9 did here, but that is luck of timing. This is why the automatic setting turns live transcription on only in Auto language mode (#12).

Limits: synthetic speech with clear pauses and a steady level. In a real meeting the level changes (a louder speaker, a cough near the microphone), so more Auto chunks may be cut differently live and recognized again after the stop: slower, but never a different transcript. The transcription model stays loaded during the recording; before, it was loaded only after the stop.

```powershell
.\.venv\Scripts\python.exe tools\benchmark_engine.py --live-meeting --meeting-min 8 --meeting-language auto
.\.venv\Scripts\python.exe tools\benchmark_engine.py --live-meeting --meeting-min 8 --meeting-language pl
.\.venv\Scripts\python.exe tools\benchmark_engine.py --live-meeting --louder-from 4 --speed 4 --offline-runs 1
```

## Meeting titles (#17)

Automatic short titles for unnamed meetings (`ucho/titles.py`). Measured on 10 synthetic meetings written for the test (5 to 60 minutes, 7 Polish and 3 English); OpenVINO GenAI 2026.4.1; Core Ultra 7 258V, Arc 140V; greedy decoding, JSON schema; quality judged by hand:

| Model | Size | Usable titles (of 10) | Title, Arc iGPU | Title, CPU |
|---|---|---|---|---|
| Qwen3-0.6B INT4 | 0.45 GB | 2 (most start with "Spotkanie", broken grammar) | 0.9-2.5 s | not measured |
| Qwen2.5-1.5B Instruct INT4 | 0.94 GB | 6 (wrong topic for the interview, "Kampania Premiera") | 0.6-1.5 s | not measured |
| Qwen3-1.7B INT4 | 1.21 GB | 6-7 (missed the job interview and the rescheduling call in 2 of 3 prompt variants, lower-case titles) | 0.4-1.1 s | 14-43 s |
| Qwen3-4B INT4 | 2.29 GB | 10 | 0.6-1.2 s | 7-27 s |

Notes:
- Times are per title for ~1,000 prompt tokens (1,800 + 900 characters of transcript), after the model is loaded.
- Loading from the OpenVINO cache took 1.4-2.8 s on the GPU and 4.4 s on the CPU; the first load ever (compilation) took about 30 s on the GPU.
- With 1,450 prompt tokens the CPU took 40-47 s, hence the smaller excerpt (1,800 + 900 characters).
- CPU numbers vary because the machine was shared with other work during the test.
- Through the app's code path (`titles.generate_title`: load, one generation, release, filters), all 10 meetings got a usable title. One title took 3-4 s on the GPU including the model load (15 s the first time, with compilation), and 26-58 s on the CPU including the load. The CPU misses the 30 s target; the title runs in the background and steps aside for user work, so this only delays the name.
- Example titles by Qwen3-4B: "Plan wdrożenia systemu magazynowego", "Przełożenie czwartkowego spotkania", "Przenoszenie pipeline'ów na GitHub Actions", "Analytics Dashboard Roadmap Review", "Xero Invoice Sync Issue Resolution".
- Prompt lessons: example titles in the prompt were copied into unrelated meetings ("Odbiór prac remontowych..."), and a list of meeting kinds made every title start with "Planowanie" or "Negocjacje".
- Next step: check on real meetings locally (not in the repo).

## Parakeet TDT 0.6B v3 as an optional backend (v0.2)

Plan v0.2, W6: is NVIDIA's Parakeet TDT 0.6B v3 (25 European languages, Polish among them), run through [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx), good enough to offer as an alternative to Whisper? Measure first; Whisper stays the default whatever the result.

**Result: offered as an optional backend, fp32 export only, never recommended automatically.** On synthetic Polish speech the fp32 export is within 1 pp WER of Whisper large-v3 and 3 to 10 times faster on the CPU, with less memory. The int8 export is twice as fast again but loses Polish word endings and fails the quality bar (more than 2 pp worse). Both exports handle English phrases inside Polish sentences much worse than Whisper, so Parakeet is not a drop-in replacement for bilingual meetings.

Setup: Core Ultra 7 258V (4 P-cores + 4 LP E-cores), Arc 140V, 32 GB, Windows 11, Python 3.12, OpenVINO GenAI 2026.4.1, sherpa-onnx 2.0.1 (PyPI), 14.11.2026. Whisper is large-v3 INT8 (the app default). Parakeet: `csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3` (fp32, revision `5fcc012`) and `...-v3-int8` (revision `ed5d745`), both exported by the sherpa-onnx project; file hashes in `ucho/config.py`. Parakeet on 2 CPU threads. Every run goes through the app's code path (`transcribe_file`: silence gate, chunking, hallucination filter) via `tools/benchmark_wer.py`. All audio is synthetic, from the offline Windows voices *Microsoft Paulina* (Polish) and *Microsoft Zira* (English); nobody was recorded.

The sets:

- **20 sentences**: `tools/benchmark_engine.py --make-wer-set`, 95.5 s, 158 words (the set used above).
- **Meeting**: the 20 sentences and the 8-sentence meeting monologue of `benchmark_engine.py`, whole sentences only, at four speaking rates (Paulina, rate -1 to 2), 1 s between blocks: 5.2 minutes, 523 words.
- **Quiet meeting**: the same at -30 dB (peak 0.03).
- **Mixed PL/EN**: 6 utterances, 54 words. Four Polish sentences with an English phrase spoken by Zira ("Klient pytał o the service level agreement dla drugiej linii.") and two English sentences.
- **Noise**: 30 s each of digital silence, white noise (RMS 0.002 and 0.03), 50 Hz hum and speech-shaped noise.

| Set | Engine | Device | WER | CER | Processing | Speed | Load | Peak RAM |
|---|---|---|---|---|---|---|---|---|
| 20 sentences | Whisper large-v3 INT8 | GPU | 3.2 % | 3.3 % | 11.7 s | 8.2x | 2.0 s | 2.8 GB |
| 20 sentences | Whisper large-v3 INT8 | CPU | 3.2 % | 3.3 % | 89.0 s | 1.1x | 4.5 s | 3.8 GB |
| 20 sentences | Parakeet fp32 | CPU | 3.2 % | 3.4 % | 8.6 s | 11.1x | 2.6 s | 2.6 GB |
| 20 sentences | Parakeet int8 | CPU | 6.3 % | 4.0 % | 4.3 s | 22.4x | 1.8 s | 0.8 GB |
| Meeting, `pl` | Whisper large-v3 INT8 | GPU | 2.7 % | 3.3 % | 26.2 s | 11.9x | 2.0 s | 2.8 GB |
| Meeting, `auto` | Whisper large-v3 INT8 | GPU | 4.2 % | 4.3 % | 35.0 s | 8.9x | 1.9 s | 2.8 GB |
| Meeting, `pl` | Whisper large-v3 INT8 | CPU | 2.7 % | 3.3 % | 81.3 s | 3.8x | 3.4 s | 3.8 GB |
| Meeting, `auto` | Whisper large-v3 INT8 | CPU | 4.4 % | 4.4 % | 222.4 s | 1.4x | 3.5 s | 3.8 GB |
| Meeting, `pl` | Parakeet fp32 | CPU | 3.6 % | 3.5 % | 25.7 s | 12.1x | 2.7 s | 2.7 GB |
| Meeting, `auto` | Parakeet fp32 | CPU | 3.6 % | 3.5 % | 26.9 s | 11.6x | 2.7 s | 2.7 GB |
| Meeting, `pl` | Parakeet int8 | CPU | 7.6 % | 4.5 % | 12.8 s | 24.2x | 1.7 s | 1.0 GB |
| Quiet meeting, `pl` | Whisper large-v3 INT8 | GPU | 2.9 % | 3.5 % | 26.5 s | 11.7x | | |
| Quiet meeting, `pl` | Parakeet fp32 | CPU | 3.4 % | 3.4 % | | | | |
| Mixed PL/EN, `auto` | Whisper large-v3 INT8 | GPU | 14.8 % | 6.1 % | | | | |
| Mixed PL/EN | Parakeet fp32 | CPU | 27.8 % | 12.1 % | | | | |
| Mixed PL/EN | Parakeet int8 | CPU | 27.8 % | 12.1 % | | | | |

Load is with the OpenVINO GPU cache already filled (the first GPU compile took 9.4 s). Peak RAM is the peak working set of the benchmark process (Python included); on the Arc iGPU part of Whisper's memory is shared GPU memory that this number does not show. The 20 sentences and the meeting share their sentences, so the two are not independent samples.

What the numbers say:

- **Quality.** Whisper's only errors on Polish are numbers written as digits ("dziesiątej" → "10"), which the WER normalization counts as wrong. Parakeet makes the same digit errors; fp32 adds a few real ones (3.6 % vs 2.7 % on the meeting), int8 many more: it drops word endings, mostly a final nasal vowel ("salę" → "sal", "Dziękuję" → "Dziękuje", "rusza" → "rus", "szkoleniu" → "szkoleni"). The int8 numbers are why the app offers fp32 only.
- **Mixed PL/EN is Parakeet's weak spot.** English phrases come out spelled the Polish way: "the service level agreement" → "Deserwice Level Agreement", "the shared backlog" → "Deshred Bike Lock". Whisper with `auto` gets three of the four phrases right. One English-only sentence was garbled by both. Parakeet has no language setting; it picks the language per utterance on its own.
- **Speed.** On the CPU Parakeet fp32 is 3.2x faster than Whisper with a forced language and 8.3x faster in `auto` mode (Whisper's 12 s auto chunks each pay a language detection), and about as fast as Whisper on the Arc GPU. That is the case it is for: laptops without a usable Intel GPU (L2 and L3 in the matrix below, not measured yet).
- **Threads.** Meeting, fp32: 1 thread 6.8x, 2 threads 12.1x, 4 threads 12.1x. int8: 1 thread 16.5x, 2 threads 24.2x, 4 threads 17.6x, 8 threads 8.4x. The E-cores slow the P-cores down; Ucho uses 2 threads (`parakeet.default_threads`). Other CPUs were not measured.
- **GPU.** The PyPI wheel of sherpa-onnx contains only the CPU execution provider. Asking for DirectML, OpenVINO or CUDA logs a warning and falls back to the CPU. A GPU build would mean building sherpa-onnx with ONNX Runtime DirectML ourselves, which is out of scope.
- **Silence and noise.** Neither engine wrote anything on the noise set. Parakeet also wrote nothing with Ucho's silence gate switched off, so unlike Whisper it does not invent text on noise.
- **Quiet input.** The model README warns that very quiet input decodes badly. At -30 dB the meeting scored 3.4 % without any gain and 3.8 % when each piece was first scaled to a 0.5 peak, so Ucho applies no gain.
- **Paragraphs.** A transcript paragraph ends where one track pauses for more than 1.5 s (`merge.merge_tracks`). Parakeet's sentence times come from its token times and durations and match the real pauses closely: the ~0.7 s breaks of the synthetic meeting came out as 0.5 to 1.6 s gaps. Whisper's timestamps stretch the same pauses to 1.4 to 2.6 s. So the same 5-minute meeting became 36 paragraphs with Whisper and 3 with Parakeet. The text is the same quality, but on the system audio track, where all remote speakers share one track, Parakeet transcripts have fewer and longer paragraphs. A different joining rule for Parakeet should be decided on real meetings, not on synthetic speech.
- **Long audio.** The model README says long audio must be split at silences into 15 to 20 s pieces. Ucho's forced-language chunks are up to 5 minutes, so `ucho/parakeet.py` splits them again into pieces of at most 20 s (`MAX_PIECE_S`); the `pl` and `auto` meeting rows show the same WER.

Limits: a synthetic voice is clean and steady, without accents, crosstalk, Teams compression or hesitations. Real meetings may change the ranking in either direction, and the mixed PL/EN set is only 54 words. Check on a few real recordings (local only) before recommending Parakeet to anyone.

```powershell
# Optional package (or: .\install.ps1 -WithParakeet), then download the model in Settings > Hardware
.\.venv\Scripts\python.exe -m pip install -r requirements-parakeet.txt
.\.venv\Scripts\python.exe tools\benchmark_engine.py --make-wer-set C:\bench\tts-pl
.\.venv\Scripts\python.exe tools\benchmark_wer.py C:\bench\tts-pl --language pl --backend parakeet
.\.venv\Scripts\python.exe tools\benchmark_wer.py C:\bench\tts-pl --language pl --backend parakeet --threads 4
.\.venv\Scripts\python.exe tools\benchmark_wer.py C:\bench\tts-pl --language pl --backend parakeet --model csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8
```

The int8 model is not offered in the app. For a comparison, download it once by hand: `python -c "from ucho.transcribe import model_path; model_path('csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8', download=True)"`.

## 1. Hardware matrix

### Laptops

At least these three classes. Add more rows when someone from the community sends results.

| ID | Class | Model | CPU | GPU / NPU | RAM | Windows | GPU driver |
|---|---|---|---|---|---|---|---|
| L1 | Intel Core Ultra with Arc (reference) | | Core Ultra 7 258V | Arc 140V / NPU | 32 GB | 11 | |
| L2 | Older Intel without Arc (CPU path) | | e.g. Core i5 10th to 12th gen | Iris Xe or UHD | | | |
| L3 | AMD | | e.g. Ryzen 5 or 7 | Radeon (not used by OpenVINO) | | | |
| L4 | (optional) 8 GB RAM laptop | | | | 8 GB | | |

Why these: L1 is what Ucho is tuned for; L2 is the most common business laptop and tests the CPU path and the Turbo models; L3 checks that nothing assumes Intel (OpenVINO runs on AMD CPUs, but not on Radeon GPUs).

### What to measure on each laptop

Use a fresh Windows user account (or a machine where Ucho was never installed), so the installer starts from zero.

**A. Clean install**

| ID | Python before | `install.ps1` result | Duration (without model download) | Model download | Warnings / errors | Shortcuts work |
|---|---|---|---|---|---|---|
| L1 | | ok / failed | | | | |

Note anything the installer printed in red or yellow, and the SmartScreen prompt, if any.

**B. Hardware profile and recommendation**

```powershell
.\.venv\Scripts\python.exe -m ucho.hardware              # profile + recommended model and device
.\.venv\Scripts\python.exe -m ucho.hardware --benchmark  # also measures the recommended model
```

`ucho.hardware` is added in v0.2 (workstream W1). It reads RAM, CPU features and OpenVINO devices, picks a model and device from an explicit rule table, and `--benchmark` measures the speed on a synthetic signal (no recordings), stepping one row down when the machine is too slow. Paste its full output into the results folder; it is the bug-report format too.

| ID | OpenVINO devices | Recommended model / device | Benchmark speed (x real time) | Load time | Accepted or stepped down |
|---|---|---|---|---|---|
| L1 | | | | | |

**C. Speed per model and device on real speech**

`--benchmark` measures only the recommended combination. For the full table, transcribe the same 5-minute files with every model and device the laptop supports, using `tools/benchmark_wer.py` (section 2), which also reports speed and load time:

```powershell
$py = ".\.venv\Scripts\python.exe"
foreach ($m in "whisper-large-v3-int8-ov", "whisper-large-v3-turbo-int8-ov", "whisper-large-v3-turbo-int4-ov") {
  foreach ($d in "GPU", "CPU") {
    & $py tools\benchmark_wer.py C:\bench\5min --model "OpenVINO/$m" --device $d --json "C:\bench\L1_${m}_$d.json"
  }
}
```

Only models that are already downloaded can be measured; the tool never downloads. Check `device_used` in the JSON: if a GPU load fails, Ucho falls back to the CPU and the row is then a CPU result.

| ID | Model | Device | 5 min PL: time (speed) | 5 min EN: time (speed) | Load | WER PL | WER EN | Peak RAM |
|---|---|---|---|---|---|---|---|---|
| L1 | large-v3 INT8 | GPU | | | | | | |
| L1 | large-v3 INT8 | CPU | | | | | | |
| L1 | turbo INT8 | GPU | | | | | | |
| L1 | turbo INT4 | CPU | | | | | | |

**The 5-minute recordings.** One Polish and one English file, the same on every laptop, so the rows are comparable. Make them once: two team members read a fixed, made-up script (a fictional meeting, no real names or business data) in a quiet room, recorded as 16 kHz mono FLAC. The script is the reference text, so the same files give WER too. Keep them outside the repository (for example `C:\bench\5min\pl.flac` + `pl.txt`, `en.flac` + `en.txt`); recordings are never committed (`.gitignore` excludes `*.flac` and `*.wav`). Readers agree to the use of their voice for this purpose.

**D. Peak RAM**

Windows keeps the peak working set of a process, so no sampling is needed. While the benchmark process is still running near its end, or right after a transcription in the app (before closing it):

```powershell
Get-Process python, pythonw -ErrorAction SilentlyContinue |
  Select-Object Id, ProcessName, @{n="PeakGB"; e={[math]::Round($_.PeakWorkingSet64 / 1GB, 2)}}
```

On Intel iGPUs the model lives in shared memory; also note **Shared GPU memory** from Task Manager (Performance, GPU) during transcription. Report both.

**E. End-to-end check in the app**

Start a recording in Ucho, play the 5-minute PL file on the laptop (it lands on the system audio track), or hold a real 5-minute test call with headphones, then stop and note: transcription time shown, device in `meta.json`, any errors in `ucho.log`. This catches problems the command-line tools do not (audio devices, GUI freezes).

### Results

| Date | Ucho version | ID | Install | Recommended | Speed (bench) | 5 min PL | 5 min EN | Peak RAM | Notes |
|---|---|---|---|---|---|---|---|---|---|
| | | | | | | | | | |

## 2. Transcription quality: `tools/benchmark_wer.py`

### What it does

For every audio file in a folder that has a same-name reference text, it transcribes the file through the same code path as the app (`ucho.transcribe`: silence cutting, chunking, restricted language detection, hallucination filter), writes `<name>.hyp.txt`, and computes:

- **WER**: word-level Levenshtein distance (substitutions + deletions + insertions) divided by the number of reference words.
- **CER**: the same on characters.

Both texts are normalized first: Unicode NFKC, lowercase, punctuation and symbols removed (apostrophes join, hyphens split), whitespace collapsed. Totals are corpus-level (all errors over all reference words), so long files weigh more than short ones. The definitions match jiwer's; the implementation is a few dozen lines of standard library code, so scoring needs no extra dependency and `--wer-only` runs in the dev environment without OpenVINO. Unit tests: `tests/test_wer.py`.

Results go to a table on screen and to JSON (`<folder>/wer_results.json` by default) with backend, model, device actually used, load time, peak working set, per-file counts and totals.

### Folder layout

```
C:\bench\cv-pl\
    common_voice_pl_123.mp3     audio: .wav, .flac or .mp3 (soundfile reads MP3)
    common_voice_pl_123.txt     reference, UTF-8
    common_voice_pl_123.hyp.txt hypothesis, written by the tool
```

### Usage

Run it with the Python of an installed Ucho (it has OpenVINO and the models), from a checkout of the repository:

```powershell
$py = "C:\Users\<you>\Ucho\app\.venv\Scripts\python.exe"
& $py tools\benchmark_wer.py C:\bench\cv-pl --models-dir C:\Users\<you>\Ucho\app\models --language pl
& $py tools\benchmark_wer.py C:\bench\cv-pl --language auto --json C:\bench\cv-pl-auto.json
& $py tools\benchmark_wer.py C:\bench\cv-pl --wer-only                  # rescore, no transcription
& $py tools\benchmark_wer.py C:\bench\cv-pl --wer-only --baseline C:\bench\v0.1.json
```

Options: `--backend` (`whisper`, the default, or the optional `parakeet`, see above), `--model` (model repo id, default: the backend's default), `--threads` (CPU threads for Parakeet), `--device` (GPU, CPU, NPU, AUTO; GPU falls back to CPU), `--language` (auto, pl, en), `--limit N`, `--json`, `--baseline` (prints the change in percentage points), `--models-dir` (Hugging Face cache with the models; default `HF_HOME`, else `<repo>\models`), `--from-tsv` (see below).

The tool works offline: it sets `HF_HUB_OFFLINE=1` and loads models from the local cache only. The model is loaded before the first file, so the per-file speed is not distorted by the one-off GPU compilation. The compiled GPU model is cached in `<checkout>\models\ovcache` (ignored by git, about 3 GB for large-v3); delete it when you are done.

### Test data that may legally be used

Nothing is downloaded by the tool and no audio is committed to the repository. Get the data by hand:

**Mozilla Common Voice, Polish (recommended).** Released under **CC0** (public domain), so it can be used for benchmarking without restrictions on the results.

1. Go to <https://commonvoice.mozilla.org/pl/datasets>, choose Polish and the latest corpus version, enter an e-mail address and accept the terms. The terms include a promise **not to try to identify the speakers**; keep to it.
2. Unpack `cv-corpus-<version>-pl.tar.gz`. Inside `pl\` are `clips\*.mp3` and TSV files; use `test.tsv` (the official test split). Whisper's training data is not public and may overlap with Common Voice, so treat absolute numbers as optimistic and use them to compare versions and models.
3. Copy a fixed subset of clips to a working folder, for example the first 300 rows of `test.tsv`:

   ```powershell
   $cv = "C:\data\cv-corpus-<version>\pl"
   New-Item -ItemType Directory -Force C:\bench\cv-pl | Out-Null
   Import-Csv "$cv\test.tsv" -Delimiter "`t" | Select-Object -First 300 |
     ForEach-Object { Copy-Item "$cv\clips\$($_.path)" C:\bench\cv-pl\ }
   ```

4. Write the reference texts from the TSV:

   ```powershell
   & $py tools\benchmark_wer.py C:\bench\cv-pl --from-tsv "$cv\test.tsv"
   ```

Record the corpus version and the number of clips in the results table: WER is only comparable on the same subset.

**Alternatives** (check the licence on the download page before use; both require attribution):

- **FLEURS** (Google), Polish subset, CC BY 4.0: read Wikipedia sentences, longer than Common Voice clips.
- **Multilingual LibriSpeech**, Polish, CC BY 4.0: audiobook segments of 10 to 20 s.

**Own recordings: local only.** Short excerpts of your own meetings, with the participants' consent, are the best test of real conditions (two speakers, Teams compression, PL/EN switching). They stay on your disk: never commit them, never attach them to issues, and publish only the WER numbers.

### Reading the numbers

- Common Voice clips are single read sentences of 3 to 6 s. They test word accuracy and short-utterance language detection, not long meetings. `--language auto` vs `--language pl` on the same set shows how much the auto-detect costs; on Polish data the difference should be small thanks to the restricted language list.
- Numbers are not normalized: Whisper writes "5", references usually spell "pięć". This inflates WER slightly and equally for every model, so comparisons between versions stay fair.
- References of Common Voice can contain typos. Look at the worst files (`.hyp.txt` next to `.txt`) before blaming the model.

### Regression check before a release

1. Run the fixed Common Voice subset with the default model and device on the reference laptop (L1), `--language pl` and `--language auto`.
2. Compare with the previous release's JSON using `--baseline`.
3. A rise of more than **1 percentage point** in WER on the same subset blocks the release until explained (model revision change in `config.REVISIONS`, a change in `transcribe.py` thresholds, an OpenVINO upgrade).
4. Add a row below.

| Date | Ucho version | Model (revision) | Device | Data (version, clips) | Language | WER | CER | Speed |
|---|---|---|---|---|---|---|---|---|
| | | | | | | | | |
