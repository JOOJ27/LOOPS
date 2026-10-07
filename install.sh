#!/bin/sh
# Instalador de uma linha do LOOPS (Linux e macOS):
#   curl -fsSL https://raw.githubusercontent.com/JOOJ27/LOOPS/client/install.sh | sh
set -e

REPO="JOOJ27/LOOPS"
BASE="${LOOPS_URL_BASE:-https://github.com/$REPO/releases/latest/download}"   # LOOPS_URL_BASE: só para testes

case "$(uname -s)" in
  Linux)  os=linux; ext=tar.gz ;;
  Darwin) os=macos; ext=zip ;;
  *) echo "Sistema não suportado aqui. No Windows, baixe o .exe em https://github.com/$REPO/releases/latest"; exit 1 ;;
esac
case "$(uname -m)" in
  x86_64|amd64)  arch=x64 ;;
  aarch64|arm64) arch=arm64 ;;
  *) echo "Arquitetura não suportada: $(uname -m)"; exit 1 ;;
esac

asset="loops-$os-$arch.$ext"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "→ Baixando $asset ..."
curl -fL --progress-bar "$BASE/$asset" -o "$tmp/$asset" || {
  echo "Falha no download. Confira se existe um release publicado em https://github.com/$REPO/releases"; exit 1; }

echo "→ Instalando ..."
cd "$tmp"
if [ "$ext" = "tar.gz" ]; then tar xzf "$asset"; else unzip -q "$asset"; fi
sh ./loops/setup.sh
