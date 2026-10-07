#!/bin/sh
# Instala o LOOPS para o usuário atual (sem sudo) e cria o atalho de duplo clique.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/.local/opt/loops"
BIN_DIR="$HOME/.local/bin"
APPS_DIR="$HOME/.local/share/applications"

mkdir -p "$APP_DIR" "$BIN_DIR" "$APPS_DIR"
install -m 755 "$HERE/loops" "$APP_DIR/loops"
ln -sf "$APP_DIR/loops" "$BIN_DIR/loops"

# Atalho no menu de aplicativos: Terminal=true faz o sistema abrir um terminal
sed "s|@EXEC@|$APP_DIR/loops|g" "$HERE/loops.desktop" > "$APPS_DIR/loops.desktop"
chmod 755 "$APPS_DIR/loops.desktop"

# Atalho na área de trabalho (se existir)
DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
if [ -d "$DESKTOP_DIR" ]; then
  cp "$APPS_DIR/loops.desktop" "$DESKTOP_DIR/LOOPS.desktop"
  chmod 755 "$DESKTOP_DIR/LOOPS.desktop"
  # GNOME/Ubuntu exige marcar o atalho como confiável
  gio set "$DESKTOP_DIR/LOOPS.desktop" metadata::trusted true 2>/dev/null || true
fi
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS_DIR" 2>/dev/null || true

echo "LOOPS instalado!"
echo " - Pelo menu de aplicativos: procure por LOOPS"
echo " - Pelo terminal: loops   (se não funcionar, adicione ~/.local/bin ao PATH)"
