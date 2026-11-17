"""Render the ZeroDesk icon (web/icon.svg) for the store (256px) and the
launcher (100px). Needs cairosvg. Run: python3 tools/make_icons.py"""
import io
import os

import cairosvg
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
SVG = os.path.join(HERE, "..", "web", "icon.svg")
OUT = os.path.join(HERE, "..", "packaging")

if __name__ == "__main__":
    big = Image.open(io.BytesIO(cairosvg.svg2png(url=SVG, output_width=1024, output_height=1024))).convert("RGBA")
    big.resize((256, 256), Image.LANCZOS).save(os.path.join(OUT, "icon-256.png"))
    big.resize((100, 100), Image.LANCZOS).save(os.path.join(OUT, "zerodesk.png"))
    print("icons written")
