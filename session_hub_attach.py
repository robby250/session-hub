#!/usr/bin/env python3
"""Fast, screen-inert Session Hub attach-or-launch command.

The existing-session path intentionally does only one exact tmux liveness check before
replacing this process with ``tmux attach-session``.  Metadata/provider resolution is only
needed when the target is absent, which keeps a phone/SSH snippet as quick as a direct tmux
attach in the common case and avoids importing Qt altogether.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Callable

from session_hub_control import ControlError, SessionHubController


_TMUX_NAME_UNSAFE = re.compile(r"[.:]")
_FLAG_ONLY = frozenset({"--chrome"})


class AttachError(RuntimeError):
    """A user-facing attach/launch failure."""


def sanitize_tmux_session_name(name: str) -> str:
    """Match Session Hub's canonical tmux identity substitution."""
    return _TMUX_NAME_UNSAFE.sub("_", name)


def exact_target(name: str) -> str:
    """Force tmux to match a session name exactly, never as a prefix."""
    return f"={name}"


def _read_metadata(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise AttachError(f"cannot read Session Hub metadata: {path}") from error
    if not isinstance(data, dict):
        raise AttachError("Session Hub metadata must be an object")
    return data


def _tmux_run(tmux: str, args: list[str], run: Callable) -> object:
    try:
        return run(
            [tmux, *args],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as error:
        raise AttachError(f"tmux command failed: {error}") from error


def _has_session(tmux: str, name: str, run: Callable) -> bool:
    result = _tmux_run(tmux, ["has-session", "-t", exact_target(name)], run)
    return result.returncode == 0


def _attach(tmux: str, name: str, execvp: Callable) -> int:
    # Successful real invocations never return: the SSH/Terminus process becomes tmux.
    execvp(tmux, [tmux, "attach-session", "-t", exact_target(name)])
    return 0


def _group_sources(metadata: dict, cwd: str, override_key: str | None) -> tuple[dict, dict, dict]:
    settings = metadata.get("settings") or {}
    group = (metadata.get("groups") or {}).get(cwd) or {}
    session_entry = (metadata.get("sessions") or {}).get(override_key or "") or {}
    return (
        settings if isinstance(settings, dict) else {},
        group if isinstance(group, dict) else {},
        session_entry if isinstance(session_entry, dict) else {},
    )


def _env_overrides(metadata: dict, cwd: str, override_key: str, *, strip: list[str]) -> tuple[dict[str, str], list[str]]:
    settings, group, session_entry = _group_sources(metadata, cwd, override_key)
    combined: dict[str, str] = {}
    for source in (
        settings.get("global_env") or {},
        group.get("env") or {},
        session_entry.get("env") or {},
    ):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            if str(key).strip():
                combined[str(key)] = str(value)
    # A tmux server may predate this SSH invocation.  Put configured values in the command
    # itself, and explicitly unset inherited child-session state just like the GUI launch path.
    return combined, [str(key) for key in strip]


def _flag_args(metadata: dict, cwd: str, override_key: str, extra: dict[str, object] | None = None) -> list[str]:
    settings, group, session_entry = _group_sources(metadata, cwd, override_key)
    combined: dict[str, object] = {}
    for source in (
        settings.get("global_flags") or {},
        group.get("flags") or {},
        session_entry.get("flags") or {},
        extra or {},
    ):
        if isinstance(source, dict):
            combined.update(source)
    result: list[str] = []
    for flag, value in combined.items():
        flag = str(flag).strip()
        if not flag:
            continue
        # --caveman is a Session Hub pseudo-flag expanded by the Qt launch path.  It is not
        # safe to pass it to Claude literally; the attach command's contract is to preserve
        # real CLI flags and the normal global env, while unknown pseudo-flags are ignored.
        if flag == "--caveman":
            continue
        if flag in _FLAG_ONLY or value in (None, "", True):
            result.append(flag)
        else:
            result.extend((flag, str(value)))
    return result


def _configured_command_env(
    metadata: dict,
    cwd: str,
    override_key: str,
    *,
    strip: list[str],
) -> list[str]:
    overrides, unsets = _env_overrides(metadata, cwd, override_key, strip=strip)
    prefix = ["env"]
    for key in unsets:
        prefix.extend(("-u", key))
    for key, value in overrides.items():
        if key in unsets:
            continue
        prefix.append(f"{key}={value}")
    return prefix


def _claude_executable(which: Callable[[str], str | None]) -> str:
    return which("claude") or str(Path.home() / ".local" / "bin" / "claude")


def _launch_claude(
    metadata: dict,
    *,
    cwd: str,
    name: str,
    override_key: str,
    session_id: str | None,
    transcripts: object,
    which: Callable[[str], str | None],
    run: Callable,
    tmux: str,
) -> None:
    if not Path(cwd).is_dir():
        raise AttachError(f"working directory does not exist: {cwd}")
    settings, _group, session_entry = _group_sources(metadata, cwd, override_key)
    args = [_claude_executable(which)]
    if settings.get("claude_danger_mode", False):
        args.append("--dangerously-skip-permissions")
    if session_id:
        args.extend(("--resume", session_id))
    else:
        args.extend(_flag_args(metadata, cwd, override_key, {"--name": name}))
    if session_id:
        # Saved per-session --name/other flags still apply when resuming; the GUI's launch
        # path resolves them from the same override bucket.
        args[1:1] = _flag_args(metadata, cwd, override_key)
    strip = ["CLAUDE_CODE_CHILD_SESSION"] if transcripts is not False else []
    command = _configured_command_env(metadata, cwd, override_key, strip=strip) + args
    command_string = shlex.join(command)

    if _has_session(tmux, name, run):
        return
    created = _tmux_run(
        tmux,
        ["new-session", "-d", "-s", name, "-c", cwd, command_string],
        run,
    )
    if created.returncode != 0 and not _has_session(tmux, name, run):
        detail = (created.stderr or "").strip()
        raise AttachError(detail or f"could not launch Claude session {name!r}")
    # Keep the same visible identity as desktop launches.  These are best-effort server options;
    # failure here must not strand an otherwise usable agent session.
    for option, value in (
        ("set-titles", "on"),
        ("set-titles-string", "#S"),
        ("status-left", "#S "),
        ("status-left-length", "40"),
        ("status-right", ""),
        ("focus-events", "off"),
    ):
        _tmux_run(tmux, ["set-option", "-t", exact_target(name), option, value], run)


def _group_matches(metadata: dict, wanted: str) -> list[dict]:
    matches: list[dict] = []
    groups = metadata.get("groups") or {}
    if not isinstance(groups, dict):
        return matches
    for cwd, group in groups.items():
        if not isinstance(cwd, str) or not isinstance(group, dict):
            continue
        rows = group.get("rows") or []
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict) or row.get("name") != wanted:
                continue
            matches.append({
                "kind": "group",
                "cwd": cwd,
                "name": wanted,
                "provider": row.get("provider", "Claude"),
                "session_key": row.get("session_key"),
                "override_key": row.get("override_key") or f"group:{cwd}#{wanted}",
                "transcripts": row.get("transcripts", True),
            })
    return matches


def _standalone_snapshot() -> dict:
    """Resolve standalone names only on the slow missing-target path.

    The existing-target path never reaches this function.  Reusing the canonical JSON discovery
    command keeps transcript/provider matching in one place instead of making this fast helper
    maintain a second parser for Codex SQLite and Claude transcript layouts.
    """
    command = [sys.executable, str(Path(__file__).with_name("session_hub.py")), "--sessions-json"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=15)
        data = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError, TypeError) as error:
        raise AttachError(f"could not enumerate Session Hub sessions: {error}") from error
    if not isinstance(data, dict):
        raise AttachError("Session Hub returned invalid session data")
    return data


