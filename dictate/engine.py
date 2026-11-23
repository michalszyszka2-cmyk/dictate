"""Parakeet TDT 0.6B v3 int8 speech-to-text engine (sherpa-onnx, CPU).

Loads the recognizer once at start-up and exposes a single
``transcribe(audio_float32_16k) -> str`` call. Same model + settings as
stream A's ``tests/bench_engine.py`` (0.42-0.51 s warm on the 9.12 s sample
clip): ``num_threads=8``, ``decoding_method="greedy_search"``,
``model_type="nemo_transducer"``.

Hotwords finding (21 Sept 2026): stream C's ``vocab.write_hotwords_file``
works and sherpa-onnx's ``from_transducer`` does accept ``hotwords_file`` /
``hotwords_score`` as parameters -- but passing a hotwords file together with
``decoding_method="greedy_search"`` raises immediately:

    ValueError: Please use --decoding-method=modified_beam_search when using
    --hotwords-file. Currently given: greedy_search

Switching to ``modified_beam_search`` to use it would change the decoding
method this stream is pinned to (and was not re-benchmarked for latency), so
per the brief ("if wiring it degrades the sample transcript or errors, leave
it off and say so") hotwords are left OFF here. ``HOTWORDS_WIRED = False`` and
the finding above are surfaced by ``--doctor`` output indirectly through this
module's docstring; see the stream E report for the full note.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path

import numpy as np
import sherpa_onnx

from dictate import paths

_MODELS = paths.MODELS_DIR
# int4 weight-only build (hmepas/parakeet-tdt-0.6b-v3-int4) is preferred: measured 22 Sept on
# real clips it hears "WordPress"/"bar"/"5%" where int8 heard "Waterplace"/"ball"/"five
# players", at the same speed. Falls back to the stock int8 export if int4 is absent.
def resolve_model_dir() -> Path:
    """Re-checked on every call: the first-run download may have just landed int4."""
    int4 = _MODELS / "parakeet-tdt-0.6b-v3-int4"
    return int4 if (int4 / "encoder.int4.onnx").exists() else (_MODELS / "parakeet-tdt-0.6b-v3-int8")


MODEL_DIR = resolve_model_dir()
SAMPLE_RATE = 16_000

# See module docstring: hotwords_file requires modified_beam_search, which
# this engine does not use (greedy_search is the brief's pinned setting).
HOTWORDS_WIRED = False


class EngineError(RuntimeError):
    """Raised when the Parakeet model files are missing or fail to load."""


def _pick(model_dir: Path, *names: str) -> Path:
    """First of *names that exists in model_dir (else the first name, so the error names it)."""
    for n in names:
        if (model_dir / n).exists():
            return model_dir / n
    return model_dir / names[0]


def _model_paths(model_dir: Path) -> dict:
    return {
        "encoder": str(_pick(model_dir, "encoder.int4.onnx", "encoder.int8.onnx", "encoder.onnx")),
        "decoder": str(_pick(model_dir, "decoder.int8.onnx", "decoder.onnx")),
        "joiner": str(_pick(model_dir, "joiner.int8.onnx", "joiner.onnx")),
        "tokens": str(model_dir / "tokens.txt"),
    }


def model_files_present(model_dir: Path | None = None) -> bool:
    files = _model_paths(model_dir or resolve_model_dir())
    return all(os.path.isfile(f) for f in files.values())


class Engine:
    """Loads the Parakeet recognizer once; ``transcribe()`` is cheap after that."""

    def __init__(self, model_dir: Path | None = None, num_threads: int = 8):
        self.model_dir = model_dir or resolve_model_dir()
        self.num_threads = num_threads
        self._recognizer = None
        self._lock = threading.Lock()
        self.load_seconds = 0.0

    def load(self):
        if self._recognizer is not None:
            return self
        if not model_files_present(self.model_dir):
            raise EngineError(f"Parakeet model files not found under {self.model_dir}")
        paths = _model_paths(self.model_dir)
        t0 = time.time()
        self._recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
            encoder=paths["encoder"],
            decoder=paths["decoder"],
            joiner=paths["joiner"],
            tokens=paths["tokens"],
            model_type="nemo_transducer",
            num_threads=self.num_threads,
            decoding_method="greedy_search",
        )
        self.load_seconds = time.time() - t0
        return self

    @property
    def loaded(self) -> bool:
        return self._recognizer is not None

    def transcribe(self, audio_float32_16k: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str:
        """audio_float32_16k: 1-D float32 array, 16 kHz mono, range [-1, 1].
        Returns the raw ASR text ("" for empty/silent input)."""
        if audio_float32_16k is None or len(audio_float32_16k) == 0:
            return ""
        if self._recognizer is None:
            self.load()
        # sherpa-onnx OfflineRecognizer streams are not documented as
        # thread-safe for concurrent decode_stream calls on one recognizer;
        # the pipeline only ever has one clip in flight at a time (recording
        # is exclusive), but guard it anyway since transcribe() could be
        # called from a worker thread while the main thread still touches ui.
        with self._lock:
            stream = self._recognizer.create_stream()
            stream.accept_waveform(sample_rate, audio_float32_16k.astype(np.float32))
            self._recognizer.decode_stream(stream)
            return (stream.result.text or "").strip()


_ENGINE: "Engine | None" = None
_ENGINE_LOCK = threading.Lock()


def get_engine() -> Engine:
    """Process-wide singleton, loaded on first use."""
    global _ENGINE
    if _ENGINE is None:
        with _ENGINE_LOCK:
            if _ENGINE is None:
                _ENGINE = Engine().load()
    return _ENGINE


def transcribe(audio_float32_16k: np.ndarray, sample_rate: int = SAMPLE_RATE) -> str:
    return get_engine().transcribe(audio_float32_16k, sample_rate)


if __name__ == "__main__":
    import wave

    clip = Path(__file__).resolve().parent.parent / "bench" / "sample.wav"
    eng = Engine().load()
    print(f"load {eng.load_seconds:.2f}s")
    with wave.open(str(clip)) as w:
        sr = w.getframerate()
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    t0 = time.time()
    text = eng.transcribe(audio, sr)
    print(f"decode {time.time() - t0:.2f}s | {text}")
