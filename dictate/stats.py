"""``python -m dictate.stats --json`` -- one JSON object summarising
``%APPDATA%\\dictate\\history.sqlite``, read-only, for the HQ Credits tab's
Dictation block (``hq/app/src/lib/credits/dictateStats.ts`` runs this and
parses stdout).

Windows: ``today`` (local calendar day), ``7d``, ``30d``, ``all`` (rolling
from *now*, except ``today`` which is midnight-to-now). Never crashes on a
missing or empty database -- every window then reads as zeros.  Reads only;
never writes to history.sqlite, never touches the audio blob column, never
prints any transcript text.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import statistics
import time
from datetime import datetime
from pathlib import Path

DB_PATH = Path(os.environ.get("APPDATA", "")) / "dictate" / "history.sqlite"

WINDOWS = ("today", "7d", "30d", "all")

# Anything slower than this is not a latency: a caller passed t_release=0
# (old unit tests logged ~1.8e9 s). Kept out of the percentiles.
MAX_E2E_S = 600.0

ASSUMPTIONS = {"typingWpm": 40, "wisprProUsdPerMonth": 12}


def _empty_window() -> dict:
    return {
        "clips": 0,
        "words": 0,
        "speakingSeconds": 0.0,
        "cleanupOk": 0,
        "cleanupSkipped": 0,
        "verbatim": 0,
        "medianE2eS": None,
        "p90E2eS": None,
        "costUsd": 0.0,
        "byApp": [],
        "activeDays": 0,
        "typingMinutesSaved": 0.0,
    }


def _window_start(name: str, now: float) -> float:
    if name == "today":
        d = datetime.fromtimestamp(now)
        return datetime(d.year, d.month, d.day).timestamp()
    if name == "7d":
        return now - 7 * 86400
    if name == "30d":
        return now - 30 * 86400
    return 0.0  # "all"


def _words(row: sqlite3.Row) -> int:
    text = row["formattedText"] or row["asrText"] or ""
    return len(text.split())


def _percentile(values: list, p: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    k = (len(s) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(s) - 1)
    if lo == hi:
        return s[lo]
    frac = k - lo
    return s[lo] + (s[hi] - s[lo]) * frac


def _summarise(rows: list) -> dict:
    if not rows:
        return _empty_window()
    words = sum(_words(r) for r in rows)
    speaking = sum((r["duration"] or 0.0) for r in rows)
    cleanup_ok = sum(1 for r in rows if r["cleanup"] == "ok")
    verbatim = sum(1 for r in rows if r["cleanup"] == "verbatim")
    cleanup_skipped = len(rows) - cleanup_ok - verbatim
    e2e = [r["e2eLatency"] for r in rows
           if r["e2eLatency"] is not None and 0 <= r["e2eLatency"] <= MAX_E2E_S]
    cost = sum((r["costUsd"] or 0.0) for r in rows)
    by_app: dict = {}
    days: set = set()
    for r in rows:
        app = r["app"] or "unknown"
        by_app[app] = by_app.get(app, 0) + _words(r)
        if r["timestamp"]:
            d = datetime.fromtimestamp(r["timestamp"])
            days.add((d.year, d.month, d.day))
    top_apps = sorted(by_app.items(), key=lambda kv: kv[1], reverse=True)[:6]
    typing_minutes_saved = max(0.0, words / ASSUMPTIONS["typingWpm"] - speaking / 60.0)
    return {
        "clips": len(rows),
        "words": words,
        "speakingSeconds": round(speaking, 1),
        "cleanupOk": cleanup_ok,
        "cleanupSkipped": cleanup_skipped,
        "verbatim": verbatim,
        "medianE2eS": round(statistics.median(e2e), 3) if e2e else None,
        "p90E2eS": round(_percentile(e2e, 0.9), 3) if e2e else None,
        "costUsd": round(cost, 6),
        "byApp": [{"app": a, "words": w} for a, w in top_apps],
        "activeDays": len(days),
        "typingMinutesSaved": round(typing_minutes_saved, 1),
    }


def _engine_name() -> str | None:
    try:
        from . import engine  # local import: keeps a plain --json call fast if this ever fails
        return engine.MODEL_DIR.name
    except Exception:
        return None


def _cleanup_model_config() -> str | None:
    try:
        from . import config
        return config.cleanup_config().get("model")
    except Exception:
        return None


def compute(db_path: Path = DB_PATH) -> dict:
    now = time.time()
    out: dict = {
        "windows": {w: _empty_window() for w in WINDOWS},
        "firstClipAt": None,
        "lastClipAt": None,
        "engine": _engine_name(),
        "cleanupModel": _cleanup_model_config(),
        "assumptions": ASSUMPTIONS,
        "generatedAt": datetime.fromtimestamp(now).isoformat(),
        "dbExists": db_path.exists(),
    }
    if not db_path.exists():
        return out

    try:
        conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
    except sqlite3.OperationalError:
        return out

    try:
        try:
            all_rows = conn.execute(
                "SELECT asrText, formattedText, app, duration, e2eLatency, "
                "cleanup, costUsd, timestamp FROM History ORDER BY timestamp ASC"
            ).fetchall()
        except sqlite3.OperationalError:
            all_rows = []

        if all_rows:
            out["firstClipAt"] = datetime.fromtimestamp(all_rows[0]["timestamp"]).isoformat()
            out["lastClipAt"] = datetime.fromtimestamp(all_rows[-1]["timestamp"]).isoformat()

        for name in WINDOWS:
            start = _window_start(name, now)
            rows = [r for r in all_rows if (r["timestamp"] or 0) >= start]
            out["windows"][name] = _summarise(rows)
    finally:
        conn.close()

    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true", help="print the stats as one JSON object")
    args = ap.parse_args()
    data = compute()
    if args.json:
        print(json.dumps(data))
    else:
        print(json.dumps(data, indent=2))


if __name__ == "__main__":
    main()
