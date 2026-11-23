"""Always-on-top floating meter, bottom-centre of the primary screen.

While recording it is a little Winamp-style spectrum analyser: a black LCD
panel with a row of bars, one per frequency band of your voice, built from
lit blocks -- green at the bottom, yellow, then red at the top -- with a peak
cap above each bar that hangs for a moment and then drops. In silence every
bar rests on the floor as one lit block. While the clip is being processed
(STT + cleanup) the bars turn grey and run a slow wave.

The meter is a native per-pixel-alpha layered window, not a Tk widget: Tk's
canvas has no anti-aliasing and, in a DPI-unaware process, Windows bitmap-
stretches it, which blurs everything. Here every frame is drawn with Pillow
at the monitor's real pixel density (blocks snapped to whole pixels, the
panel supersampled 4x) and pushed with UpdateLayeredWindow. The window is
created in a per-monitor-DPI-aware thread context only, so the rest of the
app keeps its existing DPI behaviour.

Tk still owns the main thread: its mainloop pumps this window's messages and
drives the animation via `after()`. Hotkey callbacks arrive on the pynput
listener thread, so every public method marshals onto the Tk thread.
"""

from __future__ import annotations

import ctypes
import math
import time
import tkinter as tk
from collections import deque
from contextlib import contextmanager
from ctypes import wintypes as wt

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

# -- look (logical pixels, scaled by the monitor DPI) --------------------------
PANEL_W = 84
PANEL_H = 30
RADIUS = 7
PAD = 8  # room around the panel for its soft shadow
MARGIN_BOTTOM = 18  # above the taskbar
PANEL_FILL = (7, 8, 9, 250)
PANEL_EDGE = (255, 255, 255, 38)
SHADOW = (0, 0, 0, 120)
SS = 4  # supersampling for the panel art

N_BARS = 19
BAR_W = 3.0
BAR_PITCH = 4.0  # bar + gap
SEGMENTS = 10
SEG_H = 1.6
SEG_GAP = 0.6
# block colours, bottom to top -- the classic Winamp ramp
SEG_COLORS = (
    (34, 160, 40), (46, 182, 44), (64, 198, 48), (92, 210, 50), (128, 220, 52),
    (170, 226, 54), (214, 222, 50), (240, 186, 40), (246, 128, 32), (238, 58, 30),
)
SEG_UNLIT = (255, 255, 255, 12)  # faint LCD ghost of the unlit blocks
PEAK_COLOR = (205, 208, 216)
BUSY_COLOR = (120, 122, 130)

# -- motion --------------------------------------------------------------------
STEP_S = 0.02  # one audio slice per bar update
FALL_PER_S = 2.2  # bars drop at a constant rate, like the original
PEAK_HOLD_S = 0.35
PEAK_GRAVITY = 4.0  # full heights per s^2 once the hold ends

# -- analysis: 20 ms slices -> N_BARS log-spaced bands on a dB scale -----------
BAND_LO_HZ = 120
BAND_HI_HZ = 7000
SAMPLE_RATE = 16_000
DB_FLOOR = -70.0
DB_TOP = -12.0
TILT_DB_PER_OCT = 3.0  # lift the highs so consonants show, as Winamp did

FRAME_MS = 15
FADE_IN_S = 0.14
FADE_OUT_S = 0.12

# -- Win32 ---------------------------------------------------------------------
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32")
kernel32 = ctypes.WinDLL("kernel32")
winmm = ctypes.WinDLL("winmm")

