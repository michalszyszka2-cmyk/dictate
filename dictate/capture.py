"""Microphone capture with a 500 ms pre-roll ring buffer.

Always-running input stream at 16 kHz mono float32. The ring buffer keeps the
last 500 ms so that Recorder.start() can prepend audio from just before the
key-press, avoiding a clipped first word.

WASAPI is used when sounddevice exposes it (Windows); otherwise the default
host API is used and it still works, just without the WASAPI-specific
low-latency path.
"""

from __future__ import annotations

import threading
import time
from collections import deque

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16_000
CHANNELS = 1
DTYPE = "float32"
PRE_ROLL_SECONDS = 0.5
BLOCK_SIZE = 1600  # 100 ms at 16 kHz, keeps callback overhead low
LEVEL_SLICE = 320  # 20 ms -- one frame of the UI's spectrum meter
LEVEL_SLICES_KEPT = 50


def _find_wasapi_device():
    """Return (device_index, extra_settings) for a WASAPI input device, or
    (None, None) if WASAPI isn't available on this system (e.g. not Windows).
    """
    try:
        hostapis = sd.query_hostapis()
    except Exception:
        return None, None

    wasapi_index = None
    for i, api in enumerate(hostapis):
        if "wasapi" in api["name"].lower():
            wasapi_index = i
            break
    if wasapi_index is None:
        return None, None

    hostapi = hostapis[wasapi_index]
    default_input = hostapi.get("default_input_device", -1)
    device_index = default_input if default_input is not None and default_input >= 0 else None
    if device_index is None:
        # fall back to first device on this hostapi with input channels
        for idx, dev in enumerate(sd.query_devices()):
            if dev["hostapi"] == wasapi_index and dev["max_input_channels"] > 0:
                device_index = idx
                break

    extra_settings = None
    try:
        extra_settings = sd.WasapiSettings()
    except Exception:
        extra_settings = None

    return device_index, extra_settings


class Recorder:
    """Always-on microphone capture with a rolling pre-roll buffer.

    Usage:
        rec = Recorder()
        rec.open()          # starts the always-running background stream
        rec.start()         # begin an utterance (includes pre-roll)
        ...
        audio = rec.stop()  # -> np.ndarray float32 16kHz mono
        rec.close()
    """

    def __init__(self, sample_rate: int = SAMPLE_RATE, pre_roll_seconds: float = PRE_ROLL_SECONDS):
        self.sample_rate = sample_rate
        self.pre_roll_samples = int(sample_rate * pre_roll_seconds)

        self._lock = threading.Lock()
        self._ring = np.zeros(self.pre_roll_samples, dtype=np.float32)
        self._ring_pos = 0
        self._ring_filled = False

        self._recording = False
        self._capture_chunks: list[np.ndarray] = []

        self._level = 0.0
        # 20 ms audio slices for the spectrum meter; drained by the UI poll thread
        self._slices: deque[np.ndarray] = deque(maxlen=LEVEL_SLICES_KEPT)
        self._stream: sd.InputStream | None = None

    # -- always-running stream -------------------------------------------------
    def open(self):
        if self._stream is not None:
            return

        device_index, extra_settings = _find_wasapi_device()

        kwargs = dict(
            samplerate=self.sample_rate,
            channels=CHANNELS,
            dtype=DTYPE,
            blocksize=BLOCK_SIZE,
            callback=self._callback,
        )
        if device_index is not None:
            kwargs["device"] = device_index
        if extra_settings is not None:
            kwargs["extra_settings"] = extra_settings

        try:
            self._stream = sd.InputStream(**kwargs)
            self._stream.start()
        except Exception:
            # Retry without WASAPI-specific settings/device — fall back to
            # whatever default input device sounddevice picks.
            kwargs.pop("device", None)
            kwargs.pop("extra_settings", None)
            self._stream = sd.InputStream(**kwargs)
            self._stream.start()

    def close(self):
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def _callback(self, indata, frames, time_info, status):
        mono = indata[:, 0].copy() if indata.ndim > 1 else indata.copy()

        # RMS level for the UI meter
        if len(mono):
            self._level = float(np.sqrt(np.mean(np.square(mono))))
            n = len(mono) // LEVEL_SLICE * LEVEL_SLICE
            self._slices.extend(mono[:n].reshape(-1, LEVEL_SLICE))

        with self._lock:
            if self._recording:
                self._capture_chunks.append(mono)
            else:
                n = len(mono)
                if n >= self.pre_roll_samples:
                    self._ring[:] = mono[-self.pre_roll_samples:]
                    self._ring_pos = 0
                    self._ring_filled = True
                else:
                    end = self._ring_pos + n
                    if end <= self.pre_roll_samples:
                        self._ring[self._ring_pos:end] = mono
                    else:
                        first = self.pre_roll_samples - self._ring_pos
                        self._ring[self._ring_pos:] = mono[:first]
                        self._ring[: n - first] = mono[first:]
                        self._ring_filled = True
                    self._ring_pos = end % self.pre_roll_samples

    def _pre_roll_copy(self) -> np.ndarray:
        if not self._ring_filled:
            # not enough history yet — return what we have from the start
            return self._ring[: self._ring_pos].copy()
        return np.concatenate([self._ring[self._ring_pos:], self._ring[: self._ring_pos]])

    # -- recording lifecycle ----------------------------------------------------
    def start(self):
        """Begin capturing an utterance, seeded with the pre-roll buffer."""
        if self._stream is None:
            self.open()
        with self._lock:
            pre_roll = self._pre_roll_copy()
            self._capture_chunks = [pre_roll] if len(pre_roll) else []
            self._recording = True

    def stop(self) -> np.ndarray:
        """Stop capturing and return the utterance as float32 16kHz mono."""
        with self._lock:
            self._recording = False
            chunks = self._capture_chunks
            self._capture_chunks = []

        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(chunks).astype(np.float32)

    def cancel(self):
        """Discard the current in-progress capture without returning audio."""
        with self._lock:
            self._recording = False
            self._capture_chunks = []

    @property
    def level(self) -> float:
        """Current RMS level (0..~1) for the UI meter."""
        return self._level

    def drain_slices(self) -> list[np.ndarray]:
        """Every 20 ms audio slice since the last call, oldest first."""
        out = []
        while self._slices:
            out.append(self._slices.popleft())
        return out


if __name__ == "__main__":
    rec = Recorder()
    rec.open()
    print("Recording 2s in 1s...")
    time.sleep(1)
    rec.start()
    time.sleep(2)
    audio = rec.stop()
    rec.close()
    print(f"Captured {len(audio)} samples ({len(audio) / SAMPLE_RATE:.2f}s)")
