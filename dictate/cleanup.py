"""LLM cleanup pass for one dictated clip.

One chat call per clip, OpenAI-compatible, provider chain:

  1. OpenRouter with a ``:free`` model chosen live from the model list and
     cached for 24 h in ``%APPDATA%\\dictate\\model-cache.json``.
  2. NVIDIA NIM (https://integrate.api.nvidia.com/v1), a fast instruct model.
  3. ``anthropic/claude-haiku-4-5`` via OpenRouter -- only when DICTATE_PAID=1.

The LLM never blocks delivery: every call is bounded by ``timeout`` seconds and
any failure returns the raw transcript with a ``skipped:*`` status.  ``clean()``
never raises.

Environment (keys are read from D:\\projects\\hq\\app\\.env.local when not
already in the environment, and are never logged):

  OPENROUTER_API_KEY   default provider
  NVIDIA_API_KEY       fallback provider
  DICTATE_PAID=1       allow the paid Haiku fallback as a last resort
  DICTATE_MODEL        pin the OpenRouter model id (skips the live pick)
  DICTATE_NIM_MODEL    pin the NVIDIA NIM model id
  DICTATE_EVAL_DB      used by tests/eval_cleanup.py, not here

``%APPDATA%\\dictate\\config.yaml`` (see ``config.py``) additionally controls,
without needing any of the above: ``cleanup.enabled`` (false = vocabulary
pass only, no LLM call), ``cleanup.model`` (null = free chain above; a model
id = use that single OpenRouter model instead, paid, off by default) and
``cleanup.budget_s`` (default wall-clock budget). ``DICTATE_MODEL`` /
``DICTATE_CLEANUP_BUDGET`` env vars still take precedence when set.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time

try:
    import requests
except Exception:                                            # pragma: no cover
    requests = None

try:
    from .policy import mode_for
except Exception:                                            # pragma: no cover
    try:
        from policy import mode_for
    except Exception:
        def mode_for(_app):
            return "cleanup"

from . import providers

try:
    from .config import cleanup_config
except Exception:                                            # pragma: no cover
    try:
        from config import cleanup_config
    except Exception:
        def cleanup_config():
            return {"enabled": True, "model": None, "budget_s": 1.2}

HERE = os.path.dirname(os.path.abspath(__file__))
PROMPT_PATH = os.path.join(HERE, "prompts", "cleanup.txt")
PROMPT_PATHS = {"en": PROMPT_PATH, "pl": os.path.join(HERE, "prompts", "cleanup_pl.txt")}
INSTRUCT_PATH = os.path.join(HERE, "prompts", "instruct.txt")
APPDIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "dictate")
MODEL_CACHE = os.path.join(APPDIR, "model-cache.json")
ENV_FILE = os.environ.get("DICTATE_ENV_FILE", "")  # optional extra .env with the API keys

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
NVIDIA_BASE = "https://integrate.api.nvidia.com/v1"
PAID_MODEL = "anthropic/claude-haiku-4-5"
FALLBACK_MODEL = "google/gemini-2.5-flash"   # second try behind config.yaml cleanup.model ($0.30/M in, $2.50/M out)
MODEL_CACHE_TTL = 24 * 3600

# NVIDIA NIM: ordered preference.  NIM's /models list is not an availability
# list -- most ids on it return 404 "Function not found for account", and the
# llama-3.1 instruct models reached end of life on 2026-08-26.  Probed
# 21 Sep 2026: nemotron-3-super-120b-a12b answers in ~1.7 s, the rest of the
# small instruct models 404 or cold-start past 15 s.
NIM_PREFERRED = [
    "nvidia/nemotron-3-super-120b-a12b",
    "nvidia/nemotron-3.5-lightning-30b-a3b",
    "mistralai/mistral-large-2-instruct",
]

# How many OpenRouter :free ids to try before falling through to NVIDIA.  The
# free pool is shared and answers 429 "temporarily rate-limited upstream"
# several times an hour, in ~0.3 s -- cheap enough to step over.
FREE_ATTEMPTS = int(os.environ.get("DICTATE_FREE_ATTEMPTS", "3"))

# Free models that are the wrong tool for this job whatever their size.
_ID_BLOCK = ("minimax-m3", "content-safety", "guard", "moderation", "embed",
             "rerank", "-tts", "-stt", "whisper", "image", "-vl")
_SLOW_HINT = ("reasoning", "thinking", "-r1", "deepseek-r")

_LOCK = threading.Lock()
_SESSIONS: dict = {}
_PROMPT_CACHE: dict = {}
_MODEL_MEM: dict = {}

#: Details of the most recent clean() call -- which model answered, what was
#: tried and how long each attempt took, plus (when the provider returns them)
#: prompt_tokens / completion_tokens / cost_usd.  For logging and for the
#: eval; the return value of clean() stays a plain (text, status) pair.
LAST: dict = {}

#: usage numbers from the most recent _chat() call that got a response --
#: set inside _chat (which returns only the content string, per its own
#: contract) and read by clean() right after _with_deadline returns.
#: OpenRouter fills these in when the request asks for them (see _chat's
#: ``body["usage"]``); NIM does not support the field and leaves them None.
_LAST_USAGE: dict = {}

#: model id -> (blocked_until, strikes).  The free pool answers 429
#: "temporarily rate-limited upstream" in bursts; a model that just refused is
#: demoted for a few minutes instead of being tried first on every clip.
_HEALTH: dict = {}
_COOLDOWN = 300.0


# --------------------------------------------------------------------------- keys
def _load_env_file() -> None:
    """Fill missing keys from this PC's secrets.env (paths.SECRETS_ENV, set from
    the tray), then hq's .env.local.  Values are never printed."""
    from dictate import paths

    for k, v in paths.read_secrets().items():
        if k in ("OPENROUTER_API_KEY", "NVIDIA_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY")                 and v and not os.environ.get(k):
            os.environ[k] = v
    if not os.path.exists(ENV_FILE):
        return
    try:
        with open(ENV_FILE, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k, v = k.strip(), v.strip().strip('"').strip("'")
                if k in ("OPENROUTER_API_KEY", "NVIDIA_API_KEY") and not os.environ.get(k):
                    os.environ[k] = v
    except Exception:
        pass


def _key(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        _load_env_file()
        v = os.environ.get(name)
    return v or ""


def _session(host: str) -> "requests.Session":
    """One keep-alive session per host -- a fresh TLS handshake costs ~0.3 s."""
    with _LOCK:
        s = _SESSIONS.get(host)
        if s is None:
            s = requests.Session()
            _SESSIONS[host] = s
        return s


def _drop_session(host: str) -> None:
    """Forget a session whose call was abandoned -- the orphaned thread may still
    be reading on that connection."""
    with _LOCK:
        _SESSIONS.pop(host, None)


class Abandoned(Exception):
    """A call that blew the wall-clock deadline and was left behind."""


def _with_deadline(host: str, fn, deadline: float):
    """Run ``fn`` in a daemon thread and give up on it after ``deadline``
    seconds.  requests' own timeout is per socket read, so a provider that
    trickles bytes (OpenRouter sends processing padding) can hold a call open
    far past it -- observed at 79 s against a 5 s timeout.  Delivery must never
    wait on that, so the call is abandoned rather than waited out."""
    box = {}

    def run():
        try:
            box["v"] = fn()
        except BaseException as exc:                 # noqa: BLE001
            box["e"] = exc

    th = threading.Thread(target=run, daemon=True)
    th.start()
    th.join(max(0.0, deadline))
    if th.is_alive():
        _drop_session(host)
        raise Abandoned("deadline %.2fs" % deadline)
    if "e" in box:
        raise box["e"]
    return box.get("v")


# --------------------------------------------------------------------------- prompt
def system_prompt(app: str, vocab: str, language: str = "en", trigger: str | None = None) -> str:
    """The cleanup prompt, or the instruction-mode prompt when ``trigger`` is set."""
    path = INSTRUCT_PATH if trigger else PROMPT_PATHS.get(language, PROMPT_PATH)
    if path not in _PROMPT_CACHE:
        with open(path, "r", encoding="utf-8") as fh:
            _PROMPT_CACHE[path] = fh.read()
    return _PROMPT_CACHE[path].replace("{app}", app or "unknown").replace(
        "{vocab}", vocab.strip() if vocab and vocab.strip() else "(none)").replace(
        "{trigger}", trigger or "")


# Words that are common in spoken Polish and never English words, so two of
# them in one clip is a safe tell even when the engine dropped the diacritics.
_POLISH_WORDS = frozenset("""
    się że nie jest czy jak ale już żeby bo tylko może będzie jestem mam dla tak też
    więc który która które bardzo teraz można trzeba chcę proszę dzięki dobrze wszystko
    jeszcze tego tym tej jego jej ich mnie mi ciebie tobie jestes jesteś sie ze juz zeby
    moze bedzie chce prosze dzieki wiec ktory ktora ktore mozna potem gdzie kiedy dlaczego
""".split())
_POLISH_LETTERS = set("ąćęłńóśźżĄĆĘŁŃÓŚŹŻ")


def detect_language(text: str) -> str:
    """"pl" or "en" for one transcript -- what ``language: auto`` runs on.
    Parakeet writes Polish with its diacritics, so one ą/ł/ż... is enough;
    the word list catches a clip that came back without them."""
    if any(ch in _POLISH_LETTERS for ch in text):
        return "pl"
    words = [w.strip(".,!?;:\"'()").lower() for w in text.split()]
    hits = sum(1 for w in words if w in _POLISH_WORDS)
    return "pl" if hits >= 2 or (words and hits / len(words) >= 0.3) else "en"


def resolve_language(setting: str, text: str) -> str:
    """The config's ``language`` (en | pl | auto) resolved for this clip."""
    if setting in ("en", "pl"):
        return setting
    return detect_language(text) if setting == "auto" else "en"


# --------------------------------------------------------------------------- model pick
def _size_b(model: dict) -> float:
    """Parameter count in billions, read off the id/name (OpenRouter does not
    publish one).  For a mixture-of-experts id like ``...-26b-a4b`` this is the
    total, 26."""
    text = (model.get("id", "") + " " + model.get("name", "")).lower()
    best = 0.0
    for m in re.finditer(r"(\d+(?:\.\d+)?)\s*b\b", text):
        try:
            best = max(best, float(m.group(1)))
        except ValueError:
            pass
    return best


def _active_b(model: dict) -> float:
    """Active parameters for an MoE id such as ``-a4b`` -- the number that
    decides how fast it answers.  0 when unknown."""
    m = re.search(r"-a(\d+(?:\.\d+)?)b", model.get("id", "").lower())
    return float(m.group(1)) if m else 0.0


def _rank_free(models: list) -> list:
    """Order the :free models best-first for a 1.2 s cleanup call."""
    out = []
    for m in models:
        mid = m.get("id", "")
        if not mid.endswith(":free"):
            continue
        low = mid.lower()
        if any(b in low for b in _ID_BLOCK):
            continue
        arch = m.get("architecture") or {}
        mods = arch.get("input_modalities") or ["text"]
        if "text" not in mods:
            continue
        size, active = _size_b(m), _active_b(m)
        slow = any(h in low or h in (m.get("name", "").lower()) for h in _SLOW_HINT)
        out.append((
            0 if size >= 20 else 1,        # brief: prefer >= 20B
            1 if slow else 0,              # a reasoning model cannot answer in 1.2 s
            active or size,                # fewer active params -> faster
            size,
            mid,
            m,
        ))
    out.sort(key=lambda t: (t[0], t[1], t[2], t[3], t[4]))
    return [t[4] for t in out]


def pick_free_model(force: bool = False, timeout: float = 6.0) -> str:
    """The cached :free model id, refreshed from OpenRouter at most daily."""
    pinned = os.environ.get("DICTATE_MODEL")
    if pinned:
        return pinned
    now = time.time()
    cached = _MODEL_MEM or _read_cache()
    if not force and cached and now - cached.get("picked_at", 0) < MODEL_CACHE_TTL:
        return cached.get("id", "")
    try:
        r = _session("openrouter").get(OPENROUTER_BASE + "/models", timeout=timeout)
        r.raise_for_status()
        ranked = _rank_free(r.json().get("data", []))
        if ranked:
            _write_cache({"id": ranked[0], "picked_at": now,
                          "shortlist": ranked[:6], "source": "openrouter"})
            return ranked[0]
    except Exception:
        pass
    return (cached or {}).get("id", "")          # stale pick beats no pick


def free_shortlist(timeout: float = 6.0) -> list:
    cached = _MODEL_MEM or _read_cache()
    if cached.get("shortlist"):
        return cached["shortlist"]
    pick_free_model(force=True, timeout=timeout)
    return (_MODEL_MEM or _read_cache()).get("shortlist", [])


def _read_cache() -> dict:
    try:
        with open(MODEL_CACHE, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        _MODEL_MEM.update(data)
        return data
    except Exception:
        return {}


def _write_cache(data: dict) -> None:
    _MODEL_MEM.clear()
    _MODEL_MEM.update(data)
    try:
        os.makedirs(APPDIR, exist_ok=True)
        tmp = MODEL_CACHE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, MODEL_CACHE)
    except Exception:
        pass


def pick_nim_model(timeout: float = 6.0) -> str:
    pinned = os.environ.get("DICTATE_NIM_MODEL")
    if pinned:
        return pinned
    cached = _read_cache()
    if cached.get("nim_id") and time.time() - cached.get("nim_at", 0) < MODEL_CACHE_TTL:
        return cached["nim_id"]
    ids = []
    try:
        key = _key("NVIDIA_API_KEY")
        r = _session("nvidia").get(NVIDIA_BASE + "/models",
                                   headers={"Authorization": "Bearer " + key},
                                   timeout=timeout)
        r.raise_for_status()
        ids = [m.get("id", "") for m in r.json().get("data", [])]
    except Exception:
        pass
    chosen = ""
    for want in NIM_PREFERRED:
        if not ids or want in ids:
            chosen = want
            break
    if not chosen:
        for mid in ids:
            if "instruct" in mid.lower():
                chosen = mid
                break
    if chosen:
        data = dict(cached)
        data.update({"nim_id": chosen, "nim_at": time.time()})
        _write_cache(data)
    return chosen


# --------------------------------------------------------------------------- output hygiene
_THINK = re.compile(
    r"<\s*(think|thinking|reason|reasoning|scratchpad|analysis)\s*>.*?"
    r"<\s*/\s*\1\s*>", re.S | re.I)
_OPEN_THINK = re.compile(r"^.*?<\s*/\s*(think|thinking|reason|reasoning|analysis)\s*>",
                         re.S | re.I)
_LEAD = re.compile(
    r"^(here('| i)s (the )?(cleaned|corrected|revised)[^:\n]*:|"
    r"cleaned( text)?:|corrected( text)?:|output:|result:)\s*", re.I)


def strip_reasoning(text: str) -> str:
    """Remove <think> blocks, fences, wrapping quotes and 'Here is...' lead-ins."""
    if not text:
        return ""
    t = _THINK.sub(" ", text)
    if re.search(r"<\s*/\s*(think|thinking|reason|reasoning|analysis)\s*>", t, re.I):
        t = _OPEN_THINK.sub("", t, count=1)
    t = re.sub(r"<\s*/?\s*(think|thinking|reason|reasoning|analysis)\s*>", " ", t, flags=re.I)
    t = t.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t).strip()
    t = _LEAD.sub("", t).strip()
    if len(t) > 1 and t[0] in "\"'\u201c\u2018" and t[-1] in "\"'\u201d\u2019":
        inner = t[1:-1]
        if '"' not in inner and "\u201d" not in inner:
            t = inner.strip()
    return re.sub(r"[ \t]+", " ", t).strip()


def _penalise(model: str, slow: bool, ok: bool = False) -> None:
    """Demote a model that just failed; clear the mark when it answers."""
    with _LOCK:
        if ok:
            _HEALTH.pop(model, None)
            return
        _until, strikes = _HEALTH.get(model, (0.0, 0))
        strikes += 1
        # A refusal (429/5xx) means the model is off the air for a while, so
        # demote it at once.  A single slow answer is ordinary variance on a
        # free endpoint -- only demote after two in a row, and for less time.
        if slow:
            # Strikes still order it behind a model that has not been slow, but
            # a slow model is never taken out of the running: on this free pool
            # the alternatives are mostly refusing outright.
            until = 0.0
        else:
            until = time.time() + _COOLDOWN
        _HEALTH[model] = (until, strikes)


def _healthy_first(models: list) -> list:
    now = time.time()
    return sorted(models, key=lambda m: (_HEALTH.get(m, (0.0, 0))[0] > now,
                                         _HEALTH.get(m, (0.0, 0))[1]))


def _plausible(raw: str, out: str, instructed: bool = False) -> bool:
    """Reject a reply that is not the same message back. An instructed clip
    may legitimately shrink ("make it shorter") or grow (bullets, a heading),
    so it only has to be non-empty and not an essay."""
    if not out:
        return False
    n_raw, n_out = len(raw.split()), len(out.split())
    if n_raw == 0:
        return False
    ratio = n_out / n_raw
    if instructed:
        return ratio <= 3.0
    return 0.5 <= ratio <= 1.6


# --------------------------------------------------------------------------- the call
def _chat(base: str, key: str, model: str, sys_prompt: str, raw: str,
          timeout: float, extra_headers: dict | None = None, instructed: bool = False) -> str:
    host = "openrouter" if "openrouter" in base else "nvidia"
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    body = {
        "model": model,
        # The transcript is fenced and the user turn ends with an editing
        # instruction: a bare request-shaped transcript ("can you show me three
        # sectors...") made gemini-2.5-flash-lite ANSWER it instead of editing
        # it (22 Sept, 5 of 28 clips) -- the plausibility check caught those as
        # "implausible", but the user got raw text.
        "messages": [{"role": "system", "content": sys_prompt},
                     {"role": "user", "content": "<transcript>" + chr(10) + raw + chr(10) + "</transcript>" + chr(10) + chr(10) + ("Return only the final text, with the instructions applied and removed." if instructed else "Return only the edited transcript.")}],
        "temperature": 0,
        "top_p": 1,
        "max_tokens": max(64, int(len(raw.split()) * (6 if instructed else 3)) + 32),
        "stream": False,
    }
    if host == "openrouter":
        # Ask OpenRouter not to spend the budget on a reasoning pass.
        body["reasoning"] = {"enabled": False, "exclude": True}
        body["provider"] = {"sort": "latency"}
        # Ask for per-call usage accounting (tokens + USD cost) in the
        # response body. NIM does not support this field; it is simply
        # ignored there, so _LAST_USAGE stays empty for a NIM answer.
        body["usage"] = {"include": True}
    elif "nemotron" in model:
        # NIM's nemotron chat template thinks out loud unless told not to.
        body["chat_template_kwargs"] = {"thinking": False}
    r = _session(host).post(base + "/chat/completions", headers=headers,
                            json=body, timeout=timeout)
    r.raise_for_status()
    data = r.json()
    if "error" in data and not data.get("choices"):
        raise RuntimeError("provider error")
    usage = data.get("usage") or {}
    _LAST_USAGE.clear()
    _LAST_USAGE.update({
        "prompt_tokens": usage.get("prompt_tokens"),
        "completion_tokens": usage.get("completion_tokens"),
        "cost_usd": usage.get("cost"),
    })
    msg = (data["choices"][0].get("message") or {})
    return msg.get("content") or ""


def clean(raw: str, app: str, vocab: str, timeout: float = 1.2, budget: float | None = None,
          language: str = "en", trigger: str | None = None,
          model: str | None = None) -> tuple:
    """Clean one dictated transcript.

    Returns ``(text, status)`` with status one of
    ``"ok"``, ``"skipped:timeout"``, ``"skipped:error"``, ``"verbatim"``,
    ``"skipped:disabled"``.
    Never raises; on any failure the raw transcript comes straight back.

    ``timeout`` is the hard per-call bound.  The whole provider chain is capped
    by ``budget`` seconds when the caller passes one explicitly (the pipeline
    does this -- it shrinks the budget by however much the ASR pass already
    used, so a slow clip cannot push the whole cleanup call past the e2e
    latency target); otherwise the cap is ``DICTATE_CLEANUP_BUDGET`` if set,
    else ``config.yaml``'s ``cleanup.budget_s`` (default 1.2s).

    ``trigger`` (e.g. ``"dictate"``) switches to instruction mode: the clip
    holds spoken instructions to the editor (see ``instruct.py``), so it
    gets ``prompts/instruct.txt`` and runs even in a verbatim app.
    ``model`` overrides ``cleanup.model`` for this call (instruction mode uses
    a stronger one: Flash-Lite turned list items into headings).
    """
    raw = (raw or "").strip()
    if not raw:
        return raw, "skipped:error"
    try:
        if not trigger and mode_for(app) == "verbatim":
            return raw, "verbatim"
    except Exception:
        pass

    cfg = {}
    try:
        cfg = cleanup_config()
    except Exception:
        cfg = {"enabled": True, "model": None, "budget_s": 1.2}
    if not cfg.get("enabled", True):
        return raw, "skipped:disabled"

    if requests is None:
        return raw, "skipped:error"

    if budget is not None:
        budget = float(budget)
    elif "DICTATE_CLEANUP_BUDGET" in os.environ:
        try:
            budget = float(os.environ["DICTATE_CLEANUP_BUDGET"])
        except ValueError:
            budget = timeout
    else:
        budget = float(cfg.get("budget_s", timeout) or timeout)
    started = time.monotonic()
    sys_prompt = system_prompt(app, vocab, language, trigger)
    instructed = bool(trigger)
    LAST.clear()
    LAST.update({"model": "", "tried": [], "status": "", "app": app,
                 "prompt_tokens": None, "completion_tokens": None, "cost_usd": None,
                 "language": language, "instructed": instructed})

    provider = cfg.get("provider", "openrouter")
    if provider in providers.API_PROVIDERS + providers.APP_PROVIDERS:
        return _clean_direct(provider, raw, sys_prompt, budget, instructed, started)

    attempts = []
    or_key = _key("OPENROUTER_API_KEY")
    or_hdr = {"HTTP-Referer": "https://github.com/michalszyszka2-cmyk/dictate", "X-Title": "dictate"}
    # config.yaml's cleanup.model (unless DICTATE_MODEL already pins one)
    # opts into a single paid OpenRouter model instead of the free chain --
    # off (None) by default, so paid cleanup stays off until you set it.
    configured_model = model or (cfg.get("model") if not os.environ.get("DICTATE_MODEL") else None)
    if or_key and configured_model:
        attempts.append((OPENROUTER_BASE, or_key, configured_model, or_hdr))
        # One stronger second try if the pinned model answers the transcript
        # instead of editing it (caught by _plausible) or times out.
        fb = os.environ.get("DICTATE_FALLBACK_MODEL", FALLBACK_MODEL)
        if fb and fb != configured_model:
            attempts.append((OPENROUTER_BASE, or_key, fb, or_hdr))
    elif or_key:
        free = pick_free_model()
        order = [free] + [m for m in (_MODEL_MEM.get("shortlist") or []) if m != free]
        order = _healthy_first([m for m in order if m])
        for mid in order[:max(1, FREE_ATTEMPTS)]:
            attempts.append((OPENROUTER_BASE, or_key, mid, or_hdr))
    nv_key = _key("NVIDIA_API_KEY")
    if nv_key and not configured_model:
        nim = pick_nim_model()
        if nim:
            attempts.append((NVIDIA_BASE, nv_key, nim, None))
    if os.environ.get("DICTATE_PAID") == "1" and or_key and not configured_model:
        attempts.append((OPENROUTER_BASE, or_key, PAID_MODEL, or_hdr))
    if not attempts:
        return raw, "skipped:error"

    status = "skipped:error"
    for base, key, model, extra in attempts:  # OpenRouter / NIM chain
        left = budget - (time.monotonic() - started)
        if left <= 0.05:
            break
        # An instructed clip gets the whole budget per call, not the
        # per-call cap tuned for one-line cleanups.
        slice_ = left if instructed else min(timeout, left)
        host = "openrouter" if "openrouter" in base else "nvidia"
        t0 = time.monotonic()
        _LAST_USAGE.clear()
        try:
            out = _with_deadline(
                host,
                lambda: _chat(base, key, model, sys_prompt, raw, slice_, extra, instructed),
                slice_ + 0.05)
        except Exception as exc:                    # timeouts included
            name = type(exc).__name__.lower()
            slow = "timeout" in name or "timedout" in name or "abandoned" in name
            status = "skipped:timeout" if slow else "skipped:error"
            _penalise(model, slow)
            LAST["tried"].append((model, round(time.monotonic() - t0, 3), status))
            continue
        dt = round(time.monotonic() - t0, 3)
        out = strip_reasoning(out)
        if _plausible(raw, out, instructed):
            LAST.update({"model": model, "status": "ok",
                         "prompt_tokens": _LAST_USAGE.get("prompt_tokens"),
                         "completion_tokens": _LAST_USAGE.get("completion_tokens"),
                         "cost_usd": _LAST_USAGE.get("cost_usd")})
            LAST["tried"].append((model, dt, "ok"))
            _penalise(model, False, ok=True)
            return out, "ok"
        status = "skipped:error"                    # wrong length = a failure
        _penalise(model, False)
        LAST["tried"].append((model, dt, "implausible"))
    LAST["status"] = status
    return raw, status


def _user_turn(raw: str, instructed: bool) -> str:
    return ("<transcript>\n" + raw + "\n</transcript>\n\n" +
            ("Return only the final text, with the instructions applied and removed."
             if instructed else "Return only the edited transcript."))


def _clean_direct(provider: str, raw: str, sys_prompt: str, budget: float,
                  instructed: bool, started: float) -> tuple:
    """One call to the person's own Claude / ChatGPT key or app (providers.py)."""
    key = ""
    if provider in providers.API_PROVIDERS:
        key = _key(providers.KEY_NAMES[provider])
        if not key:
            LAST["status"] = "skipped:error"
            return raw, "skipped:error"
    left = max(budget, providers.budget_for(provider) or 0) - (time.monotonic() - started)
    max_tokens = max(64, int(len(raw.split()) * (6 if instructed else 3)) + 32)
    t0 = time.monotonic()
    try:
        out, usage = _with_deadline(
            provider,
            lambda: providers.call(provider, key, sys_prompt, _user_turn(raw, instructed),
                                   max_tokens, left),
            left + 0.05)
    except Exception as exc:
        name = type(exc).__name__.lower()
        status = "skipped:timeout" if ("timeout" in name or "abandoned" in name) else "skipped:error"
        LAST["tried"].append((provider, round(time.monotonic() - t0, 3), status))
        LAST["status"] = status
        return raw, status
    out = strip_reasoning(out)
    LAST["tried"].append((provider, round(time.monotonic() - t0, 3), "ok"))
    if not _plausible(raw, out, instructed):
        LAST["status"] = "skipped:error"
        return raw, "skipped:error"
    LAST.update({"model": provider, "status": "ok", **usage})
    return out, "ok"


PROBE_CLIP = ("so I need you to, I need you to check the booking page and tell me "
              "why the twenty three slots are not showing when I open it")


def probe_free(candidates: list, per_call: float = 2.0) -> list:
    """Time a real one-line cleanup on each candidate, best-first.

    The list order alone is a poor predictor: the shared free pool answers 429
    "temporarily rate-limited upstream" for whole models at a time, and the
    ones that do answer differ by a second.  One probe at startup is worth more
    than any heuristic over the model metadata.
    """
    key = _key("OPENROUTER_API_KEY")
    if not key:
        return candidates
    hdr = {"HTTP-Referer": "https://github.com/michalszyszka2-cmyk/dictate", "X-Title": "dictate"}
    sys_prompt = system_prompt("claude", "Claude, Supabase, Brave")
    scored = []
    for mid in candidates:
        t0 = time.monotonic()
        try:
            out = _with_deadline(
                "openrouter",
                lambda m=mid: _chat(OPENROUTER_BASE, key, m, sys_prompt,
                                    PROBE_CLIP, per_call, hdr),
                per_call + 0.05)
            dt = time.monotonic() - t0
            ok = _plausible(PROBE_CLIP, strip_reasoning(out))
        except Exception:
            dt, ok = time.monotonic() - t0, False
        scored.append((0 if ok else 1, round(dt, 3), mid))
        if not ok:
            _penalise(mid, dt >= per_call)
    scored.sort()
    return [s[2] for s in scored]


def warmup(timeout: float = 6.0, probe: bool = True, probe_n: int = 3) -> dict:
    """Resolve the model picks and open the TLS connections before the first
    clip, so no user-facing call pays for them.  Returns the ids chosen.

    With ``probe`` the top ``probe_n`` free ids are raced once and the cache is
    re-ordered by which of them actually answered, and how fast.  Re-probed at
    most once every 24 h.
    """
    info = {"openrouter": "", "nvidia": ""}
    try:
        if cleanup_config().get("provider", "openrouter") != "openrouter":
            return info
    except Exception:
        pass
    try:
        info["openrouter"] = pick_free_model(timeout=timeout)
    except Exception:
        pass
    cached = _MODEL_MEM or _read_cache()
    fresh = time.time() - cached.get("probed_at", 0) < MODEL_CACHE_TTL
    if probe and not os.environ.get("DICTATE_MODEL") and not fresh:
        shortlist = list(cached.get("shortlist") or [])
        if shortlist:
            head = probe_free(shortlist[:max(1, probe_n)])
            new = head + [m for m in shortlist if m not in head]
            data = dict(cached)
            data.update({"id": new[0], "shortlist": new, "probed_at": time.time(),
                         "picked_at": cached.get("picked_at", time.time())})
            _write_cache(data)
            info["openrouter"] = new[0]
    try:
        info["nvidia"] = pick_nim_model(timeout=timeout)
    except Exception:
        pass
    return info


if __name__ == "__main__":                                   # pragma: no cover
    import sys
    text = " ".join(sys.argv[1:]) or "can you just, can you make it adjustable please"
    print(warmup())
    t0 = time.monotonic()
    print(clean(text, "claude", "Claude, Supabase, Alice, Firecrawl, Vercel"))
    print("%.2f s" % (time.monotonic() - t0))
