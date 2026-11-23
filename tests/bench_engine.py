"""Stream A proof: Parakeet TDT 0.6B v3 int8 latency on the sample clip (CPU)."""
import os, sys, time, wave
import numpy as np
import sherpa_onnx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from dictate import engine as _engine
MODEL = str(_engine.MODEL_DIR)   # int4 if installed, else int8
CLIP = os.path.join(os.path.dirname(__file__), "..", "bench", "sample.wav")

def load(threads=8):
    f = _engine._model_paths(_engine.MODEL_DIR)
    enc = f["encoder"] if f else f"{MODEL}/encoder.int8.onnx"; dec = f["decoder"] if f else f"{MODEL}/decoder.int8.onnx"; joi = f["joiner"] if f else f"{MODEL}/joiner.int8.onnx"
    return sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=enc, decoder=dec, joiner=joi, tokens=f"{MODEL}/tokens.txt",
        model_type="nemo_transducer", num_threads=threads, decoding_method="greedy_search")

if __name__ == "__main__":
    t = time.time(); rec = load(); print(f"load {time.time()-t:.2f}s")
    w = wave.open(CLIP); sr = w.getframerate()
    a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    worst = 0
    for i in range(3):
        t = time.time(); s = rec.create_stream(); s.accept_waveform(sr, a); rec.decode_stream(s)
        dt = time.time() - t; worst = max(worst, dt); print(f"pass{i} {dt:.2f}s | {s.result.text}")
    sys.exit(0 if worst <= 1.0 else 1)