LRESULT = ctypes.c_ssize_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TRANSPARENT = 0x00000020
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000
WS_EX_NOACTIVATE = 0x08000000
SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = wt.HWND(-1)
SWP_NOSIZE, SWP_NOMOVE, SWP_NOACTIVATE = 0x1, 0x2, 0x10
WM_MOUSEACTIVATE = 0x0021
MA_NOACTIVATE = 3
ULW_ALPHA = 2
SPI_GETWORKAREA = 0x30
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wt.UINT), ("style", wt.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wt.HINSTANCE), ("hIcon", wt.HICON), ("hCursor", wt.HANDLE),
        ("hbrBackground", wt.HBRUSH), ("lpszMenuName", wt.LPCWSTR),
        ("lpszClassName", wt.LPCWSTR), ("hIconSm", wt.HICON),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wt.DWORD), ("biWidth", wt.LONG), ("biHeight", wt.LONG),
        ("biPlanes", wt.WORD), ("biBitCount", wt.WORD), ("biCompression", wt.DWORD),
        ("biSizeImage", wt.DWORD), ("biXPelsPerMeter", wt.LONG),
        ("biYPelsPerMeter", wt.LONG), ("biClrUsed", wt.DWORD), ("biClrImportant", wt.DWORD),
    ]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_ubyte), ("BlendFlags", ctypes.c_ubyte),
        ("SourceConstantAlpha", ctypes.c_ubyte), ("AlphaFormat", ctypes.c_ubyte),
    ]


user32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.DefWindowProcW.restype = LRESULT
user32.CreateWindowExW.argtypes = [
    wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int, ctypes.c_int,
    ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
]
user32.CreateWindowExW.restype = wt.HWND
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.UpdateLayeredWindow.argtypes = [
    wt.HWND, wt.HDC, ctypes.POINTER(wt.POINT), ctypes.POINTER(wt.SIZE), wt.HDC,
    ctypes.POINTER(wt.POINT), wt.COLORREF, ctypes.POINTER(BLENDFUNCTION), wt.DWORD,
]
user32.SetWindowPos.argtypes = [
    wt.HWND, wt.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.UINT,
]
user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
user32.GetDpiForWindow.argtypes = [wt.HWND]
user32.GetWindowRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
user32.SystemParametersInfoW.argtypes = [wt.UINT, wt.UINT, wt.LPVOID, wt.UINT]
user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_ssize_t]
user32.SetThreadDpiAwarenessContext.restype = ctypes.c_ssize_t
gdi32.CreateCompatibleDC.argtypes = [wt.HDC]
gdi32.CreateCompatibleDC.restype = wt.HDC
gdi32.CreateDIBSection.argtypes = [
    wt.HDC, ctypes.POINTER(BITMAPINFOHEADER), wt.UINT,
    ctypes.POINTER(ctypes.c_void_p), wt.HANDLE, wt.DWORD,
]
gdi32.CreateDIBSection.restype = wt.HBITMAP
gdi32.SelectObject.argtypes = [wt.HDC, wt.HGDIOBJ]
gdi32.SelectObject.restype = wt.HGDIOBJ
gdi32.DeleteObject.argtypes = [wt.HGDIOBJ]
kernel32.GetModuleHandleW.restype = wt.HMODULE


