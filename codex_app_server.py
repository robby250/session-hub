"""Fail-closed Codex App Server launch and ownership primitives (task-2194 row518).

This module deliberately contains no Qt, tmux, or terminal calls.  Session Hub owns one
private Unix endpoint per saved group row and publishes its process identity atomically.
"""
from __future__ import annotations

import json
import hashlib
import os
import re
import secrets
import signal
import subprocess
import socket
import time
from pathlib import Path

SCHEMA_VERSION = 1
REGISTRY_DIR = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "session-hub" / "codex-app-server"


def endpoint_for(row_id: str, root: Path = REGISTRY_DIR) -> Path:
    # AF_UNIX paths are capped at roughly 104-108 bytes.  Managed row ids contain the provider,
    # full native session UUID and sometimes group identity, so embedding them in the filename can
    # exceed SUN_LEN before Codex even binds.  The full row id remains authoritative inside the
    # owner record; the pathname needs only a collision-resistant, bounded private token.
    digest = hashlib.sha256(row_id.encode("utf-8")).hexdigest()[:16]
    endpoint = root / f"c-{digest}-{secrets.token_hex(6)}.sock"
    if len(os.fsencode(endpoint)) >= 104:
        raise RuntimeError("Codex App Server registry path is too long for a Unix socket")
    return endpoint


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

def _read_owner_record(path: Path) -> dict:
    """Read the ownership authority strictly; malformed state is never treated as absence."""
    try:
        record = json.loads(path.read_text())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"malformed Codex App Server owner record: {path}") from error
    if not isinstance(record, dict):
        raise RuntimeError("Codex App Server owner record must be a JSON object")
    required = {"schema", "row_id", "endpoint", "thread_id", "pid", "start_time"}
    optional = {
        "name", "aliases", "remote_pid", "remote_start_time", "remote_window_id",
    }
    if set(record) - required - optional or not required.issubset(record):
        raise RuntimeError("Codex App Server owner record has the wrong shape")
    if (
        record["schema"] != SCHEMA_VERSION
        or not isinstance(record["row_id"], str)
        or not isinstance(record["endpoint"], str)
        or not isinstance(record["thread_id"], str)
        or not isinstance(record["pid"], int)
        or isinstance(record["pid"], bool)
        or not isinstance(record["start_time"], str)
        or ("name" in record and not isinstance(record["name"], str))
        or ("aliases" in record and (
            not isinstance(record["aliases"], list)
            or any(not isinstance(alias, str) for alias in record["aliases"])
        ))
        or ("remote_pid" in record and (
            not isinstance(record["remote_pid"], int) or isinstance(record["remote_pid"], bool)
        ))
        or ("remote_start_time" in record and not isinstance(record["remote_start_time"], str))
        or ("remote_window_id" in record and not isinstance(record["remote_window_id"], str))
    ):
        raise RuntimeError("Codex App Server owner record has invalid fields")
    return record


def live_record(path: Path, *, row_id: str) -> dict:
    record = _read_owner_record(path)
    if record["row_id"] != row_id:
        raise RuntimeError("Codex App Server owner record mismatch")
    pid = record["pid"]
    if not record["start_time"] or process_start_time(pid) != record["start_time"]:
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
        record = _read_owner_record(path)
        if record["row_id"] == row_id:
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
    record = _read_owner_record(path)
    if record["row_id"] != row_id:
        return False
    endpoint = Path(record["endpoint"])
    if endpoint.parent != path.parent or endpoint.name != f"{path.stem}.sock":
        return False
    # A stale PID cannot prove that the pathname still belongs to this owner. Retire only the
    # registry claim; never unlink a replacement process's socket at the same endpoint.
    path.unlink(missing_ok=True)
    return True


def live_remote_owner_names(registry_dir: Path = REGISTRY_DIR) -> dict[str, str]:
    """Return exact row->tmux owners proven live by one bounded registry scan.

    App Server liveness alone is insufficient: only a record with a live, start-time-bound
    remote client and a tmux window identity can make a row appear Running. Duplicate owner
    claims are omitted rather than guessed - by row id (live_owner_records) AND by tmux NAME
    here: two records claiming one name is the split-identity state row772 hit live, where the
    Running tab silently believed one record while peer addressing refused the name as
    ambiguous. Dropping both is what makes the two disagreements the same visible fault.
    """
    names = {
        row_id: record["name"] for row_id, record in live_owner_records(registry_dir).items()
    }
    claims: dict[str, int] = {}
    for name in names.values():
        claims[name] = claims.get(name, 0) + 1
    return {row_id: name for row_id, name in names.items() if claims[name] == 1}


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


_ROLLOUT_ID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$"
)


