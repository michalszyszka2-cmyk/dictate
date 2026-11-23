"""HotkeyListener with simulated key events (no real keyboard hook)."""
import time

from pynput.keyboard import Key, KeyCode

from dictate import hotkey


def make(keys=None):
    log = []
    hk = hotkey.HotkeyListener(lambda: log.append("start"), lambda: log.append("stop"),
                               lambda: log.append("cancel"), lambda: log.append("ask"), keys=keys)
    return hk, log


def hold(hk, key, seconds=hotkey.SHIFT_ARM_S + 0.1):
    hk._on_press(key)
    time.sleep(seconds)


def test_default_right_shift_hold():
    hk, log = make()
    hold(hk, Key.shift_r)
    hk._on_release(Key.shift_r)
    assert log == ["start", "stop"]


def test_shift_plus_letter_is_typing():
    hk, log = make()
    hk._on_press(Key.shift_r)
    hk._on_press(KeyCode.from_char("a"))
    time.sleep(hotkey.SHIFT_ARM_S + 0.1)
    hk._on_release(Key.shift_r)
    assert log == []


def test_ask_chord_either_order():
    for first, second in ((Key.shift_r, Key.ctrl_r), (Key.ctrl_r, Key.shift_r)):
        hk, log = make()
        hk._on_press(first)
        hk._on_press(second)
        hk._on_release(second)
        hk._on_release(first)
        assert log == ["start", "ask"], (first, log)


def test_ctrl_win_combo_and_handsfree():
    hk, log = make()
    hk._on_press(Key.ctrl_l)
    hk._on_press(Key.cmd)
    hk._on_release(Key.cmd)
    assert log == ["start", "stop"]
    log.clear()
    hk._on_release(Key.ctrl_l)
    hk._on_press(Key.ctrl_l)
    hk._on_press(Key.cmd)
    hk._on_press(Key.space)  # hands-free on
    hk._on_release(Key.space)
    hk._on_release(Key.cmd)
    hk._on_release(Key.ctrl_l)
    assert log == ["start"]


def test_custom_keys_f9_alt_win_no_ask():
    hk, log = make({"dictate": "f9", "ask": "off", "combo": "alt+win", "handsfree": False})
    hold(hk, Key.shift_r)  # old key does nothing now
    hk._on_release(Key.shift_r)
    hold(hk, Key.f9)
    hk._on_press(Key.ctrl_r)  # no ask key: counts as typing, cancels
    hk._on_release(Key.f9)
    hk._on_release(Key.ctrl_r)
    assert log == ["start", "cancel"]
    log.clear()
    hk._on_press(Key.ctrl_l)
    hk._on_press(Key.cmd)  # Ctrl+Win is no longer a combo
    hk._on_release(Key.cmd)
    hk._on_release(Key.ctrl_l)
    hk._on_press(Key.alt_l)
    hk._on_press(Key.cmd)
    hk._on_release(Key.alt_l)
    assert log == ["start", "stop"]


def test_combo_off_and_configure_live():
    hk, log = make({"combo": "off"})
    hk._on_press(Key.ctrl_l)
    hk._on_press(Key.cmd)
    assert log == []
    hk._on_release(Key.cmd)
    hk._on_release(Key.ctrl_l)
    hold(hk, Key.shift_r)
    hk.configure({"dictate": "f8"})  # mid-clip: cancelled, never pasted
    assert log == ["start", "cancel"]
    hk._on_release(Key.shift_r)
    hold(hk, Key.f8)
    hk._on_release(Key.f8)
    assert log == ["start", "cancel", "start", "stop"]


def test_describe():
    assert hotkey.describe(hotkey.DEFAULTS) == "hold Right Shift; + Right Ctrl asks Claude"
    assert hotkey.describe({"dictate": "off", "ask": "off", "combo": "alt+win"}) == "hold Alt + Win"
