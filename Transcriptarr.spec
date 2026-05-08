# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Transcriptarr.exe (the wizard + launcher).

Build with:
    pyinstaller Transcriptarr.spec --clean --noconfirm

Output: dist\\Transcriptarr.exe (single self-contained file).

Notes
-----
- This builds the WIZARD only, not the WhisperX-laden app itself. The app's
  heavy dependencies (PyTorch, faster-whisper, pyannote, etc.) are installed
  by the wizard at runtime into a venv inside the user's chosen install
  folder. So this .exe stays small (~30-40 MB) and bundles only:
    * Python + stdlib (PyInstaller runtime)
    * customtkinter (UI)
    * tkinterdnd2 is NOT bundled here - the wizard never needs it; it
      gets pip-installed into the user's venv during setup.
    * The transcriptarr.py source file (so the wizard can copy it into
      the install folder).
- We use a one-file build (`exe = EXE(...)`) because the wizard is small
  enough that startup latency from unpacking is negligible (~1-2 seconds).
- Console is hidden so users get a GUI experience.
"""

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

# customtkinter ships data files (themes JSON, fonts) that PyInstaller
# misses by default. collect_all picks them up.
ctk_datas, ctk_binaries, ctk_hiddenimports = collect_all("customtkinter")

# Files we want embedded inside the .exe so the wizard can extract them
# at runtime via sys._MEIPASS.
#
# IMPORTANT: dist/update.exe must already exist before building this spec.
# build.bat enforces that order: it builds Update.spec first, then this one.
import os as _os
_datas_extra = [
    ("transcriptarr.py", "."),    # the app source - copied into install dir on setup
    ("transcriptarr.ico", "."),   # window/taskbar icon for the installed app
]
if _os.path.exists("dist/update.exe"):
    _datas_extra.append(("dist/update.exe", "."))   # bundled updater binary
else:
    print("[Transcriptarr.spec] WARNING: dist/update.exe not found. "
          "Build Update.spec first, or run build.bat which does both in order.")

datas = list(_datas_extra)
datas += ctk_datas

hiddenimports = list(ctk_hiddenimports) + collect_submodules("customtkinter") + [
    "tkinter",
    "tkinter.ttk",
    "tkinter.filedialog",
    "tkinter.messagebox",
]

a = Analysis(
    ["wizard.py"],
    pathex=[],
    binaries=ctk_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Keep the .exe lean - these are not needed in the wizard.
        "torch", "torchaudio", "torchvision",
        "whisperx", "faster_whisper", "ctranslate2",
        "pyannote", "pyannote.audio", "pyannote.core",
        "speechbrain", "transformers", "huggingface_hub",
        "lightning", "lightning_fabric", "pytorch_lightning",
        "numba", "llvmlite", "soundfile", "librosa",
        "tkinterdnd2",
        "matplotlib", "scipy", "pandas",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# One-file build: the .exe is fully self-contained and unpacks to a
# temp folder on launch.
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="Transcriptarr",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,            # UPX can trip antivirus; off for safety
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,        # hide the console window - GUI only
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="transcriptarr.ico",   # custom icon used by Windows for the .exe
)
