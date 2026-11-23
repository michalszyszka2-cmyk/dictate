"""Per-app policy: which foreground app gets the LLM cleanup, which gets verbatim.

The list lives in ``%APPDATA%\\dictate\\apps.yaml`` so you can edit it
without touching the code.  The file is written with the defaults the first
time it is needed.

    default: cleanup
    apps:
      Code.exe: verbatim
      Cursor.exe: verbatim
      WindowsTerminal.exe: verbatim

Matching is case-insensitive and ignores a missing ``.exe``, because the
foreground process name arrives as ``Code.exe`` from the Win32 API but as
``Code`` in Wispr's history rows.
"""
from __future__ import annotations

import os
import time

from dictate import paths

try:
    import yaml
except Exception:                                            # pragma: no cover
    yaml = None

APPDIR = str(paths.LOCAL_DIR)
APPS_YAML = str(paths.settings_file("apps.yaml"))

VERBATIM = "verbatim"
CLEANUP = "cleanup"

DEFAULT_YAML = """# dictate - per-app policy
# verbatim = deterministic vocabulary pass only, no LLM cleanup
# cleanup  = vocabulary pass + LLM cleanup
# Process names are matched case-insensitively, with or without .exe.
default: cleanup
apps:
  Code.exe: verbatim
  Cursor.exe: verbatim
  WindowsTerminal.exe: verbatim
"""

DEFAULTS = {"default": CLEANUP,
            "apps": {"code.exe": VERBATIM,
                     "cursor.exe": VERBATIM,
                     "windowsterminal.exe": VERBATIM}}

_CACHE: dict = {"mtime": 0.0, "data": None, "checked": 0.0}


def _norm(name: str) -> str:
    name = (name or "").strip().lower()
    name = name.rsplit("\\", 1)[-1].rsplit("/", 1)[-1]
    if not name.endswith(".exe"):
        name += ".exe"
    return name


def ensure_default_config(path: str = APPS_YAML) -> str:
    """Write the default apps.yaml if it is not there yet.  Returns the path."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(DEFAULT_YAML)
    except Exception:
        pass
    return path


def load(path: str = APPS_YAML) -> dict:
    """Policy dict, re-read when apps.yaml changes (checked at most once a second)."""
    now = time.time()
    if _CACHE["data"] is not None and now - _CACHE["checked"] < 1.0:
        return _CACHE["data"]
    _CACHE["checked"] = now
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        ensure_default_config(path)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            _CACHE["data"] = DEFAULTS
            return DEFAULTS
    if _CACHE["data"] is not None and mtime == _CACHE["mtime"]:
        return _CACHE["data"]
    data = dict(DEFAULTS)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
        parsed = yaml.safe_load(raw) if yaml else None
        if isinstance(parsed, dict):
            apps = parsed.get("apps") or {}
            data = {
                "default": str(parsed.get("default", CLEANUP)).strip().lower() or CLEANUP,
                "apps": {_norm(k): str(v).strip().lower()
                         for k, v in apps.items() if isinstance(k, str)},
            }
    except Exception:
        data = DEFAULTS
    _CACHE["mtime"] = mtime
    _CACHE["data"] = data
    return data


def mode_for(app: str, path: str = APPS_YAML) -> str:
    """``"verbatim"`` or ``"cleanup"`` for a foreground process name."""
    data = load(path)
    mode = data["apps"].get(_norm(app))
    if mode not in (VERBATIM, CLEANUP):
        mode = data.get("default", CLEANUP)
    return mode if mode in (VERBATIM, CLEANUP) else CLEANUP


if __name__ == "__main__":                                   # pragma: no cover
    import sys
    print(ensure_default_config())
    for a in (sys.argv[1:] or ["Code.exe", "Code", "Cursor.exe", "WindowsTerminal.exe",
                               "claude", "Claude.exe", "brave", "Antigravity IDE", ""]):
        print("%-20s -> %s" % (a, mode_for(a)))