def open_rollout_keys(pid: int, sessions_root: Path, proc_root: Path = Path("/proc")) -> list[str]:
    """Codex transcript keys whose rollout JSONL is open on ``pid``, newest write first.

    In App Server mode the tmux pane runs a thin ``codex --remote`` client that holds NO
    rollout; the transcript is open on the detached ``codex app-server`` process, which is a
    child of Session Hub and never a descendant of the pane.  Every /proc-descendant walk in
    session_hub.py therefore resolves an App Server row to None, which is why such a row could
    not auto-link, could not appear in Running, and could not be addressed by name.  This walks
    the ONE pid the owner record already names, so ownership needs no discovery at all.

    Ordered by rollout mtime, newest first: a single app-server holding more than one open
    rollout (mid-/clear, or a fork) is on the most recently written one.  An empty list is
    itself meaningful - an app-server with no open rollout has no conversation to lose and is
    the exact signature of an orphan left behind by a remote client that died at startup.
    """
    found: list[tuple[float, str]] = []
    try:
        descriptors = list((proc_root / str(pid) / "fd").iterdir())
    except OSError:
        return []
    for descriptor in descriptors:
        try:
            target = descriptor.resolve(strict=True)
            target.relative_to(sessions_root)
            mtime = target.stat().st_mtime
        except (OSError, ValueError):
            continue
        match = _ROLLOUT_ID_RE.search(target.name)
        if match:
            found.append((mtime, f"Codex:{match.group(1)}"))
    seen: set[str] = set()
    ordered = []
    for _mtime, key in sorted(found, reverse=True):
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


def live_owner_records(registry_dir: Path = REGISTRY_DIR) -> dict[str, dict]:
    """{row_id: record} for every owner whose App Server AND remote client are both live.

    Same liveness contract as live_remote_owner_names (which returns only the names), exposed
    whole so a caller that needs the App Server pid or the record path - the identity census and
    the /clear reconciler - does not re-scan the registry a second time.  Duplicate row_id claims
    are omitted rather than guessed, exactly as before.
    """
    candidates: dict[str, list[dict]] = {}
    for path in sorted(registry_dir.glob("*.json")):
        try:
            raw = _read_owner_record(path)
            record = live_record(path, row_id=raw["row_id"])
        except (OSError, RuntimeError, ValueError):
            continue
        remote_pid = record.get("remote_pid")
        remote_start = record.get("remote_start_time")
        if (
            not isinstance(remote_pid, int)
            or isinstance(remote_pid, bool)
            or not remote_start
            or process_start_time(remote_pid) != remote_start
            or not record.get("remote_window_id")
            or not record.get("name")
        ):
            continue
        candidates.setdefault(record["row_id"], []).append({**record, "path": path})
    return {
        row_id: records[0]
        for row_id, records in candidates.items()
        if len(records) == 1
    }


def save_owner_thread_id(path: Path, *, row_id: str, thread_id: str) -> bool:
    """Persist the thread this owner's App Server is CURRENTLY on, atomically.

    The record is the only place a thread change survives across refreshes, and it is bound to
    one start-time-verified process, so comparing the stored value against the live rollout is
    what makes a /clear detectable without guessing from cwd or recency.  Refuses to write
    through a record that is not exactly this row's.
    """
    record = _read_owner_record(path)
    if record["row_id"] != row_id or record["thread_id"] == thread_id:
        return False
    record["thread_id"] = thread_id
    tmp = path.with_name(f".{path.name}.thread.tmp")
    tmp.write_text(json.dumps(record, sort_keys=True) + "\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return True


def retire_orphan_owner(path: Path, *, row_id: str, sessions_root: Path) -> bool:
    """Retire an owner record whose App Server holds no conversation, and stop that server.

    Only reachable for a record whose remote client is already provably dead by start time.  An
    App Server with zero open rollouts has nothing to lose, so this is the recovery path out of
    the permanent lockout task row772 hit live: a dead remote plus a reserved window owned by a
    DIFFERENT record made launch_exact refuse forever with no way back through the UI.  Every
    other ambiguous or still-working case keeps failing closed.
    """
    record = _read_owner_record(path)
    if record["row_id"] != row_id:
        return False
    endpoint = Path(record["endpoint"])
    if endpoint.parent != path.parent or endpoint.name != f"{path.stem}.sock":
        return False
    pid = record["pid"]
    if not record["start_time"] or process_start_time(pid) != record["start_time"]:
        # Not our process any more; retire the claim only, never signal a stranger.
        path.unlink(missing_ok=True)
        return True
    if open_rollout_keys(pid, sessions_root):
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        pass
    endpoint.unlink(missing_ok=True)
    path.unlink(missing_ok=True)
    return True
