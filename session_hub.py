#!/usr/bin/env python3
"""Desktop launcher for local Codex, Claude Code, and Antigravity sessions."""

from __future__ import annotations

import json
import hashlib
import fcntl
import os
import pty
import re
import select
import shutil
import sqlite3
import struct
import subprocess
import sys
import termios
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QByteArray, QObject, QRunnable, QThreadPool, QTimer, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QDesktopServices, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
    QInputDialog,
)


HOME = Path.home()
CODEX_SESSIONS = HOME / ".codex" / "sessions"
CODEX_STATE = HOME / ".codex" / "state_5.sqlite"
CLAUDE_PROJECTS = HOME / ".claude" / "projects"
CLAUDE_HISTORY = HOME / ".claude" / "history.jsonl"
ANTIGRAVITY_HOME = HOME / ".gemini" / "antigravity-cli"
ANTIGRAVITY_CONVERSATIONS = ANTIGRAVITY_HOME / "conversations"
ANTIGRAVITY_BRAIN = ANTIGRAVITY_HOME / "brain"
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share")) / "session-hub"
METADATA_PATH = DATA_DIR / "metadata.json"
TRASH_DIR = DATA_DIR / "trash"
HANDOFF_DIR = DATA_DIR / "handoffs"
SUMMARY_DIR = HANDOFF_DIR / "summaries"
APP_ICON = Path(__file__).resolve().parent / "assets" / "session-hub.svg"
PROVIDERS = ("Codex", "Claude", "Antigravity")


@dataclass
class Session:
    provider: str
    session_id: str
    title: str
    cwd: str
    source_cwd: str
    updated_ms: int
    path: Path
    logical_key: str | None = None
    linked_keys: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return self.logical_key or self.native_key

    @property
    def native_key(self) -> str:
        return f"{self.provider}:{self.session_id}"


@dataclass
class UsageWindow:
    name: str
    used_percent: int
    resets: str


def format_reset_timestamp(timestamp: int | None) -> str:
    if not timestamp:
        return "Reset time unavailable"
    value = datetime.fromtimestamp(timestamp)
    return f"Resets {value.strftime('%Y-%m-%d %H:%M')}"


def format_claude_reset(value: str, now: datetime | None = None) -> str:
    """Normalize Claude's English reset text to the same local format as Codex."""
    match = re.fullmatch(
        r"([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{1,2})(?::(\d{2}))?(am|pm)"
        r"(?:\s+\([^)]+\))?",
        value.strip(),
    )
    if not match:
        return f"Resets {value.strip()}"
    month_name, day, hour, minute, meridiem = match.groups()
    months = {
        "Jan": 1,
        "Feb": 2,
        "Mar": 3,
        "Apr": 4,
        "May": 5,
        "Jun": 6,
        "Jul": 7,
        "Aug": 8,
        "Sep": 9,
        "Oct": 10,
        "Nov": 11,
        "Dec": 12,
    }
    current = now or datetime.now()
    hour_value = int(hour) % 12 + (12 if meridiem == "pm" else 0)
    reset = datetime(
        current.year,
        months[month_name],
        int(day),
        hour_value,
        int(minute or 0),
    )
    if reset < current:
        reset = reset.replace(year=current.year + 1)
    return f"Resets {reset.strftime('%Y-%m-%d %H:%M')}"


def parse_claude_usage(text: str) -> list[UsageWindow]:
    pattern = re.compile(
        r"^(Current session|Current week \(all models\)):\s*"
        r"(\d+)% used\s*[·•]\s*resets (.+)$",
        re.MULTILINE,
    )
    return [
        UsageWindow(
            "5-hour" if label == "Current session" else "Weekly",
            max(0, min(100, int(percent))),
            format_claude_reset(reset),
        )
        for label, percent, reset in pattern.findall(text)
    ]


def strip_terminal_codes(text: str) -> str:
    text = re.sub(r"\x1b\][^\x07]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|[()][A-Z0-9]|.)", "", text)
    return text.replace("\r", "\n")


def relative_reset_timestamp(value: str, now: datetime | None = None) -> str:
    hours = re.search(r"(\d+)\s*h", value)
    minutes = re.search(r"(\d+)\s*m", value)
    seconds = (int(hours.group(1)) if hours else 0) * 3600
    seconds += (int(minutes.group(1)) if minutes else 0) * 60
    if not seconds:
        return f"Refreshes in {value.strip()}"
    timestamp = int((now or datetime.now()).timestamp()) + seconds
    return format_reset_timestamp(timestamp)


def parse_antigravity_usage(text: str) -> list[UsageWindow]:
    clean = strip_terminal_codes(text)
    groups = (
        ("Gemini", "GEMINI MODELS", "CLAUDE AND GPT MODELS"),
        ("Claude/GPT", "CLAUDE AND GPT MODELS", None),
    )
    windows = []
    for group_name, heading, next_heading in groups:
        start = clean.rfind(heading)
        if start < 0:
            continue
        section = clean[start:]
        if next_heading:
            end = section.find(next_heading, len(heading))
            if end >= 0:
                section = section[:end]
        limits = (
            ("weekly", "Weekly Limit", "Five Hour Limit"),
            ("5-hour", "Five Hour Limit", None),
        )
        for limit_name, limit_heading, following_heading in limits:
            limit_start = section.find(limit_heading)
            if limit_start < 0:
                continue
            block = section[limit_start + len(limit_heading) :]
            if following_heading:
                limit_end = block.find(following_heading)
                if limit_end >= 0:
                    block = block[:limit_end]
            remaining_match = re.search(
                r"(\d+(?:\.\d+)?)%\s+remaining\s*[·•]\s*Refreshes in\s*([^\n]+)",
                block,
            )
            if remaining_match:
                remaining = round(float(remaining_match.group(1)))
                windows.append(
                    UsageWindow(
                        f"{group_name} {limit_name}",
                        max(0, min(100, 100 - remaining)),
                        relative_reset_timestamp(remaining_match.group(2)),
                    )
                )
            elif "Quota available" in block:
                windows.append(
                    UsageWindow(
                        f"{group_name} {limit_name}",
                        0,
                        "Quota available",
                    )
                )
    return windows


def read_antigravity_usage(timeout: float = 15.0) -> list[UsageWindow]:
    master, slave = pty.openpty()
    fcntl.ioctl(
        slave,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", 40, 140, 0, 0),
    )
    process = subprocess.Popen(
        [executable("agy")],
        cwd=HOME,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        close_fds=True,
        start_new_session=True,
    )
    os.close(slave)
    output = bytearray()
    started = time.monotonic()
    quota_sent = False
    page_sent = False
    try:
        while time.monotonic() - started < timeout:
            ready, _, _ = select.select([master], [], [], 0.2)
            if ready:
                try:
                    chunk = os.read(master, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
            elapsed = time.monotonic() - started
            if not quota_sent and elapsed > 2:
                os.write(master, b"/quota\r")
                quota_sent = True
            if quota_sent and not page_sent and elapsed > 7:
                os.write(master, b"\x1b[6~")
                page_sent = True
            text = output.decode("utf-8", errors="replace")
            windows = parse_antigravity_usage(text)
            clean = strip_terminal_codes(text)
            if len(windows) >= 4 or (
                len(windows) == 2
                and elapsed > 8
                and "Five Hour Limit" not in clean
            ):
                return windows
        raise RuntimeError("Antigravity returned no recognizable quota information.")
    finally:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
        os.close(master)


def read_codex_usage(timeout: float = 12.0) -> list[UsageWindow]:
    process = subprocess.Popen(
        [executable("codex"), "app-server", "--listen", "stdio://"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        bufsize=1,
    )
    requests = (
        {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "session-hub",
                    "title": "Session Hub",
                    "version": "0.2.0",
                }
            },
        },
        {"method": "initialized", "params": {}},
        {"id": 2, "method": "account/rateLimits/read", "params": None},
    )
    try:
        if process.stdin is None or process.stdout is None:
            raise RuntimeError("Could not communicate with the Codex app server.")
        for request in requests:
            process.stdin.write(json.dumps(request) + "\n")
            process.stdin.flush()
        deadline = datetime.now().timestamp() + timeout
        while datetime.now().timestamp() < deadline:
            ready, _, _ = select.select([process.stdout], [], [], 0.25)
            if not ready:
                if process.poll() is not None:
                    break
                continue
            line = process.stdout.readline()
            if not line:
                continue
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if response.get("id") != 2:
                continue
            snapshot = response.get("result", {}).get("rateLimits", {})
            windows = []
            for key, fallback in (("primary", "5-hour"), ("secondary", "Weekly")):
                window = snapshot.get(key)
                if not window:
                    continue
                duration = window.get("windowDurationMins")
                name = (
                    f"{duration // 60}-hour"
                    if duration and duration < 1440
                    else "Weekly" if duration else fallback
                )
                windows.append(
                    UsageWindow(
                        name,
                        max(0, min(100, int(window.get("usedPercent", 0)))),
                        format_reset_timestamp(window.get("resetsAt")),
                    )
                )
            if windows:
                return windows
            raise RuntimeError("Codex returned no usage windows.")
        raise TimeoutError("Codex usage request timed out.")
    finally:
        process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()


