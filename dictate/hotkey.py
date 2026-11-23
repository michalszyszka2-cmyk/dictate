"""Low-level global hotkey handling for dictate.

Default bindings (Wispr Flow's own, plus Right Shift), all changeable in
config.yaml's ``hotkeys:`` block or the tray's Settings window:
  - hold Right Shift        -> record while held, release = stop   (hotkeys.dictate)
  - hold Ctrl+Win           -> record while held, release = stop   (hotkeys.combo)
  - Escape while holding    -> cancel the in-progress recording
  - Ctrl+Win+Space          -> toggle hands-free recording         (hotkeys.handsfree)
  - hold Right Shift + Right Ctrl -> record a question for Claude (ask box);
                              releasing either key stops it         (hotkeys.ask)

Implementation: pynput's keyboard.Listener. On Windows this installs a
WH_KEYBOARD_LL low-level hook internally (event-driven, not polling). The
listener is never started with suppress=True, so every key event still
reaches the OS and the foreground app -- normal Ctrl and Win shortcuts
(copy/paste, Win+D, alt-tab, etc.) keep working untouched.

Either key of a combo group (left or right Ctrl, Win or Alt) counts.

The hold key (Right Shift by default) is also an ordinary key, so it only
starts a recording once it has been held on its own for SHIFT_ARM_S; any
other key pressed while it is down (Shift+letter) disarms it, or cancels the
clip if it had already started. Nothing is lost to the delay: the recorder's
pre-roll ring buffer already holds the half second before on_start.
"""

from __future__ import annotations

import threading
from typing import Callable, Optional

from pynput import keyboard

Key = keyboard.Key

_GROUPS = {
    "ctrl": {Key.ctrl, Key.ctrl_l, Key.ctrl_r},
    "win": {Key.cmd, Key.cmd_l, Key.cmd_r},
    "alt": {Key.alt, Key.alt_l, Key.alt_r, getattr(Key, "alt_gr", Key.alt_r)},
}
_KEYS = {
    "right_shift": Key.shift_r, "right_ctrl": Key.ctrl_r,
    "f8": Key.f8, "f9": Key.f9, "f10": Key.f10, "f12": Key.f12,
    "scroll_lock": getattr(Key, "scroll_lock", None), "pause": getattr(Key, "pause", None),
    "insert": getattr(Key, "insert", None),
}
LABELS = {
    "right_shift": "Right Shift", "right_ctrl": "Right Ctrl", "f8": "F8", "f9": "F9",
    "f10": "F10", "f12": "F12", "scroll_lock": "Scroll Lock", "pause": "Pause",
    "insert": "Insert", "ctrl+win": "Ctrl + Win", "alt+win": "Alt + Win", "off": "Off",
}
SHIFT_ARM_S = 0.25
DEFAULTS = {"dictate": "right_shift", "ask": "right_ctrl", "combo": "ctrl+win", "handsfree": True}


def describe(hk: dict) -> str:
    """'hold Right Shift; + Right Ctrl asks Claude' for the tray tooltip."""
    parts = []
    if hk.get("dictate", "off") != "off":
        parts.append("hold " + LABELS[hk["dictate"]])
    elif hk.get("combo", "off") != "off":
        parts.append("hold " + LABELS[hk["combo"]])
    if hk.get("dictate", "off") != "off" and hk.get("ask", "off") != "off":
        parts.append("+ " + LABELS[hk["ask"]] + " asks Claude")
    return "; ".join(parts) or "no hotkey set"


