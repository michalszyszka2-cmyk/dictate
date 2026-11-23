"""Tray app wiring capture + hotkey + pipeline + history together.

Stream E wires the real pipeline in: key-release hands the captured clip to
``pipeline.run`` on a worker thread (so the pynput hotkey thread is never
blocked) while the pill shows "..."; the pipeline runs Parakeet, the
vocabulary pass, per-app policy, cleanup and delivery, then this app hides
the pill once it returns.

At start-up, refuses to run (message box + exit code 2) if Wispr Flow.exe is
already running -- two low-level keyboard hooks fighting over Ctrl+Win is a
worse experience than just not starting.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys
import threading
import time
from pathlib import Path

import psutil
import pystray
from PIL import Image, ImageDraw, ImageOps

from dictate import __version__, askbox, capture, cleanup, config, deliver, engine, history, hotkey, install, models, paths, pipeline, providers, settings_ui, ui, whisper_api

SAMPLE_RATE = capture.SAMPLE_RATE
LEVEL_POLL_INTERVAL = 0.05
WISPR_PROCESS_NAME = "Wispr Flow.exe"


def _wispr_running() -> bool:
    for proc in psutil.process_iter(["name"]):
        try:
            name = proc.info.get("name") or ""
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if name.lower() == WISPR_PROCESS_NAME.lower():
            return True
    return False


def _refuse_to_start_if_wispr_running():
    if not _wispr_running():
        return
    message = (
        "dictate can't start: Wispr Flow is running.\n\n"
        "Both tools hook Ctrl+Win at the OS level -- running both fights over "
        "the same hotkey. Quit Wispr Flow first, then relaunch dictate."
    )
    try:
        ctypes.windll.user32.MessageBoxW(0, message, "dictate", 0x30)  # MB_ICONWARNING
    except Exception:
        print(message, file=sys.stderr)
    sys.exit(2)


_INSTANCE_MUTEX = None  # held for the life of the process


def _refuse_to_start_if_already_running():
    """One dictate per Windows session: two keyboard hooks would both paste every clip."""
    global _INSTANCE_MUTEX
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    _INSTANCE_MUTEX = kernel32.CreateMutexW(None, False, "Local\\dictate-single-instance")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        print("dictate is already running.", file=sys.stderr)
        sys.exit(0)


def _appdata_is_redirected() -> bool:
    """True when this process's %APPDATA% writes land in an MSIX package's
    LocalCache -- e.g. when started from a Claude desktop session. The Desktop
    and Startup shortcuts never see that copy: on 24 Sept it left the real
    folder without the Parakeet model, so the Desktop icon died silently.

    Checked by writing a probe file and looking for it under
    Packages\\*\\LocalCache\\Roaming, because the process has no package
    identity (GetCurrentPackageFullName says "no package") yet is still
    redirected. Outside a package the probe lands in the real folder, so the
    Explorer relaunch below can never loop.
    """
    name = f"dictate-probe-{os.getpid()}.tmp"
    probe = Path(os.environ.get("APPDATA", "")) / name
    try:
        probe.touch()
        packages = Path(os.environ.get("LOCALAPPDATA", "")) / "Packages"
        return any(packages.glob(f"*/LocalCache/Roaming/{name}"))
    except Exception:
        return False
    finally:
        try:
            probe.unlink()
        except Exception:
            pass


def _hand_off_if_packaged():
    """Relaunch through Explorer (outside any package) and exit, so the tray app
    always reads and writes the real %APPDATA%\\dictate."""
    if not _appdata_is_redirected():
        return
    import subprocess

    from .install import launch_target

    subprocess.Popen(["explorer.exe", str(launch_target())])
    sys.exit(0)


LANGUAGE_LABELS = {"en": "English", "pl": "Polish", "auto": "Auto (English or Polish)"}
TRAY_ICON_PATH = Path(__file__).resolve().parent.parent / "assets" / "dictate.ico"
ENGINE_LABELS = {"parakeet": "Parakeet (on this PC)", "whisper": "Whisper (cloud, needs a key)"}
# Optional: a local clarity app that scores clips. Set DICTATE_CLARITY_DIR; otherwise the menu item is hidden.
CLARITY_DIR = Path(os.environ.get("DICTATE_CLARITY_DIR") or "")


def _tray_log(line: str):
    """pythonw has no console, so tray-menu failures would vanish without this."""
    try:
        with open(os.path.join(config.APPDIR, "tray.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line.rstrip() + "\n")
    except Exception:
        pass


def _tray_image(enabled: bool) -> Image.Image:
    """The shortcut's own mic tile when on; the same tile greyed and faded when
    off, so the state reads at a glance in the taskbar the way Wispr's does."""
    try:
        img = Image.open(TRAY_ICON_PATH)
        img.size = max(img.info.get("sizes", {img.size}))  # pick the largest frame
        img = img.convert("RGBA").resize((64, 64), Image.LANCZOS)
    except Exception:
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        draw.ellipse((4, 4, 60, 60), fill=(76, 175, 80, 255))
    if enabled:
        return img
    alpha = img.getchannel("A").point(lambda a: a * 55 // 100)
    grey = ImageOps.grayscale(img).convert("RGBA")
    grey.putalpha(alpha)
    return grey


class DictateApp:
    def __init__(self):
        self.recorder = capture.Recorder(sample_rate=SAMPLE_RATE)
        self.pill = ui.Pill()
        self.askbox = askbox.AskBox(self.pill.root)
        self.hotkey = hotkey.HotkeyListener(
            on_start=self._on_start,
            on_stop=self._on_stop,
            on_cancel=self._on_cancel,
            on_ask_stop=self._on_ask_stop,
            keys=config.hotkeys(),
        )
        self.settings = settings_ui.SettingsWindow(self.pill.root, self._settings_saved)

        self._level_thread: threading.Thread | None = None
        self._level_thread_stop = threading.Event()

        self._t_key_release = 0.0
        self._foreground_app = ""
        self._foreground_title = ""

        self._toggle_lock = threading.Lock()
        self.icon = pystray.Icon(
            "dictate",
            icon=_tray_image(True),
            title=self._tray_title(),
            menu=pystray.Menu(
                # default=True: a left-click on the tray icon flips it too
                pystray.MenuItem(
                    "Dictation on", self._toggle_enabled, checked=lambda item: self.hotkey.enabled, default=True
                ),
                pystray.MenuItem(
                    "Language",
                    pystray.Menu(*(
                        pystray.MenuItem(label, self._language_setter(code), checked=self._language_checker(code), radio=True)
                        for code, label in LANGUAGE_LABELS.items()
                    )),
                ),
                pystray.MenuItem(
                    "Engine",
                    pystray.Menu(*(
                        pystray.MenuItem(label, self._engine_setter(code), checked=self._engine_checker(code), radio=True)
                        for code, label in ENGINE_LABELS.items()
                    )),
                ),
                pystray.MenuItem(
                    "Clarity nudges", self._toggle_clarity_nudges, checked=lambda item: config.clarity_nudges_enabled(),
                    visible=CLARITY_DIR.is_dir(),
                ),
                pystray.MenuItem("Settings (keys and clean-up)...", self._open_settings_window),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(
                    "Start with Windows", self._toggle_autostart, checked=lambda item: install.shortcut_path().exists()
                ),
                pystray.MenuItem("Open settings folder", self._open_settings),
                pystray.MenuItem("Whisper key (OpenAI or Groq)...", self._set_whisper_key),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(f"dictate {__version__}", None, enabled=False),
                pystray.MenuItem("Quit dictate", self._quit),
            ),
        )

    # -- hotkey callbacks (fire on the pynput listener thread) ----------------
    def _on_start(self):
        self.recorder.start()
        self.pill.show_recording()
        self._start_level_thread()

    def _on_stop(self):
        # capture foreground app/window at key-release, before anything else
        # (paste, if any, can shift focus)
        self._foreground_app, self._foreground_title = deliver.foreground_app()
        t_release = time.time()

        self._stop_level_thread()
        audio = self.recorder.stop()
        self.pill.show_processing()

        # Run the pipeline (Parakeet -> vocab -> policy -> cleanup -> paste ->
        # log) on a worker thread so the pynput hotkey listener thread is
        # never blocked by it -- a held-up hook thread would delay the next
        # key event across the whole system, not just this app.
        app_name, title = self._foreground_app, self._foreground_title
        threading.Thread(
            target=self._run_pipeline,
            args=(audio, app_name, title, t_release),
            daemon=True,
        ).start()

    def _run_pipeline(self, audio, app_name, title, t_release):
        try:
            notify_fn = None
            if config.clarity_nudges_enabled():
                notify_fn = lambda m: self.icon.notify(m, "clarity")
            pipeline.run(audio, app_name, title, t_release, notify_fn=notify_fn)
        except Exception:
            import traceback

            traceback.print_exc()
        finally:
            self.pill.hide()

    def _on_ask_stop(self):
        """Right Shift + Right Ctrl released: the clip is a question for Claude.
        Transcribe it (no cleanup, no paste) and put it in the ask box for a
        Cancel / Send check. An empty clip just opens the box for typing."""
        self._stop_level_thread()
        audio = self.recorder.stop()
        self.pill.hide()
        self.askbox.listening()

        def _transcribe():
            text = ""
            try:
                text = pipeline.transcribe(audio)[0].strip()
            except Exception:
                import traceback

                _tray_log(traceback.format_exc())
            self.askbox.open(text)

        threading.Thread(target=_transcribe, daemon=True).start()

    def _on_cancel(self):
        self._stop_level_thread()
        self.recorder.cancel()
        self.pill.hide()

    # -- level meter ------------------------------------------------------------
    def _start_level_thread(self):
        self._level_thread_stop.clear()
        self.recorder.drain_slices()  # drop audio from before the hotkey

        def _poll():
            while not self._level_thread_stop.is_set():
                self.pill.push_audio(self.recorder.drain_slices())
                time.sleep(LEVEL_POLL_INTERVAL)

        self._level_thread = threading.Thread(target=_poll, daemon=True)
        self._level_thread.start()

    def _stop_level_thread(self):
        self._level_thread_stop.set()
        if self._level_thread is not None:
            self._level_thread.join(timeout=0.5)
            self._level_thread = None

    # -- tray menu (pystray thread) ---------------------------------------------
    def _tray_title(self) -> str:
        if not self.hotkey.enabled:
            return "dictate - off (click to turn on)"
        try:
            language = LANGUAGE_LABELS.get(config.language(), "English")
        except Exception:
            language = "English"
        eng = " + Whisper" if config.engine() == "whisper" else ""
        return f"dictate - on, {language}{eng} ({hotkey.describe(config.hotkeys())})"

    def _toggle_enabled(self, icon=None, item=None):
        try:
            self.set_enabled(not self.hotkey.enabled)
        except Exception:
            import traceback

            _tray_log(traceback.format_exc())

    def set_enabled(self, enabled: bool):
        """Off = hotkeys ignored AND the mic stream closed, so the Windows mic
        indicator goes away too. On = reopen the mic, then re-arm the keys."""
        with self._toggle_lock:
            if enabled == self.hotkey.enabled:
                return
            if enabled:
                try:
                    self.recorder.open()
                except Exception as exc:
                    self.icon.notify(f"Couldn't open the microphone: {exc}", "dictate")
                    return
                self.hotkey.set_enabled(True)
            else:
                self.hotkey.set_enabled(False)  # cancels a clip in progress
                self._stop_level_thread()
                self.pill.hide()
                self.recorder.close()
            self.icon.icon = _tray_image(enabled)
            self.icon.title = self._tray_title()
            self.icon.update_menu()

    @staticmethod
    def _language_checker(code: str):
        return lambda item: config.language() == code

    def _language_setter(self, code: str):
        def set_language(icon=None, item=None):
            try:
                config.set_language(code)
            except Exception:
                import traceback

                _tray_log(traceback.format_exc())
            self.icon.title = self._tray_title()
            self.icon.update_menu()

        return set_language

    @staticmethod
    def _engine_checker(code: str):
        return lambda item: config.engine() == code

    def _engine_setter(self, code: str):
        def set_engine(icon=None, item=None):
            try:
                config.set_engine(code)
                if code == "whisper" and whisper_api.provider() is None:
                    self.icon.notify("No Whisper key on this PC yet - clips use Parakeet until you add one "
                                     "(tray: Whisper key...).", "dictate")
            except Exception:
                import traceback

                _tray_log(traceback.format_exc())
            self.icon.title = self._tray_title()
            self.icon.update_menu()

        return set_engine

    def _toggle_clarity_nudges(self, icon=None, item=None):
        try:
            current = config.clarity_nudges_enabled()
            config.set_clarity_nudges(not current)
        except Exception:
            import traceback

            _tray_log(traceback.format_exc())
        self.icon.update_menu()

    def _toggle_autostart(self, icon=None, item=None):
        if install.shortcut_path().exists():
            install.remove_autostart()
        else:
            install.install_autostart()
        self.icon.update_menu()

    def _open_settings(self, icon=None, item=None):
        os.startfile(paths.settings_dir())

    # Dialogs must run on the Tk thread (the pill's mainloop), not pystray's.
    def _dialog_parent(self):
        import tkinter as tk

        top = tk.Toplevel(self.pill.root)
        top.withdraw()
        top.attributes("-topmost", True)
        return top

    def _open_settings_window(self, icon=None, item=None):
        self.settings.open()

    def _settings_saved(self):
        """Settings window saved: re-arm the keyboard hook with the new keys."""
        try:
            self.hotkey.configure(config.hotkeys())
        except Exception:
            import traceback

            _tray_log(traceback.format_exc())
        self.icon.title = self._tray_title()
        self.icon.update_menu()

    def _set_whisper_key(self, icon=None, item=None):
        self.pill.root.after(0, self._set_whisper_key_tk)

    def _set_whisper_key_tk(self):
        from tkinter import messagebox, simpledialog

        top = self._dialog_parent()
        try:
            p = whisper_api.provider()
            key = simpledialog.askstring(
                "dictate - Whisper key",
                "Paste an OpenAI key (sk-...) or a Groq key (gsk_...) for the Whisper engine.\n"
                "Stored on this PC only. Leave it empty and press OK to remove both.\n\n"
                + (f"A {p[0]} key is already set." if p else "No key set yet."),
                parent=top, show="*",
            )
            if key is None:
                return
            key = key.strip()
            name = "GROQ_API_KEY" if key.startswith("gsk_") else "OPENAI_API_KEY"
            if key:
                paths.write_secret(name, key)
                os.environ[name] = key
            else:
                for n in ("OPENAI_API_KEY", "GROQ_API_KEY"):
                    paths.write_secret(n, "")
                    os.environ.pop(n, None)
            messagebox.showinfo("dictate", "Key saved." if key else "Keys removed.", parent=top)
        except Exception:
            import traceback

            _tray_log(traceback.format_exc())
        finally:
            top.destroy()

    # -- lifecycle ---------------------------------------------------------------
    def run(self):
        self.recorder.open()
        # pystray creates its windows (and so the tray menu) on its own thread.
        # Make that thread per-monitor DPI aware, or Windows bitmap-stretches
        # the menu at any scaling above 100% and the text comes out blurry.
        tray_run = self.icon._run

        def _tray_run_dpi_aware():
            with ui._dpi_aware():
                tray_run()

        self.icon._run = _tray_run_dpi_aware
        self.icon.run_detached()
        threading.Thread(target=self._prepare, daemon=True).start()
        try:
            self.pill.run()  # blocks on the Tk mainloop (must be main thread)
        finally:
            self._shutdown()

    def _prepare(self):
        """Download the model on a new PC, then load the Parakeet recognizer and
        warm up the cleanup chain before the hotkey goes live -- engine load
        alone is ~2 s, which would blow the latency budget on the first clip."""
        try:
            if models.missing():
                self._download_model()
            engine.get_engine()
            try:
                cleanup.warmup()
            except Exception:
                pass
            self.hotkey.start()
            self.icon.title = self._tray_title()
        except Exception as e:
            import traceback

            _tray_log(traceback.format_exc())
            self.icon.title = "dictate - not ready: speech model missing (restart to retry)"
            try:
                self.icon.notify(f"Could not get the speech model: {e}. Check the internet connection and restart dictate.", "dictate")
            except Exception:
                pass

    def _download_model(self):
        mb = models.TOTAL_BYTES / 1e6
        try:
            self.icon.notify(f"First start on this PC: downloading the speech model ({mb:.0f} MB). "
                             "Dictation turns on when it is done.", "dictate")
        except Exception:
            pass
        last = [0.0]

        def _progress(done: int, total: int):
            if time.monotonic() - last[0] > 1.0:
                last[0] = time.monotonic()
                self.icon.title = f"dictate - downloading speech model {done * 100 // total}% of {mb:.0f} MB"

        models.ensure(_progress)
        try:
            self.icon.notify(f"Speech model ready. {hotkey.describe(config.hotkeys()).split(';')[0].capitalize()} and speak.",
                             "dictate")
        except Exception:
            pass

    def _quit(self, icon=None, item=None):
        self.pill.shutdown()

    def _shutdown(self):
        self._stop_level_thread()
        self.hotkey.stop()
        self.recorder.close()
        try:
            self.icon.stop()
        except Exception:
            pass


def _trust_windows_certificates():
    """Verify HTTPS against the Windows certificate store, not the bundled
    certifi list. Antivirus that scans HTTPS (Avast, measured 29 Oct 2026)
    re-signs every site with its own root, which only Windows trusts: without
    this every clean-up call from the installed app failed with
    CERTIFICATE_VERIFY_FAILED."""
    try:
        import truststore

        truststore.inject_into_ssl()
    except Exception:
        pass


def main():
    _trust_windows_certificates()
    parser = argparse.ArgumentParser(prog="python -m dictate.app")
    parser.add_argument("--doctor", action="store_true", help="run health checks and exit")
    parser.add_argument("--autostart", action="store_true", help="add a shell:startup shortcut to dictate.cmd")
    parser.add_argument("--no-autostart", action="store_true", help="remove the shell:startup shortcut")
    parser.add_argument("--desktop-icon", action="store_true", help="add a Desktop shortcut to dictate.cmd")
    parser.add_argument("--no-desktop-icon", action="store_true", help="remove the Desktop shortcut")
    parser.add_argument("--version", action="version", version=f"dictate {__version__}")
    parser.add_argument("--selftest", metavar="WAV", help="transcribe WAV, write the text to WAV.txt and exit")
    parser.add_argument("--selftest-cleanup", nargs=2, metavar=("PROVIDER", "OUT"),
                        help="one clean-up call through PROVIDER (key from secrets.env), result to OUT")
    args = parser.parse_args()

    if args.selftest_cleanup:
        # Proves a frozen build carries each provider's code. Writes the text
        # or the error's class and message; never the key.
        name, out = args.selftest_cleanup
        try:
            key = cleanup._key(providers.KEY_NAMES.get(name, "")) if name in providers.KEY_NAMES else ""
            text, _ = providers.call(name, key or "no-key", "Fix the punctuation. Return only the text.",
                                     "so we meet thursday ok", 64, providers.budget_for(name) or 10)
            result = "ok: " + text
        except Exception as exc:
            result = f"error: {type(exc).__module__}.{type(exc).__name__}: {str(exc)[:200]}"
        Path(out).write_text(result, encoding="utf-8")
        sys.exit(0)

    if args.selftest:
        # The installed exe has no console, so the result goes to a file.
        import wave

        import numpy as np

        with wave.open(args.selftest) as w:
            sr = w.getframerate()
            audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        Path(args.selftest + ".txt").write_text(engine.transcribe(audio, sr), encoding="utf-8")
        sys.exit(0)

    if args.doctor:
        from . import install

        sys.exit(install.run_doctor())

    if args.autostart or args.no_autostart or args.desktop_icon or args.no_desktop_icon:
        from . import install

        if args.no_autostart:
            removed = install.remove_autostart()
            print("Removed autostart shortcut." if removed else "No autostart shortcut was present.")
        if args.autostart:
            path = install.install_autostart()
            print(f"Autostart shortcut created: {path}")
        if args.no_desktop_icon:
            removed = install.remove_desktop_icon()
            print("Removed Desktop shortcut." if removed else "No Desktop shortcut was present.")
        if args.desktop_icon:
            path = install.install_desktop_icon()
            print(f"Desktop shortcut created: {path}")
        sys.exit(0)

    _hand_off_if_packaged()
    _refuse_to_start_if_wispr_running()
    _refuse_to_start_if_already_running()
    app = DictateApp()
    app.run()


if __name__ == "__main__":
    main()
