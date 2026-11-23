"""Tray > Settings...: pick the hotkeys and who does the clean-up step.

One small dark window in the ask box's style. Save writes config.yaml's
``hotkeys:`` block and ``cleanup.provider`` (config.py), puts a typed key in
secrets.env, and calls ``on_saved`` so the app re-arms the keyboard hook at
once. Runs on the Tk thread (the pill's mainloop); ``open()`` is safe from any
thread.
"""

from __future__ import annotations

import os
import tkinter as tk
from tkinter import ttk

from dictate import cleanup, config, hotkey, paths, providers

BG = "#111111"
PANEL = "#1c1c1c"
INK = "#ececec"
DIM = "#8a8a8a"
ACCENT = "#b5f23d"
OK = "#8fd16a"
ERR = "#ff7a6b"

PROVIDER_LABELS = {
    "off": "Off - paste exactly what was heard",
    "openrouter": "OpenRouter key (has free models)",
    "anthropic": "Claude API key",
    "openai": "OpenAI (ChatGPT) API key",
    "claude-app": "My Claude plan, via Claude Code (~10 s a clip)",
    "codex-app": "My ChatGPT plan, via Codex (slow, several s)",
}
KEY_HELP = {
    "openrouter": "Get a key at openrouter.ai/keys. Free models work without paying.",
    "anthropic": "Get a key at console.anthropic.com. Pay as you go, a tiny fraction of a penny a clip.",
    "openai": "Get a key at platform.openai.com. Pay as you go, a tiny fraction of a penny a clip.",
}
APP_HELP = {
    "claude-app": ("Claude Code", "claude.com/claude-code"),
    "codex-app": ("Codex", "developers.openai.com/codex"),
}


