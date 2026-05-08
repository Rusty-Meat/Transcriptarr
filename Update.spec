# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for update.exe (the Transcriptarr updater & repair tool).

Build with:
    pyinstaller Update.spec --clean --noconfirm

Output: dist\\update.exe (single self-contained file).

This is built BEFORE Transcriptarr.exe, because Transcriptarr.spec then
bundles dist\\update.exe as a data file inside Transcriptarr.exe so the
wizard can extract it during install.
"""

from PyInstaller.utils.hooks import collect_all, collect_submodules

block_cipher = None

# customtkinter ships data files (themes JSON, fonts) PyInstaller misses by default.
ctk_datas, ctk_binaries, ctk_hiddenimports = collect_all("customtkinter")

datas = list(ctk_datas)
hiddenimports = list(ctk_hiddenimports) + collect_submodules("customtkinter") + [
    "tkinter",
    "tkinter.ttk",
    "tkinter.filedialog",
    "tkinter.messagebox",
]

a = Analysis(
    ["update.py"],
    pathex=[],
    binaries=ctk_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Updater doesn't need the heavy ML deps; those live in the user's venv.
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

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="update",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon="transcriptarr.ico",
)
