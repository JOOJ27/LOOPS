#!/usr/bin/env python3
"""
Update the LOOPS icon: place a SQUARE image (preferably 1024x1024,
PNG with a transparent background) in packaging/icons/source.png and run:

    pip install pillow
    python packaging/make_icons.py

Gera icon.png (Linux), icon.ico (Windows) e icon.icns (Mac).
"""
from pathlib import Path
from PIL import Image

ICONS = Path(__file__).resolve().parent / "icons"
src = Image.open(ICONS / "source.png").convert("RGBA")
if src.width != src.height:
    raise SystemExit("The image must be square.")
big = src.resize((1024, 1024), Image.LANCZOS)
big.resize((512, 512), Image.LANCZOS).save(ICONS / "icon.png")
big.save(ICONS / "icon.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
big.save(ICONS / "icon.icns")
print("Icons generated in", ICONS)
