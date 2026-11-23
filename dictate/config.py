"""``%APPDATA%\\dictate\\config.yaml`` -- user-editable cleanup settings.

    cleanup:
      enabled: true       # false = vocabulary pass only, no LLM call at all
      model: null          # null = free OpenRouter chain (cleanup.py's own
                            # pick/fallback logic); a model id (e.g.
                            # "nvidia/nemotron-3-super-120b-a12b") = use that
                            # single OpenRouter model instead, paid, opt-in
      budget_s: 1.2         # wall-clock budget for the whole cleanup call
    language: en            # en | pl | auto (English or Polish, picked per clip)
    instructions:           # "Dictate, make this bullet points" inside a clip
      enabled: true
      trigger: [dictate]
      budget_s: 6.0

Written with these defaults the first time it is needed -- paid cleanup
stays OFF (``model: null``) until you edit the file yourself. Re-read at
most once a second (mtime-checked), same pattern as ``policy.py``.
"""
from __future__ import annotations

import os
import time

from dictate import paths

try:
    import yaml
except Exception:                                            # pragma: no cover
    yaml = None

APPDIR = str(paths.LOCAL_DIR)  # this machine: logs, history, models
CONFIG_YAML = str(paths.settings_file("config.yaml"))

DEFAULT_YAML = """# dictate - cleanup config
cleanup:
  enabled: true
  # model: null keeps the free OpenRouter/NIM chain in cleanup.py.
  # Set a model id (e.g. nvidia/nemotron-3-super-120b-a12b, a paid id on
  # OpenRouter) to use that single model instead -- off by default, this
  # spends money per clip once turned on.
  model: null
  budget_s: 1.2
  e2e_target_s: 1.4     # total key-release→paste target the pipeline fits cleanup into; ~2.2 with a paid model
language: en            # en | pl | auto -- also switchable from the tray menu
engine: parakeet        # parakeet | whisper -- also switchable from the tray menu (Engine)
whisper:                # used only when engine: whisper (cloud Whisper, then this cleanup model)
  cleanup_model: openai/gpt-5.4-mini
clarity_nudges: true    # true = show clarity nudges after each dictation; false = silent
instructions:           # say "Dictate, make this bullet points" inside a clip
  enabled: true
  trigger: [dictate]    # word(s) that open a spoken instruction
  budget_s: 6.0         # cleanup budget for a clip with an instruction in it
  model: google/gemini-2.5-flash   # stronger than Flash-Lite, ~$0.0004 a clip
history:
  audio_days: 7         # keep each clip's audio this many days, then delete it (text stays); 0 = never save audio
"""

DEFAULTS = {"cleanup": {"enabled": True, "model": None, "budget_s": 1.2, "e2e_target_s": 1.4,
                        "provider": "openrouter"},
            "language": "en",
            "engine": "parakeet",
            "whisper": {"model": None, "cleanup_model": "openai/gpt-5.4-mini"},
            "clarity_nudges": True,
            "instructions": {"enabled": True, "trigger": ["dictate"], "budget_s": 6.0,
                             "model": "google/gemini-2.5-flash"},
            "history": {"audio_days": 7.0},
            "hotkeys": {"dictate": "right_shift", "ask": "right_ctrl", "combo": "ctrl+win",
                        "handsfree": True}}
LANGUAGES = ("en", "pl", "auto")
# Who does the clean-up step. The two "-app" ones run the person's own Claude
# Code / Codex CLI on their monthly plan: no key, but several seconds a clip.
PROVIDERS = ("off", "openrouter", "anthropic", "openai", "claude-app", "codex-app")
# Hotkey choices offered in the Settings window (hotkey.py maps the names).
HOLD_KEYS = ("right_shift", "right_ctrl", "f8", "f9", "f10", "f12", "scroll_lock", "pause", "insert", "off")
ASK_KEYS = ("right_ctrl", "right_shift", "off")
COMBOS = ("ctrl+win", "alt+win", "off")
ENGINES = ("parakeet", "whisper")

_CACHE: dict = {"mtime": 0.0, "data": None, "checked": 0.0, "path": None}


