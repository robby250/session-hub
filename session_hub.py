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
import threading
import time
import uuid
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path

from PyQt6.QtCore import QByteArray, QObject, QRunnable, QThreadPool, QTimer, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QColor,
    QDesktopServices,
    QIcon,
    QKeySequence,
    QShortcut,
)
from PyQt6.QtWidgets import (
    QAbstractItemView,
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
    QSlider,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
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

# Session transcripts can grow to hundreds of MB over long-running sessions.
# Re-scanning every file on every refresh (startup, after any metadata save,
# manual refresh, ...) is the dominant cost in this file, so scan results are
# cached per path and invalidated by (mtime, size). Unchanged files - the
# overwhelming majority on any given refresh - cost a single stat() call.
_FILE_SCAN_CACHE: dict[str, tuple[tuple[float, int], dict]] = {}


def _file_signature(path: Path) -> tuple[float, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime, stat.st_size)


def _cached_file_scan(path: Path, scan) -> dict:
    """Return scan(path) result, cached until the file's mtime/size change."""
    signature = _file_signature(path)
    if signature is None:
        return {}
    key = str(path)
    cached = _FILE_SCAN_CACHE.get(key)
    if cached is not None and cached[0] == signature:
        return cached[1]
    result = scan(path)
    _FILE_SCAN_CACHE[key] = (signature, result)
    return result
TRASH_DIR = DATA_DIR / "trash"
HANDOFF_DIR = DATA_DIR / "handoffs"
SUMMARY_DIR = HANDOFF_DIR / "summaries"
# Tracks Claude processes Session Hub itself launched, so a same-directory
# /clear inside that same process (new session id, same PID) can be detected
# and linked to the session it continues - see pid_capture_command and
# resolve_clear_continuations.
PID_DIR = DATA_DIR / "pids"
PROC_ROOT = Path("/proc")
APP_ICON = Path(__file__).resolve().parent / "assets" / "session-hub.svg"
# One-off sessions launch here instead of literally $HOME, so they don't
# scatter loose files/clones directly in the home directory.
DEFAULT_SESSION_DIR = HOME / "projects"
PROVIDERS = ("Codex", "Claude", "Antigravity")
# Claude's CLI has no command to enumerate models, but --model accepts these
# family aliases, which always resolve to the latest model of each family, so
# they stay valid across model refreshes. "Default" omits --model entirely.
CLAUDE_MODELS = (
    ("Default", None),
    ("Opus", "opus"),
    ("Sonnet", "sonnet"),
    ("Haiku", "haiku"),
    ("Fable", "fable"),
)
# Shared session-table column set: SessionHub's main listview and
# ManageGroupDialog both render from this (see SessionHub.populate_session_table)
# so their common columns are defined once, in one order.
SESSION_TABLE_COLUMNS = ("Agent", "Model", "Name", "Working directory", "Last updated", "Session ID")
# Catalog of well-known agent environment variables. Each spec drives the
# value editor (a slider, spin box, dropdown, or text field, per "kind") and
# the description shown when the row is selected. The editor still accepts any
# custom name/value pair; this catalog just makes the common knobs typed and
# self-documenting.
#   kind "percent" -> slider + spin box, 0-100
#   kind "int"     -> spin box with min/max/step and an optional suffix
#   kind "toggle"  -> Off (unset) / On (1) dropdown
#   kind "choice"  -> dropdown of (label, value) pairs
#   kind "text"    -> line edit, or an editable dropdown when "suggestions" given
ENV_VAR_SPECS: dict[str, dict] = {
    "CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": {
        "kind": "percent",
        "default": 70,
        "description": (
            "Context-window fill % at which Claude Code auto-compacts the "
            "conversation. Lower compacts earlier, keeping the working context "
            "fresher (less rot) at the cost of more frequent, lossy summaries; "
            "~70–75 is a good balance. The built-in default is ~92."
        ),
    },
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": {
        "kind": "int", "min": 1024, "max": 200000, "step": 1024,
        "default": 32000, "suffix": " tokens",
        "description": "Maximum tokens Claude may produce in a single response.",
    },
    "MAX_THINKING_TOKENS": {
        "kind": "int", "min": 0, "max": 128000, "step": 1024,
        "default": 16000, "suffix": " tokens",
        "description": (
            "Fixed extended-thinking (reasoning) budget. Only takes effect when "
            "adaptive reasoning is off; on current models (which manage the "
            "budget themselves) setting it usually has no benefit."
        ),
    },
    "ANTHROPIC_MODEL": {
        "kind": "text",
        "suggestions": ["opus", "sonnet", "haiku", "fable"],
        "placeholder": "opus / sonnet / full model id",
        "description": "Override the default model. A family alias or a full model id.",
    },
    "BASH_DEFAULT_TIMEOUT_MS": {
        "kind": "int", "min": 1000, "max": 1800000, "step": 1000,
        "default": 120000, "suffix": " ms",
        "description": "Default timeout for a Bash command before it is interrupted.",
    },
    "BASH_MAX_TIMEOUT_MS": {
        "kind": "int", "min": 1000, "max": 3600000, "step": 1000,
        "default": 600000, "suffix": " ms",
        "description": "Maximum timeout a Bash command may request.",
    },
    "BASH_MAX_OUTPUT_LENGTH": {
        "kind": "int", "min": 1000, "max": 1000000, "step": 1000,
        "default": 30000, "suffix": " chars",
        "description": "Maximum characters of Bash output kept before truncation.",
    },
    "MCP_TIMEOUT": {
        "kind": "int", "min": 1000, "max": 600000, "step": 1000,
        "default": 30000, "suffix": " ms",
        "description": "How long to wait for an MCP server to start up.",
    },
    "MCP_TOOL_TIMEOUT": {
        "kind": "int", "min": 1000, "max": 600000, "step": 1000,
        "default": 60000, "suffix": " ms",
        "description": "How long to wait for a single MCP tool call to return.",
    },
    "MAX_MCP_OUTPUT_TOKENS": {
        "kind": "int", "min": 1000, "max": 200000, "step": 1000,
        "default": 25000, "suffix": " tokens",
        "description": "Maximum tokens accepted from a single MCP tool result.",
    },
    "DISABLE_AUTOUPDATER": {
        "kind": "toggle",
        "description": "Stop Claude Code from automatically updating itself.",
    },
    "DISABLE_TELEMETRY": {
        "kind": "toggle",
        "description": "Opt out of anonymized usage telemetry (Statsig).",
    },
    "DISABLE_ERROR_REPORTING": {
        "kind": "toggle",
        "description": "Opt out of automatic error reporting (Sentry).",
    },
    "DISABLE_COST_WARNINGS": {
        "kind": "toggle",
        "description": "Hide the spending / cost-limit warning messages.",
    },
    "DISABLE_NON_ESSENTIAL_MODEL_CALLS": {
        "kind": "toggle",
        "description": "Skip non-critical background model calls such as conversation auto-titles.",
    },
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": {
        "kind": "toggle",
        "description": "Disable all non-essential network traffic (telemetry, update checks, etc.).",
    },
    "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": {
        "kind": "toggle",
        "description": "Stop Claude Code from updating the terminal window title.",
    },
    "USE_BUILTIN_RIPGREP": {
        "kind": "choice",
        "choices": [("Bundled ripgrep (default)", "1"), ("System ripgrep", "0")],
        "description": "Whether to use Claude Code's bundled ripgrep or the system-installed one.",
    },
    "HTTPS_PROXY": {
        "kind": "text",
        "placeholder": "http://proxy.example:8080",
        "description": "Route all outbound HTTPS (API) traffic through this proxy URL.",
    },
}

CUSTOM_ENV_DESCRIPTION = "Custom variable — its value is passed through to the agent unchanged."

# Catalog of well-known Claude CLI launch flags, structured identically to
# ENV_VAR_SPECS (same "kind" system) so EnvEditor renders both catalogs with
# the same widgets. Unlike env vars, these are appended to the launch command
# line rather than passed through the process environment, and only apply to
# the Claude provider.
# --- Caveman mode -----------------------------------------------------------
# Compressed-prose mode, injected with `claude --append-system-prompt`. Doing it
# that way means it needs nothing installed and cannot drift out of sync with a
# skill on disk -- the text below IS the instruction the session gets.
#
# When the upstream skill (github.com/JuliusBrussee/caveman) is also installed
# the two agree on intensity and DISAGREE ON SCOPE, on purpose. Upstream lists
# "code, comments, commit messages, file contents, memory files" as things it
# must never compress; the "+files" levels here exist because that exclusion is
# exactly what the user asked to remove. The artifact block says so in the
# prompt itself, so a session holding both instructions is told which one wins
# instead of being left to arbitrate.
CAVEMAN_INTENSITY = {
    "lite": "Cut filler, hedging and preamble. Keep articles and sentence shape.",
    "full": "Cut articles, copulas, pronouns, filler. Short declarative fragments.",
    "ultra": "Telegraphic. Content words only. Fragments always, never sentences.",
}

CAVEMAN_BASE = """\
CAVEMAN MODE ({level}) is active for this session. {intensity}

Never compress, at any level: code, identifiers, API and function names, CLI
commands, file paths, error strings, numbers, and quoted third-party text.
Reproduce all of those byte-for-byte.

Return to ordinary prose on your own, without being asked, for: security
warnings, confirming any irreversible or outward-facing action, and any step
sequence where dropping connective words would make the order ambiguous. Resume
caveman immediately after. Terseness must never be the reason a warning is
misread.

Exit on "stop caveman" or "normal mode"."""

CAVEMAN_ARTIFACTS = """\
SCOPE -- this covers prose you WRITE TO FILES, not just chat: code comments and
docstrings, task-tracker entries, memory-file bodies, design and handoff docs,
PR and issue text, and GIT COMMIT MESSAGES. This deliberately overrides any
installed caveman skill's rule that persisted text is exempt -- including its
commit-message exemption, which the user removed on purpose after being told
commits are published to a remote.

Two carve-outs, because each one is machine-read and breaks otherwise:
  1. Memory frontmatter `description:` stays ordinary prose. It is matched
     against to decide relevance during recall, so a telegraphic one retrieves
     worse -- the cost lands later, on a session that never sees this note.
  2. Structural syntax is untouched: checkbox and task-id prefixes, `| EXIT:`
     and status segments, frontmatter keys, conventional-commit type prefixes
     (`feat:`, `fix:`, `docs:`), table headers, markdown scaffolding. Compress
     the prose INSIDE a field, never the field around it."""


def caveman_system_prompt(value: str) -> str | None:
    """Expand a `--caveman` picker value into --append-system-prompt text.

    Returns None for off/unset/unrecognised, so a stale or hand-typed value
    degrades to "no caveman" rather than to a broken command line.
    """
    token = str(value or "").strip().lower()
    if not token or token == "off":
        return None
    level, _, scope = token.partition("+")
    intensity = CAVEMAN_INTENSITY.get(level)
    if intensity is None or scope not in ("", "files"):
        # An unknown SCOPE is rejected outright rather than falling back to the
        # bare level: "full+flies" would otherwise turn on caveman while quietly
        # dropping the artifact coverage that was the reason for typing it.
        return None
    prompt = CAVEMAN_BASE.format(level=level, intensity=intensity)
    if scope == "files":
        prompt += "\n\n" + CAVEMAN_ARTIFACTS
    return prompt


CLI_FLAG_SPECS: dict[str, dict] = {
    "--caveman": {
        "kind": "choice",
        "choices": [
            ("Off", ""),
            ("Lite — chat only", "lite"),
            ("Full — chat only", "full"),
            ("Ultra — chat only", "ultra"),
            ("Full + written artifacts", "full+files"),
            ("Ultra + written artifacts", "ultra+files"),
        ],
        "description": (
            "Compress this session's prose. Expands to --append-system-prompt, "
            "so no skill install is needed. The '+ written artifacts' levels "
            "extend it to code comments, task entries, memory bodies and docs; "
            "commit messages, memory `description:` lines and machine-parsed "
            "structure stay plain either way."
        ),
    },
    "--effort": {
        "kind": "choice",
        "choices": [
            ("Not set", ""),
            ("Low", "low"),
            ("Medium", "medium"),
            ("High (default)", "high"),
            ("Xhigh", "xhigh"),
            ("Max", "max"),
        ],
        "description": (
            "Reasoning/response effort for this session. Higher levels spend "
            "more tokens on thinking and tool calls for better results; lower "
            "levels trade some capability for speed and cost. Overrides the "
            "session's default and does not persist beyond it."
        ),
    },
    "--fallback-model": {
        "kind": "text",
        "suggestions": ["opus", "sonnet", "haiku", "fable"],
        "placeholder": "sonnet / full model id",
        "description": "Model to fall back to automatically if the primary model is overloaded.",
    },
    "--name": {
        "kind": "text",
        "placeholder": "session-name",
        "description": (
            "Display name for this session - shown in /resume and the "
            "terminal title, and how other Claude Code sessions on this "
            "machine address it via cross-session messaging."
        ),
    },
    "--max-turns": {
        "kind": "int", "min": 1, "max": 100000, "step": 1,
        "default": 100,
        "description": "Maximum number of agentic turns before the session stops itself.",
    },
    "--chrome": {
        "kind": "flag",
        "description": "Enable the Claude in Chrome browser integration for this session.",
    },
}

