#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPLICATIONS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/scalable/apps"
BIN_DIR="$HOME/.local/bin"

# Pin the maintained OSS textual-terminal adapter used by the retained TUI terminal.
python3 -m pip install --user --break-system-packages textual-terminal==0.3.0

mkdir -p "$APPLICATIONS_DIR" "$ICON_DIR" "$BIN_DIR"
ln -sfn "$PROJECT_DIR/session_hub.py" "$BIN_DIR/session-hub"
ln -sfn "$PROJECT_DIR/session_hub_tui.py" "$BIN_DIR/session-hub-tui"
cp "$PROJECT_DIR/assets/session-hub.svg" "$ICON_DIR/session-hub.svg"

sed "s|@PROJECT_DIR@|$PROJECT_DIR|g" \
  "$PROJECT_DIR/session-hub.desktop.in" \
  > "$APPLICATIONS_DIR/session-hub.desktop"

chmod +x \
  "$PROJECT_DIR/session_hub.py" \
  "$PROJECT_DIR/session_hub_tui.py" \
  "$BIN_DIR/session-hub" \
  "$BIN_DIR/session-hub-tui" \
  "$APPLICATIONS_DIR/session-hub.desktop"
gio set "$APPLICATIONS_DIR/session-hub.desktop" metadata::trusted true 2>/dev/null || true
update-desktop-database "$APPLICATIONS_DIR" 2>/dev/null || true
gtk-update-icon-cache -f -t "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" 2>/dev/null || true

echo "Session Hub installed."
