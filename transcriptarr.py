"""Transcriptarr - local audio transcription with speaker diarization.

CustomTkinter UI wrapped around WhisperX. Runs offline after the one-time
model downloads. See requirements.md for setup.
"""

from __future__ import annotations

# Silence known-harmless warnings from upstream libs before heavy imports.
import warnings as _warnings
_warnings.filterwarnings("ignore", message=r".*torchcodec.*")
_warnings.filterwarnings("ignore", message=r".*libtorchcodec.*")
_warnings.filterwarnings("ignore", message=r".*TensorFloat-32.*")
_warnings.filterwarnings("ignore", message=r".*TF32.*")
_warnings.filterwarnings("ignore", message=r".*symlinks.*")
_warnings.filterwarnings("ignore", message=r".*Developer Mode.*")
_warnings.filterwarnings("ignore", message=r".*Xet Storage.*")
_warnings.filterwarnings("ignore", module=r"pyannote\.audio\.core\.io")
_warnings.filterwarnings("ignore", module=r"pyannote\.audio\.utils\.reproducibility")

import logging as _logging
for _name in (
    "lightning_fabric.utilities.migration",
    "lightning_fabric.utilities.migration.utils",
    "pytorch_lightning.utilities.migration",
    "pytorch_lightning.utilities.migration.utils",
    "lightning.pytorch.utilities.migration",
):
    _logging.getLogger(_name).setLevel(_logging.ERROR)

import gc
import json
import os
import queue
import sys
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox

try:
    import customtkinter as ctk
except ImportError:
    raise SystemExit(
        "customtkinter is not installed in this Python environment.\n"
        "Install it with:\n"
        '    "C:\\Second Brain\\Second Brain\\.venv\\Scripts\\python.exe" -m pip install customtkinter'
    )

# Optional: drag-and-drop support. App still works without it; drops just
# won't fire. Install with:
#     pip install tkinterdnd2
try:
    from tkinterdnd2 import TkinterDnD, DND_FILES
    _DND_AVAILABLE = True
except ImportError:
    _DND_AVAILABLE = False


# constants

APP_TITLE = "Transcriptarr"
MODELS = ["tiny", "base", "small", "medium", "large-v2", "large-v3"]
DEFAULT_MODEL = "small"

_AUDIO_EXTS = (
    "m4a m4b m4r mp3 mp2 wav wave aif aiff aifc flac ogg oga opus "
    "wma aac ac3 dts amr au snd caf mka mogg ape alac wv tta voc "
    "w64 ra rm gsm mp1 mpa"
).split()
_VIDEO_EXTS = (
    "mp4 m4v mkv mov avi webm flv wmv 3gp 3g2 ts mts m2ts vob ogv "
    "mpg mpeg mpe asf rmvb f4v divx"
).split()
_ALL_AV = "*." + " *.".join(_AUDIO_EXTS + _VIDEO_EXTS)

SUPPORTED_FILETYPES = [
    ("Audio & video files", _ALL_AV),
    ("All files", "*.*"),
    ("Audio only", "*." + " *.".join(_AUDIO_EXTS)),
    ("Video only (audio track will be transcribed)", "*." + " *.".join(_VIDEO_EXTS)),
    ("M4A", "*.m4a"),
    ("MP3", "*.mp3"),
    ("WAV", "*.wav *.wave"),
    ("FLAC", "*.flac"),
    ("OGG / Opus", "*.ogg *.oga *.opus"),
    ("MP4", "*.mp4 *.m4v"),
    ("MKV", "*.mkv"),
]

if getattr(sys, "frozen", False):
    SCRIPT_DIR = Path(sys.executable).resolve().parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent

TOKEN_FILE = SCRIPT_DIR / ".hf_token"
CONFIG_FILE = SCRIPT_DIR / ".transcriptarr.json"


# logging
# Logs to %LOCALAPPDATA%\Transcriptarr\logs\transcriptarr.log on Windows
# (no admin perms required), with a rotating buffer (~5MB x 3 files).
# Falls back to a `logs` folder next to the script if LOCALAPPDATA isn't set.
# Uncaught exceptions on the main thread AND background threads are
# captured automatically via sys.excepthook / threading.excepthook.

import logging
from logging.handlers import RotatingFileHandler


def _resolve_log_dir() -> Path:
    appdata = os.environ.get("LOCALAPPDATA")
    if appdata:
        candidate = Path(appdata) / "Transcriptarr" / "logs"
    else:
        candidate = SCRIPT_DIR / "logs"
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate
    except Exception:
        # Last-ditch: just dump next to the script
        fallback = SCRIPT_DIR
        try:
            fallback.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return fallback


LOG_DIR = _resolve_log_dir()
LOG_FILE = LOG_DIR / "transcriptarr.log"


