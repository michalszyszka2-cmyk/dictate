"""Cloud Whisper speech-to-text -- the A/B alternative to Parakeet.

Picked from the tray (Engine -> Whisper + GPT mini) or ``engine: whisper`` in
config.yaml. Same contract as ``engine.transcribe``: float32 16 kHz mono in,
plain text out. Raises on any failure; the pipeline then falls back to
Parakeet for that clip, so a dead key or no network never loses a dictation.

Provider is picked by which key this PC has (secrets.env or the environment):

  OPENAI_API_KEY  -> api.openai.com,  whisper-1               ($0.006 / min)
  GROQ_API_KEY    -> api.groq.com,    whisper-large-v3-turbo  ($0.04 / hour, free tier)

OpenAI wins when both are set. ``whisper.model`` in config.yaml overrides the
model id.
"""
from __future__ import annotations

import io
import os
import time
import wave

import numpy as np
import requests

from dictate import paths

PROVIDERS = {
    "OPENAI_API_KEY": ("openai", "https://api.openai.com/v1", "whisper-1", 0.006 / 60),
    "GROQ_API_KEY": ("groq", "https://api.groq.com/openai/v1", "whisper-large-v3-turbo", 0.04 / 3600),
}
TIMEOUT_S = 10.0

_SESSION = requests.Session()   # keep-alive: a fresh TLS handshake costs ~0.3 s
#: what the last call used: provider, model, seconds, est_cost_usd
LAST: dict = {}


def _key(name: str) -> str:
    return os.environ.get(name) or paths.read_secrets().get(name, "")


def provider() -> tuple | None:
    """(name, base_url, model, usd_per_second, key) for the first key found, else None."""
    for env, (name, base, model, rate) in PROVIDERS.items():
        k = _key(env)
        if k:
            return name, base, model, rate, k
    return None


def _wav_bytes(audio: np.ndarray, sample_rate: int) -> bytes:
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm.tobytes())
    return buf.getvalue()


def transcribe(audio: np.ndarray, sample_rate: int = 16_000, language: str | None = None,
               model: str | None = None) -> str:
    """Raw Whisper text for one clip. ``language`` "en"/"pl" pins it; None lets Whisper detect."""
    p = provider()
    if p is None:
        raise RuntimeError("no OPENAI_API_KEY or GROQ_API_KEY on this PC")
    name, base, default_model, rate, key = p
    model = model or default_model
    data = {"model": model, "response_format": "json", "temperature": "0"}
    if language in ("en", "pl"):
        data["language"] = language
    t0 = time.monotonic()
    r = _SESSION.post(base + "/audio/transcriptions",
                      headers={"Authorization": "Bearer " + key},
                      files={"file": ("clip.wav", _wav_bytes(audio, sample_rate), "audio/wav")},
                      data=data, timeout=TIMEOUT_S)
    r.raise_for_status()
    seconds = len(audio) / sample_rate
    LAST.clear()
    LAST.update({"provider": name, "model": model, "seconds": round(time.monotonic() - t0, 3),
                 # OpenAI bills whole seconds; Groq bills a 10 s minimum per request
                 "est_cost_usd": rate * (max(10.0, seconds) if name == "groq" else seconds)})
    return (r.json().get("text") or "").strip()


if __name__ == "__main__":                                   # pragma: no cover
    import sys
    from pathlib import Path

    clip = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "bench" / "sample.wav"
    with wave.open(str(clip)) as w:
        sr = w.getframerate()
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    print(transcribe(a, sr))
    print(LAST)