def _standalone_matches(metadata: dict, wanted: str, snapshot: dict) -> list[dict]:
    matches: list[dict] = []
    overrides = metadata.get("sessions") or {}
    for item in snapshot.get("sessions") or []:
        if not isinstance(item, dict) or item.get("is_group"):
            continue
        key = item.get("key")
        custom = (overrides.get(key) or {}).get("name") if isinstance(overrides, dict) else None
        candidates = {str(value) for value in (custom, item.get("title"), item.get("tmux_name"), key, item.get("session_id")) if value}
        if wanted not in candidates:
            continue
        name = str(custom or item.get("tmux_name") or item.get("title") or wanted)
        matches.append({
            "kind": "standalone",
            "cwd": item.get("cwd"),
            "name": name,
            "provider": item.get("provider"),
            "session_key": key,
            "session_id": item.get("session_id"),
            "override_key": key,
            "transcripts": True,
        })
    return matches


def _resolve_target(metadata: dict, wanted: str, *, standalone_snapshot: Callable[[], dict]) -> dict:
    matches = _group_matches(metadata, wanted)
    if not matches:
        matches = _standalone_matches(metadata, wanted, standalone_snapshot())
    if not matches:
        raise AttachError(
            f"No Session Hub session named {wanted!r}. Use the exact Name shown in Session Hub."
        )
    if len(matches) > 1:
        locations = ", ".join(str(item.get("cwd") or "standalone") for item in matches)
        raise AttachError(f"{wanted!r} is ambiguous ({locations}); use the unique Session Hub name.")
    return matches[0]