def _setup_logging() -> logging.Logger:
    logger = logging.getLogger("transcriptarr")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # don't double-log via root

    # Avoid double-installing handlers if this module is somehow re-imported
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-5s] %(name)s.%(funcName)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        fh = RotatingFileHandler(
            LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3,
            encoding="utf-8", delay=True,
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception as e:
        # File handler unavailable - stderr handler below still runs.
        print(f"[Transcriptarr] log file unavailable: {e}", file=sys.stderr)

    # Console handler (only INFO+ to keep stdout readable)
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(ch)

    return logger


LOG = _setup_logging()


def _install_excepthooks() -> None:
    """Route unhandled exceptions to the log file before crashing."""
    prev_excepthook = sys.excepthook

    def main_excepthook(exc_type, exc_value, exc_tb):
        try:
            LOG.critical("Unhandled exception on main thread",
                         exc_info=(exc_type, exc_value, exc_tb))
        except Exception:
            pass
        prev_excepthook(exc_type, exc_value, exc_tb)

    sys.excepthook = main_excepthook

    # Python 3.8+ thread excepthook
    if hasattr(threading, "excepthook"):
        prev_thread_hook = threading.excepthook

        def thread_excepthook(args):
            try:
                thread_name = args.thread.name if args.thread else "?"
                LOG.critical(
                    "Unhandled exception in thread %s", thread_name,
                    exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
                )
            except Exception:
                pass
            prev_thread_hook(args)

        threading.excepthook = thread_excepthook


_install_excepthooks()
LOG.info("=" * 60)
LOG.info("Transcriptarr starting up")
LOG.info("Log file: %s", LOG_FILE)
LOG.info("Script dir: %s", SCRIPT_DIR)
LOG.info("Python: %s", sys.version.split()[0])


# THEMES

THEMES: dict[str, dict[str, dict[str, str]]] = {
    "Indigo": {
        # Dark mode: indigo-tinted dark with paper softness in the text.
        "dark": {
            "bg_primary":   "#1a1d2c",
            "bg_secondary": "#23263a",
            "bg_input":     "#2d314a",
            "border":       "#3d4361",
            "text":         "#e4dfd0",
            "text_dim":     "#9b9aaf",
            "accent":       "#8b5cf6",   # cosmic purple
            "accent_hover": "#a78bfa",
            "accent_text":  "#ffffff",
            "green":        "#7cc775",
            "amber":        "#ffb74d",
        },
        # Light mode: soft paper aesthetic.
        "light": {
            "bg_primary":   "#ede4c4",
            "bg_secondary": "#f7efd5",
            "bg_input":     "#f7efd5",
            "border":       "#cebb95",
            "text":         "#2a2520",
            "text_dim":     "#6e5e48",
            "accent":       "#3e5a99",
            "accent_hover": "#5572b3",
            "accent_text":  "#ffffff",
            "green":        "#5a7a3d",
            "amber":        "#a86b1c",
        },
    },
    "Forest": {
        "dark": {
            "bg_primary":   "#171c19",
            "bg_secondary": "#1f2622",
            "bg_input":     "#28312c",
            "border":       "#3a463e",
            "text":         "#e8eee9",
            "text_dim":     "#9bafa1",
            "accent":       "#6dbf6d",
            "accent_hover": "#88cf88",
            "accent_text":  "#0e1410",
            "green":        "#a3e0a3",
            "amber":        "#ffb74d",
        },
        "light": {
            "bg_primary":   "#f3f7f3",
            "bg_secondary": "#ffffff",
            "bg_input":     "#ffffff",
            "border":       "#cfdbd1",
            "text":         "#1a221d",
            "text_dim":     "#5e6e62",
            "accent":       "#3a7a3a",
            "accent_hover": "#4d8e4d",
            "accent_text":  "#ffffff",
            "green":        "#1f5d1f",
            "amber":        "#a45f00",
        },
    },
    "Sunset": {
        "dark": {
            "bg_primary":   "#1f1a1c",
            "bg_secondary": "#2a2225",
            "bg_input":     "#352c30",
            "border":       "#473a3e",
            "text":         "#eee8ea",
            "text_dim":     "#b39ca5",
            "accent":       "#ef8b5b",
            "accent_hover": "#f4a37a",
            "accent_text":  "#1a0d05",
            "green":        "#7cc775",
            "amber":        "#ffb74d",
        },
        "light": {
            "bg_primary":   "#fbf5f4",
            "bg_secondary": "#ffffff",
            "bg_input":     "#ffffff",
            "border":       "#e6d5d2",
            "text":         "#1f1216",
            "text_dim":     "#7a5d62",
            "accent":       "#d96f3b",
            "accent_hover": "#e8855a",
            "accent_text":  "#ffffff",
            "green":        "#2e8b2e",
            "amber":        "#a45f00",
        },
    },
    # Slate: clean neutral grayscale with a single subtle blue accent.
    # Inspired by Linear / Notion's professional minimalism.
    "Slate": {
        "dark": {
            "bg_primary":   "#0e0f12",
            "bg_secondary": "#16181d",
            "bg_input":     "#1f2127",
            "border":       "#2c2f37",
            "text":         "#e6e8ed",
            "text_dim":     "#93989f",
            "accent":       "#5e8ad8",
            "accent_hover": "#7aa4eb",
            "accent_text":  "#ffffff",
            "green":        "#6ec77a",
            "amber":        "#e6b958",
        },
        "light": {
            "bg_primary":   "#f7f8fa",
            "bg_secondary": "#ffffff",
            "bg_input":     "#ffffff",
            "border":       "#dfe1e6",
            "text":         "#1c1e22",
            "text_dim":     "#6c7079",
            "accent":       "#3b6dc7",
            "accent_hover": "#5582d4",
            "accent_text":  "#ffffff",
            "green":        "#2d8540",
            "amber":        "#b07000",
        },
    },
    # Tidal: deep ocean teal-cyan. Calm, focused, fresh.
    "Tidal": {
        "dark": {
            "bg_primary":   "#0c1820",
            "bg_secondary": "#14242e",
            "bg_input":     "#1c303c",
            "border":       "#2c4150",
            "text":         "#e0e9ee",
            "text_dim":     "#8fa5b3",
            "accent":       "#4ec9b0",
            "accent_hover": "#67d8c1",
            "accent_text":  "#062028",
            "green":        "#7cc775",
            "amber":        "#ffb74d",
        },
        "light": {
            "bg_primary":   "#eaf2f4",
            "bg_secondary": "#ffffff",
            "bg_input":     "#ffffff",
            "border":       "#c8dde2",
            "text":         "#0e2129",
            "text_dim":     "#5a6e76",
            "accent":       "#1f8c75",
            "accent_hover": "#2da188",
            "accent_text":  "#ffffff",
            "green":        "#1e7d3a",
            "amber":        "#b07000",
        },
    },
    # Mocha: warm coffee-shop browns with caramel accent. Cozy, organic.
    # Distinct from Sunset (which leans peach/orange).
    "Mocha": {
        "dark": {
            "bg_primary":   "#1f1612",
            "bg_secondary": "#2a1f19",
            "bg_input":     "#362a22",
            "border":       "#4a3c30",
            "text":         "#f0e8dd",
            "text_dim":     "#b5a290",
            "accent":       "#d49467",
            "accent_hover": "#e0a780",
            "accent_text":  "#2a1810",
            "green":        "#97b56b",
            "amber":        "#e6b958",
        },
        "light": {
            "bg_primary":   "#f5ebdd",
            "bg_secondary": "#fdf6ea",
            "bg_input":     "#fdf6ea",
            "border":       "#d6c3a8",
            "text":         "#2a1f17",
            "text_dim":     "#6e5c48",
            "accent":       "#8b5a35",
            "accent_hover": "#a06f48",
            "accent_text":  "#ffffff",
            "green":        "#5a7a3d",
            "amber":        "#a86b1c",
        },
    },
    # Nord: the popular Polar Night / Snow Storm / Frost palette.
    # https://www.nordtheme.com/ - cool Arctic blues and grays.
    "Nord": {
        "dark": {
            "bg_primary":   "#2e3440",   # Polar Night 0
            "bg_secondary": "#3b4252",   # Polar Night 1
            "bg_input":     "#434c5e",   # Polar Night 2
            "border":       "#4c566a",   # Polar Night 3
            "text":         "#eceff4",   # Snow Storm 2
            "text_dim":     "#8b95a5",
            "accent":       "#88c0d0",   # Frost - cool cyan
            "accent_hover": "#a3cee0",
            "accent_text":  "#2e3440",
            "green":        "#a3be8c",   # Aurora green
            "amber":        "#ebcb8b",   # Aurora yellow
        },
        "light": {
            "bg_primary":   "#eceff4",   # Snow Storm 2
            "bg_secondary": "#ffffff",
            "bg_input":     "#ffffff",
            "border":       "#d8dee9",   # Snow Storm 0
            "text":         "#2e3440",   # Polar Night 0
            "text_dim":     "#4c566a",   # Polar Night 3
            "accent":       "#5e81ac",   # Frost 3
            "accent_hover": "#6e8eb6",
            "accent_text":  "#ffffff",
            "green":        "#5a8a5f",
            "amber":        "#b07000",
        },
    },
    # Crimson: deep wine on cream parchment. Library / wine-bar mood.
    "Crimson": {
        "dark": {
            "bg_primary":   "#1a1011",
            "bg_secondary": "#241618",
            "bg_input":     "#2e1d20",
            "border":       "#3e2a2d",
            "text":         "#f0e3e3",
            "text_dim":     "#a89395",
            "accent":       "#c44a55",
            "accent_hover": "#d6606a",
            "accent_text":  "#ffffff",
            "green":        "#7cc775",
            "amber":        "#ffb74d",
        },
        "light": {
            "bg_primary":   "#f7eee9",
            "bg_secondary": "#ffffff",
            "bg_input":     "#ffffff",
            "border":       "#e0cdc8",
            "text":         "#2a1416",
            "text_dim":     "#745c5e",
            "accent":       "#9b2c34",
            "accent_hover": "#b03e47",
            "accent_text":  "#ffffff",
            "green":        "#2e8b2e",
            "amber":        "#b07000",
        },
    },
}

DEFAULT_THEME = "Indigo"


XRT_FACTORS = {
    ("tiny",     "cuda"): 50, ("tiny",     "cpu"): 6,
    ("base",     "cuda"): 40, ("base",     "cpu"): 4,
    ("small",    "cuda"): 25, ("small",    "cpu"): 2,
    ("medium",   "cuda"): 12, ("medium",   "cpu"): 0.6,
    ("large-v2", "cuda"): 7,  ("large-v2", "cpu"): 0.2,
    ("large-v3", "cuda"): 6,  ("large-v3", "cpu"): 0.2,
}


def estimate_transcribe_seconds(audio_duration: float, model: str, device: str) -> float:
    rate = XRT_FACTORS.get((model, device), 5.0)
    return max(2.0, audio_duration / max(0.1, rate))


def format_eta(seconds: float) -> str:
    seconds = int(round(seconds))
    if seconds < 60:
        return f"{seconds}s"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


# utilities

def detect_device():
    try:
        import torch
        if torch.cuda.is_available():
            try:
                name = torch.cuda.get_device_name(0)
            except Exception:
                name = "CUDA GPU"
            return "cuda", name
    except Exception:
        pass
    return "cpu", "CPU"


def load_hf_token() -> str | None:
    token = os.environ.get("HF_TOKEN") or os.environ.get("HUGGING_FACE_HUB_TOKEN")
    if token:
        return token.strip()
    if TOKEN_FILE.exists():
        try:
            text = TOKEN_FILE.read_text(encoding="utf-8").strip()
            return text or None
        except Exception:
            return None
    return None


def save_hf_token(token: str) -> None:
    TOKEN_FILE.write_text(token.strip(), encoding="utf-8")


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_config(updates: dict) -> None:
    """Merge `updates` into the existing config file (does NOT clobber other keys)."""
    try:
        cfg = load_config()
        cfg.update(updates)
        CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except Exception:
        pass


def normalize_speaker(label: str | None) -> str:
    if not label:
        return "SPEAKER ?"
    s = str(label)
    if s.upper().startswith("SPEAKER_"):
        try:
            n = int(s.split("_", 1)[1]) + 1
            return f"SPEAKER {n}"
        except Exception:
            return s
    return s


def format_transcript(result: dict) -> str:
    segments = result.get("segments", []) or []
    blocks: list[str] = []
    current_speaker: str | None = None
    current_parts: list[str] = []

    def flush():
        if current_parts:
            label = current_speaker or "SPEAKER ?"
            text = " ".join(p.strip() for p in current_parts if p.strip())
            if text:
                blocks.append(f"[{label}]: {text}")

    for seg in segments:
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        spk = normalize_speaker(seg.get("speaker"))
        if spk != current_speaker:
            flush()
            current_parts = []
            current_speaker = spk
        current_parts.append(text)
    flush()
    return "\n\n".join(blocks)


# App

ctk.set_default_color_theme("blue")


# When tkinterdnd2 is available, mix its DnDWrapper into ctk.CTk for
# both modern theming AND drag-drop. When not available, _CTkRoot is just
# ctk.CTk and drops are silently disabled.
if _DND_AVAILABLE:
    class _CTkRoot(ctk.CTk, TkinterDnD.DnDWrapper):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.TkdndVersion = TkinterDnD._require(self)
else:
    _CTkRoot = ctk.CTk


class TranscriptarrApp(_CTkRoot):
    def __init__(self) -> None:
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1000x740")
        self.minsize(820, 560)

        # Use transcriptarr.ico for the window/taskbar icon if present.
        icon_path = SCRIPT_DIR / "transcriptarr.ico"
        if icon_path.exists():
            try:
                self.iconbitmap(str(icon_path))
            except Exception as e:
                LOG.warning("Could not set window icon: %s", e)

        # Fonts
        self.ui_font      = ctk.CTkFont(family="Bahnschrift", size=14)
        self.ui_font_bold = ctk.CTkFont(family="Bahnschrift", size=14, weight="bold")
        self.heading_font = ctk.CTkFont(family="Bahnschrift", size=16, weight="bold")
        self.mono_font    = ctk.CTkFont(family="Cascadia Mono", size=13)

        # State
        cfg = load_config()
        self.audio_path_var = tk.StringVar(value=cfg.get("last_path", ""))
        self.model_var      = tk.StringVar(value=cfg.get("model", DEFAULT_MODEL))
        self.diarize_var    = tk.BooleanVar(value=cfg.get("diarize", True))
        self.status_var     = tk.StringVar(value="Ready.")
        self.pct_var        = tk.StringVar(value="")

        self.device, self.device_name = detect_device()
        self.cuda_available = (self.device == "cuda")
        self.device_var = tk.StringVar(value="GPU" if self.cuda_available else "CPU")

        self.msg_queue: "queue.Queue[tuple[str, object]]" = queue.Queue()
        self.worker_thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self.last_result: dict | None = None
        self.last_transcript: str = ""

        # Smooth progress
        self.progress_current = 0.0
        self.progress_target = 0.0
        self.progress_ramp_per_sec = 0.0
        self.progress_eta_end: float | None = None
        self._last_tick: float | None = None

        # Theme state
        saved_theme = cfg.get("theme", DEFAULT_THEME)
        if saved_theme not in THEMES:
            saved_theme = DEFAULT_THEME
        self.theme_name = saved_theme
        self.is_dark = bool(cfg.get("dark_mode", True))
        self._palette = self._compute_palette()

        ctk.set_appearance_mode("dark" if self.is_dark else "light")

        LOG.info(
            "App init: theme=%s, mode=%s, device=%s (%s)",
            self.theme_name, "dark" if self.is_dark else "light",
            self.device, self.device_name,
        )

        self.configure(fg_color=self.C("bg_primary"))
        self._build_ui()
        self._poll_queue()
        self._tick_progress()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # Register drag-and-drop on the root window. CTk-composite widgets
        # (CTkEntry, CTkTextbox) can't always be registered directly because
        # they're frames wrapping the real tk widget; the root catches drops
        # anywhere in the window so this is sufficient AND more reliable.
        if _DND_AVAILABLE:
            try:
                self.drop_target_register(DND_FILES)
                self.dnd_bind("<<Drop>>", self._handle_drop)
                self.set_status("Ready. Drag a file onto the window or click Browse.")
                LOG.info("Drag-and-drop enabled.")
            except Exception as e:
                self.set_status(f"Ready. (Drag-drop setup failed: {e})")
                LOG.exception("Drag-and-drop registration failed")
        else:
            self.set_status("Ready. (Drag-drop disabled - tkinterdnd2 not installed.)")
            LOG.warning("tkinterdnd2 not importable; drag-drop disabled.")

    # theming

    def C(self, key: str) -> str:
        return self._palette.get(key, "#ff00ff")

    def _compute_palette(self) -> dict[str, str]:
        theme = THEMES.get(self.theme_name, THEMES[DEFAULT_THEME])
        return theme["dark"] if self.is_dark else theme["light"]

    def apply_theme(self, theme_name: str | None = None,
                    is_dark: bool | None = None) -> None:
        """Switch theme/mode by tearing down and rebuilding the UI."""
        if theme_name is not None and theme_name in THEMES:
            self.theme_name = theme_name
        if is_dark is not None:
            self.is_dark = bool(is_dark)
        LOG.info("Theme change: theme=%s, mode=%s",
                 self.theme_name, "dark" if self.is_dark else "light")

        try:
            saved_text = self.output.get("1.0", "end-1c") if hasattr(self, "output") else ""
        except Exception:
            saved_text = ""

        for child in self.winfo_children():
            try:
                child.destroy()
            except Exception:
                pass

        self._palette = self._compute_palette()
        ctk.set_appearance_mode("dark" if self.is_dark else "light")
        self.configure(fg_color=self.C("bg_primary"))

        self._build_ui()

        if saved_text:
            try:
                self.output.insert("1.0", saved_text)
            except Exception:
                pass

        save_config({"theme": self.theme_name, "dark_mode": self.is_dark})

    # UI build

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(5, weight=1)

        PAD_X = 16
        PAD_Y = 8

        C = self.C

        # File picker row
        file_row = ctk.CTkFrame(self, fg_color="transparent")
        file_row.grid(row=0, column=0, sticky="ew", padx=PAD_X, pady=(PAD_X, PAD_Y))
        file_row.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(file_row, text="Audio file", font=self.ui_font,
                     text_color=C("text_dim"), width=80, anchor="w").grid(
            row=0, column=0, padx=(0, 8))
        self.path_entry = ctk.CTkEntry(
            file_row, textvariable=self.audio_path_var, font=self.ui_font,
            fg_color=C("bg_input"), border_color=C("border"), border_width=1,
            text_color=C("text"), height=36, corner_radius=8,
        )
        self.path_entry.grid(row=0, column=1, sticky="ew", padx=(0, 8))
        ctk.CTkButton(
            file_row, text="Browse", font=self.ui_font, command=self.pick_file,
            width=100, height=36, corner_radius=8,
            fg_color=C("bg_input"), hover_color=C("border"),
            text_color=C("text"), border_width=1, border_color=C("border"),
        ).grid(row=0, column=2)

        # Options row
        opts_row = ctk.CTkFrame(self, fg_color="transparent")
        opts_row.grid(row=1, column=0, sticky="ew", padx=PAD_X, pady=PAD_Y)
        opts_row.grid_columnconfigure(4, weight=1)

        ctk.CTkLabel(opts_row, text="Model", font=self.ui_font,
                     text_color=C("text_dim"), width=80, anchor="w").grid(
            row=0, column=0, padx=(0, 8))
        ctk.CTkOptionMenu(
            opts_row, variable=self.model_var, values=MODELS, font=self.ui_font,
            fg_color=C("bg_input"), button_color=C("bg_input"),
            button_hover_color=C("border"), text_color=C("text"),
            dropdown_fg_color=C("bg_secondary"), dropdown_text_color=C("text"),
            dropdown_hover_color=C("border"), dropdown_font=self.ui_font,
            width=130, height=36, corner_radius=8,
        ).grid(row=0, column=1, padx=(0, 16))

        ctk.CTkCheckBox(
            opts_row, text="Speaker diarization", variable=self.diarize_var,
            font=self.ui_font, text_color=C("text"),
            fg_color=C("accent"), hover_color=C("accent_hover"),
            border_color=C("border"), border_width=2, corner_radius=4,
            checkbox_width=20, checkbox_height=20,
        ).grid(row=0, column=2, padx=(0, 16))

        # Device picker (right side)
        device_frame = ctk.CTkFrame(opts_row, fg_color="transparent")
        device_frame.grid(row=0, column=4, sticky="e")

        ctk.CTkLabel(device_frame, text="Device", font=self.ui_font,
                     text_color=C("text_dim")).pack(side="left", padx=(0, 8))

        device_options = ["GPU", "CPU"] if self.cuda_available else ["CPU"]
        self.device_menu = ctk.CTkOptionMenu(
            device_frame, variable=self.device_var, values=device_options,
            command=self._on_device_change, font=self.ui_font_bold,
            fg_color=C("bg_input"), button_color=C("bg_input"),
            button_hover_color=C("border"),
            dropdown_fg_color=C("bg_secondary"), dropdown_text_color=C("text"),
            dropdown_hover_color=C("border"), dropdown_font=self.ui_font,
            width=90, height=36, corner_radius=8,
        )
        self.device_menu.pack(side="left")
        self._refresh_device_color()

        # Action buttons row
        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.grid(row=2, column=0, sticky="ew", padx=PAD_X, pady=PAD_Y)
        btn_row.grid_columnconfigure(5, weight=1)

        BTN_KW = dict(font=self.ui_font, height=38, corner_radius=8,
                      width=140, border_width=1)

        self.start_btn = ctk.CTkButton(
            btn_row, text="Transcribe", command=self.start_transcription,
            fg_color=C("accent"), hover_color=C("accent_hover"),
            text_color=C("accent_text"), border_color=C("accent"),
            **BTN_KW,
        )
        self.start_btn.grid(row=0, column=0, padx=(0, 6))

        sec_kwargs = dict(fg_color=C("bg_secondary"), hover_color=C("bg_input"),
                          text_color=C("text"), border_color=C("border"), **BTN_KW)

        self.stop_btn = ctk.CTkButton(
            btn_row, text="Cancel", command=self.cancel_transcription,
            state="disabled", **sec_kwargs)
        self.stop_btn.grid(row=0, column=1, padx=6)

        ctk.CTkButton(btn_row, text="Save as .md",
                      command=self.save_markdown, **sec_kwargs).grid(row=0, column=2, padx=6)
        ctk.CTkButton(btn_row, text="Copy to clipboard",
                      command=self.copy_clipboard, **sec_kwargs).grid(row=0, column=3, padx=6)
        ctk.CTkButton(btn_row, text="Clear",
                      command=self.clear_output, **sec_kwargs).grid(row=0, column=4, padx=6)
        ctk.CTkButton(btn_row, text="Settings",
                      command=self.open_settings, **sec_kwargs).grid(row=0, column=6, padx=(6, 0), sticky="e")

        # Progress bar
        prog_row = ctk.CTkFrame(self, fg_color="transparent")
        prog_row.grid(row=3, column=0, sticky="ew", padx=PAD_X, pady=(PAD_Y, 4))
        prog_row.grid_columnconfigure(0, weight=1)

        self.progress = ctk.CTkProgressBar(
            prog_row, height=10, corner_radius=5,
            fg_color=C("bg_input"), progress_color=C("accent"),
            border_width=0,
        )
        self.progress.set(self.progress_current / 100.0)
        self.progress.grid(row=0, column=0, sticky="ew", padx=(0, 12))

        ctk.CTkLabel(prog_row, textvariable=self.pct_var, font=self.ui_font,
                     text_color=C("text_dim"), width=60, anchor="e").grid(
            row=0, column=1)

        # Status
        ctk.CTkLabel(self, textvariable=self.status_var, font=self.ui_font,
                     text_color=C("text_dim"), anchor="w").grid(
            row=4, column=0, sticky="ew", padx=PAD_X, pady=(2, PAD_Y))

        # Output
        self.output = ctk.CTkTextbox(
            self, font=self.mono_font, wrap="word",
            fg_color=C("bg_secondary"), text_color=C("text"),
            border_color=C("border"), border_width=1, corner_radius=10,
            scrollbar_button_color=C("border"),
            scrollbar_button_hover_color=C("text_dim"),
        )
        self.output.grid(row=5, column=0, sticky="nsew",
                         padx=PAD_X, pady=(0, PAD_X))

    # progress system

    def _set_progress(self, value: float) -> None:
        v = max(0.0, min(100.0, float(value)))
        self.progress_current = v
        self.progress_target = v
        self.progress_ramp_per_sec = 0.0
        self.progress_eta_end = None
        try:
            self.progress.set(v / 100.0)
        except Exception:
            pass
        self.pct_var.set(f"{v:.0f}%" if v > 0 else "")

    def _ramp_to(self, target: float, duration_s: float) -> None:
        target = max(0.0, min(100.0, float(target)))
        duration_s = max(0.1, float(duration_s))
        self.progress_target = target
        delta = target - self.progress_current
        self.progress_ramp_per_sec = delta / duration_s if delta > 0 else 0.0
        self.progress_eta_end = time.monotonic() + duration_s if delta > 0 else None

    def _tick_progress(self) -> None:
        try:
            now = time.monotonic()
            if self._last_tick is not None:
                dt = now - self._last_tick
                if (self.progress_ramp_per_sec > 0
                        and self.progress_current < self.progress_target):
                    new_value = self.progress_current + self.progress_ramp_per_sec * dt
                    if new_value > self.progress_target - 0.5:
                        new_value = min(self.progress_target - 0.5, self.progress_target)
                        self.progress_ramp_per_sec *= 0.5
                    self.progress_current = new_value
                    try:
                        self.progress.set(self.progress_current / 100.0)
                        self.pct_var.set(f"{self.progress_current:.0f}%")
                    except Exception:
                        pass
            self._last_tick = now
        except Exception:
            pass
        finally:
            self.after(100, self._tick_progress)

    # helpers

    def set_status(self, text: str) -> None:
        if self.progress_eta_end is not None:
            remaining = max(0.0, self.progress_eta_end - time.monotonic())
            if remaining >= 1.0:
                text = f"{text}    ~{format_eta(remaining)} remaining"
        self.status_var.set(text)

    def append_output(self, text: str) -> None:
        try:
            self.output.insert("end", text)
            self.output.see("end")
        except Exception:
            pass

    def clear_output(self) -> None:
        try:
            self.output.delete("1.0", "end")
        except Exception:
            pass
        self.last_result = None
        self.last_transcript = ""

    # device

    def _on_device_change(self, choice: str) -> None:
        if choice == "GPU":
            if not self.cuda_available:
                messagebox.showwarning(
                    "GPU not available",
                    "No CUDA-capable GPU was detected when the app started.\n"
                    "Run diagnose.bat to see why, then restart the app.",
                )
                self.device_var.set("CPU")
                return
            self.device = "cuda"
        else:
            self.device = "cpu"
        self._refresh_device_color()
        self.set_status(f"Device set to {choice}.")

    def _refresh_device_color(self) -> None:
        key = "green" if self.device == "cuda" else "amber"
        try:
            self.device_menu.configure(text_color=self.C(key))
        except Exception:
            pass

    # actions

    def pick_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose an audio file", filetypes=SUPPORTED_FILETYPES,
        )
        if path:
            self.audio_path_var.set(path)

    # drag & drop

    @staticmethod
    def _parse_dnd_paths(raw: str) -> list[str]:
        """Split a Tk drag-and-drop data string into individual paths.

        Tk DnD encodes a list of paths as space-separated entries, with any
        path that contains spaces wrapped in {curly braces}. Single paths
        without spaces just come through as the bare string.
        """
        paths: list[str] = []
        i = 0
        s = raw.strip()
        while i < len(s):
            ch = s[i]
            if ch == "{":
                j = s.find("}", i + 1)
                if j == -1:
                    paths.append(s[i + 1:])
                    break
                paths.append(s[i + 1:j])
                i = j + 1
            elif ch.isspace():
                i += 1
            else:
                j = i
                while j < len(s) and not s[j].isspace():
                    j += 1
                paths.append(s[i:j])
                i = j
        return [p for p in paths if p]

    def _handle_drop(self, event):
        paths = self._parse_dnd_paths(getattr(event, "data", "") or "")
        if not paths:
            return
        # If a folder was dropped, refuse with a helpful status
        first = paths[0]
        if os.path.isdir(first):
            self.set_status(f"Cannot transcribe a folder. Drop an audio file instead.")
            return
        if not os.path.isfile(first):
            self.set_status(f"Dropped path doesn't exist: {first}")
            return
        self.audio_path_var.set(first)
        LOG.info("File dropped: %s (%d total dropped)", first, len(paths))
        if len(paths) > 1:
            self.set_status(f"Loaded first of {len(paths)} dropped files: {Path(first).name}")
        else:
            self.set_status(f"Loaded {Path(first).name}. Click Transcribe.")

    def start_transcription(self) -> None:
        path = self.audio_path_var.get().strip().strip('"')
        if not path:
            messagebox.showerror("No file", "Please select an audio file first.")
            return
        if not os.path.isfile(path):
            messagebox.showerror("File not found", f"Cannot find file:\n{path}")
            return
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showwarning("Busy", "A transcription is already running.")
            return

        save_config({
            "last_path": path,
            "model": self.model_var.get(),
            "diarize": bool(self.diarize_var.get()),
        })

        self.clear_output()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self._set_progress(0)
        self.set_status("Starting...")

        self._cancel = threading.Event()
        opts = {
            "audio_path": path,
            "model_size": self.model_var.get(),
            "diarize": bool(self.diarize_var.get()),
            "device": self.device,
            "hf_token": load_hf_token(),
        }
        LOG.info(
            "Transcribe started: file=%s, model=%s, diarize=%s, device=%s",
            path, opts["model_size"], opts["diarize"], opts["device"],
        )
        self.worker_thread = threading.Thread(
            target=self._worker, args=(opts, self._cancel), daemon=True,
        )
        self.worker_thread.start()

    def cancel_transcription(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            self._cancel.set()
            self.set_status("Cancelling... (will stop after the current step)")
            LOG.info("Cancel requested by user")

    # worker (background)

    def _worker(self, opts: dict, cancel: threading.Event) -> None:
        Q = self.msg_queue

        def check_cancel() -> bool:
            if cancel.is_set():
                Q.put(("status", "Cancelled."))
                return True
            return False

        try:
            Q.put(("progress", 2))
            Q.put(("status", "Importing WhisperX..."))
            try:
                import whisperx
            except ImportError as e:
                LOG.error("whisperx import failed: %s", e)
                Q.put(("error",
                       f"WhisperX is not installed in this Python environment.\n\n{e}\n\n"
                       "See requirements.md for setup."))
                return
            try:
                import torch
            except ImportError as e:
                LOG.error("torch import failed: %s", e)
                Q.put(("error", f"PyTorch is not installed.\n\n{e}\n\nSee requirements.md."))
                return
            LOG.info("Imports OK: whisperx=%s, torch=%s",
                     getattr(whisperx, "__version__", "?"), torch.__version__)

            if check_cancel():
                return

            device = opts["device"]
            compute_type = "int8_float16" if device == "cuda" else "int8"
            model_size = opts["model_size"]
            audio_path = opts["audio_path"]

            Q.put(("progress", 5))
            Q.put(("ramp", (10, 8)))
            Q.put(("status", f"Loading Whisper '{model_size}' on {device.upper()}..."))
            try:
                asr_model = whisperx.load_model(
                    model_size, device=device, compute_type=compute_type)
                LOG.info("Loaded ASR model %s on %s (%s)",
                         model_size, device, compute_type)
            except Exception as e:
                LOG.exception("Failed to load Whisper model %s", model_size)
                Q.put(("error", f"Could not load model '{model_size}'.\n\n{e}"))
                return

            if check_cancel():
                return

            Q.put(("progress", 12))
            Q.put(("status", "Loading audio..."))
            try:
                audio = whisperx.load_audio(audio_path)
            except Exception as e:
                LOG.exception("ffmpeg decode failed for %s", audio_path)
                Q.put(("error",
                       f"Could not decode the audio file.\n\n{e}\n\n"
                       "Tip: ensure ffmpeg is on PATH."))
                return

            audio_duration = float(len(audio)) / 16000.0
            LOG.info("Audio loaded: %.1fs (%d samples)",
                     audio_duration, len(audio))

            if check_cancel():
                return

            est_transcribe = estimate_transcribe_seconds(audio_duration, model_size, device)
            transcribe_target = 65 if opts.get("diarize") else 80
            Q.put(("progress", 15))
            Q.put(("ramp", (transcribe_target, est_transcribe)))
            Q.put(("status", f"Transcribing {format_eta(audio_duration)} of audio..."))
            try:
                batch_size = 8 if device == "cuda" else 4
                _t0 = time.monotonic()
                result = asr_model.transcribe(audio, batch_size=batch_size)
                LOG.info("Transcription done in %.1fs (audio=%.1fs)",
                         time.monotonic() - _t0, audio_duration)
            except Exception as e:
                LOG.exception("Transcription crashed")
                Q.put(("error", f"Transcription failed.\n\n{e}"))
                return

            language = result.get("language", "en")
            Q.put(("progress", transcribe_target))
            Q.put(("status", f"Transcribed (language: {language}). Aligning words..."))

            try:
                del asr_model
                gc.collect()
                if device == "cuda":
                    torch.cuda.empty_cache()
            except Exception:
                pass

            if check_cancel():
                return

            align_target = 80 if opts.get("diarize") else 95
            est_align = max(2.0, audio_duration / 30.0)
            Q.put(("ramp", (align_target, est_align)))
            try:
                align_model, metadata = whisperx.load_align_model(
                    language_code=language, device=device)
                result = whisperx.align(
                    result["segments"], align_model, metadata, audio, device,
                    return_char_alignments=False)
                try:
                    del align_model
                    gc.collect()
                    if device == "cuda":
                        torch.cuda.empty_cache()
                except Exception:
                    pass
            except Exception as e:
                LOG.warning("Alignment skipped: %s", e)
                Q.put(("warn", f"Alignment skipped ({e})."))

            if check_cancel():
                return

            Q.put(("progress", align_target))

            if opts.get("diarize"):
                token = opts.get("hf_token")
                if not token:
                    Q.put(("warn",
                           "Diarization skipped: no HuggingFace token set.\n"
                           "Click 'Settings' to enable speaker labels."))
                else:
                    est_diar = max(5.0, audio_duration / 4.0)
                    Q.put(("ramp", (97, est_diar)))
                    Q.put(("status", "Running speaker diarization..."))
                    try:
                        try:
                            from whisperx.diarize import DiarizationPipeline
                        except ImportError:
                            from whisperx import DiarizationPipeline
                        try:
                            diar = DiarizationPipeline(token=token, device=device)
                        except TypeError:
                            diar = DiarizationPipeline(use_auth_token=token, device=device)
                        diar_segments = diar(audio)
                        result = whisperx.assign_word_speakers(diar_segments, result)
                        LOG.info("Diarization OK")
                    except Exception as e:
                        LOG.exception("Diarization failed")
                        Q.put(("warn",
                               f"Diarization failed: {e}\n"
                               "Verify your HF token AND that you've accepted the user\n"
                               "conditions on the three pyannote pages in requirements.md."))

            if check_cancel():
                return

            transcript = format_transcript(result)
            LOG.info(
                "Transcribe complete: %d segments, language=%s, %d chars",
                len(result.get("segments", []) or []),
                language, len(transcript),
            )
            Q.put(("done", {
                "result": result,
                "transcript": transcript,
                "audio_path": audio_path,
                "language": language,
            }))

        except Exception:
            tb = traceback.format_exc()
            LOG.critical("Unhandled exception in worker thread:\n%s", tb)
            Q.put(("error", f"Unexpected error:\n\n{tb}"))
        finally:
            Q.put(("end", None))

    # queue pump

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "status":
                    self.set_status(str(payload))
                elif kind == "progress":
                    self._set_progress(float(payload))
                elif kind == "ramp":
                    target, duration = payload
                    self._ramp_to(float(target), float(duration))
                elif kind == "warn":
                    text = str(payload)
                    self.set_status(text.split("\n", 1)[0])
                    self.append_output(f"\n[!] {text}\n\n")
                elif kind == "error":
                    text = str(payload)
                    self.progress_eta_end = None
                    self.set_status("Error.")
                    self.append_output(f"\n[ERROR] {text}\n")
                    messagebox.showerror("Transcription error", text)
                elif kind == "done":
                    self.last_result = payload["result"]
                    self.last_transcript = payload["transcript"]
                    self.append_output(self.last_transcript or "(empty transcript)")
                    self._set_progress(100)
                    self.set_status(
                        f"Done. Language: {payload.get('language', '?')}, "
                        f"{len(self.last_transcript.splitlines())} lines."
                    )
                elif kind == "end":
                    try:
                        self.start_btn.configure(state="normal")
                        self.stop_btn.configure(state="disabled")
                    except Exception:
                        pass
        except queue.Empty:
            pass
        except Exception:
            pass
        finally:
            self.after(100, self._poll_queue)

    # save / copy

    def save_markdown(self) -> None:
        if not self.last_transcript:
            messagebox.showinfo("Nothing to save", "Run a transcription first.")
            return
        src = self.audio_path_var.get().strip()
        default_name = (Path(src).stem if src else "transcript") + ".md"
        path = filedialog.asksaveasfilename(
            title="Save transcript as Markdown",
            defaultextension=".md", initialfile=default_name,
            filetypes=[("Markdown", "*.md"), ("Text", "*.txt"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(f"# Transcript - {Path(src).name if src else ''}\n\n")
                f.write(f"- Source: `{src}`\n")
                f.write(f"- Model: `{self.model_var.get()}`\n")
                f.write(f"- Device: `{self.device}`\n\n---\n\n")
                f.write(self.last_transcript)
                f.write("\n")
            messagebox.showinfo("Saved", f"Saved to:\n{path}")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    def copy_clipboard(self) -> None:
        if not self.last_transcript:
            messagebox.showinfo("Nothing to copy", "Run a transcription first.")
            return
        self.clipboard_clear()
        self.clipboard_append(self.last_transcript)
        self.update()
        self.set_status("Copied transcript to clipboard.")

    # Settings dialog

    def open_settings(self) -> None:
        SettingsDialog(self).focus()

    # close

    def _on_close(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            if not messagebox.askyesno(
                "Quit?", "A transcription is still running. Quit anyway?"):
                return
            self._cancel.set()
        LOG.info("App closing")
        self.destroy()


# Settings dialog

class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, master: TranscriptarrApp) -> None:
        super().__init__(master)
        self.master_app = master
        C = master.C

        self.title("Settings")
        self.geometry("680x620")
        self.minsize(620, 560)
        self.configure(fg_color=C("bg_primary"))
        self.transient(master)
        self.after(50, self.grab_set)

        container = ctk.CTkFrame(self, fg_color="transparent")
        container.pack(fill="both", expand=True, padx=20, pady=20)
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(9, weight=1)

        # HuggingFace section
        ctk.CTkLabel(container, text="HuggingFace Token",
                     font=master.heading_font, text_color=C("text"),
                     anchor="w").grid(row=0, column=0, sticky="ew", pady=(0, 4))

        ctk.CTkLabel(
            container,
            text=("Only needed for speaker diarization. Get a free token at "
                  "huggingface.co/settings/tokens, then accept access on all "
                  "three pyannote model pages listed in requirements.md."),
            font=master.ui_font, text_color=C("text_dim"),
            wraplength=580, justify="left", anchor="w",
        ).grid(row=1, column=0, sticky="ew", pady=(0, 12))

        token_frame = ctk.CTkFrame(container, fg_color="transparent")
        token_frame.grid(row=2, column=0, sticky="ew")
        token_frame.grid_columnconfigure(0, weight=1)

        existing = load_hf_token() or ""
        self.token_var = tk.StringVar(value=existing)
        self.show_var = tk.BooleanVar(value=False)

        self.token_entry = ctk.CTkEntry(
            token_frame, textvariable=self.token_var, font=master.ui_font,
            fg_color=C("bg_input"), border_color=C("border"), border_width=1,
            text_color=C("text"), height=36, corner_radius=8, show="*",
        )
        self.token_entry.grid(row=0, column=0, sticky="ew", padx=(0, 8))

        ctk.CTkCheckBox(
            token_frame, text="Show", variable=self.show_var,
            command=self._toggle_show, font=master.ui_font, text_color=C("text"),
            fg_color=C("accent"), hover_color=C("accent_hover"),
            border_color=C("border"), border_width=2, corner_radius=4,
            checkbox_width=20, checkbox_height=20,
        ).grid(row=0, column=1)

        # Appearance section
        ctk.CTkLabel(container, text="Appearance",
                     font=master.heading_font, text_color=C("text"),
                     anchor="w").grid(row=3, column=0, sticky="ew", pady=(20, 4))

        ctk.CTkLabel(
            container,
            text=("Pick a color theme and toggle dark / light mode. "
                  "Changes apply when you click Save."),
            font=master.ui_font, text_color=C("text_dim"),
            wraplength=580, justify="left", anchor="w",
        ).grid(row=4, column=0, sticky="ew", pady=(0, 12))

        theme_row = ctk.CTkFrame(container, fg_color="transparent")
        theme_row.grid(row=5, column=0, sticky="ew", pady=(0, 8))

        ctk.CTkLabel(theme_row, text="Theme", font=master.ui_font,
                     text_color=C("text_dim"), width=80, anchor="w").pack(side="left")
        self.theme_local = tk.StringVar(value=master.theme_name)
        ctk.CTkOptionMenu(
            theme_row, variable=self.theme_local, values=list(THEMES.keys()),
            font=master.ui_font,
            fg_color=C("bg_input"), button_color=C("bg_input"),
            button_hover_color=C("border"), text_color=C("text"),
            dropdown_fg_color=C("bg_secondary"), dropdown_text_color=C("text"),
            dropdown_hover_color=C("border"), dropdown_font=master.ui_font,
            width=160, height=36, corner_radius=8,
        ).pack(side="left")

        mode_row = ctk.CTkFrame(container, fg_color="transparent")
        mode_row.grid(row=6, column=0, sticky="ew", pady=(0, 8))

        ctk.CTkLabel(mode_row, text="Mode", font=master.ui_font,
                     text_color=C("text_dim"), width=80, anchor="w").pack(side="left")
        self.dark_mode_local = tk.BooleanVar(value=master.is_dark)
        ctk.CTkSwitch(
            mode_row, text="Dark mode", variable=self.dark_mode_local,
            font=master.ui_font, text_color=C("text"),
            progress_color=C("accent"), fg_color=C("border"),
        ).pack(side="left")

        # Diagnostics section
        ctk.CTkLabel(container, text="Diagnostics",
                     font=master.heading_font, text_color=C("text"),
                     anchor="w").grid(row=7, column=0, sticky="ew", pady=(20, 4))
        diag_row = ctk.CTkFrame(container, fg_color="transparent")
        diag_row.grid(row=8, column=0, sticky="ew", pady=(0, 4))

        ctk.CTkLabel(
            diag_row,
            text=(f"Logs are written to:\n{LOG_FILE}\n"
                  "Send the most recent log file when reporting a bug."),
            font=master.ui_font, text_color=C("text_dim"),
            justify="left", anchor="w",
        ).pack(side="left", fill="x", expand=True)

        ctk.CTkButton(
            diag_row, text="Open log folder", command=self._open_log_folder,
            font=master.ui_font, height=36, width=130, corner_radius=8,
            fg_color=C("bg_secondary"), hover_color=C("bg_input"),
            text_color=C("text"), border_color=C("border"), border_width=1,
        ).pack(side="right", padx=(8, 0))

        # Action buttons
        btns = ctk.CTkFrame(container, fg_color="transparent")
        btns.grid(row=10, column=0, sticky="ew", pady=(20, 0))
        btns.grid_columnconfigure(1, weight=1)

        BTN_KW = dict(font=master.ui_font, height=36, corner_radius=8, width=120,
                      border_width=1)

        ctk.CTkButton(
            btns, text="Clear token", command=self._clear_token,
            fg_color=C("bg_secondary"), hover_color=C("bg_input"),
            text_color=C("text"), border_color=C("border"),
            **BTN_KW,
        ).grid(row=0, column=0, sticky="w")

        ctk.CTkButton(
            btns, text="Cancel", command=self.destroy,
            fg_color=C("bg_secondary"), hover_color=C("bg_input"),
            text_color=C("text"), border_color=C("border"),
            **BTN_KW,
        ).grid(row=0, column=2, padx=(0, 8))

        ctk.CTkButton(
            btns, text="Save", command=self._save_and_close,
            fg_color=C("accent"), hover_color=C("accent_hover"),
            text_color=C("accent_text"), border_color=C("accent"),
            **BTN_KW,
        ).grid(row=0, column=3)

    def _toggle_show(self) -> None:
        self.token_entry.configure(show="" if self.show_var.get() else "*")

    def _open_log_folder(self) -> None:
        """Open the log directory in the user's file explorer."""
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(LOG_DIR))
            elif sys.platform == "darwin":
                os.system(f'open "{LOG_DIR}"')
            else:
                os.system(f'xdg-open "{LOG_DIR}"')
            LOG.info("Opened log folder: %s", LOG_DIR)
        except Exception as e:
            LOG.exception("Failed to open log folder")
            messagebox.showerror(
                "Could not open folder",
                f"{e}\n\nLog folder is at:\n{LOG_DIR}",
                parent=self,
            )

    def _clear_token(self) -> None:
        try:
            if TOKEN_FILE.exists():
                TOKEN_FILE.unlink()
        except Exception:
            pass
        self.token_var.set("")

    def _save_and_close(self) -> None:
        token = self.token_var.get().strip()
        try:
            if token:
                save_hf_token(token)
            elif TOKEN_FILE.exists():
                TOKEN_FILE.unlink()
        except Exception as e:
            messagebox.showerror("Save failed", str(e), parent=self)
            return

        new_theme = self.theme_local.get()
        new_dark = bool(self.dark_mode_local.get())
        master = self.master_app
        appearance_changed = (new_theme != master.theme_name) or (new_dark != master.is_dark)

        self.destroy()
        if appearance_changed:
            master.after_idle(lambda: master.apply_theme(theme_name=new_theme,
                                                         is_dark=new_dark))


# main

def main() -> None:
    app = TranscriptarrApp()
    app.mainloop()


if __name__ == "__main__":
    main()
