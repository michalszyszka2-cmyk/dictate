"""Clean-up through someone's own Claude or ChatGPT account, for the public build.

``cleanup.provider`` in config.yaml picks one (cleanup.py keeps OpenRouter):

  anthropic   Claude API key (ANTHROPIC_API_KEY), official ``anthropic`` SDK
  openai      OpenAI API key (OPENAI_API_KEY), chat completions over HTTP
  claude-app  the person's own Claude Code CLI, logged in on their Claude plan
  codex-app   the person's own Codex CLI, logged in on their ChatGPT plan

A monthly Claude or ChatGPT plan has no API key; the only way a plan does the
clean-up is through its own CLI, which costs ~10 s a clip (measured 29 Oct
2026: 11 s wall for ``claude -p --model haiku``, 1.6 s of it the model). So
the two "-app" providers get APP_BUDGET_S instead of the 1-2 s API budget.

Every function returns ``(text, usage)`` and raises on any failure; cleanup.py
turns a failure into the raw transcript.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ANTHROPIC_MODEL = "claude-haiku-5-5"   # fastest current Claude; a clip must paste in ~2 s
OPENAI_MODEL = "gpt-5.4-mini"
OPENAI_BASE = "https://api.openai.com/v1"

API_PROVIDERS = ("anthropic", "openai")
APP_PROVIDERS = ("claude-app", "codex-app")
API_BUDGET_S = 2.5    # a direct API call with a fresh connection
APP_BUDGET_S = 20.0   # CLI start-up dominates

KEY_NAMES = {"openrouter": "OPENROUTER_API_KEY", "anthropic": "ANTHROPIC_API_KEY",
             "openai": "OPENAI_API_KEY"}

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CLIENTS: dict = {}


def budget_for(provider: str) -> float | None:
    """Seconds the clean-up step needs at least for this provider; None = the configured one."""
    if provider in APP_PROVIDERS:
        return APP_BUDGET_S
    if provider in API_PROVIDERS:
        return API_BUDGET_S
    return None


# --------------------------------------------------------------------------- API keys
def anthropic_chat(key: str, system: str, user: str, max_tokens: int, timeout: float):
    import anthropic

    client = _CLIENTS.get(key)
    if client is None:
        client = anthropic.Anthropic(api_key=key, max_retries=0)
        _CLIENTS[key] = client
    msg = client.with_options(timeout=timeout).messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=max_tokens,
        system=system,
        thinking={"type": "disabled"},          # allowed on Haiku 5.5 at effort high or below
        output_config={"effort": "low"},
        messages=[{"role": "user", "content": user}],
    )
    if msg.stop_reason == "refusal":
        raise RuntimeError("refused")
    text = "".join(b.text for b in msg.content if b.type == "text")
    return text, {"prompt_tokens": msg.usage.input_tokens,
                  "completion_tokens": msg.usage.output_tokens, "cost_usd": None}


def openai_chat(key: str, system: str, user: str, max_tokens: int, timeout: float, session=None):
    import requests

    s = session or requests
    body = {"model": OPENAI_MODEL,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "max_completion_tokens": max_tokens,
            "reasoning_effort": "none"}
    hdr = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    r = s.post(OPENAI_BASE + "/chat/completions", headers=hdr, json=body, timeout=timeout)
    if r.status_code == 400 and "reasoning_effort" in r.text:
        body.pop("reasoning_effort")       # a model that cannot switch reasoning off
        r = s.post(OPENAI_BASE + "/chat/completions", headers=hdr, json=body, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    usage = data.get("usage") or {}
    text = (data["choices"][0].get("message") or {}).get("content") or ""
    return text, {"prompt_tokens": usage.get("prompt_tokens"),
                  "completion_tokens": usage.get("completion_tokens"), "cost_usd": None}


# --------------------------------------------------------------------------- monthly plans via their CLI
def _find(exe: str) -> str:
    found = shutil.which(exe)
    if found:
        return found
    guess = Path(os.environ.get("APPDATA", "")) / "npm" / f"{exe}.cmd"
    if guess.exists():
        return str(guess)
    raise FileNotFoundError(f"{exe} is not installed")


def app_installed(provider: str) -> bool:
    try:
        _find("claude" if provider == "claude-app" else "codex")
        return True
    except FileNotFoundError:
        return False


def _one_prompt(system: str, user: str) -> str:
    # Everything goes in on stdin: the CLIs are .cmd shims, so an argument
    # holding quotes or % would be mangled by cmd.exe on the way in.
    return system.strip() + "\n\n" + user


def claude_app(system: str, user: str, timeout: float):
    cmd = [_find("claude"), "-p", "--model", "haiku", "--output-format", "json",
           "--no-session-persistence", "--tools", "", "--strict-mcp-config",
           "--setting-sources", ""]
    with tempfile.TemporaryDirectory() as cwd:   # no project CLAUDE.md gets read in
        p = subprocess.run(cmd, input=_one_prompt(system, user), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", cwd=cwd, timeout=timeout,
                           creationflags=_NO_WINDOW)
    out = None
    for line in reversed(p.stdout.strip().splitlines()):
        try:
            out = json.loads(line)
            break
        except ValueError:
            continue
    if not isinstance(out, dict) or out.get("is_error"):
        raise RuntimeError("claude: " + (p.stderr or p.stdout or "no output")[-300:])
    return out.get("result") or "", {"prompt_tokens": None, "completion_tokens": None,
                                     "cost_usd": None}


def codex_app(system: str, user: str, timeout: float):
    with tempfile.TemporaryDirectory() as cwd:
        last = os.path.join(cwd, "last.txt")
        cmd = [_find("codex"), "exec", "--skip-git-repo-check", "--sandbox", "read-only",
               "--color", "never", "--output-last-message", last, "-"]
        p = subprocess.run(cmd, input=_one_prompt(system, user), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", cwd=cwd, timeout=timeout,
                           creationflags=_NO_WINDOW)
        if p.returncode != 0 or not os.path.exists(last):
            raise RuntimeError("codex: " + (p.stderr or p.stdout or "no output")[-300:])
        text = Path(last).read_text(encoding="utf-8", errors="replace")
    return text, {"prompt_tokens": None, "completion_tokens": None, "cost_usd": None}


def call(provider: str, key: str, system: str, user: str, max_tokens: int, timeout: float):
    if provider == "anthropic":
        return anthropic_chat(key, system, user, max_tokens, timeout)
    if provider == "openai":
        return openai_chat(key, system, user, max_tokens, timeout)
    if provider == "claude-app":
        return claude_app(system, user, timeout)
    if provider == "codex-app":
        return codex_app(system, user, timeout)
    raise ValueError(provider)