def ensure_default_config(path: str = CONFIG_YAML) -> str:
    """Write the default config.yaml if it is not there yet. Returns the path."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(DEFAULT_YAML)
    except Exception:
        pass
    return path


def load(path: str = CONFIG_YAML) -> dict:
    """Config dict, re-read when config.yaml changes (checked at most once a second)."""
    now = time.time()
    if (_CACHE["data"] is not None and _CACHE["path"] == path and
        now - _CACHE["checked"] < 1.0):
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
            _CACHE["path"] = path
            return DEFAULTS
    if _CACHE["data"] is not None and _CACHE["path"] == path and mtime == _CACHE["mtime"]:
        return _CACHE["data"]

    data = {"cleanup": dict(DEFAULTS["cleanup"]), "language": DEFAULTS["language"],
            "engine": DEFAULTS["engine"], "whisper": dict(DEFAULTS["whisper"]),
            "clarity_nudges": DEFAULTS["clarity_nudges"],
            "instructions": dict(DEFAULTS["instructions"]),
            "history": dict(DEFAULTS["history"]),
            "hotkeys": dict(DEFAULTS["hotkeys"])}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = fh.read()
        parsed = yaml.safe_load(raw) if yaml else None
        if isinstance(parsed, dict):
            cleanup = parsed.get("cleanup") or {}
            if isinstance(cleanup, dict):
                enabled = cleanup.get("enabled", True)
                model = cleanup.get("model")
                model = str(model).strip() if model else None
                try:
                    budget_s = float(cleanup.get("budget_s", 1.2))
                except (TypeError, ValueError):
                    budget_s = 1.2
                try:
                    e2e_target_s = float(cleanup.get("e2e_target_s", 1.4))
                except (TypeError, ValueError):
                    e2e_target_s = 1.4
                provider = str(cleanup.get("provider") or "openrouter").strip().lower()
                data["cleanup"] = {"enabled": bool(enabled), "model": model or None,
                                   "budget_s": budget_s, "e2e_target_s": e2e_target_s,
                                   "provider": provider if provider in PROVIDERS else "openrouter"}
            language = str(parsed.get("language") or "en").strip().lower()
            data["language"] = language if language in LANGUAGES else "en"
            eng = str(parsed.get("engine") or "parakeet").strip().lower()
            data["engine"] = eng if eng in ENGINES else "parakeet"
            wh = parsed.get("whisper")
            if isinstance(wh, dict):
                data["whisper"] = {"model": str(wh.get("model") or "") or None,
                                   "cleanup_model": str(wh.get("cleanup_model") or "")
                                   or DEFAULTS["whisper"]["cleanup_model"]}
            clarity_nudges = parsed.get("clarity_nudges", True)
            data["clarity_nudges"] = bool(clarity_nudges)
            instr = parsed.get("instructions")
            if isinstance(instr, dict):
                trigger = instr.get("trigger", ["dictate"])
                if isinstance(trigger, str):
                    trigger = [trigger]
                try:
                    ibudget = float(instr.get("budget_s", 6.0))
                except (TypeError, ValueError):
                    ibudget = 6.0
                data["instructions"] = {"enabled": bool(instr.get("enabled", True)),
                                        "trigger": [str(t) for t in (trigger or []) if t],
                                        "budget_s": ibudget,
                                        "model": str(instr.get("model") or "") or DEFAULTS["instructions"]["model"]}
            hk = parsed.get("hotkeys")
            if isinstance(hk, dict):
                data["hotkeys"] = _parse_hotkeys(hk)
            hist = parsed.get("history")
            if isinstance(hist, dict):
                try:
                    data["history"] = {"audio_days": max(0.0, float(hist.get("audio_days", 7)))}
                except (TypeError, ValueError):
                    pass
    except Exception:
        data = {"cleanup": dict(DEFAULTS["cleanup"]), "language": DEFAULTS["language"],
                "engine": DEFAULTS["engine"], "whisper": dict(DEFAULTS["whisper"]),
                "clarity_nudges": DEFAULTS["clarity_nudges"],
                "instructions": dict(DEFAULTS["instructions"]),
                "history": dict(DEFAULTS["history"]),
                "hotkeys": dict(DEFAULTS["hotkeys"])}
    _CACHE["mtime"] = mtime
    _CACHE["data"] = data
    _CACHE["path"] = path
    return data


def _parse_hotkeys(hk: dict) -> dict:
    out = dict(DEFAULTS["hotkeys"])
    for field, allowed in (("dictate", HOLD_KEYS), ("ask", ASK_KEYS), ("combo", COMBOS)):
        v = str(hk.get(field, out[field]) or "off").strip().lower()
        out[field] = v if v in allowed else out[field]
    out["handsfree"] = bool(hk.get("handsfree", True))
    if out["ask"] == out["dictate"]:
        out["ask"] = "off"
    return out


def hotkeys(path: str = CONFIG_YAML) -> dict:
    """dictate (hold key), ask (held with it), combo (second hold), handsfree (combo + Space)."""
    return load(path).get("hotkeys") or dict(DEFAULTS["hotkeys"])


def _replace_block(name: str, block: list[str], path: str) -> None:
    """Swap the top-level ``name:`` block (its line plus the indented ones under
    it) for ``block``, or append it. Comments elsewhere in the file survive."""
    ensure_default_config(path)
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    out, i, done = [], 0, False
    while i < len(lines):
        if lines[i].startswith(name + ":"):
            i += 1
            while i < len(lines) and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
                i += 1
            out.extend(block)
            done = True
            continue
        out.append(lines[i])
        i += 1
    if not done:
        out.extend(block)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")
    _CACHE["data"] = None


def set_hotkeys(values: dict, path: str = CONFIG_YAML) -> None:
    hk = _parse_hotkeys(values)
    _replace_block("hotkeys", [
        "hotkeys:               # also set from the tray: Settings...",
        f"  dictate: {hk['dictate']}   # hold to dictate: {' | '.join(HOLD_KEYS)}",
        f"  ask: {hk['ask']}       # hold with it to ask Claude: {' | '.join(ASK_KEYS)}",
        f"  combo: {hk['combo']}     # second hold combo: {' | '.join(COMBOS)}",
        f"  handsfree: {'true' if hk['handsfree'] else 'false'}   # combo + Space toggles hands-free",
    ], path)


def set_cleanup_provider(provider: str, path: str = CONFIG_YAML) -> None:
    """Write ``cleanup.provider`` (and turn clean-up on or off with it),
    touching only those two lines of the cleanup block."""
    if provider not in PROVIDERS:
        raise ValueError(f"provider must be one of {PROVIDERS}, got {provider!r}")
    ensure_default_config(path)
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    try:
        start = next(i for i, l in enumerate(lines) if l.startswith("cleanup:"))
    except StopIteration:
        lines += ["cleanup:", "  enabled: true"]
        start = len(lines) - 2
    end = start + 1
    while end < len(lines) and (lines[end].startswith((" ", "\t")) or not lines[end].strip()):
        end += 1
    body = [l for l in lines[start + 1:end] if not l.strip().startswith(("provider:", "enabled:"))]
    body = [f"  enabled: {'false' if provider == 'off' else 'true'}",
            f"  provider: {provider if provider != 'off' else 'openrouter'}   # {' | '.join(p for p in PROVIDERS if p != 'off')}"] + body
    lines[start + 1:end] = body
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    _CACHE["data"] = None


def cleanup_provider(path: str = CONFIG_YAML) -> str:
    """"off" when clean-up is disabled, else who runs it."""
    c = cleanup_config(path)
    return c.get("provider", "openrouter") if c.get("enabled", True) else "off"


def cleanup_config(path: str = CONFIG_YAML) -> dict:
    return load(path)["cleanup"]


def instructions_config(path: str = CONFIG_YAML) -> dict:
    """Spoken-instruction settings: enabled, trigger words, budget_s."""
    return load(path).get("instructions") or dict(DEFAULTS["instructions"])


def audio_days(path: str = CONFIG_YAML) -> float:
    """Days a clip's audio is kept in history.sqlite; 0 = never saved."""
    return float((load(path).get("history") or {}).get("audio_days", 7.0))