class HotkeyListener:
    """Global hotkey listener implementing the record bindings.

    on_start(), on_stop(), on_cancel() are invoked on the pynput listener
    thread -- callers that touch the UI must marshal back to the main
    thread themselves (e.g. tkinter's `after()`).
    """

    def __init__(
        self,
        on_start: Callable[[], None],
        on_stop: Callable[[], None],
        on_cancel: Callable[[], None],
        on_ask_stop: Optional[Callable[[], None]] = None,
        keys: Optional[dict] = None,
    ):
        self.on_start = on_start
        self.on_stop = on_stop
        self.on_cancel = on_cancel
        self.on_ask_stop = on_ask_stop

        self._lock = threading.Lock()
        self._enabled = True
        self._reset_state()
        self._apply_keys(keys or DEFAULTS)

        self._listener: Optional[keyboard.Listener] = None

    def _reset_state(self):
        self._a_down: set = set()  # keys of combo group A held (Ctrl by default)
        self._b_down: set = set()  # keys of combo group B held (Win)
        self._space_down = False
        self._space_handled = False
        self._shift_down = False  # the hold key, whichever it is
        self._ask_down = False
        self._shift_timer: Optional[threading.Timer] = None
        self._shift_started = False  # the current clip came from the hold key

        self._recording = False
        self._hold_started = False
        self._hands_free = False
        self._ask = False  # the current clip is a question for the ask box

    def _apply_keys(self, keys: dict):
        hk = dict(DEFAULTS)
        hk.update(keys or {})
        self._solo = _KEYS.get(hk["dictate"])
        self._ask_key = _KEYS.get(hk["ask"]) if self._solo is not None else None
        if self._ask_key == self._solo:
            self._ask_key = None
        if hk["combo"] in ("ctrl+win", "alt+win"):
            a, b = hk["combo"].split("+")
            self._group_a, self._group_b = _GROUPS[a], _GROUPS[b]
        else:
            self._group_a = self._group_b = None
        self._handsfree_on = bool(hk["handsfree"]) and self._group_a is not None

    def configure(self, keys: dict):
        """Swap the bindings live. A clip in progress is cancelled, never pasted."""
        with self._lock:
            self._disarm_shift()
            was_recording = self._recording
            self._reset_state()
            self._apply_keys(keys)
            if was_recording:
                self._fire(self.on_cancel)

    # -- lifecycle ---------------------------------------------------------
    def start(self):
        if self._listener is not None:
            return
        self._listener = keyboard.Listener(
            on_press=self._on_press,
            on_release=self._on_release,
            suppress=False,  # never swallow keys headed for other apps
        )
        self._listener.start()

    def stop(self):
        if self._listener is not None:
            self._listener.stop()
            self._listener = None

    @property
    def hands_free(self) -> bool:
        return self._hands_free

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def enabled(self) -> bool:
        return self._enabled

    def set_enabled(self, enabled: bool):
        """Pause or resume the bindings without unhooking the keyboard.

        Disabling mid-clip cancels it (on_cancel), so turning dictation off
        never pastes anything. Key state is reset either way: a Ctrl held
        across the switch must not count as half a combo afterwards.
        """
        with self._lock:
            if enabled == self._enabled:
                return
            self._enabled = enabled
            self._disarm_shift()
            was_recording = self._recording
            self._reset_state()
            if was_recording and not enabled:
                self._fire(self.on_cancel)

    # -- event handling ------------------------------------------------------
    def _on_press(self, key):
        with self._lock:
            if not self._enabled:
                return
            if self._solo is not None and key == self._solo:
                if not self._shift_down:  # ignore auto-repeat
                    self._shift_down = True
                    if self._ask_down:
                        self._start_ask()
                    else:
                        self._arm_shift()
                return
            if self._ask_key is not None and key == self._ask_key and self._shift_down:
                if not self._ask_down:  # ignore auto-repeat
                    self._ask_down = True
                    self._start_ask()
                return
            if self._shift_down and key != Key.esc and not self._ask:
                self._shift_interrupted()
            if self._ask_key is not None and key == self._ask_key:
                self._ask_down = True  # held first; the hold key may follow
            if self._group_a is not None and key in self._group_a:
                self._a_down.add(key)
            elif self._group_b is not None and key in self._group_b:
                self._b_down.add(key)
            elif key == Key.space and self._handsfree_on:
                was_down = self._space_down
                self._space_down = True
                if not was_down:
                    self._maybe_toggle_handsfree()
                return
            elif key == Key.esc:
                self._maybe_cancel()
                return
            else:
                return
            self._maybe_start_hold()

    def _on_release(self, key):
        with self._lock:
            if not self._enabled:
                return
            if self._ask and key in (self._solo, self._ask_key):
                self._stop_ask()
            if self._ask_key is not None and key == self._ask_key:
                self._ask_down = False
            if self._solo is not None and key == self._solo:
                self._shift_down = False
                self._disarm_shift()
                if self._recording and self._shift_started and not self._hands_free:
                    self._recording = False
                    self._shift_started = False
                    self._fire(self.on_stop)
                return
            if self._group_a is not None and key in self._group_a:
                self._a_down.discard(key)
            elif self._group_b is not None and key in self._group_b:
                self._b_down.discard(key)
            elif key == Key.space:
                self._space_down = False
                self._space_handled = False
                return
            else:
                return
            self._maybe_stop_hold()

    # -- combo logic (call sites hold self._lock) ---------------------------
    def _combo_down(self) -> bool:
        return bool(self._a_down) and bool(self._b_down)

    def _maybe_toggle_handsfree(self):
        if not self._combo_down() or self._space_handled:
            return
        self._space_handled = True
        self._hands_free = not self._hands_free
        if self._hands_free:
            if not self._recording:
                self._recording = True
                self._hold_started = False
                self._fire(self.on_start)
        else:
            if self._recording:
                self._recording = False
                self._hold_started = False
                self._fire(self.on_stop)

    def _maybe_start_hold(self):
        if self._hands_free:
            return  # hands-free owns the session; hold does nothing
        if self._combo_down() and not self._recording:
            self._recording = True
            self._hold_started = True
            self._fire(self.on_start)

    def _maybe_stop_hold(self):
        if self._hands_free:
            return
        if not self._combo_down() and self._recording and self._hold_started:
            self._recording = False
            self._hold_started = False
            self._fire(self.on_stop)

    # -- the hold key (call sites hold self._lock, except the timer) ----------
    def _arm_shift(self):
        if self._recording or self._hands_free:
            return
        self._disarm_shift()
        self._shift_timer = threading.Timer(SHIFT_ARM_S, self._shift_armed)
        self._shift_timer.daemon = True
        self._shift_timer.start()

    def _disarm_shift(self):
        if self._shift_timer is not None:
            self._shift_timer.cancel()
            self._shift_timer = None

    def _shift_armed(self):
        with self._lock:
            if self._shift_timer is None or not self._enabled:
                return  # disarmed in the meantime
            self._shift_timer = None
            if self._shift_down and not self._recording and not self._hands_free:
                self._recording = True
                self._shift_started = True
                self._fire(self.on_start)

    def _shift_interrupted(self):
        """Another key went down with the hold key held: it's typing, not dictating."""
        self._disarm_shift()
        if self._recording and self._shift_started and not self._hands_free:
            self._recording = False
            self._shift_started = False
            self._fire(self.on_cancel)

    # -- ask chord (call sites hold self._lock) -------------------------------
    def _start_ask(self):
        if self._hands_free or self.on_ask_stop is None:
            return
        self._disarm_shift()
        if self._recording and not self._shift_started:
            return  # a combo clip owns the mic
        self._ask = True
        self._shift_started = False
        if not self._recording:  # else: a hold-key clip just became a question
            self._recording = True
            self._fire(self.on_start)

    def _stop_ask(self):
        self._ask = False
        if self._recording:
            self._recording = False
            self._fire(self.on_ask_stop)

    def _maybe_cancel(self):
        if self._recording:
            self._recording = False
            self._hold_started = False
            self._shift_started = False
            self._hands_free = False
            self._ask = False
            self._fire(self.on_cancel)

    @staticmethod
    def _fire(callback):
        try:
            callback()
        except Exception:
            # a callback failure must never take the hook down
            import traceback

            traceback.print_exc()


if __name__ == "__main__":
    import time

    def _start():
        print("START recording")

    def _stop():
        print("STOP recording")

    def _cancel():
        print("CANCEL recording")

    hk = HotkeyListener(_start, _stop, _cancel)
    hk.start()
    print("Listening for Right Shift / Ctrl+Win (hold), Ctrl+Win+Space (toggle), Escape (cancel). Ctrl+C to quit.")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        hk.stop()
