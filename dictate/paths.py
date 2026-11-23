"""Where dictate keeps things: all of it on this PC, in ``%APPDATA%\\dictate``.

LOCAL_DIR holds the speech model, history.sqlite, clip audio, logs, caches,
``secrets.env`` (API keys) and the four hand-edited settings files
(SETTINGS_FILES). Each PC has its own; nothing is synced between PCs.
``DICTATE_HOME`` overrides where the settings files are read from.
"""
from __future__ import annotations

import os
from pathlib import Path

LOCAL_DIR = Path(os.environ.get("APPDATA", os.path.expanduser("~"))) / "dictate"
MODELS_DIR = LOCAL_DIR / "models"
SECRETS_ENV = LOCAL_DIR / "secrets.env"
SETTINGS_FILES = ("config.yaml", "dictionary.yaml", "snippets.yaml", "apps.yaml")


def settings_dir() -> Path:
    env = os.environ.get("DICTATE_HOME", "").strip()
    return Path(env) if env else LOCAL_DIR


def settings_file(name: str) -> Path:
    return settings_dir() / name


# -- secrets ---------------------------------------------------------------------
def read_secrets() -> dict[str, str]:
    out: dict[str, str] = {}
    try:
        for line in SECRETS_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def write_secret(name: str, value: str) -> None:
    """Set (or, with an empty value, remove) one key in LOCAL_DIR\\secrets.env."""
    secrets = read_secrets()
    if value:
        secrets[name] = value
    else:
        secrets.pop(name, None)
    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    body = "# dictate API keys for this computer\n"
    body += "".join(f"{k}={v}\n" for k, v in secrets.items())
    SECRETS_ENV.write_text(body, encoding="utf-8")
