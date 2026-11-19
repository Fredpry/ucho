# TEMPORARY: macOS CI spike (task M3a, issue #22)

These files are a throwaway experiment, not part of Ucho. They answer yes/no questions
about macOS runners before the macOS tasks start: wheels for Python 3.12 arm64, Tk and
customtkinter, pystray next to Tk, Carbon hotkeys, `say`, whisper.cpp through
`pywhispercpp`, Core Audio Process Tap through `catap`, and a minimal PyInstaller build.

- `spike.py`: the checks; each runs in its own subprocess (a crash is a result, not a stop).
- `pyi_probe.py`: the smallest frozen app (Tk + pywhispercpp) for the PyInstaller check.
- `.github/workflows/macos-spike.yml`: runs them on `macos-15` and `macos-26` for pushes to
  `macos/m3a` and on manual dispatch.

Results and decisions: `docs/macos-spike.md`. Delete this folder and the workflow once
M3b has its own `macos.yml`.
