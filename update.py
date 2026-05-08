"""Transcriptarr updater and repair tool.

Companion to Transcriptarr.exe. Reads %APPDATA%\\Transcriptarr\\install.json
to find the active install, then offers:

  - check for updates (auto-runs at launch; hits the GitHub Releases API)
  - install the latest update (downloads new transcriptarr.py from the
    release source archive and swaps it in, with a backup)
  - repair installation (verifies ffmpeg, venv, key packages, app files
    and fixes whatever's broken)
  - open install folder

Built into update.exe via Update.spec. Bundled inside Transcriptarr.exe
and extracted into the install folder by the wizard during setup.
"""

from __future__ import annotations

import io
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path

import tkinter as tk
from tkinter import messagebox

import customtkinter as ctk


# Constants (kept in sync with wizard.py - if you change one, change both)

APP_NAME    = "Transcriptarr"
WINDOW_TITLE = "Transcriptarr Updater"
GITHUB_USER = "Rusty-Meat"
GITHUB_REPO = "transcriptarr"
GITHUB_API_LATEST = (
    f"https://api.github.com/repos/{GITHUB_USER}/{GITHUB_REPO}/releases/latest"
)
GITHUB_URL  = f"https://github.com/{GITHUB_USER}/{GITHUB_REPO}"

TRACKER_DIR  = Path(
    os.environ.get("APPDATA", str(Path.home() / "AppData" / "Roaming"))
) / APP_NAME
TRACKER_FILE = TRACKER_DIR / "install.json"
UPDATE_LOG   = TRACKER_DIR / "update.log"

FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"

REQUIRED_PACKAGES = ["torch", "whisperx", "customtkinter", "tkinterdnd2"]

# Indigo (cosmic purple) palette - matches the app's default theme.
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

def _setup_logging() -> logging.Logger:
    TRACKER_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("transcriptarr.updater")
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    if logger.handlers:
        return logger

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-5s] %(funcName)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    try:
        fh = RotatingFileHandler(UPDATE_LOG, maxBytes=2_000_000, backupCount=2,
                                 encoding="utf-8", delay=True)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except Exception as e:
        print(f"[updater] log file unavailable: {e}", file=sys.stderr)

    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("[%(levelname)s] %(message)s"))
    logger.addHandler(ch)
    return logger


LOG = _setup_logging()


# Tracker / install info

