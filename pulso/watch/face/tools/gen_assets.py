#!/usr/bin/env python3
"""Generate the Pulso face's binary resources.

  * resources/drawables/launcher_icon.png  40x40 launcher icon (pure-Python PNG writer, no PIL needed)
  * resources/fonts/*.fnt + *_0.png        BMFont (text .fnt + 8-bit grey PNG page) bitmap fonts,
                                            rasterised with Pillow from OFL fonts (Carlito, Big Shoulders)

Usage:  python3 tools/gen_assets.py [--scale 1.0] [--out resources] [--icon-only]

--scale 0.84 (218/260) produces a font set for the 218x218 Forerunner 255S; put it in
resources-round-218x218/fonts/ and add fr255s/fr255sm to the manifest.

Fonts are looked up in the usual Linux locations; override with PULSO_FONT_DIRS=dir1:dir2.
The glyphs are rendered white on black (the PNG value is the coverage/alpha), the format the
Connect IQ resource compiler expects (see the SDK's Analog sample).
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROJECT = HERE.parent

# --- palette of the FR255 MIP display: 2 bits per channel ---------------------------------------
LEVELS = (0x00, 0x55, 0xAA, 0xFF)


def snap(c: int) -> int:
    return min(LEVELS, key=lambda v: abs(v - c))


# --- tiny PNG writer (RGBA, 8 bit) -----------------------------------------------------------------
def write_png(path: Path, w: int, h: int, rgba: list[tuple[int, int, int, int]]) -> None:
    raw = bytearray()
    for y in range(h):
        raw.append(0)  # filter: none
        for x in range(w):
            raw.extend(rgba[y * w + x])

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def make_icon(path: Path, size: int = 40) -> None:
    """Round dark badge with a 64-colour 'stress ring' (blue -> red) and a white pulse line."""
    import math

    ring = [(0x00, 0x55, 0xFF), (0x00, 0xAA, 0xFF), (0x00, 0xFF, 0xAA), (0x55, 0xFF, 0x55),
            (0xFF, 0xFF, 0x00), (0xFF, 0xAA, 0x00), (0xFF, 0x55, 0x00), (0xFF, 0x00, 0x00)]
    c = (size - 1) / 2.0
    # ECG trace in a 40-unit grid (scaled to `size`): P wave, QRS spike, T wave
    trace = [(8, 21), (13, 21), (15, 18.5), (17, 21), (19, 21), (21.5, 8), (24.5, 31), (26.5, 21),
             (28.5, 21), (30.5, 18), (32.5, 21)]
    pts = [(x * size / 40.0, y * size / 40.0) for x, y in trace]

    def dist_to_polyline(x: float, y: float) -> float:
        best = 1e9
        for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
            dx, dy = x1 - x0, y1 - y0
            t = max(0.0, min(1.0, ((x - x0) * dx + (y - y0) * dy) / (dx * dx + dy * dy)))
            best = min(best, math.hypot(x - (x0 + t * dx), y - (y0 + t * dy)))
        return best

    px: list[tuple[int, int, int, int]] = []
    for y in range(size):
        for x in range(size):
            r = math.hypot(x - c, y - c)
            if r > size / 2.0:
                px.append((0, 0, 0, 0))
                continue
            col = (0, 0, 0)
            if r >= size / 2.0 - 4:
                ang = (math.degrees(math.atan2(x - c, -(y - c))) + 360) % 360  # 0 at 12 o'clock, cw
                col = ring[min(7, int(ang / 45))]
                if int(ang) % 45 < 4:      # small gaps between segments
                    col = (0, 0, 0)
            if dist_to_polyline(x, y) < 1.15:
                col = (0xFF, 0xFF, 0xFF)
            px.append((snap(col[0]), snap(col[1]), snap(col[2]), 255))
    write_png(path, size, size, px)


# --- BMFont generation -------------------------------------------------------------------------------
FONT_DIRS = [
    "/usr/share/fonts/truetype/crosextra",
    "/usr/share/fonts/truetype/carlito",
    "/usr/share/fonts/truetype",
    "/usr/share/fonts",
    "/mnt/skills/examples/canvas-design/canvas-fonts",
    str(Path.home() / ".fonts"),
    str(HERE / "fonts"),
]

LATIN_PT = "ÁÀÂÃÉÊÍÓÔÕÚÜÇáàâãéêíóôõúüç"
ASCII = "".join(chr(c) for c in range(32, 127))

FONTS = [
    # id, file, pixel size at 260 px, charset, tabular digits, contrast boost
    ("time", "BigShoulders-Bold.ttf", 58, "0123456789:", True, 1.3),
    ("mid", "Carlito-Bold.ttf", 25, " 0123456789+-.,:%bhmpz", True, 1.3),
    ("small", "Carlito-Bold.ttf", 16, ASCII + LATIN_PT + "°·•…–’“”", True, 1.45),
    ("tiny", "Carlito-Bold.ttf", 12, " ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789+-.,:%/()·" + "ÁÂÃÉÊÍÓÔÕÚÇ", True, 1.6),
]


# 1-bit pixel icons packed as a BMFont: one ASCII letter per icon so Monkey C can draw them with
# drawText (colour = current foreground colour). 10x10, '#' = on.
ICONS = {
    "h": [  # heart: heart rate
        "..........",
        ".##...##..",
        "####.####.",
        "#########.",
        "#########.",
        ".#######..",
        "..#####...",
        "...###....",
        "....#.....",
        "..........",
    ],
    "b": [  # battery: Body Battery
        "...####...",
        "..######..",
        "..#....#..",
        "..#.##.#..",
        "..#.##.#..",
        "..#.##.#..",
        "..#.##.#..",
        "..#.##.#..",
        "..######..",
        "..........",
    ],
    "r": [  # wind: respiration
        "..........",
        "######....",
        "......#...",
        "######....",
        "..........",
        "########..",
        "........#.",
        "#######.#.",
        ".......#..",
        "..........",
    ],
    "c": [  # hourglass: recovery time
        "#########.",
        ".#.....#..",
        "..#...#...",
        "...#.#....",
        "....#.....",
        "...#.#....",
        "..#.#.#...",
        ".#.###.#..",
        "#########.",
        "..........",
    ],
    "v": [  # ECG beat: HRV
        "..........",
        "....#.....",
        "....#.....",
        "...#.#....",
        "#..#.#..##",
        ".##..#.#..",
        ".....#.#..",
        "......#...",
        "......#...",
        "..........",
    ],
    "s": [  # moon: sleep
        "...####...",
        ".###......",
        ".##.......",
        "##........",
        "##........",
        "##........",
        ".##.....#.",
        ".####.###.",
        "...####...",
        "..........",
    ],
    "d": [  # moon with minus: sleep debt
        "..###.....",
        ".##.......",
        "##........",
        "##..#####.",
        "##........",
        ".##.......",
        "..###.....",
        "..........",
        "..........",
        "..........",
    ],
    "p": [  # peak
        "..........",
        "....#.....",
        "...###....",
        "...###....",
        "..#####...",
        "..#####...",
        ".#######..",
        ".#######..",
        "#########.",
        "..........",
    ],
    "k": [  # calendar: next meeting
        ".#....#...",
        "########..",
        "#......#..",
        "########..",
        "#.#.#.##..",
        "#......#..",
        "#.#.#..#..",
        "#......#..",
        "########..",
        "..........",
    ],
    "g": [  # bolt: Garmin stress
        ".....##...",
        "....##....",
        "...##.....",
        "..######..",
        ".....##...",
        "....##....",
        "...##.....",
        "..##......",
        ".#........",
        "..........",
    ],
    "o": [  # filled dot (risk)
        "..........",
        "..........",
        "...###....",
        "..#####...",
        "..#####...",
        "..#####...",
        "...###....",
        "..........",
        "..........",
        "..........",
    ],
    "O": [  # hollow dot (risk slot)
        "..........",
        "..........",
        "...###....",
        "..#...#...",
        "..#...#...",
        "..#...#...",
        "...###....",
        "..........",
        "..........",
        "..........",
    ],
    "u": [  # up arrow: link/upload
        "....#.....",
        "...###....",
        "..#.#.#...",
        ".#..#..#..",
        "....#.....",
        "....#.....",
        "....#.....",
        "....#.....",
        "..........",
        "..........",
    ],
    "x": [  # cross: link error
        "..........",
        "#......#..",
        ".#....#...",
        "..#..#....",
        "...##.....",
        "...##.....",
        "..#..#....",
        ".#....#...",
        "#......#..",
        "..........",
    ],
}


def make_icon_font(out: Path, scale: float = 1.0) -> dict:
    """Pack ICONS into a BMFont. Scale is applied by nearest-neighbour (keeps pixels crisp)."""
    from PIL import Image

    k = max(1, round(scale)) if scale >= 1 else 1
    n = 10 * k
    names = list(ICONS)
    page_w = 256
    per_row = (page_w - 1) // (n + 1)
    rows = (len(names) + per_row - 1) // per_row
    page_h = 1
    while page_h < rows * (n + 1) + 1:
        page_h *= 2
    page = Image.new("L", (page_w, page_h), 0)
    lines_c = []
    for i, ch in enumerate(names):
        px, py = 1 + (i % per_row) * (n + 1), 1 + (i // per_row) * (n + 1)
        for y, row in enumerate(ICONS[ch]):
            assert len(row) == 10, (ch, y)
            for x, c in enumerate(row):
                if c == "#":
                    for dy in range(k):
                        for dx in range(k):
                            page.putpixel((px + x * k + dx, py + y * k + dy), 255)
        lines_c.append(
            f"char id={ord(ch):<5} x={px:<5} y={py:<5} width={n:<5} height={n:<5} "
            f"xoffset=0     yoffset=0     xadvance={n + k:<5} page=0  chnl=15"
        )
    page.save(out / "icons_0.png")
    head = [
        f'info face="pulso-icons" size=-{n} bold=0 italic=0 charset="" unicode=1 stretchH=100 smooth=0 aa=0 '
        f"padding=0,0,0,0 spacing=1,1 outline=0",
        f"common lineHeight={n} base={n} scaleW={page_w} scaleH={page_h} pages=1 packed=0 "
        f"alphaChnl=1 redChnl=0 greenChnl=0 blueChnl=0",
        'page id=0 file="icons_0.png"',
        f"chars count={len(names)}",
    ]
    (out / "icons.fnt").write_text("\n".join(head + lines_c) + "\n", encoding="utf-8")
    return {"id": "icons", "lineHeight": n, "glyphs": len(names)}


def find_font(name: str) -> str:
    dirs = os.environ.get("PULSO_FONT_DIRS", "").split(":") + FONT_DIRS
    for d in dirs:
        if not d:
            continue
        for root, _, files in os.walk(d):
            if name in files:
                return os.path.join(root, name)
    raise SystemExit(f"font {name} not found; set PULSO_FONT_DIRS")


def make_bmfont(fid: str, fname: str, px: int, chars: str, tabular: bool, boost: float, out: Path) -> dict:
    from PIL import Image, ImageDraw, ImageFont

    path = find_font(fname)
    font = ImageFont.truetype(path, px)
    glyphs = []
    for ch in dict.fromkeys(chars):  # keep order, drop duplicates
        x0, y0, x1, y1 = font.getbbox(ch, anchor="ls")
        adv = font.getlength(ch)
        w, h = max(0, x1 - x0), max(0, y1 - y0)
        img = None
        if w > 0 and h > 0:
            img = Image.new("L", (w, h), 0)
            ImageDraw.Draw(img).text((-x0, -y0), ch, fill=255, font=font, anchor="ls")
            # steepen the coverage curve: the MIP panel only has 4 grey levels, thin AA fringes vanish
            img = img.point(lambda v: min(255, int(v * boost)))
        glyphs.append({"ch": ch, "x0": x0, "y0": y0, "w": w, "h": h, "adv": adv, "img": img})

    top = min(g["y0"] for g in glyphs if g["h"] > 0)
    bottom = max(g["y0"] + g["h"] for g in glyphs if g["h"] > 0)
    line_h = bottom - top
    base = -top

    if tabular:
        digits = [g for g in glyphs if g["ch"].isdigit()]
        if digits:
            dadv = max(round(g["adv"]) for g in digits)
            for g in digits:
                g["x0"] += (dadv - round(g["adv"])) // 2
                g["adv"] = dadv

    # shelf packing, 1 px gutter, page width 256
    page_w, x, y, shelf = 256, 1, 1, 0
    for g in glyphs:
        w, h = max(1, g["w"]), max(1, g["h"])
        if x + w + 1 > page_w:
            x, y, shelf = 1, y + shelf + 1, 0
        g["px"], g["py"] = x, y
        x += w + 1
        shelf = max(shelf, h)
    page_h = 1
    while page_h < y + shelf + 1:
        page_h *= 2
    page = Image.new("L", (page_w, page_h), 0)
    for g in glyphs:
        if g["img"] is not None:
            page.paste(g["img"], (g["px"], g["py"]))

    png_name = f"{fid}_0.png"
    page.save(out / png_name)
    lines = [
        f'info face="pulso-{fid}" size=-{px} bold=1 italic=0 charset="" unicode=1 stretchH=100 smooth=1 aa=1 '
        f"padding=0,0,0,0 spacing=1,1 outline=0",
        f"common lineHeight={line_h} base={base} scaleW={page_w} scaleH={page_h} pages=1 packed=0 "
        f"alphaChnl=1 redChnl=0 greenChnl=0 blueChnl=0",
        f'page id=0 file="{png_name}"',
        f"chars count={len(glyphs)}",
    ]
    for g in glyphs:
        w, h = (g["w"], g["h"]) if g["img"] is not None else (1, 1)
        yoff = (g["y0"] - top) if g["img"] is not None else 0
        lines.append(
            f"char id={ord(g['ch']):<5} x={g['px']:<5} y={g['py']:<5} width={w:<5} height={h:<5} "
            f"xoffset={g['x0']:<5} yoffset={yoff:<5} xadvance={round(g['adv']):<5} page=0  chnl=15"
        )
    (out / f"{fid}.fnt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return {"id": fid, "lineHeight": line_h, "base": base, "page": (page_w, page_h), "glyphs": len(glyphs)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--scale", type=float, default=1.0, help="font scale (218/260 for the 255S)")
    ap.add_argument("--out", default=str(PROJECT / "resources"), help="resources directory")
    ap.add_argument("--icon-only", action="store_true")
    a = ap.parse_args()

    res = Path(a.out)
    (res / "drawables").mkdir(parents=True, exist_ok=True)
    make_icon(res / "drawables" / "launcher_icon.png", 40)
    print("icon:", res / "drawables" / "launcher_icon.png")
    if a.icon_only:
        return 0
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("Pillow missing: fonts not generated (pip install pillow)", file=sys.stderr)
        return 1
    (res / "fonts").mkdir(parents=True, exist_ok=True)
    for fid, fname, px, chars, tab, boost in FONTS:
        info = make_bmfont(fid, fname, max(8, round(px * a.scale)), chars, tab, boost, res / "fonts")
        print("font:", info)
    print("font:", make_icon_font(res / "fonts", a.scale))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