CUSTOM_FLAG_DESCRIPTION = "Custom flag — passed through to the agent's CLI unchanged."


def env_int(value, fallback: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return int(fallback)


def suggest_session_name(
    directory: Path | None, model_alias: str | None, existing_names: set[str]
) -> str:
    """`<dirname>-<model>` for a model's first row in a group, `-2`/`-3`/...
    for further rows of the same model - a starting point only, since the
    name field stays freely editable in the dialogs that call this.
    """
    base = (directory.name if directory else "") or "session"
    if model_alias:
        base = f"{base}-{model_alias}"
    if base not in existing_names:
        return base
    suffix = 2
    while f"{base}-{suffix}" in existing_names:
        suffix += 1
    return f"{base}-{suffix}"


class EnvEditor(QWidget):
    """Name/value table with typed value editors, driven by a catalog of specs.

    Renders either environment variables (the default catalog) or CLI launch
    flags (pass specs=CLI_FLAG_SPECS) — same widgets and editing behavior,
    just a different catalog, column label, and item noun.
    """

    def __init__(
        self,
        env: dict | None = None,
        parent=None,
        *,
        specs: dict | None = None,
        name_label: str = "Variable",
        item_noun: str = "variable",
        custom_description: str | None = None,
    ) -> None:
        super().__init__(parent)
        self.specs = specs if specs is not None else ENV_VAR_SPECS
        self.custom_description = custom_description or CUSTOM_ENV_DESCRIPTION
        self.item_noun = item_noun
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels([name_label, "Value"])
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(150)
        self.table.currentCellChanged.connect(self.update_description)
        layout.addWidget(self.table)

        self.description = QLabel(f"Add a {item_noun} to see what it does.")
        self.description.setWordWrap(True)
        self.description.setStyleSheet("color: #888;")
        self.description.setMinimumHeight(
            self.description.fontMetrics().lineSpacing() * 2
        )
        layout.addWidget(self.description)

        controls = QHBoxLayout()
        self.suggestions = QComboBox()
        self.suggestions.addItem(f"Add a known {item_noun}…", None)
        for name, spec in self.specs.items():
            self.suggestions.addItem(name, name)
            self.suggestions.setItemData(
                self.suggestions.count() - 1,
                f"{name}\n{spec['description']}",
                Qt.ItemDataRole.ToolTipRole,
            )
        self.suggestions.activated.connect(self.insert_suggested)
        add_custom = QPushButton("Add custom…")
        add_custom.clicked.connect(self.add_custom_row)
        remove = QPushButton("Remove selected")
        remove.clicked.connect(self.remove_selected)
        controls.addWidget(self.suggestions, 1)
        controls.addWidget(add_custom)
        controls.addWidget(remove)
        layout.addLayout(controls)

        self.set_env(env or {})

    # -- value editors -----------------------------------------------------
    def value_widget(self, name: str, value: str) -> QWidget:
        spec = self.specs.get(name)
        kind = spec.get("kind") if spec else "text"
        if kind == "percent":
            return self._percent_widget(spec, value)
        if kind == "int":
            return self._int_widget(spec, value)
        if kind in ("toggle", "flag", "choice"):
            return self._combo_widget(spec, value)
        return self._text_widget(spec, value)

    def _percent_widget(self, spec: dict, value: str) -> QWidget:
        lo, hi = spec.get("min", 0), spec.get("max", 100)
        container = QWidget()
        row = QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        slider = QSlider(Qt.Orientation.Horizontal)
        slider.setRange(lo, hi)
        spin = QSpinBox()
        spin.setRange(lo, hi)
        spin.setSuffix("%")
        initial = max(lo, min(hi, env_int(value, spec.get("default", lo))))
        slider.setValue(initial)
        spin.setValue(initial)
        slider.valueChanged.connect(spin.setValue)
        spin.valueChanged.connect(slider.setValue)
        row.addWidget(slider, 1)
        row.addWidget(spin)
        container.env_value = lambda: str(spin.value())
        return container

    def _int_widget(self, spec: dict, value: str) -> QWidget:
        spin = QSpinBox()
        spin.setRange(spec.get("min", 0), spec.get("max", 2_000_000_000))
        spin.setSingleStep(spec.get("step", 1))
        if spec.get("suffix"):
            spin.setSuffix(spec["suffix"])
        spin.setValue(
            max(
                spec.get("min", 0),
                min(
                    spec.get("max", 2_000_000_000),
                    env_int(value, spec.get("default", spec.get("min", 0))),
                ),
            )
        )
        spin.env_value = lambda: str(spin.value())
        return spin

    def _combo_widget(self, spec: dict, value: str) -> QWidget:
        combo = QComboBox()
        if spec.get("kind") == "toggle":
            choices = [("Off (not set)", ""), ("On (1)", "1")]
        elif spec.get("kind") == "flag":
            choices = [("Not set", ""), ("On (no value)", "1")]
        else:
            choices = spec.get("choices", [])
        for label, data in choices:
            combo.addItem(label, data)
        value = str(value)
        index = combo.findData(value)
        if index < 0 and value:
            combo.addItem(f"{value} (custom)", value)
            index = combo.count() - 1
        combo.setCurrentIndex(max(0, index))
        combo.env_value = lambda: str(combo.currentData() or "")
        return combo

    def _text_widget(self, spec: dict | None, value: str) -> QWidget:
        placeholder = spec.get("placeholder", "") if spec else ""
        suggestions = spec.get("suggestions") if spec else None
        if suggestions:
            combo = QComboBox()
            combo.setEditable(True)
            combo.addItems(suggestions)
            combo.setCurrentText(str(value))
            if placeholder:
                combo.lineEdit().setPlaceholderText(placeholder)
            combo.env_value = lambda: combo.currentText().strip()
            return combo
        edit = QLineEdit(str(value))
        if placeholder:
            edit.setPlaceholderText(placeholder)
        edit.env_value = lambda: edit.text().strip()
        return edit

    # -- rows --------------------------------------------------------------
    def add_known_row(self, name: str, value: str = "") -> None:
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            if item and item.text().strip() == name:
                self.table.setCurrentCell(row, 1)
                return
        row = self.table.rowCount()
        self.table.insertRow(row)
        name_item = QTableWidgetItem(name)
        name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        spec = self.specs.get(name)
        if spec:
            name_item.setToolTip(spec["description"])
        self.table.setItem(row, 0, name_item)
        self.table.setCellWidget(row, 1, self.value_widget(name, value))
        self.table.setCurrentCell(row, 1)

    def add_custom_row(self, name: str = "", value: str = "") -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(str(name)))
        self.table.setItem(row, 1, QTableWidgetItem(str(value)))
        self.table.setCurrentCell(row, 0)

    def insert_suggested(self, index: int) -> None:
        name = self.suggestions.itemData(index)
        self.suggestions.setCurrentIndex(0)
        if name:
            self.add_known_row(name)

    def remove_selected(self) -> None:
        rows = sorted(
            {index.row() for index in self.table.selectedIndexes()}, reverse=True
        )
        for row in rows:
            self.table.removeRow(row)

    def update_description(self, row: int, *args) -> None:
        if row < 0:
            return
        item = self.table.item(row, 0)
        name = item.text().strip() if item else ""
        spec = self.specs.get(name)
        if not name:
            self.description.setText(f"Add a {self.item_noun} to see what it does.")
        elif spec:
            self.description.setText(f"{name} — {spec['description']}")
        else:
            self.description.setText(f"{name} — {self.custom_description}")

    def set_env(self, env: dict) -> None:
        self.table.setRowCount(0)
        for name, value in env.items():
            if str(name) in self.specs:
                self.add_known_row(str(name), str(value))
            else:
                self.add_custom_row(str(name), str(value))

    def env(self) -> dict:
        result: dict[str, str] = {}
        for row in range(self.table.rowCount()):
            name_item = self.table.item(row, 0)
            name = (name_item.text() if name_item else "").strip()
            if not name:
                continue
            widget = self.table.cellWidget(row, 1)
            if widget is not None and hasattr(widget, "env_value"):
                value = widget.env_value()
            else:
                value_item = self.table.item(row, 1)
                value = (value_item.text() if value_item else "").strip()
            if value == "":
                continue
            result[name] = value
        return result


class LaunchOptionsEditor(QWidget):
    """Tabbed pairing of an environment-variable editor and a CLI-flag editor.

    Env vars are injected into the launched process's environment; flags are
    appended to the launch command line. Both are per-session-overridable the
    same way, so they share one editor (this class) wherever either is edited.
    """

    def __init__(
        self, env: dict | None = None, flags: dict | None = None, parent=None
    ) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        tabs = QTabWidget()
        self.env_editor = EnvEditor(env or {})
        self.flags_editor = EnvEditor(
            flags or {},
            specs=CLI_FLAG_SPECS,
            name_label="Flag",
            item_noun="flag",
            custom_description=CUSTOM_FLAG_DESCRIPTION,
        )
        tabs.addTab(self.env_editor, "Environment variables")
        tabs.addTab(self.flags_editor, "CLI flags")
        layout.addWidget(tabs)

    def env(self) -> dict:
        return self.env_editor.env()

    def flags(self) -> dict:
        return self.flags_editor.env()


