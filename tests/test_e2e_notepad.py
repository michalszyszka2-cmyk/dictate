"""Stream E proof: the full pipeline, end to end, into a real Notepad window.

Departure from the brief (agreed with the manager, logged in the stream E
report): the brief's stream E proof asks for Notepad, Claude desktop, Brave
and VS Code, each with its own screenshot. An agent cannot speak, and
the owner is working on this desktop, so only Notepad -- a target this test can
drive unattended without touching apps he might be using -- is automated
here. Claude / Brave / VS Code are left for the owner to try by voice; every
clip's e2eLatency is in history.sqlite afterwards either way.

Flow: opens a small Notepad window (bottom-right corner, closed without
saving in a finally -- copied from tests/test_delivery.py's pattern), loads
bench/sample.wav as float32 16 kHz, warms the engine and cleanup provider
chain (mirrors what app.py does once at start-up, so this run's timings
reflect steady-state use rather than first-call model-load cost), then calls
pipeline.run() for real -- Parakeet, vocab, policy, cleanup, deliver.paste,
history.log -- with Notepad focused. Reads the result back from the
RichEditD2DPT control, asserts it contains "campaign" and "website" (from
the sample clip's content), prints the per-stage timings, and screenshots
the window to proof/e2e-1-notepad.png.

Bar: e2eLatency <= 1.5s. The free OpenRouter cleanup pool answers well under
half the time today (stream D's documented gate failure -- proof/eval_*.txt);
when it doesn't, pipeline.py hands cleanup.clean() a shrinking budget so the
fallback to raw still lands under the target, and that fallback is expected,
not a bug in this test.
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
import wave
from ctypes import wintypes
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dictate import cleanup, deliver, engine, pipeline  # noqa: E402

# Without this, GetWindowRect/MoveWindow return DPI-virtualized coordinates
# that don't line up with ImageGrab's physical-pixel capture on a scaled
# display, so the screenshot crop silently grabs whatever real screen region
# those virtual coordinates happen to land on instead of the Notepad window
# (caught while writing this test -- the first screenshot was of an
# unrelated window). Must run before any window/geometry call below.
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

user32 = ctypes.windll.user32

WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_SETTEXT = 0x000C
SW_RESTORE = 9

EnumWindows = user32.EnumWindows
EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
GetClassName = user32.GetClassNameW
IsWindowVisible = user32.IsWindowVisible
FindWindowExW = user32.FindWindowExW
SendMessageW = user32.SendMessageW
SetForegroundWindow = user32.SetForegroundWindow
ShowWindow = user32.ShowWindow
MoveWindow = user32.MoveWindow
GetWindowRect = user32.GetWindowRect

SM_CXSCREEN = 0
SM_CYSCREEN = 1
WINDOW_W = 600
WINDOW_H = 300
SCREEN_MARGIN = 12
TASKBAR_ALLOWANCE = 56

SAMPLE_WAV = ROOT / "bench" / "sample.wav"
PROOF_PNG = ROOT / "proof" / "e2e-1-notepad.png"
try:
    from dictate import config as _cfg
    E2E_BAR_S = float((_cfg.cleanup_config() or {}).get("e2e_target_s", 1.5) or 1.5)   # 1.4 raw path; ~2.2 with a paid cleanup model
except Exception:
    E2E_BAR_S = 1.5


# -- Notepad plumbing (copied from tests/test_delivery.py's pattern) --------
def _shrink_to_corner(hwnd):
    screen_w = user32.GetSystemMetrics(SM_CXSCREEN)
    screen_h = user32.GetSystemMetrics(SM_CYSCREEN)
    x = max(0, screen_w - WINDOW_W - SCREEN_MARGIN)
    y = max(0, screen_h - WINDOW_H - SCREEN_MARGIN - TASKBAR_ALLOWANCE)
    MoveWindow(hwnd, x, y, WINDOW_W, WINDOW_H, True)


def _list_notepad_hwnds():
    found = []

    def cb(hwnd, lParam):
        if IsWindowVisible(hwnd):
            cls = ctypes.create_unicode_buffer(256)
            GetClassName(hwnd, cls, 256)
            if cls.value == "Notepad":
                found.append(hwnd)
        return True

    EnumWindows(EnumWindowsProc(cb), 0)
    return found


def _find_edit_child(main_hwnd):
    textbox = FindWindowExW(main_hwnd, None, "NotepadTextBox", None)
    if textbox:
        richedit = FindWindowExW(textbox, None, "RichEditD2DPT", None)
        if richedit:
            return "RichEditD2DPT", richedit
    edit = FindWindowExW(main_hwnd, None, "Edit", None)
    if edit:
        return "Edit", edit
    return None, None


def _wm_get_text(hwnd) -> str:
    length = SendMessageW(hwnd, WM_GETTEXTLENGTH, 0, 0)
    buf = ctypes.create_unicode_buffer(length + 1)
    SendMessageW(hwnd, WM_GETTEXT, length + 1, buf)
    return buf.value


def _wm_set_text(hwnd, text: str):
    SendMessageW(hwnd, WM_SETTEXT, 0, text)


def _click(hwnd):
    rect = wintypes.RECT()
    GetWindowRect(hwnd, ctypes.byref(rect))
    cx = (rect.left + rect.right) // 2
    cy = (rect.top + rect.bottom) // 2
    user32.SetCursorPos(cx, cy)
    user32.mouse_event(0x0002, 0, 0, 0, 0)
    user32.mouse_event(0x0004, 0, 0, 0, 0)
    time.sleep(0.15)


def _ensure_foreground(main_hwnd, edit_hwnd, attempts: int = 10) -> bool:
    """See test_delivery.py: a background widget on this dev machine
    periodically steals the foreground on its own timer. Retry click +
    verify instead of hoping SetForegroundWindow alone worked."""
    for _ in range(attempts):
        SetForegroundWindow(main_hwnd)
        _click(edit_hwnd)
        if user32.GetForegroundWindow() == main_hwnd:
            return True
        time.sleep(0.1)
    return False


def open_new_notepad_tab(timeout_s: float = 5.0):
    before = set(_list_notepad_hwnds())
    subprocess.Popen(["notepad.exe"])
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(0.1)
        for hwnd in _list_notepad_hwnds():
            if hwnd in before:
                continue
            kind, edit_hwnd = _find_edit_child(hwnd)
            if edit_hwnd:
                return hwnd, kind, edit_hwnd
    raise TimeoutError("Notepad window with a usable text control did not appear in time")


def close_tab(main_hwnd, edit_hwnd):
    try:
        _wm_set_text(edit_hwnd, "")
    except Exception:
        pass
    try:
        SetForegroundWindow(main_hwnd)
        time.sleep(0.1)
        user32.keybd_event(0x11, 0, 0, 0)
        user32.keybd_event(0x57, 0, 0, 0)
        user32.keybd_event(0x57, 0, 2, 0)
        user32.keybd_event(0x11, 0, 2, 0)
        time.sleep(0.3)
    except Exception as e:
        print(f"[WARN] close_tab: could not close cleanly ({e})")


def _screenshot(main_hwnd, out_path: Path):
    from PIL import ImageGrab

    rect = wintypes.RECT()
    GetWindowRect(main_hwnd, ctypes.byref(rect))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    img = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom))
    img.save(str(out_path))


def _load_sample_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        sr = w.getframerate()
        raw = w.readframes(w.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    if sr != 16_000:
        raise ValueError(f"sample.wav is {sr} Hz, expected 16000 Hz mono")
    return audio


def main() -> int:
    print(f"Loading {SAMPLE_WAV}")
    audio = _load_sample_wav(SAMPLE_WAV)
    print(f"Loaded {len(audio) / 16000:.2f}s of audio")

    print("Warming Parakeet engine + cleanup provider chain (mirrors app.py start-up)...")
    engine.get_engine()
    try:
        cleanup.warmup()
    except Exception as exc:
        print(f"[WARN] cleanup.warmup() failed, continuing anyway: {exc}")

    main_hwnd, kind, edit_hwnd = open_new_notepad_tab()
    print(f"Opened Notepad tab, text control kind={kind}")
    ShowWindow(main_hwnd, SW_RESTORE)
    _shrink_to_corner(main_hwnd)
    time.sleep(0.2)

    ok = False
    try:
        got_focus = _ensure_foreground(main_hwnd, edit_hwnd)
        if not got_focus:
            print("[WARN] could not confirm Notepad held the foreground before dictating")

        title = "Untitled - Notepad"
        t_release = time.time()
        result = pipeline.run(audio, "notepad.exe", title, t_release)
        time.sleep(0.3)  # let the async RichEdit control catch up, as test_delivery.py does

        readback = _wm_get_text(edit_hwnd)

        print()
        print("Pipeline result:")
        print(f"  asrText       : {result['asrText']!r}")
        print(f"  formattedText : {result['formattedText']!r}")
        print(f"  mode          : {result['mode']}")
        print(f"  cleanup status: {result['cleanup']}")
        print(f"  asr_ms        : {result['asr_ms']:.1f}")
        print(f"  vocab_ms      : {result['vocab_ms']:.1f}")
        print(f"  cleanup_ms    : {result['cleanup_ms']:.1f}")
        print(f"  paste_ms      : {result['paste_ms']:.1f}")
        print(f"  e2eLatency    : {result['e2eLatency']:.3f} s (bar: <= {E2E_BAR_S}s)")
        print()
        print(f"Notepad readback: {readback!r}")

        low = readback.lower()
        contains_campaign = "campaign" in low
        contains_website = "website" in low
        latency_ok = result["e2eLatency"] <= E2E_BAR_S

        print(f"  contains 'campaign' : {contains_campaign}")
        print(f"  contains 'website'  : {contains_website}")
        print(f"  latency <= {E2E_BAR_S}s     : {latency_ok}")

        _screenshot(main_hwnd, PROOF_PNG)
        print(f"Screenshot saved: {PROOF_PNG}")

        ok = contains_campaign and contains_website and latency_ok
    finally:
        close_tab(main_hwnd, edit_hwnd)

    print()
    print("OVERALL:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