def load_tracker() -> dict:
    if TRACKER_FILE.exists():
        try:
            return json.loads(TRACKER_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def get_install_dir() -> Path | None:
    t = load_tracker()
    p = t.get("install_dir")
    return Path(p) if p else None


def get_installed_version() -> str:
    return load_tracker().get("version", "unknown")


# Version comparison

def _norm_version(v: str) -> tuple[int, ...]:
    """'v1.2.3', '1.2.3', 'v1.2.3-rc1' -> (1, 2, 3). Bad input -> (0,)."""
    s = v.lstrip("vV").split("-", 1)[0].split("+", 1)[0]
    parts = []
    for p in s.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    return tuple(parts) if parts else (0,)


def is_newer(remote: str, installed: str) -> bool:
    return _norm_version(remote) > _norm_version(installed)


# GitHub API

def fetch_latest_release() -> dict | None:
    """Returns {'tag_name': ..., 'zipball_url': ..., 'name': ..., 'body': ...}
    or None on failure."""
    try:
        req = urllib.request.Request(
            GITHUB_API_LATEST,
            headers={
                "User-Agent": "Transcriptarr-Updater/1.0",
                "Accept": "application/vnd.github+json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            LOG.info("No releases yet on the GitHub repo.")
        else:
            LOG.warning("GitHub API HTTP %s: %s", e.code, e.reason)
    except Exception as e:
        LOG.warning("GitHub API failed: %s", e)
    return None


def download_release_zip(zip_url: str, dest: Path,
                         on_progress=lambda d, t: None) -> None:
    LOG.info("Downloading release zip: %s -> %s", zip_url, dest)
    req = urllib.request.Request(
        zip_url, headers={"User-Agent": "Transcriptarr-Updater/1.0"},
    )
    dest.parent.mkdir(parents=True, exist_ok=True)
    with urllib.request.urlopen(req, timeout=30) as r:
        total = int(r.headers.get("Content-Length", 0))
        downloaded = 0
        with open(dest, "wb") as f:
            while True:
                chunk = r.read(64 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                downloaded += len(chunk)
                on_progress(downloaded, total)


def download_with_progress(url: str, dest: Path, on_progress) -> None:
    LOG.info("Downloading: %s -> %s", url, dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers={"User-Agent": "Transcriptarr-Updater/1.0"})
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
                if now - last_emit > 0.1:
                    on_progress(downloaded, total)
                    last_emit = now
        on_progress(downloaded, total)


# Update + repair operations

def apply_app_update(install_dir: Path, zipball_url: str,
                     tag_name: str, on_status) -> None:
    """Download the release source archive, extract the app file(s), and
    swap them into the install folder. Creates .bak backups so a bad update
    can be rolled back manually."""
    on_status("")
    on_status(f"Step 1/5: Downloading source archive for release {tag_name}.")
    on_status("(This is the small zip GitHub builds for every tag, ~1-2 MB. "
              "It contains the new transcriptarr.py we'll swap in.)")
    tmp_zip = TRACKER_DIR / "downloads" / f"{tag_name}.zip"

    def progress(done, total):
        if total:
            on_status(f"   {done // 1024} KB / {total // 1024} KB")

    download_release_zip(zipball_url, tmp_zip, progress)
    on_status(f"Saved archive to {tmp_zip}")

    on_status("")
    on_status("Step 2/5: Extracting transcriptarr.py from the archive.")
    new_py = None
    new_ico = None
    with zipfile.ZipFile(tmp_zip) as zf:
        for name in zf.namelist():
            base = Path(name).name
            if base == "transcriptarr.py" and new_py is None:
                new_py = zf.read(name)
                on_status(f"   found {name} ({len(new_py)} bytes)")
            elif base == "transcriptarr.ico" and new_ico is None:
                new_ico = zf.read(name)
                on_status(f"   found {name} ({len(new_ico)} bytes)")
            if new_py and new_ico:
                break

    if not new_py:
        raise RuntimeError(
            f"Release archive doesn't contain transcriptarr.py. "
            f"Cannot update from {tag_name}."
        )

    target_py  = install_dir / "transcriptarr.py"
    backup_py  = install_dir / "transcriptarr.py.bak"

    on_status("")
    on_status("Step 3/5: Backing up your current copy.")
    on_status("(Saved as transcriptarr.py.bak in the install folder. "
              "If anything looks wrong after the update you can rename it "
              "back to restore the old version.)")
    if target_py.exists():
        shutil.copy2(target_py, backup_py)
        on_status(f"   backed up to {backup_py.name}")
    else:
        on_status("   (no existing transcriptarr.py to back up)")

    on_status("")
    on_status("Step 4/5: Writing the new transcriptarr.py.")
    target_py.write_bytes(new_py)
    on_status(f"   wrote {len(new_py)} bytes to {target_py}")

    if new_ico:
        target_ico = install_dir / "transcriptarr.ico"
        if target_ico.exists():
            shutil.copy2(target_ico, install_dir / "transcriptarr.ico.bak")
            on_status(f"   backed up icon to transcriptarr.ico.bak")
        target_ico.write_bytes(new_ico)
        on_status(f"   wrote new transcriptarr.ico ({len(new_ico)} bytes)")

    on_status("")
    on_status("Step 5/5: Updating the install tracker.")
    on_status("(Writes the new version number to install.json so this "
              "updater knows you're now on the latest release.)")
    t = load_tracker()
    t["version"] = tag_name.lstrip("vV")
    TRACKER_FILE.write_text(json.dumps(t, indent=2), encoding="utf-8")
    on_status(f"   tracker now says version = {t['version']}")
    on_status("")
    on_status(f"Update to {tag_name} complete. Restart Transcriptarr to use it.")


# Repair -----------------------

def check_install_health(install_dir: Path) -> dict[str, tuple[bool, str]]:
    """Returns {component: (ok, detail)} for each thing we verify."""
    results: dict[str, tuple[bool, str]] = {}

    # 1. Install dir exists
    results["install_dir"] = (
        install_dir.exists(),
        str(install_dir) if install_dir.exists() else "missing",
    )

    # 2. App files
    app_py = install_dir / "transcriptarr.py"
    results["app_files"] = (
        app_py.exists(),
        "transcriptarr.py present" if app_py.exists() else "transcriptarr.py missing",
    )

    # 3. ffmpeg
    ffmpeg_exe = install_dir / "ffmpeg" / "bin" / "ffmpeg.exe"
    results["ffmpeg"] = (
        ffmpeg_exe.exists(),
        f"found at {ffmpeg_exe}" if ffmpeg_exe.exists() else "missing",
    )

    # 4. Venv
    venv_python = install_dir / ".venv" / "Scripts" / "python.exe"
    results["venv"] = (
        venv_python.exists(),
        f"venv python at {venv_python}" if venv_python.exists() else "missing",
    )

    # 5. Required Python packages
    if venv_python.exists():
        missing: list[str] = []
        for pkg in REQUIRED_PACKAGES:
            try:
                r = subprocess.run(
                    [str(venv_python), "-c", f"import {pkg}"],
                    capture_output=True, text=True, timeout=20,
                )
                if r.returncode != 0:
                    missing.append(pkg)
            except Exception as e:
                missing.append(f"{pkg} (timeout/error: {e})")
        if missing:
            results["packages"] = (False, f"missing: {', '.join(missing)}")
        else:
            results["packages"] = (True, f"all present: {', '.join(REQUIRED_PACKAGES)}")
    else:
        results["packages"] = (False, "skipped (no venv)")

    return results


def repair_install(install_dir: Path, on_status) -> None:
    """Verify each component the app needs and fix anything missing or broken."""
    on_status("")
    on_status("Running diagnostics on your install.")
    on_status("(Checking that the install folder, app files, ffmpeg, the "
              "Python venv, and required packages are all present and intact.)")
    health = check_install_health(install_dir)

    for component, (ok, detail) in health.items():
        marker = "OK     " if ok else "MISSING"
        on_status(f"   [{marker}] {component:14s} -> {detail}")

    if all(ok for ok, _ in health.values()):
        on_status("")
        on_status("Everything looks healthy. No repair needed.")
        return

    on_status("")
    on_status("Some components are broken. Fixing them now.")

    # ffmpeg
    if not health["ffmpeg"][0]:
        on_status("")
        on_status("Re-downloading ffmpeg.")
        on_status("(ffmpeg is what decodes m4a/mp3/wav/etc. before transcription. "
                  "Without it the app can't open audio files.)")
        target = install_dir / "ffmpeg"
        zip_path = install_dir / "downloads" / "ffmpeg.zip"

        def prog(d, t):
            if t:
                on_status(f"   {d // 1024} KB / {t // 1024} KB")

        download_with_progress(FFMPEG_URL, zip_path, prog)
        target.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path) as zf:
            members = zf.namelist()
            top = members[0].split("/")[0] if members else ""
            for m in members:
                if not m.startswith(top + "/"):
                    continue
                rel = m[len(top) + 1:]
                if not rel:
                    continue
                out = target / rel
                if m.endswith("/"):
                    out.mkdir(parents=True, exist_ok=True)
                else:
                    out.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(m) as src, open(out, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        on_status("ffmpeg restored.")

    # Packages
    if not health["packages"][0] and health["venv"][0]:
        venv_python = install_dir / ".venv" / "Scripts" / "python.exe"
        on_status("")
        on_status("Re-installing missing Python packages.")
        on_status("(One or more of torch / whisperx / customtkinter / "
                  "tkinterdnd2 was missing from the venv. Re-running pip "
                  "install will fetch fresh copies.)")
        # Torch flavour (CUDA vs CPU) was decided during the original install
        # and recorded in install.json; reuse the same wheels here.
        cmd = [str(venv_python), "-m", "pip", "install", "--no-cache-dir",
               *REQUIRED_PACKAGES]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace")
        for line in proc.stdout or []:
            line = line.rstrip()
            LOG.debug(line)
            if any(p in line for p in ("Downloading ", "Installing ",
                                       "Successfully installed", "ERROR")):
                on_status(f"   {line[:140]}")
        proc.wait()
        if proc.returncode != 0:
            raise RuntimeError(f"pip install failed (exit {proc.returncode}). See {UPDATE_LOG}.")
        on_status("Packages restored.")

    if not health["venv"][0]:
        raise RuntimeError(
            "The Python venv is missing. Repair can't recreate it from update.exe alone. "
            "Re-run the original Transcriptarr.exe (or the latest from the GitHub Releases "
            "page) to fully reinstall."
        )

    if not health["app_files"][0]:
        raise RuntimeError(
            "transcriptarr.py is missing from the install folder. "
            "Run an Update (above) to redownload it from the latest release."
        )

    on_status("Repair complete. Verifying again...")
    final = check_install_health(install_dir)
    for component, (ok, detail) in final.items():
        marker = "OK" if ok else "STILL BROKEN"
        on_status(f"   [{marker}] {component}: {detail}")


# UI

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class UpdaterApp(ctk.CTk):
    def __init__(self) -> None:
        super().__init__()
        self.title(WINDOW_TITLE)
        self.geometry("680x520")
        self.minsize(620, 460)
        self.configure(fg_color=COLOR_BG)

        # Set window icon if one was shipped to the install dir
        try:
            install_dir = get_install_dir()
            if install_dir:
                ic = install_dir / "transcriptarr.ico"
                if ic.exists():
                    self.iconbitmap(str(ic))
        except Exception:
            pass

        # Fonts
        self.ui_font      = ctk.CTkFont(family="Bahnschrift", size=14)
        self.ui_font_bold = ctk.CTkFont(family="Bahnschrift", size=14, weight="bold")
        self.heading_font = ctk.CTkFont(family="Bahnschrift", size=22, weight="bold")
        self.mono_font    = ctk.CTkFont(family="Cascadia Mono", size=12)

        # Async I/O queue for worker -> UI updates
        import queue as _q
        self.msg_queue: "_q.Queue[tuple[str, object]]" = _q.Queue()
        self.worker: threading.Thread | None = None

        # Update state
        self.latest_release: dict | None = None
        self.installed_version = get_installed_version()

        self._build_ui()
        self._poll()

        # Auto-check on launch
        self.after(300, self._do_check)

    # UI construction

    def _build_ui(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)

        # Header
        header = ctk.CTkFrame(self, fg_color=COLOR_PANEL, corner_radius=0, height=72)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        header.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(
            header, text=WINDOW_TITLE, font=self.heading_font,
            text_color=COLOR_TEXT, anchor="w",
        ).grid(row=0, column=0, sticky="w", padx=24, pady=(14, 0))

        self.subhead_var = tk.StringVar(value=f"Installed version: {self.installed_version}")
        ctk.CTkLabel(
            header, textvariable=self.subhead_var, font=self.ui_font,
            text_color=COLOR_TEXT_DIM, anchor="w",
        ).grid(row=1, column=0, sticky="w", padx=24)

        # Version status panel
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=1, column=0, sticky="ew", padx=24, pady=(20, 8))
        body.grid_columnconfigure(0, weight=1)

        self.version_status = tk.StringVar(value="Checking GitHub for the latest release...")
        ctk.CTkLabel(
            body, textvariable=self.version_status, font=self.ui_font_bold,
            text_color=COLOR_TEXT, anchor="w", justify="left", wraplength=620,
        ).grid(row=0, column=0, sticky="ew", pady=(0, 8))

        # Action buttons row
        BTN_KW = dict(font=self.ui_font, height=42, corner_radius=8,
                      width=170, border_width=1)

        btn_row = ctk.CTkFrame(body, fg_color="transparent")
        btn_row.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        btn_row.grid_columnconfigure(4, weight=1)

        self.update_btn = ctk.CTkButton(
            btn_row, text="Install update", command=self._do_update,
            state="disabled",
            fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HVR,
            text_color="#ffffff", border_color=COLOR_ACCENT,
            **BTN_KW,
        )
        self.update_btn.grid(row=0, column=0, padx=(0, 6))

        sec = dict(fg_color=COLOR_PANEL, hover_color=COLOR_INPUT,
                   text_color=COLOR_TEXT, border_color=COLOR_BORDER, **BTN_KW)

        self.repair_btn = ctk.CTkButton(
            btn_row, text="Repair installation",
            command=self._do_repair, **sec,
        )
        self.repair_btn.grid(row=0, column=1, padx=6)

        self.recheck_btn = ctk.CTkButton(
            btn_row, text="Check again", command=self._do_check, **sec,
        )
        self.recheck_btn.grid(row=0, column=2, padx=6)

        self.open_btn = ctk.CTkButton(
            btn_row, text="Open install folder", command=self._open_install_folder, **sec,
        )
        self.open_btn.grid(row=0, column=3, padx=6)

        # Activity log
        self.activity = ctk.CTkTextbox(
            self, font=self.mono_font, wrap="word",
            fg_color=COLOR_PANEL, text_color=COLOR_TEXT_DIM,
            border_color=COLOR_BORDER, border_width=1, corner_radius=8,
            scrollbar_button_color=COLOR_BORDER,
            scrollbar_button_hover_color=COLOR_TEXT_DIM,
        )
        self.activity.grid(row=2, column=0, sticky="nsew", padx=24, pady=(0, 16))
        self.activity.configure(state="disabled")

        # Bottom row: Close
        ctk.CTkButton(
            self, text="Close", command=self.destroy,
            font=self.ui_font, height=38, width=120, corner_radius=8,
            fg_color=COLOR_PANEL, hover_color=COLOR_INPUT,
            text_color=COLOR_TEXT, border_color=COLOR_BORDER, border_width=1,
        ).grid(row=3, column=0, sticky="e", padx=24, pady=(0, 18))

    # helpers

    def _activity_print(self, line: str) -> None:
        self.activity.configure(state="normal")
        self.activity.insert("end", line.rstrip() + "\n")
        self.activity.see("end")
        self.activity.configure(state="disabled")

    def _disable_actions(self) -> None:
        self.update_btn.configure(state="disabled")
        self.repair_btn.configure(state="disabled")
        self.recheck_btn.configure(state="disabled")

    def _enable_actions(self, allow_update: bool) -> None:
        self.update_btn.configure(state="normal" if allow_update else "disabled")
        self.repair_btn.configure(state="normal")
        self.recheck_btn.configure(state="normal")

    def _open_install_folder(self) -> None:
        d = get_install_dir()
        if not d or not d.exists():
            messagebox.showinfo(
                "No install found",
                "Couldn't find an install of Transcriptarr. The install tracker "
                "at\n\n  " + str(TRACKER_FILE) + "\n\nis missing or doesn't point at a "
                "real folder. Run Transcriptarr.exe to (re)install.",
                parent=self,
            )
            return
        try:
            os.startfile(str(d))
        except Exception as e:
            messagebox.showerror("Could not open folder", str(e), parent=self)

    # async runner

    def _run_in_background(self, fn, on_done=None) -> None:
        if self.worker and self.worker.is_alive():
            return
        self._disable_actions()

        def go():
            try:
                fn()
            except Exception as e:
                LOG.exception("Worker raised")
                self.msg_queue.put(("error", f"{e.__class__.__name__}: {e}"))
            finally:
                self.msg_queue.put(("done", on_done))

        self.worker = threading.Thread(target=go, daemon=True)
        self.worker.start()

    def _poll(self) -> None:
        try:
            while True:
                kind, payload = self.msg_queue.get_nowait()
                if kind == "log":
                    self._activity_print(str(payload))
                elif kind == "status":
                    self.version_status.set(str(payload))
                elif kind == "error":
                    self._activity_print(f"!! {payload}")
                    messagebox.showerror("Error", str(payload), parent=self)
                elif kind == "done":
                    fn = payload
                    if callable(fn):
                        fn()
                    self._enable_actions(self._has_update_available())
        except Exception:
            pass
        finally:
            self.after(120, self._poll)

    def _has_update_available(self) -> bool:
        return bool(
            self.latest_release
            and is_newer(
                self.latest_release.get("tag_name", ""),
                self.installed_version,
            )
        )

    # actions

    def _do_check(self) -> None:
        self.version_status.set("Checking GitHub for the latest release...")
        self._activity_print("Checking for updates...")

        def work():
            release = fetch_latest_release()
            self.msg_queue.put(("log", "GitHub API responded."
                                if release else
                                "GitHub API returned no release "
                                "(no releases yet, or no internet)."))
            self.latest_release = release
            if not release:
                self.msg_queue.put((
                    "status",
                    f"Couldn't reach GitHub. You're on {self.installed_version}.\n"
                    "Check your connection or try again later.",
                ))
                return

            tag = release.get("tag_name", "?")
            if is_newer(tag, self.installed_version):
                self.msg_queue.put((
                    "status",
                    f"Update available: {self.installed_version} -> {tag}\n"
                    f"Click 'Install update' to apply.",
                ))
            else:
                self.msg_queue.put((
                    "status",
                    f"You're on the latest version ({self.installed_version}).",
                ))

        self._run_in_background(work)

    def _do_update(self) -> None:
        if not self.latest_release:
            return
        install_dir = get_install_dir()
        if not install_dir or not install_dir.exists():
            messagebox.showerror(
                "No install found",
                "Couldn't find an install of Transcriptarr. Run Transcriptarr.exe "
                "to install before updating.",
                parent=self,
            )
            return

        tag = self.latest_release.get("tag_name", "?")
        zipball = self.latest_release.get("zipball_url")
        if not zipball:
            messagebox.showerror("No download URL",
                                  "GitHub release didn't include a source archive URL.",
                                  parent=self)
            return

        if not messagebox.askyesno(
            "Install update?",
            f"This will replace transcriptarr.py in\n\n  {install_dir}\n\n"
            f"with the version from release {tag}. Your old copy will be backed up "
            f"as transcriptarr.py.bak.\n\nContinue?",
            parent=self,
        ):
            return

        def work():
            apply_app_update(
                install_dir, zipball, tag,
                on_status=lambda s: self.msg_queue.put(("log", s)),
            )
            self.msg_queue.put(("status", f"Updated to {tag}. Restart Transcriptarr to use it."))
            # Refresh installed version
            self.installed_version = get_installed_version()
            self.subhead_var.set(f"Installed version: {self.installed_version}")
            self.latest_release = None  # disable update button until next check

        self._run_in_background(work)

    def _do_repair(self) -> None:
        install_dir = get_install_dir()
        if not install_dir or not install_dir.exists():
            messagebox.showerror(
                "No install found",
                "Couldn't find an install of Transcriptarr. Run Transcriptarr.exe "
                "to install before repairing.",
                parent=self,
            )
            return

        def work():
            self.msg_queue.put(("log", "Starting repair..."))
            repair_install(
                install_dir,
                on_status=lambda s: self.msg_queue.put(("log", s)),
            )
            self.msg_queue.put(("log", "Repair finished."))

        self._run_in_background(work)


# Entry point

def main() -> int:
    LOG.info("=" * 60)
    LOG.info("Updater starting up")
    if not load_tracker():
        # Soft-fail: still let the user click Open install folder etc., but
        # explain that no install was found.
        LOG.warning("No install tracker at %s", TRACKER_FILE)

    app = UpdaterApp()
    app.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
                                                                   