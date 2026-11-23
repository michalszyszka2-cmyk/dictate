"""SQLite history log at %APPDATA%\\dictate\\history.sqlite.

Mirrors Wispr Flow's own History columns (asrText, formattedText, timestamp,
app, duration, e2eLatency) plus windowTitle, cleanup (status string, e.g.
"ok" / "skipped") and the raw audio blob, so later evals can run on real
clips.

Audio is ~32 KB a second of speech (150 MB after the first 600 clips), text
~1 KB a clip, so only the audio is aged out: ``prune_audio`` blanks the blob
on rows older than config.yaml ``history.audio_days`` (default 7) and
VACUUMs the file back down. The text rows are kept for good.
"""

from __future__ import annotations

import io
import os
import sqlite3
import time
import wave
from pathlib import Path

import numpy as np

DB_PATH = Path(os.environ.get("APPDATA", "")) / "dictate" / "history.sqlite"


def default_db_path() -> Path:
    """DB_PATH, unless DICTATE_HISTORY_DB points elsewhere. Read on every call
    (not at import) so tests/conftest.py can redirect it per test and never
    write into the real history."""
    override = os.environ.get("DICTATE_HISTORY_DB")
    return Path(override) if override else DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS History (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asrText TEXT,
    formattedText TEXT,
    timestamp REAL,
    app TEXT,
    windowTitle TEXT,
    duration REAL,
    e2eLatency REAL,
    cleanup TEXT,
    audio BLOB
);
"""

#: Added for per-clip cost logging (cleanup.py's LAST + pipeline.py's stage
#: timings). Nullable, migrated in with ALTER TABLE so existing rows and the
#: Wispr-imported history are untouched.
_NEW_COLUMNS = [
    ("cleanupModel", "TEXT"),
    ("promptTokens", "INTEGER"),
    ("completionTokens", "INTEGER"),
    ("costUsd", "REAL"),
    ("asrMs", "REAL"),
    ("cleanupMs", "REAL"),
    ("pasteMs", "REAL"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent ALTER TABLE ... ADD COLUMN for each of _NEW_COLUMNS."""
    for name, sqltype in _NEW_COLUMNS:
        try:
            conn.execute(f"ALTER TABLE History ADD COLUMN {name} {sqltype}")
        except sqlite3.OperationalError as exc:
            if "duplicate column" not in str(exc).lower():
                raise


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    db_path = db_path or default_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    conn.execute(_SCHEMA)
    _migrate(conn)
    conn.commit()
    return conn


def _wav_bytes(audio: "np.ndarray | None", sample_rate: int = 16_000) -> bytes | None:
    if audio is None or len(audio) == 0:
        return None
    pcm16 = np.clip(audio, -1.0, 1.0)
    pcm16 = (pcm16 * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())
    return buf.getvalue()


def log(
    asrText: str,
    formattedText: str,
    app: str,
    windowTitle: str,
    duration: float,
    e2eLatency: float,
    cleanup: str = "ok",
    audio=None,
    sample_rate: int = 16_000,
    timestamp: float | None = None,
    db_path: Path | None = None,
    cleanupModel: str | None = None,
    promptTokens: int | None = None,
    completionTokens: int | None = None,
    costUsd: float | None = None,
    asrMs: float | None = None,
    cleanupMs: float | None = None,
    pasteMs: float | None = None,
    keep_audio: bool = True,
) -> int:
    """Insert one clip into History. Returns the new row id.

    ``cleanupModel``/``promptTokens``/``completionTokens``/``costUsd`` come
    from ``cleanup.LAST`` after a clip's cleanup pass; the ``*Ms`` timings
    come from the pipeline's own per-stage measurements. All optional and
    nullable -- a verbatim-mode or cleanup-skipped clip simply logs None.
    """
    conn = _connect(db_path)
    try:
        blob = _wav_bytes(audio, sample_rate) if keep_audio else None
        cur = conn.execute(
            """
            INSERT INTO History
                (asrText, formattedText, timestamp, app, windowTitle,
                 duration, e2eLatency, cleanup, audio,
                 cleanupModel, promptTokens, completionTokens, costUsd,
                 asrMs, cleanupMs, pasteMs)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                asrText,
                formattedText,
                timestamp if timestamp is not None else time.time(),
                app,
                windowTitle,
                duration,
                e2eLatency,
                cleanup,
                blob,
                cleanupModel,
                promptTokens,
                completionTokens,
                costUsd,
                asrMs,
                cleanupMs,
                pasteMs,
            ),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def prune_audio(days: float, db_path: Path | None = None, now: float | None = None) -> int:
    """Drop the audio of clips older than ``days`` (text stays) and shrink the
    file. Returns how many clips lost their audio."""
    cutoff = (now if now is not None else time.time()) - days * 86400
    conn = _connect(db_path)
    try:
        n = conn.execute("UPDATE History SET audio = NULL "
                         "WHERE audio IS NOT NULL AND timestamp < ?", (cutoff,)).rowcount
        conn.commit()
        if n:
            conn.execute("VACUUM")
        return n
    finally:
        conn.close()


def recent(n: int = 10, db_path: Path | None = None):
    conn = _connect(db_path)
    try:
        cur = conn.execute(
            "SELECT id, asrText, formattedText, timestamp, app, windowTitle, "
            "duration, e2eLatency, cleanup FROM History ORDER BY id DESC LIMIT ?",
            (n,),
        )
        return cur.fetchall()
    finally:
        conn.close()


if __name__ == "__main__":
    rid = log(
        asrText="test one two",
        formattedText="Test one two.",
        app="python.exe",
        windowTitle="history.py smoke test",
        duration=1.2,
        e2eLatency=0.55,
        cleanup="ok",
        audio=None,
    )
    print(f"Logged row {rid} to {default_db_path()}")
    print(recent(5))
