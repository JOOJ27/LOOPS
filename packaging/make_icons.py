#!/usr/bin/env python3
"""
Troca o ícone do LOOPS: coloque uma imagem QUADRADA (de preferência 1024x1024,
PNG com fundo transparente) em packaging/icons/source.png e rode:

    pip install pillow
    python packaging/make_icons.py

Gera icon.png (Linux), icon.ico (Windows) e icon.icns (Mac).
"""
from pathlib import Path
from PIL import Image

ICONS = Path(__file__).resolve().parent / "icons"
src = Image.open(ICONS / "source.png").convert("RGBA")
if src.width != src.height:
    raise SystemExit("A imagem precisa ser quadrada.")
big = src.resize((1024, 1024), Image.LANCZOS)
big.resize((512, 512), Image.LANCZOS).save(ICONS / "icon.png")
big.save(ICONS / "icon.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
big.save(ICONS / "icon.icns")
print("Ícones gerados em", ICONS)
