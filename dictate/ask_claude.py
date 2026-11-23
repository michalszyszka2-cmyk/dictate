"""Backend for the ask box: run a question through headless Claude Code, save
the whole conversation as markdown where the brain can find it.

Claude Code's desktop app never lists sessions started with ``claude -p`` (it
only shows the ones it started itself; checked 22 Oct 2026), so the full
conversation goes to ``hq/chats/ask-box/`` instead -- the same shape
``hq/antigravity_export.py`` gives Antigravity chats -- and that file is
upserted into ``hq/.cache/brain.db`` so ``brain.py ask`` finds it straight
away, without waiting for a re-index.

Runs on the person's Claude plan through the ``claude`` CLI: no API key, no
extra spend. On a PC without ``D:\\projects`` (anyone else's) the folder list
starts at the home folder and conversations go to ``%APPDATA%\\dictate\\ask-box``.
"""

from __future__ import annotations

import datetime
import json
import os
import re
import shutil
import sqlite3
import subprocess
from pathlib import Path

from dictate import paths

_WORKSPACE = Path(os.environ.get("DICTATE_WORKSPACE") or "")  # optional: a folder with hq/ for chats and brain.db
ROOT = _WORKSPACE if _WORKSPACE.name and (_WORKSPACE / "hq").is_dir() else Path.home()
CHATS = (ROOT / "hq" / "chats" / "ask-box") if ROOT == _WORKSPACE else paths.LOCAL_DIR / "ask-box"
BRAIN_DB = _WORKSPACE / "hq" / ".cache" / "brain.db"
ASKER = "You"
SESSIONS = Path.home() / ".claude" / "projects"

MODELS = ("opus", "sonnet", "haiku")
EFFORTS = ("low", "medium", "high")
TIMEOUT_S = 900

# Kept free of cmd.exe metacharacters (& | < > ^ " %): claude is a .cmd shim,
# so the argument goes through cmd.exe's parser.
BRIEF = (
    "This question came from a small floating ask box on the desktop. "
    "Reply in two or three short sentences of plain text with no headings, lists or markdown. "
    "If the question needs work first, do the work, then say what you found or did in two or three sentences."
)


def folders() -> list[str]:
    """ROOT and its folders, hidden ones left out."""
    subs = sorted(
        (p for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith((".", "_", "$"))),
        key=lambda p: p.name.lower(),
    )
    return [str(ROOT)] + [str(p) for p in subs]


def _claude_exe() -> str:
    exe = shutil.which("claude")
    if exe:
        return exe
    guess = Path(os.environ.get("APPDATA", "")) / "npm" / "claude.cmd"
    if guess.exists():
        return str(guess)
    raise FileNotFoundError("The ask box needs Claude Code installed and signed in "
                            "(claude.com/claude-code). It was not found on this PC.")


def ask(prompt: str, cwd: str, model: str, effort: str, session_id: str | None = None) -> dict:
    """One turn. Returns {"text", "session_id", "is_error", "seconds"}."""
    cmd = [_claude_exe(), "-p", "--output-format", "json", "--model", model,
           "--effort", effort, "--append-system-prompt", BRIEF]
    if session_id:
        cmd += ["--resume", session_id]
    proc = subprocess.run(
        cmd, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=cwd, timeout=TIMEOUT_S, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    out = None
    for line in reversed(proc.stdout.strip().splitlines()):
        try:
            out = json.loads(line)
            break
        except ValueError:
            continue
    if not isinstance(out, dict):
        err = (proc.stderr or proc.stdout or "no output").strip()
        return {"text": f"Claude Code failed: {err[-400:]}", "session_id": session_id,
                "is_error": True, "seconds": 0.0}
    return {
        "text": (out.get("result") or "").strip() or "(no reply)",
        "session_id": out.get("session_id") or session_id,
        "is_error": bool(out.get("is_error")),
        "seconds": (out.get("duration_ms") or 0) / 1000,
    }


# -- the full conversation as markdown ------------------------------------------
def _session_file(session_id: str) -> Path | None:
    hits = list(SESSIONS.glob(f"*/{session_id}.jsonl"))
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def _hhmm(ts: str | None) -> str:
    try:
        t = datetime.datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone()
        return t.strftime("%H:%M")
    except Exception:
        return ""


def _short(value, n: int = 140) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _render_turns(session_path: Path) -> list[str]:
    parts: list[str] = []
    for line in session_path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            d = json.loads(line)
        except ValueError:
            continue
        kind = d.get("type")
        if kind not in ("user", "assistant") or d.get("isMeta"):
            continue
        content = (d.get("message") or {}).get("content")
        when = _hhmm(d.get("timestamp"))
        if kind == "user":
            if isinstance(content, str) and content.strip():
                parts.append(f"## {ASKER} — {when}\n\n{content.strip()}\n")
            continue
        for block in content if isinstance(content, list) else []:
            if block.get("type") == "text" and block.get("text", "").strip():
                parts.append(f"## Claude — {when}\n\n{block['text'].strip()}\n")
            elif block.get("type") == "tool_use":
                parts.append(f"> tool: {block.get('name')} — `{_short(block.get('input'))}`\n")
    return parts


def _slug(s: str) -> str:
    return (re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:50]) or "question"


def chat_path(first_session_id: str, first_prompt: str) -> Path:
    existing = list(CHATS.glob(f"*-{first_session_id[:8]}.md"))
    if existing:
        return existing[0]
    today = datetime.date.today().isoformat()
    return CHATS / f"{today}-{_slug(first_prompt)}-{first_session_id[:8]}.md"


def save_conversation(first_session_id: str, latest_session_id: str, first_prompt: str,
                      cwd: str, model: str) -> Path | None:
    """Write (or rewrite) the conversation file and index it for the brain."""
    src = _session_file(latest_session_id) or _session_file(first_session_id)
    if src is None:
        return None
    CHATS.mkdir(parents=True, exist_ok=True)
    path = chat_path(first_session_id, first_prompt)
    title = _short(first_prompt, 80)
    today = datetime.date.today().isoformat()
    captured = path.name[:10] if path.exists() else today
    body = (
        "---\n"
        f"captured: {captured}\n"
        "origin: ask-box\n"
        f"title: {json.dumps(title, ensure_ascii=False)}\n"
        f"workspace: {cwd}\n"
        f"conversation: {latest_session_id}\n"
        f"model: {model}\n"
        f"exported: {today} (dictate ask box)\n"
        "---\n\n"
        f"# Ask box: {title}\n\n"
        + "\n".join(_render_turns(src))
    )
    path.write_text(body, encoding="utf-8")
    _index(path, title, body)
    return path


def _index(path: Path, title: str, body: str) -> None:
    """Upsert into brain.py's FTS table (same columns build_index writes). A
    missing index is fine: brain.py builds it from disk on the next ask."""
    if not BRAIN_DB.exists():
        return
    heads = " ".join(re.findall(r"^#{1,4}\s+(.+)$", body, re.M))
    con = sqlite3.connect(BRAIN_DB, timeout=5)
    try:
        con.execute("DELETE FROM docs WHERE path = ?", (str(path),))
        con.execute("INSERT INTO docs VALUES (?,?,?,?)", (str(path), f"Ask box: {title}", heads, body))
        con.commit()
    finally:
        con.close()
