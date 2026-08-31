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


def remote_tui_argv(endpoint: Path, thread_id: str | None, cwd: str) -> list[str]:
    """Build a remote client command, resuming only an existing saved thread.

    A fresh Codex row has no thread id yet; omitting ``resume`` asks the remote
    client to create its first thread instead of emitting the invalid ``resume ""``.
    """
    argv = ["codex", "--remote", f"unix://{endpoint}", "--cd", cwd]
    if thread_id:
        argv += ["resume", thread_id]
    return argv


def publish_record(
    path: Path, *, row_id: str, endpoint: Path, thread_id: str, process: subprocess.Popen,
    name: str | None = None, aliases: list[str] | None = None,
) -> dict:
    if path.exists():
        raise RuntimeError("Codex App Server owner record already exists")
    if endpoint.parent != path.parent or endpoint.name != f"{path.stem}.sock":
        raise RuntimeError("Codex App Server endpoint is not bound to its owner record")
    start = process_start_time(process.pid)
    if not start:
        raise RuntimeError("Codex App Server has no process identity")
    record = {"schema": SCHEMA_VERSION, "row_id": row_id, "endpoint": str(endpoint),
              "thread_id": thread_id, "pid": process.pid, "start_time": start}
    # ``row_id`` is the stable ownership key; ``name``/``aliases`` are the public managed-row
    # addresses consumed by session_ctl's resolver. Keeping both in one record prevents a group
    # row's private override key from becoming an unaddressable App Server owner.
    if name:
        record["name"] = name
    if aliases:
        record["aliases"] = sorted({alias for alias in aliases if alias})
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
    if endpoint.parent != path.parent or endpoint.name != f"{path.stem}.sock":
        raise RuntimeError("Codex App Server endpoint is outside the owned registry")
    if not endpoint.exists():
        raise RuntimeError("Codex App Server endpoint is unavailable")
    return record


def stop_owned(path: Path, *, row_id: str) -> None:
    record = live_record(path, row_id=row_id)
    os.kill(int(record["pid"]), signal.SIGTERM)
    Path(record["endpoint"]).unlink(missing_ok=True)
    path.unlink(missing_ok=True)


def record_for_row(row_id: str, registry_dir: Path = REGISTRY_DIR) -> Path | None:
    """Return the sole owner record for ``row_id``; ambiguity fails closed."""
    matches = []
    for path in registry_dir.glob("*.json"):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if record.get("schema") == SCHEMA_VERSION and record.get("row_id") == row_id:
            matches.append(path)
    if len(matches) > 1:
        raise RuntimeError("Multiple Codex App Server owner records for row")
    return matches[0] if matches else None


def discard_stale_record(path: Path, *, row_id: str) -> bool:
    """Remove one dead owner record after proving its row/path binding.

    This is deliberately not a stop operation: a stale PID is never signalled.
    The endpoint is removed only when the record's schema, row id, registry
    directory, and filename-derived socket name all agree.
    """
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError):
        return False
    if record.get("schema") != SCHEMA_VERSION or record.get("row_id") != row_id:
        return False
    try:
        endpoint = Path(record["endpoint"])
    except (KeyError, TypeError):
        return False
    if endpoint.parent != path.parent or endpoint.name != f"{path.stem}.sock":
        return False
    path.unlink(missing_ok=True)
    endpoint.unlink(missing_ok=True)
    return True


def stop_owned_for_row(row_id: str, registry_dir: Path = REGISTRY_DIR) -> bool:
    """Stop only the uniquely registered owner for a row; missing is a no-op."""
    path = record_for_row(row_id, registry_dir)
    if path is None:
        return False
    stop_owned(path, row_id=row_id)
    return True


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
