#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPLICATIONS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
BIN_DIR="$HOME/.local/bin"

mkdir -p "$APPLICATIONS_DIR" "$ICON_DIR" "$BIN_DIR"
ln -sfn "$PROJECT_DIR/session_hub.py" "$BIN_DIR/session-hub"
cp "$PROJECT_DIR/assets/session-hub.svg" "$ICON_DIR/session-hub.svg"

sed "s|@PROJECT_DIR@|$PROJECT_DIR|g" \
  "$PROJECT_DIR/session-hub.desktop.in" \
  > "$APPLICATIONS_DIR/session-hub.desktop"

chmod +x \
  "$PROJECT_DIR/session_hub.py" \
  "$BIN_DIR/session-hub" \
  "$APPLICATIONS_DIR/session-hub.desktop"
gio set "$APPLICATIONS_DIR/session-hub.desktop" metadata::trusted true 2>/dev/null || true
update-desktop-database "$APPLICATIONS_DIR" 2>/dev/null || true
gtk-update-icon-cache -f -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" 2>/dev/null || true

echo "Session Hub installed."
