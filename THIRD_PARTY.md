# Third-party components

Ucho's own code is Apache-2.0. The installer downloads the components below; they are not stored in this repository and keep their own licenses. Licenses were read from the installed package metadata and the Hugging Face model cards on 2026-11-14. Check the upstream project if in doubt.

## Python packages (`requirements.txt`)

| Package | Version | License |
|---|---|---|
| openvino | 2026.4.1 | Apache-2.0 |
| openvino-genai | 2026.4.1.0 | Apache-2.0 |
| openvino-tokenizers | 2026.4.1.0 | Apache-2.0 |
| openvino-telemetry (dependency of openvino, not used by Ucho) | 2025.2.0 | Apache-2.0 |
| huggingface_hub | 1.30.0 | Apache-2.0 |
| PyAudioWPatch (bundles PortAudio, MIT) | 0.2.12.8 | Apache-2.0 |
| soxr | 1.1.0 | LGPL-2.1-or-later |
| soundfile (Windows wheel bundles libsndfile, LGPL-2.1) | 0.14.0 | BSD-3-Clause |
| numpy | 2.5.4 | BSD-3-Clause and others (0BSD, MIT, Zlib, CC0-1.0) |
| customtkinter | 6.0.0 | MIT (PyPI classifier; the metadata license field says CC0-1.0) |
| pillow | 13.0.0 | MIT-CMU |
| pystray | 0.19.5 | LGPL-3.0 |

Optional, only with `install.ps1 -WithParakeet` (`requirements-parakeet.txt`):

| Package | Version | License |
|---|---|---|
| sherpa-onnx (k2-fsa) | 2.0.1 | Apache-2.0 |
| sherpa-onnx-core (native libraries of sherpa-onnx; bundles ONNX Runtime, MIT) | 2.0.1 | Apache-2.0 |

The LGPL libraries are installed as separate, unmodified packages in a normal virtual environment and imported at run time, so you can replace them with any compatible version.

## Models (Hugging Face, pinned revisions in `ucho/config.py`, `REVISIONS`)

| Model | Used for | License |
|---|---|---|
| OpenVINO/whisper-large-v3-int8-ov (default) | transcription | Apache-2.0 |
| OpenVINO/whisper-large-v3-fp16-ov | dictation (separate model, recommended with 16 GB RAM or more) | Apache-2.0 |
| OpenVINO/whisper-large-v3-turbo-int8-ov | transcription, optional | MIT |
| OpenVINO/whisper-large-v3-turbo-int4-ov | transcription, optional | MIT |
| OpenVINO/Qwen3-8B-int4-ov | summaries, optional | Apache-2.0 |
| OpenVINO/Qwen3-4B-int4-ov | summaries and meeting titles, optional | Apache-2.0 |
| OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov | summaries, optional | Apache-2.0 |

| csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3 | transcription and dictation, optional (Parakeet backend, only with `-WithParakeet`) | CC BY 4.0 |
| csukuangfj/sherpa-onnx-nemo-parakeet-tdt-0.6b-v3-int8 | measurements only (`tools/benchmark_wer.py`), not offered in the app | CC BY 4.0 |

Whisper was created by OpenAI; the OpenVINO conversions are published by Intel. Qwen models are by the Qwen team, Alibaba Cloud.

**Parakeet attribution (CC BY 4.0).** The Parakeet models are [parakeet-tdt-0.6b-v3](https://huggingface.co/nvidia/parakeet-tdt-0.6b-v3) by NVIDIA, licensed under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) ("ready for commercial/non-commercial use" per the model card). The ONNX exports Ucho downloads were made by the sherpa-onnx project, unmodified by Ucho. NVIDIA does not endorse Ucho. CC BY 4.0 lets anyone use and redistribute the weights, also commercially, as long as this credit and the license link are kept.

## Installer

`Ucho-Setup.exe` is built with [Inno Setup](https://jrsoftware.org/isinfo.php) 6.7.3 (Inno Setup License, a permissive license that allows redistributing the setup and uninstaller it produces). The setup program contains Inno Setup's runtime, not modified by Ucho.

## Runtime

Python 3.12 (PSF License), installed by the installer through `winget` when missing.