def make_process_dpi_aware():
    """System-DPI-aware for the whole process, before Tk starts. Tk then sees
    the real DPI (192 at 200%) and draws its own windows -- the ask box, the
    key dialogs -- at full resolution instead of being bitmap-stretched by
    Windows, which made their text blurry. The pill and tray menu set
    per-monitor awareness per thread on top of this, as before."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
    except Exception:
        try:
            user32.SetProcessDPIAware()
        except Exception:
            pass


@contextmanager
def _dpi_aware():
    """Per-monitor-aware for this thread only, for the duration of the block."""
    try:
        old = user32.SetThreadDpiAwarenessContext(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
    except AttributeError:  # pre-1607 Windows: stay as we are
        old = 0
    try:
        yield
    finally:
        if old:
            user32.SetThreadDpiAwarenessContext(old)


_band_cache: dict[int, tuple[np.ndarray, list[tuple[int, int]], np.ndarray]] = {}


def _band_plan(n: int):
    """Window, bin ranges and per-band tilt for an n-sample slice (cached)."""
    if n not in _band_cache:
        hz_per_bin = SAMPLE_RATE / n
        edges = np.geomspace(BAND_LO_HZ, BAND_HI_HZ, N_BARS + 1) / hz_per_bin
        bins, lo = [], int(round(edges[0]))
        for e in edges[1:]:
            hi = max(lo + 1, int(round(e)))
            bins.append((lo, hi))
            lo = hi
        centres = np.array([(a + b) / 2 * hz_per_bin for a, b in bins])
        tilt = TILT_DB_PER_OCT * np.log2(centres / 500.0)
        _band_cache[n] = (np.hanning(n).astype(np.float32), bins, tilt)
    return _band_cache[n]


def _bands(x: np.ndarray) -> list[float]:
    """One audio slice -> N_BARS heights in 0..1."""
    win, bins, tilt = _band_plan(len(x))
    # a full-scale sine through a Hann window peaks at n/4
    spec = np.abs(np.fft.rfft(x * win)) / (len(x) / 4)
    mags = np.array([spec[a:b].max() for a, b in bins])
    db = 20 * np.log10(mags + 1e-9) + tilt
    return np.clip((db - DB_FLOOR) / (DB_TOP - DB_FLOOR), 0.0, 1.0).tolist()


def _premultiplied_bgra(img: Image.Image) -> bytes:
    a = np.asarray(img, dtype=np.uint16)
    alpha = a[..., 3:4]
    rgb = (a[..., :3] * alpha + 127) // 255
    return np.concatenate([rgb[..., ::-1], alpha], axis=2).astype(np.uint8).tobytes()


class Pill:
    """A small always-on-top spectrum meter for the dictation tray app.

    Create it on the main thread and call `run()` there (it drives the Tk
    mainloop). All other methods are safe to call from any thread -- they
    marshal onto the Tk thread via `after(0, ...)`, except `push_audio`, which
    only appends to a deque that the animation loop drains.
    """

    def __init__(self):
        make_process_dpi_aware()
        self.root = tk.Tk()
        self.root.withdraw()

        self._wndproc = WNDPROC(self._proc)  # keep a reference or it gets GC'd
        hinst = kernel32.GetModuleHandleW(None)
        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc
        wc.hInstance = hinst
        wc.lpszClassName = f"DictatePill{id(self)}"
        user32.RegisterClassExW(ctypes.byref(wc))
        with _dpi_aware():
            self.hwnd = user32.CreateWindowExW(
                WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW
                | WS_EX_NOACTIVATE | WS_EX_TRANSPARENT,
                wc.lpszClassName, "dictate", WS_POPUP,
                0, 0, 1, 1, None, None, hinst, None,
            )

        self._scale = 0.0
        self._size = (0, 0)
        self._pos = (0, 0)
        self._grid: dict | None = None  # whole-pixel geometry of the blocks
        self._mem_dc = gdi32.CreateCompatibleDC(None)
        self._bitmap = None
        self._bits = ctypes.c_void_p()
        self._backdrop: Image.Image | None = None

        self._mode = "hidden"  # "recording" | "processing" | "hidden"
        self._pending: deque[np.ndarray] = deque()  # appended by the poll thread
        self._acc = 0.0  # time into the current 20 ms step
        self._primed = False
        self._heights = [0.0] * N_BARS
        self._peaks = [0.0] * N_BARS
        self._peak_hold = [0.0] * N_BARS
        self._peak_vel = [0.0] * N_BARS
        self._busy = 0.0  # 0 = Winamp colours, 1 = grey
        self._t0 = self._last = time.monotonic()
        self._shown_at = 0.0
        self._hiding_at: float | None = None
        self._tick_id: str | None = None
        self._visible = False
        self._timer_res = False

    def _proc(self, hwnd, msg, wparam, lparam):
        if msg == WM_MOUSEACTIVATE:
            return MA_NOACTIVATE
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # -- geometry / static art -----------------------------------------------
    def _layout(self):
        """Size the window for the current DPI and park it bottom-centre."""
        with _dpi_aware():
            scale = user32.GetDpiForWindow(self.hwnd) / 96.0 or 1.0
            work = wt.RECT()
            user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(work), 0)
        w = round((PANEL_W + 2 * PAD) * scale)
        h = round((PANEL_H + 2 * PAD) * scale)
        x = work.left + (work.right - work.left - w) // 2
        y = work.bottom - h - round((MARGIN_BOTTOM - PAD) * scale)
        self._pos = (x, y)
        if scale != self._scale or (w, h) != self._size:
            self._scale, self._size = scale, (w, h)
            self._grid = self._make_grid()
            self._backdrop = self._render_backdrop()
            self._alloc_bitmap()

    def _make_grid(self) -> dict:
        """Block geometry in whole device pixels, so every block is crisp."""
        s = self._scale
        bw = max(2, round(BAR_W * s))
        pitch = max(bw + 1, round(BAR_PITCH * s))
        seg = max(2, round(SEG_H * s))
        gap = max(1, round(SEG_GAP * s))
        total_w = N_BARS * pitch - (pitch - bw)
        total_h = SEGMENTS * seg + (SEGMENTS - 1) * gap
        x0 = (self._size[0] - total_w) // 2
        y_floor = (self._size[1] + total_h) // 2  # bottom edge of the lowest block
        return dict(bw=bw, pitch=pitch, seg=seg, gap=gap, x0=x0, y_floor=y_floor)

    def _seg_box(self, bar: int, row: int) -> tuple[int, int, int, int]:
        g = self._grid
        x = g["x0"] + bar * g["pitch"]
        y2 = g["y_floor"] - row * (g["seg"] + g["gap"])
        return (x, y2 - g["seg"], x + g["bw"] - 1, y2 - 1)

    def _render_backdrop(self) -> Image.Image:
        s = self._scale * SS
        w, h = self._size[0] * SS, self._size[1] * SS
        box = (PAD * s, PAD * s, (PAD + PANEL_W) * s, (PAD + PANEL_H) * s)
        r = RADIUS * s

        shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        off = 1.5 * s
        ImageDraw.Draw(shadow).rounded_rectangle(
            (box[0], box[1] + off, box[2], box[3] + off), r, fill=SHADOW
        )
        shadow = shadow.filter(ImageFilter.GaussianBlur(4 * s))

        panel = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(panel)
        d.rounded_rectangle(box, r, fill=PANEL_EDGE)
        e = 1.0 * s  # hairline edge
        d.rounded_rectangle((box[0] + e, box[1] + e, box[2] - e, box[3] - e), r - e, fill=PANEL_FILL)
        art = Image.alpha_composite(shadow, panel).resize(self._size, Image.LANCZOS)

        ghost = Image.new("RGBA", self._size, (0, 0, 0, 0))
        gd = ImageDraw.Draw(ghost)
        for bar in range(N_BARS):
            for row in range(SEGMENTS):
                gd.rectangle(self._seg_box(bar, row), fill=SEG_UNLIT)
        return Image.alpha_composite(art, ghost)

    def _alloc_bitmap(self):
        if self._bitmap:
            gdi32.DeleteObject(self._bitmap)
        bih = BITMAPINFOHEADER()
        bih.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bih.biWidth, bih.biHeight = self._size[0], -self._size[1]  # top-down
        bih.biPlanes, bih.biBitCount = 1, 32
        self._bitmap = gdi32.CreateDIBSection(
            self._mem_dc, ctypes.byref(bih), 0, ctypes.byref(self._bits), None, 0
        )
        gdi32.SelectObject(self._mem_dc, self._bitmap)

    # -- public, thread-safe API ----------------------------------------------
    def show_recording(self):
        self.root.after(0, self._show_recording)

    def push_audio(self, slices: list[np.ndarray]):
        """Consecutive 20 ms mono audio slices, oldest first."""
        self._pending.extend(slices)

    def show_processing(self):
        self.root.after(0, self._show_processing)

    def hide(self):
        self.root.after(0, self._hide)

    def run(self):
        """Blocking -- drives the Tk mainloop. Call once, from main thread."""
        self.root.mainloop()

    def shutdown(self):
        self.root.after(0, self._shutdown)

    # -- Tk-thread implementations ---------------------------------------------
    def _show_recording(self):
        self._mode = "recording"
        self._pending.clear()
        self._acc = 0.0
        self._primed = False
        self._busy = 0.0
        self._show()

    def _show_processing(self):
        self._mode = "processing"
        self._show()

    def _show(self):
        self._hiding_at = None
        if not self._visible:
            self._layout()
            self._heights = [0.0] * N_BARS
            self._peaks = [0.0] * N_BARS
            self._busy = 1.0 if self._mode == "processing" else 0.0
            self._shown_at = time.monotonic()
            self._draw(0)
            with _dpi_aware():
                user32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
                user32.SetWindowPos(
                    self.hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                    SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE,
                )
            self._visible = True
            if not self._timer_res:  # 1 ms timer so frames land evenly
                winmm.timeBeginPeriod(1)
                self._timer_res = True
        if self._tick_id is None:
            self._last = time.monotonic()
            self._tick()

    def _hide(self):
        if self._visible and self._hiding_at is None:
            self._hiding_at = time.monotonic()

    def _hide_now(self):
        self._mode = "hidden"
        self._hiding_at = None
        if self._tick_id is not None:
            self.root.after_cancel(self._tick_id)
            self._tick_id = None
        if self._visible:
            user32.ShowWindow(self.hwnd, SW_HIDE)
            self._visible = False
        if self._timer_res:
            winmm.timeEndPeriod(1)
            self._timer_res = False

    def _shutdown(self):
        self._hide_now()
        user32.DestroyWindow(self.hwnd)
        self.root.quit()

    def _next_bands(self, dt: float) -> list[float] | None:
        """Analyse one 20 ms slice per 20 ms of wall time. Audio arrives in
        100 ms bursts of five slices, so keep a small buffer and hold (rather
        than invent data) when it runs dry. None = nothing new this frame."""
        pending = self._pending
        if not self._primed:
            if len(pending) < 5:
                return None
            self._primed = True
        while len(pending) > 12:  # fell behind (e.g. a stalled frame): skip ahead
            pending.popleft()
        self._acc += dt
        bands = None
        while self._acc >= STEP_S:
            if not pending:
                self._acc = 0.0
                self._primed = False
                break
            bands = _bands(pending.popleft())
            self._acc -= STEP_S
        return bands

    def _tick(self):
        self._tick_id = None
        if not self._visible:
            return
        now = time.monotonic()
        dt = min(0.05, now - self._last)
        self._last = now
        t = now - self._t0

        if self._mode == "recording":
            goals = self._next_bands(dt)
        else:  # processing: a slow grey wave travelling left to right
            goals = [
                0.08 + 0.32 * max(0.0, math.sin(t * 4.0 - i * 0.55)) ** 2
                for i in range(N_BARS)
            ]
            self._busy += (1.0 - self._busy) * (1 - math.exp(-dt / 0.15))

        # Winamp physics: bars jump up instantly and sink at a constant rate;
        # each peak cap holds for a moment, then falls with gravity
        for i in range(N_BARS):
            h = self._heights[i] - FALL_PER_S * dt
            if goals is not None and goals[i] > h:
                h = goals[i]
            self._heights[i] = h = max(0.0, h)
            if h >= self._peaks[i]:
                self._peaks[i], self._peak_hold[i], self._peak_vel[i] = h, PEAK_HOLD_S, 0.0
            elif self._peak_hold[i] > 0:
                self._peak_hold[i] -= dt
            else:
                self._peak_vel[i] += PEAK_GRAVITY * dt
                self._peaks[i] = max(h, self._peaks[i] - self._peak_vel[i] * dt)

        if self._hiding_at is not None:
            fade = 1 - (now - self._hiding_at) / FADE_OUT_S
            if fade <= 0:
                self._hide_now()
                return
        else:
            fade = min(1.0, (now - self._shown_at) / FADE_IN_S)
        self._draw(fade)
        self._tick_id = self.root.after(FRAME_MS, self._tick)

    def _draw(self, fade: float):
        frame = self._backdrop.copy()
        d = ImageDraw.Draw(frame)
        busy = self._busy
        colors = [
            tuple(round(c + (b - c) * busy) for c, b in zip(col, BUSY_COLOR)) + (255,)
            for col in SEG_COLORS
        ]
        peak_col = PEAK_COLOR + (round(255 * (1 - 0.5 * busy)),)
        for bar in range(N_BARS):
            lit = max(1, math.ceil(self._heights[bar] * SEGMENTS - 0.15))  # floor block always on
            for row in range(min(lit, SEGMENTS)):
                d.rectangle(self._seg_box(bar, row), fill=colors[row])
            peak_row = math.ceil(self._peaks[bar] * SEGMENTS - 0.15)
            if lit <= peak_row - 1 < SEGMENTS and peak_row >= 2:
                x1, y1, x2, y2 = self._seg_box(bar, peak_row - 1)
                cap = max(1, (y2 - y1 + 1) // 2)  # a thin cap, half a block tall
                d.rectangle((x1, y2 - cap + 1, x2, y2), fill=peak_col)

        data = _premultiplied_bgra(frame)
        ctypes.memmove(self._bits, data, len(data))
        blend = BLENDFUNCTION(0, 0, max(0, min(255, round(255 * fade))), 1)
        with _dpi_aware():
            user32.UpdateLayeredWindow(
                self.hwnd, None, ctypes.byref(wt.POINT(*self._pos)),
                ctypes.byref(wt.SIZE(*self._size)), self._mem_dc,
                ctypes.byref(wt.POINT(0, 0)), 0, ctypes.byref(blend), ULW_ALPHA,
            )

    def bbox(self):
        """Screen-space (x, y, x2, y2) of the meter window, for screenshotting."""
        r = wt.RECT()
        user32.GetWindowRect(self.hwnd, ctypes.byref(r))
        return (r.left, r.top, r.right, r.bottom)


def _voice(seconds: float, rms: float, pitch: float = 140.0) -> np.ndarray:
    """Synthetic vowel-ish audio for the demo: harmonics shaped by two formants."""
    t = np.arange(int(seconds * SAMPLE_RATE)) / SAMPLE_RATE
    x = np.zeros_like(t)
    for k in range(1, 60):
        f = pitch * k
        if f > 7500:
            break
        formant = math.exp(-((f - 700) / 300) ** 2) + 0.5 * math.exp(-((f - 2200) / 500) ** 2)
        x += (0.05 + formant) / k**0.5 * np.sin(2 * np.pi * f * t + k)
    x *= rms / (np.sqrt(np.mean(x**2)) + 1e-12)
    return x.astype(np.float32)


if __name__ == "__main__":
    import threading

    pill = Pill()

    def _demo():
        slice_n = int(SAMPLE_RATE * STEP_S)
        rng = np.random.default_rng()
        time.sleep(0.5)
        pill.show_recording()
        # (seconds, rms): silence, normal speech, a pause, louder, a shout...
        script = [(0.3, 0.0), (0.25, 0.03), (0.1, 0.0), (0.3, 0.05), (0.2, 0.0),
                  (0.4, 0.2), (0.3, 0.0), (0.5, 0.04), (0.4, 0.0)]
        for secs, rms in script:
            if rms:
                audio = _voice(secs, rms, pitch=rng.uniform(110, 190))
            else:
                audio = (rng.standard_normal(int(secs * SAMPLE_RATE)) * 1e-4).astype(np.float32)
            n = len(audio) // (slice_n * 5) * slice_n * 5
            for chunk in audio[:n].reshape(-1, 5, slice_n):  # 100 ms bursts, like capture
                pill.push_audio(list(chunk))
                time.sleep(0.1)
        pill.show_processing()
        time.sleep(2)
        pill.hide()
        time.sleep(0.5)
        pill.shutdown()

    threading.Thread(target=_demo, daemon=True).start()
    pill.run()