class SessionLaunchOptionsDialog(QDialog):
    """Edit the per-session environment variable and CLI flag overrides."""

    def __init__(
        self,
        session_title: str,
        global_env: dict,
        env_overrides: dict,
        global_flags: dict,
        flag_overrides: dict,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Session launch options")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        intro = QLabel(
            f"Launch options for “{session_title}”. These override the global "
            "settings for this session only."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        inherited_parts = [
            f"{name}={value}" for name, value in sorted(global_env.items())
        ] + [
            name
            if CLI_FLAG_SPECS.get(name, {}).get("kind") == "flag"
            else f"{name} {value}"
            for name, value in sorted(global_flags.items())
        ]
        if inherited_parts:
            inherited = QLabel(
                "Inherited from global settings: " + ", ".join(inherited_parts)
            )
            inherited.setWordWrap(True)
            inherited.setStyleSheet("color: #888;")
            layout.addWidget(inherited)
        self.editor = LaunchOptionsEditor(env_overrides, flag_overrides)
        layout.addWidget(self.editor)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def env(self) -> dict:
        return self.editor.env()

    def flags(self) -> dict:
        return self.editor.flags()


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
    agent_name: str | None = None

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
    window_minutes: int | None = None
    reset_epoch: float | None = None


@dataclass
class UsageActivity:
    """Fallback stats used when `/usage` omits the percentage bars (Anthropic
    has, at times, returned only the "contributing to usage" breakdown from
    headless invocations, with no `N% used · resets ...` lines to parse)."""

    label: str
    requests: int
    sessions: int


def usage_pace_text(window: UsageWindow, now: datetime | None = None) -> str | None:
    """Compare actual usage against an even pace across the window's duration."""
    if not window.window_minutes or not window.reset_epoch:
        return None
    window_seconds = window.window_minutes * 60
    if window_seconds <= 0:
        return None
    now_epoch = (now or datetime.now()).timestamp()
    remaining_seconds = max(0.0, window.reset_epoch - now_epoch)
    elapsed_fraction = max(0.0, min(1.0, 1 - remaining_seconds / window_seconds))
    expected_percent = elapsed_fraction * 100
    delta = window.used_percent - expected_percent
    if abs(delta) < 0.5:
        return f"{expected_percent:.1f}% expected · on pace"
    direction = "over" if delta > 0 else "under"
    return f"{expected_percent:.1f}% expected · {abs(delta):.1f}% {direction} pace"


def format_reset_timestamp(timestamp: int | None) -> str:
    if not timestamp:
        return "Reset time unavailable"
    value = datetime.fromtimestamp(timestamp)
    return f"Resets {value.strftime('%Y-%m-%d %H:%M')}"


def parse_claude_reset_datetime(value: str, now: datetime | None = None) -> datetime | None:
    """Parse Claude's English reset text into a local datetime, if recognized."""
    match = re.fullmatch(
        r"([A-Z][a-z]{2})\s+(\d{1,2}),\s+(\d{1,2})(?::(\d{2}))?(am|pm)"
        r"(?:\s+\([^)]+\))?",
        value.strip(),
    )
    if not match:
        return None
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
    return reset


def format_claude_reset(value: str, now: datetime | None = None) -> str:
    """Normalize Claude's English reset text to the same local format as Codex."""
    reset = parse_claude_reset_datetime(value, now)
    if reset is None:
        return f"Resets {value.strip()}"
    return f"Resets {reset.strftime('%Y-%m-%d %H:%M')}"


def parse_claude_usage(text: str) -> list[UsageWindow]:
    pattern = re.compile(
        r"^(Current session|Current week \(all models\)|Current week \(Fable\)):\s*"
        r"(\d+)% used\s*[·•]\s*resets (.+)$",
        re.MULTILINE,
    )
    names = {
        "Current session": "5-hour",
        "Current week (all models)": "Weekly",
        "Current week (Fable)": "Weekly (Fable)",
    }
    windows = []
    for label, percent, reset in pattern.findall(text):
        reset_dt = parse_claude_reset_datetime(reset)
        windows.append(
            UsageWindow(
                names[label],
                max(0, min(100, int(percent))),
                format_claude_reset(reset),
                window_minutes=300 if label == "Current session" else 10080,
                reset_epoch=reset_dt.timestamp() if reset_dt else None,
            )
        )
    return windows


def parse_claude_usage_activity(text: str) -> list[UsageActivity]:
    pattern = re.compile(r"Last (24h|7d) · ([\d,]+) requests · (\d+) sessions?")
    labels = {"24h": "Last 24h", "7d": "Last 7d"}
    activity = []
    for period, requests, sessions in pattern.findall(text):
        activity.append(
            UsageActivity(
                labels.get(period, f"Last {period}"),
                int(requests.replace(",", "")),
                int(sessions),
            )
        )
    return activity


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
                        window_minutes=duration,
                        reset_epoch=window.get("resetsAt"),
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


def read_claude_usage(timeout: float = 15.0) -> list[UsageWindow] | list[UsageActivity]:
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
    text = str(payload.get("result") or "")
    windows = parse_claude_usage(text)
    if windows:
        return windows
    activity = parse_claude_usage_activity(text)
    if activity:
        return activity
    raise RuntimeError("Claude returned no recognizable usage windows.")


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


def _scan_claude_history(path: Path) -> dict[str, dict]:
    index: dict[str, dict] = {}
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
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


def claude_history_index() -> dict[str, dict]:
    # history.jsonl is append-only and only grows while actively using Claude
    # interactively, so it's safe (and much cheaper) to cache and only
    # re-parse it when it actually changes between refreshes.
    return _cached_file_scan(CLAUDE_HISTORY, _scan_claude_history)


def claude_project_key(path: str) -> str:
    """Return the directory key Claude uses below ~/.claude/projects."""
    return path.replace("/", "-").replace(".", "-")


def _scan_claude_file(
    path: Path, max_lines: int = 200_000, max_bytes: int = 200_000_000
) -> dict:
    # `cwd` on each row is the tool call's cwd at that moment (it wanders with
    # every `cd` a Bash command makes), NOT the session's actual project root
    # - so the one exact match (claude_project_key(cwd) == project_key) can
    # legitimately sit thousands of lines in, especially in a long session
    # resumed after /compact where the opening lines are dominated by a burst
    # of Bash calls into some subdirectory. A short prefix scan found "most
    # common cwd among the first 500 lines" instead of the real project root,
    # which sent Resume's terminal to the wrong directory (found 2026-08-12,
    # milano session 0034d5f4: 500-line scan landed on the parent
    # /home/user/projects instead of /home/user/projects/milano, because the
    # exact match only appears after ~500 lines of /catalog and /ciucuri-
    # perdea churn). This is `_cached_file_scan`-cached by mtime, so scanning
    # the whole file costs nothing on repeat refreshes - only re-paid when the
    # transcript actually grows. The bounds here are just a safety valve
    # against a pathological single file, not a real limit in practice.
    # `updated_ms` is intentionally not tracked here: the file's own mtime
    # (used by callers as a fallback) already is the last-write time.
    result: dict = {}
    project_key = path.parent.name
    cwd_counts: dict[str, int] = {}
    bytes_read = 0
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle):
                bytes_read += len(line)
                if line_number >= max_lines or bytes_read >= max_bytes:
                    break
                if len(line) > 2_000_000:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if row.get("type") == "ai-title" and row.get("aiTitle"):
                    result["title"] = row["aiTitle"]
                # Written once, in the first couple of lines, only for a
                # session launched with -n/--name (or later /rename) - the
                # same name Claude Code's own cross-session messaging
                # addresses this session by, so it's what group membership
                # is matched on (see find_group_member_session).
                if row.get("type") == "agent-name" and row.get("agentName"):
                    result["agent_name"] = row["agentName"]
                # Session Hub polls Claude usage via `claude -p "/usage"`, which
                # runs under the SDK entrypoint. Flag those probe sessions so the
                # UI never lists our own background usage checks as real sessions.
                if row.get("entrypoint") == "sdk-cli":
                    result["sdk_cli"] = True
                if (
                    row.get("type") == "queue-operation"
                    and str(row.get("content") or "").strip() == "/usage"
                ):
                    result["usage_command"] = True
                cwd = row.get("cwd")
                if cwd:
                    cwd_counts[cwd] = cwd_counts.get(cwd, 0) + 1
                    if claude_project_key(cwd) == project_key:
                        result["project_cwd"] = cwd
                if result.get("title") and result.get("project_cwd"):
                    break
    except OSError:
        pass
    if not result.get("project_cwd") and cwd_counts:
        result["observed_cwd"] = max(cwd_counts, key=cwd_counts.get)
    return result


def inspect_claude_file(path: Path) -> dict:
    return _cached_file_scan(path, _scan_claude_file)


def claude_sessions() -> list[Session]:
    history = claude_history_index()
    sessions: list[Session] = []
    for path in CLAUDE_PROJECTS.glob("*/*.jsonl"):
        session_id = path.stem
        info = dict(history.get(session_id, {}))
        file_info = inspect_claude_file(path)
        info.update({key: value for key, value in file_info.items() if value})
        if file_info.get("sdk_cli") and file_info.get("usage_command"):
            # Skip Session Hub's own `/usage` polling sessions.
            continue
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
                agent_name=info.get("agent_name"),
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
    return _cached_file_scan(path, _scan_antigravity_database)


def _scan_antigravity_database(path: Path) -> dict:
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
    return _cached_file_scan(path, _scan_antigravity_transcript)


def _scan_antigravity_transcript(path: Path) -> dict:
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


def find_group_member_session(
    row: dict, cwd: str, sessions: list[Session]
) -> Session | None:
    """The live Claude session a saved group row refers to, if launched.

    Checked two ways, in order:
    1. `row["session_key"]` - the native key discover_sessions last saw this row
       matched to, kept alive across a /clear or manual relink because the active
       session's `linked_keys` (from metadata["links"]) still contains it. This is
       what lets a row survive a restart that has no agent-name record at all.
    2. `--name` (Session.agent_name, parsed from the transcript's own agent-name
       record - see _scan_claude_file) plus cwd - the bootstrap match, used before
       any session_key has been recorded (a row's first launch).
    """
    session_key = row.get("session_key")
    if session_key:
        match = next(
            (
                session
                for session in sessions
                if session.provider == "Claude"
                and session.cwd == cwd
                and (
                    session.native_key == session_key
                    or session_key in session.linked_keys
                )
            ),
            None,
        )
        if match:
            return match
    return next(
        (
            session
            for session in sessions
            if session.provider == "Claude"
            and session.cwd == cwd
            and session.agent_name == row.get("name")
        ),
        None,
    )


def discover_sessions(metadata: dict) -> list[Session]:
    settings = metadata.get("settings", {})
    sessions = []
    if settings.get("enable_codex", True):
        sessions += codex_sessions()
    if settings.get("enable_claude", True):
        sessions += claude_sessions()
    if settings.get("enable_antigravity", True):
        sessions += antigravity_sessions()
    changed = resolve_pending_handoffs(metadata, sessions)
    adopt_untracked_sessions(sessions)
    if resolve_clear_continuations(metadata, sessions):
        changed = True
    if changed:
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

    # A saved session group (see NewSessionGroupDialog/ManageGroupDialog)
    # collapses its launched member sessions into one representative row,
    # the same way a cross-agent link does above - members stay fully
    # intact and reappear on their own once removed from the group.
    group_hidden = set()
    group_pseudo_sessions = []
    groups_changed = False
    for cwd, group in metadata.setdefault("groups", {}).items():
        max_updated = 0
        for row in group.get("rows", []):
            match = find_group_member_session(row, cwd, visible)
            if match:
                group_hidden.add(match.native_key)
                max_updated = max(max_updated, match.updated_ms)
                if row.get("session_key") != match.native_key:
                    row["session_key"] = match.native_key
                    groups_changed = True
        display_name = group.get("display_name") or Path(cwd).name or cwd
        group_pseudo_sessions.append(
            Session("Claude", f"group:{cwd}", display_name, cwd, cwd, max_updated, Path(cwd))
        )
    if groups_changed:
        write_metadata(metadata)
    visible = [
        session for session in visible if session.native_key not in group_hidden
    ] + group_pseudo_sessions

    return sorted(visible, key=lambda item: item.updated_ms, reverse=True)


def native_session_index() -> dict[str, Session]:
    """All known sessions by native key, with per-session name/cwd overrides applied.

    Used by conversation pickers (linked_conversations, link_to_existing_conversation_for,
    continue_with_other_agent_for) that need the user-facing title Session Hub shows
    elsewhere, not the raw transcript-derived one.
    """
    sessions = codex_sessions() + claude_sessions() + antigravity_sessions()
    overrides = read_metadata().get("sessions", {})
    for session in sessions:
        custom = overrides.get(session.native_key, {})
        session.title = custom.get("name") or session.title
        session.cwd = custom.get("cwd") or session.cwd
    return {session.native_key: session for session in sessions}


def focus_window_by_title(title: str, timeout: float = 3.0) -> None:
    """Raise and focus the terminal window we just launched.

    GNOME's focus-stealing prevention keeps windows opened by a background
    process (like Session Hub's launch button) from taking focus on their
    own, so the newly spawned terminal sits behind Session Hub until the
    user clicks it. wmctrl activation from a short-lived poll loop sidesteps
    that. Runs on a daemon thread since the window can take a moment to
    appear and this must not block the Qt event loop.
    """
    wmctrl = shutil.which("wmctrl")
    if not wmctrl:
        return
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = subprocess.run(
                [wmctrl, "-l"], capture_output=True, text=True, timeout=1
            )
        except (OSError, subprocess.TimeoutExpired):
            return
        for line in result.stdout.splitlines():
            if title in line:
                subprocess.run([wmctrl, "-a", title], timeout=1)
                return
        time.sleep(0.15)


def new_pid_capture_file() -> Path:
    PID_DIR.mkdir(parents=True, exist_ok=True)
    return PID_DIR / f"{uuid.uuid4().hex}.pid"


def pid_capture_command(pidfile: Path, real_args: list[str]) -> list[str]:
    """Wrap a command so the real (post-exec) PID is written to `pidfile` first.

    The script takes the pidfile and command as extra `bash -c` positional
    args rather than string-interpolating them, so nothing here needs shell
    quoting. `exec` then replaces the wrapper shell with the real command,
    keeping the same PID - that's what lets a later /clear inside the same
    launched terminal be recognized as the same OS process.
    """
    return [
        "bash",
        "-c",
        'echo $$ > "$1"; shift; exec "$@"',
        "session-hub",
        str(pidfile),
        *real_args,
    ]


def read_pid_capture_file(pidfile: Path, timeout: float = 2.0) -> int | None:
    deadline = time.monotonic() + timeout
    pid: int | None = None
    while time.monotonic() < deadline:
        try:
            text = pidfile.read_text().strip()
        except OSError:
            text = ""
        if text:
            try:
                pid = int(text)
            except ValueError:
                pid = None
            break
        time.sleep(0.05)
    pidfile.unlink(missing_ok=True)
    return pid


def capture_hub_launch(pidfile: Path, cwd: str, session_id: str | None) -> None:
    pid = read_pid_capture_file(pidfile)
    if pid is not None:
        record_hub_launch(pid, cwd, session_id)


def record_hub_launch(pid: int, cwd: str, session_id: str | None) -> None:
    PID_DIR.mkdir(parents=True, exist_ok=True)
    tracking_file = PID_DIR / f"{pid}.json"
    try:
        tracking_file.write_text(json.dumps({"cwd": cwd, "session_id": session_id}))
    except OSError:
        pass


def process_alive(pid: int) -> bool:
    return Path(f"/proc/{pid}").exists()


def session_is_tracked_alive(session: Session) -> bool:
    """Best-effort: is a process Session Hub itself launched still running this?

    Reuses the same PID_DIR tracking files /clear-detection relies on
    (record_hub_launch/resolve_clear_continuations) - accurate only for
    sessions Session Hub launched. A session started some other way (a plain
    `claude` typed into a terminal, for instance) always reads as not-tracked
    here even while it's genuinely running - there's no general way to know.
    """
    if not PID_DIR.is_dir():
        return False
    keys = {session.native_key, *session.linked_keys}
    for tracking_file in PID_DIR.glob("*.json"):
        try:
            pid = int(tracking_file.stem)
            entry = json.loads(tracking_file.read_text())
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if not process_alive(pid):
            continue
        session_id = entry.get("session_id")
        if session_id and f"Claude:{session_id}" in keys:
            return True
    return False


