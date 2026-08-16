#!/usr/bin/env python3
"""Build `vpad-splash.png`, the window PyInstaller shows while the app loads.

A tray app has no window, and that is exactly the problem this solves: the
user double-clicks a downloaded .exe and for the two seconds it takes to
unpack Python, zeroconf and Pillow, absolutely nothing happens on screen.
The first field report about this app was that it "doesn't open". Windows
11 then hides the new tray icon behind the overflow arrow, so even once it
is running there is nothing to see.

The splash costs ~7 MB of Tcl/Tk in the bundle. That is real, and it is
paid for the one moment that decides whether the user waits or gives up.

Drawn from the same two sources the .ico uses (`make-icon.py`), so the
splash, the taskbar icon and the tray icon are visibly one mark:

    * the badge artwork, masked to its own corner radius
    * the sampled brand navy, so a retouch of the artwork carries through

    python make-splash.py           # brand/icongamepad.png -> vpad-splash.png
    python make-splash.py src.png out.png
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


def _icon_module():
    """Load `make-icon.py`. Its name has a hyphen, so `import` cannot.

    Loaded rather than copied: `_navy` and `_transparent_corners` are the
    two decisions that keep the splash and the .ico the same mark, and a
    second copy of them would drift the first time the artwork changes.
    """
    path = Path(__file__).resolve().parent / "make-icon.py"
    spec = importlib.util.spec_from_file_location("make_icon", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_icon = _icon_module()
navy, round_corners = _icon._navy, _icon._round_corners

WIDTH, HEIGHT = 480, 244
BADGE = 128
# Bottom strip PyInstaller writes its status text into. Nothing is drawn
# there or the text lands on top of it.
TEXT_BAND = 46


def _font(size: int, bold: bool = False):
    """A real font if the host has one, the bitmap default otherwise.

    No font file is carried in the repo. On Windows the first candidate
    always resolves; a Linux CI box building the Windows artifact is the
    case the fallbacks exist for, and there the splash is still legible,
    just plainer.
    """
    names = (("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf")
             if bold else
             ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"))
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def build(src: Path, dst: Path) -> None:
    art = Image.open(src).convert("RGBA")
    bg = navy(art)

    card = Image.new("RGB", (WIDTH, HEIGHT), bg)
    draw = ImageDraw.Draw(card)

    # A slightly lighter panel behind the text, so the wordmark does not
    # float on a flat field the way a placeholder does.
    draw.rectangle([0, HEIGHT - TEXT_BAND, WIDTH, HEIGHT],
                   fill=tuple(max(0, c - 8) for c in bg))
    draw.rectangle([0, HEIGHT - TEXT_BAND - 2, WIDTH, HEIGHT - TEXT_BAND],
                   fill=(90, 150, 235))

    # `_round_corners`, not the .ico's flood fill: the badge sits on a
    # solid field here, so a clean radius mask beats a threshold that
    # leaves the artwork's darker metallic streaks stranded in the corners.
    badge = round_corners(art).resize((BADGE, BADGE), Image.LANCZOS)
    top = (HEIGHT - TEXT_BAND - BADGE) // 2
    card.paste(badge, (36, top), badge)

    x = 36 + BADGE + 28
    draw.text((x, top + 16), "V-Pad Helper", font=_font(31, bold=True),
              fill=(255, 255, 255))
    draw.text((x, top + 58), "Companion for your phone",
              font=_font(15), fill=(168, 186, 214))
    draw.text((x, top + 84), "Turns V-Pad into a gamepad",
              font=_font(15), fill=(168, 186, 214))

    card.save(dst)
    print(f"{dst}  ({dst.stat().st_size / 1024:.1f} KB, {WIDTH}x{HEIGHT})")


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else here / "brand" / "icongamepad.png"
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else here / "vpad-splash.png"
    build(src, dst)
