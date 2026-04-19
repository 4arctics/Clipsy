#!/bin/sh
set -eu

APP_DIR="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
DESKTOP_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
TARGET="$DESKTOP_DIR/clipsy.desktop"

mkdir -p "$DESKTOP_DIR"
sed "s|@APP_DIR@|$APP_DIR|g" "$APP_DIR/desktop/clipsy.desktop.in" > "$TARGET"
chmod 0644 "$TARGET"
chmod 0755 "$APP_DIR/scripts/clipsy-gui-launch"

if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$DESKTOP_DIR" >/dev/null 2>&1 || true
fi

echo "Installed $TARGET"
echo "Open rofi with drun/app mode, then type: Clipsy"