def read_claude_usage(timeout: float = 15.0) -> list[UsageWindow]:
    result = subprocess.run(
        [
            executable("claude"),
            "-p",
            "/usage",
            "--no-session-persistence",
            "--output-format",
            "json",
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "Claude usage request failed.")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("Claude returned invalid usage data.") from error
    windows = parse_claude_usage(str(payload.get("result") or ""))
    if not windows:
        raise RuntimeError("Claude returned no recognizable usage windows.")
    return windows


class UsageWorkerSignals(QObject):
    finished = pyqtSignal(str, object, str)


class UsageWorker(QRunnable):
    def __init__(self, provider: str) -> None:
        super().__init__()
        self.provider = provider
        self.signals = UsageWorkerSignals()

    def run(self) -> None:
        try:
            readers = {
                "Codex": read_codex_usage,
                "Claude": read_claude_usage,
                "Antigravity": read_antigravity_usage,
            }
            reader = readers[self.provider]
            self.signals.finished.emit(self.provider, reader(), "")
        except Exception as error:
            self.signals.finished.emit(self.provider, [], str(error))


def read_metadata() -> dict:
    try:
        data = json.loads(METADATA_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {"sessions": {}}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {"sessions": {}}


def write_metadata(data: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = METADATA_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    temp.replace(METADATA_PATH)


def clean_title(value: str, fallback: str) -> str:
    value = " ".join(str(value).strip().split())
    if not value:
        return fallback
    return value[:180] + ("…" if len(value) > 180 else "")


def codex_sessions() -> list[Session]:
    sessions: list[Session] = []
    if CODEX_STATE.exists():
        try:
            uri = f"file:{CODEX_STATE}?mode=ro"
            with sqlite3.connect(uri, uri=True) as db:
                rows = db.execute(
                    "SELECT id, title, cwd, updated_at_ms, rollout_path "
                    "FROM threads ORDER BY updated_at_ms DESC"
                ).fetchall()
            for session_id, title, cwd, updated_ms, rollout_path in rows:
                path = Path(rollout_path)
                if not path.is_absolute():
                    path = HOME / ".codex" / path
                if path.is_file():
                    sessions.append(
                        Session(
                            "Codex",
                            session_id,
                            clean_title(title, f"Codex {session_id[:8]}"),
                            cwd or str(HOME),
                            cwd or str(HOME),
                            int(updated_ms or path.stat().st_mtime * 1000),
                            path,
                        )
                    )
            return sessions
        except (sqlite3.Error, OSError):
            pass

    for path in CODEX_SESSIONS.glob("**/*.jsonl"):
        try:
            first = json.loads(path.open(encoding="utf-8", errors="replace").readline())
            payload = first.get("payload", {})
            session_id = payload.get("id") or path.stem.rsplit("-", 5)[-1]
            sessions.append(
                Session(
                    "Codex",
                    session_id,
                    f"Codex {session_id[:8]}",
                    payload.get("cwd") or str(HOME),
                    payload.get("cwd") or str(HOME),
                    int(path.stat().st_mtime * 1000),
                    path,
                )
            )
        except (OSError, json.JSONDecodeError):
            continue
    return sessions


def claude_history_index() -> dict[str, dict]:
    index: dict[str, dict] = {}
    try:
        with CLAUDE_HISTORY.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                session_id = row.get("sessionId")
                if not session_id:
                    continue
                entry = index.setdefault(session_id, {})
                display = str(row.get("display") or "").strip()
                if display and "title" not in entry:
                    entry["title"] = display
                entry["cwd"] = row.get("project") or entry.get("cwd")
                entry["updated_ms"] = max(
                    int(row.get("timestamp") or 0), int(entry.get("updated_ms") or 0)
                )
    except OSError:
        pass
    return index


def claude_project_key(path: str) -> str:
    """Return the directory key Claude uses below ~/.claude/projects."""
    return path.replace("/", "-").replace(".", "-")


def inspect_claude_file(path: Path) -> dict:
    result: dict = {}
    project_key = path.parent.name
    cwd_counts: dict[str, int] = {}
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if len(line) > 2_000_000:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") == "ai-title" and row.get("aiTitle"):
                    result["title"] = row["aiTitle"]
                cwd = row.get("cwd")
                if cwd:
                    cwd_counts[cwd] = cwd_counts.get(cwd, 0) + 1
                    if claude_project_key(cwd) == project_key:
                        result["project_cwd"] = cwd
                if row.get("timestamp"):
                    try:
                        stamp = datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                        result["updated_ms"] = max(
                            int(stamp.timestamp() * 1000),
                            int(result.get("updated_ms") or 0),
                        )
                    except (TypeError, ValueError):
                        pass
    except OSError:
        pass
    if not result.get("project_cwd") and cwd_counts:
        result["observed_cwd"] = max(cwd_counts, key=cwd_counts.get)
    return result


def claude_sessions() -> list[Session]:
    history = claude_history_index()
    sessions: list[Session] = []
    for path in CLAUDE_PROJECTS.glob("*/*.jsonl"):
        session_id = path.stem
        info = dict(history.get(session_id, {}))
        file_info = inspect_claude_file(path)
        info.update({key: value for key, value in file_info.items() if value})
        cwd = (
            info.get("project_cwd")
            or info.get("cwd")
            or info.get("observed_cwd")
            or str(HOME)
        )
        sessions.append(
            Session(
                "Claude",
                session_id,
                clean_title(info.get("title", ""), f"Claude {session_id[:8]}"),
                cwd,
                cwd,
                int(info.get("updated_ms") or path.stat().st_mtime * 1000),
                path,
            )
        )
    return sessions


def antigravity_transcript_path(session_id: str) -> Path:
    return (
        ANTIGRAVITY_BRAIN
        / session_id
        / ".system_generated"
        / "logs"
        / "transcript.jsonl"
    )


def antigravity_database_info(path: Path) -> dict:
    info: dict = {}
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            row = db.execute(
                "SELECT data FROM trajectory_metadata_blob LIMIT 1"
            ).fetchone()
        if row and row[0]:
            printable = re.findall(rb"[\x20-\x7e]{4,}", row[0])
            for value in printable:
                marker = value.find(b"file:///")
                if marker < 0:
                    continue
                candidate = value[marker + len(b"file://") :].decode(
                    "utf-8", errors="replace"
                )
                if "z" in candidate and not Path(candidate).exists():
                    candidate = candidate.rsplit("z", 1)[0]
                if Path(candidate).is_absolute():
                    info["cwd"] = candidate
                    break
    except (OSError, sqlite3.Error):
        pass
    return info


def antigravity_transcript_info(path: Path) -> dict:
    info: dict = {}
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") == "USER_INPUT" and not info.get("title"):
                    text = str(row.get("content") or "")
                    match = re.search(
                        r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>",
                        text,
                        re.DOTALL,
                    )
                    info["title"] = clean_title(
                        match.group(1) if match else text,
                        "",
                    )
                created = row.get("created_at")
                if created:
                    try:
                        stamp = datetime.fromisoformat(
                            str(created).replace("Z", "+00:00")
                        )
                        info["updated_ms"] = max(
                            int(stamp.timestamp() * 1000),
                            int(info.get("updated_ms") or 0),
                        )
                    except ValueError:
                        pass
    except OSError:
        pass
    return info


def antigravity_sessions() -> list[Session]:
    sessions = []
    for path in ANTIGRAVITY_CONVERSATIONS.glob("*.db"):
        session_id = path.stem
        info = antigravity_database_info(path)
        transcript = antigravity_transcript_path(session_id)
        info.update(
            {
                key: value
                for key, value in antigravity_transcript_info(transcript).items()
                if value
            }
        )
        cwd = str(info.get("cwd") or HOME)
        sessions.append(
            Session(
                "Antigravity",
                session_id,
                clean_title(
                    info.get("title", ""),
                    f"Antigravity {session_id[:8]}",
                ),
                cwd,
                cwd,
                int(info.get("updated_ms") or path.stat().st_mtime * 1000),
                path,
            )
        )
    return sessions


def resolve_pending_handoffs(metadata: dict, sessions: list[Session]) -> bool:
    changed = False
    pending = metadata.setdefault("pending_handoffs", [])
    remaining = []
    now_ms = int(datetime.now().timestamp() * 1000)
    for item in pending:
        if now_ms > int(item.get("expires_ms", now_ms + 1)):
            changed = True
            continue
        handoff_name = Path(item.get("handoff_path", "")).name
        candidates = [
            session
            for session in sessions
            if session.provider == item.get("target_provider")
            and session.native_key not in set(item.get("existing_keys", []))
            and session.updated_ms >= int(item.get("started_ms", 0))
            and (
                Path(session.cwd) == Path(item.get("cwd", ""))
                or handoff_name
                and handoff_name in session.title
            )
        ]
        if not candidates:
            remaining.append(item)
            continue
        target = max(candidates, key=lambda session: session.updated_ms)
        logical_key = item["logical_key"]
        link = metadata.setdefault("links", {}).setdefault(
            logical_key, {"members": [logical_key], "active": logical_key}
        )
        if target.native_key not in link["members"]:
            link["members"].append(target.native_key)
        link["active"] = target.native_key
        changed = True
    metadata["pending_handoffs"] = remaining
    return changed


def discover_sessions(metadata: dict) -> list[Session]:
    sessions = codex_sessions() + claude_sessions() + antigravity_sessions()
    if resolve_pending_handoffs(metadata, sessions):
        write_metadata(metadata)
    by_key = {session.native_key: session for session in sessions}
    overrides = metadata.setdefault("sessions", {})
    for session in sessions:
        custom = overrides.get(session.native_key, {})
        session.title = custom.get("name") or session.title
        session.cwd = custom.get("cwd") or session.cwd

    hidden = set()
    visible_linked = []
    for logical_key, link in metadata.setdefault("links", {}).items():
        members = tuple(link.get("members", []))
        active = by_key.get(link.get("active"))
        if not active:
            active = next((by_key.get(key) for key in reversed(members) if by_key.get(key)), None)
        hidden.update(members)
        if not active:
            continue
        active.logical_key = logical_key
        active.linked_keys = members
        custom = overrides.get(logical_key, {})
        active.title = custom.get("name") or active.title
        active.cwd = custom.get("cwd") or active.cwd
        visible_linked.append(active)
    visible = [
        session for session in sessions if session.native_key not in hidden
    ] + visible_linked
    for session in visible:
        custom = overrides.get(session.key, {})
        session.title = custom.get("name") or session.title
        session.cwd = custom.get("cwd") or session.cwd
    return sorted(visible, key=lambda item: item.updated_ms, reverse=True)


def native_session_index() -> dict[str, Session]:
    return {
        session.native_key: session
        for session in codex_sessions() + claude_sessions() + antigravity_sessions()
    }


def executable(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    local = HOME / ".local" / "bin" / name
    return str(local)


def text_from_content(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if not isinstance(content, list):
        return ""
    texts = []
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") in {"input_text", "output_text", "text"}:
            value = item.get("text")
            if value:
                texts.append(str(value).strip())
    return "\n".join(text for text in texts if text)


def handoff_noise(text: str) -> bool:
    normalized = text.strip()
    return (
        normalized.startswith("Prepare a handoff summary for another coding agent.")
        or normalized.startswith("You've hit your session limit")
        or normalized.startswith("You have ")
        and "weighted tokens left" in normalized
    )


def compact_message(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    marker = "\n\n[…middle of this message omitted by Session Hub…]\n\n"
    available = max(0, limit - len(marker))
    head = available // 2
    tail = available - head
    return text[:head] + marker + text[-tail:]


def transcript_messages(session: Session, max_chars: int = 50000) -> list[tuple[str, str]]:
    messages = []
    transcript_path = (
        antigravity_transcript_path(session.session_id)
        if session.provider == "Antigravity"
        else session.path
    )
    try:
        with transcript_path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if len(line) > 2_000_000:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                role = ""
                text = ""
                if session.provider == "Codex":
                    payload = row.get("payload", {})
                    if row.get("type") == "response_item" and payload.get("type") == "message":
                        role = str(payload.get("role") or "")
                        text = text_from_content(payload.get("content"))
                elif session.provider == "Claude" and row.get("type") in {
                    "user",
                    "assistant",
                }:
                    role = row["type"]
                    message = row.get("message", {})
                    text = text_from_content(
                        message.get("content") if isinstance(message, dict) else message
                    )
                elif session.provider == "Antigravity":
                    item_type = row.get("type")
                    if item_type == "USER_INPUT":
                        role = "user"
                        text = str(row.get("content") or "")
                        match = re.search(
                            r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>",
                            text,
                            re.DOTALL,
                        )
                        if match:
                            text = match.group(1).strip()
                    elif item_type in {"PLANNER_RESPONSE", "MODEL_RESPONSE"}:
                        role = "assistant"
                        text = str(row.get("content") or "").strip()
                if role not in {"user", "assistant"} or not text:
                    continue
                if text.startswith("<environment_context>") or text.startswith(
                    "# AGENTS.md instructions"
                ):
                    continue
                if handoff_noise(text):
                    continue
                messages.append((role, text))
    except OSError:
        return []
    selected = []
    total = 0
    for role, text in reversed(messages):
        text = compact_message(text, min(12000, max_chars))
        if selected and total + len(text) > max_chars:
            break
        selected.append((role, text))
        total += len(text)
    return list(reversed(selected))


def project_state(cwd: str) -> str:
    if not (Path(cwd) / ".git").exists():
        return "No Git repository detected at the working directory."
    try:
        result = subprocess.run(
            ["git", "status", "--short", "--branch"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return result.stdout.strip() or "Git working tree is clean."
    except (OSError, subprocess.TimeoutExpired):
        return "Git status unavailable."


def summary_path(logical_key: str) -> Path:
    digest = hashlib.sha256(logical_key.encode("utf-8")).hexdigest()[:20]
    return SUMMARY_DIR / f"{digest}.md"


def summary_prompt(session: Session) -> str:
    path = summary_path(session.key)
    return (
        "Prepare a handoff summary for another coding agent. Review this session's "
        "full conversation and the current project state. Write the summary directly "
        f"to this exact file: {path}\n\n"
        "Use these headings:\n"
        "# Agent Handoff Summary\n"
        "## Objective\n"
        "## User Requirements and Preferences\n"
        "## Important Decisions and Rationale\n"
        "## Completed Work\n"
        "## Files Changed\n"
        "## Current State and Verification\n"
        "## Remaining Work\n"
        "## Known Problems and Risks\n"
        "## Recommended Next Steps\n\n"
        "Be concrete and concise, but preserve details needed to continue without "
        "re-reading the entire transcript. Do not include credentials, tokens, API "
        "keys, private prompt text, or irrelevant tool output. Create parent "
        "directories if needed. After writing the file, reply with its path."
    )


def write_handoff(session: Session, target_provider: str) -> Path:
    HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
    path = HANDOFF_DIR / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}.md"
    lines = [
        "# Session Hub Agent Handoff",
        "",
        f"- From: {session.provider}",
        f"- To: {target_provider}",
        f"- Session: {session.title}",
        f"- Working directory: {session.cwd}",
        f"- Created: {datetime.now().isoformat(timespec='seconds')}",
        "",
        "## Current project state",
        "",
        "```text",
        project_state(session.cwd),
        "```",
        "",
    ]
    prepared = summary_path(session.key)
    summary = ""
    if prepared.is_file():
        try:
            summary = prepared.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            summary = ""
        if summary:
            lines.extend(
                (
                    "## Prepared full-session summary",
                    "",
                    compact_message(summary, 35000),
                    "",
                )
            )
    lines.extend(
        (
        "## Recent conversation",
        "",
        )
    )
    recent_limit = 12000 if summary else 50000
    for role, text in transcript_messages(session, max_chars=recent_limit):
        lines.extend((f"### {role.capitalize()}", "", text, ""))
    lines.extend(
        (
            "## Continuation instruction",
            "",
            "Continue the existing task naturally. Inspect the current files and state "
            "before changing anything. Do not repeat work already completed. Ask only "
            "when a missing decision materially blocks progress. Read this file by "
            "section or in chunks if a file-viewing tool truncates its output; do not "
            "assume the first displayed chunk is the entire handoff.",
            "",
        )
    )
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def is_compatibility_link(path: Path, target: Path) -> bool:
    try:
        return path.is_symlink() and path.resolve() == target.resolve()
    except OSError:
        return False


def move_project_files(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if is_compatibility_link(destination, source):
        destination.unlink()
    elif destination.exists() or destination.is_symlink():
        raise FileExistsError(f"Destination already exists: {destination}")
    shutil.move(str(source), str(destination))
    source.symlink_to(destination, target_is_directory=True)


class SettingsDialog(QDialog):
    def __init__(self, settings: dict, parent=None) -> None:
        super().__init__(parent)
        self.original_settings = dict(settings)
        self.setWindowTitle("Session Hub Settings")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)

        group = QGroupBox("Global launch permissions")
        group_layout = QVBoxLayout(group)
        self.codex_danger = QCheckBox(
            "Codex: bypass approvals and sandbox for every Session Hub launch"
        )
        self.claude_danger = QCheckBox(
            "Claude: skip permission prompts for every Session Hub launch"
        )
        self.antigravity_danger = QCheckBox(
            "Antigravity: skip permission prompts for every Session Hub launch"
        )
        self.codex_danger.setChecked(bool(settings.get("codex_danger_mode", False)))
        self.claude_danger.setChecked(bool(settings.get("claude_danger_mode", False)))
        self.antigravity_danger.setChecked(
            bool(settings.get("antigravity_danger_mode", False))
        )
        group_layout.addWidget(self.codex_danger)
        group_layout.addWidget(self.claude_danger)
        group_layout.addWidget(self.antigravity_danger)
        layout.addWidget(group)

        warning = QLabel(
            "Danger mode lets an agent execute commands and modify files without "
            "normal approval checks. These switches affect launches from Session Hub only."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #d9534f;")
        layout.addWidget(warning)

        projects_group = QGroupBox("Project locations")
        projects_form = QFormLayout(projects_group)
        self.primary_projects = QLineEdit(
            settings.get("primary_projects_dir", str(HOME / "projects"))
        )
        self.secondary_projects = QLineEdit(
            settings.get("secondary_projects_dir", "")
        )
        projects_form.addRow(
            "Primary projects:",
            self.folder_picker(self.primary_projects),
        )
        projects_form.addRow(
            "Secondary projects:",
            self.folder_picker(self.secondary_projects),
        )
        projects_note = QLabel(
            "The secondary location is optional. These folders are used only by "
            "the new-session dialogs."
        )
        projects_note.setWordWrap(True)
        projects_form.addRow(projects_note)
        if parent is not None and hasattr(parent, "move_project"):
            move_project = QPushButton("Move project between locations…")
            move_project.clicked.connect(lambda: parent.move_project(self.values()))
            projects_form.addRow(move_project)
        layout.addWidget(projects_group)

        trash_group = QGroupBox("Deleted sessions")
        trash_layout = QFormLayout(trash_group)
        trash_note = QLabel(
            "Deleted histories remain recoverable until restored or removed by "
            "the retention policy."
        )
        trash_note.setWordWrap(True)
        trash_layout.addRow(trash_note)
        self.trash_retention = QComboBox()
        for label, days in (
            ("Never delete automatically", 0),
            ("After 7 days", 7),
            ("After 30 days", 30),
            ("After 90 days", 90),
        ):
            self.trash_retention.addItem(label, days)
        current_retention = int(settings.get("trash_retention_days", 0) or 0)
        index = self.trash_retention.findData(current_retention)
        self.trash_retention.setCurrentIndex(max(0, index))
        trash_layout.addRow("Permanently delete:", self.trash_retention)
        if parent is not None and hasattr(parent, "open_deleted_sessions"):
            manage_trash = QPushButton("Manage deleted sessions…")
            manage_trash.clicked.connect(parent.open_deleted_sessions)
            trash_layout.addRow(manage_trash)
        layout.addWidget(trash_group)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> dict:
        values = dict(self.original_settings)
        values.update(
            {
                "codex_danger_mode": self.codex_danger.isChecked(),
                "claude_danger_mode": self.claude_danger.isChecked(),
                "antigravity_danger_mode": self.antigravity_danger.isChecked(),
                "primary_projects_dir": self.normalized_path(
                    self.primary_projects.text()
                ),
                "secondary_projects_dir": self.normalized_path(
                    self.secondary_projects.text()
                ),
                "trash_retention_days": int(
                    self.trash_retention.currentData() or 0
                ),
            }
        )
        return values

    @staticmethod
    def normalized_path(value: str) -> str:
        value = value.strip()
        return str(Path(value).expanduser()) if value else ""

    def folder_picker(self, line_edit: QLineEdit) -> QWidget:
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        browse = QPushButton("Browse…")
        browse.clicked.connect(lambda: self.browse_folder(line_edit))
        row.addWidget(line_edit, 1)
        row.addWidget(browse)
        return container

    def browse_folder(self, line_edit: QLineEdit) -> None:
        current = Path(line_edit.text()).expanduser()
        start = current if current.is_dir() else HOME
        directory = QFileDialog.getExistingDirectory(
            self, "Choose projects folder", str(start)
        )
        if directory:
            line_edit.setText(directory)

class NewSessionDialog(QDialog):
    def __init__(self, provider: str, settings: dict, parent=None) -> None:
        super().__init__(parent)
        self.provider = provider
        self.project_roots = {
            "primary": Path(
                settings.get("primary_projects_dir") or HOME / "projects"
            ).expanduser(),
            "secondary": (
                Path(settings["secondary_projects_dir"]).expanduser()
                if settings.get("secondary_projects_dir")
                else None
            ),
        }
        self.directory: Path | None = None
        self.setWindowTitle(f"New {provider} Session")
        self.setMinimumWidth(600)
        layout = QVBoxLayout(self)
        form = QFormLayout()

        self.location = QComboBox()
        self.location.addItem("Home — questions and one-off work", "home")
        self.location.addItem(
            f"Primary project — {self.project_roots['primary']}", "primary"
        )
        secondary = self.project_roots["secondary"]
        self.location.addItem(
            f"Secondary project — {secondary}"
            if secondary
            else "Secondary project — configure in Settings",
            "secondary",
        )
        if not secondary:
            item = self.location.model().item(2)
            if item is not None:
                item.setEnabled(False)
        self.location.addItem("Existing folder…", "existing")
        self.location.currentIndexChanged.connect(self.update_fields)
        form.addRow("Location:", self.location)

        self.project_name = QLineEdit()
        self.project_name.setPlaceholderText("project-name")
        form.addRow("Project name:", self.project_name)

        existing_row = QHBoxLayout()
        self.existing_path = QLineEdit()
        self.existing_path.setReadOnly(True)
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.browse_existing)
        existing_row.addWidget(self.existing_path, 1)
        existing_row.addWidget(browse)
        self.existing_widget = QWidget()
        self.existing_widget.setLayout(existing_row)
        form.addRow("Existing folder:", self.existing_widget)
        layout.addLayout(form)

        self.preview = QLabel()
        self.preview.setWordWrap(True)
        layout.addWidget(self.preview)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText(
            f"Start {provider}"
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.project_name.textChanged.connect(self.update_preview)
        self.update_fields()

    def location_type(self) -> str:
        return str(self.location.currentData())

    def update_fields(self) -> None:
        project = self.location_type() in {"primary", "secondary"}
        self.project_name.setEnabled(project)
        self.existing_widget.setEnabled(self.location_type() == "existing")
        self.update_preview()

    def update_preview(self) -> None:
        location = self.location_type()
        if location == "home":
            path = HOME
        elif location in {"primary", "secondary"}:
            root = self.project_roots[location]
            path = root / self.project_name.text().strip() if root else None
        else:
            path = Path(self.existing_path.text()) if self.existing_path.text() else None
        self.preview.setText(f"Working directory: {path}" if path else "Choose a folder.")

    def browse_existing(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Choose working directory", str(HOME)
        )
        if directory:
            self.existing_path.setText(directory)
            self.update_preview()

    def accept(self) -> None:
        location = self.location_type()
        if location == "home":
            directory = HOME
        elif location in {"primary", "secondary"}:
            name = self.project_name.text().strip()
            if (
                not name
                or name in {".", ".."}
                or Path(name).name != name
                or "/" in name
            ):
                QMessageBox.warning(
                    self,
                    "Invalid project name",
                    "Enter one folder name without slashes.",
                )
                return
            base = self.project_roots[location]
            if base is None:
                QMessageBox.warning(
                    self,
                    "Project location not configured",
                    "Configure the secondary projects folder in Settings first.",
                )
                return
            directory = base / name
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as error:
                QMessageBox.critical(self, "Could not create project", str(error))
                return
        else:
            if not self.existing_path.text():
                QMessageBox.warning(self, "Choose a folder", "Select an existing folder.")
                return
            directory = Path(self.existing_path.text())
        if not directory.is_dir():
            QMessageBox.warning(self, "Missing folder", f"Folder not found:\n{directory}")
            return
        self.directory = directory
        super().accept()


class MoveProjectDialog(QDialog):
    def __init__(self, settings: dict, project_labels: dict[str, str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Move Project")
        self.setMinimumWidth(620)
        self.primary = Path(
            settings.get("primary_projects_dir") or HOME / "projects"
        ).expanduser()
        secondary_value = settings.get("secondary_projects_dir")
        self.secondary = Path(secondary_value).expanduser() if secondary_value else None
        self.project_labels = project_labels
        self.source: Path | None = None
        self.destination: Path | None = None

        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.direction = QComboBox()
        self.direction.addItem("Primary → Secondary", "to_secondary")
        self.direction.addItem("Secondary → Primary", "to_primary")
        self.direction.currentIndexChanged.connect(self.load_projects)
        form.addRow("Direction:", self.direction)
        self.project = QComboBox()
        self.project.currentIndexChanged.connect(self.update_preview)
        form.addRow("Project:", self.project)
        layout.addLayout(form)

        self.preview = QLabel()
        self.preview.setWordWrap(True)
        layout.addWidget(self.preview)
        self.note = QLabel()
        self.note.setWordWrap(True)
        layout.addWidget(self.note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Move project")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.load_projects()

    def roots(self) -> tuple[Path | None, Path | None]:
        if self.direction.currentData() == "to_secondary":
            return self.primary, self.secondary
        return self.secondary, self.primary

    def load_projects(self) -> None:
        self.project.clear()
        source_root, destination_root = self.roots()
        if source_root is None or destination_root is None:
            self.preview.setText("Configure both project locations in Settings first.")
            return
        if source_root.is_dir():
            for path in sorted(source_root.iterdir(), key=lambda item: item.name.lower()):
                if path.is_dir() and not path.is_symlink():
                    label = self.project_labels.get(str(path), path.name)
                    if label != path.name:
                        label = f"{label}  [{path.name}]"
                    self.project.addItem(label, path.name)
        self.update_preview()

    def update_preview(self) -> None:
        source_root, destination_root = self.roots()
        project_name = str(self.project.currentData() or "")
        if source_root is None or destination_root is None:
            self.preview.setText("Configure both project locations in Settings first.")
        elif not project_name:
            self.preview.setText(f"No movable projects found in {source_root}")
        else:
            self.preview.setText(
                f"{source_root / project_name}\n→ {destination_root / project_name}"
            )
        if self.direction.currentData() == "to_secondary":
            self.note.setText(
                "The real project moves into the Secondary location. A "
                "compatibility symlink remains in the Primary location."
            )
            self.note.setStyleSheet("")
        else:
            self.note.setText(
                "The real project moves into the Primary location. A compatibility "
                "symlink remains in the Secondary location."
            )
            self.note.setStyleSheet("")

    def accept(self) -> None:
        source_root, destination_root = self.roots()
        project_name = str(self.project.currentData() or "")
        if source_root is None or destination_root is None or not project_name:
            QMessageBox.warning(self, "Nothing to move", self.preview.text())
            return
        source = source_root / project_name
        destination = destination_root / project_name
        if (
            destination.exists() or destination.is_symlink()
        ) and not is_compatibility_link(destination, source):
            QMessageBox.warning(
                self,
                "Destination already exists",
                f"Choose another project or resolve this folder first:\n{destination}",
            )
            return
        self.source = source
        self.destination = destination
        super().accept()


def infer_deleted_manifest(entry: Path) -> dict:
    parts = entry.name.split("-", 2)
    deleted_at = ""
    session_id = parts[2] if len(parts) == 3 else entry.name
    if len(parts) >= 2:
        try:
            deleted_at = datetime.strptime(
                f"{parts[0]}-{parts[1]}", "%Y%m%d-%H%M%S"
            ).isoformat()
        except ValueError:
            pass
    provider = entry.parent.name.capitalize()
    title = session_id
    items = []
    history_files = list(entry.glob("*.jsonl"))
    if provider == "Codex" and history_files:
        history = history_files[0]
        match = re.match(r"rollout-(\d{4})-(\d{2})-(\d{2})T", history.name)
        if match:
            year, month, day = match.groups()
            items.append(
                {
                    "trash": history.name,
                    "original": str(CODEX_SESSIONS / year / month / day / history.name),
                }
            )
        if CODEX_STATE.exists():
            try:
                uri = f"file:{CODEX_STATE}?mode=ro"
                with sqlite3.connect(uri, uri=True) as db:
                    row = db.execute(
                        "SELECT title FROM threads WHERE id = ?", (session_id,)
                    ).fetchone()
                if row and row[0]:
                    title = clean_title(row[0], session_id)
            except sqlite3.Error:
                pass
    elif provider == "Claude" and history_files:
        history = history_files[0]
        info = inspect_claude_file(history)
        title = clean_title(info.get("title", ""), session_id)
        cwd = info.get("project_cwd") or info.get("observed_cwd")
        if cwd:
            project_dir = CLAUDE_PROJECTS / claude_project_key(cwd)
            items.append(
                {"trash": history.name, "original": str(project_dir / history.name)}
            )
            related = entry / session_id
            if related.is_dir():
                items.append(
                    {"trash": related.name, "original": str(project_dir / related.name)}
                )
    return {
        "provider": provider,
        "session_id": session_id,
        "title": title,
        "deleted_at": deleted_at
        or datetime.fromtimestamp(entry.stat().st_mtime).isoformat(),
        "items": items,
        "metadata_override": {},
        "legacy": True,
    }


def deleted_manifest(entry: Path) -> dict:
    manifest_path = entry / "manifest.json"
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (OSError, json.JSONDecodeError):
        pass
    return infer_deleted_manifest(entry)


def deleted_entries() -> list[tuple[Path, dict]]:
    entries = []
    for provider_dir in TRASH_DIR.glob("*"):
        if not provider_dir.is_dir():
            continue
        for entry in provider_dir.iterdir():
            if entry.is_dir():
                entries.append((entry, deleted_manifest(entry)))
    return sorted(
        entries,
        key=lambda item: item[1].get("deleted_at", ""),
        reverse=True,
    )


class DeletedSessionsDialog(QDialog):
    def __init__(self, hub, parent=None) -> None:
        super().__init__(parent)
        self.hub = hub
        self.entries: list[tuple[Path, dict]] = []
        self.setWindowTitle("Deleted Sessions")
        self.resize(900, 480)
        layout = QVBoxLayout(self)
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(
            ["Agent", "Name", "Deleted", "Restore destination"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.table)

        buttons = QHBoxLayout()
        open_folder = QPushButton("Open storage folder")
        open_folder.clicked.connect(self.open_folder)
        restore = QPushButton("Restore selected")
        restore.clicked.connect(self.restore_selected)
        delete = QPushButton("Permanently delete selected")
        delete.clicked.connect(self.delete_selected)
        close = QPushButton("Close")
        close.clicked.connect(self.accept)
        buttons.addWidget(open_folder)
        buttons.addStretch(1)
        buttons.addWidget(delete)
        buttons.addWidget(restore)
        buttons.addWidget(close)
        layout.addLayout(buttons)
        self.reload()

    def reload(self) -> None:
        self.entries = deleted_entries()
        self.table.setRowCount(len(self.entries))
        for row, (_, manifest) in enumerate(self.entries):
            destinations = [
                item.get("original", "") for item in manifest.get("items", [])
            ]
            values = (
                manifest.get("provider", ""),
                manifest.get("title") or manifest.get("session_id", ""),
                str(manifest.get("deleted_at", "")).replace("T", " ")[:16],
                destinations[0] if destinations else "Unknown — cannot restore automatically",
            )
            for column, value in enumerate(values):
                self.table.setItem(row, column, QTableWidgetItem(str(value)))

    def selected_entries(self) -> list[tuple[Path, dict]]:
        rows = sorted(
            index.row() for index in self.table.selectionModel().selectedRows()
        )
        if not rows:
            QMessageBox.information(
                self, "Deleted Sessions", "Select one or more sessions first."
            )
            return []
        return [self.entries[row] for row in rows]

    def restore_selected(self) -> None:
        selected = self.selected_entries()
        restored = 0
        for entry, manifest in selected:
            if self.hub.restore_deleted_entry(entry, manifest, notify=False):
                restored += 1
        if restored:
            QMessageBox.information(
                self,
                "Sessions restored",
                f"Restored {restored} session{'s' if restored != 1 else ''}.",
            )
            self.reload()

    def delete_selected(self) -> None:
        selected = self.selected_entries()
        if not selected:
            return
        names = [
            manifest.get("title") or manifest.get("session_id")
            for _, manifest in selected
        ]
        answer = QMessageBox.warning(
            self,
            "Permanently delete sessions?",
            "\n".join(str(name) for name in names[:8])
            + ("\n…" if len(names) > 8 else "")
            + "\n\n"
            "This cannot be undone.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer == QMessageBox.StandardButton.Yes:
            for entry, _ in selected:
                shutil.rmtree(entry)
            self.reload()

    def open_folder(self) -> None:
        TRASH_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(TRASH_DIR)))


class SessionHub(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.metadata = read_metadata()
        self.sessions: list[Session] = []
        self.usage_widgets: dict[str, list[tuple[QLabel, QProgressBar, QLabel]]] = {}
        self.usage_workers: dict[str, UsageWorker] = {}
        self.thread_pool = QThreadPool.globalInstance()
        self.setWindowTitle("Session Hub")
        self.setWindowIcon(
            QIcon(str(APP_ICON)) if APP_ICON.is_file() else QIcon.fromTheme("utilities-terminal")
        )
        self.resize(1280, 900)
        self.setMinimumSize(900, 650)
        self.build_ui()
        self.restore_window_geometry()
        self.purge_expired_trash()
        self.refresh()
        QTimer.singleShot(0, self.refresh_usage)
        self.usage_timer = QTimer(self)
        self.usage_timer.setInterval(5 * 60 * 1000)
        self.usage_timer.timeout.connect(self.refresh_usage)
        self.usage_timer.start()

    def build_ui(self) -> None:
        root = QWidget()
        layout = QVBoxLayout(root)

        toolbar = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Filter by name, provider, directory, or ID…")
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self.apply_filter)
        toolbar.addWidget(self.search, 1)

        self.new_provider = QComboBox()
        self.new_provider.addItems(PROVIDERS)
        self.new_provider.setToolTip("Agent used for the new session")
        toolbar.addWidget(self.new_provider)

        new_button = QPushButton("New")
        new_button.clicked.connect(self.launch_selected_provider)
        toolbar.addWidget(new_button)

        for label, slot in (
            ("Refresh", self.refresh_all),
            ("Settings", self.open_settings),
        ):
            button = QPushButton(label)
            button.clicked.connect(slot)
            toolbar.addWidget(button)
        layout.addLayout(toolbar)

        usage_frame = QFrame()
        usage_frame.setFrameShape(QFrame.Shape.StyledPanel)
        usage_layout = QGridLayout(usage_frame)
        usage_layout.setContentsMargins(12, 8, 12, 8)
        usage_layout.setHorizontalSpacing(18)
        usage_layout.setVerticalSpacing(4)
        for column, provider in enumerate(PROVIDERS):
            offset = column * 2
            usage_layout.addWidget(QLabel(f"<b>{provider} usage</b>"), 0, offset, 1, 2)
            rows = []
            default_names = (
                (
                    "Gemini weekly",
                    "Gemini 5-hour",
                    "Claude/GPT weekly",
                    "Claude/GPT 5-hour",
                )
                if provider == "Antigravity"
                else ("5-hour", "Weekly")
            )
            for index, window_name in enumerate(default_names):
                label = QLabel(window_name)
                bar = QProgressBar()
                bar.setRange(0, 100)
                bar.setValue(0)
                bar.setFormat("Loading…")
                if provider == "Antigravity":
                    bar.setMaximumHeight(14)
                    bar.setMinimumWidth(125)
                detail = QLabel("")
                detail.setStyleSheet(
                    "color: #888;"
                    + (" font-size: 10px;" if provider == "Antigravity" else "")
                )
                if provider == "Antigravity":
                    label.setStyleSheet("font-size: 11px;")
                row = 1 + index * 2
                usage_layout.addWidget(label, row, offset)
                usage_layout.addWidget(bar, row, offset + 1)
                usage_layout.addWidget(detail, row + 1, offset, 1, 2)
                rows.append((label, bar, detail))
            self.usage_widgets[provider] = rows
        layout.addWidget(usage_frame)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Agent", "Name", "Working directory", "Last updated", "Session ID"]
        )
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.table.doubleClicked.connect(self.resume_selected)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.context_menu)
        layout.addWidget(self.table, 1)

        actions = QHBoxLayout()
        self.status = QLabel()
        actions.addWidget(self.status, 1)
        for label, slot in (
            ("Rename", self.rename_selected),
            ("Change directory", self.change_directory),
            ("Delete", self.delete_selected),
            ("Prepare handoff summary", self.prepare_handoff_summary),
            ("Continue with other agent", self.continue_with_other_agent),
            ("Resume in new terminal", self.resume_selected),
        ):
            button = QPushButton(label)
            button.clicked.connect(slot)
            if label.startswith("Resume"):
                button.setDefault(True)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.setCentralWidget(root)

        settings_menu = self.menuBar().addMenu("Settings")
        permissions_action = QAction("Launch permissions…", self)
        permissions_action.triggered.connect(self.open_settings)
        settings_menu.addAction(permissions_action)

    def settings(self) -> dict:
        return self.metadata.setdefault("settings", {})

    def restore_window_geometry(self) -> None:
        encoded = self.settings().get("window_geometry")
        if not encoded:
            return
        try:
            self.restoreGeometry(QByteArray.fromBase64(encoded.encode("ascii")))
        except (AttributeError, ValueError):
            self.settings().pop("window_geometry", None)

    def closeEvent(self, event) -> None:
        if QApplication.platformName() != "offscreen":
            latest = read_metadata()
            latest.setdefault("settings", {}).update(self.settings())
            latest["settings"]["window_geometry"] = bytes(
                self.saveGeometry().toBase64()
            ).decode("ascii")
            self.metadata = latest
            write_metadata(latest)
        super().closeEvent(event)

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.settings(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.metadata["settings"] = dialog.values()
            write_metadata(self.metadata)
            self.purge_expired_trash()

    def open_deleted_sessions(self) -> None:
        DeletedSessionsDialog(self, self).exec()

    def purge_expired_trash(self) -> None:
        retention_days = int(self.settings().get("trash_retention_days", 0) or 0)
        if retention_days <= 0:
            return
        cutoff = datetime.now().timestamp() - retention_days * 86400
        for entry, manifest in deleted_entries():
            try:
                deleted = datetime.fromisoformat(
                    str(manifest.get("deleted_at", ""))
                ).timestamp()
            except ValueError:
                deleted = entry.stat().st_mtime
            if deleted < cutoff:
                shutil.rmtree(entry, ignore_errors=True)

    def restore_deleted_entry(
        self, entry: Path, manifest: dict, notify: bool = True
    ) -> bool:
        items = manifest.get("items") or []
        if not items:
            QMessageBox.warning(
                self,
                "Cannot restore automatically",
                "This older trash entry does not contain enough information to "
                "determine its original location.",
            )
            return False
        destinations = [Path(item["original"]) for item in items]
        collisions = [
            destination
            for destination in destinations
            if destination.exists() or destination.is_symlink()
        ]
        if collisions:
            QMessageBox.warning(
                self,
                "Restore location occupied",
                "Move or rename the existing item first:\n"
                + "\n".join(str(path) for path in collisions),
            )
            return False
        try:
            for item, destination in zip(items, destinations):
                source = entry / item["trash"]
                if not source.exists():
                    raise FileNotFoundError(f"Missing trash item: {source}")
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(source), str(destination))
            metadata_override = manifest.get("metadata_override") or {}
            logical_key = manifest.get("logical_key")
            if metadata_override:
                key = logical_key or f"{manifest.get('provider')}:{manifest.get('session_id')}"
                self.metadata.setdefault("sessions", {})[key] = metadata_override
            link_definition = manifest.get("link_definition")
            if logical_key and link_definition:
                self.metadata.setdefault("links", {})[logical_key] = link_definition
            manifest_path = entry / "manifest.json"
            if manifest_path.exists():
                manifest_path.unlink()
            entry.rmdir()
            write_metadata(self.metadata)
            self.refresh()
            if notify:
                QMessageBox.information(
                    self, "Session restored", "The session was restored."
                )
            return True
        except OSError as error:
            QMessageBox.critical(self, "Could not restore session", str(error))
            return False

    @staticmethod
    def remap_path(value: str, source: Path, destination: Path) -> str | None:
        try:
            relative = Path(value).relative_to(source)
        except ValueError:
            return None
        return str(destination / relative)

    def move_project(self, settings: dict) -> None:
        dialog = MoveProjectDialog(settings, self.project_display_names(settings), self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        source = dialog.source
        destination = dialog.destination
        if source is None or destination is None:
            return
        answer = QMessageBox.warning(
            self,
            "Move project?",
            f"{source}\n→ {destination}\n\n"
            "Close terminals and programs using this project first. "
            "A compatibility symlink will remain at the old location.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            move_project_files(source, destination)
        except OSError as error:
            QMessageBox.critical(
                self,
                "Could not move project",
                f"{error}\n\nCheck both locations before retrying.",
            )
            return

        overrides = self.metadata.setdefault("sessions", {})
        for session in self.sessions:
            remapped = self.remap_path(session.cwd, source, destination)
            if remapped:
                overrides.setdefault(session.key, {})["cwd"] = remapped
        write_metadata(self.metadata)
        self.refresh()
        QMessageBox.information(
            self,
            "Project moved",
            f"Project moved to:\n{destination}\n\n"
            f"Compatibility link:\n{source}",
        )

    def project_display_names(self, settings: dict) -> dict[str, str]:
        roots = [
            Path(settings.get("primary_projects_dir") or HOME / "projects").expanduser()
        ]
        if settings.get("secondary_projects_dir"):
            roots.append(Path(settings["secondary_projects_dir"]).expanduser())
        labels: dict[str, list[str]] = {}
        overrides = self.metadata.get("sessions", {})
        for session in self.sessions:
            custom_name = overrides.get(session.key, {}).get("name")
            if not custom_name:
                continue
            for root in roots:
                try:
                    relative = Path(session.cwd).relative_to(root)
                except ValueError:
                    continue
                if not relative.parts:
                    continue
                project_path = root / relative.parts[0]
                names = labels.setdefault(str(project_path), [])
                if custom_name not in names:
                    names.append(custom_name)
                break
        return {
            path: " / ".join(names[:2]) + ("…" if len(names) > 2 else "")
            for path, names in labels.items()
        }

    def refresh_usage(self) -> None:
        if self.usage_workers:
            return
        for provider, rows in self.usage_widgets.items():
            for _, bar, detail in rows:
                bar.setValue(0)
                bar.setFormat("Loading…")
                bar.setStyleSheet("")
                detail.setText("")
            worker = UsageWorker(provider)
            worker.signals.finished.connect(self.usage_loaded)
            self.usage_workers[provider] = worker
            self.thread_pool.start(worker)

    def usage_loaded(
        self, provider: str, windows: list[UsageWindow], error: str
    ) -> None:
        self.usage_workers.pop(provider, None)
        rows = self.usage_widgets[provider]
        if error:
            for index, (_, bar, detail) in enumerate(rows):
                bar.setFormat("Unavailable")
                detail.setText(error if index == 0 else "")
        else:
            for index, (label, bar, detail) in enumerate(rows):
                window = windows[index] if index < len(windows) else None
                if not window:
                    bar.setFormat("Unavailable")
                    detail.setText("")
                    continue
                label.setText(window.name)
                remaining = 100 - window.used_percent
                bar.setValue(remaining)
                bar.setFormat(f"{remaining}% left ({window.used_percent}% used)")
                detail.setText(window.resets)
                color = (
                    "#3da35d"
                    if remaining > 40
                    else "#d69e2e" if remaining > 15 else "#d9534f"
                )
                bar.setStyleSheet(
                    "QProgressBar { text-align: center; } "
                    f"QProgressBar::chunk {{ background-color: {color}; }}"
                )
    def refresh_all(self) -> None:
        self.usage_timer.start()
        self.refresh()
        self.refresh_usage()

    def refresh(self) -> None:
        self.metadata = read_metadata()
        self.sessions = discover_sessions(self.metadata)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.sessions))
        for row, session in enumerate(self.sessions):
            agent = QTableWidgetItem(session.provider)
            colors = {
                "Codex": "#5aa9ff",
                "Claude": "#d977ff",
                "Antigravity": "#42d6c5",
            }
            agent.setForeground(QColor(colors.get(session.provider, "#ffffff")))
            name = QTableWidgetItem(session.title)
            cwd = QTableWidgetItem(session.cwd)
            updated = QTableWidgetItem(
                datetime.fromtimestamp(session.updated_ms / 1000).strftime("%Y-%m-%d %H:%M")
            )
            updated.setData(Qt.ItemDataRole.UserRole, session.updated_ms)
            session_id = QTableWidgetItem(session.session_id)
            for item in (agent, name, cwd, updated, session_id):
                item.setData(Qt.ItemDataRole.UserRole + 1, session.key)
                self.table.setItem(row, (agent, name, cwd, updated, session_id).index(item), item)
        self.table.setSortingEnabled(True)
        self.table.sortItems(3, Qt.SortOrder.DescendingOrder)
        self.apply_filter()

    def apply_filter(self) -> None:
        query = self.search.text().strip().lower()
        shown = 0
        for row in range(self.table.rowCount()):
            text = " ".join(
                self.table.item(row, column).text() for column in range(self.table.columnCount())
            ).lower()
            visible = not query or query in text
            self.table.setRowHidden(row, not visible)
            shown += int(visible)
        self.status.setText(f"{shown} of {len(self.sessions)} sessions")

    def selected(self) -> Session | None:
        row = self.table.currentRow()
        if row < 0:
            QMessageBox.information(self, "Session Hub", "Select a session first.")
            return None
        key = self.table.item(row, 0).data(Qt.ItemDataRole.UserRole + 1)
        return next((item for item in self.sessions if item.key == key), None)

    def selected_sessions(self) -> list[Session]:
        keys = {
            self.table.item(index.row(), 0).data(Qt.ItemDataRole.UserRole + 1)
            for index in self.table.selectionModel().selectedRows()
        }
        return [session for session in self.sessions if session.key in keys]

    def save_override(self, session: Session, field: str, value: str) -> None:
        entry = self.metadata.setdefault("sessions", {}).setdefault(session.key, {})
        entry[field] = value
        write_metadata(self.metadata)
        self.refresh()

    def rename_selected(self) -> None:
        session = self.selected()
        if not session:
            return
        name, accepted = QInputDialog.getText(
            self, "Rename session", "Display name:", text=session.title
        )
        if accepted and name.strip():
            self.save_override(session, "name", name.strip())

    def change_directory(self) -> None:
        session = self.selected()
        if not session:
            return
        start = session.cwd if Path(session.cwd).is_dir() else str(HOME)
        directory = QFileDialog.getExistingDirectory(self, "Working directory", start)
        if directory:
            self.save_override(session, "cwd", directory)

    def terminal_command(
        self,
        provider: str,
        session_id: str | None,
        cwd: str,
        source_cwd: str | None = None,
    ) -> list[str]:
        title = f"{provider} — {Path(cwd).name or cwd}"
        launch_cwd = source_cwd if provider == "Claude" and session_id else cwd
        launch_cwd = launch_cwd or cwd
        terminal = shutil.which("gnome-terminal")
        if not terminal:
            terminal = shutil.which("x-terminal-emulator")
        if not terminal:
            raise RuntimeError("No supported terminal emulator was found.")

        command = [terminal]
        if Path(terminal).name == "gnome-terminal":
            command += [
                "--window",
                f"--working-directory={launch_cwd}",
                f"--title={title}",
                "--",
            ]
        else:
            command += ["-e"]

        if provider == "Codex":
            command += [executable("codex")]
            if self.settings().get("codex_danger_mode", False):
                command += ["--dangerously-bypass-approvals-and-sandbox"]
            if session_id:
                command += ["resume", "-C", cwd, session_id]
            else:
                command += ["-C", cwd]
        elif provider == "Claude":
            command += [executable("claude")]
            if self.settings().get("claude_danger_mode", False):
                command += ["--dangerously-skip-permissions"]
            if session_id:
                command += ["--resume", session_id]
                if Path(launch_cwd) != Path(cwd):
                    command += [f"/cd {cwd}"]
        else:
            command += [executable("agy")]
            if self.settings().get("antigravity_danger_mode", False):
                command += ["--dangerously-skip-permissions"]
            if session_id:
                command += ["--conversation", session_id]
        return command

    def launch(
        self,
        provider: str,
        session_id: str | None,
        cwd: str,
        source_cwd: str | None = None,
    ) -> None:
        if not Path(cwd).is_dir():
            QMessageBox.warning(self, "Missing directory", f"This directory does not exist:\n{cwd}")
            return
        if source_cwd and not Path(source_cwd).is_dir():
            QMessageBox.warning(
                self,
                "Missing original directory",
                f"The session's original directory does not exist:\n{source_cwd}",
            )
            return
        try:
            subprocess.Popen(
                self.terminal_command(provider, session_id, cwd, source_cwd),
                start_new_session=True,
            )
        except (OSError, RuntimeError) as error:
            QMessageBox.critical(self, "Could not launch session", str(error))

    def resume_selected(self) -> None:
        session = self.selected()
        if session:
            self.launch(
                session.provider,
                session.session_id,
                session.cwd,
                session.source_cwd,
            )

    def linked_conversations(self, session: Session) -> list[Session]:
        if not session.linked_keys:
            return []
        native_sessions = native_session_index()
        conversations = [
            native_sessions[key]
            for key in session.linked_keys
            if key != session.native_key and key in native_sessions
        ]
        return sorted(conversations, key=lambda item: item.updated_ms, reverse=True)

    def open_linked_conversation(self) -> None:
        session = self.selected()
        if not session:
            return
        conversations = self.linked_conversations(session)
        if not conversations:
            QMessageBox.information(
                self,
                "No linked conversations",
                "This session has no other available native agent conversations.",
            )
            return
        labels = []
        for item in conversations:
            title = item.title or item.session_id[:8]
            if len(title) > 60:
                title = title[:57] + "…"
            labels.append(f"{item.provider} — {title}  [{item.session_id[:8]}]")

        selected_label, accepted = QInputDialog.getItem(
            self,
            "Open linked conversation",
            "Conversation:",
            labels,
            0,
            False,
        )
        if not accepted:
            return
        selected_index = labels.index(selected_label)
        conversation = conversations[selected_index]
        self.launch(
            conversation.provider,
            conversation.session_id,
            conversation.cwd,
            conversation.source_cwd,
        )

    def handoff_terminal_command(
        self,
        target_provider: str,
        cwd: str,
        handoff_path: Path,
        title: str,
        target_session_id: str | None = None,
        resume_existing: bool = False,
        source_cwd: str | None = None,
    ) -> list[str]:
        terminal = shutil.which("gnome-terminal") or shutil.which(
            "x-terminal-emulator"
        )
        if not terminal:
            raise RuntimeError("No supported terminal emulator was found.")
        prompt = (
            f"Continue the existing task using the handoff file at {handoff_path}. "
            "Read the entire file first, using section ranges or chunks if one tool "
            "output is truncated. Then inspect the current project state and continue "
            "naturally."
        )
        launch_cwd = source_cwd if target_provider == "Claude" and resume_existing else cwd
        launch_cwd = launch_cwd or cwd
        command = [terminal]
        if Path(terminal).name == "gnome-terminal":
            command += [
                "--window",
                f"--working-directory={launch_cwd}",
                f"--title={target_provider} — {title}",
                "--",
            ]
        else:
            command += ["-e"]
        if target_provider == "Claude":
            command += [executable("claude")]
            if self.settings().get("claude_danger_mode", False):
                command += ["--dangerously-skip-permissions"]
            if target_session_id:
                command += [
                    "--resume" if resume_existing else "--session-id",
                    target_session_id,
                ]
            if not resume_existing:
                command += ["--name", title]
            command += [prompt]
        elif target_provider == "Codex":
            command += [executable("codex")]
            if self.settings().get("codex_danger_mode", False):
                command += ["--dangerously-bypass-approvals-and-sandbox"]
            if target_session_id and resume_existing:
                command += ["resume", "-C", cwd, target_session_id, prompt]
            else:
                command += ["-C", cwd, prompt]
        else:
            command += [executable("agy")]
            if self.settings().get("antigravity_danger_mode", False):
                command += ["--dangerously-skip-permissions"]
            if target_session_id and resume_existing:
                command += ["--conversation", target_session_id]
            command += ["--prompt-interactive", prompt]
        return command

    def summary_terminal_command(self, session: Session) -> list[str]:
        terminal = shutil.which("gnome-terminal") or shutil.which(
            "x-terminal-emulator"
        )
        if not terminal:
            raise RuntimeError("No supported terminal emulator was found.")
        SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
        prompt = summary_prompt(session)
        launch_cwd = session.source_cwd if session.provider == "Claude" else session.cwd
        command = [terminal]
        if Path(terminal).name == "gnome-terminal":
            command += [
                "--window",
                f"--working-directory={launch_cwd}",
                f"--title={session.provider} — Prepare handoff",
                "--",
            ]
        else:
            command += ["-e"]
        if session.provider == "Codex":
            command += [executable("codex")]
            if self.settings().get("codex_danger_mode", False):
                command += ["--dangerously-bypass-approvals-and-sandbox"]
            command += ["resume", "-C", session.cwd, session.session_id, prompt]
        elif session.provider == "Claude":
            command += [executable("claude")]
            if self.settings().get("claude_danger_mode", False):
                command += ["--dangerously-skip-permissions"]
            command += ["--resume", session.session_id, prompt]
        else:
            command += [executable("agy")]
            if self.settings().get("antigravity_danger_mode", False):
                command += ["--dangerously-skip-permissions"]
            command += [
                "--conversation",
                session.session_id,
                "--prompt-interactive",
                prompt,
            ]
        return command

    def prepare_handoff_summary(self) -> None:
        session = self.selected()
        if not session:
            return
        path = summary_path(session.key)
        existing = path.is_file()
        answer = QMessageBox.question(
            self,
            "Prepare handoff summary?",
            f"This will resume {session.provider} and use some of its remaining "
            f"usage to {'replace' if existing else 'create'} a structured handoff "
            f"summary.\n\nOutput:\n{path}\n\nContinue?",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            subprocess.Popen(
                self.summary_terminal_command(session),
                start_new_session=True,
            )
        except (OSError, RuntimeError) as error:
            QMessageBox.critical(
                self, "Could not prepare handoff summary", str(error)
            )

    def continue_with_other_agent(self) -> None:
        session = self.selected()
        if not session:
            return
        targets = [provider for provider in PROVIDERS if provider != session.provider]
        target, accepted = QInputDialog.getItem(
            self,
            "Continue with another agent",
            "Destination agent:",
            targets,
            0,
            False,
        )
        if not accepted:
            return
        answer = QMessageBox.question(
            self,
            f"Continue with {target}?",
            f"Create a local handoff and continue “{session.title}” with {target}?\n\n"
            "Session Hub will keep one visible row. The original native transcript "
            "will remain stored but hidden.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            handoff = write_handoff(session, target)
            logical_key = session.key
            members = list(session.linked_keys or (session.native_key,))
            link = self.metadata.setdefault("links", {}).setdefault(
                logical_key, {"members": members, "active": session.native_key}
            )
            for member in members:
                if member not in link["members"]:
                    link["members"].append(member)

            native_sessions = native_session_index()
            existing_targets = [
                native_sessions[key]
                for key in link["members"]
                if key in native_sessions and native_sessions[key].provider == target
            ]
            existing_target = (
                max(existing_targets, key=lambda item: item.updated_ms)
                if existing_targets
                else None
            )

            if existing_target:
                link["active"] = existing_target.native_key
                command = self.handoff_terminal_command(
                    target,
                    session.cwd,
                    handoff,
                    session.title,
                    existing_target.session_id,
                    resume_existing=True,
                    source_cwd=existing_target.source_cwd,
                )
            elif target == "Claude":
                target_id = str(uuid.uuid4())
                target_key = f"Claude:{target_id}"
                link["members"].append(target_key)
                link["active"] = target_key
                command = self.handoff_terminal_command(
                    target, session.cwd, handoff, session.title, target_id
                )
            else:
                provider_sessions = (
                    codex_sessions() if target == "Codex" else antigravity_sessions()
                )
                existing = [item.native_key for item in provider_sessions]
                self.metadata.setdefault("pending_handoffs", []).append(
                    {
                        "logical_key": logical_key,
                        "target_provider": target,
                        "existing_keys": existing,
                        "cwd": session.cwd,
                        "handoff_path": str(handoff),
                        "started_ms": int(datetime.now().timestamp() * 1000) - 1000,
                        "expires_ms": int(datetime.now().timestamp() * 1000)
                        + 15 * 60 * 1000,
                    }
                )
                command = self.handoff_terminal_command(
                    target, session.cwd, handoff, session.title
                )
            write_metadata(self.metadata)
            subprocess.Popen(command, start_new_session=True)
            QTimer.singleShot(2500, self.poll_handoffs)
        except OSError as error:
            QMessageBox.critical(self, "Could not create handoff", str(error))

    def poll_handoffs(self) -> None:
        self.refresh()
        if self.metadata.get("pending_handoffs"):
            QTimer.singleShot(2500, self.poll_handoffs)

    def launch_new(self, provider: str) -> None:
        dialog = NewSessionDialog(provider, self.settings(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.directory:
            self.launch(provider, None, str(dialog.directory))

    def launch_selected_provider(self) -> None:
        self.launch_new(self.new_provider.currentText())

    def delete_selected(self) -> None:
        sessions = self.selected_sessions()
        if not sessions:
            QMessageBox.information(
                self, "Session Hub", "Select one or more sessions first."
            )
            return
        names = [session.title for session in sessions]
        answer = QMessageBox.warning(
            self,
            "Move sessions to Session Hub trash?",
            "\n".join(names[:8])
            + ("\n…" if len(names) > 8 else "")
            + "\n\nThe history files will be moved to Session Hub's "
            "recoverable trash. Close agents currently using these sessions first.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        failures = []
        for session in sessions:
            try:
                self.move_session_to_trash(session)
            except OSError as error:
                failures.append(f"{session.title}: {error}")
        write_metadata(self.metadata)
        self.refresh()
        if failures:
            QMessageBox.critical(
                self,
                "Some sessions could not be deleted",
                "\n".join(failures),
            )

    def move_session_to_trash(self, session: Session) -> None:
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = TRASH_DIR / session.provider.lower() / f"{stamp}-{session.session_id}"
        destination.mkdir(parents=True, exist_ok=False)
        raw_sessions = {
            item.native_key: item
            for item in codex_sessions() + claude_sessions() + antigravity_sessions()
        }
        native_sessions = [
            raw_sessions[key]
            for key in (session.linked_keys or (session.native_key,))
            if key in raw_sessions
        ]
        if not native_sessions:
            native_sessions = [session]
        moves: list[tuple[Path, str]] = []
        items = []
        for native in native_sessions:
            trash_name = f"{native.provider.lower()}-{native.path.name}"
            items.append({"trash": trash_name, "original": str(native.path)})
            moves.append((native.path, trash_name))
            if native.provider == "Claude":
                related = native.path.parent / native.session_id
                if related.is_dir():
                    related_name = f"claude-related-{native.session_id}"
                    items.append(
                        {"trash": related_name, "original": str(related)}
                    )
                    moves.append((related, related_name))
            elif native.provider == "Antigravity":
                related = ANTIGRAVITY_BRAIN / native.session_id
                if related.is_dir():
                    related_name = f"antigravity-brain-{native.session_id}"
                    items.append(
                        {"trash": related_name, "original": str(related)}
                    )
                    moves.append((related, related_name))
        metadata_override = self.metadata.setdefault("sessions", {}).get(
            session.key, {}
        )
        link_definition = self.metadata.setdefault("links", {}).get(session.key)
        manifest = {
            "provider": (
                " ↔ ".join(sorted({item.provider for item in native_sessions}))
                if len(native_sessions) > 1
                else session.provider
            ),
            "session_id": session.session_id,
            "title": session.title,
            "deleted_at": datetime.now().isoformat(),
            "items": items,
            "metadata_override": metadata_override,
            "logical_key": session.key,
            "link_definition": link_definition,
        }
        (destination / "manifest.json").write_text(
            json.dumps(manifest, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        for source, trash_name in moves:
            shutil.move(str(source), str(destination / trash_name))
        self.metadata.setdefault("sessions", {}).pop(session.key, None)
        self.metadata.setdefault("links", {}).pop(session.key, None)

    def context_menu(self, point) -> None:
        if self.table.itemAt(point) is None:
            return
        menu = QMenu(self)
        actions = [
            ("Resume in new terminal", self.resume_selected),
            ("Open linked conversation…", self.open_linked_conversation),
            ("Prepare handoff summary", self.prepare_handoff_summary),
            ("Continue with other agent", self.continue_with_other_agent),
            ("Rename", self.rename_selected),
            ("Change directory", self.change_directory),
            ("Delete", self.delete_selected),
        ]
        for label, slot in actions:
            action = QAction(label, self)
            action.triggered.connect(slot)
            menu.addAction(action)
        menu.exec(self.table.viewport().mapToGlobal(point))


def diagnostic() -> int:
    metadata = read_metadata()
    sessions = discover_sessions(metadata)
    print(
        json.dumps(
            {
                "total": len(sessions),
                "codex": sum(item.provider == "Codex" for item in sessions),
                "claude": sum(item.provider == "Claude" for item in sessions),
                "antigravity": sum(
                    item.provider == "Antigravity" for item in sessions
                ),
                "missing_directories": [
                    {"provider": item.provider, "id": item.session_id, "cwd": item.cwd}
                    for item in sessions
                    if not Path(item.cwd).is_dir()
                ],
            },
            indent=2,
        )
    )
    return 0


def main() -> int:
    if "--diagnose" in sys.argv:
        return diagnostic()
    app = QApplication(sys.argv)
    app.setApplicationName("Session Hub")
    app.setDesktopFileName("session-hub")
    if APP_ICON.is_file():
        app.setWindowIcon(QIcon(str(APP_ICON)))
    app.setStyle("Fusion")
    window = SessionHub()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