class SettingsWindow:
    def __init__(self, root: tk.Tk, on_saved):
        self.root = root
        self.on_saved = on_saved
        self.win: tk.Toplevel | None = None

    def open(self):
        self.root.after(0, self._open)

    # -- helpers -------------------------------------------------------------------
    def _px(self, n: float) -> int:
        return int(round(n * self.scale))

    def _combo(self, parent, var, choices: dict, width=34):
        box = ttk.Combobox(parent, values=list(choices.values()), state="readonly",
                           style="Set.TCombobox", font=self.font, width=width)
        box.set(choices[var])
        return box

    @staticmethod
    def _code(box, choices: dict) -> str:
        return next(k for k, v in choices.items() if v == box.get())

    def _label(self, parent, text, dim=False, **kw):
        return tk.Label(parent, text=text, bg=BG, fg=DIM if dim else INK,
                        font=self.small if dim else self.font, anchor="w", justify="left", **kw)

    # -- build ---------------------------------------------------------------------
    def _open(self):
        if self.win is not None and self.win.winfo_exists():
            self.win.deiconify()
            self.win.lift()
            self.win.focus_force()
            return
        win = tk.Toplevel(self.root)
        win.title("dictate - settings")
        win.configure(bg=BG)
        win.attributes("-topmost", True)
        win.resizable(False, False)
        self.scale = max(1.0, self.root.winfo_fpixels("1i") / 96)
        px = self._px
        self.font, self.small = ("Segoe UI", -px(14)), ("Segoe UI", -px(12))
        head = ("Segoe UI Semibold", -px(15))

        style = ttk.Style(win)
        style.theme_use("clam")
        style.configure("Set.TCombobox", fieldbackground=PANEL, background=PANEL, foreground=INK,
                        arrowcolor=DIM, bordercolor="#333333", lightcolor=PANEL, darkcolor=PANEL,
                        padding=px(4), arrowsize=px(12))
        style.map("Set.TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", INK)],
                  selectbackground=[("readonly", PANEL)], selectforeground=[("readonly", INK)])
        win.option_add("*TCombobox*Listbox.background", PANEL)
        win.option_add("*TCombobox*Listbox.foreground", INK)
        win.option_add("*TCombobox*Listbox.font", self.small)

        body = tk.Frame(win, bg=BG, padx=px(20), pady=px(18))
        body.pack(fill="both", expand=True)
        grid = {"sticky": "w", "pady": (0, px(8))}

        hk = config.hotkeys()
        hold = {k: hotkey.LABELS[k] for k in config.HOLD_KEYS}
        ask = {k: hotkey.LABELS[k] for k in config.ASK_KEYS}
        combo = {k: hotkey.LABELS[k] for k in config.COMBOS}

        tk.Label(body, text="Keys", bg=BG, fg=INK, font=head).grid(row=0, column=0, columnspan=2, **grid)
        self._label(body, "Hold to dictate").grid(row=1, column=0, sticky="w", padx=(0, px(16)))
        self.hold = self._combo(body, hk["dictate"], hold)
        self.hold.grid(row=1, column=1, **grid)
        self._label(body, "Also hold it to ask Claude").grid(row=2, column=0, sticky="w", padx=(0, px(16)))
        self.ask = self._combo(body, hk["ask"], ask)
        self.ask.grid(row=2, column=1, **grid)
        self._label(body, "Or hold together").grid(row=3, column=0, sticky="w", padx=(0, px(16)))
        self.combo = self._combo(body, hk["combo"], combo)
        self.combo.grid(row=3, column=1, **grid)
        self.handsfree = tk.BooleanVar(value=hk["handsfree"])
        tk.Checkbutton(body, text="Those two + Space switch hands-free on and off", variable=self.handsfree,
                       bg=BG, fg=INK, selectcolor=PANEL, activebackground=BG, activeforeground=INK,
                       font=self.small, anchor="w").grid(row=4, column=1, **grid)
        self._label(body, "Esc while holding cancels. The ask box needs Claude Code installed.", dim=True
                    ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(0, px(16)))

        tk.Label(body, text="Clean-up (fixes grammar, punctuation and filler words)", bg=BG, fg=INK,
                 font=head).grid(row=6, column=0, columnspan=2, **grid)
        self._label(body, "Done by").grid(row=7, column=0, sticky="w", padx=(0, px(16)))
        self.provider = self._combo(body, config.cleanup_provider(), PROVIDER_LABELS, width=46)
        self.provider.grid(row=7, column=1, **grid)
        self.provider.bind("<<ComboboxSelected>>", lambda e: self._refresh())
        self.key_label = self._label(body, "API key")
        self.key_label.grid(row=8, column=0, sticky="w", padx=(0, px(16)))
        self.key = tk.Entry(body, show="•", bg=PANEL, fg=INK, insertbackground=INK, relief="flat",
                            font=self.font, width=48, highlightthickness=1,
                            highlightbackground="#2e2e2e", highlightcolor="#444444")
        self.key.grid(row=8, column=1, sticky="we", pady=(0, px(6)), ipady=px(4))
        self.help = self._label(body, "", dim=True, wraplength=px(420))
        self.help.grid(row=9, column=1, sticky="w", pady=(0, px(16)))

        foot = tk.Frame(body, bg=BG)
        foot.grid(row=10, column=0, columnspan=2, sticky="e")
        self.status = self._label(foot, "", dim=True)
        self.status.pack(side="left", padx=(0, px(12)))
        for text, cmd, primary in (("Cancel", self._close, False), ("Save", self._save, True)):
            tk.Button(foot, text=text, command=cmd, font=self.small, relief="flat", bd=0, cursor="hand2",
                      padx=px(14), pady=px(5), bg=ACCENT if primary else PANEL,
                      fg="#0b0b0b" if primary else INK,
                      activebackground="#c9ff5a" if primary else "#2a2a2a").pack(side="left", padx=(px(6), 0))

        win.bind("<Escape>", lambda e: self._close())
        win.protocol("WM_DELETE_WINDOW", self._close)
        self.win = win
        self._refresh()
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        win.geometry(f"+{(win.winfo_screenwidth() - w) // 2}+{(win.winfo_screenheight() - h) // 3}")
        win.focus_force()

    def _refresh(self):
        """Show the key box for the API choices, an install check for the app ones."""
        p = self._code(self.provider, PROVIDER_LABELS)
        self.key.delete(0, "end")
        if p in providers.KEY_NAMES:
            has = bool(cleanup._key(providers.KEY_NAMES[p]))
            self.key.configure(state="normal")
            self.key_label.configure(fg=INK)
            self.help.configure(fg=DIM, text=("A key is saved on this PC. Type a new one to replace it.\n"
                                              if has else "No key yet: clips paste as heard until you add one.\n")
                                + KEY_HELP[p])
        else:
            self.key.configure(state="disabled", disabledbackground=BG)
            self.key_label.configure(fg=DIM)
            if p in APP_HELP:
                name, url = APP_HELP[p]
                if providers.app_installed(p):
                    self.help.configure(fg=OK, text=f"{name} is installed. Make sure you are signed in to it.")
                else:
                    self.help.configure(fg=ERR, text=f"{name} was not found on this PC. Install it from {url} "
                                                     f"and sign in. Until then clips paste as heard.")
            else:
                self.help.configure(fg=DIM, text="")

    def _save(self):
        try:
            values = {"dictate": self._code(self.hold, {k: hotkey.LABELS[k] for k in config.HOLD_KEYS}),
                      "ask": self._code(self.ask, {k: hotkey.LABELS[k] for k in config.ASK_KEYS}),
                      "combo": self._code(self.combo, {k: hotkey.LABELS[k] for k in config.COMBOS}),
                      "handsfree": self.handsfree.get()}
            if values["dictate"] == "off" and values["combo"] == "off":
                self.status.configure(text="Pick at least one way to dictate.", fg=ERR)
                return
            if values["ask"] == values["dictate"]:
                values["ask"] = "off"
            p = self._code(self.provider, PROVIDER_LABELS)
            config.set_hotkeys(values)
            config.set_cleanup_provider(p)
            key = self.key.get().strip()
            if key and p in providers.KEY_NAMES:
                paths.write_secret(providers.KEY_NAMES[p], key)
                os.environ[providers.KEY_NAMES[p]] = key
            self.on_saved()
            self._close()
        except Exception as exc:
            self.status.configure(text=f"Couldn't save: {exc}", fg=ERR)

    def _close(self):
        if self.win is not None:
            self.win.destroy()
            self.win = None
