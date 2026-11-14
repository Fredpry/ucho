# Third-party components

Ucho's own code is Apache-2.0. The installer downloads the components below; they are not stored in this repository and keep their own licenses. Licenses were read from the installed package metadata and the Hugging Face model cards on 2026-11-14. Check the upstream project if in doubt.

## Python packages (`requirements.txt`)

| Package | Version | License |
|---|---|---|
| openvino | 2026.3.1 | Apache-2.0 |
| openvino-genai | 2026.3.1.0 | Apache-2.0 |
| openvino-tokenizers | 2026.3.1.0 | Apache-2.0 |
| openvino-telemetry (dependency of openvino, not used by Ucho) | 2025.2.0 | Apache-2.0 |
| huggingface_hub | 1.30.0 | Apache-2.0 |
| PyAudioWPatch (bundles PortAudio, MIT) | 0.2.12.8 | Apache-2.0 |
| soxr | 1.1.0 | LGPL-2.1-or-later |
| soundfile (Windows wheel bundles libsndfile, LGPL-2.1) | 0.14.0 | BSD-3-Clause |
| numpy | 2.5.3 | BSD-3-Clause and others (0BSD, MIT, Zlib, CC0-1.0) |
| customtkinter | 6.0.0 | MIT (PyPI classifier; the metadata license field says CC0-1.0) |
| pillow | 12.3.0 | MIT-CMU |
| pystray | 0.19.5 | LGPL-3.0 |
| pynput | 1.8.2 | LGPL-3.0 |

The LGPL libraries are installed as separate, unmodified packages in a normal virtual environment and imported at run time, so you can replace them with any compatible version.

## Models (Hugging Face, pinned revisions in `ucho/config.py`)

| Model | Used for | License |
|---|---|---|
| OpenVINO/whisper-large-v3-int8-ov (default) | transcription | Apache-2.0 |
| OpenVINO/whisper-large-v3-fp16-ov | transcription (default in Dictate) | Apache-2.0 |
| OpenVINO/whisper-large-v3-turbo-int8-ov | transcription, optional | MIT |
| OpenVINO/whisper-large-v3-turbo-int4-ov | transcription, optional | MIT |
| OpenVINO/Qwen3-8B-int4-ov | summaries, optional | Apache-2.0 |
| OpenVINO/Qwen3-4B-int4-ov | summaries, optional | Apache-2.0 |
| OpenVINO/Qwen2.5-1.5B-Instruct-int4-ov | summaries, optional | Apache-2.0 |

Whisper was created by OpenAI; the OpenVINO conversions are published by Intel. Qwen models are by the Qwen team, Alibaba Cloud.

## Runtime

Python 3.12 (PSF License), installed by the installer through `winget` when missing.
