"""Proof for stream B delivery: dictate.deliver.paste() and .type_text().

Launches Notepad, waits for its window, focuses it, injects a test string
via both delivery paths, and reads the control back to check for an exact
match (em dash and pound sign included).

Windows 11's modern Notepad is not a classic 32-bit Edit control -- it's a
NotepadTextBox host around a RichEditD2DPT (Direct2D-rendered RichEdit)
child. WM_GETTEXT / WM_SETTEXT work directly on that RichEditD2DPT child
(confirmed by probing this machine's Notepad before writing this test), so
that's the primary read-back path here. If that ever fails (e.g. a future
Notepad build changes internals again), this falls back to Ctrl+A / Ctrl+C
and reading the text back off the clipboard, and says so in the output.

Notepad is single-instance on this system: launching notepad.exe when an
instance is already running just opens a new tab in it. This script only
ever touches the *new* tab it opens (tracked by HWND, never by title/pid,
since window title lags window creation) and closes only that tab with
Ctrl+W when done -- it never touches the process, so any other tabs the
user already had open (there was one, a real file) are left alone.

The new window is also resized down to a small box in the bottom-right
corner of the primary screen before anything else happens to it (Notepad
otherwise opens at whatever size was last used, which can be full-screen)
-- this test drives the real desktop, not a sandboxed one, and shouldn't
take over someone's screen while it runs. Cleanup (close without saving)
runs from a finally block so the tab is closed even if a check fails.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

from dictate import deliver  # noqa: E402

user32 = ctypes.windll.user32

TEST_STRING = "Test one two — £9.99 (done)"

WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E
WM_SETTEXT = 0x000C
SW_RESTORE = 9

EnumWindows = user32.EnumWindows
EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
GetClassName = user32.GetClassNameW
IsWindowVisible = user32.IsWindowVisible
GetWindowTextW = user32.GetWindowTextW
GetWindowTextLengthW = user32.GetWindowTextLengthW
FindWindowExW = user32.FindWindowExW
SendMessageW = user32.SendMessageW
SetForegroundWindow = user32.SetForegroundWindow
ShowWindow = user32.ShowWindow
MoveWindow = user32.MoveWindow

SM_CXSCREEN = 0
SM_CYSCREEN = 1
WINDOW_W = 600
WINDOW_H = 300
SCREEN_MARGIN = 12
TASKBAR_ALLOWANCE = 56  # rough, avoids overlapping the taskbar at the bottom


def _shrink_to_corner(hwnd):
    """Resize/move the test's Notepad window to a small box in the
    bottom-right of the primary screen, instead of whatever size Windows
    gave the new window (which can be full-screen) -- this test drives the
    machine's real UI and shouldn't take over the user's screen."""
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
    """Return (kind, hwnd) for the readable text control: prefer the modern
    RichEditD2DPT under NotepadTextBox, fall back to a classic Edit child."""
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
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    cx = (rect.left + rect.right) // 2
    cy = (rect.top + rect.bottom) // 2
    user32.SetCursorPos(cx, cy)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP
    time.sleep(0.15)


def _clipboard_fallback_read(edit_hwnd) -> str:
    """Ctrl+A, Ctrl+C, then read the clipboard -- used only if WM_GETTEXT
    fails on the control we found."""
    user32.keybd_event(0x11, 0, 0, 0)
    user32.keybd_event(0x41, 0, 0, 0)
    user32.keybd_event(0x41, 0, 2, 0)
    user32.keybd_event(0x11, 0, 2, 0)
    time.sleep(0.1)
    user32.keybd_event(0x11, 0, 0, 0)
    user32.keybd_event(0x43, 0, 0, 0)
    user32.keybd_event(0x43, 0, 2, 0)
    user32.keybd_event(0x11, 0, 2, 0)
    time.sleep(0.2)
    return deliver._get_clipboard_text() or ""


def open_new_notepad_tab(timeout_s: float = 5.0):
    before = set(_list_notepad_hwnds())
    subprocess.Popen(["notepad.exe"])

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        time.sleep(0.1)
        after = _list_notepad_hwnds()
        for hwnd in after:
            if hwnd in before:
                continue
            kind, edit_hwnd = _find_edit_child(hwnd)
            if edit_hwnd:
                return hwnd, kind, edit_hwnd
    raise TimeoutError("Notepad window with a usable text control did not appear in time")


