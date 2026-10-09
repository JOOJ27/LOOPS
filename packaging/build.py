#!/usr/bin/env python3
"""
Gera o executável do LOOPS para o sistema operacional em que está rodando.

    pip install pyinstaller -r requirements.txt   (ou: pip install .)
    python packaging/build.py

Resultado em dist/:
  Linux   -> loops-linux-<arch>.tar.gz   (binário + install.sh + loops.desktop)
  macOS   -> loops-macos-<arch>.zip      (binário + LOOPS.command)
  Windows -> loops-windows-<arch>.exe
PyInstaller NÃO faz cross-compile: precisa rodar em cada SO (o workflow do
GitHub Actions já faz isso).
"""
import os
import sys
import shutil
import platform
import subprocess
import tarfile
import zipfile
import ctypes.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PKG = ROOT / "packaging"
DIST = ROOT / "dist"
WORK = ROOT / ".pyi-build"   # pasta temporária (não mexe no seu build/ do setuptools)

SYSTEM = platform.system()
ARCH = platform.machine().lower().replace("x86_64", "x64").replace("amd64", "x64").replace("aarch64", "arm64")


def find_portaudio_linux():
    """Acha o caminho completo da libportaudio.so.2 instalada no sistema."""
    name = ctypes.util.find_library("portaudio")        # ex.: 'libportaudio.so.2'
    if not name:
        sys.exit("libportaudio não encontrada. No Ubuntu: sudo apt install libportaudio2")
    if os.path.isabs(name):
        return name
    out = subprocess.run(["ldconfig", "-p"], capture_output=True, text=True).stdout
    for line in out.splitlines():
        if name in line and "=>" in line:
            return line.split("=>")[1].strip()
    for d in ("/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu", "/usr/lib64", "/usr/lib"):
        p = Path(d) / name
        if p.exists():
            return str(p)
    sys.exit(f"Não consegui localizar {name}")


def run_pyinstaller():
    for d in (WORK, DIST):
        shutil.rmtree(d, ignore_errors=True)

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onefile", "--console",
        "--name", "loops",
        "--paths", str(ROOT),
        "--distpath", str(DIST / "bin"),
        "--workpath", str(WORK / "work"),
        "--specpath", str(WORK),
        # módulos do projeto + libs que o PyInstaller às vezes não detecta
        "--hidden-import", "Client", "--hidden-import", "Camera",
        "--hidden-import", "Textbox", "--hidden-import", "Messagebox",
        "--hidden-import", "Animation", "--hidden-import", "Config",
        "--collect-all", "sounddevice",
        "--collect-all", "pywebrtc_audio",
        "--collect-submodules", "websockets",
    ]
    if SYSTEM == "Windows":
        cmd += ["--icon", str(PKG / "icons" / "icon.ico")]
    if SYSTEM == "Linux":
        cmd += ["--add-binary", f"{find_portaudio_linux()}:."]
    cmd.append(str(PKG / "launcher.py"))
    subprocess.check_call(cmd, cwd=ROOT)


def package():
    binname = "loops.exe" if SYSTEM == "Windows" else "loops"
    binary = DIST / "bin" / binname
    if not binary.exists():
        sys.exit("Build falhou: binário não encontrado")

    if SYSTEM == "Windows":
        shutil.copy(binary, DIST / f"loops-windows-{ARCH}.exe")
        return

    stage = DIST / "stage" / "loops"
    stage.mkdir(parents=True)
    shutil.copy(binary, stage / "loops")
    (stage / "loops").chmod(0o755)

    if SYSTEM == "Darwin":
        for f in ("LOOPS.command", "setup.sh"):
            shutil.copy(PKG / f, stage / f)
            (stage / f).chmod(0o755)
        shutil.copy(PKG / "icons" / "icon.icns", stage / "icon.icns")
        out = DIST / f"loops-macos-{ARCH}.zip"
        # zip -y preserva permissões de execução (zipfile do Python não)
        subprocess.check_call(["zip", "-qr", str(out), "loops"], cwd=stage.parent)
    else:
        for f in ("setup.sh", "loops.desktop"):
            shutil.copy(PKG / f, stage / f)
        (stage / "setup.sh").chmod(0o755)
        shutil.copy(PKG / "icons" / "icon.png", stage / "icon.png")
        out = DIST / f"loops-linux-{ARCH}.tar.gz"
        with tarfile.open(out, "w:gz") as tar:
            tar.add(stage, arcname="loops")
    print("Gerado:", out)


if __name__ == "__main__":
    run_pyinstaller()
    package()
