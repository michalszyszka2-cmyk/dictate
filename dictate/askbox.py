"""The ask box: a dark floating panel at the top of the screen, like Glaido's.

Hold Right Shift + Right Ctrl, speak, release: the transcript lands in the box
for a Cancel / Send check (or type into it instead). Send runs the question
through headless Claude Code (``ask_claude``) in the chosen folder, with the
chosen model and effort, and shows the short reply. Follow-ups continue the
same session; "Full conversation" opens the markdown copy the brain indexes.

Everything here runs on the Tk thread (the pill's mainloop). ``open()`` is
safe from any thread. Claude runs on a worker thread and reports back via
``after()``.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import threading
import time
import tkinter as tk
from tkinter import ttk

from dictate import ask_claude, config, ui

BG = "#111111"
PANEL = "#1c1c1c"
INK = "#ececec"
DIM = "#8a8a8a"
ACCENT = "#b5f23d"  # Glaido's send-button green
ERR = "#ff7a6b"
WIDTH = 640  # at 100% scaling; _px() scales it to the display
STATE_FILE = os.path.join(config.APPDIR, "askbox.json")

user32 = ctypes.windll.user32


def _plain(text: str) -> str:
    """The box shows plain text: drop markdown emphasis and code ticks."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    return text


class AskBox:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.win: tk.Toplevel | None = None
        self.session_id: str | None = None
        self.first_session_id: str | None = None
        self.first_prompt = ""
        self.chat_file: str | None = None
        self.busy = False
        self._t_sent = 0.0
        self._state = self._load_state()

    # -- public, any thread -------------------------------------------------------
    def open(self, text: str = ""):
        self.root.after(0, lambda: self._open(text))

    def listening(self):
        """Shown while the question is being transcribed."""
        self.root.after(0, lambda: self._open("", status="Transcribing..."))

    # -- state -----------------------------------------------------------------------
    def _load_state(self) -> dict:
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self):
        self._state = {"folder": self.folder.get(), "model": self.model.get(), "effort": self.effort.get()}
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(self._state, f)
        except Exception:
            pass

    # -- build -------------------------------------------------------------------------
    def _px(self, n: float) -> int:
        return int(round(n * self.scale))

    def _build(self):
        # The process is system-DPI-aware (ui.make_process_dpi_aware), so Tk
        # draws at real resolution and every pixel size is scaled here by hand.
        # (Per-monitor awareness for this one window only put Tk's geometry and
        # its fonts on different scales: box drawn double size.)
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=BG, highlightthickness=1, highlightbackground="#2e2e2e")
        self.scale = max(1.0, self.root.winfo_fpixels("1i") / 96)
        px = self._px
        font = ("Segoe UI", -px(14))
        small = ("Segoe UI", -px(12))
        self.font, self.small = font, small

        style = ttk.Style(win)
        style.theme_use("clam")
        style.configure("Ask.TCombobox", fieldbackground=PANEL, background=PANEL, foreground=INK,
                        arrowcolor=DIM, bordercolor="#333333", lightcolor=PANEL, darkcolor=PANEL,
                        padding=px(3), arrowsize=px(12))
        style.map("Ask.TCombobox", fieldbackground=[("readonly", PANEL)], foreground=[("readonly", INK)],
                  selectbackground=[("readonly", PANEL)], selectforeground=[("readonly", INK)])
        win.option_add("*TCombobox*Listbox.background", PANEL)
        win.option_add("*TCombobox*Listbox.foreground", INK)
        win.option_add("*TCombobox*Listbox.font", small)

        pad = px(14)
        body = tk.Frame(win, bg=BG, padx=pad, pady=pad)
        body.pack(fill="both", expand=True)

        # the question (editable: the typing fallback)
        self.input = tk.Text(body, height=3, wrap="word", bg=PANEL, fg=INK, insertbackground=INK,
                             relief="flat", font=font, padx=px(10), pady=px(8),
                             highlightthickness=1, highlightbackground="#2e2e2e", highlightcolor="#444444")
        self.input.pack(fill="x")
        self.input.bind("<Return>", self._on_enter)
        self.input.bind("<Escape>", lambda e: self.close())

        # folder / model / effort + Cancel / Send
        row = tk.Frame(body, bg=BG, pady=px(10))
        row.pack(fill="x")
        folders = ask_claude.folders()
        self._folder_paths = {self._folder_label(p): p for p in folders}
        self.folder = tk.StringVar(value=self._folder_label(self._state.get("folder") or folders[0]))
        self.model = tk.StringVar(value=self._state.get("model") or "opus")
        self.effort = tk.StringVar(value=self._state.get("effort") or "medium")
        for var, values, width in ((self.folder, list(self._folder_paths), 20),
                                   (self.model, list(ask_claude.MODELS), 8),
                                   (self.effort, list(ask_claude.EFFORTS), 8)):
            cb = ttk.Combobox(row, textvariable=var, values=values, width=width, state="readonly",
                              style="Ask.TCombobox", font=small)
            cb.pack(side="left", padx=(0, px(6)))
            cb.bind("<<ComboboxSelected>>", lambda e: (self._save_state(), self.input.focus_set()))

        self.send_btn = self._button(row, "Send", self._send, primary=True)
        self.send_btn.pack(side="right")
        self._button(row, "Cancel", self.close).pack(side="right", padx=(0, px(6)))

        # the answer
        self.answer = tk.Text(body, height=1, wrap="word", bg=BG, fg=INK, relief="flat", font=font,
                              padx=0, pady=px(2), highlightthickness=0, cursor="arrow")
        self.answer.configure(state="disabled")

        foot = tk.Frame(body, bg=BG)
        foot.pack(fill="x", side="bottom")
        self.status = tk.Label(foot, text="", bg=BG, fg=DIM, font=small, anchor="w")
        self.status.pack(side="left")
        self.full_btn = self._button(foot, "Full conversation", self._open_chat)
        self.new_btn = self._button(foot, "New question", self._new)
        self.hint = tk.Label(foot, text="Enter send · Shift+Enter new line · Esc close", bg=BG, fg=DIM, font=small)
        self.hint.pack(side="right")

        # drag anywhere on the frame to move it
        for w in (body, row, foot):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag_move)
        win.bind("<Escape>", lambda e: self.close())
        self.win = win

    def _button(self, parent, text, cmd, primary=False):
        px = self._px
        return tk.Button(parent, text=text, command=cmd, font=self.small, relief="flat", bd=0, cursor="hand2",
                         padx=px(12), pady=px(4),
                         bg=ACCENT if primary else PANEL, fg="#0b0b0b" if primary else INK,
                         activebackground="#c9ff5a" if primary else "#2a2a2a",
                         activeforeground="#0b0b0b" if primary else INK)

    @staticmethod
    def _folder_label(path: str) -> str:
        return f"{ask_claude.ROOT.name or ask_claude.ROOT} (root)" if os.path.normcase(path) == os.path.normcase(str(ask_claude.ROOT)) \
            else os.path.basename(path)

    # -- show / hide ----------------------------------------------------------------
    def _open(self, text: str, status: str = ""):
        if self.win is None:
            self._build()
        if self.busy:
            self._raise()
            return
        if text or status:
            self.input.delete("1.0", "end")
            self.input.insert("1.0", text)
        self.status.configure(text=status, fg=DIM)
        self._place()
        self._raise()

    def _place(self):
        self.win.update_idletasks()
        w = self._px(WIDTH)
        sw = self.win.winfo_screenwidth()
        if not getattr(self, "_moved", False):
            self.win.geometry(f"{w}x{self.win.winfo_reqheight()}+{(sw - w) // 2}+{self._px(14)}")
        else:
            self.win.geometry(f"{w}x{self.win.winfo_reqheight()}")

    def _raise(self):
        self.win.deiconify()
        self.win.lift()
        self.win.attributes("-topmost", True)
        self.win.update_idletasks()
        try:  # Windows only gives focus to the app that had the last input; an Alt tap lifts that lock
            hwnd = int(self.win.wm_frame(), 16)
            user32.keybd_event(0x12, 0, 0, 0)
            user32.keybd_event(0x12, 0, 2, 0)
            user32.SetForegroundWindow(hwnd)
        except Exception:
            pass
        self.input.focus_force()
        self.input.mark_set("insert", "end")

    def close(self):
        if self.win is not None:
            self.win.withdraw()

    # -- send ---------------------------------------------------------------------------
    def _on_enter(self, event):
        if event.state & 0x0001:  # Shift: new line
            return None
        self._send()
        return "break"

    def _send(self):
        prompt = self.input.get("1.0", "end").strip()
        if not prompt or self.busy:
            return
        self._save_state()
        self.busy = True
        if self.session_id is None:
            self.first_prompt = prompt
        self.send_btn.configure(state="disabled")
        self.input.configure(state="disabled")
        self._t_sent = time.monotonic()
        self._show_answer("")
        self._tick()
        args = (prompt, self._folder_paths[self.folder.get()], self.model.get(), self.effort.get(), self.session_id)
        threading.Thread(target=self._worker, args=args, daemon=True).start()

    def _tick(self):
        if not self.busy:
            return
        self.status.configure(text=f"Claude is thinking... {time.monotonic() - self._t_sent:.0f}s", fg=DIM)
        self.root.after(500, self._tick)

    def _worker(self, prompt, cwd, model, effort, session_id):
        try:
            r = ask_claude.ask(prompt, cwd, model, effort, session_id)
        except Exception as exc:
            r = {"text": f"Couldn't run Claude Code: {exc}", "session_id": session_id, "is_error": True, "seconds": 0}
        path = None
        if r.get("session_id"):
            try:
                first = self.first_session_id or r["session_id"]
                path = ask_claude.save_conversation(first, r["session_id"], self.first_prompt, cwd, model)
            except Exception:
                path = None
        self.root.after(0, lambda: self._done(r, path))

    def _done(self, r: dict, path):
        self.busy = False
        if r.get("session_id"):
            self.session_id = r["session_id"]
            self.first_session_id = self.first_session_id or r["session_id"]
        if path:
            self.chat_file = str(path)
        self.input.configure(state="normal")
        self.input.delete("1.0", "end")
        self.send_btn.configure(state="normal")
        self._show_answer(_plain(r["text"]), error=r.get("is_error"))
        took = f"{r.get('seconds') or (time.monotonic() - self._t_sent):.0f}s"
        self.status.configure(text=("Failed · " if r.get("is_error") else "") + f"{took} · type a follow-up",
                              fg=ERR if r.get("is_error") else DIM)
        self.hint.pack_forget()
        self.new_btn.pack(side="right")
        if self.chat_file:
            self.full_btn.pack(side="right", padx=(0, self._px(6)))
        self._place()
        self.input.focus_force()

    def _show_answer(self, text: str, error: bool = False):
        a = self.answer
        a.configure(state="normal")
        a.delete("1.0", "end")
        a.insert("1.0", text)
        a.configure(fg=ERR if error else INK, state="disabled")
        if not text:
            a.pack_forget()
            self._place()
            return
        a.pack(fill="x", after=self.input.master.winfo_children()[1], pady=(0, self._px(8)))
        a.update_idletasks()
        lines = a.count("1.0", "end", "displaylines")[0]
        a.configure(height=min(max(lines, 1), 12))

    def _new(self):
        self.session_id = self.first_session_id = self.chat_file = None
        self.first_prompt = ""
        self._show_answer("")
        self.new_btn.pack_forget()
        self.full_btn.pack_forget()
        self.hint.pack(side="right")
        self.status.configure(text="")
        self.input.delete("1.0", "end")
        self._place()
        self.input.focus_force()

    def _open_chat(self):
        if self.chat_file and os.path.exists(self.chat_file):
            os.startfile(self.chat_file)

    # -- drag ---------------------------------------------------------------------------
    def _drag_start(self, e):
        self._drag = (e.x_root - self.win.winfo_x(), e.y_root - self.win.winfo_y())

    def _drag_move(self, e):
        dx, dy = self._drag
        self._moved = True
        self.win.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")


if __name__ == "__main__":  # python -m dictate.askbox  -> the box alone, for a look
    ui.make_process_dpi_aware()
    root = tk.Tk()
    root.withdraw()
    box = AskBox(root)
    box.open("")
    root.mainloop()
