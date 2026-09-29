#!/usr/bin/env python3
"""Renderiza demo/out/frames.json em demo/demo.gif.

O conteúdo de cada frame é a saída REAL de demo.py; a animação é apenas
apresentação — as linhas aparecem progressivamente, como num terminal.

    python3 demo/demo.py --json demo/out/frames.json
    python3 demo/render_gif.py demo/out/frames.json demo/demo.gif
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
MONO_B = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

BG = (13, 17, 23)        # GitHub dark canvas
BAR = (22, 27, 34)       # title bar
FG = (201, 209, 217)
DIM = (139, 148, 158)
STYLES = {
    31: (255, 123, 114),   # red
    32: (63, 185, 80),     # green
    33: (227, 179, 65),    # yellow
    36: (57, 197, 207),    # cyan
    37: (201, 209, 217),   # white
    90: (139, 148, 158),   # grey
    1: (255, 255, 255),    # bold -> brighter
}

SGR = re.compile(r"\033\[([0-9;]*)m")
PLAIN = re.compile(r"\033\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return PLAIN.sub("", text)


def runs(text: str, base_colour: tuple[int, int, int], base_bold: bool):
    """Split a string with inline SGR escapes into (chunk, colour, bold) runs."""
    colour, bold, out, pos = base_colour, base_bold, [], 0
    for m in SGR.finditer(text):
        if m.start() > pos:
            out.append((text[pos:m.start()], colour, bold))
        for part in m.group(1).split(";"):
            n = int(part) if part else 0
            if n == 0:
                colour, bold = base_colour, base_bold
            elif n == 1:
                colour, bold = STYLES[1], True
            elif n == 2:
                colour, bold = DIM, False
            elif n in STYLES:
                colour = STYLES[n]
        pos = m.end()
    if pos < len(text):
        out.append((text[pos:], colour, bold))
    return out


def parse_style(code: str) -> tuple[tuple[int, int, int], bool]:
    """Return the (colour, bold) described by a whole-line SGR prefix."""
    colour, bold = FG, False
    for part in code.replace("\033[", "").replace("m", "").split(";"):
        if not part:
            continue
        n = int(part)
        if n == 1:
            colour, bold = STYLES[1], True
        elif n == 2:
            colour, bold = DIM, False
        elif n == 0:
            colour, bold = FG, False
        elif n in STYLES:
            colour = STYLES[n]
    return colour, bold


def main() -> int:
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "demo/out/frames.json")
    dst = Path(sys.argv[2] if len(sys.argv) > 2 else "demo/demo.gif")
    if not src.is_file():
        print(f"frames não encontrados: {src}", file=sys.stderr)
        return 3

    doc = json.loads(src.read_text())
    lines: list[tuple[str, str]] = [(l["t"], l["s"]) for l in doc["lines"]]

    size = 16
    font = ImageFont.truetype(MONO, size)
    font_b = ImageFont.truetype(MONO_B, size)
    sample = ImageDraw.Draw(Image.new("RGB", (1, 1)))
    char_w = sample.textlength("M", font=font)
    line_h = size + 6
    pad = 16
    bar_h = 30

    max_cols = min(100, max((len(strip_ansi(t)) for t, _ in lines), default=40) + 1)
    cols = int(max_cols)
    rows = 34
    width = int(pad * 2 + cols * char_w)
    height = bar_h + pad * 2 + rows * line_h

    tmp = Path(tempfile.mkdtemp(prefix="ag-gif-"))
    try:
        frame_paths: list[Path] = []

        def draw(revealed: int, cursor: bool) -> Image.Image:
            img = Image.new("RGB", (width, height), BG)
            d = ImageDraw.Draw(img)
            # title bar
            d.rectangle([0, 0, width, bar_h], fill=BAR)
            for i, c in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
                d.ellipse([10 + i * 18, 10, 10 + i * 18 + 10, 20], fill=c)
            title = doc.get("title", "")
            d.text((width / 2 - d.textlength(title, font=font) / 2, 7),
                   title, font=font, fill=DIM)
            # viewport: last `rows` revealed lines (scrolls once it fills)
            visible = lines[:revealed][-rows:]
            start_y = bar_h + pad
            for i, (text, style) in enumerate(visible):
                base_colour, base_bold = parse_style(style)
                x = pad
                y = start_y + i * line_h
                for chunk, colour, bold in runs(style + text, base_colour, base_bold):
                    d.text((x, y), chunk, font=font_b if bold else font, fill=colour)
                    x += d.textlength(chunk, font=font_b if bold else font)
            if cursor and visible:
                plain = strip_ansi(visible[-1][0])
                cx = pad + len(plain) * char_w
                cy = start_y + (len(visible) - 1) * line_h
                d.rectangle([cx + 2, cy + 2, cx + 2 + char_w - 3, cy + 2 + line_h - 7],
                            fill=(63, 185, 80))
            return img

        # reveal a couple of lines per frame, then hold the result on screen
        step = 2
        n = len(lines)
        idx = 0
        for revealed in range(step, n + 1, step):
            img = draw(revealed, cursor=True)
            p = tmp / f"f{idx:05d}.png"
            img.save(p, optimize=True)
            frame_paths.append(p)
            idx += 1
        # final frame: full text, no cursor, held longer
        final = draw(n, cursor=False)
        for _ in range(24):
            p = tmp / f"f{idx:05d}.png"
            final.save(p, optimize=True)
            frame_paths.append(p)
            idx += 1

        dst.parent.mkdir(parents=True, exist_ok=True)
        pattern = str(tmp / "f%05d.png")
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-framerate", "7", "-i", pattern,
            "-vf",
            "split[s0][s1];[s0]palettegen=max_colors=128:stats_mode=diff[p];"
            "[s1][p]paletteuse=dither=bayer:bayer_scale=3",
            "-loop", "0", str(dst),
        ]
        subprocess.run(cmd, check=True)
        print(f"{dst}  ({dst.stat().st_size / 1024:.0f} KiB, "
              f"{len(frame_paths)} frames, {width}x{height})")
    finally:
        if os.environ.get("AG_KEEP_FRAMES"):
            print(f"frames mantidos em {tmp}", file=sys.stderr)
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