def find_untracked_claude_pids() -> list[tuple[int, str]]:
    """(pid, cwd) for live processes that look like a `claude` CLI, not yet tracked.

    Best-effort /proc scan: matches any process whose cmdline mentions "claude"
    (covers both a direct binary and a node-shebang-wrapped script) and whose
    cwd we can resolve, excluding PIDs PID_DIR already has a tracking file for.
    Existing tracking (record_hub_launch, written at Session Hub's own launch
    time) already survives Session Hub being closed and reopened - it's a plain
    file under PID_DIR, not in-memory state - so this only needs to cover
    sessions that were never launched *through* Session Hub in the first place
    (a `claude` typed directly into a terminal, one running from before this
    tracking feature existed, ...). See adopt_untracked_sessions.
    """
    if not PROC_ROOT.is_dir():
        return []
    tracked = (
        {int(f.stem) for f in PID_DIR.glob("*.json") if f.stem.isdigit()}
        if PID_DIR.is_dir()
        else set()
    )
    found = []
    for entry in PROC_ROOT.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid in tracked:
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().decode("utf-8", "replace")
        except OSError:
            continue
        if "claude" not in cmdline.lower():
            continue
        try:
            cwd = os.readlink(entry / "cwd")
        except OSError:
            continue
        found.append((pid, cwd))
    return found


def adopt_untracked_sessions(sessions: list[Session]) -> None:
    """Backfill PID tracking for live `claude` processes Session Hub didn't launch.

    Lets /clear-detection (resolve_clear_continuations) start working on a
    session going forward, even though Session Hub missed its actual launch.
    Only adopts an unambiguous match: exactly the most-recently-updated Claude
    session for that process's cwd. A wrong or ambiguous guess just means that
    one process stays untracked (same as today), not a corrupted link - there's
    no metadata write here, only a new PID_DIR tracking file.
    """
    candidates = find_untracked_claude_pids()
    if not candidates:
        return
    latest_by_cwd: dict[str, Session] = {}
    for session in sessions:
        if session.provider != "Claude":
            continue
        current = latest_by_cwd.get(session.cwd)
        if not current or session.updated_ms > current.updated_ms:
            latest_by_cwd[session.cwd] = session
    for pid, cwd in candidates:
        session = latest_by_cwd.get(cwd)
        if session:
            record_hub_launch(pid, cwd, session.session_id)


def link_continuation(
    metadata: dict, old_key: str, new_key: str, old_title: str | None, id_prefix: str
) -> None:
    """Link `new_key` as the continuation of `old_key` and copy over whatever
    made the old session identifiable - display name and launch env/flag
    overrides - onto the new one.

    Shared by resolve_clear_continuations (automatic, hub-launched /clear)
    and link_to_existing_conversation_for (manual) so both paths behave
    identically instead of the new session reverting to "Claude <hash>" and
    global launch options.
    """
    links = metadata.setdefault("links", {})
    link_id = next(
        (
            lid
            for lid, link in links.items()
            if old_key in link.get("members", []) or new_key in link.get("members", [])
        ),
        None,
    )
    if link_id:
        link = links[link_id]
        for key in (old_key, new_key):
            if key not in link["members"]:
                link["members"].append(key)
        link["active"] = new_key
    else:
        link_id = f"{id_prefix}:{uuid.uuid4().hex}"
        links[link_id] = {"members": [old_key, new_key], "active": new_key}

    overrides = metadata.setdefault("sessions", {})
    old_overrides = overrides.get(old_key, {})
    new_overrides = overrides.setdefault(new_key, {})
    if "name" not in new_overrides and old_title:
        new_overrides["name"] = old_title

    link_overrides = overrides.setdefault(link_id, {})
    for field in ("env", "flags"):
        if field not in link_overrides and field in old_overrides:
            link_overrides[field] = old_overrides[field]