def attach_or_launch(
    wanted: str,
    metadata_path: Path,
    *,
    run: Callable = subprocess.run,
    execvp: Callable = os.execvp,
    which: Callable[[str], str | None] = shutil.which,
    standalone_snapshot: Callable[[], dict] | None = None,
) -> int:
    raw = str(wanted).strip()
    if not raw:
        raise AttachError("usage: session-hub attach <name>")
    canonical = sanitize_tmux_session_name(raw)
    if canonical != raw or raw.startswith("="):
        raise AttachError("session name must be the exact canonical Session Hub name")
    tmux = which("tmux")
    if not tmux:
        raise AttachError("tmux is not installed")

    # This is deliberately before metadata I/O: existing sessions are the common phone path.
    if _has_session(tmux, canonical, run):
        return _attach(tmux, canonical, execvp)

    metadata = _read_metadata(metadata_path)
    target = _resolve_target(
        metadata,
        canonical,
        standalone_snapshot=standalone_snapshot or _standalone_snapshot,
    )
    provider = target.get("provider")
    if provider == "Codex":
        cwd = target.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            raise AttachError("Codex session has no working directory")
        row_id = target.get("override_key") or target.get("session_key")
        if not isinstance(row_id, str) or not row_id:
            raise AttachError("Codex session has no stable ownership key")
        session_id = target.get("session_id") or target.get("session_key")
        if isinstance(session_id, str) and session_id.startswith("Codex:"):
            session_id = session_id.split(":", 1)[1]
        if session_id is not None and not isinstance(session_id, str):
            session_id = None
        try:
            SessionHubController(metadata_path).launch_exact(
                row_id=row_id,
                name=canonical,
                cwd=cwd,
                thread_id=session_id,
                process_cwd=cwd,
            )
        except (ControlError, OSError, RuntimeError, ValueError) as error:
            raise AttachError(str(error)) from error
    elif provider == "Claude":
        session_key = target.get("session_key")
        session_id = session_key.split(":", 1)[1] if isinstance(session_key, str) and session_key.startswith("Claude:") else None
        cwd = target.get("cwd")
        if not isinstance(cwd, str) or not cwd:
            raise AttachError("Claude session has no working directory")
        _launch_claude(
            metadata,
            cwd=cwd,
            name=canonical,
            override_key=str(target.get("override_key") or session_key or ""),
            session_id=session_id,
            transcripts=target.get("transcripts", True),
            which=which,
            run=run,
            tmux=tmux,
        )
    else:
        raise AttachError(
            f"{canonical!r} uses provider {provider or 'unknown'}, which is not tmux-managed"
        )
    if not _has_session(tmux, canonical, run):
        raise AttachError(f"Session Hub could not create tmux session {canonical!r}")
    return _attach(tmux, canonical, execvp)


def cli(argv: list[str], metadata_path: Path, **kwargs) -> int:
    if len(argv) < 3 or argv[1] not in {"attach", "--attach", "--attach-session"}:
        print("usage: session-hub attach <name>", file=sys.stderr)
        return 2
    try:
        return attach_or_launch(argv[2], metadata_path, **kwargs)
    except AttachError as error:
        print(f"session-hub: {error}", file=sys.stderr)
        return 2
