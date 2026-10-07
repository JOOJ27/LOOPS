#!/bin/sh
# Instala o LOOPS para o usuário atual (sem sudo), com ícone e atalho.
# Funciona em Linux e macOS. Rode a partir da pasta extraída: ./setup.sh
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/.local/opt/loops"
BIN_DIR="$HOME/.local/bin"

mkdir -p "$APP_DIR" "$BIN_DIR"
install -m 755 "$HERE/loops" "$APP_DIR/loops"
ln -sf "$APP_DIR/loops" "$BIN_DIR/loops"

case "$(uname -s)" in
Linux)
  APPS_DIR="$HOME/.local/share/applications"
  mkdir -p "$APPS_DIR"
  cp "$HERE/icon.png" "$APP_DIR/icon.png"

  # Terminal=true faz o sistema abrir um terminal ao clicar no atalho
  sed -e "s|@EXEC@|$APP_DIR/loops|g" -e "s|@ICON@|$APP_DIR/icon.png|g" \
      "$HERE/loops.desktop" > "$APPS_DIR/loops.desktop"
  chmod 755 "$APPS_DIR/loops.desktop"

  DESKTOP_DIR="$(xdg-user-dir DESKTOP 2>/dev/null || echo "$HOME/Desktop")"
  if [ -d "$DESKTOP_DIR" ]; then
    cp "$APPS_DIR/loops.desktop" "$DESKTOP_DIR/LOOPS.desktop"
    chmod 755 "$DESKTOP_DIR/LOOPS.desktop"
    gio set "$DESKTOP_DIR/LOOPS.desktop" metadata::trusted true 2>/dev/null || true
  fi
  command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS_DIR" 2>/dev/null || true
  WHERE="no menu de aplicativos (procure por LOOPS)"
  ;;
Darwin)
  APP="$HOME/Applications/LOOPS.app"
  rm -rf "$APP"
  mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
  cp "$HERE/icon.icns" "$APP/Contents/Resources/loops.icns"

  # O "app" só pede para o Terminal abrir o executável do LOOPS
  cat > "$APP/Contents/MacOS/LOOPS" <<APPEOF
#!/bin/sh
exec open -a Terminal "$APP_DIR/loops"
APPEOF
  chmod 755 "$APP/Contents/MacOS/LOOPS"

  cat > "$APP/Contents/Info.plist" <<'PLEOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>LOOPS</string>
  <key>CFBundleDisplayName</key><string>LOOPS</string>
  <key>CFBundleIdentifier</key><string>com.joooj27.loops</string>
  <key>CFBundleExecutable</key><string>LOOPS</string>
  <key>CFBundleIconFile</key><string>loops</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>LSMinimumSystemVersion</key><string>10.13</string>
</dict>
</plist>
PLEOF
  # Remove a marca de "baixado da internet" (evita o aviso do Gatekeeper)
  xattr -dr com.apple.quarantine "$APP" "$APP_DIR/loops" 2>/dev/null || true
  touch "$APP"
  WHERE="em Aplicativos (pasta ~/Applications) e no Launchpad/Spotlight: LOOPS"
  ;;
*)
  echo "Sistema não suportado por este instalador."; exit 1 ;;
esac

echo
echo "✔ LOOPS instalado!"
echo "  • Abra $WHERE"
echo "  • Ou digite no terminal: loops   (se não achar o comando, adicione ~/.local/bin ao PATH)"
