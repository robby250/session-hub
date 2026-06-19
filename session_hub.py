#!/usr/bin/env python3
"""Desktop launcher for local Claude Code and Codex sessions."""

from __future__ import annotations

import json
import os
import re
import select
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QObject, QRunnable, QThreadPool, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
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
DATA_DIR = Path(os.environ.get("XDG_DATA_HOME", HOME / ".local/share")) / "session-hub"
METADATA_PATH = DATA_DIR / "metadata.json"
TRASH_DIR = DATA_DIR / "trash"


@dataclass
class Session:
    provider: str
    session_id: str
    title: str
    cwd: str
    source_cwd: str
    updated_ms: int
    path: Path

    @property
    def key(self) -> str:
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
            reader = read_codex_usage if self.provider == "Codex" else read_claude_usage
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


def discover_sessions(metadata: dict) -> list[Session]:
    sessions = codex_sessions() + claude_sessions()
    overrides = metadata.setdefault("sessions", {})
    for session in sessions:
        custom = overrides.get(session.key, {})
        session.title = custom.get("name") or session.title
        session.cwd = custom.get("cwd") or session.cwd
    return sorted(sessions, key=lambda item: item.updated_ms, reverse=True)


def executable(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    local = HOME / ".local" / "bin" / name
    return str(local)


class SettingsDialog(QDialog):
    def __init__(self, settings: dict, parent=None) -> None:
        super().__init__(parent)
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
        self.codex_danger.setChecked(bool(settings.get("codex_danger_mode", False)))
        self.claude_danger.setChecked(bool(settings.get("claude_danger_mode", False)))
        group_layout.addWidget(self.codex_danger)
        group_layout.addWidget(self.claude_danger)
        layout.addWidget(group)

        warning = QLabel(
            "Danger mode lets an agent execute commands and modify files without "
            "normal approval checks. These switches affect launches from Session Hub only."
        )
        warning.setWordWrap(True)
        warning.setStyleSheet("color: #d9534f;")
        layout.addWidget(warning)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> dict:
        return {
            "codex_danger_mode": self.codex_danger.isChecked(),
            "claude_danger_mode": self.claude_danger.isChecked(),
        }


class SessionHub(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.metadata = read_metadata()
        self.sessions: list[Session] = []
        self.usage_widgets: dict[str, list[tuple[QLabel, QProgressBar, QLabel]]] = {}
        self.usage_workers: dict[str, UsageWorker] = {}
        self.thread_pool = QThreadPool.globalInstance()
        self.setWindowTitle("Session Hub")
        self.setWindowIcon(QIcon.fromTheme("utilities-terminal"))
        self.resize(1280, 900)
        self.setMinimumSize(900, 650)
        self.build_ui()
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

        for label, slot in (
            ("New Codex", lambda: self.launch_new("Codex")),
            ("New Claude", lambda: self.launch_new("Claude")),
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
        for column, provider in enumerate(("Codex", "Claude")):
            offset = column * 2
            usage_layout.addWidget(QLabel(f"<b>{provider} usage</b>"), 0, offset, 1, 2)
            rows = []
            for index, window_name in enumerate(("5-hour", "Weekly")):
                label = QLabel(window_name)
                bar = QProgressBar()
                bar.setRange(0, 100)
                bar.setValue(0)
                bar.setFormat("Loading…")
                detail = QLabel("")
                detail.setStyleSheet("color: #888;")
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
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
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

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.settings(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.metadata["settings"] = dialog.values()
            write_metadata(self.metadata)

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
            by_name = {window.name: window for window in windows}
            for expected, (_, bar, detail) in zip(("5-hour", "Weekly"), rows):
                window = by_name.get(expected)
                if not window:
                    bar.setFormat("Unavailable")
                    detail.setText("")
                    continue
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
        self.refresh()
        self.refresh_usage()

    def refresh(self) -> None:
        self.sessions = discover_sessions(self.metadata)
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(self.sessions))
        for row, session in enumerate(self.sessions):
            agent = QTableWidgetItem(session.provider)
            agent.setForeground(QColor("#d977ff") if session.provider == "Claude" else QColor("#5aa9ff"))
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
        else:
            command += [executable("claude")]
            if self.settings().get("claude_danger_mode", False):
                command += ["--dangerously-skip-permissions"]
            if session_id:
                command += ["--resume", session_id]
                if Path(launch_cwd) != Path(cwd):
                    command += [f"/cd {cwd}"]
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

    def launch_new(self, provider: str) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, f"Start new {provider} session in…", str(HOME)
        )
        if directory:
            self.launch(provider, None, directory)

    def delete_selected(self) -> None:
        session = self.selected()
        if not session:
            return
        answer = QMessageBox.warning(
            self,
            "Move session to Session Hub trash?",
            f"{session.title}\n\n"
            "The history file will be moved to Session Hub's recoverable trash. "
            "Any currently running agent using this session should be closed first.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        destination = TRASH_DIR / session.provider.lower() / f"{stamp}-{session.session_id}"
        try:
            destination.mkdir(parents=True, exist_ok=False)
            shutil.move(str(session.path), str(destination / session.path.name))
            if session.provider == "Claude":
                related = session.path.parent / session.session_id
                if related.is_dir():
                    shutil.move(str(related), str(destination / related.name))
            self.metadata.setdefault("sessions", {}).pop(session.key, None)
            write_metadata(self.metadata)
            self.refresh()
        except OSError as error:
            QMessageBox.critical(self, "Could not delete session", str(error))

    def context_menu(self, point) -> None:
        if self.table.itemAt(point) is None:
            return
        menu = QMenu(self)
        actions = [
            ("Resume in new terminal", self.resume_selected),
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
    app.setStyle("Fusion")
    window = SessionHub()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
