#!/usr/bin/env python3
"""Desktop launcher for local Claude Code and Codex sessions."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QDateTime, Qt
from PyQt6.QtGui import QAction, QColor, QIcon
from PyQt6.QtWidgets import (
    QApplication,
    QFileDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
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
    updated_ms: int
    path: Path

    @property
    def key(self) -> str:
        return f"{self.provider}:{self.session_id}"


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


class SessionHub(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.metadata = read_metadata()
        self.sessions: list[Session] = []
        self.setWindowTitle("Session Hub")
        self.setWindowIcon(QIcon.fromTheme("utilities-terminal"))
        self.resize(1280, 760)
        self.setMinimumSize(900, 520)
        self.build_ui()
        self.refresh()

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
            ("Refresh", self.refresh),
        ):
            button = QPushButton(label)
            button.clicked.connect(slot)
            toolbar.addWidget(button)
        layout.addLayout(toolbar)

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

    def terminal_command(self, provider: str, session_id: str | None, cwd: str) -> list[str]:
        title = f"{provider} — {Path(cwd).name or cwd}"
        terminal = shutil.which("gnome-terminal")
        if not terminal:
            terminal = shutil.which("x-terminal-emulator")
        if not terminal:
            raise RuntimeError("No supported terminal emulator was found.")

        command = [terminal]
        if Path(terminal).name == "gnome-terminal":
            command += ["--window", f"--working-directory={cwd}", f"--title={title}", "--"]
        else:
            command += ["-e"]

        if provider == "Codex":
            command += [executable("codex")]
            if session_id:
                command += ["resume", "-C", cwd, session_id]
            else:
                command += ["-C", cwd]
        else:
            command += [executable("claude")]
            if session_id:
                command += ["--resume", session_id]
        return command

    def launch(self, provider: str, session_id: str | None, cwd: str) -> None:
        if not Path(cwd).is_dir():
            QMessageBox.warning(self, "Missing directory", f"This directory does not exist:\n{cwd}")
            return
        try:
            subprocess.Popen(
                self.terminal_command(provider, session_id, cwd),
                start_new_session=True,
            )
        except (OSError, RuntimeError) as error:
            QMessageBox.critical(self, "Could not launch session", str(error))

    def resume_selected(self) -> None:
        session = self.selected()
        if session:
            self.launch(session.provider, session.session_id, session.cwd)

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
