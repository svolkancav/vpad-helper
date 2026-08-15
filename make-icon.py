#!/usr/bin/env python3
"""Build `vpad-helper.ico` from the V-Pad brand artwork.

Windows shows this icon in four places that have nothing in common: the
installer, Explorer, the taskbar, and Add/Remove Programs. The sizes range
from 256 px down to 16 px, and a single scaled-down bitmap does not survive
that range. So the .ico carries THREE drawings:

    >= 64 px   the full badge, wordmark included
    32, 48 px  cropped to the pad + antenna, wordmark dropped
    <= 24 px   a drawn pad silhouette

Each tier was checked by rendering it, not assumed. Scaling the full badge
to 16 px was tried first and produces mush: the phone bezel, the antenna
arcs and the pad's own buttons all collapse into the same grey. The bottom
tier is therefore the same silhouette `vpad_helper.run_tray()` already
draws for the tray, so the two icons the user sees are one mark.

The source badge is a full-bleed rounded square whose corners are painted a
light blue rather than left transparent. Windows needs them transparent, or
every icon sits in a pale box. The corner region is found by flood-filling
inwards from the four corners instead of guessing a corner radius — the
white artwork in the middle is the same brightness as those corners, so a
plain brightness threshold would punch holes through the pad itself.

    python make-icon.py            # ../../icongamepad.png -> vpad-helper.ico
    python make-icon.py src.png out.ico
"""
from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

# Sizes Windows actually asks for. 24 is the small-taskbar case, 128 shows
# up in the Inno Setup wizard header on high-DPI displays.
FULL_SIZES = (256, 128, 64)
COMPACT_SIZES = (48, 32)
GLYPH_SIZES = (24, 16)

# Row where the artwork stops and the wordmark begins. Measured on the
# 512 px source: the pad's lowest white pixel is y=363, the wordmark's
# first is y=405. Cutting mid-gap keeps the pad whole and the word out.
WORDMARK_CUT = 384 / 512

# A pixel is "background" for the flood fill if every channel is at least
# this bright. The badge interior is dark navy (max channel ~128), so the
# gap is wide and the exact value is not delicate.
LIGHT = 190


def _transparent_corners(img: Image.Image) -> Image.Image:
    """Return `img` with the light area outside the rounded badge cleared.

    Flood-fills from all four corners so only pixels *connected to a corner*
    are cleared. The white pad in the middle is enclosed by navy and is
    therefore never reached.
    """
    w, h = img.size
    px = img.load()

    # Binary "is light" bitmap, then flood fill it. Doing the fill on a
    # 1-bit image rather than on the RGBA original keeps the test exact:
    # ImageDraw.floodfill's own threshold compares against the seed colour,
    # which drifts across the corner's gradient.
    mask = Image.new("L", (w, h), 0)
    mpx = mask.load()
    for y in range(h):
        for x in range(w):
            r, g, b = px[x, y][:3]
            if r >= LIGHT and g >= LIGHT and b >= LIGHT:
                mpx[x, y] = 255

    stack = [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)]
    seen = bytearray(w * h)
    outside = []
    while stack:
        x, y = stack.pop()
        if not (0 <= x < w and 0 <= y < h):
            continue
        i = y * w + x
        if seen[i] or mpx[x, y] != 255:
            continue
        seen[i] = 1
        outside.append((x, y))
        stack.extend(((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)))

    out = img.convert("RGBA")
    opx = out.load()
    for x, y in outside:
        opx[x, y] = (0, 0, 0, 0)
    return out


def _round_corners(img: Image.Image, radius_frac: float = 0.18) -> Image.Image:
    """Apply a rounded-rectangle alpha mask.

    Used only on the cropped small-size drawing: cutting a square out of
    the badge's middle leaves four hard 90° corners, which look wrong next
    to every other rounded icon in the tray.
    """
    w, h = img.size
    mask = Image.new("L", (w, h), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, w - 1, h - 1], radius=int(min(w, h) * radius_frac), fill=255
    )
    out = img.convert("RGBA")
    out.putalpha(mask)
    return out


def _navy(img: Image.Image) -> tuple[int, int, int]:
    """Darkest common colour of the badge, used as the glyph background.

    Sampled rather than hard-coded so the small sizes cannot drift away
    from the brand blue if the artwork is ever retouched. The badge has a
    metallic gradient; the median of its dark pixels is representative,
    where a single probe would land on whichever streak it hit.
    """
    px = img.convert("RGB").load()
    w, h = img.size
    dark = [px[x, y] for y in range(0, h, 4) for x in range(0, w, 4)
            if sum(px[x, y]) < 320]
    if not dark:  # pragma: no cover — artwork would have to be all-white
        return (26, 26, 46)
    mid = len(dark) // 2
    return tuple(sorted(c[i] for c in dark)[mid] for i in range(3))


def _glyph(bg: tuple[int, int, int], canvas: int = 256) -> Image.Image:
    """The pad silhouette used below 32 px.

    Geometry is `vpad_helper.run_tray()`'s `icon_image()` at 4x, so the exe
    icon and the tray icon are the same drawing — nudged up 8 px because
    the tray version sits low to leave room pystray does not actually use.
    Drawn large and downsampled: LANCZOS on a 256 px source gives cleaner
    edges at 16 px than drawing 16 px directly.
    """
    img = Image.new("RGBA", (canvas, canvas), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([0, 0, canvas - 1, canvas - 1],
                        radius=int(canvas * 0.18), fill=bg + (255,))
    d.rounded_rectangle([24, 72, 232, 184], radius=48, fill=(255, 255, 255, 255))
    d.ellipse([56, 104, 104, 152], fill=bg + (255,))
    d.ellipse([152, 104, 200, 152], fill=bg + (255,))
    return img


def build(src: Path, dst: Path) -> None:
    base = Image.open(src).convert("RGBA")
    if base.width != base.height:
        raise SystemExit(f"{src}: expected a square source, got {base.size}")

    full = _transparent_corners(base)

    # Crop a square centred on the pad, above the wordmark. Height comes
    # from the measured cut; width is the same so the pad keeps its
    # proportions, centred horizontally.
    side = int(base.height * WORDMARK_CUT)
    left = (base.width - side) // 2
    compact = _round_corners(base.crop((left, 0, left + side, side)))
    glyph = _glyph(_navy(base))

    layers = (
        [full.resize((s, s), Image.LANCZOS) for s in FULL_SIZES]
        + [compact.resize((s, s), Image.LANCZOS) for s in COMPACT_SIZES]
        + [glyph.resize((s, s), Image.LANCZOS) for s in GLYPH_SIZES]
    )

    # Pillow writes every `sizes` entry from the image it is called on, so
    # the two drawings cannot be mixed in one save() call. Save the largest
    # and append the rest explicitly.
    layers[0].save(dst, format="ICO", sizes=[(i.width, i.height) for i in layers],
                   append_images=layers[1:])
    print(f"{dst}  ({dst.stat().st_size / 1024:.1f} KB, "
          f"{len(layers)} sizes: {', '.join(str(i.width) for i in layers)})")


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else here / "brand" / "icongamepad.png"
    dst = Path(sys.argv[2]) if len(sys.argv) > 2 else here / "vpad-helper.ico"
    build(src, dst)
