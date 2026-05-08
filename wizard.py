"""Transcriptarr setup wizard and launcher.

Single binary entry point. First run (no install tracker found) runs the
setup wizard. Subsequent runs launch the installed app from its venv.

Compiled to Transcriptarr.exe via PyInstaller; see Transcriptarr.spec.

Dev usage:
    python wizard.py
    python wizard.py --force-wizard
    python wizard.py --uninstall

Flags:
    --force-wizard    always run the wizard, even if setup looks complete
    --uninstall       remove the install tracker (does not delete files)
    --debug           verbose logging to console
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
import urllib.error
import webbrowser
import zipfile
from pathlib import Path

import logging
from logging.handlers import RotatingFileHandler

import tkinter as tk
from tkinter import filedialog, messagebox

import customtkinter as ctk


# Suppress the cmd-window flash when launching subprocesses from a windowed
# .exe on Windows.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


def _clean_subprocess_env():
    """Strip env vars that PyInstaller's bundle injects which would otherwise
    confuse a spawned Python interpreter.

    When this wizard runs as a PyInstaller .exe, it sets variables like
    __PYVENV_LAUNCHER__, _MEIPASS, and _PYI_APPLICATION_HOME_DIR that are
    used by the bundled Python runtime to locate its own files. If we
    inherit those into a child python.exe (e.g. the freshly-installed
    Python or a venv's python.exe), the child uses them too and ends up
    looking for stdlib next to wherever Transcriptarr.exe was launched
    from instead of next to its own python.exe. That breaks venv
    creation and pip with cryptic "No module named encodings" errors.
    """
    env = os.environ.copy()
    for key in (
        "__PYVENV_LAUNCHER__",
        "PYTHONHOME",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "_PYI_APPLICATION_HOME_DIR",
        "_MEIPASS",
        "_MEIPASS2",
    ):
        env.pop(key, None)
    return env


def _sp_run(*args, **kwargs):
    if sys.platform == "win32":
        kwargs.setdefault("creationflags", _NO_WINDOW)
    kwargs.setdefault("env", _clean_subprocess_env())
    return subprocess.run(*args, **kwargs)


def _sp_popen(*args, **kwargs):
    if sys.platform == "win32":
        kwargs.setdefault("creationflags", _NO_WINDOW)
    kwargs.setdefault("env", _clean_subprocess_env())
    return subprocess.Popen(*args, **kwargs)


# Constants

APP_NAME           = "Transcriptarr"
WIZARD_TITLE       = "Transcriptarr Setup"
GITHUB_USER        = "Rusty-Meat"
GITHUB_REPO        = "transcriptarr"
GITHUB_URL         = f"https://github.com/{GITHUB_USER}/{GITHUB_REPO}"

# Per-user tracker / log location, independent of the install folder so
# the launcher can locate the install on subsequent runs.
TRACKER_DIR  = Path(os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))) / APP_NAME
TRACKER_FILE = TRACKER_DIR / "install.json"
SETUP_LOG    = TRACKER_DIR / "setup.log"

# Default install location (user can override via Browse).
DEFAULT_INSTALL_DIR = Path(
    os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
) / APP_NAME

# Bundled Python version
PYTHON_VERSION = "3.11.9"
PYTHON_INSTALLER_URL = (
    f"https://www.python.org/ftp/python/{PYTHON_VERSION}/python-{PYTHON_VERSION}-amd64.exe"
)

# ffmpeg static build (essentials, ~80 MB zip)
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

# Pyannote ToS pages the user has to accept while signed into HuggingFace
PYANNOTE_URLS = [
    ("Segmentation 3.0",       "https://huggingface.co/pyannote/segmentation-3.0"),
    ("Diarization 3.1",        "https://huggingface.co/pyannote/speaker-diarization-3.1"),
    ("Diarization community-1","https://huggingface.co/pyannote/speaker-diarization-community-1"),
]
HF_TOKEN_URL = "https://huggingface.co/settings/tokens"
NVIDIA_DRIVERS_URL = "https://www.nvidia.com/Download/index.aspx"

# PyTorch CUDA index URLs (cu128 first, cu126 fallback for older driver/wheels)
TORCH_CUDA_INDEXES = [
    "https://download.pytorch.org/whl/cu128",
    "https://download.pytorch.org/whl/cu126",
]
TORCH_CPU_INDEX    = "https://download.pytorch.org/whl/cpu"

TORCH_PINS         = ["torch==2.8.0", "torchaudio==2.8.0"]
APP_REQUIREMENTS   = ["whisperx", "customtkinter", "tkinterdnd2", "hf_xet"]

# Visual palette (reuse the Indigo cosmic-purple dark from the app for cohesion)
COLOR_BG         = "#1a1d2c"
COLOR_PANEL      = "#23263a"
COLOR_INPUT      = "#2d314a"
COLOR_BORDER     = "#3d4361"
COLOR_TEXT       = "#e4dfd0"
COLOR_TEXT_DIM   = "#9b9aaf"
COLOR_ACCENT     = "#8b5cf6"
COLOR_ACCENT_HVR = "#a78bfa"
COLOR_GREEN      = "#7cc775"
COLOR_AMBER      = "#ffb74d"
COLOR_RED        = "#e57373"


# Logging

def _setup_logging(debug: bool = False) -> logging.Logger:
    TRACKER_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("transcriptarr.setup")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-5s] %(funcName)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        fh = RotatingFileHandler(
            SETUP_LOG, maxBytes=2_000_000, backupCount=2,
            encoding="utf-8", delay=True,
        )
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception as e:
        print(f"[setup] log file unavailable: {e}", file=sys.stderr)

    ch = logging.StreamHandler()
    ch.setLevel(logging.DEBUG if debug else logging.INFO)
    ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(ch)

    return logger


LOG = _setup_logging("--debug" in sys.argv)


# System detection helpers

def has_nvidia_gpu() -> tuple[bool, str]:
    """Return (has_gpu, gpu_name_or_reason)."""
    try:
        r = _sp_run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip():
            return True, r.stdout.strip().splitlines()[0]
        return False, "nvidia-smi found but returned no GPU"
    except FileNotFoundError:
        return False, "nvidia-smi not on PATH (no NVIDIA driver?)"
    except Exception as e:
        return False, f"detection failed: {e}"


def find_system_python311() -> Path | None:
    """Use py launcher to locate any Python 3.11 already installed."""
    try:
        r = _sp_run(
            ["py", "-3.11", "-c", "import sys; print(sys.executable)"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0:
            return Path(r.stdout.strip())
    except Exception:
        pass
    return None


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


# Tracker file (where the install lives)

def load_tracker() -> dict:
    if TRACKER_FILE.exists():
        try:
            return json.loads(TRACKER_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def save_tracker(data: dict) -> None:
    TRACKER_DIR.mkdir(parents=True, exist_ok=True)
    TRACKER_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def is_setup_complete() -> bool:
    t = load_tracker()
    if not t.get("setup_complete"):
        return False
    install_dir = Path(t.get("install_dir", ""))
    venv_python = install_dir / ".venv" / "Scripts" / "python.exe"
    app_script  = install_dir / "transcriptarr.py"
    return venv_python.exists() and app_script.exists()


def launch_installed_app() -> bool:
    t = load_tracker()
    install_dir = Path(t.get("install_dir", ""))
    venv_python = install_dir / ".venv" / "Scripts" / "python.exe"
    app_script  = install_dir / "transcriptarr.py"
    ffmpeg_dir  = install_dir / "ffmpeg" / "bin"

    if not (venv_python.exists() and app_script.exists()):
        LOG.warning("launch_installed_app: missing python or script")
        return False

    # Prepend bundled ffmpeg to PATH for this child process so the app's
    # whisperx.load_audio() can find ffmpeg.exe even if it isn't system-installed.
    env = os.environ.copy()
    if ffmpeg_dir.exists():
        env["PATH"] = str(ffmpeg_dir) + os.pathsep + env.get("PATH", "")

    LOG.info("Launching app: %s", app_script)
    flags = 0
    if sys.platform == "win32":
        # CREATE_NO_WINDOW = 0x08000000
        flags = 0x08000000
    _sp_popen(
        [str(venv_python), str(app_script)],
        cwd=str(install_dir), env=env, creationflags=flags,
    )
    return True


# Install steps (called from a worker thread; communicate via msg_queue)

def _download_with_progress(url: str, dest: Path, on_progress) -> None:
    """on_progress(downloaded_bytes, total_bytes) is called periodically."""
    LOG.info("Downloading %s -> %s", url, dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Transcriptarr-Setup/1.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        total = int(r.headers.get("Content-Length", 0))
        downloaded = 0
        last_emit = 0.0
        with open(dest, "wb") as f:
            while True:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                now = time.monotonic()
                if now - last_emit > 0.1:  # throttle UI updates
                    on_progress(downloaded, total)
                    last_emit = now
        on_progress(downloaded, total)


def install_python(install_dir: Path, on_progress, on_status) -> Path:
    """Returns the path to a Python 3.11 executable, downloading if needed."""
    # Idempotency check: if a previous wizard run already installed Python
    # at our target location, reuse it. The Microsoft Python installer
    # silently exits on a re-run when the target dir already has files,
    # leaving the wizard thinking the install failed.
    target = install_dir / f"python{PYTHON_VERSION.replace('.', '')[:3]}"
    target_python = target / "python.exe"
    if target_python.exists():
        on_status(f"Reusing previously-installed Python at {target_python}")
        LOG.info("Reusing bundled Python: %s", target_python)
        return target_python

    sys_python = find_system_python311()
    if sys_python:
        on_status(f"Using existing Python 3.11 at {sys_python}")
        LOG.info("Using system Python: %s", sys_python)
        return sys_python

    on_status(f"Downloading Python {PYTHON_VERSION} installer...")
    installer = install_dir / "downloads" / f"python-{PYTHON_VERSION}-amd64.exe"
    _download_with_progress(PYTHON_INSTALLER_URL, installer, on_progress)

    on_status(f"Installing Python {PYTHON_VERSION} to {target}...")
    # Silent install, no admin, no PATH pollution, no Store, no docs.
    cmd = [
        str(installer), "/quiet",
        f"TargetDir={target}",
        "InstallAllUsers=0",
        "PrependPath=0",
        "Include_launcher=0",
        "Include_test=0",
        "Include_doc=0",
        "Shortcuts=0",
        "AssociateFiles=0",
    ]
    LOG.info("Running: %s", " ".join(cmd))
    r = _sp_run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(
            f"Python installer failed (exit {r.returncode}):\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )

    py = target / "python.exe"
    if not py.exists():
        raise RuntimeError(f"Python install reported success but {py} not found.")
    return py


def install_ffmpeg(install_dir: Path, on_progress, on_status) -> Path:
    """Returns the path to ffmpeg.exe, downloading + extracting if needed."""
    target_dir = install_dir / "ffmpeg"
    ffmpeg_exe = target_dir / "bin" / "ffmpeg.exe"
    if ffmpeg_exe.exists():
        on_status("ffmpeg already installed.")
        return ffmpeg_exe

    on_status("Downloading ffmpeg (about 80 MB)...")
    zip_path = install_dir / "downloads" / "ffmpeg.zip"
    _download_with_progress(FFMPEG_URL, zip_path, on_progress)

    on_status("Extracting ffmpeg...")
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        # The zip has a top-level versioned folder; flatten it into target_dir.
        members = zf.namelist()
        if not members:
            raise RuntimeError("ffmpeg zip is empty.")
        top = members[0].split("/")[0]
        for m in members:
            if not m.startswith(top + "/"):
                continue
            rel = m[len(top) + 1:]
            if not rel:
                continue
            out = target_dir / rel
            if m.endswith("/"):
                out.mkdir(parents=True, exist_ok=True)
            else:
                out.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(m) as src, open(out, "wb") as dst:
                    shutil.copyfileobj(src, dst)

    if not ffmpeg_exe.exists():
        raise RuntimeError(f"Extracted ffmpeg, but {ffmpeg_exe} not present.")
    return ffmpeg_exe


def create_venv(python_exe: Path, install_dir: Path, on_status) -> Path:
    venv_dir = install_dir / ".venv"
    venv_python = venv_dir / "Scripts" / "python.exe"
    if venv_python.exists():
        on_status("Existing venv found, reusing it.")
        return venv_python

    on_status("Creating virtual environment...")
    LOG.info("python -m venv -> %s", venv_dir)
    r = _sp_run(
        [str(python_exe), "-m", "venv", str(venv_dir)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"venv creation failed (exit {r.returncode}):\n"
            f"stdout: {r.stdout}\nstderr: {r.stderr}"
        )
    if not venv_python.exists():
        raise RuntimeError(f"Expected {venv_python} after venv creation.")

    # Make sure pip is up to date inside the venv
    on_status("Bootstrapping pip in venv...")
    _sp_run(
        [str(venv_python), "-m", "pip", "install", "--upgrade", "pip"],
        capture_output=True, text=True,
    )
    return venv_python


def pip_install(venv_python: Path, args: list[str], on_status,
                label: str | None = None) -> None:
    """Run pip install with streaming output + idle-time heartbeat.

    Pip goes silent for several minutes during the "Installing collected
    packages" phase (it's writing wheels to disk, not downloading). Without
    a heartbeat the UI looks frozen, so we tick a "...still working (Xs)"
    line every 15 seconds during quiet stretches. We also surface the most
    informative lines pip DOES print (downloads, installs, build status).
    """
    cmd = [str(venv_python), "-m", "pip", "install",
           "--no-cache-dir", "--progress-bar", "off", *args]
    LOG.info("pip install: %s", " ".join(args))
    label = label or " ".join(a for a in args if not a.startswith("--"))[:80]
    on_status(f"pip install: {label}")

    proc = _sp_popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )

    start = time.monotonic()
    last_output = [start]
    stop_hb = threading.Event()

    def heartbeat() -> None:
        while not stop_hb.wait(15):
            silence = time.monotonic() - last_output[0]
            if silence > 12:
                elapsed = int(time.monotonic() - start)
                on_status(
                    f"   ...still working ({elapsed}s elapsed; pip goes quiet "
                    f"while it writes wheels to disk)"
                )

    hb_thread = threading.Thread(target=heartbeat, daemon=True)
    hb_thread.start()

    # Pip lines worth surfacing to the activity log
    KEEP_PREFIXES = (
        "Downloading ", "Collecting ", "Building wheel ",
        "Installing collected packages",
        "Successfully installed", "Successfully built",
        "Uninstalling ", "Found existing installation",
        "ERROR", "WARNING",
    )

    try:
        for line in proc.stdout or []:
            line = line.rstrip()
            LOG.debug(line)
            last_output[0] = time.monotonic()
            stripped = line.lstrip()
            if any(stripped.startswith(p) for p in KEEP_PREFIXES):
                on_status(f"   {line[:140]}")
    finally:
        stop_hb.set()
        hb_thread.join(timeout=1)

    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(
            f"pip install failed (exit {proc.returncode}). See {SETUP_LOG}."
        )


def install_packages(venv_python: Path, use_gpu: bool, on_status) -> None:
    """Install Python packages into the venv.

    Order matters: install torch FIRST with the right wheel (CUDA or CPU)
    before pulling in whisperx. Otherwise pip installs the default CPU torch
    as a whisperx dependency, and we'd have to uninstall + redownload the
    CUDA wheel (~2.5 GB wasted bandwidth and several extra minutes).
    """
    # 1. Torch - the heavy install. Pick the right wheel for the device.
    if use_gpu:
        on_status("Installing CUDA PyTorch (about 2.5 GB - the biggest download)...")
        last_err = None
        for index in TORCH_CUDA_INDEXES:
            try:
                wheel_tag = index.rsplit("/", 1)[-1]
                pip_install(
                    venv_python,
                    TORCH_PINS + ["--index-url", index],
                    on_status, label=f"torch (CUDA, {wheel_tag})",
                )
                last_err = None
                break
            except Exception as e:
                last_err = e
                LOG.warning("CUDA torch from %s failed: %s", index, e)
        if last_err:
            raise last_err
    else:
        on_status("Installing CPU PyTorch (about 200 MB)...")
        pip_install(
            venv_python,
            TORCH_PINS + ["--index-url", TORCH_CPU_INDEX],
            on_status, label="torch (CPU)",
        )

    # 2. WhisperX + app deps. With torch already pinned, pip skips it here.
    on_status("Installing WhisperX and app dependencies...")
    pip_install(
        venv_python,
        ["whisperx"] + APP_REQUIREMENTS,
        on_status, label="whisperx + customtkinter + tkinterdnd2 + hf_xet",
    )


def copy_app_to_install(install_dir: Path, on_status) -> Path:
    """Copy the bundled transcriptarr.py + icon + the running .exe itself
    into the install directory.

    The .exe copy means shortcuts can point at a stable location (the user
    can delete or move the original download afterward without breaking
    anything). The .ico copy means the running app can use it as its
    window/taskbar icon.

    In dev mode (running wizard.py with a regular Python interpreter)
    we copy the .py source from next to wizard.py and skip the .exe copy.
    """
    on_status("Copying app files...")
    if getattr(sys, "frozen", False):
        bundled_root = Path(sys._MEIPASS)
    else:
        bundled_root = Path(__file__).resolve().parent

    app_src = bundled_root / "transcriptarr.py"
    if not app_src.exists():
        raise RuntimeError(f"Bundled transcriptarr.py not found at {app_src}.")
    app_dst = install_dir / "transcriptarr.py"
    shutil.copy2(app_src, app_dst)
    LOG.info("Copied %s -> %s", app_src, app_dst)

    # Copy the icon if it's bundled
    icon_src = bundled_root / "transcriptarr.ico"
    if icon_src.exists():
        icon_dst = install_dir / "transcriptarr.ico"
        shutil.copy2(icon_src, icon_dst)
        LOG.info("Copied icon -> %s", icon_dst)

    # In frozen mode: copy the running Transcriptarr.exe AND the bundled
    # update.exe (extracted from sys._MEIPASS) into the install dir. This
    # gives the user a self-contained install with both binaries available
    # for shortcuts and updates.
    if getattr(sys, "frozen", False):
        # 1. Transcriptarr.exe (the running .exe)
        exe_src = Path(sys.executable)
        exe_dst = install_dir / exe_src.name
        if exe_src.resolve() != exe_dst.resolve():
            try:
                shutil.copy2(exe_src, exe_dst)
                LOG.info("Copied launcher -> %s", exe_dst)
                on_status(f"Copied launcher to {exe_dst.name}")
            except Exception as e:
                LOG.warning("Could not copy launcher .exe: %s", e)

        # 2. update.exe (bundled inside Transcriptarr.exe as a data file)
        updater_src = bundled_root / "update.exe"
        if updater_src.exists():
            updater_dst = install_dir / "update.exe"
            try:
                shutil.copy2(updater_src, updater_dst)
                LOG.info("Copied updater -> %s", updater_dst)
                on_status(f"Copied updater to {updater_dst.name}")
            except Exception as e:
                LOG.warning("Could not copy update.exe: %s", e)
        else:
            LOG.warning("update.exe not bundled with this Transcriptarr.exe")

    return app_dst


def write_hf_token(install_dir: Path, token: str) -> None:
    if not token.strip():
        return
    f = install_dir / ".hf_token"
    f.write_text(token.strip(), encoding="utf-8")
    LOG.info("HF token saved to .hf_token")


def create_shortcut(target_path: Path, lnk_path: Path, args: str = "",
                    icon: Path | None = None, working_dir: Path | None = None) -> None:
    """Create a Windows .lnk via WScript.Shell COM, no third-party deps."""
    import ctypes
    # Use the COM object available in any modern Windows.
    # PowerShell is simpler than driving COM via ctypes for a one-shot.
    ps = (
        f"$s = (New-Object -ComObject WScript.Shell).CreateShortcut('{lnk_path}'); "
        f"$s.TargetPath = '{target_path}'; "
        f"$s.Arguments = '{args}'; "
        f"$s.WorkingDirectory = '{working_dir or target_path.parent}'; "
    )
    if icon and icon.exists():
        ps += f"$s.IconLocation = '{icon}'; "
    ps += "$s.Save()"
    r = _sp_run(
        ["powershell", "-NoProfile", "-Command", ps],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"Shortcut creation failed: {r.stderr}")


def create_user_shortcuts(install_dir: Path, exe_path: Path,
                           desktop: bool, start_menu: bool, on_status,
                           icon: Path | None = None) -> None:
    if not (desktop or start_menu):
        return
    if exe_path is None or not exe_path.exists():
        # In dev mode there's no .exe; skip silently.
        on_status("(skipping shortcuts: launcher .exe not present in dev mode)")
        return

    if desktop:
        on_status("Creating desktop shortcut...")
        desktop_dir = Path(os.path.expandvars(r"%USERPROFILE%\Desktop"))
        try:
            create_shortcut(exe_path, desktop_dir / f"{APP_NAME}.lnk",
                            icon=icon, working_dir=install_dir)
        except Exception as e:
            LOG.warning("Desktop shortcut failed: %s", e)

    if start_menu:
        on_status("Adding to Start Menu...")
        sm_dir = Path(os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"))
        sm_dir.mkdir(parents=True, exist_ok=True)
        try:
            create_shortcut(exe_path, sm_dir / f"{APP_NAME}.lnk",
                            icon=icon, working_dir=install_dir)
        except Exception as e:
            LOG.warning("Start Menu shortcut failed: %s", e)


# Wizard UI

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class WizardApp(ctk.CTk):
    """Multi-step setup wizard. Screens are built once and shown/hidden."""

    SCREEN_BUILDERS = [
        "_build_welcome",
        "_build_system_check",
        "_build_options",
        "_build_install",
        "_build_token",
        "_build_pyannote_tos",
        "_build_shortcuts",
        "_build_done",
    ]

    def __init__(self) -> None:
        super().__init__()
        self.title(WIZARD_TITLE)
        self.geometry("760x580")
        self.minsize(680, 520)
        self.configure(fg_color=COLOR_BG)

        # Fonts
        self.ui_font      = ctk.CTkFont(family="Bahnschrift", size=14)
        self.ui_font_bold = ctk.CTkFont(family="Bahnschrift", size=14, weight="bold")
        self.heading_font = ctk.CTkFont(family="Bahnschrift", size=22, weight="bold")
        self.mono_font    = ctk.CTkFont(family="Cascadia Mono", size=12)

        # State variables
        gpu_ok, gpu_name = has_nvidia_gpu()
        self.gpu_detected = gpu_ok
        self.gpu_name     = gpu_name

        self.install_dir_var      = tk.StringVar(value=str(DEFAULT_INSTALL_DIR))
        self.use_gpu_var          = tk.BooleanVar(value=gpu_ok)
        self.hf_token_var         = tk.StringVar(value="")
        self.shortcut_desktop_var = tk.BooleanVar(value=True)
        self.shortcut_start_var   = tk.BooleanVar(value=True)
        self.tos_acknowledged_var = tk.BooleanVar(value=False)

        # System detection result, populated by _build_system_check on entry
        self.detection: dict[str, tuple[bool, str]] = {}

        # Install thread + queue
        import queue as _q
        self.msg_queue: "_q.Queue[tuple[str, object]]" = _q.Queue()
        self.install_thread: threading.Thread | None = None
        self.install_succeeded = False

        # Layout
        self.current_index = 0
        self._build_chrome()
        self._build_screens()
        self._show(0)
        self.after(100, self._poll_queue)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    # UI scaffolding

    def _build_chrome(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # Header
        header = ctk.CTkFrame(self, fg_color=COLOR_PANEL, corner_radius=0, height=72)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header, text=WIZARD_TITLE, font=self.heading_font,
            text_color=COLOR_TEXT, anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=24, pady=(14, 0))

        self.subhead_var = tk.StringVar(value="")
        ctk.CTkLabel(
            header, textvariable=self.subhead_var, font=self.ui_font,
            text_color=COLOR_TEXT_DIM, anchor="w",
        ).grid(row=1, column=0, sticky="w", padx=24)

        # Body holder (each screen is a CTkFrame placed in this slot)
        self.body = ctk.CTkFrame(self, fg_color=COLOR_BG, corner_radius=0)
        self.body.grid(row=1, column=0, sticky="nsew", padx=24, pady=(20, 12))
        self.body.grid_columnconfigure(0, weight=1)
        self.body.grid_rowconfigure(0, weight=1)

        # Footer (Back / Next / Cancel)
        footer = ctk.CTkFrame(self, fg_color=COLOR_BG, corner_radius=0)
        footer.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 18))
        footer.grid_columnconfigure(0, weight=1)

        self.cancel_btn = ctk.CTkButton(
            footer, text="Cancel", command=self._on_close,
            font=self.ui_font, width=110, height=38, corner_radius=8,
            fg_color=COLOR_PANEL, hover_color=COLOR_INPUT,
            text_color=COLOR_TEXT, border_color=COLOR_BORDER, border_width=1,
        )
        self.cancel_btn.grid(row=0, column=0, sticky="w")

        self.back_btn = ctk.CTkButton(
            footer, text="Back", command=self._go_back,
            font=self.ui_font, width=110, height=38, corner_radius=8,
            fg_color=COLOR_PANEL, hover_color=COLOR_INPUT,
            text_color=COLOR_TEXT, border_color=COLOR_BORDER, border_width=1,
        )
        self.back_btn.grid(row=0, column=1, padx=(0, 8))

        self.next_btn = ctk.CTkButton(
            footer, text="Next", command=self._go_next,
            font=self.ui_font, width=140, height=38, corner_radius=8,
            fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HVR,
            text_color="#ffffff", border_color=COLOR_ACCENT, border_width=1,
        )
        self.next_btn.grid(row=0, column=2)

    def _build_screens(self) -> None:
        self.screens: list[ctk.CTkFrame] = []
        for builder_name in self.SCREEN_BUILDERS:
            frame = ctk.CTkFrame(self.body, fg_color="transparent")
            getattr(self, builder_name)(frame)
            self.screens.append(frame)

    def _show(self, idx: int) -> None:
        if idx < 0 or idx >= len(self.screens):
            return
        # Hide all
        for f in self.screens:
            f.grid_forget()
        # Show requested
        self.screens[idx].grid(row=0, column=0, sticky="nsew")
        self.current_index = idx
        self._update_chrome()
        self._on_screen_enter(idx)

    def _update_chrome(self) -> None:
        last = len(self.screens) - 1
        self.back_btn.configure(state="normal" if self.current_index > 0 else "disabled")
        if self.current_index == last:
            self.next_btn.configure(text="Launch app")
        elif self.current_index == 3:  # install screen
            self.next_btn.configure(text="Install")
        else:
            self.next_btn.configure(text="Next")
        # Subheading per screen
        subheads = [
            "Welcome",
            f"Step 1 / {last} - System check",
            f"Step 2 / {last} - Install location & device",
            f"Step 3 / {last} - Download and install",
            f"Step 4 / {last} - HuggingFace token (optional)",
            f"Step 5 / {last} - Pyannote terms (for speaker labels)",
            f"Step 6 / {last} - Shortcuts",
            "Setup complete",
        ]
        self.subhead_var.set(subheads[self.current_index])

    def _go_next(self) -> None:
        if self.current_index == len(self.screens) - 1:
            # Done screen: launch app and exit wizard
            if self.install_succeeded:
                launch_installed_app()
            self.destroy()
            return

        # Per-screen pre-advance validation
        if self.current_index == 2:  # options screen
            try:
                Path(self.install_dir_var.get()).mkdir(parents=True, exist_ok=True)
            except Exception as e:
                messagebox.showerror("Bad install location", str(e), parent=self)
                return

        if self.current_index == 3:  # install screen
            # The Next button on install becomes "Install" -> fire the worker
            if not self.install_thread or not self.install_thread.is_alive():
                if self.install_succeeded:
                    self._show(self.current_index + 1)
                else:
                    self._start_install()
            return

        if self.current_index == 5:  # pyannote ToS
            if self.hf_token_var.get().strip() and not self.tos_acknowledged_var.get():
                if not messagebox.askyesno(
                    "Continue without confirming?",
                    "You haven't ticked the box confirming you've accepted the\n"
                    "pyannote terms. Speaker diarization will fail at runtime\n"
                    "if you skip this. Continue anyway?",
                    parent=self,
                ):
                    return

        self._show(self.current_index + 1)

    def _go_back(self) -> None:
        # Don't allow going back during/after install
        if self.current_index >= 3 and self.install_succeeded:
            messagebox.showinfo(
                "Install already complete",
                "Setup has already finished installing. Click Next to continue.",
                parent=self,
            )
            return
        if self.install_thread and self.install_thread.is_alive():
            return
        self._show(self.current_index - 1)

    def _on_screen_enter(self, idx: int) -> None:
        if idx == 1:
            self._refresh_system_check()

    def _on_close(self) -> None:
        if self.install_thread and self.install_thread.is_alive():
            if not messagebox.askyesno(
                "Quit setup?",
                "Install is in progress. Quitting now will leave files in a "
                "partial state. Quit anyway?",
                parent=self,
            ):
                return
        self.destroy()

    # screen 0: welcome

    def _build_welcome(self, parent: ctk.CTkFrame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            parent, text=f"Welcome to {APP_NAME}",
            font=self.heading_font, text_color=COLOR_TEXT, anchor="w",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 12))

        ctk.CTkLabel(
            parent, justify="left", anchor="w", wraplength=700,
            font=self.ui_font, text_color=COLOR_TEXT_DIM,
            text=(
                "This wizard will set up everything Transcriptarr needs to run on this "
                "computer. The app transcribes audio locally using Whisper and (optionally) "
                "labels speakers using pyannote.\n\n"
                "What this installs (about 4 GB total, mostly PyTorch):\n"
                "  -  Python 3.11 (if you don't already have it)\n"
                "  -  ffmpeg (for decoding audio files)\n"
                "  -  PyTorch + WhisperX + supporting libraries\n"
                "  -  The app itself, with drag-and-drop and themes\n\n"
                "Everything goes into a folder you choose on the next screen, and "
                "nothing else on your system is modified.\n\n"
                "Click Next to begin."
            ),
        ).grid(row=1, column=0, sticky="ew")

        ctk.CTkLabel(
            parent, text=f"Source: {GITHUB_URL}",
            font=self.mono_font, text_color=COLOR_TEXT_DIM, anchor="w",
        ).grid(row=3, column=0, sticky="ew", pady=(8, 0))

    # screen 1: system check

    def _build_system_check(self, parent: ctk.CTkFrame) -> None:
        parent.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            parent, text="Let's see what's already on this machine.",
            font=self.heading_font, text_color=COLOR_TEXT, anchor="w",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 8))

        ctk.CTkLabel(
            parent, justify="left", anchor="w", wraplength=700,
            font=self.ui_font, text_color=COLOR_TEXT_DIM,
            text=(
                "Anything missing will be downloaded and installed for you. You can "
                "still continue if your GPU isn't detected - the app will just run on "
                "CPU instead (slower, but everything still works)."
            ),
        ).grid(row=1, column=0, sticky="ew", pady=(0, 16))

        # Checklist container
        self.check_rows: dict[str, dict] = {}
        for key, label in [
            ("python",  "Python 3.11"),
            ("ffmpeg",  "ffmpeg"),
            ("nvidia",  "NVIDIA GPU + driver"),
        ]:
            row = ctk.CTkFrame(parent, fg_color=COLOR_PANEL, corner_radius=8)
            row.grid(sticky="ew", pady=4)
            row.grid_columnconfigure(1, weight=1)

            status_lbl = ctk.CTkLabel(
                row, text="...", font=self.ui_font_bold, width=24, anchor="center",
                text_color=COLOR_TEXT_DIM,
            )
            status_lbl.grid(row=0, column=0, padx=(14, 8), pady=10)

            name_lbl = ctk.CTkLabel(
                row, text=label, font=self.ui_font_bold, anchor="w", text_color=COLOR_TEXT,
            )
            name_lbl.grid(row=0, column=1, sticky="w")

            detail_lbl = ctk.CTkLabel(
                row, text="", font=self.ui_font, anchor="e", text_color=COLOR_TEXT_DIM,
            )
            detail_lbl.grid(row=0, column=2, padx=(8, 14))

            self.check_rows[key] = {
                "status": status_lbl, "detail": detail_lbl,
            }

        # NVIDIA driver hint area, only shown when GPU not detected
        self.nvidia_hint = ctk.CTkLabel(
            parent, justify="left", anchor="w", wraplength=700,
            font=self.ui_font, text_color=COLOR_AMBER, text="",
        )
        self.nvidia_hint.grid(sticky="ew", pady=(12, 0))

    def _refresh_system_check(self) -> None:
        # Run synchronously - all checks are fast
        py = find_system_python311()
        ff = has_ffmpeg()
        gpu_ok, gpu_info = has_nvidia_gpu()
        self.detection = {
            "python": (py is not None, str(py) if py else "will be installed"),
            "ffmpeg": (ff, "found on PATH" if ff else "will be installed"),
            "nvidia": (gpu_ok, gpu_info),
        }

        for key, (ok, detail) in self.detection.items():
            row = self.check_rows[key]
            if ok:
                row["status"].configure(text="check", text_color=COLOR_GREEN)
                row["detail"].configure(text=detail)
            else:
                # Python and ffmpeg are auto-fixed; show as "needs install"
                if key in ("python", "ffmpeg"):
                    row["status"].configure(text="dl", text_color=COLOR_AMBER)
                else:
                    row["status"].configure(text="!", text_color=COLOR_AMBER)
                row["detail"].configure(text=detail)

        if not gpu_ok:
            self.nvidia_hint.configure(text=(
                "  No NVIDIA GPU detected. That's fine - the app will run on CPU. "
                "If you have an NVIDIA GPU and expected it to be detected, install "
                f"the latest driver from {NVIDIA_DRIVERS_URL} and re-run the wizard. "
                "Without a working driver, Transcriptarr can't use GPU acceleration "
                "and transcription will be 5-15x slower."
            ))
        else:
            self.nvidia_hint.configure(text=f"  GPU ready: {gpu_info}")

    # screen 2: install location + device

    def _build_options(self, parent: ctk.CTkFrame) -> None:
        parent.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            parent, text="Choose where to install.",
            font=self.heading_font, text_color=COLOR_TEXT, anchor="w",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 8))

        ctk.CTkLabel(
            parent, justify="left", anchor="w", wraplength=700,
            font=self.ui_font, text_color=COLOR_TEXT_DIM,
            text=(
                "Default is your user's local app data, which doesn't require admin "
                "permissions. You can pick a different folder if you prefer."
            ),
        ).grid(row=1, column=0, sticky="ew", pady=(0, 12))

        loc_row = ctk.CTkFrame(parent, fg_color="transparent")
        loc_row.grid(row=2, column=0, sticky="ew")
        loc_row.grid_columnconfigure(0, weight=1)

        ctk.CTkEntry(
            loc_row, textvariable=self.install_dir_var, font=self.mono_font,
            fg_color=COLOR_INPUT, border_color=COLOR_BORDER, border_width=1,
            text_color=COLOR_TEXT, height=36, corner_radius=8,
        ).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ctk.CTkButton(
            loc_row, text="Browse...", command=self._pick_install_dir,
            font=self.ui_font, width=110, height=36, corner_radius=8,
            fg_color=COLOR_PANEL, hover_color=COLOR_INPUT,
            text_color=COLOR_TEXT, border_color=COLOR_BORDER, border_width=1,
        ).grid(row=0, column=1)

        # Device choice
        ctk.CTkLabel(
            parent, text="Device", font=self.heading_font, text_color=COLOR_TEXT, anchor="w",
        ).grid(row=3, column=0, sticky="ew", pady=(28, 8))

        gpu_status = (
            f"GPU detected: {self.gpu_name}" if self.gpu_detected else
            f"No GPU detected ({self.gpu_name})."
        )
        ctk.CTkLabel(
            parent, text=gpu_status, font=self.ui_font,
            text_color=COLOR_GREEN if self.gpu_detected else COLOR_AMBER,
            anchor="w",
        ).grid(row=4, column=0, sticky="ew", pady=(0, 8))

        ctk.CTkCheckBox(
            parent, text="Use GPU (faster, requires NVIDIA + ~3 GB extra download)",
            variable=self.use_gpu_var,
            font=self.ui_font, text_color=COLOR_TEXT,
            fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HVR,
            border_color=COLOR_BORDER, border_width=2, corner_radius=4,
            checkbox_width=20, checkbox_height=20,
        ).grid(row=5, column=0, sticky="w", pady=(0, 8))

        ctk.CTkLabel(
            parent, justify="left", anchor="w", wraplength=700,
            font=self.ui_font, text_color=COLOR_TEXT_DIM,
            text=(
                "If you uncheck this, the CPU-only PyTorch wheel is installed instead "
                "(saves ~3 GB but transcription is much slower). You can change this "
                "later from inside the app."
            ),
        ).grid(row=6, column=0, sticky="ew")

    def _pick_install_dir(self) -> None:
        path = filedialog.askdirectory(
            title="Choose install folder", initialdir=self.install_dir_var.get(),
        )
        if path:
            self.install_dir_var.set(str(Path(path) / APP_NAME if Path(path).name != APP_NAME else path))

    # screen 3: install + progress

    def _build_install(self, parent: ctk.CTkFrame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(4, weight=1)

        ctk.CTkLabel(
            parent, text="Ready to install.",
            font=self.heading_font, text_color=COLOR_TEXT, anchor="w",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 8))

        self.install_intro = ctk.CTkLabel(
            parent, justify="left", anchor="w", wraplength=700,
            font=self.ui_font, text_color=COLOR_TEXT_DIM, text="",
        )
        self.install_intro.grid(row=1, column=0, sticky="ew", pady=(0, 16))

        # Big stage label
        self.stage_var = tk.StringVar(value="Click 'Install' to begin.")
        ctk.CTkLabel(
            parent, textvariable=self.stage_var, font=self.ui_font_bold,
            text_color=COLOR_TEXT, anchor="w",
        ).grid(row=2, column=0, sticky="ew")

        # Progress bar
        self.progress_bar = ctk.CTkProgressBar(
            parent, height=10, corner_radius=5,
            fg_color=COLOR_INPUT, progress_color=COLOR_ACCENT, border_width=0,
        )
        self.progress_bar.set(0)
        self.progress_bar.grid(row=3, column=0, sticky="ew", pady=(8, 12))

        # Scrolling activity log
        self.activity_log = ctk.CTkTextbox(
            parent, font=self.mono_font, wrap="word",
            fg_color=COLOR_PANEL, text_color=COLOR_TEXT_DIM,
            border_color=COLOR_BORDER, border_width=1, corner_radius=8,
            scrollbar_button_color=COLOR_BORDER,
            scrollbar_button_hover_color=COLOR_TEXT_DIM,
        )
        self.activity_log.grid(row=4, column=0, sticky="nsew")
        self.activity_log.configure(state="disabled")

    def _refresh_install_intro(self) -> None:
        size_note = "~4 GB" if self.use_gpu_var.get() else "~1 GB"
        self.install_intro.configure(text=(
            f"Downloading and installing into:\n   {self.install_dir_var.get()}\n\n"
            f"This will use about {size_note} of disk and "
            "may take 5-20 minutes depending on your internet speed. The window must "
            "stay open until it's finished. Click Install to begin."
        ))

    def _activity(self, line: str) -> None:
        self.activity_log.configure(state="normal")
        self.activity_log.insert("end", line.rstrip() + "\n")
        self.activity_log.see("end")
        self.activity_log.configure(state="disabled")

    def _start_install(self) -> None:
        self.next_btn.configure(state="disabled")
        self.back_btn.configure(state="disabled")
        self.cancel_btn.configure(state="disabled")
        self.install_intro.configure(text=(
            "Installing... please don't close the window. Activity is logged below "
            f"and to {SETUP_LOG} for troubleshooting."
        ))
        self.install_thread = threading.Thread(
            target=self._install_worker, daemon=True,
        )
        self.install_thread.start()

    def _install_worker(self) -> None:
        Q = self.msg_queue
        try:
            install_dir = Path(self.install_dir_var.get()).expanduser().resolve()
            install_dir.mkdir(parents=True, exist_ok=True)
            LOG.info("=" * 60)
            LOG.info("Beginning install to %s", install_dir)
            LOG.info("GPU mode: %s", self.use_gpu_var.get())

            def status(s: str) -> None:
                Q.put(("status", s))

            def progress(done: int, total: int) -> None:
                Q.put(("progress", (done, total)))

            # 1. Python
            Q.put(("stage", "Setting up Python..."))
            python_exe = install_python(install_dir, progress, status)

            # 2. ffmpeg
            Q.put(("stage", "Installing ffmpeg..."))
            install_ffmpeg(install_dir, progress, status)

            # 3. venv
            Q.put(("stage", "Creating virtual environment..."))
            venv_python = create_venv(python_exe, install_dir, status)

            # 4. pip packages
            Q.put(("stage", "Installing Python packages..."))
            install_packages(venv_python, self.use_gpu_var.get(), status)

            # 5. Copy app
            Q.put(("stage", "Copying app files..."))
            copy_app_to_install(install_dir, status)

            # 6. HF token
            write_hf_token(install_dir, self.hf_token_var.get())

            # 7. Tracker
            save_tracker({
                "setup_complete": True,
                "install_dir": str(install_dir),
                "python_exe": str(python_exe),
                "use_gpu": self.use_gpu_var.get(),
                "version": "1.0.2",
            })

            Q.put(("done", str(install_dir)))
        except Exception as e:
            LOG.exception("Install failed")
            Q.put(("error", f"{e.__class__.__name__}: {e}"))

    def _poll_queue(self) -> None:
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "stage":
                    self.stage_var.set(str(payload))
                    self._activity(f"== {payload}")
                    self.progress_bar.set(0)
                elif kind == "status":
                    self._activity(str(payload))
                elif kind == "progress":
                    done, total = payload
                    if total > 0:
                        self.progress_bar.set(min(1.0, done / total))
                    self._activity(f"   {self._fmt_bytes(done)} / {self._fmt_bytes(total) if total else '?'}")
                elif kind == "done":
                    self.install_succeeded = True
                    self.stage_var.set("Install complete.")
                    self.progress_bar.set(1.0)
                    self._activity("== Install complete.")
                    self.next_btn.configure(state="normal", text="Next")
                    self.cancel_btn.configure(state="normal")
                elif kind == "error":
                    self.install_succeeded = False
                    self.stage_var.set("Install failed.")
                    self._activity(f"!! ERROR: {payload}")
                    self.next_btn.configure(state="disabled")
                    self.cancel_btn.configure(state="normal")
                    messagebox.showerror(
                        "Install failed",
                        f"{payload}\n\nFull log: {SETUP_LOG}",
                        parent=self,
                    )
        except Exception:
            pass
        finally:
            self.after(100, self._poll_queue)

    @staticmethod
    def _fmt_bytes(n: int) -> str:
        n = float(n)
        for unit in ("B", "KB", "MB", "GB"):
            if n < 1024:
                return f"{n:.1f} {unit}"
            n /= 1024
        return f"{n:.1f} TB"

    # screen 4: HuggingFace token

    def _build_token(self, parent: ctk.CTkFrame) -> None:
        parent.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            parent, text="HuggingFace token (optional)",
            font=self.heading_font, text_color=COLOR_TEXT, anchor="w",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 8))

        ctk.CTkLabel(
            parent, justify="left", anchor="w", wraplength=700,
            font=self.ui_font, text_color=COLOR_TEXT_DIM,
            text=(
                "Speaker labels (\"[SPEAKER 1]: ...\") use pyannote, which requires "
                "a free HuggingFace account token. If you don't need speaker labels, "
                "skip this step and continue."
            ),
        ).grid(row=1, column=0, sticky="ew", pady=(0, 12))

        ctk.CTkButton(
            parent, text="Open huggingface.co/settings/tokens",
            command=lambda: webbrowser.open(HF_TOKEN_URL),
            font=self.ui_font, height=36, corner_radius=8,
            fg_color=COLOR_PANEL, hover_color=COLOR_INPUT,
            text_color=COLOR_TEXT, border_color=COLOR_BORDER, border_width=1,
        ).grid(row=2, column=0, sticky="w", pady=(0, 12))

        ctk.CTkLabel(
            parent, text="Paste token here:", font=self.ui_font_bold,
            text_color=COLOR_TEXT, anchor="w",
        ).grid(row=3, column=0, sticky="ew")
        ctk.CTkEntry(
            parent, textvariable=self.hf_token_var, show="*",
            font=self.mono_font, height=36, corner_radius=8,
            fg_color=COLOR_INPUT, border_color=COLOR_BORDER, border_width=1,
            text_color=COLOR_TEXT,
        ).grid(row=4, column=0, sticky="ew", pady=(4, 12))

        ctk.CTkLabel(
            parent, justify="left", anchor="w", wraplength=700,
            font=self.ui_font, text_color=COLOR_TEXT_DIM,
            text=(
                "The token is stored only on this machine, in a file named .hf_token "
                "inside your install folder. Click the next button to skip if you "
                "don't want to set this up now - you can always paste it later inside "
                "the app's Settings dialog."
            ),
        ).grid(row=5, column=0, sticky="ew")

    # screen 5: pyannote ToS

    def _build_pyannote_tos(self, parent: ctk.CTkFrame) -> None:
        parent.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            parent, text="Accept pyannote terms",
            font=self.heading_font, text_color=COLOR_TEXT, anchor="w",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 8))

        ctk.CTkLabel(
            parent, justify="left", anchor="w", wraplength=700,
            font=self.ui_font, text_color=COLOR_TEXT_DIM,
            text=(
                "Speaker labels download three pyannote models from HuggingFace, each "
                "of which requires a one-time click on \"Agree and access repository\" "
                "while signed in to your HuggingFace account. We can't do this for "
                "you - the agreement is tied to your account.\n\n"
                "Click each button below to open the model page in your browser. "
                "Sign in if needed, click 'Agree', then come back and tick the box."
            ),
        ).grid(row=1, column=0, sticky="ew", pady=(0, 16))

        for i, (label, url) in enumerate(PYANNOTE_URLS):
            ctk.CTkButton(
                parent, text=f"Open: {label}",
                command=lambda u=url: webbrowser.open(u),
                font=self.ui_font, height=36, corner_radius=8,
                fg_color=COLOR_PANEL, hover_color=COLOR_INPUT,
                text_color=COLOR_TEXT, border_color=COLOR_BORDER, border_width=1,
                anchor="w",
            ).grid(row=2 + i, column=0, sticky="ew", pady=4)

        ctk.CTkCheckBox(
            parent, text="I've accepted the terms on all three pages above",
            variable=self.tos_acknowledged_var,
            font=self.ui_font, text_color=COLOR_TEXT,
            fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HVR,
            border_color=COLOR_BORDER, border_width=2, corner_radius=4,
            checkbox_width=20, checkbox_height=20,
        ).grid(row=2 + len(PYANNOTE_URLS), column=0, sticky="w", pady=(16, 4))

        ctk.CTkLabel(
            parent, justify="left", anchor="w", wraplength=700,
            font=self.ui_font, text_color=COLOR_TEXT_DIM,
            text=(
                "(If you skipped the token step, you can ignore this and continue. "
                "The app will skip speaker labels at runtime.)"
            ),
        ).grid(row=3 + len(PYANNOTE_URLS), column=0, sticky="ew", pady=(8, 0))

    # screen 6: shortcuts

    def _build_shortcuts(self, parent: ctk.CTkFrame) -> None:
        parent.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            parent, text="Shortcuts",
            font=self.heading_font, text_color=COLOR_TEXT, anchor="w",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 8))

        ctk.CTkLabel(
            parent, justify="left", anchor="w", wraplength=700,
            font=self.ui_font, text_color=COLOR_TEXT_DIM,
            text=(
                "Choose where you'd like quick access. You can always launch the app "
                "by running Transcriptarr.exe directly from your install folder."
            ),
        ).grid(row=1, column=0, sticky="ew", pady=(0, 16))

        ctk.CTkCheckBox(
            parent, text="Create desktop shortcut",
            variable=self.shortcut_desktop_var,
            font=self.ui_font, text_color=COLOR_TEXT,
            fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HVR,
            border_color=COLOR_BORDER, border_width=2, corner_radius=4,
            checkbox_width=20, checkbox_height=20,
        ).grid(row=2, column=0, sticky="w", pady=4)

        ctk.CTkCheckBox(
            parent, text="Add to Start Menu",
            variable=self.shortcut_start_var,
            font=self.ui_font, text_color=COLOR_TEXT,
            fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HVR,
            border_color=COLOR_BORDER, border_width=2, corner_radius=4,
            checkbox_width=20, checkbox_height=20,
        ).grid(row=3, column=0, sticky="w", pady=4)

    # screen 7: done

    def _build_done(self, parent: ctk.CTkFrame) -> None:
        parent.grid_columnconfigure(0, weight=1)
        parent.grid_rowconfigure(2, weight=1)

        ctk.CTkLabel(
            parent, text="All set.",
            font=self.heading_font, text_color=COLOR_TEXT, anchor="w",
        ).grid(row=0, column=0, sticky="ew", pady=(0, 8))

        self.done_summary = ctk.CTkLabel(
            parent, justify="left", anchor="w", wraplength=700,
            font=self.ui_font, text_color=COLOR_TEXT_DIM, text="",
        )
        self.done_summary.grid(row=1, column=0, sticky="ew")

    def _show(self, idx: int) -> None:  # type: ignore[override]
        # Override to do per-screen on-enter actions cleanly.
        if idx < 0 or idx >= len(self.screens):
            return
        for f in self.screens:
            f.grid_forget()
        self.screens[idx].grid(row=0, column=0, sticky="nsew")
        self.current_index = idx
        self._update_chrome()

        if idx == 1:
            self._refresh_system_check()
        elif idx == 3:
            self._refresh_install_intro()
        elif idx == 6 and self.install_succeeded:
            # Apply shortcuts when reaching screen 6 (after install)
            self._apply_shortcuts()
        elif idx == 7:
            self._fill_done_summary()

    def _apply_shortcuts(self) -> None:
        # Dev mode (no .exe) has no launcher target, so skip.
        if not getattr(sys, "frozen", False):
            LOG.info("Dev mode: skipping shortcut creation (no .exe to point at). "
                     "Build via build.bat to test the real shortcut flow.")
            return
        try:
            install_dir = Path(self.install_dir_var.get())
            # Point shortcuts at the COPY in the install folder, not at
            # sys.executable (which lives wherever the user double-clicked
            # the original download).
            installed_exe = install_dir / Path(sys.executable).name
            if not installed_exe.exists():
                installed_exe = Path(sys.executable)  # fallback
            icon_path = install_dir / "transcriptarr.ico"
            create_user_shortcuts(
                install_dir, installed_exe,
                desktop=self.shortcut_desktop_var.get(),
                start_menu=self.shortcut_start_var.get(),
                on_status=lambda s: LOG.info(s),
                icon=icon_path if icon_path.exists() else None,
            )
        except Exception as e:
            LOG.warning("Shortcut creation failed: %s", e)

    def _fill_done_summary(self) -> None:
        t = load_tracker()
        install_dir = t.get("install_dir", "?")
        text = (
            f"Transcriptarr is installed at:\n   {install_dir}\n\n"
            f"Setup log: {SETUP_LOG}\n"
            f"App log:   {Path(install_dir) / 'logs' / 'transcriptarr.log' if install_dir != '?' else '?'}\n\n"
            "Click 'Launch app' to start using it. Going forward, just run "
            f"{APP_NAME}.exe (or the shortcut you created) - it'll go straight to the app, "
            "no wizard.\n\n"
            "If you ever need to repair or update, use update.exe from the same release.\n"
            f"Project home: {GITHUB_URL}"
        )
        self.done_summary.configure(text=text)


# Entry point

def main() -> int:
    args = sys.argv[1:]

    if "--uninstall" in args:
        if TRACKER_FILE.exists():
            TRACKER_FILE.unlink()
            print("Removed install tracker. Setup will run on next launch.")
        return 0

    if "--force-wizard" not in args and is_setup_complete():
        if launch_installed_app():
            return 0
        # Fall through to wizard if launch failed (broken install)
        LOG.warning("Tracker says setup complete but launch failed; running wizard.")

    LOG.info("Starting setup wizard.")
    app = WizardApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
