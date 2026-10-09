#!/bin/sh
# One-line LOOPS installer (Linux and macOS):
#   curl -fsSL https://raw.githubusercontent.com/JOOJ27/LOOPS/client/install.sh | sh
set -e

REPO="JOOJ27/LOOPS"
BASE="${LOOPS_URL_BASE:-https://github.com/$REPO/releases/latest/download}"   # LOOPS_URL_BASE: só para testes

case "$(uname -s)" in
  Linux)  os=linux; ext=tar.gz ;;
  Darwin) os=macos; ext=zip ;;
  *) echo "Unsupported operating system. On Windows, download the .exe from https://github.com/$REPO/releases/latest"; exit 1 ;;
esac
case "$(uname -m)" in
  x86_64|amd64)  arch=x64 ;;
  aarch64|arm64) arch=arm64 ;;
  *) echo "Unsupported architecture: $(uname -m)"; exit 1 ;;
esac

asset="loops-$os-$arch.$ext"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "→ Downloading $asset ..."
curl -fL --progress-bar "$BASE/$asset" -o "$tmp/$asset" || {
  echo "Download failed. Check for a published release at https://github.com/$REPO/releases"; exit 1; }

echo "→ Installing ..."
cd "$tmp"
if [ "$ext" = "tar.gz" ]; then tar xzf "$asset"; else unzip -q "$asset"; fi
sh ./loops/setup.sh
