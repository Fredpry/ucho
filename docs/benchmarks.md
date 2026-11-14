# Benchmarks

How fast and how accurately Ucho transcribes on different laptops, and how to measure it again before a release. Two parts:

1. **Hardware matrix**: does the installer work and how fast is each model on each device, on at least three kinds of laptop.
2. **Transcription quality regression**: word error rate (WER) on public Polish speech with `tools/benchmark_wer.py`.

Both are run by hand. Neither runs in CI (no models, no GPU, no audio there).

## Known numbers (v0.1)

Measured on the development laptop, Intel Core Ultra 7 258V with Arc 140V, 32 GB RAM, Whisper large-v3 INT8 (from `README.md`):

| | GPU (Arc 140V) | CPU |
|---|---|---|
| 85 s recording, forced language | 5.7 s (15x faster than real time) | 35 s (2.4x) |
| 51 s recording, PL/EN auto-detect | 3.7 s (14x) | 47 s (1.1x) |
| First model load | ~21 s (compilation, then ~2 s from cache) | ~6 s |

A one-hour meeting transcribes in about 4 minutes on the Arc GPU. Auto-detect is slower on the CPU because the recording is cut into ~12 s chunks, each with its own language detection (see `docs/architecture.md`, section 3).

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

Results go to a table on screen and to JSON (`<folder>/wer_results.json` by default) with model, device actually used, load time, per-file counts and totals.

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

Options: `--model` (OpenVINO repo id, default: the app default), `--device` (GPU, CPU, NPU, AUTO; GPU falls back to CPU), `--language` (auto, pl, en), `--limit N`, `--json`, `--baseline` (prints the change in percentage points), `--models-dir` (Hugging Face cache with the models; default `HF_HOME`, else `<repo>\models`), `--from-tsv` (see below).

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
