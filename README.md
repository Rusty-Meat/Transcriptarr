# Transcriptarr

Local audio transcription with speaker labels. Runs entirely on your computer, no audio is ever sent to a cloud service.

Built on [WhisperX](https://github.com/m-bain/whisperX) (transcription) and [pyannote.audio](https://github.com/pyannote/pyannote-audio) (speaker diarization), wrapped in a modern [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) UI.

![status](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)
![python](https://img.shields.io/badge/python-3.11-blue)

---

## Quick install (Windows)

1. Go to the [Releases page](https://github.com/Rusty-Meat/transcriptarr/releases) and download the latest **`Transcriptarr.exe`**.
2. Double-click it.
3. Windows will probably show **"Windows protected your PC"**. This is normal for unsigned hobby apps. Click **More info → Run anyway**. *(See [Why does Windows warn me?](#why-does-windows-warn-me) for details.)*
4. The setup wizard will guide you through the rest:
   - Picks an install folder (defaults to `%LOCALAPPDATA%\Transcriptarr`, no admin needed).
   - Detects whether you have an NVIDIA GPU and installs the right PyTorch wheel.
   - Downloads about 1 GB (CPU only) or 4 GB (GPU). Mostly PyTorch.
   - Optionally takes a HuggingFace token so speaker labels work.
   - Optionally creates desktop / Start Menu shortcuts.
5. When the wizard finishes, the app launches automatically.

After install, just run `Transcriptarr.exe` (or the shortcut). It skips the wizard and goes straight to the app.

---

## What you get

- Drag any audio or video file onto the window (`.m4a`, `.mp3`, `.wav`, `.flac`, `.mp4`, `.mkv`, and lots more)
- Whisper model size selector: `tiny` / `base` / `small` / `medium` / `large-v2` / `large-v3`
- Optional speaker labels (`[SPEAKER 1]: ...`)
- GPU/CPU dropdown so you can switch between fast and battery-saving modes
- Save transcript as `.md` or copy to clipboard
- Live progress bar with ETA
- 8 themes, both dark and light variants for each
- Logs to `%LOCALAPPDATA%\Transcriptarr\logs\` for debugging
- Fully offline after the one-time model download

---

## System requirements

- **Windows 10 or 11** (64-bit). The bundled installer is Windows-only.
- **About 5 GB of free disk space** (PyTorch + Whisper models + ffmpeg).
- **NVIDIA GPU optional but recommended** for speed. Without one the app runs on CPU. Works fine, just slower.
- **Internet connection** for the initial download. After that, the app runs offline.

The wizard handles installing Python, ffmpeg, PyTorch, and WhisperX for you. You don't need any of those installed beforehand.

---

## Speaker labels (optional setup)

If you want `[SPEAKER 1]:` / `[SPEAKER 2]:` labels in your transcripts, you need a free HuggingFace token AND you have to accept the user conditions on three pyannote model pages. The setup wizard walks you through this. If you skip it, transcription still works, just without labels.

The three model pages that need acceptance:

- [pyannote/segmentation-3.0](https://huggingface.co/pyannote/segmentation-3.0)
- [pyannote/speaker-diarization-3.1](https://huggingface.co/pyannote/speaker-diarization-3.1)
- [pyannote/speaker-diarization-community-1](https://huggingface.co/pyannote/speaker-diarization-community-1)

You can re-open the app's **Settings** dialog later to paste your token if you skipped during setup.

---

## GPU support

If you have an NVIDIA GPU with current drivers, the wizard installs the CUDA build of PyTorch automatically. The app shows `Device: GPU` in green.

If your GPU isn't detected (no NVIDIA card, missing/old driver, laptop with discrete GPU disabled, etc.), the wizard falls back to the CPU build. The app shows `Device: CPU` in amber. You can still transcribe; it's just much slower (often 5x to 15x slower than GPU on the same model size).

If you have an NVIDIA card but it's not detected, install the latest driver from [nvidia.com/Download](https://www.nvidia.com/Download/index.aspx) and re-run the wizard.

---

## Running from source (developers)

If you'd rather not use the .exe:

```bat
git clone https://github.com/Rusty-Meat/transcriptarr.git
cd transcriptarr

py -3.11 -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
pip install whisperx
pip uninstall -y torch torchaudio torchvision
pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cu128
pip install customtkinter tkinterdnd2 hf_xet

python transcriptarr.py
```

For CPU-only:
```bat
pip install torch==2.8.0 torchaudio==2.8.0 --index-url https://download.pytorch.org/whl/cpu
```

You'll also need [ffmpeg](https://www.gyan.dev/ffmpeg/builds/) somewhere on your PATH.

---

## Troubleshooting

### Why does Windows warn me?

The first time you run `Transcriptarr.exe`, Windows SmartScreen will show a blue dialog: **"Windows protected your PC"** with a Don't run button. This is because the executable isn't signed with a paid Authenticode certificate. To run it anyway:

1. Click **More info** under the title
2. Click **Run anyway** at the bottom

This is a one-time prompt per machine. After that, Windows trusts it.

If you're concerned about safety, the entire source code is in this repo, the build pipeline runs publicly via [GitHub Actions](https://github.com/Rusty-Meat/transcriptarr/actions) (see `.github/workflows/release.yml`), and you can also run from source as shown above. Code signing via SignPath Foundation is on the roadmap and will be enabled once the project is enrolled.

### "ffmpeg not found" / "could not decode the audio file"

Setup should have downloaded ffmpeg into `<install folder>\ffmpeg\bin\`. The app prepends that to PATH when it launches. If it's missing, run `update.exe` (when available) or rerun the wizard via `Transcriptarr.exe --force-wizard` from a command prompt.

### Diarization fails with `401` or `Repository not found`

Either your HuggingFace token is missing/wrong, or you didn't accept the user conditions on all three pyannote pages. Open the app's **Settings**, paste your token, and visit all three URLs to click "Agree". See [Speaker labels](#speaker-labels-optional-setup) above.

### Out of GPU memory

Use a smaller Whisper model (`small` or `medium`) or switch to CPU via the dropdown. Large models need ~10 GB of VRAM.

### App launches the wizard every time

The tracker file at `%APPDATA%\Transcriptarr\install.json` got deleted or corrupted. Re-running `Transcriptarr.exe` will rebuild it through the wizard. Existing files in the install folder are reused.

### Where do I find logs?

- Setup logs: `%APPDATA%\Transcriptarr\setup.log`
- App logs: `%LOCALAPPDATA%\Transcriptarr\logs\transcriptarr.log` (or whatever folder you chose during install)

Send the most recent log when reporting a bug.

### How do I uninstall?

The wizard doesn't ship an uninstaller yet. Manually:

1. Delete the install folder (default: `%LOCALAPPDATA%\Transcriptarr\`)
2. Delete `%APPDATA%\Transcriptarr\` (tracker, setup logs)
3. Delete the desktop / Start Menu shortcuts you created
4. Optionally delete model caches: `%USERPROFILE%\.cache\huggingface\`

Or run from a command prompt: `Transcriptarr.exe --uninstall`. This just removes the tracker so a re-run shows the wizard again.

---

## License

TBD.

## Credits

- [WhisperX](https://github.com/m-bain/whisperX) by Max Bain et al.
- [pyannote.audio](https://github.com/pyannote/pyannote-audio) by Hervé Bredin et al.
- [CustomTkinter](https://github.com/TomSchimansky/CustomTkinter) by Tom Schimansky
- [tkinterdnd2](https://github.com/pmgagne/tkinterdnd2) for drag-and-drop
- [ffmpeg-builds](https://www.gyan.dev/ffmpeg/builds/) by gyan.dev
