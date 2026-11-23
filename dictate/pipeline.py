"""The dictation pipeline: key-release -> text-on-screen.

``run(audio, app, title, t_release)`` wires streams A-D together in order:

    Parakeet (engine.transcribe)
    -> vocab.apply (stream C, deterministic, before any LLM)
    -> policy.mode_for(app) decides verbatim vs cleanup
       (a spoken "Dictate, ..." instruction forces cleanup, see instruct.py)
    -> cleanup.clean (stream D, only when mode == "cleanup")
    -> deliver.paste
    -> history.log

``e2eLatency`` is measured from ``t_release`` (key-release, passed in by the
caller -- app.py captures it before anything else happens) to the moment
``deliver.paste`` has been sent. Per-stage timings (``asr_ms``, ``vocab_ms``,
``cleanup_ms``, ``paste_ms``) are measured independently so a slow stage can
be identified without re-deriving it from e2eLatency.

An empty/blank transcript delivers nothing (there is nothing to paste) but is
still logged, with ``cleanup="skipped:empty"``.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

from . import config, deliver, engine, history, instruct, policy, whisper_api
from . import cleanup as cleanup_mod
from . import vocab as vocab_mod

# Soft internal target, tighter than the 1.5 s hard bar so paste + logging
# overhead after cleanup returns doesn't push the total over it. Cleanup's
# per-clip budget shrinks to fit whatever of this is left once ASR has run,
# capped by config.yaml's cleanup.budget_s (never given more than configured,
# only ever less if the clip's ASR pass ate into the target).
# deliver.paste() waits up to 300 ms for the clipboard-restore signal (see
# deliver.py's docstring: the sequence number almost never fires early, so
# that wait is close to a fixed cost) -- PASTE_RESERVE_S needs to cover that,
# not just the SendInput calls either side of it.
TARGET_E2E_S = 1.4          # overridden by config.yaml cleanup.e2e_target_s (set higher when a paid model is on)
PASTE_RESERVE_S = 0.35
MIN_CLEANUP_BUDGET_S = 0.15
PRUNE_EVERY_S = 6 * 3600    # age out old audio at most this often (first clip after start, then every 6 h)
_LAST_PRUNE = [0.0]


def cleanup_budget(t_release: float) -> float:
    """Seconds cleanup may take for a clip released at ``t_release``: whatever
    is left of the e2e target after ASR, capped by ``cleanup.budget_s``.
    Shared with ``lengthcheck`` so the test times out exactly where live does."""
    try:
        configured_budget = float((config.cleanup_config() or {}).get("budget_s", 1.2) or 1.2)
    except Exception:
        configured_budget = 1.2
    elapsed = time.time() - t_release
    try:
        target_e2e = float((config.cleanup_config() or {}).get("e2e_target_s", TARGET_E2E_S) or TARGET_E2E_S)
    except Exception:
        target_e2e = TARGET_E2E_S
    return max(MIN_CLEANUP_BUDGET_S,
               min(configured_budget, target_e2e - elapsed - PASTE_RESERVE_S))


def _audio_days() -> float:
    try:
        return config.audio_days()
    except Exception:
        return 7.0


def _maybe_prune() -> None:
    """Drop audio older than ``history.audio_days``. Runs after the paste, on
    the pipeline's worker thread, so it never delays text on screen."""
    if time.time() - _LAST_PRUNE[0] < PRUNE_EVERY_S:
        return
    _LAST_PRUNE[0] = time.time()
    try:
        history.prune_audio(_audio_days())
    except Exception:
        pass


AB_LOG = os.path.join(config.APPDIR, "engine-ab.jsonl")


def transcribe(audio) -> tuple:
    """(text, engine_used, est_cost_usd). Cloud Whisper when config.yaml says
    ``engine: whisper``; on any failure that clip falls back to Parakeet."""
    if audio is None or not len(audio):
        return "", config.engine(), None
    if config.engine() == "whisper":
        try:
            lang = config.language()
            text = whisper_api.transcribe(audio, engine.SAMPLE_RATE,
                                          language=lang if lang in ("en", "pl") else None,
                                          model=config.whisper_config().get("model"))
            return text, "whisper", whisper_api.LAST.get("est_cost_usd")
        except Exception as exc:
            text = engine.transcribe(audio)
            return text, "parakeet:fallback(%s)" % type(exc).__name__, None
    return engine.transcribe(audio), "parakeet", None


