"""Screen-inert Session Hub lifecycle controls.

This module intentionally has no Qt imports.  It is the single lifecycle boundary used by
the CLI and by the GUI's Codex group-row actions.  All subprocesses use argv lists; callers
may inject ``run``/``popen`` for pure tests.
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Callable

from codex_app_server import (
    REGISTRY_DIR, app_server_argv, discard_stale_record, endpoint_for,
    live_record, publish_record, record_for_row, remote_tui_argv,
    retire_orphan_owner, stop_owned, wait_ready,
)

REMOTE_WINDOW = "__session_hub_codex_remote__"
CODEX_SESSIONS = Path.home() / ".codex" / "sessions"
_TMUX_NAME_UNSAFE = re.compile(r"[.:]")


class ControlError(RuntimeError):
    pass


def _start_time(pid: int) -> str:
    try:
        return Path(f"/proc/{pid}/stat").read_text().split()[21]
    except (OSError, IndexError):
        return ""


class SessionHubController:
    def __init__(self, metadata_path: Path, *, registry_dir: Path = REGISTRY_DIR,
                 run: Callable | None = None, popen: Callable | None = None,
                 which: Callable = lambda name: name):
        self.metadata_path = Path(metadata_path)
        self.registry_dir = Path(registry_dir)
        self.run = run or subprocess.run
        self.popen = popen or subprocess.Popen
        self.which = which

    def _metadata(self) -> dict:
        try:
            data = json.loads(self.metadata_path.read_text())
        except (OSError, ValueError) as error:
            raise ControlError(f"cannot read Session Hub metadata: {self.metadata_path}") from error
        if not isinstance(data, dict):
            raise ControlError("Session Hub metadata must be an object")
        return data

    def _row(self, cwd: str, name: str) -> tuple[dict, str]:
        metadata = self._metadata()
        group = metadata.get("groups", {}).get(cwd)
        if not isinstance(group, dict):
            raise ControlError(f"No session group for {cwd!r}")
        rows = group.get("rows", [])
        matches = [row for row in rows if isinstance(row, dict) and row.get("name") == name]
        if len(matches) != 1:
            raise ControlError(
                f"No row named {name!r} in this group" if not matches
                else f"Ambiguous row named {name!r} in this group"
            )
        row = matches[0]
        if row.get("provider", "Claude") != "Codex":
            raise ControlError("headless row control currently supports Codex rows only")
        row_id = row.get("override_key") or f"group:{cwd}#{name}"
        if not isinstance(row_id, str) or not row_id:
            raise ControlError("row has no stable ownership key")
        return row, row_id

    def _tmux(self, *args: str, check: bool = False):
        tmux = self.which("tmux")
        if not tmux:
            raise ControlError("tmux is not installed")
        return self.run([tmux, *args], capture_output=True, text=True, check=check, timeout=5)

    def _windows(self, name: str) -> list[tuple[str, str]]:
        result = self._tmux("list-windows", "-t", f"={name}", "-F", "#{window_id}\t#{window_name}")
        if result.returncode:
            return []
        return [tuple(line.split("\t", 1)) for line in result.stdout.splitlines() if "\t" in line]

    def _ensure_remote_window(self, name: str, cwd: str, endpoint: Path,
                              thread_id: str | None, *, allow_existing: bool = False) -> tuple[str, bool]:
        windows = self._windows(name)
        prior_ids = {wid for wid, _ in windows}
        existing = [(wid, wname) for wid, wname in windows if wname == REMOTE_WINDOW]
        if len(existing) > 1:
            raise ControlError("ambiguous Session Hub remote-client windows for row")
        if existing and not allow_existing:
            raise ControlError("reserved Codex remote window exists without an owner record")
        args = remote_tui_argv(endpoint, thread_id, cwd)
        if existing:
            return existing[0][0], False
        tmux = self.which("tmux")
        probe = self._tmux("has-session", "-t", f"={name}")
        command = (
            [tmux, "new-window", "-d", "-t", f"={name}", "-n", REMOTE_WINDOW, "--", *args]
            if probe.returncode == 0
            else [tmux, "new-session", "-d", "-s", name, "-c", cwd,
                  "-n", REMOTE_WINDOW, "--", *args]
        )
        result = self.run(command, capture_output=True, text=True, check=False, timeout=5)
        if result.returncode:
            raise ControlError(result.stderr.strip() or "could not create Codex tmux client")
        windows = self._windows(name)
        created = [(wid, wname) for wid, wname in windows if wname == REMOTE_WINDOW]
        if len(created) != 1:
            new_reserved = [(wid, wname) for wid, wname in created if wid not in prior_ids]
            if len(new_reserved) == 1:
                self._remove_created_remote_window(name, new_reserved[0][0])
            raise ControlError("new Codex remote window could not be identified")
        return created[0][0], True

    def _remote_pid(self, session: str, window_id: str) -> int:
        result = self._tmux("list-panes", "-t", f"={session}:{window_id}", "-F", "#{pane_pid}")
        pids = [line.strip() for line in result.stdout.splitlines() if line.strip().isdigit()]
        if result.returncode or len(pids) != 1:
            raise ControlError("Codex remote window has no unique process")
        return int(pids[0])

    def _validate_remote_identity(self, session: str, owner: dict) -> None:
        """Require the reserved window and pane to match the persisted Hub owner."""
        window_id = owner.get("remote_window_id")
        remote_pid = owner.get("remote_pid")
        remote_start = owner.get("remote_start_time")
        if (not isinstance(window_id, str) or not isinstance(remote_pid, int)
                or isinstance(remote_pid, bool)):
            raise ControlError("owned Codex remote client has no complete process identity")
        if not isinstance(remote_start, str) or not remote_start:
            raise ControlError("owned Codex remote client has no start-time identity")
        windows = [(wid, wname) for wid, wname in self._windows(session)
                   if wid == window_id and wname == REMOTE_WINDOW]
        if len(windows) != 1:
            raise ControlError("owned Codex remote window is missing or mismatched")
        current_pid = self._remote_pid(session, window_id)
        if current_pid != remote_pid or _start_time(current_pid) != remote_start:
            raise ControlError("owned Codex remote client is stale or PID was reused")

    def _remote_identity_can_be_recreated(self, session: str, owner: dict) -> bool:
        """True only when the previously-owned remote client is provably gone.

        Ctrl+C in an attached Codex row exits the remote client.  When it was the
        tmux session's last window, tmux removes the session too, while the detached
        App Server correctly remains alive.  A later Launch must be able to recreate
        that client without weakening the ownership checks for a still-live,
        replaced, or ambiguous window.
        """
        window_id = owner.get("remote_window_id")
        remote_pid = owner.get("remote_pid")
        remote_start = owner.get("remote_start_time")
        if (
            not isinstance(window_id, str)
            or not isinstance(remote_pid, int)
            or isinstance(remote_pid, bool)
            or not isinstance(remote_start, str)
            or not remote_start
        ):
            return False
        # Never adopt or replace an unbound reserved window.  Only the exact
        # absence left by the dead owned client is recoverable.
        if any(window_name == REMOTE_WINDOW for _wid, window_name in self._windows(session)):
            return False
        # A reused PID is not signalled or trusted; it merely proves that the old
        # process identity no longer exists and its stale record may be replaced.
        return _start_time(remote_pid) != remote_start

    def _retire_if_orphaned(self, path: Path, row_id: str, owner: dict) -> bool:
        """Retire an owner record left behind by a remote client that never really ran.

        Both conditions must hold, and neither is a heuristic: the remote client is gone by
        start-time comparison (never by signalling it), and the App Server has no rollout JSONL
        open, so it is holding no conversation.  That pair is the exact signature of a remote
        launched with an invalid thread id - see _thread_id - which exits at startup and strands
        its server.  Anything still working, ambiguous, or merely detached keeps failing closed.
        """
        remote_pid = owner.get("remote_pid")
        remote_start = owner.get("remote_start_time")
        if (
            not isinstance(remote_pid, int)
            or isinstance(remote_pid, bool)
            or not isinstance(remote_start, str)
            or not remote_start
            or _start_time(remote_pid) == remote_start
        ):
            return False
        try:
            return retire_orphan_owner(path, row_id=row_id, sessions_root=CODEX_SESSIONS.resolve())
        except (OSError, RuntimeError, ValueError):
            return False

    def _remove_created_remote_window(self, session: str, window_id: str) -> None:
        """Kill only a newly-created reserved window after proving its exact identity."""
        windows = [(wid, wname) for wid, wname in self._windows(session)
                   if wid == window_id and wname == REMOTE_WINDOW]
        if len(windows) != 1:
            return
        self._tmux("kill-window", "-t", f"={session}:{window_id}", check=False)

    @staticmethod
    def _save_remote_identity(path: Path, *, pid: int, window_id: str) -> dict:
        record = json.loads(path.read_text())
        if not isinstance(record, dict):
            raise ControlError("malformed Codex App Server owner record")
        record["remote_pid"] = pid
        record["remote_start_time"] = _start_time(pid)
        record["remote_window_id"] = window_id
        if not record["remote_start_time"]:
            raise ControlError("Codex remote client has no process identity")
        tmp = path.with_name(f".{path.name}.remote.tmp")
        tmp.write_text(json.dumps(record, sort_keys=True) + "\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        return record

    def _owner_path(self, row_id: str) -> Path | None:
        return record_for_row(row_id, self.registry_dir)

    def _thread_id(self, row: dict) -> str | None:
        """The Codex thread id this row resumes, or None to start a fresh one.

        A non-``Codex:`` session_key is NOT a thread id and must never be passed through.  A row
        mid-swap (Continue with other agent) still carries the Claude key it came from, and
        returning it verbatim built ``codex --remote ... resume Claude:<uuid>`` - an invalid
        thread id whose remote client died on startup, leaving an orphaned App Server whose
        record then blocked every later Launch of the row (row772 live incident, 2026-09-04).
        """
        key = row.get("session_key")
        if not isinstance(key, str) or not key.startswith("Codex:"):
            return None
        return key.split(":", 1)[1] or None

    def status(self, cwd: str, name: str) -> dict:
        row, row_id = self._row(cwd, name)
        owner_path = self._owner_path(row_id)
        if owner_path is None:
            return {"status": "stopped", "name": name, "row_id": row_id}
        try:
            owner = live_record(owner_path, row_id=row_id)
        except (OSError, ValueError, ControlError, RuntimeError) as error:
            return {"status": "error", "name": name, "row_id": row_id, "message": str(error)}
        try:
            self._validate_remote_identity(name, owner)
        except (ControlError, OSError, RuntimeError, ValueError) as error:
            return {"status": "error", "name": name, "row_id": row_id, "message": str(error)}
        return {"status": "running", "name": name, "row_id": row_id,
                "thread_id": owner["thread_id"], "window_id": owner["remote_window_id"]}

    def launch_exact(
        self, *, row_id: str, name: str, cwd: str, thread_id: str | None,
        process_cwd: str | None = None,
    ) -> dict:
        """Launch one exact saved identity into tmux without constructing a GUI."""
        name = _TMUX_NAME_UNSAFE.sub("_", name)
        if not row_id or not name:
            raise ControlError("Codex launch requires an exact row id and tmux name")
        if not Path(cwd).is_dir():
            raise ControlError(f"working directory does not exist: {cwd}")
        if process_cwd and not Path(process_cwd).is_dir():
            raise ControlError(f"working directory does not exist: {process_cwd}")
        existing = self._owner_path(row_id)
        if existing is not None:
            try:
                owner = live_record(existing, row_id=row_id)
            except RuntimeError:
                # A strictly valid, exactly bound stale claim is safe to retire. Malformed,
                # cross-row, path-mismatched, or ambiguous state still raises and fails closed.
                if not discard_stale_record(existing, row_id=row_id):
                    raise ControlError("Codex App Server owner record does not match this row")
                existing = None
            else:
                try:
                    self._validate_remote_identity(name, owner)
                    allow_existing = True
                except ControlError:
                    if not self._remote_identity_can_be_recreated(name, owner):
                        # Last resort before failing closed: a record whose remote client is
                        # provably dead and whose App Server holds NO open rollout owns no
                        # conversation, so retiring it loses nothing.  Without this the row772
                        # state is a PERMANENT lockout - the dead owner's window is gone, but a
                        # DIFFERENT record's reserved window in the same tmux session makes
                        # _remote_identity_can_be_recreated refuse, and Stop refuses for the
                        # same reason, so the UI offers no way back at all.
                        if not self._retire_if_orphaned(existing, row_id, owner):
                            raise
                        existing = None
                    else:
                        allow_existing = False
                if existing is not None:
                    window_id, created = self._ensure_remote_window(
                        name, cwd, Path(owner["endpoint"]), owner.get("thread_id") or None,
                        allow_existing=allow_existing,
                    )
                    owner = self._save_remote_identity(
                        existing, pid=self._remote_pid(name, window_id), window_id=window_id
                    )
                    return {"status": "resumed" if owner["thread_id"] else "running", "name": name,
                            "row_id": row_id, "thread_id": owner["thread_id"],
                            "window_id": window_id, "reused": True,
                            "window_created": created}

        self.registry_dir.mkdir(parents=True, exist_ok=True)
        endpoint = endpoint_for(row_id, self.registry_dir)
        record_path = endpoint.with_suffix(".json")
        server = self.popen(
            app_server_argv(endpoint, cwd), cwd=process_cwd or cwd,
            env={k: v for k, v in os.environ.items() if k != "TMUX"},
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True,
        )
        created_window_id = None
        try:
            if not wait_ready(endpoint):
                raise ControlError("Codex App Server did not become ready")
            owner = publish_record(record_path, row_id=row_id, endpoint=endpoint,
                                   thread_id=thread_id or "", process=server,
                                   name=name, aliases=[name])
            window_id, created = self._ensure_remote_window(
                name, cwd, endpoint, thread_id
            )
            created_window_id = window_id if created else None
            owner = self._save_remote_identity(
                record_path, pid=self._remote_pid(name, window_id), window_id=window_id
            )
            return {"status": "resumed" if thread_id else "launched", "name": name,
                    "row_id": row_id, "thread_id": owner["thread_id"], "window_id": window_id}
        except Exception:
            if created_window_id:
                try:
                    self._remove_created_remote_window(name, created_window_id)
                except (ControlError, OSError, RuntimeError, ValueError):
                    pass
            try:
                server.terminate()
            except OSError:
                pass
            record_path.unlink(missing_ok=True)
            endpoint.unlink(missing_ok=True)
            raise

    def launch(self, cwd: str, name: str, *, resume: bool = False) -> dict:
        row, row_id = self._row(cwd, name)
        return self.launch_exact(
            row_id=row_id, name=name, cwd=cwd,
            thread_id=self._thread_id(row), process_cwd=cwd,
        )

    def stop(self, cwd: str, name: str) -> dict:
        _, row_id = self._row(cwd, name)
        owner_path = self._owner_path(row_id)
        if owner_path is None:
            return {"status": "stopped", "name": name, "row_id": row_id, "already_stopped": True}
        owner = live_record(owner_path, row_id=row_id)
        try:
            self._validate_remote_identity(name, owner)
        except ControlError:
            # Same recovery launch_exact takes: a dead remote plus a conversation-less App
            # Server is an orphan, and Stop refusing it too is what left row772 with no way out
            # of the lockout from either direction.
            if not self._retire_if_orphaned(owner_path, row_id, owner):
                raise
            return {"status": "stopped", "name": name, "row_id": row_id, "already_stopped": True}
        # Kill only the Hub-owned remote pane's process, then the Hub-owned App Server. The
        # session and all composer/legacy panes remain untouched.
        remote_pid = owner.get("remote_pid")
        if isinstance(remote_pid, int) and _start_time(remote_pid) == owner.get("remote_start_time"):
            os.kill(remote_pid, signal.SIGTERM)
        stop_owned(owner_path, row_id=row_id)
        return {"status": "stopped", "name": name, "row_id": row_id}


def cli(argv: list[str], metadata_path: Path) -> int:
    controller = SessionHubController(metadata_path)
    try:
        if "--launch-group-row" in argv:
            i = argv.index("--launch-group-row"); result = controller.launch(argv[i + 1], argv[i + 2])
        elif "--resume-group-row" in argv:
            i = argv.index("--resume-group-row"); result = controller.launch(argv[i + 1], argv[i + 2], resume=True)
        elif "--stop-group-row" in argv:
            i = argv.index("--stop-group-row"); result = controller.stop(argv[i + 1], argv[i + 2])
        elif "--status-group-row" in argv:
            i = argv.index("--status-group-row"); result = controller.status(argv[i + 1], argv[i + 2])
        else:
            return 2
    except (IndexError, ControlError, OSError, RuntimeError, ValueError) as error:
        result = {"status": "error", "message": str(error)}
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status") != "error" else 1