def resolve_clear_continuations(metadata: dict, sessions: list[Session]) -> bool:
    """Detect a /clear inside a Session-Hub-launched Claude terminal.

    A hub-launched terminal keeps the same OS PID across any number of
    /clear's (they only start a new session id/transcript file inside the
    same running process) - see pid_capture_command. Each such PID's tracking
    file (written by record_hub_launch right after launch) records which cwd
    it's running in and which Claude session id it was last known to be
    writing to. If that PID's session id no longer matches any live session,
    the process moved on to a new transcript without Session Hub launching
    it - i.e. a /clear - so link the old and new sessions the same way a
    cross-agent handoff would.
    """
    if not PID_DIR.is_dir():
        return False
    by_cwd: dict[str, list[Session]] = {}
    for session in sessions:
        if session.provider != "Claude":
            continue
        by_cwd.setdefault(session.cwd, []).append(session)

    tracked: list[tuple[Path, dict]] = []
    for tracking_file in PID_DIR.glob("*.json"):
        try:
            pid = int(tracking_file.stem)
        except ValueError:
            continue
        if not process_alive(pid):
            tracking_file.unlink(missing_ok=True)
            continue
        try:
            entry = json.loads(tracking_file.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        tracked.append((tracking_file, entry))

    # A session id already recorded as another live PID's OWN current
    # session, or already claimed as a saved group row's session_key,
    # can't also be *this* PID's /clear target - each tracked PID's
    # transcript is exclusively its own, and a group row's identity is
    # already resolved elsewhere (find_group_member_session). Without the
    # group-row half of this, a row whose OWN process had simply exited
    # (idle, no tracking file - the ordinary case for an unstarted or
    # finished group member) still counted as "unclaimed", so a sibling
    # row's genuinely-still-running PID got merged into it anyway the
    # moment it happened to be the most recently updated session in a
    # shared cwd - which is the normal state for a session group.
    claimed = {entry["session_id"] for _, entry in tracked if entry.get("session_id")}
    for group in metadata.get("groups", {}).values():
        for row in group.get("rows", []):
            session_key = row.get("session_key") or ""
            if session_key.startswith("Claude:"):
                claimed.add(session_key[len("Claude:"):])

    changed = False
    for tracking_file, entry in tracked:
        candidates = [
            session
            for session in by_cwd.get(entry.get("cwd"), [])
            if session.session_id == entry.get("session_id")
            or session.session_id not in claimed
        ]
        latest = max(candidates, key=lambda session: session.updated_ms, default=None)
        if not latest:
            continue
        old_session_id = entry.get("session_id")
        if old_session_id == latest.session_id:
            continue
        if old_session_id is None:
            entry["session_id"] = latest.session_id
            tracking_file.write_text(json.dumps(entry))
            claimed.add(latest.session_id)
            continue

        old_key = f"Claude:{old_session_id}"
        new_key = latest.native_key
        old_session = next(
            (
                s
                for s in by_cwd.get(entry.get("cwd"), [])
                if s.session_id == old_session_id
            ),
            None,
        )
        session_overrides = metadata.get("sessions", {})
        old_title = session_overrides.get(old_key, {}).get("name") or (
            old_session.title if old_session else None
        )
        link_continuation(metadata, old_key, new_key, old_title, "clear")
        entry["session_id"] = latest.session_id
        tracking_file.write_text(json.dumps(entry))
        claimed.add(latest.session_id)
        changed = True
    return changed


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

        # Enabled agents
        agents_group = QGroupBox("Enabled agents")
        agents_layout = QHBoxLayout(agents_group)
        self.enable_codex = QCheckBox("Codex")
        self.enable_claude = QCheckBox("Claude")
        self.enable_antigravity = QCheckBox("Antigravity")
        
        self.enable_codex.setChecked(bool(settings.get("enable_codex", True)))
        self.enable_claude.setChecked(bool(settings.get("enable_claude", True)))
        self.enable_antigravity.setChecked(bool(settings.get("enable_antigravity", True)))
        
        self.enable_codex.toggled.connect(self.validate_enabled_agents)
        self.enable_claude.toggled.connect(self.validate_enabled_agents)
        self.enable_antigravity.toggled.connect(self.validate_enabled_agents)
        self.enable_codex.toggled.connect(self.update_danger_visibility)
        self.enable_claude.toggled.connect(self.update_danger_visibility)
        self.enable_antigravity.toggled.connect(self.update_danger_visibility)
        
        agents_layout.addWidget(self.enable_codex)
        agents_layout.addWidget(self.enable_claude)
        agents_layout.addWidget(self.enable_antigravity)
        layout.addWidget(agents_group)

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
        self.update_danger_visibility()

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

        launch_group = QGroupBox("Launch options (all sessions)")
        launch_layout = QVBoxLayout(launch_group)
        launch_note = QLabel(
            "Environment variables and CLI flags applied to every session "
            "Session Hub launches. Per-session overrides (right-click a "
            "session → Launch options…) take precedence over these."
        )
        launch_note.setWordWrap(True)
        launch_layout.addWidget(launch_note)
        self.launch_options = LaunchOptionsEditor(
            settings.get("global_env") or {}, settings.get("global_flags") or {}
        )
        self.env_editor = self.launch_options.env_editor
        self.flags_editor = self.launch_options.flags_editor
        launch_layout.addWidget(self.launch_options)
        layout.addWidget(launch_group)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.validate_enabled_agents()

    def update_danger_visibility(self) -> None:
        self.codex_danger.setVisible(self.enable_codex.isChecked())
        self.claude_danger.setVisible(self.enable_claude.isChecked())
        self.antigravity_danger.setVisible(self.enable_antigravity.isChecked())

    def validate_enabled_agents(self) -> None:
        checked = [
            cb for cb in (self.enable_codex, self.enable_claude, self.enable_antigravity)
            if cb.isChecked()
        ]
        if len(checked) == 1:
            checked[0].setEnabled(False)
        else:
            for cb in (self.enable_codex, self.enable_claude, self.enable_antigravity):
                cb.setEnabled(True)

    def values(self) -> dict:
        values = dict(self.original_settings)
        values.update(
            {
                "enable_codex": self.enable_codex.isChecked(),
                "enable_claude": self.enable_claude.isChecked(),
                "enable_antigravity": self.enable_antigravity.isChecked(),
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
                "global_env": self.env_editor.env(),
                "global_flags": self.flags_editor.env(),
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
        self.model: str | None = None
        self.caveman: str = ""
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

        self.model_combo: QComboBox | None = None
        self.caveman_combo: QComboBox | None = None
        if provider == "Claude":
            self.model_combo = QComboBox()
            for label, alias in CLAUDE_MODELS:
                self.model_combo.addItem(label, alias)
            form.addRow("Model:", self.model_combo)

            # Pre-selected from the global flag so this row shows the standing
            # default rather than silently disagreeing with it; changing it here
            # affects only the session being started.
            self.caveman_combo = QComboBox()
            for label, value in CLI_FLAG_SPECS["--caveman"]["choices"]:
                self.caveman_combo.addItem(label, value)
            current = str((settings.get("global_flags") or {}).get("--caveman", ""))
            index = self.caveman_combo.findData(current)
            if index >= 0:
                self.caveman_combo.setCurrentIndex(index)
            form.addRow("Caveman:", self.caveman_combo)

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
            path = DEFAULT_SESSION_DIR
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
            directory = DEFAULT_SESSION_DIR
            directory.mkdir(parents=True, exist_ok=True)
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
        if self.model_combo is not None:
            self.model = self.model_combo.currentData()
        if self.caveman_combo is not None:
            self.caveman = str(self.caveman_combo.currentData() or "")
        super().accept()

    def flag_overrides(self) -> dict[str, str]:
        """One-off flag choices from this dialog, highest precedence at launch."""
        return {"--caveman": self.caveman} if self.caveman else {}


class NewSessionGroupDialog(QDialog):
    """Define a batch of named Claude sessions to launch into one directory.

    For cross-session messaging (e.g. an orchestrator + workers): each row
    picks a model and a session name (`--name`), auto-suggested but always
    editable. The whole batch is saved as a group keyed by directory so more
    sessions can be added to it later via "Add session to group…".
    """

    def __init__(self, settings: dict, parent=None) -> None:
        super().__init__(parent)
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
        self.setWindowTitle("New Claude Session Group")
        self.setMinimumWidth(640)
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
        self.project_name.textChanged.connect(self.update_preview)
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

        layout.addWidget(QLabel("Sessions to launch:"))
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Model", "Name"])
        self.table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.ResizeToContents
        )
        self.table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(160)
        layout.addWidget(self.table)

        row_controls = QHBoxLayout()
        add_row_button = QPushButton("Add row")
        add_row_button.clicked.connect(lambda: self.add_row())
        remove_row_button = QPushButton("Remove selected")
        remove_row_button.clicked.connect(self.remove_selected_rows)
        row_controls.addWidget(add_row_button)
        row_controls.addWidget(remove_row_button)
        row_controls.addStretch(1)
        layout.addLayout(row_controls)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Launch all")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.add_row()
        self.update_fields()

    def location_type(self) -> str:
        return str(self.location.currentData())

    def update_fields(self) -> None:
        project = self.location_type() in {"primary", "secondary"}
        self.project_name.setEnabled(project)
        self.existing_widget.setEnabled(self.location_type() == "existing")
        self.update_preview()

    def current_directory(self) -> Path | None:
        location = self.location_type()
        if location == "home":
            return DEFAULT_SESSION_DIR
        if location in {"primary", "secondary"}:
            root = self.project_roots[location]
            name = self.project_name.text().strip()
            return root / name if root and name else None
        text = self.existing_path.text()
        return Path(text) if text else None

    def update_preview(self) -> None:
        directory = self.current_directory()
        self.preview.setText(
            f"Working directory: {directory}" if directory else "Choose a folder."
        )
        for row in range(self.table.rowCount()):
            self.suggest_name(row)

    def browse_existing(self) -> None:
        directory = QFileDialog.getExistingDirectory(
            self, "Choose working directory", str(HOME)
        )
        if directory:
            self.existing_path.setText(directory)
            self.update_preview()

    # -- rows ----------------------------------------------------------
    def add_row(self) -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        model_combo = QComboBox()
        for label, alias in CLAUDE_MODELS:
            model_combo.addItem(label, alias)
        model_combo.setCurrentIndex(1)
        name_edit = QLineEdit()
        name_edit.auto_suggested = True
        name_edit.textEdited.connect(
            lambda _text, edit=name_edit: setattr(edit, "auto_suggested", False)
        )
        model_combo.currentIndexChanged.connect(lambda _i, r=row: self.suggest_name(r))
        self.table.setCellWidget(row, 0, model_combo)
        self.table.setCellWidget(row, 1, name_edit)
        self.suggest_name(row)

    def remove_selected_rows(self) -> None:
        rows = sorted(
            {index.row() for index in self.table.selectedIndexes()}, reverse=True
        )
        for row in rows:
            self.table.removeRow(row)

    def existing_row_names(self, exclude_row: int | None = None) -> set[str]:
        names = set()
        for row in range(self.table.rowCount()):
            if row == exclude_row:
                continue
            edit = self.table.cellWidget(row, 1)
            if edit is not None and edit.text().strip():
                names.add(edit.text().strip())
        return names

    def suggest_name(self, row: int) -> None:
        if row < 0 or row >= self.table.rowCount():
            return
        name_edit = self.table.cellWidget(row, 1)
        if name_edit is None or not getattr(name_edit, "auto_suggested", True):
            return
        model_combo = self.table.cellWidget(row, 0)
        alias = model_combo.currentData() if model_combo else None
        suggested = suggest_session_name(
            self.current_directory(), alias, self.existing_row_names(exclude_row=row)
        )
        name_edit.blockSignals(True)
        name_edit.setText(suggested)
        name_edit.blockSignals(False)
        name_edit.auto_suggested = True

    def rows(self) -> list[dict]:
        result = []
        for row in range(self.table.rowCount()):
            model_combo = self.table.cellWidget(row, 0)
            name_edit = self.table.cellWidget(row, 1)
            name = name_edit.text().strip() if name_edit else ""
            if not name:
                continue
            result.append(
                {"name": name, "model": model_combo.currentData() if model_combo else None}
            )
        return result

    def accept(self) -> None:
        location = self.location_type()
        if location == "home":
            directory = DEFAULT_SESSION_DIR
            directory.mkdir(parents=True, exist_ok=True)
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

        rows = self.rows()
        if not rows:
            QMessageBox.warning(
                self, "No sessions", "Add at least one row with a name."
            )
            return
        seen: set[str] = set()
        for row in rows:
            if row["name"] in seen:
                QMessageBox.warning(
                    self,
                    "Duplicate name",
                    f"The name “{row['name']}” is used more than once.",
                )
                return
            seen.add(row["name"])

        self.directory = directory
        self.group_rows = rows
        super().accept()


class AddGroupSessionDialog(QDialog):
    """Add one more named Claude session to an existing saved group."""

    def __init__(self, group: dict, parent=None) -> None:
        super().__init__(parent)
        self.group = group
        self.row: dict | None = None
        self.setWindowTitle("Add session to group")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        intro = QLabel(f"Directory: {group.get('cwd', '')}")
        intro.setWordWrap(True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.model_combo = QComboBox()
        for label, alias in CLAUDE_MODELS:
            self.model_combo.addItem(label, alias)
        self.model_combo.setCurrentIndex(1)
        self.model_combo.currentIndexChanged.connect(lambda _i: self.suggest_name())
        form.addRow("Model:", self.model_combo)

        self.name_edit = QLineEdit()
        self.name_edit.auto_suggested = True
        self.name_edit.textEdited.connect(
            lambda _text: setattr(self.name_edit, "auto_suggested", False)
        )
        form.addRow("Name:", self.name_edit)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Launch")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.suggest_name()

    def suggest_name(self) -> None:
        if not getattr(self.name_edit, "auto_suggested", True):
            return
        cwd = self.group.get("cwd")
        directory = Path(cwd) if cwd else None
        alias = self.model_combo.currentData()
        existing = {row["name"] for row in self.group.get("rows", [])}
        suggested = suggest_session_name(directory, alias, existing)
        self.name_edit.blockSignals(True)
        self.name_edit.setText(suggested)
        self.name_edit.blockSignals(False)
        self.name_edit.auto_suggested = True

    def accept(self) -> None:
        name = self.name_edit.text().strip()
        if not name:
            QMessageBox.warning(self, "Missing name", "Enter a session name.")
            return
        if name in {row["name"] for row in self.group.get("rows", [])}:
            QMessageBox.warning(
                self, "Duplicate name", f"“{name}” is already used in this group."
            )
            return
        self.row = {"name": name, "model": self.model_combo.currentData()}
        super().accept()


def configure_resizable_columns(
    table: QTableWidget,
    columns: list[str] | tuple[str, ...],
    default_widths: dict[str, int],
    stretch_column: str | None,
) -> None:
    """Independently-resizable columns, with one column absorbing table resizes.

    Interactive columns keep whatever width the user drags them to; the
    stretch column is the only one that grows or shrinks when the table
    itself is resized, so it's the one that gets truncated first on a
    narrower window instead of every column being squeezed down together.
    Columns are also drag-reorderable (setSectionsMovable) - the resulting
    order round-trips through the same saveState()/restoreState() blob as
    the widths, so a dragged order is remembered too.
    """
    header = table.horizontalHeader()
    header.setSectionsMovable(True)
    for index, column in enumerate(columns):
        header.setSectionResizeMode(
            index,
            QHeaderView.ResizeMode.Stretch
            if column == stretch_column
            else QHeaderView.ResizeMode.Interactive,
        )
        width = default_widths.get(column)
        if width is not None:
            header.resizeSection(index, width)


def restore_column_widths(table: QTableWidget, encoded: str | None) -> None:
    if not encoded:
        return
    try:
        table.horizontalHeader().restoreState(QByteArray.fromBase64(encoded.encode("ascii")))
    except (AttributeError, ValueError):
        pass


def column_widths_state(table: QTableWidget) -> str:
    return bytes(table.horizontalHeader().saveState().toBase64()).decode("ascii")


def set_default_column_order(table: QTableWidget, logical_order: list[int]) -> None:
    """Put columns in `logical_order` left-to-right, before any saved state is restored.

    Moving one section shifts everyone else's visual index, so each target
    column's current visual position has to be looked up fresh right before
    it's moved - not computed once up front.
    """
    header = table.horizontalHeader()
    for target_visual, logical_index in enumerate(logical_order):
        header.moveSection(header.visualIndex(logical_index), target_visual)


class _GroupSessionTable(QTableWidget):
    """QTableWidget with drag-to-reorder that also relocates cell widgets.

    Plain QTableWidget drag-and-drop only reorders QTableWidgetItems - it
    silently leaves cell widgets (the Transcripts checkbox, Launch button)
    behind in their original row, corrupting the display. Reordering the
    dialog's own row list and doing a full reload() instead sidesteps that
    entirely: `event.ignore()` stops Qt's own item move from happening at all.
    """

    def __init__(self, dialog: "ManageGroupDialog", *args) -> None:
        super().__init__(*args)
        self._dialog = dialog

    def dropEvent(self, event) -> None:
        source_row = self.currentRow()
        target_row = self.indexAt(event.position().toPoint()).row()
        event.ignore()
        if source_row < 0 or target_row < 0 or source_row == target_row:
            return
        self._dialog.reorder_row(source_row, target_row)


class ManageGroupDialog(QDialog):
    """Live view of a saved session group.

    Each row is its own action (launch it, rename it, remove it, delete
    it), so actions here take effect immediately rather than through a
    single accept/cancel - there's no one batch to commit. Shares its
    Agent/Model/Name/Last updated/Session ID rendering with the main listview
    via SessionHub.populate_session_table - this class only adds what's
    actually different (Transcripts, live launch status, drag reorder, and
    group-row management on top of the main listview's own context menu).
    """

    # Session ID is rendered separately, as the table's own last column
    # (after the group-specific Transcripts/Launch columns), so it's the
    # one that shrinks first on a narrower dialog instead of squeezing
    # Name down - populate_session_table only fills a contiguous block of
    # columns, and Session ID isn't adjacent to the rest here.
    SHARED_COLUMNS = tuple(
        column
        for column in SESSION_TABLE_COLUMNS
        if column not in ("Working directory", "Session ID")
    )
    TRANSCRIPTS_COLUMN = len(SHARED_COLUMNS)
    STATUS_COLUMN = len(SHARED_COLUMNS) + 1
    SESSION_ID_COLUMN = len(SHARED_COLUMNS) + 2

    def __init__(self, hub, cwd: str, parent=None) -> None:
        super().__init__(parent)
        self.hub = hub
        self.cwd = cwd
        self.setWindowTitle("Manage session group")
        self.setMinimumWidth(720)
        layout = QVBoxLayout(self)

        self.intro = QLabel()
        self.intro.setWordWrap(True)
        layout.addWidget(self.intro)

        self.table = _GroupSessionTable(self, 0, len(self.SHARED_COLUMNS) + 3)
        self.table.setHorizontalHeaderLabels(
            list(self.SHARED_COLUMNS) + ["Transcripts", "", "Session ID"]
        )
        configure_resizable_columns(
            self.table,
            self.SHARED_COLUMNS,
            {"Agent": 90, "Model": 90, "Name": 260, "Last updated": 140},
            stretch_column=None,
        )
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(
            self.TRANSCRIPTS_COLUMN, QHeaderView.ResizeMode.ResizeToContents
        )
        header.setSectionResizeMode(self.STATUS_COLUMN, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(self.SESSION_ID_COLUMN, QHeaderView.ResizeMode.Stretch)
        saved_columns = self.hub.settings().get("group_table_columns_v2")
        if saved_columns:
            restore_column_widths(self.table, saved_columns)
        else:
            # Default order: launch status up front (the thing you actually
            # act on), Agent moved to just before Session ID since every row
            # here is already known to be Claude. Only applied when there's
            # no saved order yet - once the user drags one, that wins.
            set_default_column_order(
                self.table,
                [
                    self.STATUS_COLUMN,
                    self.SHARED_COLUMNS.index("Model"),
                    self.SHARED_COLUMNS.index("Name"),
                    self.SHARED_COLUMNS.index("Last updated"),
                    self.TRANSCRIPTS_COLUMN,
                    self.SHARED_COLUMNS.index("Agent"),
                    self.SESSION_ID_COLUMN,
                ],
            )
        # QHeaderView::restoreState() also restores whether sections are
        # movable, so a state blob saved before drag-reordering existed
        # silently turns it back off - this has to run after restoring, not
        # just once up front in configure_resizable_columns.
        header.setSectionsMovable(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setMinimumHeight(200)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setDragEnabled(True)
        self.table.setAcceptDrops(True)
        self.table.setDropIndicatorShown(True)
        self.table.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self.row_context_menu)
        self.table.doubleClicked.connect(self.launch_or_resume_row)
        layout.addWidget(self.table)

        controls = QHBoxLayout()
        launch_all = QPushButton("Launch all")
        launch_all.clicked.connect(self.launch_all)
        controls.addWidget(launch_all)
        controls.addStretch(1)
        layout.addLayout(controls)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.accept)
        layout.addWidget(buttons)

        encoded_geometry = self.hub.settings().get("group_dialog_geometry")
        if encoded_geometry:
            try:
                self.restoreGeometry(QByteArray.fromBase64(encoded_geometry.encode("ascii")))
            except (AttributeError, ValueError):
                pass

        self.reload()

    def done(self, result: int) -> None:
        # The "Close" button (wired to accept()) and the window's own X button
        # (which goes through reject()) both funnel through here, unlike
        # closeEvent - which only fires for the X button - so this is the one
        # place that reliably sees every way the dialog can close.
        settings = self.hub.settings()
        settings["group_dialog_geometry"] = bytes(self.saveGeometry().toBase64()).decode("ascii")
        settings["group_table_columns_v2"] = column_widths_state(self.table)
        write_metadata(self.hub.metadata)
        super().done(result)

    def group(self) -> dict | None:
        group = self.hub.metadata.get("groups", {}).get(self.cwd)
        if group:
            self._migrate_rows(group)
        return group

    def _migrate_rows(self, group: dict) -> None:
        """Upgrade rows saved before override_key/live-status existed.

        Drops the old sticky `launched` flag (status is now always derived
        live) and mints an `override_key` for any row that predates it,
        carrying its old `model` choice into the same env-override mechanism
        a fresh row uses (see SessionHub.register_group_row).
        """
        changed = False
        for row in group.get("rows", []):
            if row.pop("launched", None) is not None:
                changed = True
            if "override_key" not in row:
                registered = self.hub.register_group_row(
                    self.cwd, row["name"], row.pop("model", None)
                )
                row["override_key"] = registered["override_key"]
                changed = True
        if changed:
            write_metadata(self.hub.metadata)

    def matched_sessions(self) -> list[tuple[dict, Session | None]]:
        """Each saved row paired with its live session, if currently matched.

        Applies the same per-session title/cwd overrides discover_sessions
        applies - group members are hidden from self.hub.sessions (that's
        the point of the group-collapsing pass), so this re-derives from raw
        claude_sessions() rather than reusing that already-filtered list.

        Checked by override_key first, native key second: "Rename" in this
        dialog's own context menu writes through row_session(), whose .key
        is the row's override_key (see row_context_menu) - not the matched
        session's native key, which changes on every restart and would
        otherwise make a rename get silently shadowed by whatever native-key
        override (an old link copy, a stale rename from a previous native
        session) happens to exist once the row re-matches a new process.
        """
        group = self.group()
        if not group:
            return []
        live = claude_sessions()
        overrides = self.hub.metadata.get("sessions", {})
        links = self.hub.metadata.get("links", {})
        pairs = []
        for row in group.get("rows", []):
            match = find_group_member_session(row, self.cwd, live)
            if match:
                row_custom = overrides.get(row["override_key"], {})
                native_custom = overrides.get(match.key, {})
                match.title = (
                    row_custom.get("name") or native_custom.get("name") or match.title
                )
                match.cwd = row_custom.get("cwd") or native_custom.get("cwd") or match.cwd
                # claude_sessions() is raw - unlike discover_sessions(), it
                # never applies metadata["links"], so linked_keys is always
                # empty here unless filled in by hand. Without this,
                # "Open linked conversation..." (linked_conversations(),
                # which reads session.linked_keys) always finds nothing for
                # a group row, even when the row's session really is linked
                # to an older conversation.
                link = next(
                    (
                        entry
                        for entry in links.values()
                        if match.native_key in entry.get("members", [])
                    ),
                    None,
                )
                if link:
                    match.linked_keys = tuple(link.get("members", []))
            pairs.append((row, match))
        return pairs

    def row_session(self, row: dict, match: Session | None) -> Session:
        base = match or Session(
            "Claude", f"pending:{row['override_key']}", row["name"], self.cwd, self.cwd, 0,
            Path(self.cwd),
        )
        return replace(base, logical_key=row["override_key"])

    def pair_at_table_row(self, table_row: int) -> tuple[dict, Session | None] | None:
        """Resolve a clicked/double-clicked table row to its (row, match) pair.

        Table row index is a VISUAL position, which drifts from
        group["rows"]'s own order once the table gets sorted (see
        populate_session_table's setSortingEnabled(True) - clicking a
        column header, or even just re-populating a previously-sorted
        table, reorders rows visually) - indexing matched_sessions()
        directly by table_row silently grabs a DIFFERENT row once that
        happens, driving actions against the wrong session entirely.
        Column 0 is always one of the shared columns populate_session_table
        fills, so its UserRole+1 data (the row's stable override_key)
        identifies the row correctly no matter how it's currently sorted.
        """
        item = self.table.item(table_row, 0)
        if item is None:
            return None
        override_key = item.data(Qt.ItemDataRole.UserRole + 1)
        for row, match in self.matched_sessions():
            if row["override_key"] == override_key:
                return row, match
        return None

    def reload(self) -> None:
        group = self.group()
        if not group:
            self.intro.setText("This group no longer exists.")
            self.table.setRowCount(0)
            return
        name = group.get("display_name") or Path(self.cwd).name or self.cwd
        self.intro.setText(f"{name} — {self.cwd}")
        pairs = self.matched_sessions()
        row_sessions = [self.row_session(row, match) for row, match in pairs]
        self.hub.populate_session_table(self.table, row_sessions, self.SHARED_COLUMNS)
        for index, ((row, match), row_session) in enumerate(zip(pairs, row_sessions)):
            self.table.setItem(
                index, self.SESSION_ID_COLUMN, QTableWidgetItem(row_session.session_id)
            )
            checkbox = QCheckBox()
            checkbox.setChecked(row.get("transcripts", True))
            checkbox.toggled.connect(
                lambda checked, n=row["name"]: self.set_transcripts(n, checked)
            )
            self.table.setCellWidget(index, self.TRANSCRIPTS_COLUMN, checkbox)

            if match and session_is_tracked_alive(match):
                status = "Running"
            elif match:
                status = "Idle"
            else:
                status = "Not started"
            self.table.setItem(index, self.STATUS_COLUMN, QTableWidgetItem(status))

    def set_transcripts(self, name: str, enabled: bool) -> None:
        group = self.group()
        if not group:
            return
        row = next((r for r in group["rows"] if r["name"] == name), None)
        if not row:
            return
        row["transcripts"] = enabled
        write_metadata(self.hub.metadata)

    def launch_row(self, name: str) -> None:
        self.hub.launch_group_row(self.cwd, name)
        self.hub.refresh()
        self.reload()

    def launch_or_resume_row(self, index) -> None:
        """Double-click a row: launch it if it's never run, resume it if it has.

        Mirrors the main listview's own double-click (resume_selected) -
        there's no separate "launch" vs "resume" button here anymore, just
        the one gesture the rest of Session Hub already uses everywhere.
        """
        pair = self.pair_at_table_row(index.row())
        if pair is None:
            return
        row, match = pair
        if match:
            self.hub.resume_session(self.row_session(row, match))
        else:
            self.hub.launch_group_row(self.cwd, row["name"])
        self.hub.refresh()
        self.reload()

    def launch_all(self) -> None:
        group = self.group()
        if not group:
            return
        for row, match in self.matched_sessions():
            if match and session_is_tracked_alive(match):
                continue
            self.launch_row(row["name"])

    def reorder_row(self, source_row: int, target_row: int) -> None:
        group = self.group()
        if not group:
            return
        # Resolved through pair_at_table_row like the other row-lookup
        # sites - source_row/target_row are visual drop-target positions,
        # which don't match group["rows"]'s own order once the table is
        # sorted.
        source_pair = self.pair_at_table_row(source_row)
        target_pair = self.pair_at_table_row(target_row)
        if source_pair is None or target_pair is None:
            return
        rows = group["rows"]
        rows.pop(rows.index(source_pair[0]))
        rows.insert(rows.index(target_pair[0]), source_pair[0])
        write_metadata(self.hub.metadata)
        self.reload()

    def row_context_menu(self, point) -> None:
        item = self.table.itemAt(point)
        if item is None:
            return
        # pair_at_table_row(), not pairs[item.row()]: table row index is a
        # visual position that drifts from matched_sessions()'s own order
        # once the table is sorted, and matched_sessions() (not a fresh
        # find_group_member_session() call) is the one place that resolves
        # title/cwd overrides *and* linked_keys (from metadata["links"])
        # onto the matched session - recomputing the match independently
        # skipped that, which is why "Open linked conversation..." always
        # came back empty for a group row even when it really was linked.
        pair = self.pair_at_table_row(item.row())
        if pair is None:
            return
        row, match = pair
        menu = QMenu(self)
        if match:
            # row_session(), not the raw match: its .key is the row's own
            # override_key, so "Launch options..." reads/writes the exact
            # bucket the Model column already reads from (effective_model)
            # instead of a native session key that goes stale on every
            # restart and never matches what the column shows.
            session = self.row_session(row, match)
            for label, slot in self.hub.context_menu_actions(session):
                if label == "Add session to group…":
                    continue
                action = QAction(label, self)
                action.triggered.connect(slot)
                menu.addAction(action)
            menu.addSeparator()
        remove_action = QAction("Remove from group", self)
        remove_action.triggered.connect(lambda: self.remove_row(row["name"]))
        menu.addAction(remove_action)
        menu.exec(self.table.viewport().mapToGlobal(point))
        self.reload()

    def remove_row(self, name: str) -> None:
        group = self.group()
        if not group:
            return
        group["rows"] = [row for row in group["rows"] if row["name"] != name]
        write_metadata(self.hub.metadata)
        self.hub.refresh()
        self.reload()


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
    SESSION_TABLE_COLUMNS = SESSION_TABLE_COLUMNS

    def __init__(self) -> None:
        super().__init__()
        self.metadata = read_metadata()
        self.sessions: list[Session] = []
        self.usage_widgets: dict[str, list[tuple[QLabel, QProgressBar, QLabel]]] = {}
        self.usage_headers: dict[str, QLabel] = {}
        self.usage_workers: dict[str, UsageWorker] = {}
        self.thread_pool = QThreadPool.globalInstance()
        self.setWindowTitle("Session Hub")
        self.setWindowIcon(
            QIcon(str(APP_ICON)) if APP_ICON.is_file() else QIcon.fromTheme("utilities-terminal")
        )
        self.resize(1280, 900)
        self.setMinimumSize(900, 650)
        self.build_ui()
        self.update_usage_visibility()
        self.update_new_provider_list()
        self.restore_window_geometry()
        self.purge_expired_trash()
        self.refresh()
        # Usage bars refresh only on demand (startup, the Refresh button, or F5);
        # there is no automatic periodic polling.
        QTimer.singleShot(0, self.refresh_usage)

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

        new_group_button = QPushButton("New session group…")
        new_group_button.setToolTip(
            "Launch several named Claude sessions into one directory at "
            "once, for cross-session messaging (e.g. an orchestrator + "
            "workers)."
        )
        new_group_button.clicked.connect(self.launch_new_group)
        toolbar.addWidget(new_group_button)

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
            header = QLabel(f"<b>{provider} usage</b>")
            usage_layout.addWidget(header, 0, offset, 1, 2)
            self.usage_headers[provider] = header
            rows = []
            default_names = (
                (
                    "Gemini weekly",
                    "Gemini 5-hour",
                    "Claude/GPT weekly",
                    "Claude/GPT 5-hour",
                )
                if provider == "Antigravity"
                else ("5-hour", "Weekly", "Weekly (Fable)")
                if provider == "Claude"
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
                else:
                    # Reserve room for the reset line plus the pace line so the
                    # panel keeps a steady height while usage is refreshing.
                    detail.setMinimumHeight(detail.fontMetrics().lineSpacing() * 2)
                    detail.setAlignment(
                        Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft
                    )
                row = 1 + index * 2
                usage_layout.addWidget(label, row, offset)
                usage_layout.addWidget(bar, row, offset + 1)
                usage_layout.addWidget(detail, row + 1, offset, 1, 2)
                rows.append((label, bar, detail))
            self.usage_widgets[provider] = rows
        layout.addWidget(usage_frame)

        self.table = QTableWidget(0, len(SessionHub.SESSION_TABLE_COLUMNS))
        self.table.setHorizontalHeaderLabels(list(SessionHub.SESSION_TABLE_COLUMNS))
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.setSortingEnabled(True)
        self.table.verticalHeader().setVisible(False)
        configure_resizable_columns(
            self.table,
            SessionHub.SESSION_TABLE_COLUMNS,
            {"Agent": 90, "Model": 90, "Name": 220, "Working directory": 320, "Last updated": 140},
            stretch_column="Session ID",
        )
        restore_column_widths(self.table, self.settings().get("main_table_columns"))
        self.table.doubleClicked.connect(self.resume_selected)
        for key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            shortcut = QShortcut(QKeySequence(key), self.table)
            shortcut.setContext(Qt.ShortcutContext.WidgetShortcut)
            shortcut.activated.connect(self.resume_selected)
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
            if label == "Continue with other agent":
                self.continue_with_other_button = button
            elif label == "Prepare handoff summary":
                self.prepare_handoff_button = button
            actions.addWidget(button)
        layout.addLayout(actions)
        self.setCentralWidget(root)

        refresh_shortcut = QShortcut(QKeySequence(Qt.Key.Key_F5), self)
        refresh_shortcut.activated.connect(self.refresh_all)

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
            latest["settings"]["main_table_columns"] = column_widths_state(self.table)
            self.metadata = latest
            write_metadata(latest)
        super().closeEvent(event)

    def update_usage_visibility(self) -> None:
        settings = self.settings()
        for provider in PROVIDERS:
            enabled = bool(settings.get(f"enable_{provider.lower()}", True))
            if provider in self.usage_headers:
                self.usage_headers[provider].setVisible(enabled)
            if provider in self.usage_widgets:
                for label, bar, detail in self.usage_widgets[provider]:
                    label.setVisible(enabled)
                    bar.setVisible(enabled)
                    detail.setVisible(enabled)

    def update_new_provider_list(self) -> None:
        settings = self.settings()
        current = self.new_provider.currentText()
        self.new_provider.clear()
        enabled_providers = [
            provider for provider in PROVIDERS
            if bool(settings.get(f"enable_{provider.lower()}", True))
        ]
        self.new_provider.addItems(enabled_providers)
        idx = self.new_provider.findText(current)
        if idx >= 0:
            self.new_provider.setCurrentIndex(idx)
        elif self.new_provider.count() > 0:
            self.new_provider.setCurrentIndex(0)
        # Handoffs need a second enabled agent to hand off to.
        multiple_agents = len(enabled_providers) > 1
        self.continue_with_other_button.setVisible(multiple_agents)
        self.prepare_handoff_button.setVisible(multiple_agents)

    def open_settings(self) -> None:
        dialog = SettingsDialog(self.settings(), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.metadata["settings"] = dialog.values()
            write_metadata(self.metadata)
            self.purge_expired_trash()
            self.update_usage_visibility()
            self.update_new_provider_list()
            self.refresh()

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
        settings = self.settings()
        for provider, rows in self.usage_widgets.items():
            if not bool(settings.get(f"enable_{provider.lower()}", True)):
                continue
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
        self,
        provider: str,
        windows: list[UsageWindow] | list[UsageActivity],
        error: str,
    ) -> None:
        self.usage_workers.pop(provider, None)
        rows = self.usage_widgets[provider]
        # The Claude "Weekly (Fable)" row (index 2) is optional: it only shows
        # while `/usage` still reports a Fable window. Once Fable becomes
        # credit-only and drops out of the output, the row hides itself instead
        # of showing "Unavailable" forever.
        optional_index = 2 if provider == "Claude" else None
        if error:
            for index, (_, bar, detail) in enumerate(rows):
                if index == optional_index:
                    self.set_usage_row_visible(rows[index], False)
                    continue
                bar.setFormat("Unavailable")
                detail.setText(error if index == 0 else "")
        elif windows and isinstance(windows[0], UsageActivity):
            # `/usage` omitted the percentage bars and returned only the
            # "contributing to usage" breakdown. Show that as a stand-in
            # until real UsageWindow data comes back, at which point the
            # branch below takes over automatically.
            for index, (label, bar, detail) in enumerate(rows):
                activity = windows[index] if index < len(windows) else None
                if not activity or index == optional_index:
                    self.set_usage_row_visible(rows[index], False)
                    continue
                self.set_usage_row_visible(rows[index], True)
                label.setText(activity.label)
                bar.setValue(0)
                bar.setFormat(f"{activity.requests} requests (no % data)")
                bar.setStyleSheet("")
                detail.setText(f"{activity.sessions} sessions")
        else:
            for index, (label, bar, detail) in enumerate(rows):
                window = windows[index] if index < len(windows) else None
                if not window:
                    if index == optional_index:
                        self.set_usage_row_visible(rows[index], False)
                        continue
                    bar.setFormat("Unavailable")
                    detail.setText("")
                    continue
                if index == optional_index:
                    self.set_usage_row_visible(rows[index], True)
                label.setText(window.name)
                remaining = 100 - window.used_percent
                bar.setValue(remaining)
                bar.setFormat(f"{remaining}% left ({window.used_percent}% used)")
                pace = usage_pace_text(window)
                detail.setText(f"{window.resets}\n{pace}" if pace else window.resets)
                color = (
                    "#3da35d"
                    if remaining > 40
                    else "#d69e2e" if remaining > 15 else "#d9534f"
                )
                bar.setStyleSheet(
                    "QProgressBar { text-align: center; } "
                    f"QProgressBar::chunk {{ background-color: {color}; }}"
                )
    @staticmethod
    def set_usage_row_visible(
        row: tuple[QLabel, QProgressBar, QLabel], visible: bool
    ) -> None:
        for widget in row:
            widget.setVisible(visible)

    def refresh_all(self) -> None:
        self.refresh()
        self.refresh_usage()

    def effective_model(self, session_key: str | None) -> str | None:
        """The ANTHROPIC_MODEL a session would (re)launch with, if any is set.

        Same global-then-session-override precedence as launch_env, just
        surfacing the one value rather than merging a whole environment -
        this is what the Model column (main listview and group dialog alike)
        displays.
        """
        global_env = self.settings().get("global_env") or {}
        overrides: dict = {}
        if session_key:
            overrides = (
                (self.metadata.get("sessions") or {}).get(session_key) or {}
            ).get("env") or {}
        return overrides.get("ANTHROPIC_MODEL") or global_env.get("ANTHROPIC_MODEL") or None

    def populate_session_table(
        self, table: QTableWidget, sessions: list[Session], columns: tuple[str, ...]
    ) -> None:
        """Fill a QTableWidget's shared columns from `columns`.

        The common rendering both the main listview and ManageGroupDialog build
        on, so only their differences (extra columns, extra rows) need writing
        out separately.
        """
        colors = {"Codex": "#5aa9ff", "Claude": "#d977ff", "Antigravity": "#42d6c5"}
        table.setSortingEnabled(False)
        table.setRowCount(len(sessions))
        for row, session in enumerate(sessions):
            for col, column in enumerate(columns):
                if column == "Agent":
                    item = QTableWidgetItem(session.provider)
                    item.setForeground(QColor(colors.get(session.provider, "#ffffff")))
                elif column == "Model":
                    item = QTableWidgetItem(self.effective_model(session.key) or "Default")
                elif column == "Name":
                    item = QTableWidgetItem(session.title)
                elif column == "Working directory":
                    item = QTableWidgetItem(session.cwd)
                elif column == "Last updated":
                    item = QTableWidgetItem(
                        datetime.fromtimestamp(session.updated_ms / 1000).strftime(
                            "%Y-%m-%d %H:%M"
                        )
                        if session.updated_ms
                        else ""
                    )
                    item.setData(Qt.ItemDataRole.UserRole, session.updated_ms)
                else:
                    item = QTableWidgetItem(session.session_id)
                item.setData(Qt.ItemDataRole.UserRole + 1, session.key)
                table.setItem(row, col, item)
        table.setSortingEnabled(True)

    def refresh(self) -> None:
        self.metadata = read_metadata()
        self.sessions = discover_sessions(self.metadata)
        self.populate_session_table(self.table, self.sessions, self.SESSION_TABLE_COLUMNS)
        self.table.sortItems(
            self.SESSION_TABLE_COLUMNS.index("Last updated"), Qt.SortOrder.DescendingOrder
        )
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

    def launch_env(
        self, session_key: str | None = None, strip: list[str] | None = None
    ) -> dict[str, str] | None:
        """Merge global + per-session env overrides onto the current process env.

        Returns None when nothing is configured or stripped so the launched
        process simply inherits Session Hub's environment as-is. Per-session
        values win over global. `strip` removes inherited keys outright (e.g.
        CLAUDE_CODE_CHILD_SESSION, which Session Hub itself may have inherited
        if it was launched from inside another Claude session, and which
        disables transcript saving in whatever it's launched into).
        """
        global_env = self.settings().get("global_env") or {}
        overrides: dict = {}
        if session_key:
            overrides = (
                (self.metadata.get("sessions") or {}).get(session_key) or {}
            ).get("env") or {}
        combined: dict[str, str] = {}
        for source in (global_env, overrides):
            for name, value in source.items():
                if str(name).strip():
                    combined[str(name)] = str(value)
        if not combined and not strip:
            return None
        result = {**os.environ, **combined}
        for key in strip or []:
            result.pop(key, None)
        return result

    def launch_flags(
        self, session_key: str | None = None, extra: dict | None = None
    ) -> list[str]:
        """Merge global + per-session CLI flag overrides into argv fragments.

        Per-session values win over global, matching launch_env's precedence.
        `extra` wins over both -- it carries a one-off choice made in the launch
        dialog, which has no session key yet because the session does not exist.
        """
        global_flags = self.settings().get("global_flags") or {}
        overrides: dict = {}
        if session_key:
            overrides = (
                (self.metadata.get("sessions") or {}).get(session_key) or {}
            ).get("flags") or {}
        combined: dict[str, str] = {}
        for source in (global_flags, overrides, extra or {}):
            for name, value in source.items():
                if str(name).strip():
                    combined[str(name)] = str(value)
        argv: list[str] = []
        for name, value in combined.items():
            if name == "--caveman":
                # A Session Hub pseudo-flag: no agent CLI has this option, so it
                # expands here rather than being passed through. An off/unknown
                # value expands to nothing at all, never to a bare flag.
                prompt = caveman_system_prompt(value)
                if prompt:
                    argv += ["--append-system-prompt", prompt]
            elif CLI_FLAG_SPECS.get(name, {}).get("kind") == "flag":
                argv += [name]
            else:
                argv += [name, value]
        return argv

    def spawn(
        self,
        command: list[str],
        session_key: str | None = None,
        *,
        pidfile: Path | None = None,
        cwd: str | None = None,
        session_id: str | None = None,
        focus: bool = True,
        strip_env: list[str] | None = None,
        wait_for_tracking: bool = False,
    ) -> None:
        subprocess.Popen(
            command,
            start_new_session=True,
            env=self.launch_env(session_key, strip=strip_env),
        )
        title = next(
            (arg[len("--title="):] for arg in command if arg.startswith("--title=")),
            None,
        )
        if title and focus:
            threading.Thread(
                target=focus_window_by_title, args=(title,), daemon=True
            ).start()
        if pidfile is not None and cwd is not None:
            if wait_for_tracking:
                # The GUI stays running long after this returns, so the
                # normal path leaves PID capture to a daemon thread in the
                # background. A one-shot CLI invocation has no "background"
                # - the process exits right after this call - so it must
                # block here or the daemon thread gets killed mid-capture
                # and the launch silently never becomes /clear-trackable.
                capture_hub_launch(pidfile, cwd, session_id)
            else:
                threading.Thread(
                    target=capture_hub_launch,
                    args=(pidfile, cwd, session_id),
                    daemon=True,
                ).start()

    def edit_session_launch_options(self) -> None:
        session = self.selected()
        if not session:
            return
        self.edit_session_launch_options_for(session)

    def edit_session_launch_options_for(self, session: Session) -> None:
        existing = (self.metadata.get("sessions") or {}).get(session.key, {})
        env_overrides = existing.get("env") or {}
        flag_overrides = existing.get("flags") or {}
        dialog = SessionLaunchOptionsDialog(
            session.title,
            self.settings().get("global_env") or {},
            env_overrides,
            self.settings().get("global_flags") or {},
            flag_overrides,
            self,
        )
        if dialog.exec() == QDialog.DialogCode.Accepted:
            entry = self.metadata.setdefault("sessions", {}).setdefault(
                session.key, {}
            )
            env = dialog.env()
            if env:
                entry["env"] = env
            else:
                entry.pop("env", None)
            flags = dialog.flags()
            if flags:
                entry["flags"] = flags
            else:
                entry.pop("flags", None)
            write_metadata(self.metadata)
            self.refresh()

    def rename_selected(self) -> None:
        session = self.selected()
        if not session:
            return
        self.rename_session(session)

    def rename_session(self, session: Session) -> None:
        name, accepted = QInputDialog.getText(
            self, "Rename session", "Display name:", text=session.title
        )
        if accepted and name.strip():
            self.save_override(session, "name", name.strip())

    def change_directory(self) -> None:
        session = self.selected()
        if not session:
            return
        self.change_directory_for(session)

    def change_directory_for(self, session: Session) -> None:
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
        model: str | None = None,
        flags: list[str] | None = None,
        pidfile: Path | None = None,
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
            claude_args = [executable("claude")]
            if self.settings().get("claude_danger_mode", False):
                claude_args += ["--dangerously-skip-permissions"]
            if model:
                claude_args += ["--model", model]
            claude_args += flags or []
            if session_id:
                claude_args += ["--resume", session_id]
                if Path(launch_cwd) != Path(cwd):
                    claude_args += [f"/cd {cwd}"]
            if pidfile is not None:
                command += pid_capture_command(pidfile, claude_args)
            else:
                command += claude_args
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
        model: str | None = None,
        session_key: str | None = None,
        flag_overrides: dict | None = None,
        focus: bool = True,
        strip_env: list[str] | None = None,
        wait_for_tracking: bool = False,
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
            flags = (
                self.launch_flags(session_key, flag_overrides)
                if provider == "Claude"
                else []
            )
            pidfile = new_pid_capture_file() if provider == "Claude" else None
            self.spawn(
                self.terminal_command(
                    provider, session_id, cwd, source_cwd, model, flags, pidfile
                ),
                session_key,
                pidfile=pidfile,
                cwd=cwd,
                session_id=session_id,
                focus=focus,
                strip_env=strip_env,
                wait_for_tracking=wait_for_tracking,
            )
        except (OSError, RuntimeError) as error:
            QMessageBox.critical(self, "Could not launch session", str(error))

    def resume_selected(self) -> None:
        session = self.selected()
        if not session:
            return
        self.resume_session(session)

    def resume_session(self, session: Session) -> None:
        if self.is_group_session(session):
            self.manage_group()
            return
        self.launch(
            session.provider,
            session.session_id,
            session.cwd,
            session.source_cwd,
            session_key=session.key,
        )

    def is_group_session(self, session: Session) -> bool:
        return session.session_id.startswith("group:")

    def manage_group(self) -> None:
        session = self.selected()
        if not session or not self.is_group_session(session):
            return
        if session.cwd not in self.metadata.get("groups", {}):
            return
        dialog = ManageGroupDialog(self, session.cwd, self)
        dialog.exec()

    def rename_group(self) -> None:
        session = self.selected()
        if not session or not self.is_group_session(session):
            return
        group = self.metadata.get("groups", {}).get(session.cwd)
        if not group:
            return
        current = group.get("display_name") or Path(session.cwd).name or session.cwd
        new_name, accepted = QInputDialog.getText(
            self, "Rename group", "Display name:", text=current
        )
        new_name = new_name.strip()
        if not accepted or not new_name:
            return
        group["display_name"] = new_name
        write_metadata(self.metadata)
        self.refresh()

    def delete_group(self) -> None:
        session = self.selected()
        if not session or not self.is_group_session(session):
            return
        cwd = session.cwd
        group = self.metadata.get("groups", {}).get(cwd)
        if not group:
            return
        name = group.get("display_name") or Path(cwd).name or cwd
        answer = QMessageBox.warning(
            self,
            "Delete group?",
            f"Move every session in “{name}” to Session Hub's trash and "
            "delete the group?",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        live_sessions = claude_sessions()
        failures = []
        for row in group.get("rows", []):
            live = find_group_member_session(row, cwd, live_sessions)
            if live:
                try:
                    self.move_session_to_trash(live)
                except OSError as error:
                    failures.append(f"{row['name']}: {error}")
        self.metadata.get("groups", {}).pop(cwd, None)
        write_metadata(self.metadata)
        self.refresh()
        if failures:
            QMessageBox.critical(
                self, "Some sessions could not be deleted", "\n".join(failures)
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
        self.open_linked_conversation_for(session)

    def open_linked_conversation_for(self, session: Session) -> None:
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
            session_key=conversation.key,
        )

    def link_to_existing_conversation(self) -> None:
        session = self.selected()
        if not session:
            return
        self.link_to_existing_conversation_for(session)

    def link_to_existing_conversation_for(self, session: Session) -> None:
        """Manually mark `session` as the continuation of an older one.

        Covers restarts Session Hub has no automatic way to notice - a
        `/clear` (or crash-and-restart) in a process it didn't itself launch,
        so resolve_clear_continuations' PID tracking never saw it happen.
        Writes into the same metadata["links"] structure that mechanism uses,
        so "Open linked conversation…" and (for a session that happens to be
        a saved group's row) group re-matching both pick it up for free - see
        find_group_member_session's session_key check.
        """
        candidates = sorted(
            (
                other
                for other in native_session_index().values()
                if other.native_key != session.native_key and other.cwd == session.cwd
            ),
            key=lambda item: item.updated_ms,
            reverse=True,
        )
        if not candidates:
            QMessageBox.information(
                self,
                "Link to existing conversation",
                "No other conversations were found in this session's working directory.",
            )
            return
        labels = []
        for item in candidates:
            title = item.title or item.session_id[:8]
            if len(title) > 60:
                title = title[:57] + "…"
            labels.append(f"{item.provider} — {title}  [{item.session_id[:8]}]")

        selected_label, accepted = QInputDialog.getItem(
            self,
            "Link to existing conversation",
            "This session continues:",
            labels,
            0,
            False,
        )
        if not accepted:
            return
        target = candidates[labels.index(selected_label)]
        old_key, new_key = target.native_key, session.native_key
        # target.title already went through native_session_index()'s own
        # override resolution, so this is the old session's real display
        # name whether that came from an explicit rename or just its own
        # auto-generated title. link_continuation keys the copied name onto
        # the new session's own native key (not the link id): ManageGroupDialog
        # resolves a group row's title by native key too and has no idea the
        # link even exists.
        link_continuation(self.metadata, old_key, new_key, target.title, "manual")
        write_metadata(self.metadata)
        self.refresh()

    def handoff_terminal_command(
        self,
        target_provider: str,
        cwd: str,
        handoff_path: Path,
        title: str,
        target_session_id: str | None = None,
        resume_existing: bool = False,
        source_cwd: str | None = None,
        flags: list[str] | None = None,
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
            command += flags or []
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
            command += self.launch_flags(session.key)
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
        self.prepare_handoff_summary_for(session)

    def prepare_handoff_summary_for(self, session: Session) -> None:
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
        if existing:
            path.unlink(missing_ok=True)
        try:
            self.spawn(self.summary_terminal_command(session), session.key)
        except (OSError, RuntimeError) as error:
            QMessageBox.critical(
                self, "Could not prepare handoff summary", str(error)
            )

    def continue_with_other_agent(self) -> None:
        session = self.selected()
        if not session:
            return
        self.continue_with_other_agent_for(session)

    def continue_with_other_agent_for(self, session: Session) -> None:
        settings = self.settings()
        targets = [
            provider for provider in PROVIDERS
            if provider != session.provider and bool(settings.get(f"enable_{provider.lower()}", True))
        ]
        if not targets:
            QMessageBox.information(
                self,
                "Continue with other agent",
                "There are no other enabled agents to continue with. "
                "You can enable more agents in Settings.",
            )
            return
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
                    flags=self.launch_flags(existing_target.key)
                    if target == "Claude"
                    else None,
                )
            elif target == "Claude":
                target_id = str(uuid.uuid4())
                target_key = f"Claude:{target_id}"
                link["members"].append(target_key)
                link["active"] = target_key
                command = self.handoff_terminal_command(
                    target,
                    session.cwd,
                    handoff,
                    session.title,
                    target_id,
                    flags=self.launch_flags(target_key),
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
            self.spawn(command, session.key)
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
            self.launch(
                provider,
                None,
                str(dialog.directory),
                model=dialog.model,
                flag_overrides=dialog.flag_overrides(),
            )

    def launch_selected_provider(self) -> None:
        self.launch_new(self.new_provider.currentText())

    def launch_new_group(self) -> None:
        dialog = NewSessionGroupDialog(self.settings(), self)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.directory:
            return
        directory = str(dialog.directory)
        group = self.metadata.setdefault("groups", {}).setdefault(
            directory, {"cwd": directory, "rows": []}
        )
        existing_names = {row["name"] for row in group["rows"]}
        for row in dialog.group_rows:
            if row["name"] in existing_names:
                continue
            registered = self.register_group_row(directory, row["name"], row.get("model"))
            self.launch(
                "Claude",
                None,
                directory,
                session_key=registered["override_key"],
                flag_overrides={"--name": registered["name"]},
                focus=False,
            )
            group["rows"].append(registered)
            existing_names.add(registered["name"])
        write_metadata(self.metadata)
        self.refresh()

    def register_group_row(self, cwd: str, name: str, model_alias: str | None) -> dict:
        """Build a saved group row, minting its durable override key.

        `override_key` is a synthetic session key that exists purely so a
        not-yet-launched row still has somewhere to store a model/env/flag
        override, the same way a real session's own `session.key` does - it
        never changes for the life of the row, whether or not it's currently
        matched to a live session (see find_group_member_session, ManageGroupDialog).
        """
        override_key = f"group:{cwd}#{name}"
        if model_alias:
            entry = self.metadata.setdefault("sessions", {}).setdefault(override_key, {})
            entry.setdefault("env", {})["ANTHROPIC_MODEL"] = model_alias
        return {"name": name, "override_key": override_key}

    def launch_group_row(
        self, cwd: str, name: str, *, wait_for_tracking: bool = False
    ) -> dict:
        """Launch (or report already-running) a single saved group row.

        Shared by ManageGroupDialog's per-row Launch button and the
        --launch-group-row CLI flag, so an orchestrator session's own Bash
        tool can bring up its group-mates through the same tracked launch
        path (PID capture for /clear detection, launch_env/launch_flags
        overrides) a GUI click uses - not a bypass that skips tracking.
        """
        group = self.metadata.get("groups", {}).get(cwd)
        if not group:
            return {"status": "error", "message": f"No session group for {cwd}"}
        row = next((r for r in group.get("rows", []) if r["name"] == name), None)
        if not row:
            return {"status": "error", "message": f"No row named {name!r} in this group"}
        live = find_group_member_session(row, cwd, claude_sessions())
        if live and session_is_tracked_alive(live):
            return {"status": "already_running", "session_id": live.session_id}
        strip_env = (
            ["CLAUDE_CODE_CHILD_SESSION"] if row.get("transcripts", True) else None
        )
        self.launch(
            "Claude",
            None,
            cwd,
            session_key=row["override_key"],
            flag_overrides={"--name": row["name"]},
            strip_env=strip_env,
            wait_for_tracking=wait_for_tracking,
        )
        return {"status": "launched", "name": name}

    def add_session_to_group(self) -> None:
        session = self.selected()
        if not session:
            return
        self.add_session_to_group_for(session)

    def add_session_to_group_for(self, session: Session) -> None:
        group = self.metadata.get("groups", {}).get(session.cwd)
        if not group:
            QMessageBox.information(
                self,
                "No group for this session",
                "This session isn't part of a saved group. Use "
                "“New session group…” first to create one.",
            )
            return
        dialog = AddGroupSessionDialog(group, self)
        if dialog.exec() != QDialog.DialogCode.Accepted or not dialog.row:
            return
        row = self.register_group_row(
            group["cwd"], dialog.row["name"], dialog.row.get("model")
        )
        self.launch(
            "Claude",
            None,
            group["cwd"],
            session_key=row["override_key"],
            flag_overrides={"--name": row["name"]},
        )
        group["rows"].append(row)
        write_metadata(self.metadata)
        self.refresh()

    def delete_session(self, session: Session) -> None:
        answer = QMessageBox.warning(
            self,
            "Move session to Session Hub trash?",
            f"{session.title}\n\nThe history file will be moved to Session Hub's "
            "recoverable trash. Close agents currently using this session first.",
            QMessageBox.StandardButton.Cancel | QMessageBox.StandardButton.Yes,
            QMessageBox.StandardButton.Cancel,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            self.move_session_to_trash(session)
        except OSError as error:
            QMessageBox.critical(self, "Could not delete", str(error))
            return
        write_metadata(self.metadata)
        self.refresh()

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

    def context_menu_actions(
        self, session: Session | None = None
    ) -> list[tuple[str, object]]:
        """The (label, slot) pairs for a session's right-click menu.

        `session=None` (the main listview's own context menu) binds slots to
        the `*_selected` methods, which read `self.selected()` off the main
        table at click time - unchanged behavior. Passing an explicit
        `session` (ManageGroupDialog, building a menu for one of its own
        rows) binds slots to the `*_for`/`*_session` variants instead, so the
        action runs against that exact session regardless of what (if
        anything) is selected in the main table.
        """
        target = session if session is not None else self.selected()
        if target and self.is_group_session(target):
            return [
                ("Manage group…", self.manage_group),
                ("Rename group", self.rename_group),
                ("Delete group", self.delete_group),
            ]
        settings = self.settings()
        multiple_agents = sum(
            bool(settings.get(f"enable_{provider.lower()}", True))
            for provider in PROVIDERS
        ) > 1

        def bound(no_arg_slot, for_session_slot):
            return (lambda: for_session_slot(session)) if session is not None else no_arg_slot

        actions = [
            ("Resume in new terminal", bound(self.resume_selected, self.resume_session)),
            (
                "Open linked conversation…",
                bound(self.open_linked_conversation, self.open_linked_conversation_for),
            ),
            (
                "Link to existing conversation…",
                bound(
                    self.link_to_existing_conversation,
                    self.link_to_existing_conversation_for,
                ),
            ),
            (
                "Prepare handoff summary",
                bound(self.prepare_handoff_summary, self.prepare_handoff_summary_for),
            ),
            (
                "Continue with other agent",
                bound(self.continue_with_other_agent, self.continue_with_other_agent_for),
            ),
            ("Rename", bound(self.rename_selected, self.rename_session)),
            ("Change directory", bound(self.change_directory, self.change_directory_for)),
            (
                "Launch options…",
                bound(
                    self.edit_session_launch_options,
                    self.edit_session_launch_options_for,
                ),
            ),
            (
                "Add session to group…",
                bound(self.add_session_to_group, self.add_session_to_group_for),
            ),
            ("Delete", bound(self.delete_selected, self.delete_session)),
        ]
        if not multiple_agents:
            actions = [
                (label, slot)
                for label, slot in actions
                if label not in ("Prepare handoff summary", "Continue with other agent")
            ]
        return actions

    def context_menu(self, point) -> None:
        if self.table.itemAt(point) is None:
            return
        menu = QMenu(self)
        for label, slot in self.context_menu_actions():
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


def launch_group_row_cli(argv: list[str]) -> int:
    """Headless `--launch-group-row <cwd> <name>`, for an orchestrator's own Bash tool.

    Builds a real (but never shown) SessionHub so the launch goes through
    the exact same tracked path (PID capture, launch_env/launch_flags
    overrides) a GUI click uses - see SessionHub.launch_group_row. Blocks
    briefly (wait_for_tracking) since this process exits right after, unlike
    the GUI where a background daemon thread has the app's whole lifetime to
    finish capturing the launched PID.
    """
    try:
        index = argv.index("--launch-group-row")
        cwd, name = argv[index + 1], argv[index + 2]
    except (ValueError, IndexError):
        print(json.dumps({
            "status": "error",
            "message": "usage: session_hub.py --launch-group-row <cwd> <name>",
        }))
        return 1
    app = QApplication.instance() or QApplication(argv[:1])
    window = SessionHub()
    result = window.launch_group_row(cwd, name, wait_for_tracking=True)
    print(json.dumps(result))
    return 0 if result.get("status") != "error" else 1


def main() -> int:
    if "--diagnose" in sys.argv:
        return diagnostic()
    if "--launch-group-row" in sys.argv:
        return launch_group_row_cli(sys.argv)
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
