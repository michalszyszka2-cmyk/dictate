"""Autostart shortcut management and ``--doctor`` health checks.

Autostart: a ``.lnk`` shortcut to the repo's ``dictate.cmd`` dropped into (or
removed from) the current user's ``shell:startup`` folder
(``%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup``), via
``win32com.client``'s WScript.Shell -- the standard, no-extra-dependency way
to create a Windows shortcut (pywin32 is already a project dependency).

Doctor: prints one PASS/FAIL line per check and exits 0 only if every check
passes -- mic present, Parakeet model files present, OPENROUTER_API_KEY
present in ``D:\\projects\\hq\\app\\.env.local`` (presence only, the value is
never read into a variable that gets printed), the hotkey hook can register,
Wispr Flow is not running, and ``config.yaml`` is readable.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
FROZEN = getattr(sys, "frozen", False)  # running from the PyInstaller build (installed copy)
SHORTCUT_NAME = "dictate.lnk"
ICON_PATH = REPO_ROOT / "assets" / "dictate.ico"


def launch_target() -> Path:
    """What a shortcut or relaunch should start: dictate.exe when installed,
    else the repo's dictate.cmd."""
    return Path(sys.executable) if FROZEN else REPO_ROOT / "dictate.cmd"
ENV_FILE = os.environ.get("DICTATE_ENV_FILE", "")


def startup_dir() -> Path:
    appdata = os.environ.get("APPDATA", os.path.expanduser("~"))
    return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"


def desktop_dir() -> Path:
    """The user's real Desktop -- read from the shell, since OneDrive redirects it."""
    try:
        import win32com.client

        return Path(win32com.client.Dispatch("WScript.Shell").SpecialFolders("Desktop"))
    except Exception:
        return Path(os.path.expanduser("~")) / "Desktop"


def shortcut_path() -> Path:
    return startup_dir() / SHORTCUT_NAME


def desktop_shortcut_path() -> Path:
    return desktop_dir() / SHORTCUT_NAME


def _write_shortcut(target: Path, cmd_path: Path) -> Path:
    """Create (or overwrite) a .lnk at ``target`` pointing at ``cmd_path``."""
    if not cmd_path.exists():
        raise FileNotFoundError(f"launcher not found at {cmd_path}")

    import win32com.client  # local import -- only needed for this command

    target.parent.mkdir(parents=True, exist_ok=True)
    shell = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortCut(str(target))
    shortcut.TargetPath = str(cmd_path)
    shortcut.WorkingDirectory = str(cmd_path.parent)
    if not FROZEN:
        shortcut.WindowStyle = 7  # minimized (hides dictate.cmd's console flash)
    shortcut.Description = "dictate -- local dictation tray app"
    if FROZEN:
        shortcut.IconLocation = f"{cmd_path},0"
    elif ICON_PATH.exists():
        shortcut.IconLocation = f"{ICON_PATH},0"
    shortcut.Save()
    return target


def install_autostart(cmd_path: Path | None = None) -> Path:
    """Create (or overwrite) the startup shortcut to dictate. Returns its path."""
    return _write_shortcut(shortcut_path(), cmd_path or launch_target())


def install_desktop_icon(cmd_path: Path | None = None) -> Path:
    """Create (or overwrite) the Desktop shortcut to dictate. Returns its path."""
    return _write_shortcut(desktop_shortcut_path(), cmd_path or launch_target())


def remove_autostart() -> bool:
    """Remove the startup shortcut if present. Returns True if it was removed."""
    target = shortcut_path()
    if target.exists():
        target.unlink()
        return True
    return False


def remove_desktop_icon() -> bool:
    """Remove the Desktop shortcut if present. Returns True if it was removed."""
    target = desktop_shortcut_path()
    if target.exists():
        target.unlink()
        return True
    return False


# --------------------------------------------------------------------------- doctor
def _check_mic() -> tuple[bool, str]:
    try:
        import sounddevice as sd

        devices = sd.query_devices()
        inputs = [d for d in devices if d.get("max_input_channels", 0) > 0]
        if inputs:
            return True, f"{len(inputs)} input device(s), default: {sd.query_devices(kind='input')['name']}"
        return False, "no input devices found"
    except Exception as exc:
        return False, f"error querying devices: {exc}"


def _check_model_files() -> tuple[bool, str]:
    try:
        from . import engine

        if engine.model_files_present():
            return True, str(engine.resolve_model_dir())
        return False, f"missing under {engine.resolve_model_dir()} -- run: python -m dictate.models"
    except Exception as exc:
        return False, f"error: {exc}"


def _check_openrouter_key() -> tuple[bool, str]:
    # Presence check only -- the value itself is never assigned to a
    # variable used for anything but a boolean, and is never printed.
    present = False
    try:
        from . import paths

        if os.environ.get("OPENROUTER_API_KEY") or paths.read_secrets().get("OPENROUTER_API_KEY"):
            present = True
        elif ENV_FILE and os.path.exists(ENV_FILE):
            with open(ENV_FILE, "r", encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("OPENROUTER_API_KEY=") and line.split("=", 1)[1].strip().strip('"\''):
                        present = True
                        break
    except Exception:
        pass
    return present, ("present" if present else "absent (checked env and secrets.env) -- set it from the tray: OpenRouter key...")


def _check_hotkey_hook() -> tuple[bool, str]:
    try:
        from . import hotkey

        hk = hotkey.HotkeyListener(lambda: None, lambda: None, lambda: None)
        hk.start()
        registered = hk._listener is not None and hk._listener.is_alive()
        hk.stop()
        return registered, "low-level keyboard hook started and stopped cleanly" if registered else "listener did not start"
    except Exception as exc:
        return False, f"error: {exc}"


def _check_wispr_not_running() -> tuple[bool, str]:
    try:
        from . import app as app_mod

        running = app_mod._wispr_running()
        return (not running), ("Wispr Flow.exe is running" if running else "not running")
    except Exception as exc:
        return False, f"error: {exc}"


def _check_config_readable() -> tuple[bool, str]:
    try:
        from . import config as config_mod

        data = config_mod.load()
        return True, str(data)
    except Exception as exc:
        return False, f"error: {exc}"


def run_doctor() -> int:
    checks = [
        ("mic present", _check_mic),
        ("Parakeet model files present", _check_model_files),
        ("OPENROUTER_API_KEY present", _check_openrouter_key),
        ("hotkey hook registers", _check_hotkey_hook),
        ("Wispr Flow not running", _check_wispr_not_running),
        ("config.yaml readable", _check_config_readable),
    ]
    all_ok = True
    for name, fn in checks:
        ok, detail = fn()
        all_ok = all_ok and ok
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name} -- {detail}")
    print("OVERALL:", "PASS" if all_ok else "FAIL")
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(run_doctor())