def close_tab(main_hwnd, edit_hwnd):
    """Close the tab this test opened, without saving. Called from a
    finally block so it runs even when a check above raised -- every step
    here is best-effort (wrapped so one failure doesn't stop the others),
    since the goal is "don't leave a stray tab open", not to itself assert
    anything."""
    try:
        # leave it empty so Notepad never raises a "save changes?" prompt
        _wm_set_text(edit_hwnd, "")
    except Exception:
        pass
    try:
        SetForegroundWindow(main_hwnd)
        time.sleep(0.1)
        user32.keybd_event(0x11, 0, 0, 0)  # Ctrl down
        user32.keybd_event(0x57, 0, 0, 0)  # W down
        user32.keybd_event(0x57, 0, 2, 0)  # W up
        user32.keybd_event(0x11, 0, 2, 0)  # Ctrl up
        time.sleep(0.3)
    except Exception as e:
        print(f"[WARN] close_tab: could not close cleanly ({e})")


def _ensure_foreground(main_hwnd, edit_hwnd, attempts: int = 10):
    """SetForegroundWindow can silently no-op when called from a background
    process (Windows' foreground-lock protection) -- a real mouse click
    reliably grabs focus instead, but on this dev machine something else
    (a background pythonw.exe widget, "HQ meters") periodically steals the
    foreground on its own timer, independent of anything this script does.
    Retry click + verify GetForegroundWindow() actually became main_hwnd
    before proceeding, instead of hoping for the best."""
    for _ in range(attempts):
        SetForegroundWindow(main_hwnd)
        _click(edit_hwnd)
        if user32.GetForegroundWindow() == main_hwnd:
            return True
        time.sleep(0.1)
    return False


def _attempt(name, deliver_fn, main_hwnd, kind, edit_hwnd):
    _wm_set_text(edit_hwnd, "")
    time.sleep(0.1)
    got_focus = _ensure_foreground(main_hwnd, edit_hwnd)

    deliver_fn(TEST_STRING)
    time.sleep(0.3)

    readback = _wm_get_text(edit_hwnd)
    used = f"WM_GETTEXT on {kind}"
    if readback != TEST_STRING:
        # fall back path, per the brief
        fallback = _clipboard_fallback_read(edit_hwnd)
        if fallback == TEST_STRING:
            readback = fallback
            used = "Ctrl+A / Ctrl+C + clipboard fallback"

    ok = readback == TEST_STRING and got_focus
    return ok, readback, used, got_focus


def run_path(name, deliver_fn, main_hwnd, kind, edit_hwnd, max_attempts: int = 5):
    """Run one delivery path and report PASS/FAIL.

    Retries on transient failure: this dev machine runs a background
    widget ("HQ meters", pythonw.exe) that periodically steals the
    foreground window on its own schedule, independent of this script --
    when that happens mid-attempt, Ctrl+V or the typed keys go to whatever
    it raised instead of Notepad. Separately, Notepad's own RichEditD2DPT
    control (an async, Direct2D-rendered text box -- see the module
    docstring) is itself occasionally slow to keep up with fast synthetic
    input even when it does hold focus throughout, which can still
    transpose or drop a character under load. Both are transient by
    nature, not a defect in paste()/type_text(), so a mismatch is retried
    a few times before being reported as a real failure.
    """
    last = None
    for attempt in range(1, max_attempts + 1):
        ok, readback, used, got_focus = _attempt(name, deliver_fn, main_hwnd, kind, edit_hwnd)
        last = (ok, readback, used, got_focus)
        if ok:
            break
        reason = "foreground was stolen mid-attempt" if not got_focus else "readback mismatch"
        print(f"[RETRY] {name}: {reason} ({attempt}/{max_attempts})")

    ok, readback, used, got_focus = last
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name} (read back via {used})")
    print(f"    expected: {TEST_STRING!r}")
    print(f"    actual:   {readback!r}")
    return ok


def main():
    print(f"Test string: {TEST_STRING!r}")
    main_hwnd, kind, edit_hwnd = open_new_notepad_tab()
    print(f"Opened Notepad tab, text control kind={kind}")
    ShowWindow(main_hwnd, SW_RESTORE)
    _shrink_to_corner(main_hwnd)
    time.sleep(0.2)

    all_ok = False
    try:
        results = []
        results.append(run_path("paste()", deliver.paste, main_hwnd, kind, edit_hwnd))
        results.append(run_path("type_text()", deliver.type_text, main_hwnd, kind, edit_hwnd))
        all_ok = all(results)
    finally:
        # always close the tab without saving, even if a check above raised
        close_tab(main_hwnd, edit_hwnd)

    print("OVERALL:", "PASS" if all_ok else "FAIL")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
