"""Delivering transcribed text into the foreground app.

Two delivery paths:
  - paste(text): clipboard + Ctrl+V (SendInput), then restore the previous
    clipboard contents. Fast, preserves formatting-sensitive apps' own
    paste handling.
  - type_text(text): SendInput with KEYEVENTF_UNICODE, one event per UTF-16
    code unit (so codepoints outside the BMP go out as a surrogate pair).
    Used where paste is blocked (e.g. some terminals / secure fields).

Also foreground_app() -> (process_name, window_title) so the pipeline can
log which app a clip was dictated into and apply per-app policy.

All Windows API access here is via ctypes (SendInput needs the raw INPUT
struct) plus pywin32's win32clipboard for clipboard read/write, which is
the simplest reliable way to get CF_UNICODETEXT in and out.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

import win32clipboard
import win32con

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# ---------------------------------------------------------------------------
# SendInput plumbing
# ---------------------------------------------------------------------------

PUL = ctypes.POINTER(ctypes.c_ulong)

INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

VK_CONTROL = 0x11
VK_V = 0x56


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", PUL),
    ]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", PUL),
    ]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("union", _INPUTunion)]


user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
user32.SendInput.restype = wintypes.UINT

_EXTRA = ctypes.pointer(ctypes.c_ulong(0))


def _send_vk(vk: int, keyup: bool = False):
    ki = KEYBDINPUT(vk, 0, KEYEVENTF_KEYUP if keyup else 0, 0, _EXTRA)
    inp = INPUT(INPUT_KEYBOARD, _INPUTunion(ki=ki))
    user32.SendInput(1, ctypes.pointer(inp), ctypes.sizeof(INPUT))


def _send_ctrl_v():
    _send_vk(VK_CONTROL)
    _send_vk(VK_V)
    _send_vk(VK_V, keyup=True)
    _send_vk(VK_CONTROL, keyup=True)


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------


def _get_clipboard_text():
    """Return the current CF_UNICODETEXT clipboard contents, or None if the
    clipboard is empty, unavailable, or holds a non-text format."""
    try:
        win32clipboard.OpenClipboard()
    except Exception:
        return None
    try:
        if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
            return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT)
        return None
    except Exception:
        return None
    finally:
        try:
            win32clipboard.CloseClipboard()
        except Exception:
            pass


def _set_clipboard_text(text: str):
    win32clipboard.OpenClipboard()
    try:
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32con.CF_UNICODETEXT, text)
    finally:
        win32clipboard.CloseClipboard()


def _clipboard_seq() -> int:
    return user32.GetClipboardSequenceNumber()


def paste(text: str, wait_ms: int = 300) -> None:
    """Set the clipboard to `text`, send Ctrl+V, then restore whatever text
    was on the clipboard before (if anything -- a non-text clipboard is left
    alone, there is nothing text-shaped to restore).

    Per the brief: after sending Ctrl+V, poll GetClipboardSequenceNumber for
    a change (signal the paste has been read) or `wait_ms`, whichever comes
    first, before restoring. Note: on Windows the sequence number only
    increments on a clipboard *write* (SetClipboardData/EmptyClipboard), not
    on a read, so ordinary paste consumers will not trip this early exit --
    in practice this loop almost always runs the full `wait_ms`. Implemented
    as specified anyway since it's a correct, harmless no-op fast path for
    the (rare) target that does write back to the clipboard on paste.
    """
    saved = _get_clipboard_text()

    _set_clipboard_text(text)
    seq_after_set = _clipboard_seq()

    _send_ctrl_v()

    deadline = time.time() + wait_ms / 1000.0
    while time.time() < deadline:
        if _clipboard_seq() != seq_after_set:
            break
        time.sleep(0.01)

    if saved is not None:
        _set_clipboard_text(saved)


def type_text(text: str) -> None:
    """Type `text` via SendInput + KEYEVENTF_UNICODE, one down+up pair per
    UTF-16 code unit. Handles characters outside the BMP by sending their
    surrogate pair as two consecutive unicode key events, matching what a
    real IME would generate.

    The down and up events for each code unit are submitted as a *single*
    SendInput call carrying a 2-element INPUT array, not two separate
    SendInput calls -- two separate calls at full speed let Windows'
    low-level input processing interleave/coalesce with the next
    character's events before Notepad's (async, Direct2D-rendered)
    RichEditD2DPT control had processed the previous key-up, observed on
    this machine as long runs collapsing into the *last* character
    repeated (e.g. a non-ASCII character typed over and over).
    Batching into one SendInput call per character cut that failure rate a
    lot but did not eliminate it (still ~1 in 3 runs against that same
    control); a small (4 ms) delay after each character on top of the
    batching made it fully reliable across dozens of runs. 4 ms x a typical
    dictated sentence (~120 chars) is under 0.5 s, which is the fallback
    path (paste() is the primary one) so this is an acceptable cost for
    reliability against a control this asynchronous.
    """
    for ch in text:
        code_units = ch.encode("utf-16-le", "surrogatepass")
        for i in range(0, len(code_units), 2):
            cu = code_units[i] | (code_units[i + 1] << 8)
            ki_down = KEYBDINPUT(0, cu, KEYEVENTF_UNICODE, 0, _EXTRA)
            ki_up = KEYBDINPUT(0, cu, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP, 0, _EXTRA)
            pair = (INPUT * 2)()
            pair[0] = INPUT(INPUT_KEYBOARD, _INPUTunion(ki=ki_down))
            pair[1] = INPUT(INPUT_KEYBOARD, _INPUTunion(ki=ki_up))
            user32.SendInput(2, pair, ctypes.sizeof(INPUT))
            time.sleep(0.004)


# ---------------------------------------------------------------------------
# Foreground app
# ---------------------------------------------------------------------------

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def foreground_app() -> tuple[str, str]:
    """Return (process_name, window_title) for the current foreground
    window, e.g. ("Code.exe", "deliver.py - dictate - Visual Studio Code")."""
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ("", "")

    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    title = buf.value

    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))

    process_name = ""
    h_process = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if h_process:
        try:
            size = wintypes.DWORD(260)
            name_buf = ctypes.create_unicode_buffer(260)
            ok = kernel32.QueryFullProcessImageNameW(h_process, 0, name_buf, ctypes.byref(size))
            if ok:
                process_name = name_buf.value.rsplit("\\", 1)[-1]
        finally:
            kernel32.CloseHandle(h_process)

    return (process_name, title)


if __name__ == "__main__":
    print("Foreground:", foreground_app())