def language(path: str = CONFIG_YAML) -> str:
    """"en", "pl" or "auto" -- which cleanup prompt a clip gets."""
    return load(path).get("language", "en")


def set_language(value: str, path: str = CONFIG_YAML) -> None:
    """Write ``language:`` into config.yaml, touching only that line so the
    file's comments and the cleanup block survive the tray menu."""
    if value not in LANGUAGES:
        raise ValueError(f"language must be one of {LANGUAGES}, got {value!r}")
    ensure_default_config(path)
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    for i, line in enumerate(lines):
        if line.startswith("language:"):
            lines[i] = f"language: {value}"
            break
    else:
        lines.append(f"language: {value}            # en | pl | auto -- also switchable from the tray menu")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    _CACHE["data"] = None


def engine(path: str = CONFIG_YAML) -> str:
    """"parakeet" (local, default) or "whisper" (cloud Whisper + GPT mini cleanup)."""
    return load(path).get("engine", "parakeet")


def whisper_config(path: str = CONFIG_YAML) -> dict:
    """model (None = the provider's default) and cleanup_model for engine: whisper."""
    return load(path).get("whisper") or dict(DEFAULTS["whisper"])


def set_engine(value: str, path: str = CONFIG_YAML) -> None:
    """Write ``engine:`` into config.yaml, touching only that line."""
    if value not in ENGINES:
        raise ValueError(f"engine must be one of {ENGINES}, got {value!r}")
    ensure_default_config(path)
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    for i, line in enumerate(lines):
        if line.startswith("engine:"):
            lines[i] = f"engine: {value}"
            break
    else:
        lines.append(f"engine: {value}        # parakeet | whisper -- also switchable from the tray menu (Engine)")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    _CACHE["data"] = None


def clarity_nudges_enabled(path: str = CONFIG_YAML) -> bool:
    """True if clarity nudges are enabled."""
    return load(path).get("clarity_nudges", True)


def set_clarity_nudges(enabled: bool, path: str = CONFIG_YAML) -> None:
    """Write ``clarity_nudges:`` into config.yaml."""
    ensure_default_config(path)
    with open(path, "r", encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    for i, line in enumerate(lines):
        if line.startswith("clarity_nudges:"):
            lines[i] = f"clarity_nudges: {'true' if enabled else 'false'}"
            break
    else:
        lines.append(f"clarity_nudges: {'true' if enabled else 'false'}    # true = show clarity nudges after each dictation; false = silent")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    _CACHE["data"] = None


if __name__ == "__main__":                                   # pragma: no cover
    print(ensure_default_config())
    print(load())
