#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APPLICATIONS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
BIN_DIR="$HOME/.local/bin"

mkdir -p "$APPLICATIONS_DIR" "$BIN_DIR"
ln -sfn "$PROJECT_DIR/session_hub.py" "$BIN_DIR/session-hub"

sed "s|@PROJECT_DIR@|$PROJECT_DIR|g" \
  "$PROJECT_DIR/session-hub.desktop.in" \
  > "$APPLICATIONS_DIR/session-hub.desktop"

chmod +x "$PROJECT_DIR/session_hub.py" "$BIN_DIR/session-hub"
update-desktop-database "$APPLICATIONS_DIR" 2>/dev/null || true

echo "Session Hub installed."