def _ab_log(row: dict) -> None:
    """One line per clip so the two engines can be compared side by side
    (history.sqlite has the texts; join on ts)."""
    try:
        with open(AB_LOG, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except Exception:
        pass


def run(audio, app: str, title: str, t_release: float, *, deliver_fn=None, notify_fn=None) -> dict:
    """Run one clip through the full pipeline.

    ``audio``: float32 16 kHz mono numpy array (may be empty).
    ``app``, ``title``: foreground process name / window title at key-release.
    ``t_release``: ``time.time()`` at key-release -- e2eLatency is measured
        from here, not from when this function was called (the caller may
        queue this call behind other work).
    ``deliver_fn``: override for the delivery call (defaults to
        ``deliver.paste``); tests pass this to point delivery at a specific
        window without stealing the real foreground focus mid-run.
    ``notify_fn``: optional callback for nudge notifications; called with a
        single-line feedback string if a nudge rule fires. Executed in a daemon
        thread with a 500ms timeout.

    Returns a dict with asrText, formattedText, app, windowTitle, duration,
    e2eLatency, cleanup (status string), and per-stage *_ms timings -- the
    same shape ``history.log`` is called with, plus the timings.
    """
    if deliver_fn is None:
        deliver_fn = deliver.paste

    duration = len(audio) / engine.SAMPLE_RATE if audio is not None and len(audio) else 0.0

    t0 = time.monotonic()
    asr_text, asr_engine, asr_cost = transcribe(audio)
    asr_ms = (time.monotonic() - t0) * 1000.0

    t0 = time.monotonic()
    vocab_text = vocab_mod.apply(asr_text) if asr_text else ""
    vocab_ms = (time.monotonic() - t0) * 1000.0

    mode = policy.mode_for(app)

    # "Dictate, make this bullet points": an instruction to the editor forces
    # cleanup (even in a verbatim app) and gets its own, longer budget --
    # the user asked for it, so waiting a few seconds beats a raw paste.
    trigger = None
    try:
        icfg = config.instructions_config()
        if icfg.get("enabled", True) and vocab_text and instruct.detect(vocab_text, icfg["trigger"]):
            trigger = icfg["trigger"][0]
            mode = "cleanup"
    except Exception:
        trigger = None

    cleanup_ms = 0.0
    cleanup_status = "verbatim"
    formatted_text = vocab_text
    cleanup_model = None
    prompt_tokens = None
    completion_tokens = None
    cost_usd = None
    if vocab_text and mode == "cleanup":
        dynamic_budget = float(icfg.get("budget_s", 6.0)) if trigger else cleanup_budget(t_release)
        try:
            language = cleanup_mod.resolve_language(config.language(), vocab_text)
        except Exception:
            language = "en"
        t0 = time.monotonic()
        formatted_text, cleanup_status = cleanup_mod.clean(
            vocab_text, app, vocab_mod.vocab_for_prompt(), budget=dynamic_budget, language=language,
            # GPT mini answers in ~1 s warm, past the 1.2 s per-call cap tuned for
            # Flash-Lite -- without this the A/B quietly measures Gemini Flash instead.
            timeout=dynamic_budget if asr_engine == "whisper" else 1.2,
            trigger=trigger,
            model=icfg.get("model") if trigger else (
                config.whisper_config().get("cleanup_model") if asr_engine == "whisper" else None),
        )
        if trigger:
            if cleanup_status != "ok":
                # Never paste "Dictate, make this bullet points" itself.
                formatted_text = instruct.strip(vocab_text, icfg["trigger"])
            cleanup_status += ":instructed"
        cleanup_ms = (time.monotonic() - t0) * 1000.0
        cleanup_model = cleanup_mod.LAST.get("model") or None
        prompt_tokens = cleanup_mod.LAST.get("prompt_tokens")
        completion_tokens = cleanup_mod.LAST.get("completion_tokens")
        cost_usd = cleanup_mod.LAST.get("cost_usd")
    elif not vocab_text:
        cleanup_status = "skipped:empty"

    if asr_cost:
        cost_usd = (cost_usd or 0.0) + asr_cost

    paste_ms = 0.0
    if formatted_text.strip():
        t0 = time.monotonic()
        deliver_fn(formatted_text)
        paste_ms = (time.monotonic() - t0) * 1000.0

    e2e_latency = time.time() - t_release

    history.log(
        asrText=asr_text,
        formattedText=formatted_text,
        app=app,
        windowTitle=title,
        duration=duration,
        e2eLatency=e2e_latency,
        cleanup=cleanup_status,
        audio=audio,
        sample_rate=engine.SAMPLE_RATE,
        cleanupModel=cleanup_model,
        promptTokens=prompt_tokens,
        completionTokens=completion_tokens,
        costUsd=cost_usd,
        asrMs=asr_ms,
        cleanupMs=cleanup_ms,
        pasteMs=paste_ms,
        keep_audio=_audio_days() > 0,
    )
    _maybe_prune()
    _ab_log({"ts": round(time.time(), 3), "engine": asr_engine, "duration": round(duration, 2),
             "asr_ms": round(asr_ms), "cleanup_ms": round(cleanup_ms), "e2e": round(e2e_latency, 3),
             "cleanup": cleanup_status, "cleanup_model": cleanup_model, "cost_usd": cost_usd,
             "words": len(formatted_text.split())})

    # Start daemon thread for clarity nudge notification (if enabled in config)
    if notify_fn is not None and asr_text and len(asr_text.split()) >= 25:
        def _nudge_worker():
            """Worker thread to compute and call nudge notification (500ms budget)."""
            try:
                # Import clarity.hook module with guarded import (avoid duplicate path inserts)
                clarity_dir = os.environ.get("DICTATE_CLARITY_DIR")
                if not clarity_dir:
                    return
                if clarity_dir not in sys.path:
                    sys.path.insert(0, clarity_dir)
                from clarity.hook import nudge_line

                # Compute nudge line (should be fast, mostly scoring)
                line = nudge_line(asr_text)
                if line:
                    notify_fn(line)
            except Exception:
                # Silently ignore any exceptions (import, scoring, notification)
                pass

        worker = threading.Thread(target=_nudge_worker, daemon=True)
        worker.start()

    return {
        "asrText": asr_text,
        "formattedText": formatted_text,
        "app": app,
        "windowTitle": title,
        "duration": duration,
        "e2eLatency": e2e_latency,
        "cleanup": cleanup_status,
        "engine": asr_engine,
        "mode": mode,
        "asr_ms": asr_ms,
        "vocab_ms": vocab_ms,
        "cleanup_ms": cleanup_ms,
        "paste_ms": paste_ms,
        "cleanup_model": cleanup_model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "cost_usd": cost_usd,
    }
