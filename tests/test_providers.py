"""cleanup.provider routing (providers.py). The API ones are faked; the
Claude-app one runs for real only with DICTATE_LIVE_APP=1 (~10 s, uses the
logged-in Claude plan)."""
import os
import types

import pytest

from dictate import cleanup, providers

RAW = "so um i think we should uh move the the meeting to thursday afternoon"


def _cfg(provider):
    return lambda: {"enabled": True, "model": None, "budget_s": 1.2, "provider": provider}


@pytest.mark.parametrize("provider,keyname", [("anthropic", "ANTHROPIC_API_KEY"),
                                              ("openai", "OPENAI_API_KEY")])
def test_api_provider_uses_its_key(monkeypatch, provider, keyname):
    seen = {}

    def fake_call(p, key, system, user, max_tokens, timeout):
        seen.update(p=p, key=key, user=user, timeout=timeout)
        return "I think we should move the meeting to Thursday afternoon.", {}

    monkeypatch.setattr(cleanup, "cleanup_config", _cfg(provider))
    monkeypatch.setattr(cleanup, "mode_for", lambda app: "cleanup")
    monkeypatch.setattr(providers, "call", fake_call)
    monkeypatch.setenv(keyname, "test-key")
    text, status = cleanup.clean(RAW, "notepad.exe", "")
    assert status == "ok" and text.startswith("I think")
    assert seen["p"] == provider and seen["key"] == "test-key"
    assert "<transcript>" in seen["user"] and seen["timeout"] > 2.0   # API floor, not 1.2


def test_missing_key_pastes_raw(monkeypatch):
    monkeypatch.setattr(cleanup, "cleanup_config", _cfg("anthropic"))
    monkeypatch.setattr(cleanup, "mode_for", lambda app: "cleanup")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(cleanup, "_load_env_file", lambda: None)
    assert cleanup.clean(RAW, "notepad.exe", "") == (RAW, "skipped:error")


def test_app_failure_pastes_raw(monkeypatch):
    monkeypatch.setattr(cleanup, "cleanup_config", _cfg("codex-app"))
    monkeypatch.setattr(cleanup, "mode_for", lambda app: "cleanup")
    monkeypatch.setattr(providers, "_find", lambda exe: (_ for _ in ()).throw(FileNotFoundError(exe)))
    assert cleanup.clean(RAW, "notepad.exe", "") == (RAW, "skipped:error")


def test_openai_request_shape(monkeypatch):
    calls = []

    class S:
        def post(self, url, headers, json, timeout):
            calls.append(dict(json))
            if len(calls) == 1:
                return types.SimpleNamespace(status_code=400, text="reasoning_effort not supported")
            return types.SimpleNamespace(
                status_code=200, text="", raise_for_status=lambda: None,
                json=lambda: {"choices": [{"message": {"content": "Hi."}}], "usage": {}})

    text, _ = providers.openai_chat("k", "sys", "user", 64, 2.0, session=S())
    assert text == "Hi."
    assert calls[0]["reasoning_effort"] == "none" and "reasoning_effort" not in calls[1]
    assert calls[1]["max_completion_tokens"] == 64 and "temperature" not in calls[1]


@pytest.mark.skipif(os.environ.get("DICTATE_LIVE_APP") != "1", reason="live Claude plan call")
def test_claude_app_live(monkeypatch):
    monkeypatch.setattr(cleanup, "cleanup_config", _cfg("claude-app"))
    monkeypatch.setattr(cleanup, "mode_for", lambda app: "cleanup")
    text, status = cleanup.clean(RAW, "notepad.exe", "")
    print(text, cleanup.LAST["tried"])
    assert status == "ok" and "um" not in text.split() and "Thursday" in text
