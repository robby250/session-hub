"""Fail-closed Codex App Server launch and ownership primitives (task-2194 row518).

This module deliberately contains no Qt, tmux, or terminal calls.  Session Hub owns one
private Unix endpoint per saved group row and publishes its process identity atomically.
"""
from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import socket
import time
from pathlib import Path

SCHEMA_VERSION = 1
REGISTRY_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "session-hub" / "codex-app-server"


def endpoint_for(row_id: str, root: Path = REGISTRY_DIR) -> Path:
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in row_id)
    return root / f"{safe}-{secrets.token_hex(8)}.sock"


def app_server_argv(endpoint: Path, cwd: str) -> list[str]:
    # cwd is supplied as Popen(cwd=...), never smuggled into a protocol argv.
    return ["codex", "app-server", "--listen", f"unix://{endpoint}"]


def remote_tui_argv(endpoint: Path, thread_id: str, cwd: str) -> list[str]:
    return ["codex", "--remote", f"unix://{endpoint}", "--cd", cwd, "resume", thread_id]


def publish_record(path: Path, *, row_id: str, endpoint: Path, thread_id: str, process: subprocess.Popen) -> dict:
    if path.exists():
        raise RuntimeError("Codex App Server owner record already exists")
    start = process_start_time(process.pid)
    if not start:
        raise RuntimeError("Codex App Server has no process identity")
    record = {"schema": SCHEMA_VERSION, "row_id": row_id, "endpoint": str(endpoint),
              "thread_id": thread_id, "pid": process.pid, "start_time": start}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    tmp.write_text(json.dumps(record, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return record


def process_start_time(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[21]
    except (OSError, IndexError):
        return ""


def live_record(path: Path, *, row_id: str) -> dict:
    record = json.loads(path.read_text())
    if record.get("schema") != SCHEMA_VERSION or record.get("row_id") != row_id:
        raise RuntimeError("Codex App Server owner record mismatch")
    pid = int(record["pid"])
    if not record.get("start_time") or process_start_time(pid) != record.get("start_time"):
        raise RuntimeError("Codex App Server owner is stale or PID was reused")
    endpoint = Path(record["endpoint"])
    if endpoint.parent != REGISTRY_DIR and endpoint.parent != path.parent:
        raise RuntimeError("Codex App Server endpoint is outside the owned registry")
    if not endpoint.exists():
        raise RuntimeError("Codex App Server endpoint is unavailable")
    return record


def stop_owned(path: Path, *, row_id: str) -> None:
    record = live_record(path, row_id=row_id)
    os.kill(int(record["pid"]), signal.SIGTERM)
    Path(record["endpoint"]).unlink(missing_ok=True)
    path.unlink(missing_ok=True)


def wait_ready(endpoint: Path, timeout: float = 5.0) -> bool:
    """Handshake readiness: connect to the Unix endpoint before publication or TUI launch."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            peer.settimeout(0.25)
            peer.connect(str(endpoint))
            peer.close()
            return True
        except OSError:
            time.sleep(0.05)
    return False
