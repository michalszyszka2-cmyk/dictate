"""First-run download of the Parakeet speech model.

The int4 build (hmepas/parakeet-tdt-0.6b-v3-int4 on Hugging Face, CC-BY-4.0,
derived from NVIDIA's parakeet-tdt-0.6b-v3) is ~720 MB in four files. Each
file is fetched into ``<name>.part`` with HTTP Range resume, checked against
the size and sha256 pinned below, then renamed into place -- so an
interrupted download picks up where it stopped, and a corrupt one is never
loaded.

Run by hand: ``python -m dictate.models`` (prints progress).
"""
from __future__ import annotations

import hashlib
import os
import time
from pathlib import Path
from typing import Callable, Optional

import requests

from dictate import paths

REPO = "hmepas/parakeet-tdt-0.6b-v3-int4"
REVISION = "main"
TARGET = paths.MODELS_DIR / "parakeet-tdt-0.6b-v3-int4"

#: (file, size in bytes, sha256) -- checked 5 Oct 2026 against the copy in use
FILES = (
    ("encoder.int4.onnx", 646_109_944, "4800d725f71e94b5e2447cd9056ad6b890b56154764e688004b5f8be40aca00e"),
    ("decoder.onnx", 47_233_743, "d593cdb0e571f5a457ec2219af9968cbf6b0e8198e8f7839b40a8754593bf68c"),
    ("joiner.onnx", 25_286_330, "b9b0bcf88ac571902e69a6536223ed2d94885e981b85045410f1403d53121a63"),
    ("tokens.txt", 93_939, "d58544679ea4bc6ac563d1f545eb7d474bd6cfa467f0a6e2c1dc1c7d37e3c35d"),
)
TOTAL_BYTES = sum(size for _, size, _ in FILES)
CHUNK = 1 << 20
RETRIES = 4

Progress = Callable[[int, int], None]  # (bytes done, bytes total)


class DownloadError(RuntimeError):
    pass


def missing() -> list[str]:
    """Files absent or the wrong size. Size only: hashing 720 MB on every start costs ~2 s."""
    return [name for name, size, _ in FILES
            if not (TARGET / name).is_file() or (TARGET / name).stat().st_size != size]


def _url(name: str) -> str:
    return f"https://huggingface.co/{REPO}/resolve/{REVISION}/{name}"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            h.update(block)
    return h.hexdigest()


def _fetch(name: str, size: int, sha: str, done_before: int, progress: Optional[Progress]) -> None:
    part = TARGET / (name + ".part")
    for attempt in range(1, RETRIES + 1):
        have = part.stat().st_size if part.exists() else 0
        if have > size:  # junk from an older revision: start over
            part.unlink()
            have = 0
        try:
            if have < size:
                headers = {"Range": f"bytes={have}-"} if have else {}
                with requests.get(_url(name), headers=headers, stream=True, timeout=30) as r:
                    if have and r.status_code != 206:  # server ignored the range
                        have = 0
                    r.raise_for_status()
                    with open(part, "ab" if have else "wb") as fh:
                        for block in r.iter_content(CHUNK):
                            fh.write(block)
                            have += len(block)
                            if progress:
                                progress(done_before + have, TOTAL_BYTES)
            if part.stat().st_size != size:
                raise DownloadError(f"{name}: got {part.stat().st_size} bytes, expected {size}")
            if _sha256(part) != sha:
                part.unlink()
                raise DownloadError(f"{name}: checksum mismatch, discarded")
            os.replace(part, TARGET / name)
            return
        except (requests.RequestException, DownloadError, OSError) as e:
            if attempt == RETRIES:
                if isinstance(e, DownloadError):
                    raise
                raise DownloadError(f"{name}: {e}") from e
            time.sleep(2 * attempt)


def ensure(progress: Optional[Progress] = None) -> bool:
    """Download whatever is missing. Returns True if anything was fetched."""
    todo = set(missing())
    if not todo:
        return False
    TARGET.mkdir(parents=True, exist_ok=True)
    done = 0
    for name, size, sha in FILES:
        if name in todo:
            _fetch(name, size, sha, done, progress)
        done += size
        if progress:
            progress(done, TOTAL_BYTES)
    return True


if __name__ == "__main__":
    last = [0.0]

    def _show(done: int, total: int) -> None:
        if time.monotonic() - last[0] > 0.5 or done == total:
            last[0] = time.monotonic()
            print(f"\r{done / 1e6:7.1f} / {total / 1e6:.1f} MB", end="", flush=True)

    print(f"model folder: {TARGET}")
    print("downloaded." if ensure(_show) else "already present, nothing to do.")
