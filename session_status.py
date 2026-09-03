"""Small status-file operations shared by the Qt process and embedded terminal helper."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path


def status_dir_from_environment() -> Path:
    """Return the Session Hub status directory selected by the inherited XDG environment."""
    data_home = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return data_home / "session-hub" / "status"


def mark_needs_input_answered(status_dir: Path, session_id: str) -> bool:
    """Atomically invalidate an existing blocker after a submitted terminal answer."""
    if not session_id:
        return False
    path = status_dir / f"{session_id}.json"
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if current.get("state") != "needs_input":
        return False
    replacement = {"state": "working", "ts": time.time(), "detail": "", "reason": ""}
    try:
        status_dir.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".answering.tmp")
        tmp.write_text(json.dumps(replacement), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return False
    return True
