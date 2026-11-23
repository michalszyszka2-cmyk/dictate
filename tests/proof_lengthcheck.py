"""End-to-end proof of ``dictate.lengthcheck`` without a microphone: every
passage is spoken by an edge-tts British voice, decoded to 16 kHz, and fed to
the same ``process_take`` the read-aloud test uses. Writes to
``proof/lengthcheck-tts.sqlite`` (never the real results file) and prints the
report. A synthetic voice is cleaner than a laptop mic, so this proves the
plumbing and the scoring, not how many mistakes your own voice gets.

    python tests/proof_lengthcheck.py [--no-cleanup]
"""
import asyncio
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dictate import engine, lengthcheck  # noqa: E402

VOICE = "en-GB-RyanNeural"
DB = Path(__file__).resolve().parents[1] / "proof" / "lengthcheck-tts.sqlite"


async def _speak(text: str, path: Path) -> None:
    import edge_tts
    await edge_tts.Communicate(text, VOICE).save(str(path))


def tts_audio(text: str) -> np.ndarray:
    with tempfile.TemporaryDirectory() as d:
        mp3 = Path(d) / "a.mp3"
        asyncio.run(_speak(text, mp3))
        pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(mp3), "-f", "s16le",
                              "-ac", "1", "-ar", str(engine.SAMPLE_RATE), "-"],
                             capture_output=True, check=True).stdout
    return np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0


def main() -> None:
    no_cleanup = "--no-cleanup" in sys.argv
    DB.unlink(missing_ok=True)
    engine.get_engine()
    conn = lengthcheck._connect(DB)
    for pid, ref in lengthcheck.PASSAGES.items():
        audio = tts_audio(ref)
        pasted, status = lengthcheck.process_take(conn, pid, audio, time.time(), no_cleanup)
        e, w = lengthcheck.word_errors(ref, pasted)
        print(f"{pid}: {w} words, {len(audio) / engine.SAMPLE_RATE:.0f} s, {e} wrong, cleanup {status}")
    conn.close()
    print()
    lengthcheck.report(DB)


if __name__ == "__main__":
    main()
