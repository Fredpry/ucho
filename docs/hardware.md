# Hardware detection and model choice

Ucho picks the Whisper model and the device (GPU or CPU) for you. The installer runs `python -m ucho.hardware --save` after installing the packages and downloads the model it recommends. The first-run wizard shows the same recommendation with its reasons and can measure it on the spot (*Measure the speed*); Settings → **Hardware** shows the profile, the recommendation, a *Measure again* button and the manual choice of device (GPU, CPU, NPU, AUTO) and model. Choosing the NPU asks first, because its first compile takes about 3 minutes.

Code: `ucho/hardware.py`. Tests: `tests/test_hardware.py`.

## Why rules and a measurement, not a "decision model"

A small ML model could in theory predict the best configuration from hardware facts. We decided against it:

- It would need training data from hundreds of real machines, measured the same way. We do not have that data, and collecting it would mean telemetry, which Ucho does not do.
- Its answer could not be explained. "The model said so" is a bad answer when transcription is slow.
- The decision space is tiny: four Whisper models, two or three devices. A table covers it.

So the choice has three explicit steps:

1. **Profile** (`profile()`): facts from Windows and OpenVINO. RAM (`GlobalMemoryStatusEx`, plus `GetPhysicallyInstalledSystemMemory` for the nominal amount), CPU name (registry), physical and logical cores, AVX2 and AVX-512 (`IsProcessorFeaturePresent`), OpenVINO devices with full name, integrated or discrete, GPU memory and micro-architecture, NPU driver version, and display adapters from the registry (to tell you when an NVIDIA or AMD card cannot be used). Every probe may fail without breaking the others; without OpenVINO the profile simply has no devices.
2. **Rules** (`recommend()`): a pure function over the profile, unit tested on fake profiles. Every recommendation carries the reasons that led to it.
3. **Measurement** (`benchmark()` and `after_benchmark()`): the recommended model runs on a synthetic signal. If it is slower than 1.5x real time, the choice goes one row down.

## Rule table

| Condition | Transcription model | Device | Dictation model |
|---|---|---|---|
| RAM < 8 GB (checked first) | large-v3-turbo INT4 | CPU | large-v3-turbo INT4, with a warning |
| Intel GPU Xe or newer, RAM >= 16 GB | large-v3 INT8 | GPU | large-v3 FP16 |
| Intel GPU Xe or newer, RAM 8-15 GB | large-v3 INT8 | GPU | large-v3 INT8 (shared) |
| no usable GPU, AVX-512 or >= 8 physical cores, RAM >= 16 GB | large-v3 INT8 | CPU | large-v3-turbo INT8 |
| no usable GPU, weaker CPU or RAM < 16 GB | large-v3-turbo INT8 | CPU | large-v3-turbo INT4 |
| NPU present | as above; the NPU is mentioned as an experimental option | | |

Details:

- **RAM** is the installed amount when Windows reports it, otherwise the visible amount plus 1 GB, because firmware and the iGPU reserve part of it (a 16 GB laptop shows 15.3-15.8 GB).
- **Usable GPU** means a GPU that OpenVINO lists (its GPU plugin drives only Intel GPUs) with micro-architecture 12 or newer (Xe-LP in Iris Xe and 11th gen Core, Xe2 in Arc 140V). Older HD/UHD 6xx graphics are too slow for large-v3 and fall through to the CPU rows. A discrete Arc card is preferred over the integrated one (`GPU.1` instead of `GPU.0`). If the architecture cannot be read, the GPU counts as usable and the measurement decides.
- **Cores** are physical cores. Hyper-threading adds little to matrix-heavy inference, and a 4-core laptop with 8 threads is clearly too slow for large-v3 on the CPU.
- **NVIDIA and AMD cards** are listed in the reasons as unsupported: OpenVINO cannot use them.

The dictation model column is a default, not a rule you must follow: Settings → Dictation → *Dictation model* can be "Same as transcription" (one model in memory) or any model. On 20 synthetic Polish dictations INT8 and FP16 gave the same text (see [benchmarks.md](benchmarks.md#int8-vs-fp16-for-dictation-v02)), while FP16 needs about 3 GB more memory and took 37 % longer.

## Step down after the measurement

The rule is accepted when the measurement is at least **1.5x faster than real time**. Otherwise the recommendation moves one row down:

| From | To |
|---|---|
| large-v3 INT8 on CPU | large-v3-turbo INT8 on CPU |
| large-v3-turbo INT8 on CPU | large-v3-turbo INT4 on CPU |
| large-v3 INT8 on GPU | large-v3-turbo INT8 on the same GPU (dictation shares it) |
| large-v3-turbo INT8 on GPU | large-v3-turbo INT4 on the same GPU |

On the GPU we keep the device and take a lighter model, because the next row of the table (large-v3 on the CPU) would be slower still. At the bottom of the ladder the choice stays and you get a warning that transcription will be slow.

## How the measurement works

`benchmark(model, device, seconds=10)` loads the model through the same `transcribe.Engine` as a real transcription (so the GPU to CPU fallback and the restricted PL/EN language detection apply), makes one warm-up call, and times one call on 10 seconds of a synthetic, deterministic speech-like signal built from vowel formants. No recording of yours is used.

Whisper emits only one or two tokens on such a signal and stops, which would leave out the decoder, the most expensive part on real speech. The measurement therefore suppresses the end-of-text token and lets the decoder run a fixed 5 tokens per second of audio. That number comes from the Whisper tokenizer: a Polish business paragraph needs 2.56 tokens per word, an English one 1.23, so dense Polish speech at 130 words per minute is about 5.5 tokens per second and English about 3. Real meetings have pauses, which Ucho cuts before the model, so 5 is on the safe side.

Measured on a Core Ultra 7 258V (Arc 140V, 32 GB, OpenVINO 2026.3.1), 10 s of signal, 50 forced tokens:

| Model | Device | Load (cached) | Processing | Speed |
|---|---|---|---|---|
| large-v3 INT8 | GPU | 1.6-2.1 s | 1.11-1.12 s | 8.9-9.0x real time |
| large-v3 FP16 | GPU | 11.6 s | 1.56 s | 6.4x |
| large-v3 INT8 | CPU | 4.5 s | 5.45 s | 1.8x |
| large-v3 INT8 | NPU | 192.6 s (first compile) | 1.62 s | 6.2x |

On this laptop large-v3 INT8 also passes on the CPU (1.8x), but only just; the GPU is five times faster. The NPU is about as fast as the GPU once compiled, but the first compile took over three minutes, which is why it stays an experimental option.

Without forcing the decoder the same GPU run took 0.42 s (24x), which overstated the speed about 2.5 times. For comparison, a real 85 s recording took 5.7 s on this GPU (15x), with its pauses cut.

## Report your hardware

When you report a bug or a slow transcription, attach the output of:

```powershell
.\.venv\Scripts\python.exe -m ucho.hardware             # profile and recommendation
.\.venv\Scripts\python.exe -m ucho.hardware --benchmark # plus the measurement (model must be downloaded)
```

The output contains only hardware names, driver versions and the recommendation: no serial numbers, user names or paths. Other options:

- `--save` writes `model`, `device`, `dictation_model` and a short `hardware_profile` summary to `settings.json`.
- `--save --keep-choice` (used when you run the installer again) overwrites only values that still hold the previous automatic recommendation. A model or device you changed by hand stays; a device that was CPU only because of an old GPU driver moves to the GPU after the driver update. Settings from v0.1, which have no recorded recommendation, are left as they are.
- `--seconds N` changes the length of the measured signal.

Nothing in this module uses the network.
