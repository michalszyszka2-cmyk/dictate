"""Generate ``dictate.ico`` -- the shortcut icon for the desktop and startup links.

A microphone in the recording pill's own palette (dark #1e1e1e tile, green
#4caf50 mic) so the shortcut, the tray and the pill read as one thing. Drawn at
4x and downsampled per size, which keeps the 16px stand from disappearing.

Run with ``python assets/make_icon.py``; writes ``assets/dictate.ico``.
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

BG = "#1e1e1e"
MIC = "#4caf50"

SIZES = [16, 24, 32, 48, 64, 128, 256]
SS = 4  # supersample factor


def draw(size: int) -> Image.Image:
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # rounded tile
    d.rounded_rectangle([0, 0, s - 1, s - 1], radius=int(s * 0.22), fill=BG)

    # microphone capsule
    cx = s / 2
    cap_w = s * 0.20
    cap_top = s * 0.15
    cap_bot = s * 0.53
    d.rounded_rectangle(
        [cx - cap_w / 2, cap_top, cx + cap_w / 2, cap_bot],
        radius=cap_w / 2,
        fill=MIC,
    )

    # cradle arc -- wraps below the capsule, with clear air either side of it
    stem_w = s * 0.06
    arc_w = s * 0.46
    arc_top = s * 0.38
    arc_bot = s * 0.64
    d.arc(
        [cx - arc_w / 2, arc_top, cx + arc_w / 2, arc_bot],
        start=0,
        end=180,
        fill=MIC,
        width=int(round(stem_w)),
    )

    # stem + base
    d.rectangle([cx - stem_w / 2, arc_bot - stem_w, cx + stem_w / 2, s * 0.79], fill=MIC)
    base_w = s * 0.30
    d.rounded_rectangle(
        [cx - base_w / 2, s * 0.79, cx + base_w / 2, s * 0.79 + stem_w],
        radius=stem_w / 2,
        fill=MIC,
    )

    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    out = Path(__file__).resolve().parent / "dictate.ico"
    frames = [draw(n) for n in SIZES]
    frames[-1].save(out, format="ICO", sizes=[(n, n) for n in SIZES], append_images=frames[:-1])
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
